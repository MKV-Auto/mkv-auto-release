"""
Tests for drive operations access control.
Verifies that unauthorized access is blocked and error messages are clear.
"""
import pytest
from types import SimpleNamespace
from unittest.mock import patch, Mock

from core._drive_operations import (
    list_drives,
    get_disc_info,
    scan_disc_info,
    hash_disc,
    handle_disc_eject,
    handle_disc_insert,
)


class TestAccessControl:
    """Tests for access control decorator."""

    def _stack_for_module(self, module_name: str):
        """Create a minimal stack for access control checks."""
        return [
            SimpleNamespace(frame=SimpleNamespace(f_globals={"__name__": "core._drive_operations"})),
            SimpleNamespace(frame=SimpleNamespace(f_globals={"__name__": module_name})),
        ]
    
    def test_disc_manager_can_call_list_drives(self, monkeypatch):
        """Test that disc_manager can call list_drives."""
        mock_get_drives = Mock(return_value=[("1", "/mnt/sr1")])
        monkeypatch.setattr("core._drive_operations.get_drives", mock_get_drives)
        
        # Simulate call from disc_manager
        with patch("core._drive_operations.inspect.stack", return_value=self._stack_for_module("core.disc_manager")):
            result = list_drives()
            assert len(result) == 1
            assert result[0]["disc_num"] == "1"
    
    def test_drives_router_can_call_list_drives(self, monkeypatch):
        """Test that drives router can call list_drives."""
        mock_get_drives = Mock(return_value=[("1", "/mnt/sr1")])
        monkeypatch.setattr("core._drive_operations.get_drives", mock_get_drives)
        
        # Simulate call from drives router
        with patch("core._drive_operations.inspect.stack", return_value=self._stack_for_module("api.routers.drives")):
            result = list_drives()
            assert len(result) == 1
    
    def test_unauthorized_module_cannot_call_list_drives(self, monkeypatch):
        """Test that unauthorized modules cannot call list_drives."""
        mock_get_drives = Mock(return_value=[("1", "/mnt/sr1")])
        monkeypatch.setattr("core._drive_operations.get_drives", mock_get_drives)
        
        # Simulate call from unauthorized module
        with patch("core._drive_operations.inspect.stack", return_value=self._stack_for_module("api.routers.jobs")):
            with pytest.raises(RuntimeError) as exc_info:
                list_drives()
            assert "internal-only" in str(exc_info.value).lower()
            assert "core.disc_manager" in str(exc_info.value)
    
    def test_unauthorized_module_cannot_call_get_disc_info(self, monkeypatch):
        """Test that unauthorized modules cannot call get_disc_info."""
        # Simulate call from unauthorized module
        with patch("core._drive_operations.inspect.stack", return_value=self._stack_for_module("workers.tasks")):
            with pytest.raises(RuntimeError) as exc_info:
                get_disc_info("1", "/mnt/sr1")
            assert "internal-only" in str(exc_info.value).lower()
            assert "core.disc_manager" in str(exc_info.value)
    
    def test_unauthorized_module_cannot_call_scan_disc_info(self, monkeypatch):
        """Test that unauthorized modules cannot call scan_disc_info."""
        # Simulate call from unauthorized module
        with patch("core._drive_operations.inspect.stack", return_value=self._stack_for_module("api.routers.discs")):
            with pytest.raises(RuntimeError) as exc_info:
                scan_disc_info("1", "/mnt/sr1")
            assert "internal-only" in str(exc_info.value).lower()
    
    def test_unauthorized_module_cannot_call_hash_disc(self, monkeypatch):
        """Test that unauthorized modules cannot call hash_disc."""
        # Simulate call from unauthorized module
        with patch("core._drive_operations.inspect.stack", return_value=self._stack_for_module("api.routers.events")):
            with pytest.raises(RuntimeError) as exc_info:
                hash_disc("1", "/mnt/sr1")
            assert "internal-only" in str(exc_info.value).lower()
    
    def test_unauthorized_module_cannot_call_handle_disc_eject(self, monkeypatch):
        """Test that unauthorized modules cannot call handle_disc_eject."""
        # Simulate call from unauthorized module
        with patch("core._drive_operations.inspect.stack", return_value=self._stack_for_module("api.routers.jobs")):
            with pytest.raises(RuntimeError) as exc_info:
                handle_disc_eject("1")
            assert "internal-only" in str(exc_info.value).lower()
    
    def test_unauthorized_module_cannot_call_handle_disc_insert(self, monkeypatch):
        """Test that unauthorized modules cannot call handle_disc_insert."""
        # Simulate call from unauthorized module
        with patch("core._drive_operations.inspect.stack", return_value=self._stack_for_module("api.routers.jobs")):
            with pytest.raises(RuntimeError) as exc_info:
                handle_disc_insert("1", "/mnt/sr1")
            assert "internal-only" in str(exc_info.value).lower()


class TestImportRestrictions:
    """Tests for import-time restrictions."""
    
    def test_import_from_disc_manager_allowed(self):
        """Test that importing from disc_manager is allowed."""
        # This should not raise an ImportError
        from core.disc_manager import get_disc_info
        assert callable(get_disc_info)
    
    def test_import_from_drives_router_allowed(self):
        """Test that importing from drives router is allowed."""
        # This should not raise an ImportError
        from api.routers.drives import router
        assert router is not None
    
    def test_import_from_tests_allowed(self):
        """Test that importing from tests is allowed."""
        # This test itself imports _drive_operations, so it should work
        from core._drive_operations import list_drives
        assert callable(list_drives)




