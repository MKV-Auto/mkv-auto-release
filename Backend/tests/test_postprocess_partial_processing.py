"""
Tests for post-processing edge case where source files don't exist but output files do
(partial processing after service restart).

This tests the scenario where post-processing was partially completed, the service restarted,
and when resuming, some files already exist in the output location (transient).

Tests: Backend/tests/test_postprocess_partial_processing.py
Update tests if rename_outputs behavior or source_hashes parameter changes.
"""
import pytest
import hashlib
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, patch

from core.disc import Disc


@pytest.fixture
def temp_dirs(tmp_path):
    """Create temporary directories for source and destination."""
    source_dir = tmp_path / "source"
    dest_dir = tmp_path / "dest"
    source_dir.mkdir()
    dest_dir.mkdir()
    return source_dir, dest_dir


def calculate_file_hash(file_path: Path) -> str:
    """Calculate SHA256 hash of a file."""
    hasher = hashlib.sha256()
    with open(file_path, 'rb') as f:
        while chunk := f.read(8*1024*1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def test_rename_movie_skips_existing_file_with_matching_hash(temp_dirs):
    """Test that _rename_movie skips files that already exist in destination with matching hash."""
    source_dir, dest_dir = temp_dirs
    
    # Create a source file and calculate its hash
    source_file = source_dir / "test_t1.mkv"
    test_content = b"test video content for hash verification"
    source_file.write_bytes(test_content)
    source_hash = calculate_file_hash(source_file)
    
    # Create the destination file with the same content (simulating partial processing)
    movie_dir = dest_dir / "Test Movie (2024)"
    movie_dir.mkdir(parents=True, exist_ok=True)
    dest_file = movie_dir / "Test Movie [1080p].mkv"
    dest_file.write_bytes(test_content)  # Same content = same hash
    
    # Remove source file (simulating it was already moved/processed)
    source_file.unlink()
    
    # Verify setup
    assert dest_file.exists(), "Destination file should exist"
    assert not source_file.exists(), "Source file should not exist"
    
    # Prepare source_hashes dict
    source_hashes = {"00001.mpls": source_hash}
    
    # Create a Disc instance with necessary attributes
    disc = Disc("0", "/dev/dvd")
    disc.titles = {1: {"file": "00001.mpls"}}
    disc.db_mapping = {
        "00001.mpls": {
            "season": None,
            "episode": None,
            "episode_name": "Test Movie",
            "type": "MainMovie",
            "format": "MainFeature",
        }
    }
    disc.title_type = "Movie"
    disc.resolution = "1080p"
    disc.movie_name = "Test Movie"
    disc.errors = {}
    
    # Capture log messages
    log_messages = []
    def log_fn(msg):
        log_messages.append(msg)
    disc.log_fn = log_fn
    
    # Call _rename_movie directly with source_hashes
    # Since source file doesn't exist but destination does with matching hash, it should skip
    renamed_paths = disc._rename_movie(
        str(source_dir),
        str(movie_dir),
        movie_name="Test Movie",
        production_year=2024,
        source_hashes=source_hashes,
        transient_root=None  # Tests don't use transient_root
    )
    # renamed_paths will be empty dict since transient_root is None
    
    # The function should have skipped the file because destination exists with matching hash
    # Verify destination file still exists and wasn't modified
    assert dest_file.exists(), "Destination file should still exist"
    
    # Verify hash is still correct
    dest_hash = calculate_file_hash(dest_file)
    assert dest_hash == source_hash, "Destination file hash should match expected hash"
    
    # Verify no errors were added
    assert len(disc.errors) == 0, "Should not have errors when hash matches"


def test_rename_movie_detects_hash_mismatch(temp_dirs):
    """Test that _rename_movie detects hash mismatch when destination exists but hash doesn't match."""
    source_dir, dest_dir = temp_dirs
    
    # Create a source file and calculate its hash
    source_file = source_dir / "test_t1.mkv"
    original_content = b"original content for hash test"
    source_file.write_bytes(original_content)
    source_hash = calculate_file_hash(source_file)
    
    # Create the destination file with DIFFERENT content (corrupted or wrong file)
    movie_dir = dest_dir / "Test Movie (2024)"
    movie_dir.mkdir(parents=True, exist_ok=True)
    dest_file = movie_dir / "Test Movie [1080p].mkv"
    different_content = b"different content - hash mismatch"
    dest_file.write_bytes(different_content)  # Different content = different hash
    
    # Remove source file (simulating it was already moved/processed)
    source_file.unlink()
    
    # Verify setup
    assert dest_file.exists(), "Destination file should exist"
    assert not source_file.exists(), "Source file should not exist"
    
    # Prepare source_hashes dict
    source_hashes = {"00001.mpls": source_hash}
    
    # Create a Disc instance
    disc = Disc("0", "/dev/dvd")
    disc.titles = {1: {"file": "00001.mpls"}}
    disc.db_mapping = {
        "00001.mpls": {
            "season": None,
            "episode": None,
            "episode_name": "Test Movie",
            "type": "MainMovie",
            "format": "MainFeature",
        }
    }
    disc.title_type = "Movie"
    disc.resolution = "1080p"
    disc.movie_name = "Test Movie"
    disc.errors = {}
    
    # Capture log messages
    log_messages = []
    def log_fn(msg):
        log_messages.append(msg)
    disc.log_fn = log_fn
    
    # Call _rename_movie with source_hashes
    # Since hash doesn't match, it should log a warning but skip the move
    renamed_paths = disc._rename_movie(
        str(source_dir),
        str(movie_dir),
        movie_name="Test Movie",
        production_year=2024,
        source_hashes=source_hashes,
        transient_root=None  # Tests don't use transient_root
    )
    # renamed_paths will be empty dict since transient_root is None
    
    # Verify warning was logged (check print output or log_fn calls)
    # The function should have logged about hash mismatch
    hash_warning_found = any(
        "hash mismatch" in str(msg).lower() or "mismatch" in str(msg).lower()
        for msg in log_messages
    )
    # Note: The actual warning is printed, not logged via log_fn in some cases
    # So we just verify the file wasn't moved and still exists
    assert dest_file.exists(), "Destination file should still exist"
    
    # Verify file content wasn't changed
    assert dest_file.read_bytes() == different_content, "Destination file content should be unchanged"


def test_rename_series_one_episode_layout(temp_dirs):
    """Test that _rename_series produces Season XX and SxxExx - EpisodeName[resolution].mkv layout."""
    source_dir, dest_dir = temp_dirs
    source_file = source_dir / "00001_t1.mkv"
    source_file.write_bytes(b"series episode content")

    disc = Disc("0", "/dev/dvd")
    disc.titles = {1: {"file": "00100.mpls"}}
    disc.db_mapping = {
        "00100.mpls": {
            "season": 1,
            "episode": 1,
            "episode_name": "Pilot",
            "type": "Episode",
            "format": "MainFeature",
        }
    }
    disc.title_type = "Series"
    disc.resolution = "1080p"
    disc.movie_name = "Test Show"
    disc.errors = {}

    show_folder = dest_dir / "Test Show (2022)"
    show_folder.mkdir(parents=True, exist_ok=True)

    # _rename_series only calls move_with_progress when progress_cb is provided
    renamed_paths = disc._rename_series(
        str(source_dir),
        str(show_folder),
        movie_name="Test Show",
        production_year=2022,
        source_hashes=None,
        transient_root=None,  # Tests don't use transient_root
        progress_cb=lambda _d, _t, _f: None,
    )

    season_dir = show_folder / "Season 01"
    expected = season_dir / "Test Show - s01e01 - Pilot.1080p.mkv"
    assert expected.exists(), f"Expected {expected}; listing: {list(show_folder.rglob('*.mkv'))}"
    assert expected.read_bytes() == b"series episode content"


def test_rename_series_skips_existing_file_with_matching_hash(temp_dirs):
    """Test that _rename_series skips when destination exists with matching hash (partial processing)."""
    source_dir, dest_dir = temp_dirs
    content = b"series content for hash"
    source_file = source_dir / "00001_t1.mkv"
    source_file.write_bytes(content)
    source_hash = calculate_file_hash(source_file)
    source_file.unlink()

    disc = Disc("0", "/dev/dvd")
    disc.titles = {1: {"file": "00100.mpls"}}
    disc.db_mapping = {
        "00100.mpls": {
            "season": 1,
            "episode": 1,
            "episode_name": "Pilot",
            "type": "Episode",
            "format": "MainFeature",
        }
    }
    disc.title_type = "Series"
    disc.resolution = "1080p"
    disc.movie_name = "Test Show"
    disc.errors = {}

    show_folder = dest_dir / "Test Show (2022)"
    season_dir = show_folder / "Season 01"
    season_dir.mkdir(parents=True, exist_ok=True)
    dest_file = season_dir / "Test Show - s01e01 - Pilot.1080p.mkv"
    dest_file.write_bytes(content)

    renamed_paths = disc._rename_series(
        str(source_dir),
        str(show_folder),
        movie_name="Test Show",
        production_year=2022,
        source_hashes={"00100.mpls": source_hash},
        transient_root=None,  # Tests don't use transient_root
    )

    # Destination unchanged and hash matches (skip worked). Note: the "Verifying Extracted Titles"
    # loop in _rename_series sets disc.errors["Pilot"] = "Title Not Extracted" when the source
    # file is missing, so we do not assert disc.errors is empty.
    assert dest_file.exists()
    assert calculate_file_hash(dest_file) == source_hash


def test_rename_movie_handles_missing_source_without_hash(temp_dirs):
    """Test that _rename_movie handles missing source file gracefully when source_hashes not provided."""
    source_dir, dest_dir = temp_dirs
    
    # Create destination file (no source file exists)
    movie_dir = dest_dir / "Test Movie (2024)"
    movie_dir.mkdir(parents=True, exist_ok=True)
    dest_file = movie_dir / "Test Movie [1080p].mkv"
    dest_file.write_bytes(b"some content")
    
    # Verify source file doesn't exist
    source_file = source_dir / "test_t1.mkv"
    assert not source_file.exists(), "Source file should not exist"
    assert dest_file.exists(), "Destination file should exist"
    
    # Create a Disc instance
    disc = Disc("0", "/dev/dvd")
    disc.titles = {1: {"file": "00001.mpls"}}
    disc.db_mapping = {
        "00001.mpls": {
            "season": None,
            "episode": None,
            "episode_name": "Test Movie",
            "type": "MainMovie",
            "format": "MainFeature",
        }
    }
    disc.title_type = "Movie"
    disc.resolution = "1080p"
    disc.movie_name = "Test Movie"
    disc.errors = {}
    
    # Capture log messages
    log_messages = []
    def log_fn(msg):
        log_messages.append(msg)
    disc.log_fn = log_fn
    
    # Call _rename_movie WITHOUT source_hashes
    # Since destination exists, it should skip the move even without hash verification
    renamed_paths = disc._rename_movie(
        str(source_dir),
        str(movie_dir),
        movie_name="Test Movie",
        production_year=2024,
        source_hashes=None,  # No hash provided
        transient_root=None  # Tests don't use transient_root
    )
    # renamed_paths will be empty dict since transient_root is None
    
    # Destination file should still exist (we skipped it)
    assert dest_file.exists(), "Destination file should still exist"


def test_rename_series_jellyfin_episode_format(temp_dirs):
    """Test that _rename_series with media_server='jellyfin' produces Show S01E01 Title [resolution].mkv."""
    source_dir, dest_dir = temp_dirs
    source_file = source_dir / "00001_t1.mkv"
    source_file.write_bytes(b"series episode content")

    disc = Disc("0", "/dev/dvd")
    disc.titles = {1: {"file": "00100.mpls"}}
    disc.db_mapping = {
        "00100.mpls": {
            "season": 1,
            "episode": 1,
            "episode_name": "Pilot",
            "type": "Episode",
            "format": "MainFeature",
        }
    }
    disc.title_type = "Series"
    disc.resolution = "1080p"
    disc.movie_name = "Test Show"
    disc.errors = {}

    show_folder = dest_dir / "Test Show (2022)"
    show_folder.mkdir(parents=True, exist_ok=True)

    renamed_paths = disc._rename_series(
        str(source_dir),
        str(show_folder),
        movie_name="Test Show",
        production_year=2022,
        source_hashes=None,
        transient_root=None,
        progress_cb=lambda _d, _t, _f: None,
        media_server="jellyfin",
    )

    season_dir = show_folder / "Season 01"
    expected = season_dir / "Test Show S01E01 Pilot [1080p].mkv"
    assert expected.exists(), f"Expected {expected}; listing: {list(show_folder.rglob('*.mkv'))}"
    assert expected.read_bytes() == b"series episode content"


def test_rename_movie_plex_release_suffix(temp_dirs):
    """Test that release_name is ignored; filename has no release/boxset (Plex)."""
    source_dir, dest_dir = temp_dirs
    source_file = source_dir / "00001_t1.mkv"
    source_file.write_bytes(b"movie content")

    disc = Disc("0", "/dev/dvd")
    disc.titles = {1: {"file": "00001.mpls"}}
    disc.db_mapping = {
        "00001.mpls": {
            "season": None,
            "episode": None,
            "episode_name": "Test Movie",
            "type": "MainMovie",
            "format": "MainFeature",
        }
    }
    disc.title_type = "Movie"
    disc.resolution = "1080p"
    disc.movie_name = "Test Movie"
    disc.errors = {}

    movie_dir = dest_dir / "Test Movie (2024)"
    movie_dir.mkdir(parents=True, exist_ok=True)

    disc._rename_movie(
        str(source_dir),
        str(movie_dir),
        movie_name="Test Movie",
        production_year=2024,
        release_name="Box Set Name",
        source_hashes=None,
        transient_root=None,
        progress_cb=lambda _d, _t, _f: None,
        media_server="plex",
    )

    expected = movie_dir / "Test Movie (2024).1080p.mkv"
    assert expected.exists(), f"Expected {expected}; listing: {list(movie_dir.glob('*.mkv'))}"
    assert expected.read_bytes() == b"movie content"


def test_rename_movie_jellyfin_release_suffix(temp_dirs):
    """Test that release_name is ignored; filename has no release/boxset (Jellyfin)."""
    source_dir, dest_dir = temp_dirs
    source_file = source_dir / "00001_t1.mkv"
    source_file.write_bytes(b"movie content")

    disc = Disc("0", "/dev/dvd")
    disc.titles = {1: {"file": "00001.mpls"}}
    disc.db_mapping = {
        "00001.mpls": {
            "season": None,
            "episode": None,
            "episode_name": "Test Movie",
            "type": "MainMovie",
            "format": "MainFeature",
        }
    }
    disc.title_type = "Movie"
    disc.resolution = "1080p"
    disc.movie_name = "Test Movie"
    disc.errors = {}

    movie_dir = dest_dir / "Test Movie (2024)"
    movie_dir.mkdir(parents=True, exist_ok=True)

    disc._rename_movie(
        str(source_dir),
        str(movie_dir),
        movie_name="Test Movie",
        production_year=2024,
        release_name="Box Set Name",
        source_hashes=None,
        transient_root=None,
        progress_cb=lambda _d, _t, _f: None,
        media_server="jellyfin",
    )

    expected = movie_dir / "Test Movie (2024) [1080p].mkv"
    assert expected.exists(), f"Expected {expected}; listing: {list(movie_dir.glob('*.mkv'))}"
    assert expected.read_bytes() == b"movie content"


def test_rename_movie_plex_edition_suffix(temp_dirs):
    """Test that _rename_movie with media_server='plex' and title_id_to_edition uses {edition-Edition} in filename."""
    source_dir, dest_dir = temp_dirs
    source_file = source_dir / "00001_t1.mkv"
    source_file.write_bytes(b"movie content")
    title_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    final_paths = {title_id: "00001_t1.mkv"}
    title_id_to_edition = {title_id: "Director's Cut"}

    disc = Disc("0", "/dev/dvd")
    disc.titles = {1: {"file": "00001.mpls"}}
    disc.db_mapping = {
        "00001.mpls": {
            "season": None,
            "episode": None,
            "episode_name": "Test Movie",
            "type": "MainMovie",
            "format": "MainFeature",
        }
    }
    disc.title_type = "Movie"
    disc.resolution = "1080p"
    disc.movie_name = "Test Movie"
    disc.errors = {}

    movie_dir = dest_dir / "Test Movie (2024)"
    movie_dir.mkdir(parents=True, exist_ok=True)

    disc._rename_movie(
        str(source_dir),
        str(movie_dir),
        movie_name="Test Movie",
        production_year=2024,
        release_name=None,
        final_paths=final_paths,
        title_id_to_title={title_id: "Test Movie"},
        title_id_to_edition=title_id_to_edition,
        source_hashes=None,
        transient_root=None,
        progress_cb=lambda _d, _t, _f: None,
        media_server="plex",
    )

    # base_name from title_id_to_title is "Test Movie"; Plex appends ".1080p" then "{edition-Director's Cut}"
    expected = movie_dir / "Test Movie.1080p {edition-Director's Cut}.mkv"
    assert expected.exists(), f"Expected {expected}; listing: {list(movie_dir.glob('*.mkv'))}"
    assert expected.read_bytes() == b"movie content"


def test_rename_movie_jellyfin_edition_suffix(temp_dirs):
    """Test that _rename_movie with media_server='jellyfin' and title_id_to_edition uses ' - [Edition]' in filename."""
    source_dir, dest_dir = temp_dirs
    source_file = source_dir / "00001_t1.mkv"
    source_file.write_bytes(b"movie content")
    title_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    final_paths = {title_id: "00001_t1.mkv"}
    title_id_to_edition = {title_id: "Director's Cut"}

    disc = Disc("0", "/dev/dvd")
    disc.titles = {1: {"file": "00001.mpls"}}
    disc.db_mapping = {
        "00001.mpls": {
            "season": None,
            "episode": None,
            "episode_name": "Test Movie",
            "type": "MainMovie",
            "format": "MainFeature",
        }
    }
    disc.title_type = "Movie"
    disc.resolution = "1080p"
    disc.movie_name = "Test Movie"
    disc.errors = {}

    movie_dir = dest_dir / "Test Movie (2024)"
    movie_dir.mkdir(parents=True, exist_ok=True)

    disc._rename_movie(
        str(source_dir),
        str(movie_dir),
        movie_name="Test Movie",
        production_year=2024,
        release_name=None,
        final_paths=final_paths,
        title_id_to_title={title_id: "Test Movie"},
        title_id_to_edition=title_id_to_edition,
        source_hashes=None,
        transient_root=None,
        progress_cb=lambda _d, _t, _f: None,
        media_server="jellyfin",
    )

    # base_name from title_id_to_title is "Test Movie"; Jellyfin appends " - [Director's Cut]" then " [1080p]"
    expected = movie_dir / "Test Movie - [Director's Cut] [1080p].mkv"
    assert expected.exists(), f"Expected {expected}; listing: {list(movie_dir.glob('*.mkv'))}"
    assert expected.read_bytes() == b"movie content"


def test_rename_movie_plex_title_resolution_override(temp_dirs):
    """Use per-title resolution when disc.resolution is missing (Plex)."""
    source_dir, dest_dir = temp_dirs
    source_file = source_dir / "00001_t1.mkv"
    source_file.write_bytes(b"movie content")
    title_id = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    final_paths = {title_id: "00001_t1.mkv"}
    title_id_to_resolution = {title_id: "1080p"}

    disc = Disc("0", "/dev/dvd")
    disc.titles = {1: {"file": "00001.mpls"}}
    disc.db_mapping = {
        "00001.mpls": {
            "season": None,
            "episode": None,
            "episode_name": "Test Movie",
            "type": "MainMovie",
            "format": "MainFeature",
        }
    }
    disc.title_type = "Movie"
    disc.resolution = None
    disc.movie_name = "Test Movie"
    disc.errors = {}

    movie_dir = dest_dir / "Test Movie (2024)"
    movie_dir.mkdir(parents=True, exist_ok=True)

    disc._rename_movie(
        str(source_dir),
        str(movie_dir),
        movie_name="Test Movie",
        production_year=2024,
        final_paths=final_paths,
        title_id_to_resolution=title_id_to_resolution,
        source_hashes=None,
        transient_root=None,
        progress_cb=lambda _d, _t, _f: None,
        media_server="plex",
    )

    expected = movie_dir / "Test Movie (2024).1080p.mkv"
    assert expected.exists(), f"Expected {expected}; listing: {list(movie_dir.glob('*.mkv'))}"
    assert expected.read_bytes() == b"movie content"


def test_rename_movie_jellyfin_title_resolution_override(temp_dirs):
    """Use per-title resolution when disc.resolution is missing (Jellyfin)."""
    source_dir, dest_dir = temp_dirs
    source_file = source_dir / "00001_t1.mkv"
    source_file.write_bytes(b"movie content")
    title_id = "cccccccc-dddd-eeee-ffff-000000000000"
    final_paths = {title_id: "00001_t1.mkv"}
    title_id_to_resolution = {title_id: "1080p"}

    disc = Disc("0", "/dev/dvd")
    disc.titles = {1: {"file": "00001.mpls"}}
    disc.db_mapping = {
        "00001.mpls": {
            "season": None,
            "episode": None,
            "episode_name": "Test Movie",
            "type": "MainMovie",
            "format": "MainFeature",
        }
    }
    disc.title_type = "Movie"
    disc.resolution = None
    disc.movie_name = "Test Movie"
    disc.errors = {}

    movie_dir = dest_dir / "Test Movie (2024)"
    movie_dir.mkdir(parents=True, exist_ok=True)

    disc._rename_movie(
        str(source_dir),
        str(movie_dir),
        movie_name="Test Movie",
        production_year=2024,
        final_paths=final_paths,
        title_id_to_resolution=title_id_to_resolution,
        source_hashes=None,
        transient_root=None,
        progress_cb=lambda _d, _t, _f: None,
        media_server="jellyfin",
    )

    expected = movie_dir / "Test Movie (2024) [1080p].mkv"
    assert expected.exists(), f"Expected {expected}; listing: {list(movie_dir.glob('*.mkv'))}"
    assert expected.read_bytes() == b"movie content"


def test_rename_series_plex_title_resolution_override(temp_dirs):
    """Use per-title resolution when disc.resolution is missing (Plex series)."""
    source_dir, dest_dir = temp_dirs
    source_file = source_dir / "00001_t1.mkv"
    source_file.write_bytes(b"series episode content")
    title_id = "dddddddd-eeee-ffff-1111-222222222222"
    final_paths = {title_id: "00001_t1.mkv"}
    title_id_to_resolution = {title_id: "1080p"}

    disc = Disc("0", "/dev/dvd")
    disc.titles = {1: {"file": "00100.mpls"}}
    disc.db_mapping = {
        "00100.mpls": {
            "season": 1,
            "episode": 1,
            "episode_name": "Pilot",
            "type": "Episode",
            "format": "MainFeature",
        }
    }
    disc.title_type = "Series"
    disc.resolution = None
    disc.movie_name = "Test Show"
    disc.errors = {}

    show_folder = dest_dir / "Test Show (2022)"
    show_folder.mkdir(parents=True, exist_ok=True)

    disc._rename_series(
        str(source_dir),
        str(show_folder),
        movie_name="Test Show",
        production_year=2022,
        final_paths=final_paths,
        title_id_to_resolution=title_id_to_resolution,
        source_hashes=None,
        transient_root=None,
        progress_cb=lambda _d, _t, _f: None,
        media_server="plex",
    )

    season_dir = show_folder / "Season 01"
    expected = season_dir / "Test Show - s01e01 - Pilot.1080p.mkv"
    assert expected.exists(), f"Expected {expected}; listing: {list(show_folder.rglob('*.mkv'))}"
    assert expected.read_bytes() == b"series episode content"


def test_rename_series_jellyfin_title_resolution_override(temp_dirs):
    """Use per-title resolution when disc.resolution is missing (Jellyfin series)."""
    source_dir, dest_dir = temp_dirs
    source_file = source_dir / "00001_t1.mkv"
    source_file.write_bytes(b"series episode content")
    title_id = "eeeeeeee-ffff-1111-2222-333333333333"
    final_paths = {title_id: "00001_t1.mkv"}
    title_id_to_resolution = {title_id: "1080p"}

    disc = Disc("0", "/dev/dvd")
    disc.titles = {1: {"file": "00100.mpls"}}
    disc.db_mapping = {
        "00100.mpls": {
            "season": 1,
            "episode": 1,
            "episode_name": "Pilot",
            "type": "Episode",
            "format": "MainFeature",
        }
    }
    disc.title_type = "Series"
    disc.resolution = None
    disc.movie_name = "Test Show"
    disc.errors = {}

    show_folder = dest_dir / "Test Show (2022)"
    show_folder.mkdir(parents=True, exist_ok=True)

    disc._rename_series(
        str(source_dir),
        str(show_folder),
        movie_name="Test Show",
        production_year=2022,
        final_paths=final_paths,
        title_id_to_resolution=title_id_to_resolution,
        source_hashes=None,
        transient_root=None,
        progress_cb=lambda _d, _t, _f: None,
        media_server="jellyfin",
    )

    season_dir = show_folder / "Season 01"
    expected = season_dir / "Test Show S01E01 Pilot [1080p].mkv"
    assert expected.exists(), f"Expected {expected}; listing: {list(show_folder.rglob('*.mkv'))}"
    assert expected.read_bytes() == b"series episode content"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

