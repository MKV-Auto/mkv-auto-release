"""
Tests for stage backup gating: create_stage_backup and backup_files.

Verifies that create_stage_backup returns None when is_dev_mode() or
get_quick_postprocess_tests_enabled() is false, and that backup_files
is a no-op unless both are true.
"""
import json
import uuid
from pathlib import Path

import pytest

from api import models
from core.stage_backup import (
    create_stage_backup,
    backup_files,
    get_stage_backup_dir,
    validate_backup,
)


def _create_job_with_disc(session):
    disc = models.Disc(id=str(uuid.uuid4()), content_hash="stage-backup-test")
    session.add(disc)
    session.flush()
    job = models.Job(
        disc_id=disc.id,
        disc_num="1",
        mount_point="/mnt/dvd",
        mode="copy",
    )
    session.add(job)
    session.commit()
    return job, disc


class TestCreateStageBackupGating:
    """Test create_stage_backup returns None when dev or quick_postprocess_tests is off."""

    def test_returns_none_when_is_dev_mode_false(self, test_db, tmp_path, monkeypatch):
        monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path))
        monkeypatch.setattr("core.stage_backup.is_dev_mode", lambda: False)
        monkeypatch.setattr("core.settings.get_quick_postprocess_tests_enabled", lambda: True)

        with test_db() as session:
            job, _ = _create_job_with_disc(session)
            job_id = str(job.id)

        out = create_stage_backup(job_id, "postprocess", test_db(), reason="test")
        assert out is None
        backup_dir = get_stage_backup_dir(job_id, "postprocess")
        assert not (backup_dir / "database.json").exists()

    def test_returns_none_when_quick_postprocess_tests_disabled(self, test_db, tmp_path, monkeypatch):
        monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path))
        monkeypatch.setattr("core.stage_backup.is_dev_mode", lambda: True)
        monkeypatch.setattr("core.settings.get_quick_postprocess_tests_enabled", lambda: False)

        with test_db() as session:
            job, _ = _create_job_with_disc(session)
            job_id = str(job.id)

        out = create_stage_backup(job_id, "postprocess", test_db(), reason="test")
        assert out is None

    def test_returns_path_and_creates_database_json_when_both_true(
        self, test_db, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path))
        monkeypatch.setattr("core.stage_backup.is_dev_mode", lambda: True)
        monkeypatch.setattr("core.settings.get_quick_postprocess_tests_enabled", lambda: True)

        with test_db() as session:
            job, disc = _create_job_with_disc(session)
            job_id = str(job.id)

        out = create_stage_backup(job_id, "postprocess", test_db(), reason="test")
        assert out is not None
        assert out.exists()
        db_json = out / "database.json"
        assert db_json.exists()
        with open(db_json) as f:
            data = json.load(f)
        assert data["job_id"] == job_id
        assert data["stage"] == "postprocess"
        assert data["reason"] == "test"
        assert data.get("job") is not None
        assert data.get("disc") is not None


class TestBackupFilesGating:
    """Test backup_files is no-op when dev or quick_postprocess_tests is off, and copies files when both on."""

    def test_noop_when_is_dev_mode_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path))
        monkeypatch.setattr("core.stage_backup.is_dev_mode", lambda: False)
        monkeypatch.setattr("core.settings.get_quick_postprocess_tests_enabled", lambda: True)

        src = tmp_path / "src"
        src.mkdir()
        (src / "a.mkv").write_bytes(b"x" * 2000)
        backup_dir = tmp_path / "backups" / "job1" / "postprocess"
        backup_dir.mkdir(parents=True)

        backup_files(src, backup_dir)

        assert (src / "a.mkv").exists(), "Source should be unchanged"
        assert not (backup_dir / "files").exists(), "backup/files should not be created"

    def test_noop_when_quick_postprocess_tests_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path))
        monkeypatch.setattr("core.stage_backup.is_dev_mode", lambda: True)
        monkeypatch.setattr("core.settings.get_quick_postprocess_tests_enabled", lambda: False)

        src = tmp_path / "src"
        src.mkdir()
        (src / "a.mkv").write_bytes(b"x" * 2000)
        backup_dir = tmp_path / "backups" / "job1" / "postprocess"
        backup_dir.mkdir(parents=True)

        backup_files(src, backup_dir)

        assert (src / "a.mkv").exists(), "Source should be unchanged"
        assert not (backup_dir / "files").exists(), "backup/files should not be created"

    def test_copies_files_into_backup_when_both_true(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path))
        monkeypatch.setattr("core.stage_backup.is_dev_mode", lambda: True)
        monkeypatch.setattr("core.settings.get_quick_postprocess_tests_enabled", lambda: True)

        src = tmp_path / "src"
        src.mkdir()
        (src / "a.mkv").write_bytes(b"x" * 2000)
        (src / "b.mkv").write_bytes(b"y" * 3000)
        backup_dir = tmp_path / "backups" / "job1" / "postprocess"
        backup_dir.mkdir(parents=True)

        backup_files(src, backup_dir)

        files_backup = backup_dir / "files"
        assert files_backup.exists(), "backup/files should be created"
        assert (files_backup / "a.mkv").exists()
        assert (files_backup / "b.mkv").exists()
        assert (src / "a.mkv").exists(), "Source should be unchanged (copy-only)"
        assert (src / "b.mkv").exists(), "Source should be unchanged (copy-only)"
        assert (files_backup / "a.mkv").read_bytes() == (src / "a.mkv").read_bytes()
        assert (files_backup / "b.mkv").read_bytes() == (src / "b.mkv").read_bytes()


class TestValidateBackup:
    """Test validate_backup: DB snapshot and file size checks before devmode mock creation."""

    def test_passes_when_db_and_files_match(self, test_db, tmp_path, monkeypatch):
        monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path))
        monkeypatch.setattr("core.stage_backup.is_dev_mode", lambda: True)
        monkeypatch.setattr("core.settings.get_quick_postprocess_tests_enabled", lambda: True)

        with test_db() as session:
            job, _ = _create_job_with_disc(session)
            job_id = str(job.id)

        backup_dir = create_stage_backup(job_id, "postprocess", test_db(), reason="test")
        assert backup_dir is not None
        (backup_dir / "files").mkdir(parents=True)
        (backup_dir / "files" / "a.mkv").write_bytes(b"x" * 2000)
        expected_file_sizes = {"a.mkv": 2000}

        ok, errors = validate_backup(backup_dir, expected_file_sizes, job_id=job_id)
        assert ok is True
        assert errors == []

    def test_fails_when_database_json_missing(self, tmp_path):
        backup_dir = tmp_path / "backups" / "job1" / "postprocess"
        backup_dir.mkdir(parents=True)
        (backup_dir / "files").mkdir()
        (backup_dir / "files" / "a.mkv").write_bytes(b"x" * 100)

        ok, errors = validate_backup(backup_dir, {"a.mkv": 100})
        assert ok is False
        assert any("database.json" in e for e in errors)

    def test_fails_when_file_missing_in_backup(self, test_db, tmp_path, monkeypatch):
        monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path))
        monkeypatch.setattr("core.stage_backup.is_dev_mode", lambda: True)
        monkeypatch.setattr("core.settings.get_quick_postprocess_tests_enabled", lambda: True)

        with test_db() as session:
            job, _ = _create_job_with_disc(session)
            job_id = str(job.id)

        backup_dir = create_stage_backup(job_id, "postprocess", test_db(), reason="test")
        assert backup_dir is not None
        # No files/ or empty files/ - expected a.mkv
        (backup_dir / "files").mkdir(parents=True, exist_ok=True)

        ok, errors = validate_backup(backup_dir, {"a.mkv": 100}, job_id=job_id)
        assert ok is False
        assert any("missing" in e.lower() and "a.mkv" in e for e in errors)

    def test_fails_when_file_size_mismatch(self, test_db, tmp_path, monkeypatch):
        monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path))
        monkeypatch.setattr("core.stage_backup.is_dev_mode", lambda: True)
        monkeypatch.setattr("core.settings.get_quick_postprocess_tests_enabled", lambda: True)

        with test_db() as session:
            job, _ = _create_job_with_disc(session)
            job_id = str(job.id)

        backup_dir = create_stage_backup(job_id, "postprocess", test_db(), reason="test")
        assert backup_dir is not None
        (backup_dir / "files").mkdir(parents=True)
        (backup_dir / "files" / "a.mkv").write_bytes(b"x" * 200)  # 200 bytes

        ok, errors = validate_backup(backup_dir, {"a.mkv": 100}, job_id=job_id)
        assert ok is False
        assert any("Size mismatch" in e and "a.mkv" in e for e in errors)

    def test_fails_when_job_id_mismatch(self, test_db, tmp_path, monkeypatch):
        monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path))
        monkeypatch.setattr("core.stage_backup.is_dev_mode", lambda: True)
        monkeypatch.setattr("core.settings.get_quick_postprocess_tests_enabled", lambda: True)

        with test_db() as session:
            job, _ = _create_job_with_disc(session)
            job_id = str(job.id)

        backup_dir = create_stage_backup(job_id, "postprocess", test_db(), reason="test")
        assert backup_dir is not None
        # Overwrite database.json so job_id is different
        db_json = backup_dir / "database.json"
        data = json.loads(db_json.read_text())
        data["job_id"] = "other-job-id"
        db_json.write_text(json.dumps(data))

        ok, errors = validate_backup(backup_dir, {}, job_id="expected-job-id")
        assert ok is False
        assert any("job_id" in e.lower() for e in errors)
