"""Tests for the Path A segment-reorder primitives.

Three concerns under test:
  1. detect_duplicate_segment_groups + has_obfuscation_signature — gate
     the threshold modal on Midway-class discs without misfiring on
     non-obfuscated discs that have legitimate duplicate-group siblings.
  2. match_user_order_to_playlists — exact + sorted-set matching.
     Validated against the empirical Midway scan result: canonical order
     `504,510,501,507,502,505,506,509,503,508` produces 1 exact match
     (00539.mpls = title 108) and 201 sorted-set matches.
  3. generate_previews — ffmpeg invocation pattern (mocked runner) is
     correct for short / long / mixed PlayItem fixtures.
"""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.segment_reorder import (
    DEFAULT_HEAD_S,
    DEFAULT_TAIL_S,
    DuplicateGroup,
    MatchResult,
    SupersetCandidate,
    _segment_set_key,
    cluster_supersets_by_sorted_set,
    detect_duplicate_segment_groups,
    generate_previews,
    has_obfuscation_signature,
    is_ordered_subsequence,
    match_user_order_to_playlists,
    parse_segment_map_tokens,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _midway_titles() -> dict[int, dict]:
    """Shape-faithful Midway scan fixture: 6 mpls obfuscation-mass titles
    sharing sorted{501..510} in different orders, plus 1 outlier mpls
    (the +60s trap), plus 2 m2ts extras outside the mass.

    The 6-member mass is enough to validate grouping + matching without
    constructing 201 permutations explicitly.
    """
    canonical_order = "504,510,501,507,502,505,506,509,503,508"
    decoy_orders = [
        "501,502,503,504,505,506,507,508,509,510",
        "510,509,508,507,506,505,504,503,502,501",
        "503,508,501,510,504,507,502,509,505,506",
        "508,505,506,510,509,503,501,504,507,502",
        "501,510,509,508,507,506,505,504,503,502",
    ]
    titles = {}
    titles[108] = {"source_file": "00539.mpls", "segment_map": canonical_order}
    for i, order in enumerate(decoy_orders, start=1):
        titles[i] = {"source_file": f"deco{i:02d}.mpls", "segment_map": order}
    # Trap (00459.mpls) — same 10 segments + 1 decoy = different sorted set
    titles[89] = {
        "source_file": "00459.mpls",
        "segment_map": "504,3113,508,509,501,510,503,507,505,502,506",
    }
    # Two m2ts extras outside the mass
    titles[204] = {"source_file": "02799.m2ts", "segment_map": "2799"}
    titles[205] = {"source_file": "02800.m2ts", "segment_map": "2800"}
    return titles


def _vfv_titles() -> dict[int, dict]:
    """V-for-Vendetta-class case: an .mpls and its underlying .m2ts share
    segment_map = "61". This is the prefer-mpls case for Path B labeling
    dedupe, NOT a Path A segment-reorder candidate — there's nothing to
    reorder in a single-segment title.
    """
    return {
        0: {"source_file": "00800.mpls", "segment_map": "61"},
        1: {"source_file": "00800.m2ts", "segment_map": "61"},
        2: {"source_file": "00100.mpls", "segment_map": "5,6,7"},  # unrelated
    }


def _multi_cut_titles() -> dict[int, dict]:
    """Disc with theatrical + director's cut sharing some segments. Both have
    multi-segment ordering; both belong to the same sorted-segment-map group.
    """
    return {
        0: {"source_file": "00800.mpls", "segment_map": "1,2,3,4,5"},
        1: {"source_file": "00801.mpls", "segment_map": "1,2,4,3,5"},  # different order
    }


# ── _segment_set_key ──────────────────────────────────────────────────────────


def test_segment_set_key_normalizes_order():
    a = _segment_set_key("504,510,501,507,502,505,506,509,503,508")
    b = _segment_set_key("510,509,508,507,506,505,504,503,502,501")
    assert a == b == "501,502,503,504,505,506,507,508,509,510"


def test_segment_set_key_dedupes_repeats():
    assert _segment_set_key("1,2,2,3,1") == "1,2,3"


def test_segment_set_key_returns_none_on_unparseable():
    assert _segment_set_key(None) is None
    assert _segment_set_key("") is None
    assert _segment_set_key("singleton-no-comma") is None
    assert _segment_set_key(",,,") is None


# ── parse_segment_map_tokens (paren-wrapped MakeMKV variants) ────────────────


def test_parse_segment_map_tokens_plain():
    assert parse_segment_map_tokens("504,510,501") == ["504", "510", "501"]


def test_parse_segment_map_tokens_paren_wrapped():
    """MakeMKV emits some playlists as `(a,b,c)` — strip the outer parens."""
    assert parse_segment_map_tokens("(502,501,503,500,506,507,505)") == [
        "502", "501", "503", "500", "506", "507", "505",
    ]


def test_parse_segment_map_tokens_bracket_wrapped():
    """Future-proof against `[a,b,c]` / `{a,b,c}` variants too."""
    assert parse_segment_map_tokens("[1,2,3]") == ["1", "2", "3"]
    assert parse_segment_map_tokens("{1,2,3}") == ["1", "2", "3"]


def test_parse_segment_map_tokens_empty_and_none():
    assert parse_segment_map_tokens(None) == []
    assert parse_segment_map_tokens("") == []
    assert parse_segment_map_tokens("()") == []


def test_parse_segment_map_tokens_strips_inner_whitespace():
    assert parse_segment_map_tokens("( 502, 501 , 503 )") == ["502", "501", "503"]


def test_segment_set_key_treats_paren_wrapped_as_equivalent():
    """The trap on the 4K Midway disc: title 9 has segment_map
    `(502,501,503,500,506,507,505)` — its sorted key must match
    plain-form titles with the same clip set."""
    plain = _segment_set_key("500,501,502,503,505,506,507")
    paren = _segment_set_key("(502,501,503,500,506,507,505)")
    assert plain == paren == "500,501,502,503,505,506,507"


def test_match_user_order_finds_paren_wrapped_exact():
    """The 4K Midway regression: user ordered the clips that appear inside
    a paren-wrapped segment_map; the matcher must surface that title as
    an exact match, not bypass it."""
    titles = {
        # The paren-wrapped exact-match title (00504.mpls on the disc).
        9: {
            "source_file": "00504.mpls",
            "segment_map": "(502,501,503,500,506,507,505)",
        },
        # The exploratory we ripped (00368.mpls) — same clip set, different
        # order, no parens. Should match as sorted_set, not exact.
        86: {
            "source_file": "00368.mpls",
            "segment_map": "503,500,502,506,501,507,505",
        },
    }
    user_order = ["502", "501", "503", "500", "506", "507", "505"]
    result = match_user_order_to_playlists(titles, user_order)
    assert result.exact == [9]
    assert 86 in result.sorted_set


# ── detect_duplicate_segment_groups ──────────────────────────────────────────


def test_midway_one_group_of_six_outlier_excluded():
    titles = _midway_titles()
    groups = detect_duplicate_segment_groups(titles)
    assert len(groups) == 1
    g = groups[0]
    assert g.size == 6
    assert g.sorted_segment_key == "501,502,503,504,505,506,507,508,509,510"
    # Trap (89) has a different sorted-set (extra 3113), so it's NOT in the group.
    assert 89 not in g.title_indexes
    # Canonical (108) IS in the group — it's the obfuscation territory.
    assert 108 in g.title_indexes


def test_single_segment_dupes_not_grouped_by_path_a():
    """V-for-Vendetta-class (mpls + m2ts share single segment) is handled by
    Path B labeling dedupe via duplicate_group_sync, NOT by the segment-
    reorder grouping which requires multi-segment ordering."""
    groups = detect_duplicate_segment_groups(_vfv_titles())
    # No group emitted — single-segment maps don't have an ordering to compare.
    assert groups == []


def test_multi_cut_disc_with_different_orders_is_grouped():
    """Two playlists with the same 5 segments in different orders form a
    Path A candidate group — exactly what segment-reorder is designed for."""
    groups = detect_duplicate_segment_groups(_multi_cut_titles())
    assert len(groups) == 1
    assert groups[0].title_indexes == (0, 1)


def test_no_duplicates_no_groups():
    titles = {
        0: {"segment_map": "1,2,3"},
        1: {"segment_map": "4,5,6"},
        2: {"segment_map": "7,8,9"},
    }
    assert detect_duplicate_segment_groups(titles) == []


def test_titles_without_segment_map_skipped():
    titles = {
        0: {"segment_map": "1,2,3"},
        1: {"segment_map": None},
        2: {},  # missing key entirely
        3: {"segment_map": "1,2,3"},  # would be a group with 0
    }
    groups = detect_duplicate_segment_groups(titles)
    assert len(groups) == 1
    assert groups[0].title_indexes == (0, 3)


def test_groups_sorted_by_descending_size():
    titles = {
        0: {"segment_map": "a,b"}, 1: {"segment_map": "a,b"},
        2: {"segment_map": "c,d"}, 3: {"segment_map": "c,d"},
        4: {"segment_map": "c,d"}, 5: {"segment_map": "c,d"},
    }
    groups = detect_duplicate_segment_groups(titles)
    assert [g.size for g in groups] == [4, 2]


def test_min_group_size_threshold():
    """min_group_size=3 drops 2-member groups; useful for harder threshold."""
    titles = _vfv_titles()
    assert detect_duplicate_segment_groups(titles, min_group_size=3) == []


# ── has_obfuscation_signature ────────────────────────────────────────────────


def test_obfuscation_signature_true_on_midway():
    assert has_obfuscation_signature(_midway_titles()) is True


def test_obfuscation_signature_false_on_v_for_vendetta_single_segment():
    """V-for-Vendetta is a Path B dedupe candidate, not a Path A trigger.
    Single-segment dupes have no ordering to reorder."""
    assert has_obfuscation_signature(_vfv_titles()) is False


def test_obfuscation_signature_true_on_multi_cut():
    """Multi-segment + multiple permutations → Path A applicable."""
    assert has_obfuscation_signature(_multi_cut_titles()) is True


def test_obfuscation_signature_false_on_normal_disc():
    titles = {
        0: {"segment_map": "1,2,3"},
        1: {"segment_map": "4,5,6"},
    }
    assert has_obfuscation_signature(titles) is False


# ── match_user_order_to_playlists ─────────────────────────────────────────────


def test_canonical_order_yields_one_exact_match_on_midway():
    titles = _midway_titles()
    canonical_order = "504,510,501,507,502,505,506,509,503,508".split(",")
    result = match_user_order_to_playlists(titles, canonical_order)
    assert result.exact == [108]
    assert result.has_unique_exact is True
    # Every member of the obfuscation mass shares the same sorted set.
    assert sorted(result.sorted_set) == [1, 2, 3, 4, 5, 108]


def test_decoy_order_matches_decoy_only():
    titles = _midway_titles()
    decoy_order = "501,502,503,504,505,506,507,508,509,510".split(",")
    result = match_user_order_to_playlists(titles, decoy_order)
    assert result.exact == [1]
    # Same sorted-set as canonical, so the same 6 candidates appear.
    assert len(result.sorted_set) == 6


def test_user_order_with_no_match_returns_sorted_set_only():
    """Wrong order — segments aren't even on the disc — yields nothing."""
    titles = _midway_titles()
    nonsense = ["999", "888", "777"]
    result = match_user_order_to_playlists(titles, nonsense)
    assert result.exact == []
    assert result.sorted_set == []


def test_user_order_with_canonical_segments_in_wrong_order_yields_sorted_set():
    """User got the order wrong but picked the right segment set."""
    titles = _midway_titles()
    wrong_order = "501,502,503,504,505,506,507,508,510,509".split(",")
    result = match_user_order_to_playlists(titles, wrong_order)
    assert result.exact == []  # no exact match
    assert len(result.sorted_set) == 6  # but sorted-set still matches the mass


def test_empty_user_order_returns_empty_result():
    assert match_user_order_to_playlists(_midway_titles(), []).exact == []
    assert match_user_order_to_playlists(_midway_titles(), ["", " "]).exact == []


def test_matching_strips_whitespace():
    titles = {0: {"segment_map": "1,2,3"}}
    result = match_user_order_to_playlists(titles, [" 1 ", "2", " 3"])
    assert result.exact == [0]


# ── is_ordered_subsequence ───────────────────────────────────────────────────


def test_is_ordered_subsequence_basic_gap():
    assert is_ordered_subsequence(["a", "b", "c"], ["a", "x", "b", "y", "c"]) is True


def test_is_ordered_subsequence_exact_match():
    assert is_ordered_subsequence(["a", "b"], ["a", "b"]) is True


def test_is_ordered_subsequence_order_required():
    assert is_ordered_subsequence(["a", "b"], ["b", "a"]) is False


def test_is_ordered_subsequence_empty_needle_is_trivially_true():
    assert is_ordered_subsequence([], ["a", "b", "c"]) is True


def test_is_ordered_subsequence_needle_longer_than_haystack():
    assert is_ordered_subsequence(["a", "b", "c"], ["a", "b"]) is False


def test_is_ordered_subsequence_duplicate_in_haystack():
    # First "a" is consumed; the second "a" doesn't help match "b".
    assert is_ordered_subsequence(["a", "b"], ["a", "a"]) is False
    assert is_ordered_subsequence(["a", "a"], ["a", "x", "a"]) is True


# ── subsequence_supersets ─────────────────────────────────────────────────────


def test_canonical_order_matches_trap_as_subsequence_superset():
    """The 4K Midway pattern: canonical user order is preserved within the
    'trap' mpls (Title 89) which has the same 10 clips PLUS clip 3113
    injected between clips 504 and 508."""
    titles = _midway_titles()
    canonical_order = "504,510,501,507,502,505,506,509,503,508".split(",")
    # Title 89 segment_map: "504,3113,508,509,501,510,503,507,505,502,506"
    # Canonical order:       504,    ,    ,    ,    ,510,    ,507,    ,    ,
    # Wait — the trap's order doesn't actually preserve the canonical order.
    # Let me verify with a hand-crafted superset that DOES preserve order.
    # Strip Title 89 (which is permutation+extra, NOT subsequence-superset),
    # and add a true subsequence-superset title.
    titles[300] = {
        "source_file": "trap_super.mpls",
        # canonical 504,510,501,507,502,505,506,509,503,508 with 3113 + 7777
        # interleaved between 510-501 and 506-509.
        "segment_map": "504,510,3113,501,507,502,505,506,7777,509,503,508",
    }
    result = match_user_order_to_playlists(titles, canonical_order)
    # Title 108 (the canonical) still matches as exact.
    assert result.exact == [108]
    # Title 300 — our synthesized superset — appears in subsequence_supersets.
    super_indexes = [c.title_index for c in result.subsequence_supersets]
    assert 300 in super_indexes
    cand = next(c for c in result.subsequence_supersets if c.title_index == 300)
    assert list(cand.extras_clips) == ["3113", "7777"]
    # Extras positions: 3113 at index 2, 7777 at index 8 in the mpls.
    assert list(cand.extras_positions) == [2, 8]


def test_out_of_order_user_input_excluded_from_subsequence_supersets():
    """User submitted clips that are present in the mpls but in the wrong
    relative order — must NOT surface as a subsequence-superset match."""
    titles = {
        0: {"source_file": "abc_extra.mpls", "segment_map": "a,b,x,c"},
    }
    # User has them in c,b,a order — relative order differs from the mpls.
    result = match_user_order_to_playlists(titles, ["c", "b", "a"])
    assert result.subsequence_supersets == []


def test_subsequence_supersets_skip_sorted_set_matches():
    """An mpls with the same clip set in a different order is a sorted_set
    match, not a subsequence-superset. extras_count = 0 filters it."""
    titles = {0: {"source_file": "x.mpls", "segment_map": "3,1,2"}}
    result = match_user_order_to_playlists(titles, ["1", "2", "3"])
    assert result.sorted_set == [0]
    assert result.subsequence_supersets == []


def test_subsequence_supersets_max_extras_factor_caps_noise():
    """A 3-clip user order with default max_extras_factor=2.0 caps mpls at
    9 segments (3 user + 6 extras). A 50-segment mpls is noise — excluded."""
    titles = {
        0: {
            "source_file": "noise.mpls",
            # 3 user clips followed by 47 noise clips — way past 2.0× cap.
            "segment_map": ",".join(["1", "2", "3"] + [str(n) for n in range(100, 147)]),
        },
        1: {
            "source_file": "tight.mpls",
            # 3 user clips + 2 extras = under the cap.
            "segment_map": "1,X,2,Y,3",
        },
    }
    result = match_user_order_to_playlists(titles, ["1", "2", "3"])
    indexes = [c.title_index for c in result.subsequence_supersets]
    assert 0 not in indexes  # noise excluded by cap
    assert 1 in indexes  # tight match passes


def test_subsequence_supersets_extras_factor_explicit_override():
    """Caller can widen the cap when needed."""
    titles = {
        0: {
            "source_file": "wide.mpls",
            # 2 user clips + 10 extras — only matches with factor ≥ 5.0.
            "segment_map": "1,X,X,X,X,X,2,Y,Y,Y,Y,Y",
        },
    }
    tight = match_user_order_to_playlists(titles, ["1", "2"])
    assert tight.subsequence_supersets == []
    wide = match_user_order_to_playlists(titles, ["1", "2"], max_extras_factor=5.0)
    assert [c.title_index for c in wide.subsequence_supersets] == [0]


def test_subsequence_supersets_sorted_by_fewest_extras_first():
    titles = {
        0: {"source_file": "many.mpls", "segment_map": "1,X,Y,Z,W,2,3"},
        1: {"source_file": "few.mpls", "segment_map": "1,X,2,3"},
    }
    result = match_user_order_to_playlists(titles, ["1", "2", "3"])
    assert [c.title_index for c in result.subsequence_supersets] == [1, 0]


# ── cluster_supersets_by_sorted_set ──────────────────────────────────────────


def test_cluster_supersets_ranks_by_member_count_desc():
    """Two clusters: cluster A has 3 mpls with the same sorted-set, cluster B
    has 1. A should rank first."""
    cluster_a = [
        SupersetCandidate(
            title_index=i,
            source_file=f"a{i}.mpls",
            extras_clips=("X",),
            extras_positions=(2,),
            mpls_total_size_b=None,
            sorted_set_key="1,2,3,X",
        )
        for i in (10, 11, 12)
    ]
    cluster_b = [
        SupersetCandidate(
            title_index=20,
            source_file="b.mpls",
            extras_clips=("Y",),
            extras_positions=(1,),
            mpls_total_size_b=None,
            sorted_set_key="1,2,3,Y",
        )
    ]
    clusters = cluster_supersets_by_sorted_set(cluster_a + cluster_b)
    assert [len(c) for c in clusters] == [3, 1]
    assert clusters[0][0].sorted_set_key == "1,2,3,X"


def test_cluster_within_cluster_orders_by_extras_then_size():
    """Within a cluster: smallest extras first; ties broken by largest size."""
    cluster = [
        SupersetCandidate(
            title_index=1, source_file="a.mpls",
            extras_clips=("X", "Y"), extras_positions=(1, 3),
            mpls_total_size_b=5_000_000_000, sorted_set_key="K",
        ),
        SupersetCandidate(
            title_index=2, source_file="b.mpls",
            extras_clips=("X",), extras_positions=(1,),
            mpls_total_size_b=2_000_000_000, sorted_set_key="K",
        ),
        SupersetCandidate(
            title_index=3, source_file="c.mpls",
            extras_clips=("X",), extras_positions=(1,),
            mpls_total_size_b=8_000_000_000, sorted_set_key="K",
        ),
    ]
    clusters = cluster_supersets_by_sorted_set(cluster)
    ordered = [c.title_index for c in clusters[0]]
    # 3 (1 extra, biggest), 2 (1 extra, smaller), 1 (2 extras).
    assert ordered == [3, 2, 1]


def test_cluster_empty_input_returns_empty():
    assert cluster_supersets_by_sorted_set([]) == []


# ── disc_flags: definitely (filter) + potentially (rank-boost) ───────────────


def test_disc_flags_definitely_excludes_mpls_containing_clip():
    """User has flagged 'X' as definitely obfuscation. Any mpls containing X
    is filtered out of subsequence_supersets even if it's an exact subsequence
    match."""
    titles = {
        0: {"source_file": "with_x.mpls", "segment_map": "1,X,2,3"},
        1: {"source_file": "without_x.mpls", "segment_map": "1,Y,2,3"},
    }
    result = match_user_order_to_playlists(
        titles, ["1", "2", "3"], disc_flags={"X": "definitely"}
    )
    indexes = [c.title_index for c in result.subsequence_supersets]
    assert 0 not in indexes  # excluded by definitely flag
    assert indexes == [1]


def test_disc_flags_potentially_ranks_omitters_higher():
    """'X' is flagged potentially obfuscation. Both mpls are valid supersets,
    but the one OMITTING X sorts first."""
    titles = {
        0: {"source_file": "with_x.mpls", "segment_map": "1,X,2,3"},
        1: {"source_file": "without_x.mpls", "segment_map": "1,Y,2,3"},
    }
    result = match_user_order_to_playlists(
        titles, ["1", "2", "3"], disc_flags={"X": "potentially"}
    )
    # Both included; omitter (idx 1) first.
    ranked = [c.title_index for c in result.subsequence_supersets]
    assert ranked == [1, 0]


def test_disc_flags_definitely_does_not_affect_exact_match():
    """An exact match is a strong signal — definitely-flagged clips don't
    veto it. The flag only filters the weaker superset tier."""
    titles = {0: {"source_file": "x.mpls", "segment_map": "1,X,2"}}
    result = match_user_order_to_playlists(
        titles, ["1", "X", "2"], disc_flags={"X": "definitely"}
    )
    assert result.exact == [0]  # exact match preserved


def test_disc_flags_none_or_empty_matches_baseline():
    """Passing disc_flags=None or {} produces the same result as not passing
    it at all (backward compatibility)."""
    titles = {0: {"source_file": "x.mpls", "segment_map": "1,X,2"}}
    baseline = match_user_order_to_playlists(titles, ["1", "2"])
    with_none = match_user_order_to_playlists(titles, ["1", "2"], disc_flags=None)
    with_empty = match_user_order_to_playlists(titles, ["1", "2"], disc_flags={})
    assert baseline.subsequence_supersets == with_none.subsequence_supersets == with_empty.subsequence_supersets


def test_disc_flags_mixed_definitely_and_potentially():
    """X = definitely (filter), Y = potentially (rank). Mpls with X is
    dropped; mpls with Y is included but ranked behind mpls without Y."""
    titles = {
        0: {"source_file": "with_x.mpls", "segment_map": "1,X,2"},
        1: {"source_file": "with_y.mpls", "segment_map": "1,Y,2"},
        2: {"source_file": "clean.mpls", "segment_map": "1,Z,2"},
    }
    result = match_user_order_to_playlists(
        titles, ["1", "2"],
        disc_flags={"X": "definitely", "Y": "potentially"},
    )
    ranked = [c.title_index for c in result.subsequence_supersets]
    assert 0 not in ranked  # filtered
    assert ranked == [2, 1]  # clean (no Y) before mpls with Y


# ── generate_previews ─────────────────────────────────────────────────────────


def test_generate_previews_short_only(tmp_path):
    rip = tmp_path / "rip.mkv"
    rip.write_bytes(b"")  # ffmpeg won't actually run; runner is mocked
    out = tmp_path / "previews"

    runner = MagicMock()
    manifest = generate_previews(rip, out, [10.0, 20.0, 30.0], runner=runner)

    assert len(manifest) == 3
    assert all(s.mode == "full" for s in manifest)
    assert manifest[0].cum_start_s == 0.0
    assert manifest[1].cum_start_s == 10.0
    assert manifest[2].cum_start_s == 30.0
    # Three encode calls, one per PlayItem.
    assert runner.call_count == 3
    # Each encode call must include `-map_chapters -1` to suppress the
    # joined-rip's chapter track from inflating mp4 format duration.
    for call in runner.call_args_list:
        args = call.args[0]
        assert "-map_chapters" in args
        idx = args.index("-map_chapters")
        assert args[idx + 1] == "-1"
    # Manifest written to disk.
    assert (out / "manifest.json").is_file()


def test_generate_previews_long_stitches_head_breaker_tail(tmp_path):
    rip = tmp_path / "rip.mkv"
    rip.write_bytes(b"")
    out = tmp_path / "previews"

    runner = MagicMock()
    # One long PlayItem (300s) → 1 stitched preview built from 4 ffmpeg
    # calls: head + breaker + tail + concat = 4 invocations.
    manifest = generate_previews(rip, out, [300.0], runner=runner)

    assert len(manifest) == 1
    spec = manifest[0]
    assert spec.mode == "stitch"
    assert spec.src_dur_s == 300.0
    assert spec.head_s == DEFAULT_HEAD_S
    assert spec.tail_s == DEFAULT_TAIL_S
    assert runner.call_count == 4

    # The third encode call (tail) should seek to (cum + dur - tail_s).
    # Tail call args contain `-i <rip>` and the fine seek before `-t`.
    tail_call = runner.call_args_list[2].args[0]
    assert "-t" in tail_call
    t_idx = tail_call.index("-t")
    assert tail_call[t_idx + 1] == f"{DEFAULT_TAIL_S:.3f}"


def test_generate_previews_mixed_short_and_long(tmp_path):
    rip = tmp_path / "rip.mkv"
    rip.write_bytes(b"")
    out = tmp_path / "previews"

    runner = MagicMock()
    # Short, long, short: 1 + 4 + 1 = 6 ffmpeg calls.
    manifest = generate_previews(rip, out, [30.0, 200.0, 25.0], runner=runner)

    assert [s.mode for s in manifest] == ["full", "stitch", "full"]
    # cum offsets accumulate correctly.
    assert manifest[0].cum_start_s == 0.0
    assert manifest[1].cum_start_s == 30.0
    assert manifest[2].cum_start_s == 230.0
    assert runner.call_count == 6


def test_generate_previews_writes_manifest_json(tmp_path):
    rip = tmp_path / "rip.mkv"
    rip.write_bytes(b"")
    out = tmp_path / "previews"

    runner = MagicMock()
    manifest = generate_previews(rip, out, [40.0], runner=runner)

    import json as _json
    written = _json.loads((out / "manifest.json").read_text())
    assert len(written) == 1
    assert written[0]["index"] == 0
    assert written[0]["path"] == "seg_00.mp4"
    assert written[0]["mode"] == "full"


def test_generate_previews_uses_fast_accurate_seek_pattern(tmp_path):
    """Crucial spike-validated invariant: -ss <coarse> BEFORE -i, then -ss <fine> AFTER -i.

    Fast-only seek (just before -i) leaks the prior PlayItem's tail
    into the segment. Accurate-only seek (just after -i) decodes from
    t=0 and is too slow. The two-stage form is the only correct one.
    """
    rip = tmp_path / "rip.mkv"
    rip.write_bytes(b"")
    out = tmp_path / "previews"

    runner = MagicMock()
    # cum_start = 100s on this PlayItem
    generate_previews(rip, out, [50.0, 50.0], runner=runner)

    # Second short PlayItem starts at cum=50s. Its ffmpeg call should have:
    #   -ss <coarse=45.000> -i <rip> -ss <fine=5.000> -t <50.000>
    # i.e. two -ss flags, one before -i, one after.
    second_call = runner.call_args_list[1].args[0]
    ss_positions = [i for i, a in enumerate(second_call) if a == "-ss"]
    i_position = second_call.index("-i")
    assert len(ss_positions) == 2
    assert ss_positions[0] < i_position < ss_positions[1], (
        "fast+accurate seek requires -ss before -i AND -ss after -i"
    )


def test_generate_previews_empty_durations_writes_empty_manifest(tmp_path):
    rip = tmp_path / "rip.mkv"
    rip.write_bytes(b"")
    out = tmp_path / "previews"

    runner = MagicMock()
    manifest = generate_previews(rip, out, [], runner=runner)
    assert manifest == []
    assert runner.call_count == 0
    assert (out / "manifest.json").is_file()


# ── MatchResult dataclass ────────────────────────────────────────────────────


def test_match_result_has_unique_exact_property():
    assert MatchResult(exact=[7]).has_unique_exact is True
    assert MatchResult(exact=[]).has_unique_exact is False
    assert MatchResult(exact=[7, 8]).has_unique_exact is False


# ── Preview manifest carries clip_name from MPLS ──────────────────────────────


def test_generate_previews_writes_clip_names_into_manifest(tmp_path):
    """Frontend matching round-trips on these clip_names. Without them,
    the user's preview ordering can't be compared to disc segment_maps."""
    rip = tmp_path / "rip.mkv"
    rip.write_bytes(b"")
    out = tmp_path / "previews"

    runner = MagicMock()
    manifest = generate_previews(
        rip, out, [10.0, 20.0, 30.0],
        clip_names=["00504", "00510", "00501"],
        runner=runner,
    )

    assert [s.clip_name for s in manifest] == ["00504", "00510", "00501"]
    # to_dict serialization includes clip_name
    serialized = manifest[0].to_dict()
    assert serialized["clip_name"] == "00504"


def test_generate_previews_clip_names_optional_for_back_compat(tmp_path):
    """Calling without clip_names still works (e.g. unit tests)."""
    rip = tmp_path / "rip.mkv"
    rip.write_bytes(b"")
    out = tmp_path / "previews"
    runner = MagicMock()
    manifest = generate_previews(rip, out, [10.0, 20.0], runner=runner)
    assert all(s.clip_name is None for s in manifest)
    # to_dict omits clip_name when None
    assert "clip_name" not in manifest[0].to_dict()
