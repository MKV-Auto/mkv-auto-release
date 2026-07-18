"""
Tests for Drive Manager endpoints (internal use only).
Tests the refactored endpoints that return raw info only.
Now tests the backend API router instead of the separate drive manager service.
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
from fastapi.testclient import TestClient
from filelock import Timeout

from api.main import app
from core.disc_cache import set_payload as cache_set, get as cache_get


@pytest.fixture
def client():
    """Create FastAPI TestClient for Backend API (includes drive endpoints)."""
    # Mock UDS server to avoid socket issues in tests
    with patch("api.main.UDSServer"):
        yield TestClient(app)


@pytest.fixture
def mock_hash_media_disc(monkeypatch):
    """Mock hash_media_disc function."""
    def mock_hash(mount_point, allow_reentrant=False):
        return "TESTHASH123"
    
    monkeypatch.setattr("core._drive_operations.hash_media_disc", mock_hash)
    return mock_hash


@pytest.fixture
def mock_run_makemkv(monkeypatch):
    """Mock run_makemkv function. Production returns (log_str, pid)."""
    _log = """DRV:1,256,999,0,"BD-ROM","TEST_DISC","/mnt/sr1"
TINFO:0,0,0,"Test Title"
SINFO:0,0,19,0,"1920x1080"
MSG:3307,0,2,"File 00001.mpls was added as title #1"
"""

    def mock_run(args, log_path=None, line_cb=None):
        args_s = args if isinstance(args, str) else str(args)
        if "disc:9999" in args_s:
            return (
                'DRV:1,0,256,1,"BD-ROM","ENUM","/mnt/sr1"\n',
                None,
            )
        return (_log, None)

    monkeypatch.setattr("core._drive_operations.run_makemkv", mock_run)
    monkeypatch.setattr("core.utils.run_makemkv", mock_run)
    return mock_run


@pytest.fixture
def mock_get_drives(monkeypatch):
    """Mock get_drives function."""
    def mock_drives():
        return [("1", "/mnt/sr1"), ("2", "/mnt/sr2")]
    
    monkeypatch.setattr("core._drive_operations.get_drives", mock_drives)
    return mock_drives


class TestHealthz:
    """Tests for /healthz endpoint."""
    
    def test_healthz(self, client):
        """Test health check endpoint."""
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestDrives:
    """Tests for /drives endpoint."""

    def test_drives_list(self, client, mock_get_drives):
        """Test listing drives."""
        response = client.get("/drives/drives")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["disc_num"] == "1"
        assert data[0]["mount_point"] == "/mnt/sr1"

    def test_drives_snapshot_endpoint_returns_loaded_and_unloaded(self, monkeypatch):
        """#571: ``/drives/snapshot`` exposes every drive the registry knows
        about — including drives with no media loaded — so the frontend can
        distinguish "Insert Disc" from "Drive Not Connected"."""

        from core import drive_registry
        from core.drive_identity import DriveIdentity
        from core.drive_registry import DriveSnapshot

        snapshots = [
            DriveSnapshot(
                mount_point="/dev/sr0", loaded=True, volume_label="VENOM",
                media_kind="BD",
                identity=DriveIdentity(
                    by_id_serial="1958040110900395", vendor="PIONEER",
                    model="BD-RW BDR-XD06U", bus="usb", by_id_name="",
                    hardware_name=None, identity_source="by-id",
                ),
                udev_state={}, observed_at=0.0,
            ),
            DriveSnapshot(
                mount_point="/dev/sr1", loaded=False, volume_label=None,
                media_kind=None,
                identity=DriveIdentity(
                    by_id_serial="AAAABBBB000E", vendor="ASUS",
                    model="BW-16D1HT", bus="usb", by_id_name="",
                    hardware_name=None, identity_source="by-id",
                ),
                udev_state={}, observed_at=0.0,
            ),
        ]
        monkeypatch.setattr(
            drive_registry, "snapshot_drives", lambda **kw: snapshots
        )

        # Call the handler directly — bypasses the FastAPI middleware that
        # requires a live Postgres for the warmup gate.
        from api.routers.drives import drives_snapshot

        result = drives_snapshot()

        assert len(result) == 2
        assert [r["mount_point"] for r in result] == ["/dev/sr0", "/dev/sr1"]
        assert [r["loaded"] for r in result] == [True, False]
        assert result[0]["by_id_serial"] == "1958040110900395"
        assert result[0]["multi_drive_safe"] is True
        assert result[0]["media_kind"] == "BD"
        assert result[1]["volume_label"] is None
        assert result[1]["media_kind"] is None

    def test_list_drives_does_not_invoke_makemkv(self, monkeypatch):
        """After #562 PR 2, the ``/drives/drives`` handler — ``list_drives()``
        — must answer from the OS-level registry without touching MakeMKV.
        That's the #545 / #557 contention root cause.

        The test exercises ``list_drives()`` directly rather than via
        ``TestClient`` because the FastAPI middleware stack requires a live
        Postgres for the warmup gate — orthogonal to what's under test here.
        """

        from core import drive_registry, utils
        from core._drive_operations import list_drives
        from core.drive_identity import DriveIdentity
        from core.drive_registry import DriveSnapshot

        snapshot = DriveSnapshot(
            mount_point="/dev/sr0",
            loaded=True,
            volume_label="VENOM_2018",
            media_kind="BD",
            identity=DriveIdentity(
                by_id_serial="S0", vendor="PIONEER", model="BD-RW BDR-XD06U",
                bus="usb", by_id_name="", hardware_name=None,
                identity_source="by-id",
            ),
            udev_state={},
            observed_at=0.0,
        )
        monkeypatch.setattr(
            drive_registry, "snapshot_drives", lambda **kw: [snapshot]
        )
        # Empty MakeMKV hardware map exercises the identity-fallback path.
        monkeypatch.setattr(utils, "get_drive_hardware_map", lambda: {})
        # ``build_drive_api_dict`` re-resolves identity from the live FS; in
        # the test env there is no real /dev/sr0, so stub it with the same
        # identity the registry already produced.
        monkeypatch.setattr(
            utils, "resolve_drive_identity", lambda mp, **kw: snapshot.identity
        )

        makemkv_called = Mock()
        monkeypatch.setattr(utils, "run_makemkv", makemkv_called)

        result = list_drives()

        makemkv_called.assert_not_called()
        assert len(result) == 1
        assert result[0]["mount_point"] == "/dev/sr0"
        # Identity-fallback hardware label populated without a MakeMKV scan.
        assert "PIONEER" in result[0]["drive_hardware_name"]
        assert result[0]["by_id_serial"] == "S0"


class TestDiscInfo:
    """Tests for /discinfo endpoint."""
    
    def test_discinfo_from_cache(self, client, mock_hash_media_disc, mock_run_makemkv):
        """Test getting disc info from cache."""
        # Pre-populate cache
        cached_info = {
            "disc_num": "1",
            "mount_point": "/mnt/sr1",
            "disc_hash": "CACHEDHASH",
            "info_log": "cached log",
        }
        cache_set("1", cached_info)
        
        response = client.get("/drives/discinfo", params={"disc_num": "1", "mount_point": "/mnt/sr1"})
        assert response.status_code == 200
        data = response.json()
        assert data["disc_hash"] == "CACHEDHASH"
    
    def test_discinfo_not_cached_no_refresh(self, client):
        """Test getting disc info when not cached and refresh not requested."""
        # Clear cache first
        from core.disc_cache import clear_key
        clear_key("1")
        
        response = client.get("/drives/discinfo", params={"disc_num": "1", "mount_point": "/mnt/sr1", "refresh": False})
        assert response.status_code == 404
    
    def test_discinfo_scan_returns_raw_info(self, client, mock_hash_media_disc, mock_run_makemkv):
        """Test discinfo scan returns raw info only (no DiscDB, no parsing)."""
        response = client.get("/drives/discinfo", params={"disc_num": "1", "mount_point": "/mnt/sr1", "refresh": True})
        assert response.status_code == 200
        data = response.json()
        
        # Should have raw info only
        assert "disc_num" in data
        assert "mount_point" in data
        assert "disc_hash" in data
        assert "info_log" in data
        assert "raw_info_log" in data
        
        # Should NOT have parsed/DiscDB fields
        assert "movie_name" not in data
        assert "tracks" not in data
        assert "discdb_hit" not in data
    
    def test_discinfo_scan_with_active_rip(self, client, mock_hash_media_disc, mock_run_makemkv):
        """Test discinfo scan when rip is active."""
        response = client.get("/drives/discinfo", params={"disc_num": "1", "mount_point": "/mnt/sr1", "refresh": True})
        assert response.status_code == 200
        data = response.json()
        assert data["disc_hash"] == "TESTHASH123"


class TestDiscInfoScan:
    """Tests for /discinfo/scan endpoint."""
    
    def test_discinfo_scan_success(self, client, mock_hash_media_disc, mock_run_makemkv):
        """Test discinfo scan endpoint."""
        with patch("core._drive_operations.acquire_operation_lock") as mock_acquire:
            mock_lock = Mock()
            mock_acquire.return_value = mock_lock
            
            response = client.post("/drives/discinfo/scan", params={"disc_num": "1", "mount_point": "/mnt/sr1"})
            assert response.status_code == 200
            data = response.json()
            assert "disc_hash" in data
            assert "info_log" in data
            
            mock_acquire.assert_called()
            mock_lock.release.assert_called()
    
    def test_discinfo_scan_with_active_rip(self, client):
        """Test discinfo scan when rip is active."""
        with patch("core._drive_operations.is_operation_active", return_value=True):
            response = client.post("/drives/discinfo/scan", params={"disc_num": "1", "mount_point": "/mnt/sr1"})
            assert response.status_code == 409
            assert "rip operation in progress" in response.json()["detail"]
    
    def test_discinfo_scan_with_active_info(self, client):
        """Test discinfo scan when info scan is already active."""
        with patch("core._drive_operations.acquire_operation_lock", return_value=None):
            response = client.post("/drives/discinfo/scan", params={"disc_num": "1", "mount_point": "/mnt/sr1"})
            assert response.status_code == 409
            assert "already in progress" in response.json()["detail"]


class TestDiscInfoHash:
    """Tests for /discinfo/hash endpoint."""
    
    def test_discinfo_hash_success(self, client, mock_hash_media_disc):
        """Test discinfo hash endpoint."""
        with patch("core._drive_operations.acquire_operation_lock") as mock_acquire:
            mock_lock = Mock()
            mock_acquire.return_value = mock_lock
            
            response = client.post("/drives/discinfo/hash", params={"disc_num": "1", "mount_point": "/mnt/sr1"})
            assert response.status_code == 200
            data = response.json()
            assert data["disc_hash"] == "TESTHASH123"
            assert data["content_hash"] == "TESTHASH123"
            
            mock_acquire.assert_called()
            mock_lock.release.assert_called()
    
    def test_discinfo_hash_with_active_rip(self, client):
        """Test discinfo hash when rip is active."""
        with patch("core._drive_operations.is_operation_active", return_value=True):
            response = client.post("/drives/discinfo/hash", params={"disc_num": "1", "mount_point": "/mnt/sr1"})
            assert response.status_code == 409


class TestDiscEject:
    """Tests for /disc/eject endpoint."""
    
    def test_disc_eject_success(self, client):
        """Test disc eject endpoint."""
        # Pre-populate cache
        cache_set("1", {"disc_num": "1", "disc_hash": "TESTHASH"})
        
        response = client.post("/drives/disc/eject", params={"disc_num": "1"})
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        
        # Cache should be cleared
        assert cache_get("1") is None


class TestDiscInsert:
    """Tests for /disc/insert endpoint."""
    
    def test_disc_insert_success(self, client):
        """Test disc insert endpoint."""
        # Pre-populate cache
        cache_set("1", {"disc_num": "1", "disc_hash": "TESTHASH"})
        from core.disc_cache import clear_key

        def mock_handle_disc_insert(disc_num, mount_point):
            clear_key(str(disc_num))
            return {"status": "ok", "message": "Cache cleared"}

        with patch("api.routers.drives.handle_disc_insert", mock_handle_disc_insert):
            response = client.post("/drives/disc/insert", params={"disc_num": "1", "mount_point": "/mnt/sr1"})
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
        
        # Cache should be cleared without triggering a scan
        assert cache_get("1") is None

