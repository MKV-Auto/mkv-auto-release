"""Tests for the Blu-ray MPLS PlayItem parser.

Fixtures are copies of two real Midway 2019 (Lions Gate) playlists:
- midway_canonical_00539.mpls: 10 PlayItems, sum 8304.338s, MakeMKV TINFO 9 = 8304s
- midway_trap_00459.mpls: 11 PlayItems (10 canonical + 1 decoy 03113), sum 8364.398s

These two playlists are the heart of the playlist-obfuscation case the
selective-rip workstream is designed to handle. Round-trip parses below
match the empirical measurements exactly.
"""
from pathlib import Path

import pytest

from core.bd_mpls import (
    MplsPlaylist,
    PlayItem,
    parse_mpls_bytes,
    parse_mpls_file,
    parse_playitem_durations,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mpls"
CANONICAL = FIXTURE_DIR / "midway_canonical_00539.mpls"
TRAP = FIXTURE_DIR / "midway_trap_00459.mpls"


def test_parse_canonical_midway_playlist():
    p = parse_mpls_file(CANONICAL)
    assert isinstance(p, MplsPlaylist)
    assert p.version == "0200"
    assert len(p.play_items) == 10
    # MakeMKV TINFO 9 reports 8304s for this playlist; sum should match within sub-second.
    assert abs(p.total_duration_s - 8304.338) < 0.01
    # PlayItem ordering is what segment-reorder matches against.
    assert [pi.clip_name for pi in p.play_items] == [
        "00504", "00510", "00501", "00507", "00502",
        "00505", "00506", "00509", "00503", "00508",
    ]
    # All clips are M2TS codec.
    assert all(pi.codec == "M2TS" for pi in p.play_items)


def test_parse_trap_playlist_has_decoy_segment():
    p = parse_mpls_file(TRAP)
    assert len(p.play_items) == 11
    # Trap is 60s longer than canonical due to the injected segment 03113.
    assert abs(p.total_duration_s - 8364.398) < 0.01
    clips = [pi.clip_name for pi in p.play_items]
    assert "03113" in clips
    # Position of the decoy is what makes the trap detectable by segment-reorder.
    assert clips.index("03113") == 1


def test_durations_s_helper_returns_per_playitem_floats():
    p = parse_mpls_file(CANONICAL)
    durs = p.durations_s
    assert len(durs) == len(p.play_items)
    assert all(isinstance(d, float) for d in durs)
    # First PlayItem (00504) is ~257.7s.
    assert abs(durs[0] - 257.674) < 0.01


def test_parse_mpls_bytes_rejects_non_mpls():
    with pytest.raises(ValueError, match="not an MPLS file"):
        parse_mpls_bytes(b"NOTMPLS\x00" + b"\x00" * 32)


def test_parse_mpls_bytes_rejects_truncated_header():
    with pytest.raises(ValueError):
        parse_mpls_bytes(b"MPLS")  # 4 bytes only, can't parse offsets


def test_parse_playitem_durations_helper_finds_mpls(tmp_path):
    """End-to-end: helper takes a disc-mount-style path and an mpls filename."""
    bdmv = tmp_path / "BDMV" / "PLAYLIST"
    bdmv.mkdir(parents=True)
    (bdmv / "00539.mpls").write_bytes(CANONICAL.read_bytes())

    durs = parse_playitem_durations(tmp_path, "00539.mpls")
    assert durs is not None
    assert len(durs) == 10
    assert abs(sum(durs) - 8304.338) < 0.01


def test_parse_playitem_durations_returns_none_on_missing_file(tmp_path):
    (tmp_path / "BDMV" / "PLAYLIST").mkdir(parents=True)
    assert parse_playitem_durations(tmp_path, "99999.mpls") is None


def test_parse_playitem_durations_returns_none_on_m2ts_request(tmp_path):
    """m2ts files aren't playlists; helper should refuse without trying to read."""
    assert parse_playitem_durations(tmp_path, "00505.m2ts") is None


def test_parse_playitem_durations_returns_none_on_corrupt_file(tmp_path):
    bdmv = tmp_path / "BDMV" / "PLAYLIST"
    bdmv.mkdir(parents=True)
    (bdmv / "bad.mpls").write_bytes(b"GARBAGE" * 100)
    assert parse_playitem_durations(tmp_path, "bad.mpls") is None
