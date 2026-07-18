"""Unit tests for core.devmode_backup: get_backup_root, get_stage_backup_dir."""
import os
import pytest
from pathlib import Path

from core.devmode_backup import get_backup_root, get_stage_backup_dir


def test_get_backup_root_ends_with_backups(tmp_path, monkeypatch):
    monkeypatch.setattr("os.getenv", lambda k, d=None: None if k in ("MKVAUTO_DATA_DIR", "MKVAUTO_DATA", "MAKEMKV_DATA_DIR") else os.getenv(k, d))
    monkeypatch.setattr("core.devmode_backup.get_mkvauto_root", lambda: tmp_path)
    root = get_backup_root()
    assert root.name == "backups"
    assert root.parent == tmp_path


def test_get_backup_root_uses_env_when_set(tmp_path, monkeypatch):
    monkeypatch.setattr("os.getenv", lambda k, d=None: str(tmp_path) if k == "MKVAUTO_DATA_DIR" else (None if k in ("MKVAUTO_DATA", "MAKEMKV_DATA_DIR") else os.getenv(k, d)))
    root = get_backup_root()
    assert root == tmp_path / "backups"


def test_get_stage_backup_dir_is_job_id_stage_under_backup_root(tmp_path, monkeypatch):
    monkeypatch.setattr("os.getenv", lambda k, d=None: None if k in ("MKVAUTO_DATA_DIR", "MKVAUTO_DATA", "MAKEMKV_DATA_DIR") else os.getenv(k, d))
    monkeypatch.setattr("core.devmode_backup.get_mkvauto_root", lambda: tmp_path)
    got = get_stage_backup_dir("j1", "postprocess")
    assert got == get_backup_root() / "j1" / "postprocess"
