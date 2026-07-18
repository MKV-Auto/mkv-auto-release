from types import SimpleNamespace

from workers.tasks import (
    _build_title_id_maps,
    _ensure_previews_map,
    _resolve_preview_rel_path,
    _resolve_preview_title_id,
)


def test_preview_title_id_mapping():
    """Test preview mapping with title_id keys directly (new format)."""
    title = SimpleNamespace(id="title-1", source_file="00001.mpls", index=1)
    disc = SimpleNamespace(titles=[title])
    job = SimpleNamespace(disc=disc)
    disc_payload = {
        "title_output_map": {"title-1": "movie_t01.mkv"},
        "title_filename_map": {"title-1": "movie_t01.mkv"},
    }
    maps = _build_title_id_maps(job, disc_payload)
    # ripped_files/post_paths now have title_id keys directly
    ripped_files = {"title-1": "movie_t01.mkv"}
    # When track_key is already a title_id, it should resolve directly
    assert _resolve_preview_title_id("title-1", "movie_t01.mkv", maps) == "title-1"
    # Test fallback resolution from source_file (for backward compatibility)
    assert _resolve_preview_title_id("00001.mpls", "movie_t01.mkv", maps) == "title-1"
    assert _resolve_preview_rel_path("title-1", ripped_files, maps) == "movie_t01.mkv"


def test_previews_include_title_id():
    """Test that previews include title_id when using ripped_files/post_paths with title_id keys."""
    title = SimpleNamespace(id="title-1", source_file="00001.mpls", index=1)
    disc = SimpleNamespace(titles=[title])
    job = SimpleNamespace(disc=disc)
    disc_payload = {
        "title_output_map": {"title-1": "movie_t01.mkv"},
        "title_filename_map": {"title-1": "movie_t01.mkv"},
    }
    maps = _build_title_id_maps(job, disc_payload)
    # ripped_files/post_paths now have title_id keys directly
    ripped_files = {"title-1": "movie_t01.mkv"}
    payload = _ensure_previews_map({}, ripped_files, maps)
    assert payload["previews"]["tracks"]["title-1"]["title_id"] == "title-1"
    assert payload["previews"]["tracks"]["title-1"]["source_file"] == "00001.mpls"
    assert payload["previews"]["tracks"]["title-1"]["track_id"] == "00001.mpls"


def test_preview_resolves_source_file_when_unique_row():
    """Single row per source_file: resolve preview key by filename; same for unambiguous MakeMKV index."""
    title = SimpleNamespace(id="id-a", source_file="00928.m2ts", index=302)
    disc = SimpleNamespace(titles=[title])
    job = SimpleNamespace(disc=disc)
    maps = _build_title_id_maps(job, {})
    assert "00928.m2ts" not in maps["ambiguous_source_files"]
    assert _resolve_preview_title_id("00928.m2ts", None, maps) == "id-a"
    assert _resolve_preview_title_id("302", None, maps) == "id-a"


def test_preview_resolves_make_index_when_distinct_titles():
    """Numeric track_key maps via index_to_id only when that index appears on exactly one title."""
    t1 = SimpleNamespace(id="id-1", source_file="001.mpls", index=1)
    t2 = SimpleNamespace(id="id-2", source_file="002.mpls", index=2)
    disc = SimpleNamespace(titles=[t1, t2])
    job = SimpleNamespace(disc=disc)
    maps = _build_title_id_maps(job, {})
    assert _resolve_preview_title_id("1", None, maps) == "id-1"
    assert _resolve_preview_title_id("2", None, maps) == "id-2"


def test_previews_map_sets_queued_status_and_manifest():
    """Test that preview map sets correct status and manifest with title_id keys."""
    disc_payload = {}
    # ripped_files/post_paths now have title_id keys directly
    ripped_files = {"title-99": "movie_t99.mkv"}
    payload = _ensure_previews_map(disc_payload, ripped_files)
    previews = payload["previews"]
    track = previews["tracks"]["title-99"]
    assert previews["status"] == "queued"
    assert track["status"] == "queued"
    assert track["manifest"] == "previews/title-99/preview.m3u8"
    assert track["source"] == "movie_t99.mkv"
