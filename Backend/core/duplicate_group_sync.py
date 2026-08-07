"""
Persist duplicate-title semantics on DiscTitle rows (Option B).

- Duplicate group: same disc_id + normalized segment_map (see duplicate_info._normalize_segment_map).
- Exactly one primary per group (active=True); all other rows are type='ignore' with user labeling fields cleared.
  Per-title `comment` (output filename / file identity) is not cleared and is not swapped on primary change.

Pipeline alignment: postprocess rename paths in core.disc and stage_validation already skip type='ignore'
titles; DiscDB summary export (_write_disc_summary) omits ignore rows while full disc JSON still lists them.
Rip still follows scan/job title lists; duplicate secondaries remain separate source_file rows until/unless
the rip pipeline filters by type.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from api import models as db_models
from core.duplicate_info import _comparative_metrics, _normalize_segment_map

log = logging.getLogger(__name__)

# Labeling fields copied on primary swap and cleared on secondaries. Excludes `comment` (file identity).
DUPLICATE_LABEL_METADATA_FIELDS: tuple[str, ...] = (
    "title",
    "type",
    "season",
    "episode",
    "edition",
    "description",
)

SECONDARY_IGNORE_TYPE = "ignore"


def _fold_subsumed_m2ts_into_mpls_group(
    all_titles: list[db_models.DiscTitle],
    groups: dict[str, list[db_models.DiscTitle]],
) -> None:
    """Add subsumed m2ts titles to their wrapping mpls's segment_map group.

    Uses ``core.path_b_dedupe.compute_mpls_clip_index`` for source_file parsing
    + clip-id-in-segment_map matching + tiebreak (smallest mpls index wins when
    multiple wrappers reference the same clip). Mutates ``groups`` in place.
    Idempotent — if an m2ts is already in its wrapper's group by exact
    segment_map match (shouldn't happen structurally, but defensive), it's not
    added a second time.
    """
    from core.path_b_dedupe import compute_mpls_clip_index

    titles_by_id_dict: dict[str, dict] = {
        str(t.id): {
            "source_file": t.source_file,
            "segment_map": t.segment_map,
            "index": t.index,
        }
        for t in all_titles
    }
    clip_index = compute_mpls_clip_index(titles_by_id_dict)
    if not clip_index:
        return

    by_id = {str(t.id): t for t in all_titles}
    for m2ts_id, wrapper_id in clip_index.items():
        m2ts = by_id.get(m2ts_id)
        wrapper = by_id.get(wrapper_id)
        if m2ts is None or wrapper is None:
            continue
        wrapper_key = _normalize_segment_map(wrapper.segment_map)
        if wrapper_key is None:
            continue
        members = groups.setdefault(wrapper_key, [])
        if wrapper not in members:
            members.append(wrapper)
        if m2ts not in members:
            members.append(m2ts)


def _disc_title_metrics_payload(t: db_models.DiscTitle) -> dict[str, Any]:
    meta = t.metadata_scan
    if not isinstance(meta, dict):
        meta = {}
    ch = t.chapters
    if not isinstance(ch, dict):
        ch = {}
    streams = t.streams
    if streams is not None and not isinstance(streams, (list, str)):
        streams = None
    return {
        "metadata_scan": meta,
        "streams": streams,
        "chapters": ch,
        "size": t.size,
        "mkv_size": t.mkv_size,
    }


def pick_primary_duplicate_row(members: list[db_models.DiscTitle]) -> db_models.DiscTitle:
    """**Tiebreaker only — never use as a grouping signal.**

    Given a list of disc_titles already known to be members of one
    duplicate group (same `_normalize_segment_map` order-preserving
    key), pick which row should be the primary. Heuristic ranks by
    active flag, then audio_score + chapters + size, with a
    prefer-mpls bias inside a 5% tie window.

    This function is invoked from `sync_duplicate_group_labels_for_disc`
    *after* the group has been formed by `_normalize_segment_map` (or,
    in Path B's case, by `_segment_set_key`). The grouping is
    deterministic on segment composition; this picker only chooses
    among already-grouped rows.

    Do not call this to *decide* whether two titles are duplicates —
    the FFmpeg heuristics (audio_score, chapters_count, size) are
    unreliable as a grouping signal because two unrelated titles can
    score similarly. Per the saved memory `feedback_dedupe_authority`,
    sorted-segment-set equivalence is the only authority on what
    constitutes a duplicate. See `core.path_b_dedupe.compute_dedupe_groups`
    for the canonical grouping path used by the workflow context.

    Choose primary: single active=True wins; tie-break multiple actives;
    else auto-score like attach_duplicate_info.
    """
    if not members:
        raise ValueError("empty duplicate group")
    if len(members) == 1:
        return members[0]

    actives = [t for t in members if t.active is True]
    if len(actives) == 1:
        return actives[0]
    if len(actives) > 1:
        return sorted(
            actives,
            key=lambda t: (t.order_index is None, t.order_index if t.order_index is not None else 0, t.id or ""),
        )[0]

    metrics_by_id: dict[str, dict[str, Any]] = {}
    for t in members:
        tid = str(t.id)
        metrics_by_id[tid] = _comparative_metrics(_disc_title_metrics_payload(t))

    def _score(t: db_models.DiscTitle) -> float:
        m = metrics_by_id.get(str(t.id)) or {}
        p = _disc_title_metrics_payload(t)
        audio = (m.get("audio_score") or 0) * 3
        chapters = m.get("chapters_count") or 0
        size = p.get("size") or p.get("mkv_size") or 0
        size_gb = size / (1024**3) if size else 0
        return audio + chapters + size_gb

    def _is_mpls(t: db_models.DiscTitle) -> bool:
        return (t.source_file or "").lower().endswith(".mpls")

    def _size_int(t: db_models.DiscTitle) -> int:
        p = _disc_title_metrics_payload(t)
        s = p.get("size") or p.get("mkv_size") or 0
        return int(s) if s else 0

    # Prefer-mpls tiebreaker: when the top score has near-equals (within 5%),
    # prefer .mpls over .m2ts. Handles V-for-Vendetta UHD / D&D Honor Among
    # Thieves where an mpls and its underlying m2ts share segment_map; the
    # mpls is the "intended" playlist and carries chapter markers. For
    # disparate-size cases (e.g. main feature vs short extra) the 5% window
    # leaves only the dominant winner anyway.
    top_score = max(_score(t) for t in members)
    threshold = top_score * 0.95 if top_score > 0 else top_score
    leaders = [t for t in members if _score(t) >= threshold]
    leaders.sort(key=lambda t: (_is_mpls(t), _size_int(t)), reverse=True)
    return leaders[0]


# (dead helper removed: _clear_label_metadata_fields had no callers and its
# raw setattr predates the provenance split — apply_secondary_duplicate_row
# below is the only clearing path and routes through set_title_field.)


def user_claimed_row(title: db_models.DiscTitle) -> bool:
    """True when the user gave this row a real type of their own.

    ``user_type`` set to anything other than ``ignore`` is the user saying
    "I want this title on its own", and it is the only signal needed — the
    provenance columns already record it. Used to stop the auto-demotion
    below from deactivating a row the user deliberately claimed.

    The motivating case is a play-all ``.mpls`` wrapping several ``.m2ts``
    clips (#797): the clips get ``auto_type='ignore'`` from subsumption,
    the user types ``BehindTheScenes`` on each, resolution correctly yields
    ``BehindTheScenes`` — and then ``active=False`` hid them anyway, so
    they could never be ripped.
    """
    user_type = (str(getattr(title, "user_type", None) or "")).strip().lower()
    return bool(user_type) and user_type != SECONDARY_IGNORE_TYPE


def apply_secondary_duplicate_row(title: db_models.DiscTitle) -> bool:
    """Clear labeling metadata, auto-ignore, deactivate. Returns True if any column changed.

    Must be IDEMPOTENT: this runs after *every* title patch, and each True
    return bumps the row's ``title_seq``. A pass that "changes" an
    already-demoted row inflates sibling seqs on every edit, guaranteeing
    the client's seq cache is stale by its next write — which is rejected
    as a conflict, whose recovery then wipes the label form (#775). Two
    prior non-idempotencies, both hit in prod:

    - ``type`` sat in the metadata-clear loop, so every pass nulled it
      directly (the raw-``setattr`` cache drift ``set_title_type``'s
      docstring warns about) and the guard below re-set it: changed=True
      forever. ``type`` is now the guard's job alone.
    - The guard compared the EFFECTIVE type, which a user's ``user_type``
      wins by the resolution rule — so a row the user had typed on
      re-fired the guard on every pass. It now compares ``auto_type``,
      the only channel this auto-derived demotion writes.
    """
    changed = False
    from api.crud import set_title_field
    for field in DUPLICATE_LABEL_METADATA_FIELDS:
        if field == "type":
            continue  # provenance-managed below; never raw-setattr (#775)
        # This demotion is automation, so it clears the AUTO opinion only —
        # a secondary the user hand-labeled keeps its user value through
        # resolution (user ?? auto). The changed-check reads the auto
        # column for the same reason the type guard reads auto_type
        # (documented above): comparing the resolved value on a row with
        # a user override would report "changed" on every pass and
        # re-inflate sibling seqs forever.
        #
        # A provenance-orphaned value (resolved set, BOTH source columns
        # NULL — a raw write that bypassed set_title_field) counts as
        # automation's, matching the backfill rule: unknown provenance
        # defaults to auto. Clearing via the helper leaves resolved None,
        # so the check can't re-fire on the next pass.
        has_auto = getattr(title, f"auto_{field}", None) is not None
        orphaned = (
            getattr(title, field, None) is not None
            and getattr(title, f"user_{field}", None) is None
            and not has_auto
        )
        if has_auto or orphaned:
            # The helper recomputes resolved = user ?? auto, which is None
            # in both branches — the orphan cache clears through it too.
            set_title_field(title, field, None, source="auto")
            changed = True
    auto_lower = (str(title.auto_type).strip().lower() if getattr(title, "auto_type", None) else "")
    if auto_lower != SECONDARY_IGNORE_TYPE:
        # Sibling-of-primary auto-ignore — derived from the duplicate
        # group consensus, not the user. source='auto'. A user_type on
        # this row still wins the effective-type resolution, which is
        # the documented user-over-auto rule.
        from api.crud import set_title_type
        set_title_type(title, SECONDARY_IGNORE_TYPE, source="auto")
        changed = True
    # A row the user typed stays visible and rippable. Everything above is
    # deliberately unchanged: the auto-* columns still record automation's
    # opinion, and resolution (user ?? auto) still lets the user's type win
    # — this only stops `active` from overriding that decision.
    #
    # Both branches are idempotent, which matters: every True return bumps
    # title_seq, and an always-changed row re-inflates sibling seqs on each
    # pass until the client's next write is rejected as a conflict (#775).
    if user_claimed_row(title):
        if title.active is not True:
            title.active = True
            changed = True
    elif title.active is not False:
        title.active = False
        changed = True
    return changed


def apply_primary_duplicate_row(
    title: db_models.DiscTitle,
    *,
    group_members: list[db_models.DiscTitle] | None = None,
    fill_null_type_from_consensus: bool = True,
) -> bool:
    """Ensure the primary's active=True invariant. Returns True if changed.

    The previous "consensus-fill" — propagating type='ignore' onto a NULL
    primary when every sibling was already type='ignore' — was removed
    because the secondaries' ignore is set on the same pass by
    ``apply_secondary_duplicate_row`` (line ~138): their "vote" is
    circular and falsely hid every primary in a 2-row duplicate group
    even when no detector ever flagged the content. Surfaced on
    Fallout S2 where each episode's .mpls primary inherited ignore
    from its matching .m2ts secondary.

    ``_validate_all_titles_labeled`` already accepts NULL primary +
    ignored secondaries as a labeled state, and postprocess
    ``_rename_movie`` / ``_rename_series`` fall back to ``Track{tid}``
    for any title with no resolved name — both safe without the fill.

    ``group_members`` and ``fill_null_type_from_consensus`` are kept on
    the signature for backwards-compatibility with the per-patch sync
    call sites in ``api/routers/discs.py``; both are now no-ops and
    will be removed in a follow-up cleanup.
    """
    changed = False
    if title.active is not True:
        title.active = True
        changed = True
    return changed


def _bump_title_seq(title: db_models.DiscTitle) -> None:
    title.title_seq = (title.title_seq or 0) + 1


def _bump_disc_titles_version(db: Session, disc_id: str) -> None:
    disc = db.query(db_models.Disc).filter(db_models.Disc.id == disc_id).first()
    if not disc:
        return
    label_draft = disc.label_draft if isinstance(disc.label_draft, dict) else {}
    v = label_draft.get("titles_version")
    try:
        n = int(v) if v is not None else 0
    except (TypeError, ValueError):
        n = 0
    new_ld = {**label_draft, "titles_version": n + 1}
    disc.label_draft = new_ld
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(disc, "label_draft")
    except Exception:
        pass


def demote_duplicate_secondaries_in_group(
    group_titles: list[db_models.DiscTitle],
    *,
    primary_id: str,
    fill_null_type_from_consensus: bool = True,
    collect_modified: "list[db_models.DiscTitle] | None" = None,
) -> int:
    """
    Enforce invariant: primary_id is active=True; every other row in group is ignore + cleared metadata.
    Returns count of titles modified.

    Two-pass ordering: demote all secondaries first so `apply_primary_duplicate_row` sees
    post-demotion sibling state when filling a NULL primary type from group consensus.
    Pass fill_null_type_from_consensus=False from per-patch sync sites to avoid clobbering
    a user's just-cleared type (e.g. unignoring a primary whose sibling is still ignore).
    """
    modified = 0
    pid = str(primary_id)
    primary: db_models.DiscTitle | None = None
    for t in group_titles:
        if str(t.id) == pid:
            primary = t
            continue
        if apply_secondary_duplicate_row(t):
            _bump_title_seq(t)
            if collect_modified is not None:
                collect_modified.append(t)
            modified += 1
    if primary is not None:
        if apply_primary_duplicate_row(
            primary,
            group_members=group_titles,
            fill_null_type_from_consensus=fill_null_type_from_consensus,
        ):
            _bump_title_seq(primary)
            if collect_modified is not None:
                collect_modified.append(primary)
            modified += 1
    return modified


def sync_duplicate_group_labels_for_disc(
    db: Session,
    disc_id: str,
    *,
    fill_null_type_from_consensus: bool = True,
    collect_modified: "list[db_models.DiscTitle] | None" = None,
) -> int:
    """
    Load all disc_titles for disc; for each multi-title segment_map group, pick primary and demote secondaries.

    Returns number of title rows modified.

    fill_null_type_from_consensus: when True (default), a NULL-typed primary in a group whose
    other members are all 'ignore' has its type filled with 'ignore' too. Pass False from
    per-patch endpoints so a user-driven type clear (e.g. unignore toggle on a primary whose
    sibling is still ignore) is not immediately reverted by sibling consensus.
    """
    all_titles = (
        db.query(db_models.DiscTitle)
        .filter(db_models.DiscTitle.disc_id == disc_id)
        .all()
    )
    if len(all_titles) < 2:
        return 0

    groups: dict[str, list[db_models.DiscTitle]] = {}
    for t in all_titles:
        key = _normalize_segment_map(t.segment_map)
        if key is None:
            continue
        groups.setdefault(key, []).append(t)

    # #642 sub-3: m2ts wrapped by an mpls on the same disc belongs in the
    # wrapper's group. Exact segment_map match only groups mpls-vs-mpls
    # variants (e.g. Blu-Ray boxsets that publish the same movie under two
    # playlist forms). m2ts titles have segment_map='CID' (a single clip)
    # while their wrapping mpls has segment_map='CID1,CID2,...' — different
    # keys, so without folding here the m2ts stays a top-level entity the
    # user has to ignore manually. Fold via title-id keyed clip_index
    # (compute_mpls_clip_index handles source_file parsing + tiebreaks).
    _fold_subsumed_m2ts_into_mpls_group(all_titles, groups)

    total_modified = 0
    for _seg, members in groups.items():
        if len(members) <= 1:
            continue
        primary = pick_primary_duplicate_row(members)
        n = demote_duplicate_secondaries_in_group(
            members,
            primary_id=str(primary.id),
            fill_null_type_from_consensus=fill_null_type_from_consensus,
            collect_modified=collect_modified,
        )
        total_modified += n

    if total_modified:
        _bump_disc_titles_version(db, disc_id)
        log.debug(
            "sync_duplicate_group_labels_for_disc disc_id=%s modified=%s title rows",
            disc_id,
            total_modified,
        )
    return total_modified
