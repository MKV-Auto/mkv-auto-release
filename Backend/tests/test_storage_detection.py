"""Unit tests for core.storage_detection: get_storage_info, get_local_storage_info, SMB/NFS/rsync error branches."""
import pytest
from pathlib import Path
from types import SimpleNamespace

from core.storage_detection import (
    get_storage_info,
    get_local_storage_info,
    get_smb_storage_info,
    get_nfs_storage_info,
    get_rsync_storage_info,
)


def _cfg(mode, transfer_dir=None, config_data=None, id="c1"):
    return SimpleNamespace(mode=mode, transfer_dir=transfer_dir, config_data=config_data or {}, id=id)


# --- get_local_storage_info ---


def test_get_local_storage_info_not_configured():
    config = _cfg("local", transfer_dir=None)
    info, err = get_local_storage_info(config)
    assert info is None
    assert "not configured" in (err or "").lower()


def test_get_local_storage_info_success(tmp_path):
    config = _cfg("local", transfer_dir=str(tmp_path))
    info, err = get_local_storage_info(config)
    assert err is None
    assert info is not None
    assert "path" in info
    assert "total" in info
    assert "used" in info
    assert "free" in info
    assert info["total"] >= 0


# --- get_storage_info ---


def test_get_storage_info_unknown_mode():
    config = _cfg("unknown")
    info, err = get_storage_info(None, config)
    assert info is None
    assert "Unknown transfer mode" in (err or "")


def test_get_storage_info_local_delegates(tmp_path):
    config = _cfg("local", transfer_dir=str(tmp_path))
    info, err = get_storage_info(None, config)
    assert err is None
    assert info is not None
    assert "path" in info


# --- get_smb_storage_info (error branch) ---


def test_get_smb_storage_info_host_share_not_configured(monkeypatch):
    monkeypatch.setattr("core.transfer.utils.credentials.get_decrypted_credentials", lambda db, cid: {})
    config = _cfg("smb", config_data={})
    info, err = get_smb_storage_info(None, config)
    assert info is None
    assert "host and share" in (err or "").lower()


# --- get_nfs_storage_info (error branch) ---


def test_get_nfs_storage_info_server_export_not_configured():
    config = _cfg("nfs", config_data={})
    info, err = get_nfs_storage_info(config, None)
    assert info is None
    assert "server" in (err or "").lower() and "export" in (err or "").lower()


# --- get_rsync_storage_info (error branch) ---


def test_get_rsync_storage_info_host_user_path_not_configured(monkeypatch):
    monkeypatch.setattr("core.transfer.utils.credentials.get_decrypted_credentials", lambda db, cid: {})
    config = _cfg("rsync", config_data={})
    info, err = get_rsync_storage_info(None, config)
    assert info is None
    assert "host" in (err or "").lower() or "path" in (err or "").lower()
