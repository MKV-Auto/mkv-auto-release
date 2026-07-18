"""Tests for MakeMKV MSG:3307 obfuscation-flag extraction.

Bit 0x01000000 (16777216) of the MSG:3307 flag field marks playlists that
MakeMKV's BD-J emulator considers part of a suspected fake-playlist mass
(playlist obfuscation). On Midway 2019:
- 201 of 213 titles are flagged (the playlist mass)
- 12 titles are unflagged (m2ts extras + a handful of non-mass mpls)

Both the canonical (00539.mpls = title 108) and the trap (00459.mpls = title 89)
are flagged — the bit detects "obfuscation territory" but does NOT identify
the canonical. The mechanism that picks the canonical is DiscDB or Path A's
segment-reorder workflow.
"""
from core.utils import MSG_3307_OBFUSCATION_BIT, parse_log, parse_title_metadata


def _line(flag: int, source_file: str, title_id: int) -> str:
    """Build a MakeMKV-formatted MSG:3307 line for the given flag/file/title."""
    return (
        f'MSG:3307,{flag},2,'
        f'"File {source_file} was added as title #{title_id}",'
        f'"File %1 was added as title #%2",'
        f'"{source_file}","{title_id}"'
    )


def test_msg_3307_constant_is_correct():
    """0x01000000 is the bit MakeMKV sets on obfuscation-mass playlists."""
    assert MSG_3307_OBFUSCATION_BIT == 0x01000000
    assert MSG_3307_OBFUSCATION_BIT == 16777216


def test_parse_log_extracts_obfuscation_flag_from_titles():
    log = "\n".join([
        _line(16777216, "00539.mpls", 108),  # canonical, obfuscated
        _line(16777216, "00459.mpls", 89),   # trap, obfuscated
        _line(0, "00440.mpls", 86),           # legitimate non-mass mpls
        _line(0, "02799.m2ts", 204),          # m2ts extra, never obfuscated
    ])
    titles = parse_log(log)

    assert titles[108]["file"] == "00539.mpls"
    assert titles[108]["flag"] == 16777216
    assert titles[108]["obfuscated"] is True

    assert titles[89]["file"] == "00459.mpls"
    assert titles[89]["obfuscated"] is True

    assert titles[86]["obfuscated"] is False
    assert titles[86]["flag"] == 0

    assert titles[204]["obfuscated"] is False


def test_parse_log_handles_other_flag_bits_without_obfuscation_misfire():
    """MakeMKV may set bits other than 0x01000000; we should NOT treat them as obfuscation."""
    log = _line(0x00000004, "00539.mpls", 108)  # some unrelated flag set
    titles = parse_log(log)
    assert titles[108]["flag"] == 4
    assert titles[108]["obfuscated"] is False


def test_parse_log_legacy_format_omits_flag_field():
    """The pre-2024 MakeMKV log format doesn't carry the source filename in the
    quoted suffix. parse_log's old-format fallback can't recover the flag — that's
    acceptable; obfuscated discs in production are running modern MakeMKV (1.18+)."""
    legacy = 'MSG:3307,0,2,"File 00539.mpls was added as title #108"'
    titles = parse_log(legacy)
    assert titles[108]["file"] == "00539.mpls"
    assert "flag" not in titles[108]
    assert "obfuscated" not in titles[108]


def test_parse_title_metadata_propagates_obfuscation_flag():
    """The title-metadata parser used by scan-track persistence also surfaces the flag."""
    log = "\n".join([
        _line(16777216, "00539.mpls", 108),
        _line(0, "02799.m2ts", 204),
    ])
    tracks = parse_title_metadata(log)
    by_index = {t["index"]: t for t in tracks}
    assert by_index[108]["obfuscation_flag"] is True
    assert by_index[108]["flag"] == 16777216
    assert by_index[204]["obfuscation_flag"] is False
    assert by_index[204]["flag"] == 0
