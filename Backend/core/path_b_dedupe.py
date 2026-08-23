"""
Path B — sorted-segment-set dedupe in the post-rip labeling UI.

Today's `core.duplicate_info.attach_duplicate_info` groups titles by
ORDER-PRESERVED segment_map (so "504,510,501" and "501,510,504" are NOT
siblings). That's right for V-for-Vendetta where two siblings genuinely
share the exact ordered list, but it's exactly wrong for the Midway case
where 200+ playlists reference the same 10 segments in different orders.

Path B adds a parallel sorted-segment-set grouping. Same-set siblings
collapse into one row in the labeling UI; the user picks one
representative without having to mark 199 others as ignore. The
representative is picked in this precedence:

    1. DiscDB-classified                  authoritative when present
    2. MakeMKV obfuscation flag clear     "Likely real" wins over flagged
    3. Heuristic                          (audio_score, chapters, size_gb)
                                          with the prefer-mpls tiebreaker

When DiscDB and the flag disagree (Midway-style: DiscDB says A but the
flag-clear sibling is B), `disagreement` is non-null on the group so the
frontend can render the side-by-side compare card from the UI prototype
instead of silently picking one.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from core.segment_identity import segment_maps_identify_content

_log = logging.getLogger(__name__)


# Per-process memo of the most recent (apply_obfuscation_reason / apply_subsumption)
# input signature per disc. The two `apply_*` helpers each fire a 200+-row SELECT
# even when nothing has changed since the last successful run; on a workflow
# context fetch hit twice per disc-card click that's two redundant SELECTs every
# navigation. We short-circuit when the input signature matches the last apply.
#
# Cleared by process restart. Safe to be a plain dict — a wrong-positive (skip
# when we should have written) would only delay the write until the next
# state-changing event, and a wrong-negative just pays the cost we used to.
_LAST_APPLY_SIG: dict[str, dict[str, str]] = {}


def _dedupe_input_signature(groups: Iterable["DedupeGroup"]) -> str:
    """Fingerprint the (rep_id, sibling_ids) shape of the computed groups.
    Stable across runs; changes when membership changes."""
    rows: list[tuple[str, tuple[str, ...]]] = []
    for g in groups:
        rep = str(getattr(g, "representative_title_id", "") or "")
        sibs = tuple(sorted(str(s) for s in (getattr(g, "sibling_title_ids", None) or [])))
        rows.append((rep, sibs))
    rows.sort()
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()[:16]


def _subsumption_input_signature(clip_index: Mapping[str, str]) -> str:
    """Fingerprint the (m2ts_id -> wrapper_id) mapping. Stable + cheap."""
    rows = sorted(clip_index.items())
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()[:16]

from core.segment_reorder import _segment_set_key


@dataclass
class DedupeGroup:
    """One Path B dedupe group, carried into the workflow-context payload.

    `representative_title_id` is the row that should remain visible in the
    label UI; sibling_title_ids collapse behind a "Show N grouped duplicates"
    disclosure. `representative_source` tells the UI which precedence tier
    won the pick — useful for explainer copy.
    """

    group_id: str  # e.g. "sortedseg:abc123de"
    sorted_segment_key: str
    duration_bucket_s: int  # rounded duration the members share
    representative_title_id: str
    representative_source: str  # "discdb" | "makemkv_flag" | "heuristic"
    sibling_title_ids: list[str] = field(default_factory=list)
    discdb_pick_id: str | None = None
    makemkv_flag_pick_id: str | None = None
    disagreement: dict | None = None

    def to_dict(self) -> dict:
        d = {
            "group_id": self.group_id,
            "sorted_segment_key": self.sorted_segment_key,
            "duration_bucket_s": self.duration_bucket_s,
            "representative_title_id": self.representative_title_id,
            "representative_source": self.representative_source,
            "sibling_title_ids": list(self.sibling_title_ids),
            "discdb_pick_id": self.discdb_pick_id,
            "makemkv_flag_pick_id": self.makemkv_flag_pick_id,
        }
        if self.disagreement is not None:
            d["disagreement"] = self.disagreement
        return d


def _is_discdb_classified(payload: dict) -> bool:
    """A title is DiscDB-classified when it has a non-default item type. The
    enrichment pass in disc_manager populates this from DiscDB; titles that
    DiscDB didn't classify keep type=null/'ignore'/no-meaningful-classification.
    Heuristic check: any non-empty type that isn't 'ignore' counts."""
    t = payload.get("type")
    if not t:
        return False
    return str(t).strip().lower() not in ("", "ignore", "junk")


def _is_makemkv_flag_real(payload: dict) -> bool:
    """The obfuscation flag is set on titles MakeMKV's BD-J emulator
    considers part of the suspected fake-playlist mass. "Real" = flag clear."""
    return not bool(payload.get("obfuscation_flag", False))


def _heuristic_score(payload: dict) -> tuple:
    """Score consistent with duplicate_group_sync.pick_primary_duplicate_row.
    Returns a key suitable for sort/max."""
    meta = payload.get("metadata_scan") or {}
    if not isinstance(meta, dict):
        meta = {}
    audio = (meta.get("audio_score") or 0) * 3
    chapters = meta.get("chapters_count") or 0
    size = payload.get("size") or payload.get("mkv_size") or 0
    size_gb = size / (1024**3) if size else 0
    is_mpls = (payload.get("source_file") or "").lower().endswith(".mpls")
    return (audio + chapters + size_gb, is_mpls, int(size) if size else 0)


def _pick_with_precedence(
    members: list[tuple[str, dict]],
) -> tuple[str, str, str | None, str | None]:
    """Return (representative_id, source, discdb_pick_id, makemkv_flag_pick_id).

    Precedence: DiscDB > MakeMKV-flag > heuristic. discdb_pick_id /
    makemkv_flag_pick_id stay populated even when they don't win the
    representative slot — caller compares them to detect disagreement.
    """
    discdb_picks = [tid for tid, p in members if _is_discdb_classified(p)]
    flag_picks = [tid for tid, p in members if _is_makemkv_flag_real(p)]

    discdb_pick_id = discdb_picks[0] if discdb_picks else None
    flag_pick_id = flag_picks[0] if flag_picks else None

    if discdb_pick_id is not None:
        return discdb_pick_id, "discdb", discdb_pick_id, flag_pick_id
    if flag_pick_id is not None:
        return flag_pick_id, "makemkv_flag", discdb_pick_id, flag_pick_id

    # Heuristic fallback. Mirrors duplicate_group_sync.pick_primary_duplicate_row:
    # within the top 5% score band, prefer .mpls over .m2ts, then larger size.
    scored = [(tid, p, _heuristic_score(p)) for tid, p in members]
    top = max(s[2][0] for s in scored)  # primary score component
    threshold = top * 0.95 if top > 0 else top
    leaders = [(tid, p, key) for tid, p, key in scored if key[0] >= threshold]
    # Sort leaders by (is_mpls desc, size desc); first wins.
    leaders.sort(key=lambda x: x[2][1:], reverse=True)
    return leaders[0][0], "heuristic", discdb_pick_id, flag_pick_id


def _payload_duration(payload: dict) -> float | None:
    """Read and coerce the title duration to seconds; None when missing/invalid."""
    dur = payload.get("duration")
    try:
        dur = float(dur) if dur is not None else None
    except (TypeError, ValueError):
        return None
    if dur is None or dur <= 0:
        return None
    return dur


def _split_by_duration_tolerance(
    title_ids: list[str],
    titles_by_id: dict[str, dict],
    tolerance_pct: float,
) -> list[list[str]]:
    """Within a sorted-segment-set bucket, partition titles into sub-groups
    whose durations are pairwise within `tolerance_pct` of each other.

    Sort by duration, then walk: any title within `tolerance_pct` of the
    cluster's running median joins the current cluster; otherwise it
    starts a new one. Titles with no duration form their own cluster
    (one each — better to under-group than to misidentify durations).
    """
    no_dur = [t for t in title_ids if _payload_duration(titles_by_id[t]) is None]
    has_dur = [t for t in title_ids if _payload_duration(titles_by_id[t]) is not None]
    has_dur.sort(key=lambda t: _payload_duration(titles_by_id[t]) or 0.0)

    clusters: list[list[str]] = []
    cluster: list[str] = []
    cluster_anchor: float | None = None
    for tid in has_dur:
        dur = _payload_duration(titles_by_id[tid]) or 0.0
        if cluster_anchor is None:
            cluster = [tid]
            cluster_anchor = dur
            continue
        # Compare against the anchor (smallest duration in current cluster);
        # if dur exceeds anchor by more than tolerance, start a new cluster.
        if dur <= cluster_anchor * (1.0 + tolerance_pct / 100.0):
            cluster.append(tid)
        else:
            clusters.append(cluster)
            cluster = [tid]
            cluster_anchor = dur
    if cluster:
        clusters.append(cluster)
    # Each no-duration title becomes its own (singleton) cluster.
    for tid in no_dur:
        clusters.append([tid])
    return clusters


def compute_dedupe_groups(
    titles_by_id: dict[str, dict],
    *,
    duration_tolerance_pct: float = 1.0,
    min_group_size: int = 2,
    disc_format: str | None = None,
) -> list[DedupeGroup]:
    """Compute Path B dedupe groups from a workflow-context titles dict.

    Args:
        titles_by_id: mapping title_id (UUID string) → payload dict. Each
            payload should carry segment_map, duration, type, obfuscation_flag,
            metadata_scan, source_file, size — anything missing degrades the
            heuristic gracefully.
        disc_format: the disc's recorded format. On DVD the segment map is
            the PGC-relative cell list — shape, not identity — so no groups
            are computed at all (#831; see core.segment_identity). None keeps
            the legacy (Blu-ray) behaviour.
        duration_tolerance_pct: titles within this % of each other count as
            same-duration. Defaults to 1% per the plan; the plan explicitly
            calls out that two cuts using same segments in completely
            different orders is rare-but-real and the tolerance keeps them
            apart when their runtimes diverge.
        min_group_size: skip singletons; only emit groups with at least
            this many members. 2 by default.

    Returns:
        List of DedupeGroup, ordered by descending group size (stable).
        Singletons, titles without segment_map, and titles the user has
        explicitly ungrouped are excluded.
    """
    if not segment_maps_identify_content(disc_format):
        return []
    by_seg: dict[str, list[str]] = {}
    for tid, payload in titles_by_id.items():
        if not isinstance(payload, dict):
            continue
        # Per-title escape hatch, mirroring `attach_duplicate_info` and
        # `fold_subsumption_into_groups`: Ungroup stamps
        # `force_independent_group=True`, and this is the pass that has to
        # honour it. `sibling_title_ids` is the only thing that hides a row
        # from the left rail, so a title left in its cluster here stays
        # collapsed no matter what the flag says — the button fired its
        # request and still looked broken (mkv-auto-release#8).
        if payload.get("force_independent_group"):
            continue
        seg_key = _segment_set_key(payload.get("segment_map"))
        if seg_key is None:
            continue
        by_seg.setdefault(seg_key, []).append(tid)

    groups: list[DedupeGroup] = []
    for seg_key, tids in by_seg.items():
        # Within each sorted-segment-set bucket, split by duration tolerance.
        clusters = _split_by_duration_tolerance(
            tids, titles_by_id, duration_tolerance_pct,
        )
        for cluster in clusters:
            if len(cluster) < min_group_size:
                continue
            members = [(tid, titles_by_id[tid]) for tid in cluster]
            rep_id, source, discdb_pick, flag_pick = _pick_with_precedence(members)
            siblings = [tid for tid in cluster if tid != rep_id]

            disagreement = None
            if discdb_pick is not None and flag_pick is not None and discdb_pick != flag_pick:
                disagreement = {
                    "discdb_pick_id": discdb_pick,
                    "makemkv_flag_pick_id": flag_pick,
                }

            # Anchor the duration bucket on the smallest member for stability.
            anchor_dur = min(
                (_payload_duration(titles_by_id[t]) or 0.0 for t in cluster),
                default=0.0,
            )
            seg_hash = hashlib.sha256(
                f"{seg_key}|{int(anchor_dur)}".encode()
            ).hexdigest()[:12]
            groups.append(DedupeGroup(
                group_id=f"sortedseg:{seg_hash}",
                sorted_segment_key=seg_key,
                duration_bucket_s=int(anchor_dur),
                representative_title_id=rep_id,
                representative_source=source,
                sibling_title_ids=sorted(siblings),
                discdb_pick_id=discdb_pick,
                makemkv_flag_pick_id=flag_pick,
                disagreement=disagreement,
            ))

    groups.sort(key=lambda g: (-len(g.sibling_title_ids) - 1, g.group_id))
    return groups


def annotate_titles_with_dedupe_group(
    titles_by_id: dict[str, dict],
    groups: Iterable[DedupeGroup],
) -> None:
    """Stamp each title's payload with its dedupe_group_id (or None for
    singletons). Mutates payloads in place. Called by the workflow-context
    builder so the frontend can render the disclosure-folder UI without
    a second round-trip.
    """
    by_member: dict[str, str] = {}
    for g in groups:
        by_member[g.representative_title_id] = g.group_id
        for tid in g.sibling_title_ids:
            by_member[tid] = g.group_id
    for tid, payload in titles_by_id.items():
        if isinstance(payload, dict):
            payload["dedupe_group_id"] = by_member.get(tid)


def _clip_id_from_m2ts_source(source_file: str | None) -> int | None:
    """Parse the integer clip ID from an m2ts source filename.

    `02807.m2ts` → 2807, `00006.m2ts` → 6, anything else → None.
    """
    if not source_file:
        return None
    s = str(source_file).strip()
    if not s.lower().endswith(".m2ts"):
        return None
    stem = s[:-len(".m2ts")]
    if not stem or not stem.isdigit():
        return None
    return int(stem)


def _clip_ids_from_mpls_segment_map(segment_map: str | None) -> set[int]:
    """Parse the integer clip IDs out of an .mpls `segment_map`.

    `"504,510,501"` → `{504, 510, 501}`. Also handles paren-wrapped
    MakeMKV forms (`"(504,510,501)"` → same result). Non-integer
    tokens are ignored.
    """
    from core.segment_reorder import parse_segment_map_tokens
    out: set[int] = set()
    for tok in parse_segment_map_tokens(segment_map):
        if tok.isdigit():
            out.add(int(tok))
    return out


_MPLS_SUFFIX_RE = re.compile(r"\.mpls(?:\(\d+\))?$", re.IGNORECASE)


def _is_mpls_source(source_file: str | None) -> bool:
    """True for `00539.mpls` and MakeMKV's de-duplicated form `00451.mpls(2)`."""
    if not source_file:
        return False
    return bool(_MPLS_SUFFIX_RE.search(str(source_file)))


def compute_mpls_clip_index(
    titles_by_id: dict[str, dict],
) -> dict[str, str]:
    """Return ``{m2ts_title_id: wrapping_mpls_title_id}`` for every m2ts
    on the disc whose clip ID is included in some mpls's `segment_map`.

    The mpls is the *playlist* that wraps the m2ts *clip*; surfacing
    both the wrapper and its underlying clips as separate top-level
    rows is noise the user shouldn't have to ignore manually. Used by
    the workflow-context builder to (a) fold the m2ts rows into the
    wrapper's dedupe group (`fold_subsumption_into_groups`) so they
    collapse out of the left rail and (b) point the
    DuplicateGroupPanel at the wrapping mpls for the right-panel
    "Component clips" section.

    Tiebreaker when a single clip ID is referenced by multiple mpls:
    deterministic — the mpls with the smallest `index` wins, falling
    back to lexicographic `source_file`. Should be vanishingly rare on
    real discs.
    """
    # Build clip_id → list of (sort_key, mpls_title_id) for every mpls.
    mpls_index: dict[int, list[tuple[tuple, str]]] = {}
    m2ts_rows: list[tuple[int, str]] = []  # (clip_id, m2ts_title_id)

    for tid, payload in titles_by_id.items():
        if not isinstance(payload, dict):
            continue
        source = payload.get("source_file")
        if not source:
            continue
        # m2ts: candidate to be subsumed
        m2ts_clip = _clip_id_from_m2ts_source(source)
        if m2ts_clip is not None:
            m2ts_rows.append((m2ts_clip, str(tid)))
            continue
        # mpls: candidate wrapper
        if _is_mpls_source(source):
            idx = payload.get("index")
            sort_key = (idx if isinstance(idx, int) else 10**9, str(source))
            for cid in _clip_ids_from_mpls_segment_map(payload.get("segment_map")):
                mpls_index.setdefault(cid, []).append((sort_key, str(tid)))

    out: dict[str, str] = {}
    for clip_id, m2ts_tid in m2ts_rows:
        wrappers = mpls_index.get(clip_id)
        if not wrappers:
            continue
        # Smallest sort_key wins (lowest index, then lex source_file).
        wrappers.sort()
        out[m2ts_tid] = wrappers[0][1]
    return out


def fold_subsumption_into_groups(
    groups: list[DedupeGroup],
    clip_index: dict[str, str],
    titles_by_id: dict[str, dict],
) -> list[DedupeGroup]:
    """Fold subsumed m2ts clips into their wrapper's dedupe group.

    ``clip_index`` is ``compute_mpls_clip_index`` output
    (m2ts_title_id → wrapping_mpls_title_id). An m2ts never forms a
    sorted-segment-set group of its own (single-clip segment_maps have
    no set key), so without this fold it renders as its own left-rail
    row even though the wrapping mpls already plays it. Folding it into
    the wrapper's group hides it via the existing dedupe-sibling gate
    and keeps one source of truth for row collapse.

    - Wrapper already in a group (as representative OR sibling) → the
      m2ts joins that group's ``sibling_title_ids``.
    - Wrapper ungrouped → a synthetic group is created with the wrapper
      as representative and ``representative_source='subsumption'``.
      The group_id is keyed on the wrapper's title_id so it is stable
      across re-runs.
    - m2ts with ``force_independent_group`` (the Ungroup escape hatch)
      are skipped, mirroring ``attach_duplicate_info``.

    Call this AFTER ``apply_obfuscation_reason_from_dedupe``: folded
    m2ts are component clips, not decoy playlists, and must not be
    stamped ``obfuscation_reason='segment_set_sibling'``. Mutates the
    given groups in place; returns the combined (re-sorted) list.
    """
    member_group: dict[str, DedupeGroup] = {}
    for g in groups:
        member_group[str(g.representative_title_id)] = g
        for tid in g.sibling_title_ids:
            member_group[str(tid)] = g

    out = list(groups)
    for m2ts_tid in sorted(clip_index):
        wrapper_tid = str(clip_index[m2ts_tid])
        m2ts_tid = str(m2ts_tid)
        payload = titles_by_id.get(m2ts_tid)
        if isinstance(payload, dict) and payload.get("force_independent_group"):
            continue
        if m2ts_tid in member_group:
            continue
        group = member_group.get(wrapper_tid)
        if group is None:
            wrapper_payload = titles_by_id.get(wrapper_tid)
            if not isinstance(wrapper_payload, dict):
                continue
            seg_hash = hashlib.sha256(wrapper_tid.encode()).hexdigest()[:12]
            group = DedupeGroup(
                group_id=f"subsumed:{seg_hash}",
                sorted_segment_key=_segment_set_key(wrapper_payload.get("segment_map")) or "",
                duration_bucket_s=int(_payload_duration(wrapper_payload) or 0.0),
                representative_title_id=wrapper_tid,
                representative_source="subsumption",
            )
            member_group[wrapper_tid] = group
            out.append(group)
        group.sibling_title_ids = sorted([*group.sibling_title_ids, m2ts_tid])
        member_group[m2ts_tid] = group

    out.sort(key=lambda g: (-len(g.sibling_title_ids) - 1, g.group_id))
    return out


def _user_claimed_type(row: Any) -> bool:
    """True when the user typed this row with something other than ignore."""
    user_type = (str(getattr(row, "user_type", None) or "")).strip().lower()
    return bool(user_type) and user_type != "ignore"


def apply_subsumption_marks(
    db: Any,
    disc_id: str,
    clip_index: dict[str, str],
) -> tuple[int, int]:
    """Persist the m2ts⊆mpls subsumption on disc_titles.

    For each entry in ``clip_index`` (m2ts_title_id → wrapping_mpls_title_id):
    - Set ``subsumed_by_title_id`` to the wrapper.
    - If ``type`` is unset OR already ``'ignore'``, mark ``type='ignore'``
      so downstream stages skip the redundant clip. (Left-rail hiding
      comes from ``fold_subsumption_into_groups``, not from this mark.)
      User-applied non-ignore types are respected (idempotency).

    Idempotent: rows already in the desired state aren't re-written.
    Returns ``(marked_ignore, set_subsumed_by)`` for caller logging.
    """
    if not disc_id or not clip_index:
        return (0, 0)
    # Short-circuit: if the clip_index hasn't changed since the last successful
    # apply on this disc, the SELECT + writes can't possibly produce different
    # results. Saves a 50+-row query on every workflow-context fetch when the
    # disc's titles haven't changed.
    sig = _subsumption_input_signature(clip_index)
    last = _LAST_APPLY_SIG.get(disc_id, {})
    if last.get("subsumption") == sig:
        return (0, 0)
    from api import models as db_models
    rows = (
        db.query(db_models.DiscTitle)
        .filter(db_models.DiscTitle.disc_id == disc_id)
        .filter(db_models.DiscTitle.id.in_(set(clip_index.keys())))
        .all()
    )
    marked = 0
    set_sub = 0
    for row in rows:
        rid = str(getattr(row, "id", ""))
        wrapper = clip_index.get(rid)
        if not wrapper:
            continue
        if getattr(row, "subsumed_by_title_id", None) != wrapper:
            row.subsumed_by_title_id = wrapper
            set_sub += 1
        existing = (getattr(row, "type", None) or "").strip().lower()
        if existing and existing != "ignore":
            # Respect user-applied types — leave them visible.
            continue
        if existing != "ignore":
            # Subsumption is an automated decision (m2ts wrapped by an
            # mpls). source='auto' keeps the user_type column NULL so
            # the chip system shows "needs review" instead of "ignored
            # by user".
            from api.crud import set_title_type
            set_title_type(row, "ignore", source="auto")
            marked += 1
    # A wrapper whose clips the user has claimed must step aside, or the
    # same footage is ripped twice — once as the play-all and once as each
    # clip (#797). Auto-ignore is source='auto', so a user who wants the
    # play-all as well can set a type on the wrapper and win by resolution.
    claimed_wrappers = {
        clip_index[str(getattr(row, "id", ""))]
        for row in rows
        if clip_index.get(str(getattr(row, "id", ""))) and _user_claimed_type(row)
    }
    if claimed_wrappers:
        from api.crud import set_title_type
        wrapper_rows = (
            db.query(db_models.DiscTitle)
            .filter(db_models.DiscTitle.disc_id == disc_id)
            .filter(db_models.DiscTitle.id.in_(claimed_wrappers))
            .all()
        )
        for wrapper_row in wrapper_rows:
            # Idempotent: only write when auto_type isn't already ignore, and
            # never touch a wrapper the user typed themselves.
            if _user_claimed_type(wrapper_row):
                continue
            if (getattr(wrapper_row, "auto_type", None) or "").strip().lower() == "ignore":
                continue
            set_title_type(wrapper_row, "ignore", source="auto")
            marked += 1

    # Update memo only after a successful pass so a mid-flight crash retries.
    _LAST_APPLY_SIG.setdefault(disc_id, {})["subsumption"] = sig
    return (marked, set_sub)


def apply_obfuscation_reason_from_dedupe(
    db: Any,
    disc_id: str,
    groups: Iterable[DedupeGroup],
) -> tuple[int, int]:
    """Persist tier-aware obfuscation_reason for every dedupe-group member.

    Walks the computed groups and writes ``disc_titles.obfuscation_reason``:

    - Representative (the canonical of the sorted-segment-set bucket) →
      ``NULL``. Overrides MakeMKV's per-title MSG:3307 bit on a row that
      sorted-segment-set membership has just identified as the canonical;
      this is the explicit "demote MakeMKV's signal when we have a
      stronger one" path.
    - Sibling (non-representative member of a group with ≥2 members) →
      ``'segment_set_sibling'``. HIGH-tier reason — catches Midway-class
      false negatives that MakeMKV's per-title bit missed.

    Precedence note (#374): this overwrite also applies to the post-ffprobe
    reasons (``duration_short`` / ``low_bitrate_decoy``) — relational
    group membership is the stronger signal, so a representative loses any
    post-ffprobe reason and a sibling's reason becomes
    ``segment_set_sibling``. Intentional; pinned by
    tests/test_path_b_dedupe.py.

    Also keeps ``obfuscation_flag`` in sync with ``obfuscation_reason``
    so legacy readers stay correct (`flag = reason IS NOT NULL`).

    Idempotent: only writes when the row's value would change, so
    rerunning on the same disc emits no DB churn. Returns
    ``(rows_cleared, rows_set_sibling)`` for callers that want to log.
    """
    if not disc_id:
        return (0, 0)
    # Materialize once so we can fingerprint AND iterate without exhausting
    # the iterable (groups may be a one-shot iterator from compute_dedupe_groups).
    groups_list = list(groups)
    rep_ids: set[str] = set()
    sibling_ids: set[str] = set()
    for g in groups_list:
        rep = getattr(g, "representative_title_id", None)
        if rep:
            rep_ids.add(str(rep))
        for tid in (getattr(g, "sibling_title_ids", None) or []):
            sibling_ids.add(str(tid))
    if not rep_ids and not sibling_ids:
        return (0, 0)

    # Short-circuit: if the group shape hasn't changed since the last
    # successful apply on this disc, the SELECT + writes can't produce
    # different results. Saves a 200+-row query on every workflow-context
    # fetch when the disc's titles haven't changed.
    sig = _dedupe_input_signature(groups_list)
    last = _LAST_APPLY_SIG.get(disc_id, {})
    if last.get("dedupe_reason") == sig:
        return (0, 0)

    # Single round-trip: pull every candidate row, mutate in Python, let
    # SQLAlchemy emit only the rows that actually changed.
    from api import models as db_models
    candidates = (
        db.query(db_models.DiscTitle)
        .filter(db_models.DiscTitle.disc_id == disc_id)
        .filter(db_models.DiscTitle.id.in_(rep_ids | sibling_ids))
        .all()
    )
    cleared = 0
    set_sibling = 0
    for row in candidates:
        rid = str(getattr(row, "id", ""))
        if rid in rep_ids:
            # Representative: clear any pre-existing reason (typically the
            # MakeMKV per-title bit) — sorted-segment-set membership has
            # decided this row is the canonical, which is a stronger signal.
            if getattr(row, "obfuscation_reason", None) is not None:
                row.obfuscation_reason = None
                cleared += 1
            if getattr(row, "obfuscation_flag", False):
                row.obfuscation_flag = False
        elif rid in sibling_ids:
            if getattr(row, "obfuscation_reason", None) != "segment_set_sibling":
                row.obfuscation_reason = "segment_set_sibling"
                set_sibling += 1
            if not getattr(row, "obfuscation_flag", False):
                row.obfuscation_flag = True
    # Update memo only after a successful pass so a mid-flight crash retries.
    _LAST_APPLY_SIG.setdefault(disc_id, {})["dedupe_reason"] = sig
    return (cleared, set_sibling)


def apply_path_b_marks_for_disc(db: Any, disc_id: str) -> tuple[int, int, int, int]:
    """Run the persisting half of Path B — obfuscation_reason for dedupe
    siblings/representatives and m2ts⊆mpls subsumption marks — against the
    disc's current rows.

    This used to run inside GET /jobs/{id}/workflow-context: a read that
    wrote (and committed) mid-request. Consequences, both measured on the
    rc test rig: the GET's response was serialized *before* its own side
    effects committed, so clients cached state stale relative to the very
    request that returned it; and read traffic caused write churn (the
    first GET after a container restart re-stamped ~300 rows). The marks
    depend only on segment maps and scan metadata — which change at
    scan/detect time — so they now apply there, and reads stay reads.

    Ordering matters and mirrors the old GET block: obfuscation_reason
    first against the UNFOLDED groups, subsumption after — so component
    clips never receive obfuscation_reason='segment_set_sibling' (they
    aren't decoys; see fold_subsumption_into_groups).

    Does NOT commit — the caller owns the transaction, so the marks ride
    the same commit as the scan/detect changes that made them stale.
    Returns (reason_cleared, reason_set_sibling, subsumption_marked_ignore,
    subsumption_set_subsumed_by) for caller logging.
    """
    if not disc_id:
        return (0, 0, 0, 0)
    from api import models as db_models
    rows = (
        db.query(db_models.DiscTitle)
        .filter(db_models.DiscTitle.disc_id == disc_id)
        .all()
    )
    if not rows:
        return (0, 0, 0, 0)
    disc_row = db.query(db_models.Disc).filter(db_models.Disc.id == disc_id).first()
    if not segment_maps_identify_content(getattr(disc_row, "format", None)):
        # DVD: the shape-keyed marks this pass would write are meaningless.
        # Clear any it wrote before it knew that (#831) and do nothing else;
        # `duplicate_group_sync.release_segment_map_demotions` handles the
        # active / auto_type side of the same heal.
        cleared = 0
        for r in rows:
            if getattr(r, "obfuscation_reason", None) == "segment_set_sibling":
                r.obfuscation_reason = None
                r.obfuscation_flag = False
                cleared += 1
        # What a DVD *does* have is arithmetic: a play-all PGC is the sum of
        # its parts. The duration-sum detector is the DVD stand-in for the
        # m2ts ⊆ mpls fold below (#831).
        from core.play_all_wrapper import apply_play_all_wrapper_marks
        wrappers_marked, wrappers_cleared = apply_play_all_wrapper_marks(db, rows)
        if wrappers_marked or wrappers_cleared:
            _log.info(
                "apply_path_b_marks_for_disc disc_id=%s: play-all wrappers marked=%s cleared=%s",
                disc_id, wrappers_marked, wrappers_cleared,
            )
        return (cleared, 0, 0, 0)
    titles_by_id: dict[str, dict] = {}
    for r in rows:
        rid = str(getattr(r, "id", "") or "")
        if not rid:
            continue
        titles_by_id[rid] = {
            "title_id": rid,
            "source_file": getattr(r, "source_file", None),
            "segment_map": getattr(r, "segment_map", None),
            "type": getattr(r, "type", None),
            "size": getattr(r, "size", None),
            "mkv_size": getattr(r, "mkv_size", None),
            "metadata_scan": getattr(r, "metadata_scan", None),
            "duration": getattr(r, "duration", None),
            "index": getattr(r, "index", None),
            "force_independent_group": bool(getattr(r, "force_independent_group", False)),
            "obfuscation_flag": bool(getattr(r, "obfuscation_flag", False)),
        }
    clip_index = compute_mpls_clip_index(titles_by_id)
    groups = compute_dedupe_groups(titles_by_id)
    cleared, set_sibling = apply_obfuscation_reason_from_dedupe(db, disc_id, groups)
    marked, set_sub = apply_subsumption_marks(db, disc_id, clip_index)
    return (cleared, set_sibling, marked, set_sub)


def invalidate_dedupe_apply_memo(disc_id: str | None = None) -> None:
    """Clear the per-disc apply-state memo. Call when the title set is known
    to have changed in a way the signature wouldn't catch (re-scan, title
    delete, type edit by the user). Without disc_id, clears the whole memo.
    """
    if disc_id is None:
        _LAST_APPLY_SIG.clear()
        return
    _LAST_APPLY_SIG.pop(disc_id, None)
