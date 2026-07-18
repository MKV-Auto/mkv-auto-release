"""Tests for Path B sorted-segment-set dedupe in the labeling UI.

Three scenarios under test:
  1. Midway-class: 200+ same-set permutations collapse into one group.
     DiscDB-classified canonical wins as representative; the rest become
     siblings hidden behind the disclosure.
  2. V-for-Vendetta-class: an .mpls and its underlying .m2ts that share
     the same single segment. (Note: Path B groups by sorted SET — for
     single-segment titles, the "set" is just the one segment, so they
     do group together. This is intentional and complementary to the
     existing ORDER-PRESERVED `_normalize_segment_map` grouping in
     duplicate_info.py — Path B captures more cases.)
  3. Disagreement: DiscDB and MakeMKV's obfuscation flag pick different
     siblings. Both candidates surface for the side-by-side compare card.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.path_b_dedupe import (
    _clip_id_from_m2ts_source,
    _clip_ids_from_mpls_segment_map,
    annotate_titles_with_dedupe_group,
    apply_obfuscation_reason_from_dedupe,
    apply_subsumption_marks,
    compute_dedupe_groups,
    compute_mpls_clip_index,
    fold_subsumption_into_groups,
    invalidate_dedupe_apply_memo,
)


@pytest.fixture(autouse=True)
def _reset_apply_memo():
    """Per-process apply-state memo would otherwise let one test's state
    leak into another (e.g. silently short-circuiting a SELECT we assert
    on). Clear before every test in this module."""
    invalidate_dedupe_apply_memo()
    yield
    invalidate_dedupe_apply_memo()


GB = 1024 ** 3


def _payload(
    *,
    title_id: str,
    source_file: str = "00800.mpls",
    segment_map: str | None = "1,2,3",
    duration: float = 8000.0,
    type_: str | None = None,
    obfuscation_flag: bool = False,
    audio_score: int = 5,
    chapters_count: int = 16,
    size_gb: float = 39.0,
):
    return {
        "title_id": title_id,
        "source_file": source_file,
        "segment_map": segment_map,
        "duration": duration,
        "type": type_,
        "obfuscation_flag": obfuscation_flag,
        "metadata_scan": {
            "audio_score": audio_score,
            "chapters_count": chapters_count,
        },
        "size": int(size_gb * GB),
    }


# ── Midway-class: large group, DiscDB-classified canonical ───────────────────


def _midway_group_titles():
    """6 same-set siblings (different orders); only one (canonical) has type='movie'."""
    canonical_order = "504,510,501,507,502,505,506,509,503,508"
    decoy_orders = [
        "501,502,503,504,505,506,507,508,509,510",
        "510,509,508,507,506,505,504,503,502,501",
        "503,508,501,510,504,507,502,509,505,506",
        "508,505,506,510,509,503,501,504,507,502",
        "501,510,509,508,507,506,505,504,503,502",
    ]
    titles = {}
    titles["uuid-canonical"] = _payload(
        title_id="uuid-canonical", source_file="00539.mpls",
        segment_map=canonical_order, type_="movie",
        obfuscation_flag=True,  # MakeMKV flagged the canonical too on Midway
    )
    for i, order in enumerate(decoy_orders, start=1):
        titles[f"uuid-decoy-{i}"] = _payload(
            title_id=f"uuid-decoy-{i}", source_file=f"deco{i}.mpls",
            segment_map=order, type_=None,  # not DiscDB-classified
            obfuscation_flag=True,
        )
    return titles


def test_midway_collapses_to_one_group_with_canonical_representative():
    titles = _midway_group_titles()
    groups = compute_dedupe_groups(titles)
    assert len(groups) == 1
    g = groups[0]
    assert g.representative_title_id == "uuid-canonical"
    assert g.representative_source == "discdb"
    assert len(g.sibling_title_ids) == 5
    # No disagreement: only one DiscDB pick exists; flag wins not consulted.
    assert g.disagreement is None


def test_annotate_titles_stamps_dedupe_group_id():
    titles = _midway_group_titles()
    groups = compute_dedupe_groups(titles)
    annotate_titles_with_dedupe_group(titles, groups)
    canonical_gid = titles["uuid-canonical"]["dedupe_group_id"]
    assert canonical_gid is not None
    # Every member shares the same group id.
    for tid, payload in titles.items():
        assert payload["dedupe_group_id"] == canonical_gid


def test_singletons_not_emitted():
    titles = {
        "uuid-a": _payload(title_id="uuid-a", segment_map="1,2,3"),
        "uuid-b": _payload(title_id="uuid-b", segment_map="4,5,6"),
    }
    assert compute_dedupe_groups(titles) == []


# ── DiscDB-vs-MakeMKV-flag disagreement ──────────────────────────────────────


def test_disagreement_flagged_when_discdb_and_flag_pick_different_siblings():
    """DiscDB classified A; MakeMKV's flag is clear on B. Both surface."""
    titles = {
        "uuid-A": _payload(
            title_id="uuid-A", source_file="00539.mpls",
            segment_map="504,510,501,507,502,505,506,509,503,508",
            type_="movie",  # DiscDB classified
            obfuscation_flag=True,  # but flag set
        ),
        "uuid-B": _payload(
            title_id="uuid-B", source_file="00440.mpls",
            segment_map="501,502,503,504,505,506,507,508,509,510",
            type_=None,  # not DiscDB classified
            obfuscation_flag=False,  # flag clear
        ),
    }
    groups = compute_dedupe_groups(titles)
    assert len(groups) == 1
    g = groups[0]
    # DiscDB wins precedence.
    assert g.representative_title_id == "uuid-A"
    assert g.representative_source == "discdb"
    # Disagreement is surfaced for the compare card UI.
    assert g.disagreement == {
        "discdb_pick_id": "uuid-A",
        "makemkv_flag_pick_id": "uuid-B",
    }


def test_no_disagreement_when_discdb_and_flag_agree():
    """Both DiscDB classification AND flag-clear point at the same title."""
    titles = {
        "uuid-A": _payload(
            title_id="uuid-A", source_file="00800.mpls",
            segment_map="1,2,3", type_="movie",
            obfuscation_flag=False,
        ),
        "uuid-B": _payload(
            title_id="uuid-B", source_file="00801.mpls",
            segment_map="3,2,1", type_=None,
            obfuscation_flag=True,
        ),
    }
    g = compute_dedupe_groups(titles)[0]
    assert g.disagreement is None
    assert g.representative_title_id == "uuid-A"


def test_makemkv_flag_wins_when_no_discdb_classification():
    """No DiscDB → MakeMKV flag picks; representative_source = makemkv_flag."""
    titles = {
        "uuid-real": _payload(
            title_id="uuid-real", source_file="00800.mpls",
            segment_map="1,2,3", obfuscation_flag=False,
        ),
        "uuid-decoy": _payload(
            title_id="uuid-decoy", source_file="00801.mpls",
            segment_map="3,2,1", obfuscation_flag=True,
        ),
    }
    g = compute_dedupe_groups(titles)[0]
    assert g.representative_title_id == "uuid-real"
    assert g.representative_source == "makemkv_flag"
    # No disagreement: only flag is set, DiscDB silent.
    assert g.disagreement is None


def test_heuristic_wins_when_neither_discdb_nor_flag_decides():
    """No DiscDB, all flagged. Heuristic falls back to score then mpls preference."""
    titles = {
        "uuid-mpls": _payload(
            title_id="uuid-mpls", source_file="00800.mpls",
            segment_map="1,2,3", obfuscation_flag=True,
            size_gb=39.0,
        ),
        "uuid-m2ts": _payload(
            title_id="uuid-m2ts", source_file="00800.m2ts",
            segment_map="3,2,1", obfuscation_flag=True,
            size_gb=39.5,  # slightly bigger but mpls wins on tiebreaker
        ),
    }
    g = compute_dedupe_groups(titles)[0]
    assert g.representative_title_id == "uuid-mpls"
    assert g.representative_source == "heuristic"


# ── Duration tolerance ───────────────────────────────────────────────────────


def test_titles_with_divergent_durations_not_grouped():
    """Multi-cut disc: same segments, different runtimes → not Path B group.
    Theatrical (2h) and director's cut (2h30m) shouldn't collapse."""
    titles = {
        "theatrical": _payload(
            title_id="theatrical", segment_map="1,2,3,4,5", duration=7200.0,
        ),
        "directors": _payload(
            title_id="directors", segment_map="1,2,5,4,3", duration=9000.0,
        ),
    }
    groups = compute_dedupe_groups(titles)
    # Different duration buckets at 1% tolerance → no group.
    assert groups == []


def test_titles_within_tolerance_grouped():
    """Same segments, runtimes within 1% (e.g. 8304 vs 8307 sec) → grouped."""
    titles = {
        "uuid-a": _payload(
            title_id="uuid-a", segment_map="1,2,3,4,5", duration=8304.0,
        ),
        "uuid-b": _payload(
            title_id="uuid-b", segment_map="5,4,3,2,1", duration=8307.0,
        ),
    }
    groups = compute_dedupe_groups(titles)
    assert len(groups) == 1


def test_missing_duration_treated_as_distinct_bucket():
    """When a title has no duration, it gets bucket=None which won't
    collide with any sibling that has a duration. Conservative — better
    to under-group than to misidentify durations as siblings."""
    titles = {
        "uuid-a": _payload(title_id="uuid-a", segment_map="1,2,3", duration=8000.0),
        "uuid-b": _payload(title_id="uuid-b", segment_map="3,2,1"),
    }
    titles["uuid-b"]["duration"] = None
    groups = compute_dedupe_groups(titles)
    assert groups == []


# ── Edge cases ───────────────────────────────────────────────────────────────


def test_titles_without_segment_map_excluded():
    titles = {
        "uuid-a": _payload(title_id="uuid-a", segment_map=None),
        "uuid-b": _payload(title_id="uuid-b", segment_map="1,2,3"),
        "uuid-c": _payload(title_id="uuid-c", segment_map="3,2,1"),
    }
    groups = compute_dedupe_groups(titles)
    assert len(groups) == 1
    members = {groups[0].representative_title_id, *groups[0].sibling_title_ids}
    assert members == {"uuid-b", "uuid-c"}


def test_min_group_size_threshold_skips_smaller_groups():
    """min_group_size=3 drops 2-member groups; useful when caller wants a
    harder dedupe threshold."""
    titles = {
        "a": _payload(title_id="a", segment_map="1,2,3"),
        "b": _payload(title_id="b", segment_map="3,2,1"),
        # Different group, 3 members
        "c": _payload(title_id="c", segment_map="4,5,6"),
        "d": _payload(title_id="d", segment_map="5,6,4"),
        "e": _payload(title_id="e", segment_map="6,4,5"),
    }
    groups = compute_dedupe_groups(titles, min_group_size=3)
    assert len(groups) == 1
    # 3-member group emitted, 2-member dropped.
    rep_and_siblings = {
        groups[0].representative_title_id, *groups[0].sibling_title_ids
    }
    assert rep_and_siblings == {"c", "d", "e"}


def test_groups_sorted_by_descending_size():
    titles = {
        "a": _payload(title_id="a", segment_map="1,2,3"),
        "b": _payload(title_id="b", segment_map="3,2,1"),
        "c": _payload(title_id="c", segment_map="4,5,6"),
        "d": _payload(title_id="d", segment_map="5,6,4"),
        "e": _payload(title_id="e", segment_map="6,4,5"),
    }
    groups = compute_dedupe_groups(titles)
    assert len(groups) == 2
    # 3-member group first, 2-member second.
    assert len(groups[0].sibling_title_ids) == 2
    assert len(groups[1].sibling_title_ids) == 1


class _FakeRow:
    """SimpleNamespace stand-in for SQLAlchemy DiscTitle in unit tests."""
    def __init__(self, tid: str, *, obfuscation_flag=False, obfuscation_reason=None):
        self.id = tid
        self.obfuscation_flag = obfuscation_flag
        self.obfuscation_reason = obfuscation_reason


class _FakeFilter:
    def __init__(self, rows): self._rows = rows
    def filter(self, *a, **k): return self
    def all(self): return self._rows


def _fake_db(rows):
    db = SimpleNamespace()
    db.commit = MagicMock()
    db.query = MagicMock(return_value=_FakeFilter(rows))
    return db


def test_apply_obfuscation_reason_clears_rep_and_marks_siblings():
    """Midway-shape: one canonical + 5 siblings, all originally
    flagged by MakeMKV's per-title bit. After dedupe-driven write,
    the rep is cleared and the siblings carry the high-tier reason."""
    titles = _midway_group_titles()
    groups = compute_dedupe_groups(titles)
    assert len(groups) == 1
    g = groups[0]
    # Mirror what the DB would have: every row pre-flagged by MakeMKV.
    rows = [_FakeRow(tid, obfuscation_flag=True, obfuscation_reason="makemkv_msg3307")
            for tid in titles.keys()]
    db = _fake_db(rows)

    cleared, set_sibling = apply_obfuscation_reason_from_dedupe(
        db, disc_id="disc-1", groups=groups,
    )

    rep_row = next(r for r in rows if r.id == g.representative_title_id)
    sibling_rows = [r for r in rows if r.id in g.sibling_title_ids]

    assert cleared == 1
    assert set_sibling == len(g.sibling_title_ids)
    assert rep_row.obfuscation_reason is None
    assert rep_row.obfuscation_flag is False
    assert all(r.obfuscation_reason == "segment_set_sibling" for r in sibling_rows)
    assert all(r.obfuscation_flag is True for r in sibling_rows)


def test_apply_obfuscation_reason_is_idempotent_on_rerun():
    """Re-running on rows that already carry the correct reason emits
    no writes. The (cleared, set_sibling) counters both return 0."""
    titles = _midway_group_titles()
    groups = compute_dedupe_groups(titles)
    g = groups[0]
    rows = []
    for tid in titles.keys():
        if tid == g.representative_title_id:
            rows.append(_FakeRow(tid, obfuscation_flag=False, obfuscation_reason=None))
        else:
            rows.append(_FakeRow(tid, obfuscation_flag=True, obfuscation_reason="segment_set_sibling"))
    db = _fake_db(rows)

    cleared, set_sibling = apply_obfuscation_reason_from_dedupe(
        db, disc_id="disc-1", groups=groups,
    )

    assert cleared == 0
    assert set_sibling == 0


def test_apply_obfuscation_reason_promotes_sibling_from_makemkv_only():
    """A row that arrived with reason='makemkv_msg3307' but is now
    classified as a non-rep sibling gets upgraded to the HIGH-tier reason."""
    titles = _midway_group_titles()
    groups = compute_dedupe_groups(titles)
    g = groups[0]
    rows = []
    for tid in titles.keys():
        if tid == g.representative_title_id:
            rows.append(_FakeRow(tid, obfuscation_flag=True, obfuscation_reason="makemkv_msg3307"))
        else:
            rows.append(_FakeRow(tid, obfuscation_flag=True, obfuscation_reason="makemkv_msg3307"))
    db = _fake_db(rows)

    _, set_sibling = apply_obfuscation_reason_from_dedupe(
        db, disc_id="disc-1", groups=groups,
    )
    assert set_sibling == len(g.sibling_title_ids)
    sibling_rows = [r for r in rows if r.id in g.sibling_title_ids]
    assert all(r.obfuscation_reason == "segment_set_sibling" for r in sibling_rows)


def test_apply_obfuscation_reason_overrides_post_ffprobe_reasons():
    """Precedence pin (#374): relational group membership beats the
    post-ffprobe reasons. A representative that arrived with
    'duration_short' is cleared; a sibling that arrived with
    'low_bitrate_decoy' is overwritten to 'segment_set_sibling'.
    If this ever needs to change, change the docstring in
    apply_obfuscation_reason_from_dedupe too — it documents this exact
    interaction as intentional."""
    titles = _midway_group_titles()
    groups = compute_dedupe_groups(titles)
    g = groups[0]
    rows = []
    for tid in titles.keys():
        if tid == g.representative_title_id:
            rows.append(_FakeRow(tid, obfuscation_flag=True, obfuscation_reason="duration_short"))
        else:
            rows.append(_FakeRow(tid, obfuscation_flag=True, obfuscation_reason="low_bitrate_decoy"))
    db = _fake_db(rows)

    cleared, set_sibling = apply_obfuscation_reason_from_dedupe(
        db, disc_id="disc-374-precedence", groups=groups,
    )

    rep_row = next(r for r in rows if r.id == g.representative_title_id)
    sibling_rows = [r for r in rows if r.id in g.sibling_title_ids]
    assert cleared == 1
    assert set_sibling == len(g.sibling_title_ids)
    assert rep_row.obfuscation_reason is None
    assert rep_row.obfuscation_flag is False
    assert all(r.obfuscation_reason == "segment_set_sibling" for r in sibling_rows)
    assert all(r.obfuscation_flag is True for r in sibling_rows)


def test_apply_obfuscation_reason_no_op_when_no_groups():
    db = _fake_db([])
    cleared, set_sibling = apply_obfuscation_reason_from_dedupe(
        db, disc_id="disc-1", groups=[],
    )
    assert (cleared, set_sibling) == (0, 0)


def test_apply_obfuscation_reason_no_op_when_disc_id_missing():
    titles = _midway_group_titles()
    groups = compute_dedupe_groups(titles)
    db = _fake_db([_FakeRow(tid) for tid in titles.keys()])
    cleared, set_sibling = apply_obfuscation_reason_from_dedupe(
        db, disc_id="", groups=groups,
    )
    assert (cleared, set_sibling) == (0, 0)


def test_to_dict_includes_disagreement_only_when_present():
    titles = {
        "uuid-A": _payload(
            title_id="uuid-A", source_file="00539.mpls",
            segment_map="1,2,3", type_="movie", obfuscation_flag=True,
        ),
        "uuid-B": _payload(
            title_id="uuid-B", source_file="00540.mpls",
            segment_map="3,2,1", type_=None, obfuscation_flag=False,
        ),
    }
    g = compute_dedupe_groups(titles)[0]
    d = g.to_dict()
    assert "disagreement" in d  # with disagreement
    # Now without disagreement
    titles["uuid-B"]["obfuscation_flag"] = True
    g_clean = compute_dedupe_groups(titles)[0]
    d_clean = g_clean.to_dict()
    assert "disagreement" not in d_clean


# ── Phase 6: m2ts ⊆ mpls subsumption ────────────────────────────────────────


class _FakeRowSub:
    def __init__(self, tid: str, *, type_=None, subsumed_by=None,
                 auto_type=None, user_type=None):
        self.id = tid
        self.type = type_
        self.subsumed_by_title_id = subsumed_by
        # crud.set_title_type expects the source-split columns. Legacy
        # tests passed `type_` directly; treat it as the user-set value
        # when not otherwise supplied.
        self.auto_type = auto_type
        self.user_type = user_type if user_type is not None else (type_ if type_ else None)


def _fake_db_sub(rows):
    db = SimpleNamespace()
    db.commit = MagicMock()
    db.query = MagicMock(return_value=_FakeFilter(rows))
    return db


class TestClipIdParsing:
    def test_m2ts_clip_id_strips_leading_zeros(self):
        assert _clip_id_from_m2ts_source("02807.m2ts") == 2807
        assert _clip_id_from_m2ts_source("00006.m2ts") == 6
        assert _clip_id_from_m2ts_source("00000.m2ts") == 0

    def test_m2ts_clip_id_returns_none_for_non_m2ts(self):
        assert _clip_id_from_m2ts_source("00539.mpls") is None
        assert _clip_id_from_m2ts_source("foo.mkv") is None
        assert _clip_id_from_m2ts_source(None) is None
        assert _clip_id_from_m2ts_source("") is None

    def test_mpls_segment_map_parses_clip_ids(self):
        assert _clip_ids_from_mpls_segment_map("504,510,501") == {504, 510, 501}
        assert _clip_ids_from_mpls_segment_map("2807,2808,2809") == {2807, 2808, 2809}
        assert _clip_ids_from_mpls_segment_map("") == set()
        assert _clip_ids_from_mpls_segment_map(None) == set()
        # Non-numeric tokens skipped, valid ones kept.
        assert _clip_ids_from_mpls_segment_map("1, foo, 2") == {1, 2}


class TestComputeMplsClipIndex:
    def test_midway_extras_subsumed_by_mpls_wrapper(self):
        """`00451.mpls(2)` wraps three m2ts (02807, 02808, 02809).
        All three should map to the mpls's title_id."""
        titles = {
            "uuid-mpls": {
                "source_file": "00451.mpls(2)",
                "segment_map": "2807,2808,2809",
                "index": 88,
            },
            "uuid-m2ts-7": {"source_file": "02807.m2ts", "index": 219},
            "uuid-m2ts-8": {"source_file": "02808.m2ts", "index": 220},
            "uuid-m2ts-9": {"source_file": "02809.m2ts", "index": 221},
            # A standalone m2ts NOT referenced by any mpls should stay free.
            "uuid-m2ts-free": {"source_file": "00006.m2ts", "index": 205},
        }
        idx = compute_mpls_clip_index(titles)
        assert idx == {
            "uuid-m2ts-7": "uuid-mpls",
            "uuid-m2ts-8": "uuid-mpls",
            "uuid-m2ts-9": "uuid-mpls",
        }
        # The free m2ts isn't in the result at all.
        assert "uuid-m2ts-free" not in idx

    def test_tiebreaker_prefers_smallest_index(self):
        """When two mpls reference the same clip, the one with the lower
        `index` wins. Deterministic so the user sees stable subsumption
        across context reloads."""
        titles = {
            "uuid-mpls-low":  {"source_file": "00100.mpls", "segment_map": "42", "index": 5},
            "uuid-mpls-high": {"source_file": "00200.mpls", "segment_map": "42", "index": 99},
            "uuid-m2ts":      {"source_file": "00042.m2ts", "index": 250},
        }
        idx = compute_mpls_clip_index(titles)
        assert idx == {"uuid-m2ts": "uuid-mpls-low"}

    def test_returns_empty_when_no_mpls(self):
        titles = {
            "uuid-m2ts": {"source_file": "00042.m2ts", "index": 200},
        }
        assert compute_mpls_clip_index(titles) == {}

    def test_returns_empty_when_no_m2ts(self):
        titles = {
            "uuid-mpls": {"source_file": "00539.mpls", "segment_map": "1,2,3", "index": 109},
        }
        assert compute_mpls_clip_index(titles) == {}


class TestApplySubsumptionMarks:
    def test_marks_type_ignore_and_sets_subsumed_by(self):
        rows = [
            _FakeRowSub("uuid-m2ts-7", type_=None, subsumed_by=None),
            _FakeRowSub("uuid-m2ts-8", type_=None, subsumed_by=None),
        ]
        db = _fake_db_sub(rows)
        clip_index = {"uuid-m2ts-7": "uuid-mpls", "uuid-m2ts-8": "uuid-mpls"}

        marked, set_sub = apply_subsumption_marks(db, "disc-1", clip_index)

        assert marked == 2
        assert set_sub == 2
        assert all(r.type == "ignore" for r in rows)
        assert all(r.subsumed_by_title_id == "uuid-mpls" for r in rows)

    def test_respects_user_applied_type(self):
        """If the user has tagged the m2ts as e.g. 'extra', leave its type
        alone — only fill subsumed_by_title_id so the wrapper still
        surfaces it in the Component-clips panel."""
        rows = [
            _FakeRowSub("uuid-m2ts-7", type_="extra", subsumed_by=None),
        ]
        db = _fake_db_sub(rows)
        clip_index = {"uuid-m2ts-7": "uuid-mpls"}

        marked, set_sub = apply_subsumption_marks(db, "disc-1", clip_index)

        assert marked == 0  # type left alone
        assert set_sub == 1
        assert rows[0].type == "extra"
        assert rows[0].subsumed_by_title_id == "uuid-mpls"

    def test_idempotent_on_already_marked(self):
        rows = [
            _FakeRowSub("uuid-m2ts-7", type_="ignore", subsumed_by="uuid-mpls"),
        ]
        db = _fake_db_sub(rows)
        clip_index = {"uuid-m2ts-7": "uuid-mpls"}

        marked, set_sub = apply_subsumption_marks(db, "disc-1", clip_index)

        assert marked == 0
        assert set_sub == 0

    def test_no_op_when_disc_id_missing(self):
        rows = [_FakeRowSub("uuid-m2ts-7")]
        db = _fake_db_sub(rows)
        marked, set_sub = apply_subsumption_marks(db, "", {"uuid-m2ts-7": "uuid-mpls"})
        assert (marked, set_sub) == (0, 0)

    def test_no_op_when_clip_index_empty(self):
        rows = [_FakeRowSub("uuid-m2ts-7")]
        db = _fake_db_sub(rows)
        marked, set_sub = apply_subsumption_marks(db, "disc-1", {})
        assert (marked, set_sub) == (0, 0)


# ── Phase 2: per-disc apply-state memo (skip redundant SELECTs) ─────────────


class TestApplyMemoShortCircuits:
    def setup_method(self) -> None:
        # Each test starts with a clean memo so they're order-independent.
        invalidate_dedupe_apply_memo()

    def test_repeat_apply_reason_skips_db_when_signature_unchanged(self):
        titles = _midway_group_titles()
        groups = compute_dedupe_groups(titles)
        rows = [_FakeRow(tid, obfuscation_flag=True, obfuscation_reason="makemkv_msg3307")
                for tid in titles.keys()]
        db = _fake_db(rows)

        # First call: actually writes.
        cleared1, set1 = apply_obfuscation_reason_from_dedupe(db, "disc-1", groups)
        assert (cleared1 + set1) > 0
        first_call_count = db.query.call_count

        # Second call with same input: no SELECT, no writes.
        cleared2, set2 = apply_obfuscation_reason_from_dedupe(db, "disc-1", groups)
        assert (cleared2, set2) == (0, 0)
        assert db.query.call_count == first_call_count  # no new query

    def test_apply_reason_runs_again_when_groups_change(self):
        titles = _midway_group_titles()
        groups_v1 = compute_dedupe_groups(titles)
        rows = [_FakeRow(tid, obfuscation_flag=True, obfuscation_reason="makemkv_msg3307")
                for tid in titles.keys()]
        db = _fake_db(rows)
        apply_obfuscation_reason_from_dedupe(db, "disc-1", groups_v1)
        baseline_calls = db.query.call_count

        # Now flip the dedupe shape: drop one decoy entirely.
        first_decoy_key = next(k for k in titles if k != "uuid-canonical")
        titles.pop(first_decoy_key)
        groups_v2 = compute_dedupe_groups(titles)
        apply_obfuscation_reason_from_dedupe(db, "disc-1", groups_v2)
        assert db.query.call_count == baseline_calls + 1

    def test_repeat_apply_subsumption_skips_db_when_signature_unchanged(self):
        rows = [
            _FakeRowSub("uuid-m2ts-7", type_=None, subsumed_by=None),
            _FakeRowSub("uuid-m2ts-8", type_=None, subsumed_by=None),
        ]
        db = _fake_db_sub(rows)
        clip_index = {"uuid-m2ts-7": "uuid-mpls", "uuid-m2ts-8": "uuid-mpls"}

        apply_subsumption_marks(db, "disc-1", clip_index)
        baseline = db.query.call_count

        apply_subsumption_marks(db, "disc-1", clip_index)
        assert db.query.call_count == baseline  # short-circuited

    def test_apply_subsumption_runs_again_when_clip_index_changes(self):
        rows = [_FakeRowSub("uuid-m2ts-7", type_=None, subsumed_by=None)]
        db = _fake_db_sub(rows)
        apply_subsumption_marks(db, "disc-1", {"uuid-m2ts-7": "uuid-mpls"})
        baseline = db.query.call_count

        # New clip mapped under the same disc — signature changes, runs again.
        rows2 = [
            _FakeRowSub("uuid-m2ts-7", type_=None, subsumed_by=None),
            _FakeRowSub("uuid-m2ts-8", type_=None, subsumed_by=None),
        ]
        db2 = _fake_db_sub(rows2)
        apply_subsumption_marks(
            db2, "disc-1",
            {"uuid-m2ts-7": "uuid-mpls", "uuid-m2ts-8": "uuid-mpls"},
        )
        assert db2.query.call_count == 1
        # And the original db hasn't been touched again.
        assert db.query.call_count == baseline

    def test_invalidate_apply_memo_forces_rewrite(self):
        titles = _midway_group_titles()
        groups = compute_dedupe_groups(titles)
        rows = [_FakeRow(tid, obfuscation_flag=True, obfuscation_reason="makemkv_msg3307")
                for tid in titles.keys()]
        db = _fake_db(rows)
        apply_obfuscation_reason_from_dedupe(db, "disc-1", groups)
        baseline = db.query.call_count

        invalidate_dedupe_apply_memo("disc-1")
        apply_obfuscation_reason_from_dedupe(db, "disc-1", groups)
        assert db.query.call_count == baseline + 1

    def test_memo_is_per_disc(self):
        titles = _midway_group_titles()
        groups = compute_dedupe_groups(titles)
        rows1 = [_FakeRow(tid, obfuscation_flag=True) for tid in titles.keys()]
        db1 = _fake_db(rows1)
        apply_obfuscation_reason_from_dedupe(db1, "disc-A", groups)

        # Same group shape, different disc → MUST run (different memo key).
        rows2 = [_FakeRow(tid, obfuscation_flag=True) for tid in titles.keys()]
        db2 = _fake_db(rows2)
        apply_obfuscation_reason_from_dedupe(db2, "disc-B", groups)
        assert db2.query.call_count == 1

# ── Subsumption fold: m2ts collapse under their wrapping mpls (#534) ─────────


def _wrapped_clip_titles(*, rep_type: str | None = "movie"):
    """Two permutation-sibling mpls (A wins via DiscDB type) + two m2ts
    wrapped by A (lowest index) + one free-standing m2ts."""
    return {
        "uuid-mpls-a": _payload(
            title_id="uuid-mpls-a", source_file="00451.mpls",
            segment_map="2807,2808", type_=rep_type,
        ) | {"index": 5},
        "uuid-mpls-b": _payload(
            title_id="uuid-mpls-b", source_file="00452.mpls",
            segment_map="2808,2807", type_=None,
        ) | {"index": 6},
        "uuid-m2ts-7": {
            "title_id": "uuid-m2ts-7", "source_file": "02807.m2ts",
            "segment_map": "2807", "duration": 4000.0, "index": 219,
        },
        "uuid-m2ts-8": {
            "title_id": "uuid-m2ts-8", "source_file": "02808.m2ts",
            "segment_map": "2808", "duration": 4000.0, "index": 220,
        },
        "uuid-m2ts-free": {
            "title_id": "uuid-m2ts-free", "source_file": "00006.m2ts",
            "segment_map": "6", "duration": 120.0, "index": 205,
        },
    }


class TestFoldSubsumptionIntoGroups:
    def test_m2ts_join_wrapper_group_as_siblings(self):
        titles = _wrapped_clip_titles()
        clip_index = compute_mpls_clip_index(titles)
        groups = compute_dedupe_groups(titles)
        assert len(groups) == 1

        folded = fold_subsumption_into_groups(groups, clip_index, titles)

        assert len(folded) == 1
        g = folded[0]
        assert g.representative_title_id == "uuid-mpls-a"
        assert g.sibling_title_ids == ["uuid-m2ts-7", "uuid-m2ts-8", "uuid-mpls-b"]
        # The free m2ts (no wrapper) is untouched.
        assert "uuid-m2ts-free" not in g.sibling_title_ids

    def test_m2ts_join_group_when_wrapper_is_a_sibling(self):
        """clip_index can point at a wrapper that lost the representative
        pick — the m2ts still folds into that wrapper's group."""
        titles = _wrapped_clip_titles(rep_type=None)
        # Make B the DiscDB-classified representative; A (the wrapper by
        # lowest index) becomes a sibling.
        titles["uuid-mpls-b"]["type"] = "movie"
        clip_index = compute_mpls_clip_index(titles)
        assert set(clip_index.values()) == {"uuid-mpls-a"}
        groups = compute_dedupe_groups(titles)
        assert groups[0].representative_title_id == "uuid-mpls-b"
        assert groups[0].sibling_title_ids == ["uuid-mpls-a"]

        folded = fold_subsumption_into_groups(groups, clip_index, titles)

        assert len(folded) == 1
        assert folded[0].sibling_title_ids == [
            "uuid-m2ts-7", "uuid-m2ts-8", "uuid-mpls-a",
        ]

    def test_ungrouped_wrapper_gets_synthetic_group(self):
        """A lone mpls (no permutation siblings) wrapping two m2ts still
        collapses them — via a synthetic 'subsumption' group keyed on the
        wrapper's title_id (stable across re-runs)."""
        titles = {
            "uuid-mpls": _payload(
                title_id="uuid-mpls", source_file="00451.mpls",
                segment_map="2807,2808",
            ) | {"index": 5},
            "uuid-m2ts-7": {
                "title_id": "uuid-m2ts-7", "source_file": "02807.m2ts",
                "segment_map": "2807", "duration": 4000.0, "index": 219,
            },
            "uuid-m2ts-8": {
                "title_id": "uuid-m2ts-8", "source_file": "02808.m2ts",
                "segment_map": "2808", "duration": 4000.0, "index": 220,
            },
        }
        clip_index = compute_mpls_clip_index(titles)
        groups = compute_dedupe_groups(titles)
        assert groups == []

        folded = fold_subsumption_into_groups(groups, clip_index, titles)

        assert len(folded) == 1
        g = folded[0]
        assert g.group_id.startswith("subsumed:")
        assert g.representative_title_id == "uuid-mpls"
        assert g.representative_source == "subsumption"
        assert g.sibling_title_ids == ["uuid-m2ts-7", "uuid-m2ts-8"]
        # Deterministic group_id across re-runs.
        refolded = fold_subsumption_into_groups([], clip_index, titles)
        assert refolded[0].group_id == g.group_id

    def test_force_independent_group_m2ts_is_skipped(self):
        """Ungroup escape hatch: a force_independent m2ts stays out of the
        fold so it renders as its own row (parity with attach_duplicate_info)."""
        titles = _wrapped_clip_titles()
        titles["uuid-m2ts-7"]["force_independent_group"] = True
        clip_index = compute_mpls_clip_index(titles)
        groups = compute_dedupe_groups(titles)

        folded = fold_subsumption_into_groups(groups, clip_index, titles)

        assert folded[0].sibling_title_ids == ["uuid-m2ts-8", "uuid-mpls-b"]

    def test_m2ts_already_in_a_group_not_folded_twice(self):
        titles = _wrapped_clip_titles()
        clip_index = compute_mpls_clip_index(titles)
        groups = compute_dedupe_groups(titles)
        once = fold_subsumption_into_groups(groups, clip_index, titles)
        twice = fold_subsumption_into_groups(once, clip_index, titles)
        assert twice[0].sibling_title_ids == once[0].sibling_title_ids

    def test_empty_clip_index_returns_groups_unchanged(self):
        titles = _wrapped_clip_titles()
        groups = compute_dedupe_groups(titles)
        folded = fold_subsumption_into_groups(groups, {}, titles)
        assert folded == groups

    def test_annotate_stamps_folded_m2ts_with_wrapper_group_id(self):
        titles = _wrapped_clip_titles()
        clip_index = compute_mpls_clip_index(titles)
        folded = fold_subsumption_into_groups(
            compute_dedupe_groups(titles), clip_index, titles,
        )
        annotate_titles_with_dedupe_group(titles, folded)
        gid = titles["uuid-mpls-a"]["dedupe_group_id"]
        assert gid is not None
        assert titles["uuid-m2ts-7"]["dedupe_group_id"] == gid
        assert titles["uuid-m2ts-8"]["dedupe_group_id"] == gid
        assert titles["uuid-m2ts-free"]["dedupe_group_id"] is None

    def test_apply_reason_on_unfolded_groups_leaves_m2ts_untouched(self):
        """Component clips are not decoys: the workflow-context builder runs
        apply_obfuscation_reason_from_dedupe BEFORE the fold, so folded m2ts
        must never receive obfuscation_reason='segment_set_sibling'."""
        titles = _wrapped_clip_titles()
        clip_index = compute_mpls_clip_index(titles)
        groups = compute_dedupe_groups(titles)
        rows = [_FakeRow(tid) for tid in titles.keys()]
        db = _fake_db(rows)

        apply_obfuscation_reason_from_dedupe(db, "disc-1", groups)
        fold_subsumption_into_groups(groups, clip_index, titles)

        m2ts_rows = [r for r in rows if r.id.startswith("uuid-m2ts")]
        assert all(r.obfuscation_reason is None for r in m2ts_rows)
        assert all(r.obfuscation_flag is False for r in m2ts_rows)
        # The true permutation sibling still got the high-tier reason.
        sib = next(r for r in rows if r.id == "uuid-mpls-b")
        assert sib.obfuscation_reason == "segment_set_sibling"
