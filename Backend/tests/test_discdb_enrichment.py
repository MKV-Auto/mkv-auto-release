"""Tests for DiscDB metadata merge into MakeMKV scan titles."""
from core.discdb_enrichment import merge_discdb_enrichment_into_titles


def test_merge_applies_discdb_ignore_when_strip_off():
    titles = [{"source_file": "00800.mpls", "index": 0, "comment": "c"}]
    db_tracks = {"00800.mpls": {"type": "ignore", "description": "d"}}
    out = merge_discdb_enrichment_into_titles(
        titles, db_tracks, strip_discdb_ignore_type=False
    )
    assert out[0]["type"] == "ignore"
    assert out[0]["description"] == "d"
    assert out[0]["comment"] == "c"


def test_merge_skips_discdb_ignore_type_when_strip_on():
    titles = [{"source_file": "00800.mpls", "index": 0}]
    db_tracks = {"00800.mpls": {"type": "ignore", "season": 2}}
    out = merge_discdb_enrichment_into_titles(
        titles, db_tracks, strip_discdb_ignore_type=True
    )
    assert "type" not in out[0]
    assert out[0]["season"] == 2


def test_merge_blank_discdb_type_treated_as_ignore_when_strip_on():
    titles = [{"source_file": "00800.mpls", "index": 0}]
    db_tracks = {"00800.mpls": {"type": "", "title": "Bonus"}}
    out = merge_discdb_enrichment_into_titles(
        titles, db_tracks, strip_discdb_ignore_type=True
    )
    assert "type" not in out[0]
    assert out[0]["title"] == "Bonus"


def test_merge_still_applies_mainmovie_when_strip_on():
    titles = [{"source_file": "00800.mpls", "index": 0}]
    db_tracks = {"00800.mpls": {"type": "MainMovie", "episode": 3}}
    out = merge_discdb_enrichment_into_titles(
        titles, db_tracks, strip_discdb_ignore_type=True
    )
    assert out[0]["type"] == "MainMovie"
    assert out[0]["episode"] == 3
