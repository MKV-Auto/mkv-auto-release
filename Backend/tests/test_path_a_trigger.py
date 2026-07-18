"""Tests for the Path A trigger gate.

The gate fires only when BOTH conditions hold:
  1. The disc has at least one duplicate-segment-map group, AND
  2. The projected rip exceeds the threshold (default 200 GB, env-overridable).

A normal disc (no duplicates) bypasses the gate entirely — Path A doesn't
fire and the default `mkv DEV all OUT` rip proceeds. A small obfuscated
disc (groups exist but projected < threshold) also bypasses the gate.
Only the Midway-class case (groups + huge projected size) returns 409.
"""
import pytest

from core.path_a_trigger import (
    DEFAULT_THRESHOLD_GB,
    evaluate_path_a_trigger,
    get_threshold_gb,
)


GB = 1024 ** 3


def _midway_titles_with_huge_projection():
    """6 mass titles each ~37 GB + 1 outlier = ~6 × 37 GB projected (well over 200 GB).

    The actual size field is what calculate_required_rip_space_bytes sums,
    so we set per-title size in bytes to make the math reproducible.
    """
    titles = {}
    canonical_order = "504,510,501,507,502,505,506,509,503,508"
    decoy_orders = [
        "501,502,503,504,505,506,507,508,509,510",
        "510,509,508,507,506,505,504,503,502,501",
        "503,508,501,510,504,507,502,509,505,506",
        "508,505,506,510,509,503,501,504,507,502",
        "501,510,509,508,507,506,505,504,503,502",
    ]
    titles[108] = {
        "source_file": "00539.mpls", "segment_map": canonical_order,
        "size": 39 * GB,
    }
    for i, order in enumerate(decoy_orders, start=1):
        titles[i] = {
            "source_file": f"deco{i:02d}.mpls", "segment_map": order,
            "size": 39 * GB,
        }
    # Trap (different sorted set)
    titles[89] = {
        "source_file": "00459.mpls",
        "segment_map": "504,3113,508,509,501,510,503,507,505,502,506",
        "size": 40 * GB,
    }
    return titles


def _normal_uhd_titles():
    """Single main feature, no duplicates. Even at 80 GB it shouldn't trigger."""
    return {
        0: {"source_file": "00800.mpls", "segment_map": "1,2,3,4,5", "size": 80 * GB},
        1: {"source_file": "00801.mpls", "segment_map": "10,11,12", "size": 5 * GB},
    }


def _small_obfuscated_disc_titles():
    """Has duplicate-segment-map groups but the total projected size is well
    under threshold (multi-cut disc with two short cuts sharing segments).
    Should NOT trigger Path A — modal would just be noise."""
    return {
        0: {"source_file": "00100.mpls", "segment_map": "1,2,3,4", "size": 5 * GB},
        1: {"source_file": "00101.mpls", "segment_map": "1,3,2,4", "size": 5 * GB},
    }


# ── evaluate_path_a_trigger ───────────────────────────────────────────────────


def test_midway_class_disc_triggers_path_a():
    titles = _midway_titles_with_huge_projection()
    decision = evaluate_path_a_trigger(titles, disc_size_bytes=None)
    assert decision.needs_user_choice is True
    assert decision.reason == "obfuscation_and_over_threshold"
    assert len(decision.duplicate_groups) == 1
    assert decision.projected_rip_bytes is not None
    assert decision.projected_rip_bytes > DEFAULT_THRESHOLD_GB * GB


def test_normal_disc_bypasses_gate():
    """No duplicate groups → Path A never fires regardless of size."""
    decision = evaluate_path_a_trigger(_normal_uhd_titles(), disc_size_bytes=None)
    assert decision.needs_user_choice is False
    assert decision.reason == "no_duplicate_segment_groups"
    assert decision.duplicate_groups == []


def test_small_obfuscated_disc_bypasses_gate():
    """Duplicate groups but projected size below threshold → no modal."""
    decision = evaluate_path_a_trigger(
        _small_obfuscated_disc_titles(), disc_size_bytes=None,
    )
    assert decision.needs_user_choice is False
    assert decision.reason == "below_threshold"
    # Groups still get reported for diagnostic / Path B downstream use.
    assert len(decision.duplicate_groups) == 1


def test_empty_titles_dict_bypasses_gate():
    decision = evaluate_path_a_trigger({}, disc_size_bytes=None)
    assert decision.needs_user_choice is False
    assert decision.reason == "no_titles_to_evaluate"


def test_none_titles_bypasses_gate():
    decision = evaluate_path_a_trigger(None, disc_size_bytes=None)
    assert decision.needs_user_choice is False
    assert decision.reason == "no_titles_to_evaluate"


def test_threshold_can_be_overridden_per_call():
    """Ops scenario: bump threshold to 500 GB; Midway-class no longer triggers."""
    decision = evaluate_path_a_trigger(
        _midway_titles_with_huge_projection(),
        disc_size_bytes=None,
        threshold_gb=500,
    )
    assert decision.needs_user_choice is False
    assert decision.reason == "below_threshold"


def test_409_payload_shape_for_frontend():
    titles = _midway_titles_with_huge_projection()
    decision = evaluate_path_a_trigger(titles, disc_size_bytes=None)
    payload = decision.to_409_payload(available_disk_bytes=500 * GB)

    assert payload["code"] == "needs_user_choice"
    assert payload["threshold_gb"] == DEFAULT_THRESHOLD_GB
    assert payload["projected_rip_bytes"] > 0
    assert payload["available_disk_bytes"] == 500 * GB
    assert payload["duplicate_group_count"] == 1
    # Each candidate carries its title index + duplicate group context
    # the modal needs to render "candidate 1: title 108 (00539.mpls)".
    assert all(
        "title_index" in c and "duplicate_group_size" in c and "sorted_segment_key" in c
        for c in payload["candidates"]
    )
    # Exactly the 6 mass-group members appear as candidates.
    assert len(payload["candidates"]) == 6


def test_disc_size_fallback_when_titles_have_no_size():
    """If per-title sizes are missing, fall back to disc_size_bytes for projection."""
    titles = {
        0: {"source_file": "a.mpls", "segment_map": "1,2,3"},
        1: {"source_file": "b.mpls", "segment_map": "3,2,1"},
    }
    # No per-title size; pass disc_size = 500 GB
    decision = evaluate_path_a_trigger(titles, disc_size_bytes=500 * GB)
    # 500 GB × 1.3 buffer = 650 GB projected — over threshold + groups present.
    assert decision.needs_user_choice is True


# ── get_threshold_gb env override ────────────────────────────────────────────


def test_default_threshold_when_env_unset(monkeypatch):
    monkeypatch.delenv("MKVAUTO_RIP_REVIEW_THRESHOLD_GB", raising=False)
    assert get_threshold_gb() == DEFAULT_THRESHOLD_GB


def test_env_override_applied(monkeypatch):
    monkeypatch.setenv("MKVAUTO_RIP_REVIEW_THRESHOLD_GB", "350")
    assert get_threshold_gb() == 350


def test_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("MKVAUTO_RIP_REVIEW_THRESHOLD_GB", "not-an-int")
    assert get_threshold_gb() == DEFAULT_THRESHOLD_GB


def test_zero_or_negative_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("MKVAUTO_RIP_REVIEW_THRESHOLD_GB", "0")
    assert get_threshold_gb() == DEFAULT_THRESHOLD_GB
    monkeypatch.setenv("MKVAUTO_RIP_REVIEW_THRESHOLD_GB", "-50")
    assert get_threshold_gb() == DEFAULT_THRESHOLD_GB


# ── Auto-pick exploratory title (precedence) ─────────────────────────────────
#
# Within a single duplicate-segment-map group, every member references the
# same physical segments — the choice doesn't affect what the user sees in
# previews, just which mpls filename gets fed to the MPLS parser. Backend
# auto-picks via DiscDB > MakeMKV flag-clear > first member.


def _midway_titles_with_discdb_pick():
    """6-member group; only title 108 is DiscDB-classified (type='movie')."""
    titles = _midway_titles_with_huge_projection()
    # The default fixture leaves type unset on every entry; populate the
    # canonical (108) as DiscDB-classified.
    titles[108]["type"] = "movie"
    titles[108]["obfuscation_flag"] = True
    for i in range(1, 6):
        titles[i]["type"] = None
        titles[i]["obfuscation_flag"] = True
    return titles


def test_auto_pick_prefers_discdb_classified_member():
    titles = _midway_titles_with_discdb_pick()
    decision = evaluate_path_a_trigger(titles, disc_size_bytes=None)
    assert decision.auto_picked_exploratory_title_index == 108


def test_auto_pick_falls_back_to_flag_clear_when_no_discdb():
    """No DiscDB classification on any member; the flag-clear one wins."""
    titles = _midway_titles_with_huge_projection()
    for idx in (1, 2, 3, 4, 5, 108):
        titles[idx]["type"] = None
        titles[idx]["obfuscation_flag"] = True
    titles[3]["obfuscation_flag"] = False  # only flag-clear member
    decision = evaluate_path_a_trigger(titles, disc_size_bytes=None)
    assert decision.auto_picked_exploratory_title_index == 3


def test_auto_pick_falls_back_to_first_member_when_all_flagged_no_discdb():
    """All members flagged + no DiscDB; takes the first by index."""
    titles = _midway_titles_with_huge_projection()
    for idx in (1, 2, 3, 4, 5, 108):
        titles[idx]["type"] = None
        titles[idx]["obfuscation_flag"] = True
    decision = evaluate_path_a_trigger(titles, disc_size_bytes=None)
    # detect_duplicate_segment_groups sorts members ascending → first is 1.
    assert decision.auto_picked_exploratory_title_index == 1


def test_auto_pick_none_when_no_groups():
    decision = evaluate_path_a_trigger(_normal_uhd_titles(), disc_size_bytes=None)
    assert decision.auto_picked_exploratory_title_index is None


def test_409_payload_carries_auto_pick():
    titles = _midway_titles_with_discdb_pick()
    decision = evaluate_path_a_trigger(titles, disc_size_bytes=None)
    payload = decision.to_409_payload()
    assert payload["auto_picked_exploratory_title_index"] == 108
    # Candidates still listed (diagnostic display) but UI shouldn't let
    # the user pick between them.
    assert len(payload["candidates"]) > 0
