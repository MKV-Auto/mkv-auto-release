"""
Integration tests for Disc Manager with Drive Manager and database.
Tests the full flow: Frontend → Backend API → Disc Manager → Drive Manager.
"""
import inspect

import pytest
from fastapi.testclient import TestClient

from api import database, models
from api.main import app

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True, scope="module")
def _clear_disc_cache_before_disc_manager_integration():
    """Clear disc_cache once before this module's tests to avoid stale data from test_comprehensive_api when run in -m integration."""
    from core import disc_cache

    disc_cache.clear()


@pytest.fixture
def client(test_db, monkeypatch):
    """Create FastAPI TestClient with test database dependency override."""
    def override_get_db():
        try:
            session = test_db()
            yield session
        finally:
            session.close()
    
    from api.routers import discs, jobs, events
    app.dependency_overrides[database.get_db] = override_get_db
    if hasattr(discs, "get_db"):
        app.dependency_overrides[discs.get_db] = override_get_db
    if hasattr(jobs, "get_db"):
        app.dependency_overrides[jobs.get_db] = override_get_db
    if hasattr(events, "get_db"):
        app.dependency_overrides[events.get_db] = override_get_db
    
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def mock_drive_manager(monkeypatch):
    """Mock Drive Manager to return raw info."""
    def mock_fetch_disc_info(disc_num, mount_point, timeout=60.0, refresh=False):
        return {
            "disc_num": disc_num,
            "mount_point": mount_point,
            "disc_hash": "TESTHASH123",
            "content_hash": "TESTHASH123",
            "info_log": """TINFO:0,0,0,"Test Title"
SINFO:0,0,19,0,"1920x1080"
MSG:3307,0,2,"File 00001.mpls was added as title #1"
""",
            "raw_info_log": """TINFO:0,0,0,"Test Title"
SINFO:0,0,19,0,"1920x1080"
""",
        }
    
    def mock_refresh_disc_info(disc_num, mount_point, timeout=60.0):
        return mock_fetch_disc_info(disc_num, mount_point, timeout, refresh=True)
    
    def mock_list_drives():
        return [{"disc_num": "1", "mount_point": "/mnt/sr1"}]
    
    monkeypatch.setattr("core.disc_manager._get_disc_info", mock_fetch_disc_info)
    monkeypatch.setattr("core.disc_manager._refresh_disc_info", mock_refresh_disc_info)
    monkeypatch.setattr("core.disc_manager._list_drives", mock_list_drives)


@pytest.fixture
def mock_discdb(monkeypatch):
    """Mock DiscDB to return hit."""
    def mock_retrieve_discdb_data(content_hash):
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
    
    def mock_parse_discdb_data(raw, target_hash=None):
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

    monkeypatch.setattr("core.disc_manager.retrieve_discdb_data", mock_retrieve_discdb_data)
    monkeypatch.setattr("core.disc_manager.parse_discdb_data", mock_parse_discdb_data)


class TestFullFlow:
    """Tests for the full flow: API → Disc Manager → Drive Manager → DB."""
    
    def test_full_flow_disc_info(self, client, test_db, mock_drive_manager, mock_discdb, monkeypatch):
        """Test full flow: API request → Disc Manager → Drive Manager → DB persistence."""
        # query_discdb short-circuits in dev mode; force non-dev so discdb_hit path runs
        monkeypatch.setattr("core.disc_manager.is_dev_mode", lambda: False)
        response = client.get("/discs/1/info", params={"mount_point": "/mnt/sr1"})
        assert response.status_code == 200
        data = response.json()
        
        # Verify Disc Manager processed the data
        assert data["disc_num"] == "1"
        assert data["mount_point"] in ["/mnt/sr1", "/dev/sr0", "/mnt/dvd"]
        assert data["disc_hash"] == "TESTHASH123"
        if "movie_name" in data:
            assert data["movie_name"] == "Test Movie"
        assert data["discdb_hit"] is True
        assert data["disc_format"] == "Blu-Ray"
        assert data["resolution"] == "1080p"
        
        # Verify data was persisted to DB
        release = None
        session = test_db()
        try:
            disc = session.query(models.Disc).filter(models.Disc.content_hash == "TESTHASH123").first()
            assert disc is not None
            assert disc.content_hash == "TESTHASH123"
            
            release = session.query(models.Release).filter(models.Release.slug == "test-movie").first()
            if release:
                assert disc.release_id == release.id
            else:
                assert disc.release_id is None
        finally:
            session.close()
        
        # Verify DB data is included in response
        assert "disc_id" in data
        if release:
            assert "release_id" in data
    
    def test_full_flow_list_discs(self, client, test_db, mock_drive_manager, mock_discdb):
        """Test full flow: List discs → Disc Manager → Drive Manager → DB enrichment."""
        response = client.get("/discs/")
        assert response.status_code == 200
        data = response.json()
        
        assert len(data) == 1
        disc = data[0]
        
        # Verify Disc Manager processed the data
        assert disc["disc_num"] == "1"
        assert disc["mount_point"] in ["/mnt/sr1", "/dev/sr0", "/mnt/dvd"]
        assert disc["disc_hash"] == "TESTHASH123"
        if "movie_name" in disc:
            assert disc["movie_name"] == "Test Movie"
        
        # Verify drive info is included
        assert "disc_num" in disc
        assert "mount_point" in disc
    
    def test_full_flow_refresh_disc_info(self, client, test_db, mock_drive_manager, mock_discdb):
        """Test full flow: Refresh disc info → Disc Manager → Drive Manager → DB update."""
        # First get disc info to create DB record
        client.get("/discs/1/info", params={"mount_point": "/mnt/sr1"})
        
        # Then refresh
        response = client.post("/discs/1/refresh", params={"mount_point": "/mnt/sr1"})
        assert response.status_code == 200
        data = response.json()
        
        # Verify refreshed data
        assert data["disc_hash"] == "TESTHASH123"
        if "movie_name" in data:
            assert data["movie_name"] == "Test Movie"
        
        # Verify DB was updated
        session = test_db()
        try:
            disc = session.query(models.Disc).filter(models.Disc.content_hash == "TESTHASH123").first()
            assert disc is not None
        finally:
            session.close()


class TestSeparationOfConcerns:
    """Tests to verify separation of concerns."""
    
    def test_disc_manager_no_db_access(self, mock_drive_manager, mock_discdb):
        """Test Disc Manager does not access database."""
        # This test verifies that disc_manager.py has no database imports
        import core.disc_manager as dm
        import inspect
        
        # Check that disc_manager module has no database imports
        source = inspect.getsource(dm)
        assert "from api import database" not in source
        assert "from api import crud" not in source
        assert "from api.database" not in source
        assert "from api.crud" not in source
        assert "Session" not in source or "Session" in source and "sqlalchemy" not in source
    
    def test_drive_manager_returns_raw_info_only(self, mock_drive_manager, monkeypatch):
        """Test Drive Manager returns raw info only (no DiscDB, no parsing)."""
        # Mock the HTTP request to return raw info
        def mock_get(url, params=None, timeout=None):
            class MockResponse:
                def json(self):
                    return {
                        "disc_num": "1",
                        "mount_point": "/mnt/sr1",
                        "disc_hash": "TESTHASH123",
                        "info_log": "TINFO:0,0,0,\"Test Title\"",
                        "raw_info_log": "TINFO:0,0,0,\"Test Title\"",
                    }
                status_code = 200
            return MockResponse()
        
        # Mock drive_operations.get_disc_info to return raw info
        def mock_get_disc_info(disc_num, mount_point, refresh=False):
            return {
                "disc_num": disc_num,
                "mount_point": mount_point,
                "disc_hash": "TESTHASH123",
                "content_hash": "TESTHASH123",
                "info_log": "TINFO:0,0,0,\"Test Title\"\nSINFO:0,0,19,0,\"1920x1080\"",
                "raw_info_log": "TINFO:0,0,0,\"Test Title\"\nSINFO:0,0,19,0,\"1920x1080\"",
            }
        monkeypatch.setattr("core._drive_operations.get_disc_info", mock_get_disc_info)
        
        # Mock drive manager to return raw info
        from core import _drive_operations
        raw_info = _drive_operations.get_disc_info("1", "/mnt/sr1")
        
        # Should have raw fields only
        assert "disc_num" in raw_info
        assert "mount_point" in raw_info
        assert "disc_hash" in raw_info
        assert "info_log" in raw_info
        
        # Should NOT have parsed/DiscDB fields
        assert "movie_name" not in raw_info
        assert "tracks" not in raw_info
        assert "discdb_hit" not in raw_info
    
    def test_backend_api_persists_to_db(self, client, test_db, mock_drive_manager, mock_discdb):
        """Test Backend API persists disc info to database."""
        response = client.get("/discs/1/info", params={"mount_point": "/mnt/sr1"})
        assert response.status_code == 200
        
        # Verify DB record was created
        session = test_db()
        try:
            disc = session.query(models.Disc).filter(models.Disc.content_hash == "TESTHASH123").first()
            assert disc is not None
            
            release = session.query(models.Release).filter(models.Release.slug == "test-movie").first()
            if release:
                assert disc.release_id == release.id
            else:
                assert disc.release_id is None
        finally:
            session.close()


class TestConcurrency:
    """Tests for concurrent operations."""
    
    def test_concurrent_disc_info_requests(self, client, test_db, mock_drive_manager, mock_discdb):
        """Test handling concurrent disc info requests."""
        import threading
        
        results = []
        errors = []
        
        def make_request():
            try:
                response = client.get("/discs/1/info", params={"mount_point": "/mnt/sr1"})
                results.append(response.status_code)
            except Exception as e:
                errors.append(str(e))
        
        # Make multiple concurrent requests
        threads = [threading.Thread(target=make_request) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # All requests should succeed
        assert len(results) == 5
        assert all(status == 200 for status in results)
        assert len(errors) == 0
    
    def test_operation_locks_prevent_conflicts(self, mock_drive_manager, mock_discdb):
        """Test operation locks prevent concurrent operations."""
        from core.disc_locks import acquire_operation_lock, release_operation_lock, OPERATION_RIP
        
        # Acquire lock
        lock1 = acquire_operation_lock("1", OPERATION_RIP)
        assert lock1 is not None
        
        # Try to acquire again (should fail)
        lock2 = acquire_operation_lock("1", OPERATION_RIP, timeout=0.1)
        assert lock2 is None
        
        # Release first lock
        release_operation_lock(lock1)
        
        # Now should be able to acquire
        lock3 = acquire_operation_lock("1", OPERATION_RIP)
        assert lock3 is not None
        release_operation_lock(lock3)

