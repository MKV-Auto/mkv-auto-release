"""Tests for the tier-aware ``obfuscation_reason`` column on disc_titles.

The reason is set at scan time from MakeMKV's MSG:3307 hint
(`'makemkv_msg3307'`) and is later overridden by Path B dedupe
(`'segment_set_sibling'`) and Path A canonical/skip logic
(`'path_a_decoy'` / NULL). This file pins the scan-time write — Phase 1
of the obfuscation tiering plan. Per-Phase coverage for Path A / Path B
overrides lives in `test_path_a_workflow_step.py` and
`test_path_b_dedupe.py` respectively.
"""
from __future__ import annotations

from types import SimpleNamespace

from api import crud


def _fake_disc():
    return SimpleNamespace(id="disc-1", titles=[], title_streams=[])


def test_scan_time_write_sets_reason_when_makemkv_flag_set():
    disc = _fake_disc()
    track = {
        "source_file": "00073.mpls",
        "index": 10,
        "duration": 8304,
        "segment_map": "504,506,507,502,501,509,510,503,505,508",
        "obfuscation_flag": True,
    }

    title = crud._append_disc_title_from_scan_track(disc, track, order_index=10)

    assert title is not None
    assert title.obfuscation_flag is True
    assert title.obfuscation_reason == "makemkv_msg3307"


def test_scan_time_write_leaves_reason_null_when_makemkv_flag_clear():
    disc = _fake_disc()
    track = {
        "source_file": "00006.m2ts",
        "index": 205,
        "duration": 50,
        "obfuscation_flag": False,
    }

    title = crud._append_disc_title_from_scan_track(disc, track, order_index=205)

    assert title is not None
    assert title.obfuscation_flag is False
    assert title.obfuscation_reason is None


def test_scan_time_write_treats_missing_flag_as_false():
    """Defensive: when the scan payload omits obfuscation_flag entirely
    (rare on real MakeMKV output but possible on legacy fixtures), the
    new column stays NULL rather than getting a default reason."""
    disc = _fake_disc()
    track = {
        "source_file": "00009.m2ts",
        "index": 208,
        "duration": 128,
        # no obfuscation_flag key
    }

    title = crud._append_disc_title_from_scan_track(disc, track, order_index=208)

    assert title is not None
    assert title.obfuscation_flag is False
    assert title.obfuscation_reason is None
