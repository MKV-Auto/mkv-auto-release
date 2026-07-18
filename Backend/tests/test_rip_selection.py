"""Tests for selective-rip set construction.

This module is invoked only from the Phase 2 Path A flow on Midway-class
obfuscated discs. The default rip path (`mkv DEV all OUT`) does not call
`build_rip_title_set` — it's specifically for the case where we need to
exclude the unmatched same-sorted-segment-map siblings before passing
indexes to the per-title rip loop.
"""
import pytest

from core.rip_selection import build_rip_title_set


# ── Midway-shaped fixture ─────────────────────────────────────────────────────
# 213 titles total: 201 mpls forming the obfuscation mass (10 segment IDs,
# permuted) + 12 non-mass titles. Real canonical (per DiscDB) = title 108
# (00539.mpls), which is a member of the obfuscation mass.
# Non-mass titles in the empirical Midway scan: titles 57, 86, 125, 166, 200,
# and 204-210 (the 7 m2ts extras). Titles 211-212 are short fragments.
MIDWAY_ALL_INDEXES = list(range(213))
MIDWAY_NON_MASS = {57, 86, 125, 166, 200, 204, 205, 206, 207, 208, 209, 210, 211, 212}
MIDWAY_DUPLICATE_GROUP = [i for i in range(213) if i not in MIDWAY_NON_MASS]
MIDWAY_CANONICAL = 108  # == 00539.mpls in production


def test_midway_canonical_keeps_only_canonical_from_duplicate_group():
    rip_set = build_rip_title_set(
        MIDWAY_ALL_INDEXES,
        canonical_title_index=MIDWAY_CANONICAL,
        duplicate_group_member_indexes=MIDWAY_DUPLICATE_GROUP,
    )
    # All non-mass titles plus 1 canonical.
    expected_len = len(MIDWAY_NON_MASS) + 1
    assert len(rip_set) == expected_len
    assert MIDWAY_CANONICAL in rip_set
    # No other duplicate-group member made it through.
    decoys = set(MIDWAY_DUPLICATE_GROUP) - {MIDWAY_CANONICAL}
    assert not (set(rip_set) & decoys)
    # Non-mass titles are preserved.
    for non_mass in MIDWAY_NON_MASS:
        assert non_mass in rip_set


def test_returns_sorted_list():
    rip_set = build_rip_title_set(
        [5, 0, 9, 3, 7],
        canonical_title_index=3,
        duplicate_group_member_indexes=[3, 9],
    )
    assert rip_set == [0, 3, 5, 7]


def test_single_title_disc_no_decoys():
    """Edge case: rip_set is just the canonical when it's the only title."""
    rip_set = build_rip_title_set(
        [0],
        canonical_title_index=0,
        duplicate_group_member_indexes=[0],
    )
    assert rip_set == [0]


def test_canonical_is_only_member_of_group():
    """If the duplicate group has only the canonical, we drop nothing."""
    rip_set = build_rip_title_set(
        [0, 1, 2, 3],
        canonical_title_index=2,
        duplicate_group_member_indexes=[2],
    )
    assert rip_set == [0, 1, 2, 3]


def test_canonical_not_in_all_titles_raises():
    with pytest.raises(ValueError, match="not in all_title_indexes"):
        build_rip_title_set(
            [0, 1, 2],
            canonical_title_index=99,
            duplicate_group_member_indexes=[99, 1],
        )


def test_canonical_not_in_duplicate_group_raises():
    with pytest.raises(ValueError, match="not in duplicate_group_member_indexes"):
        build_rip_title_set(
            [0, 1, 2, 3],
            canonical_title_index=0,
            duplicate_group_member_indexes=[1, 2, 3],
        )


def test_handles_iterable_inputs_not_just_lists():
    """Caller may pass any iterable; we don't assume list."""
    rip_set = build_rip_title_set(
        all_title_indexes=range(5),
        canonical_title_index=2,
        duplicate_group_member_indexes={1, 2, 3},
    )
    assert rip_set == [0, 2, 4]


def test_duplicate_indexes_in_inputs_dedupe_correctly():
    """If the caller hands us duplicates, the set semantics still hold."""
    rip_set = build_rip_title_set(
        [0, 1, 1, 2, 2, 2, 3],
        canonical_title_index=2,
        duplicate_group_member_indexes=[2, 2, 1],
    )
    assert rip_set == [0, 2, 3]
