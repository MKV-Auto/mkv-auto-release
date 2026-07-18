"""
Tests for Disc Manager module.
Tests parsing, DiscDB queries, and formatting without database access.
"""
import pytest
from unittest.mock import Mock, patch, MagicMock

from core.disc_manager import (
    parse_info_log,
    query_discdb,
    get_disc_info,
    refresh_disc_info,
    get_disc_hash,
    list_discs,
    register_db_discdb_lookup,
    DriveManagerError,
)
# DriveManagerError is now defined in disc_manager module


@pytest.fixture
def mock_drive_manager_client(monkeypatch):
    """Mock drive manager client."""
    mock_fetch = Mock()
    mock_refresh = Mock()
    
    def mock_list_drives():
        return [
            {"disc_num": "1", "mount_point": "/mnt/sr1"},
            {"disc_num": "2", "mount_point": "/mnt/sr2"},
        ]
    
    def mock_fetch_disc_info(disc_num, mount_point, timeout=60.0, refresh=False):
        mock_fetch(disc_num, mount_point, timeout, refresh)
        return {
            "disc_num": disc_num,
            "mount_point": mount_point,
            "disc_hash": "TESTHASH123",
            "content_hash": "TESTHASH123",
            "info_log": "TINFO:0,0,0,\"Test Title\"\nSINFO:0,0,19,0,\"1920x1080\"",
            "raw_info_log": "TINFO:0,0,0,\"Test Title\"\nSINFO:0,0,19,0,\"1920x1080\"",
        }
    
    def mock_refresh_disc_info(disc_num, mount_point, timeout=60.0):
        mock_refresh(disc_num, mount_point, timeout)
        return mock_fetch_disc_info(disc_num, mount_point, timeout, refresh=True)
    
    # Mock drive_operations functions (now used by disc_manager)
    monkeypatch.setattr("core.disc_manager._list_drives", mock_list_drives)
    monkeypatch.setattr("core.disc_manager._get_disc_info", mock_fetch_disc_info)
    monkeypatch.setattr("core.disc_manager._refresh_disc_info", mock_refresh_disc_info)
    
    return {"fetch_disc_info": mock_fetch, "refresh_disc_info": mock_refresh}


@pytest.fixture
def mock_disc_cache(monkeypatch):
    """Mock disc cache.
    
    Simulates the mount_point-keyed cache: set_payload stores under
    mount_point (from payload), disc_num, and disc_hash.
    """
    cache = {}
    
    def mock_get(key):
        return cache.get(key)
    
    def mock_set_payload(primary_key, payload):
        # Mirror the real set_payload: mount_point is primary, disc_num/hash are aliases
        mount_point = (payload.get("mount_point") or "").strip()
        if mount_point:
            cache[mount_point] = payload
        cache[primary_key] = payload
        disc_num = payload.get("disc_num")
        if disc_num and str(disc_num) != primary_key and str(disc_num) != mount_point:
            cache[str(disc_num)] = payload
        if payload.get("disc_hash"):
            cache[payload["disc_hash"]] = payload
    
    monkeypatch.setattr("core.disc_manager.cache_get", mock_get)
    monkeypatch.setattr("core.disc_manager.cache_set", mock_set_payload)
    
    return cache


@pytest.fixture
def mock_discdb(monkeypatch):
    """Mock DiscDB queries."""
    def mock_retrieve_discdb_data(content_hash):
        if content_hash == "TESTHASH123":
            return {
                "mediaItems": {
                    "nodes": [{
                        "id": "movie-123",
                        "title": "Test Movie",
                        "year": 2020,
                        "releases": [{
                            "slug": "test-movie",
                            "discs": [{
                                "contentHash": "TESTHASH123",
                                "format": "Blu-Ray",
                                "titles": []
                            }]
                        }]
                    }]
                }
            }
        raise Exception("DiscDB: no match found")
    
    def mock_parse_discdb_data(raw, target_hash=None):
        if "mediaItems" in raw and raw["mediaItems"]["nodes"]:
            node = raw["mediaItems"]["nodes"][0]
            release = node["releases"][0]
            disc = release["discs"][0]
            return (
                node["title"],  # movie_name
                None,  # release_image
                release["slug"],  # disc_slug
                {},  # db_mapping
                "1080p",  # resolution
                disc["format"],  # disc_format
                "movie",  # title_type
                release["slug"],  # disc_group
                2020,  # release_year
                None,  # release_date
                2020,  # original_year
                None,  # original_release_date
                [],  # release_discs
                None,  # tmdb_id
                "1080p",  # release_resolution
                None,  # tmdb_type
                2020,  # production_year
                1,  # matched_disc_index (disc number from DiscDB)
                None,  # discdb_boxset
            )
        raise Exception("DiscDB: no match found")
    
    monkeypatch.setattr("core.disc_manager.retrieve_discdb_data", mock_retrieve_discdb_data)
    monkeypatch.setattr("core.disc_manager.parse_discdb_data", mock_parse_discdb_data)
    
    return mock_retrieve_discdb_data, mock_parse_discdb_data


@pytest.fixture
def mock_disc_locks(monkeypatch):
    """Mock disc locks. get_disc_info uses get_active_operations(disc_num); set _active_list to e.g. ["rip"] to simulate drive busy."""
    active_operations = {}

    def mock_is_operation_active(disc_num, operation_type):
        return active_operations.get((disc_num, operation_type), False)

    def mock_get_active_operations(disc_num):
        # Tests set active_operations["_active_list"] = ["rip"] (or ["info"]) to simulate drive busy
        forced = active_operations.get("_active_list")
        if forced:
            return list(forced)
        return [op for op in ["hash", "info", "rip"] if active_operations.get((disc_num, op), False)]

    monkeypatch.setattr("core.disc_manager.is_operation_active", mock_is_operation_active)
    monkeypatch.setattr("core.disc_manager.get_active_operations", mock_get_active_operations)

    return active_operations


class TestParseInfoLog:
    """Tests for parse_info_log function."""
    
    def test_parse_info_log_with_valid_log(self):
        """Test parsing valid makemkv info log."""
        info_log = """TINFO:0,0,0,"Test Title"
SINFO:0,0,19,0,"1920x1080"
MSG:3307,0,2,"File 00001.mpls was added as title #1"
"""
        result = parse_info_log(info_log)
        
        assert "titles" in result
        assert "resolution" in result
        assert "disc_format" in result
        assert result["resolution"] == "1080p"
        assert result["disc_format"] == "Blu-Ray"
    
    def test_parse_info_log_with_list(self):
        """Test parsing info log as list of lines."""
        info_log = [
            "TINFO:0,0,0,\"Test Title\"",
            "SINFO:0,0,19,0,\"3840x2160\"",
        ]
        result = parse_info_log(info_log)
        
        assert result["resolution"] == "2160p"
        assert result["disc_format"] == "UHD"
    
    def test_parse_info_log_with_empty(self):
        """Test parsing empty info log."""
        result = parse_info_log(None)
        assert result == {}
        
        result = parse_info_log("")
        assert result == {}
    
    def test_parse_info_log_with_invalid(self):
        """Test parsing invalid info log."""
        result = parse_info_log("invalid log content")
        # Should not raise, but may return empty dict
        assert isinstance(result, dict)


class TestQueryDiscDB:
    """Tests for query_discdb function."""
    
    def test_query_discdb_hit(self, mock_discdb, monkeypatch):
        """Test DiscDB query with hit."""
        # query_discdb short-circuits in dev mode; force non-dev so hit path runs
        monkeypatch.setattr("core.disc_manager.is_dev_mode", lambda: False)
        result = query_discdb("TESTHASH123")
        
        assert result["discdb_hit"] is True
        assert result["movie_name"] == "Test Movie"
        assert result["label_required"] is False
        assert result["label_ready"] is True
        assert result["disc_format"] == "Blu-Ray"
        assert result["resolution"] == "1080p"
        assert result["raw_db_query"]["mediaItems"]["nodes"][0]["title"] == "Test Movie"
    
    def test_query_discdb_miss(self, mock_discdb):
        """Test DiscDB query with miss."""
        result = query_discdb("MISSINGHASH")
        
        assert result["discdb_hit"] is False
        assert result["label_required"] is True
        assert result["label_ready"] is False
        assert "error" in result
    
    def test_query_discdb_dev_mode(self, monkeypatch):
        """Test DiscDB query in dev mode."""
        monkeypatch.setenv("MKVAUTO_DEV_MODE", "1")
        monkeypatch.setattr("core.disc_manager.is_dev_mode", lambda: True)
        
        result = query_discdb("TESTHASH123")
        
        assert result["discdb_hit"] is False
        assert result["label_required"] is True

    def test_query_discdb_prefill_miss_workflow_on_hit(self, mock_discdb, monkeypatch):
        """When discdb_miss_workflow_with_prefill is on, API still runs and hit forces label_required."""
        monkeypatch.setattr("core.disc_manager.is_dev_mode", lambda: False)
        monkeypatch.setattr(
            "core.disc_manager.app_settings.get_discdb_miss_workflow_with_prefill", lambda: True
        )
        result = query_discdb("TESTHASH123")
        assert result["discdb_hit"] is True
        assert result["label_required"] is True
        assert result["label_ready"] is False
        assert result["movie_name"] == "Test Movie"


class TestQueryDiscDBCache:
    """Tests for DB-backed DiscDB lookup cache (#77)."""

    def test_uses_db_cache_on_hit(self, monkeypatch):
        """When DB callback returns data, query_discdb uses it and skips the API."""
        db_data = {
            "discdb_hit": True,
            "label_required": False,
            "label_ready": True,
            "movie_name": "Cached Movie",
            "disc_format": "Blu-Ray",
        }
        monkeypatch.setattr("core.disc_manager._db_discdb_lookup", lambda h: db_data)
        # Ensure the API function would fail if called (proves we skipped it)
        monkeypatch.setattr(
            "core.disc_manager.retrieve_discdb_data",
            lambda h: (_ for _ in ()).throw(AssertionError("API should not be called")),
        )

        result = query_discdb("SOMEHASH")

        assert result["discdb_hit"] is True
        assert result["movie_name"] == "Cached Movie"

    def test_repeated_lookups_same_hash_hit_cache_skip_api(self, monkeypatch):
        """Repeated query_discdb calls for the same hash use the DB cache each time
        and never fall through to the TheDiscDB API. The cache is consulted on every
        call (it's the DB session itself, not a process-local memoization), but the
        API call count must remain zero."""
        db_calls: list[str] = []
        api_calls: list[str] = []

        def fake_db_lookup(h):
            db_calls.append(h)
            return {
                "discdb_hit": True,
                "label_required": False,
                "label_ready": True,
                "movie_name": "Cached Movie",
                "disc_format": "Blu-Ray",
            }

        def fake_api(h):
            api_calls.append(h)
            raise AssertionError("API should not be called on a DB cache hit")

        monkeypatch.setattr("core.disc_manager._db_discdb_lookup", fake_db_lookup)
        monkeypatch.setattr("core.disc_manager.retrieve_discdb_data", fake_api)

        for _ in range(3):
            result = query_discdb("HASH-AAA")
            assert result["discdb_hit"] is True
            assert result["movie_name"] == "Cached Movie"

        assert db_calls == ["HASH-AAA", "HASH-AAA", "HASH-AAA"]
        assert api_calls == []

    def test_distinct_hashes_routed_independently_through_cache(self, mock_discdb, monkeypatch):
        """Cache lookups are keyed per hash: a hit for hash A does not satisfy
        hash B. Hash B falls through to the API independently."""
        monkeypatch.setattr("core.disc_manager.is_dev_mode", lambda: False)

        db_calls: list[str] = []
        cached_data = {
            "discdb_hit": True,
            "label_required": False,
            "label_ready": True,
            "movie_name": "Cached Movie A",
            "disc_format": "Blu-Ray",
        }

        def fake_db_lookup(h):
            db_calls.append(h)
            return cached_data if h == "HASH-A" else None

        api_calls: list[str] = []
        real_retrieve = __import__(
            "core.disc_manager", fromlist=["retrieve_discdb_data"]
        ).retrieve_discdb_data

        def tracking_api(h):
            api_calls.append(h)
            return real_retrieve(h)

        monkeypatch.setattr("core.disc_manager._db_discdb_lookup", fake_db_lookup)
        monkeypatch.setattr("core.disc_manager.retrieve_discdb_data", tracking_api)

        result_a = query_discdb("HASH-A")
        result_b = query_discdb("TESTHASH123")

        # Hash A: served from DB cache, API never called for it
        assert result_a["movie_name"] == "Cached Movie A"
        assert "HASH-A" not in api_calls
        # Hash B: DB cache miss, API consulted exactly once for that hash
        assert result_b["discdb_hit"] is True
        assert result_b["movie_name"] == "Test Movie"
        assert api_calls == ["TESTHASH123"]
        # Both hashes were independently routed through the cache
        assert db_calls == ["HASH-A", "TESTHASH123"]


class TestDiscDBEnrichmentMerge:
    """Tests for DiscDB enrichment of scan titles in get_disc_info."""

    def test_enrichment_preserves_structure_and_applies_metadata(self, monkeypatch, mock_disc_locks):
        """DiscDB enrichment should not change structural fields but should overlay metadata fields."""
        # Force no active operations
        mock_disc_locks.clear()

        # Mock raw info with titles from scan
        raw_info = {
            "info_log": "dummy",
            "titles": [],
            "disc_hash": "testhashfordiscdb",
        }

        def mock_get_disc_info(disc_num, mount_point, refresh=False):
            return raw_info

        # Parsed info with one title
        parsed_title = {
            "index": 1,
            "source_file": "00001.m2ts",
            "segment_map": "1,2,3",
            "duration": 3600,
            "size": 1_000_000_000,
            "comment": "Keep this from MakeMKV",
            "tracks": [{"index": 0, "type": "video", "language": "eng"}],
        }

        def mock_parse_info_log(info_log):
            return {
                "titles": {},
                "scan_tracks": [parsed_title],
                "resolution": "1080p",
                "disc_format": "Blu-Ray",
            }

        # DiscDB mapping keyed by sourceFile, with metadata fields
        discdb_tracks = {
            "00001.m2ts": {
                "type": "Episode",
                "season": 2,
                "episode": 5,
                "title": "DiscDB Episode Title",
                "description": "From DiscDB",
            }
        }

        discdb_payload = {
            "discdb_hit": True,
            "tracks": discdb_tracks,
        }

        monkeypatch.setattr("core.disc_manager._get_disc_info", mock_get_disc_info)
        monkeypatch.setattr("core.disc_manager._refresh_disc_info", mock_get_disc_info)
        monkeypatch.setattr("core.disc_manager.parse_info_log", mock_parse_info_log)
        monkeypatch.setattr("core.disc_manager.query_discdb", lambda h: discdb_payload)

        result = get_disc_info("0", "/dev/sr0")

        assert result["discdb_hit"] is True
        assert "titles" in result
        assert len(result["titles"]) == 1
        enriched = result["titles"][0]
        # Structural fields unchanged
        assert enriched["index"] == parsed_title["index"]
        assert enriched["source_file"] == parsed_title["source_file"]
        assert enriched["segment_map"] == parsed_title["segment_map"]
        assert enriched["duration"] == parsed_title["duration"]
        assert enriched["size"] == parsed_title["size"]
        assert enriched["tracks"] == parsed_title["tracks"]
        # Metadata fields applied from DiscDB
        assert enriched["type"] == "Episode"
        assert enriched["season"] == 2
        assert enriched["episode"] == 5
        assert enriched["title"] == "DiscDB Episode Title"
        assert enriched["description"] == "From DiscDB"
        assert enriched["comment"] == "Keep this from MakeMKV"
        assert len(result.get("scan_tracks") or []) == 1
        assert result["scan_tracks"][0]["type"] == "Episode"
        assert result["scan_tracks"][0]["comment"] == "Keep this from MakeMKV"

    def test_db_cache_prefill_miss_workflow(self, monkeypatch):
        """DB cache hit path applies prefill+miss-workflow override like API hits."""
        def lookup(_h):
            return {
                "discdb_hit": True,
                "label_required": False,
                "label_ready": True,
                "movie_name": "Cached Movie",
                "disc_format": "Blu-Ray",
            }

        monkeypatch.setattr("core.disc_manager._db_discdb_lookup", lookup)
        monkeypatch.setattr(
            "core.disc_manager.retrieve_discdb_data",
            lambda h: (_ for _ in ()).throw(AssertionError("API should not be called")),
        )
        monkeypatch.setattr(
            "core.disc_manager.app_settings.get_discdb_miss_workflow_with_prefill", lambda: True
        )

        result = query_discdb("SOMEHASH")

        assert result["discdb_hit"] is True
        assert result["label_required"] is True
        assert result["label_ready"] is False
        assert result["movie_name"] == "Cached Movie"

    def test_falls_through_on_db_miss(self, mock_discdb, monkeypatch):
        """When DB callback returns None (miss), query_discdb falls through to API."""
        monkeypatch.setattr("core.disc_manager._db_discdb_lookup", lambda h: None)
        monkeypatch.setattr("core.disc_manager.is_dev_mode", lambda: False)

        result = query_discdb("TESTHASH123")

        # Should hit the API and get a real hit from mock_discdb
        assert result["discdb_hit"] is True
        assert result["movie_name"] == "Test Movie"
        assert result["raw_db_query"]["mediaItems"]["nodes"][0]["title"] == "Test Movie"

    def test_falls_through_on_db_error(self, mock_discdb, monkeypatch):
        """When DB callback raises, query_discdb falls through to API gracefully."""
        def _broken_lookup(h):
            raise RuntimeError("DB connection failed")

        monkeypatch.setattr("core.disc_manager._db_discdb_lookup", _broken_lookup)
        monkeypatch.setattr("core.disc_manager.is_dev_mode", lambda: False)

        result = query_discdb("TESTHASH123")

        # Should still work via API fallback
        assert result["discdb_hit"] is True
        assert result["movie_name"] == "Test Movie"
        assert "raw_db_query" in result

    def test_no_callback_registered(self, mock_discdb, monkeypatch):
        """When no DB callback is registered, query_discdb works normally via API."""
        monkeypatch.setattr("core.disc_manager._db_discdb_lookup", None)
        monkeypatch.setattr("core.disc_manager.is_dev_mode", lambda: False)

        result = query_discdb("TESTHASH123")

        assert result["discdb_hit"] is True
        assert result["movie_name"] == "Test Movie"
        assert result["raw_db_query"]["mediaItems"]["nodes"][0]["title"] == "Test Movie"

    def test_register_db_discdb_lookup(self, monkeypatch):
        """register_db_discdb_lookup sets the callback."""
        monkeypatch.setattr("core.disc_manager._db_discdb_lookup", None)
        dummy = lambda h: None
        register_db_discdb_lookup(dummy)
        import core.disc_manager as dm
        assert dm._db_discdb_lookup is dummy
        # Cleanup
        monkeypatch.setattr("core.disc_manager._db_discdb_lookup", None)


class TestGetDiscInfo:
    """Tests for get_disc_info function."""
    
    def test_get_disc_info_from_cache(self, mock_drive_manager_client, mock_disc_cache, mock_discdb, mock_disc_locks):
        """Test getting disc info from cache."""
        # Pre-populate cache by mount_point (primary key)
        cached_info = {
            "disc_num": "1",
            "mount_point": "/mnt/sr1",
            "disc_hash": "TESTHASH123",
            "movie_name": "Cached Movie",
        }
        mock_disc_cache["/mnt/sr1"] = cached_info
        mock_disc_cache["1"] = cached_info
        
        result = get_disc_info("1", "/mnt/sr1", refresh=False)
        
        assert result["disc_num"] == "1"
        assert result["mount_point"] == "/mnt/sr1"
        assert result["movie_name"] == "Cached Movie"
        # Should not call drive manager
        mock_drive_manager_client["fetch_disc_info"].assert_not_called()
    
    def test_get_disc_info_from_drive_manager(self, mock_drive_manager_client, mock_disc_cache, mock_discdb, mock_disc_locks):
        """Test getting disc info from Drive Manager."""
        result = get_disc_info("1", "/mnt/sr1", refresh=False)
        
        assert result["disc_num"] == "1"
        assert result["mount_point"] == "/mnt/sr1"
        assert result["disc_hash"] == "TESTHASH123"
        assert "info_log" in result
        # Should be cached by mount_point (primary key)
        assert "/mnt/sr1" in mock_disc_cache
    
    def test_get_disc_info_with_discdb_hit(self, mock_drive_manager_client, mock_disc_cache, mock_discdb, mock_disc_locks, monkeypatch):
        """Test getting disc info with DiscDB hit."""
        # query_discdb short-circuits in dev mode; force non-dev so hit path runs
        monkeypatch.setattr("core.disc_manager.is_dev_mode", lambda: False)
        result = get_disc_info("1", "/mnt/sr1", refresh=False)
        
        assert result["discdb_hit"] is True
        assert result["movie_name"] == "Test Movie"
        assert result["label_required"] is False
    
    def test_get_disc_info_with_discdb_miss(self, mock_drive_manager_client, mock_disc_cache, mock_discdb, mock_disc_locks):
        """Test getting disc info with DiscDB miss."""
        # Override mock to return different hash
        def mock_fetch_different_hash(disc_num, mount_point, timeout=60.0, refresh=False):
            return {
                "disc_num": disc_num,
                "mount_point": mount_point,
                "disc_hash": "MISSINGHASH",
                "content_hash": "MISSINGHASH",
                "info_log": "TINFO:0,0,0,\"Test Title\"",
            }
        
        with patch("core.disc_manager._get_disc_info", mock_fetch_different_hash):
            result = get_disc_info("1", "/mnt/sr1", refresh=False)
            
            assert result["discdb_hit"] is False
            assert result["label_required"] is True
            assert result["label_ready"] is False
    
    def test_get_disc_info_with_active_rip(self, mock_drive_manager_client, mock_disc_cache, mock_discdb, mock_disc_locks):
        """Test getting disc info when rip is active."""
        # Mark rip as active
        mock_disc_locks[("1", "rip")] = True
        
        # Pre-populate cache by mount_point (primary key)
        cached_info = {
            "disc_num": "1",
            "mount_point": "/mnt/sr1",
            "disc_hash": "TESTHASH123",
        }
        mock_disc_cache["/mnt/sr1"] = cached_info
        mock_disc_cache["1"] = cached_info
        
        result = get_disc_info("1", "/mnt/sr1", refresh=False)
        
        # Should return cached data
        assert result["disc_num"] == "1"
    
    def test_get_disc_info_with_active_rip_no_cache(self, mock_drive_manager_client, mock_disc_cache, mock_discdb, mock_disc_locks):
        """Test getting disc info when rip is active but no cache."""
        # get_disc_info uses get_active_operations(); simulate any operation active
        mock_disc_locks["_active_list"] = ["rip"]

        # No cache available
        with pytest.raises(DriveManagerError) as exc_info:
            get_disc_info("1", "/mnt/sr1", refresh=False)

        assert exc_info.value.status_code == 409
        assert "Drive busy" in str(exc_info.value) or "operation" in str(exc_info.value).lower()


class TestRefreshDiscInfo:
    """Tests for refresh_disc_info function."""
    
    def test_refresh_disc_info_success(self, mock_drive_manager_client, mock_disc_cache, mock_discdb, mock_disc_locks):
        """Test refreshing disc info successfully."""
        result = refresh_disc_info("1", "/mnt/sr1")
        
        assert result["disc_num"] == "1"
        assert result["disc_hash"] == "TESTHASH123"
        # Should call refresh
        mock_drive_manager_client["refresh_disc_info"].assert_called()
    
    def test_refresh_disc_info_with_active_operation(self, mock_drive_manager_client, mock_disc_cache, mock_discdb, mock_disc_locks):
        """Test refreshing disc info when operation is active."""
        # refresh_disc_info uses get_active_operations()
        mock_disc_locks["_active_list"] = ["info"]

        with pytest.raises(DriveManagerError) as exc_info:
            refresh_disc_info("1", "/mnt/sr1")

        assert exc_info.value.status_code == 409
        assert "operations active" in str(exc_info.value)


class TestGetDiscHash:
    """Tests for get_disc_hash function."""
    
    def test_get_disc_hash_from_cache(self, mock_drive_manager_client, mock_disc_cache):
        """Test getting disc hash from cache."""
        cached_info = {
            "disc_num": "1",
            "mount_point": "/mnt/sr1",
            "disc_hash": "CACHEDHASH",
        }
        mock_disc_cache["/mnt/sr1"] = cached_info
        mock_disc_cache["1"] = cached_info
        
        result = get_disc_hash("1", "/mnt/sr1")
        
        assert result == "CACHEDHASH"
    
    def test_get_disc_hash_from_drive_manager(self, mock_drive_manager_client, mock_disc_cache):
        """Test getting disc hash from Drive Manager."""
        result = get_disc_hash("1", "/mnt/sr1")
        
        assert result == "TESTHASH123"
    
    def test_get_disc_hash_not_found(self, mock_drive_manager_client, mock_disc_cache):
        """Test getting disc hash when not found."""
        def mock_fetch_no_hash(disc_num, mount_point, timeout=60.0, refresh=False):
            return {
                "disc_num": disc_num,
                "mount_point": mount_point,
            }
        
        with patch("core.disc_manager._get_disc_info", mock_fetch_no_hash):
            with pytest.raises(DriveManagerError) as exc_info:
                get_disc_hash("1", "/mnt/sr1")
            
            assert exc_info.value.status_code == 404


class TestListDiscs:
    """Tests for list_discs function."""
    
    def test_list_discs_with_cached_discs(self, mock_drive_manager_client, mock_disc_cache):
        """Test listing discs with cached info."""
        # Pre-populate cache by mount_point (primary key)
        cached_info = {
            "disc_num": "1",
            "mount_point": "/mnt/sr1",
            "disc_hash": "TESTHASH123",
            "movie_name": "Test Movie",
        }
        mock_disc_cache["/mnt/sr1"] = cached_info
        mock_disc_cache["1"] = cached_info
        
        result = list_discs()
        
        assert len(result) == 2
        # First disc should have cached info
        disc1 = next(d for d in result if d["disc_num"] == "1")
        assert disc1["movie_name"] == "Test Movie"
        # Second disc should be pending
        disc2 = next(d for d in result if d["disc_num"] == "2")
        assert disc2["pending"] is True
    
    def test_list_discs_no_drives(self, mock_drive_manager_client, mock_disc_cache):
        """Test listing discs when no drives."""
        def mock_list_no_drives():
            return []
        
        with patch("core.disc_manager._list_drives", mock_list_no_drives):
            result = list_discs()
            assert result == []
    
    def test_list_discs_handles_exception(self, mock_drive_manager_client, mock_disc_cache):
        """Test listing discs handles exceptions."""
        def mock_list_drives_raises():
            raise Exception("Drive scan failed")
        
        with patch("core.disc_manager._list_drives", mock_list_drives_raises):
            with pytest.raises(Exception, match="Drive scan failed"):
                list_discs()

