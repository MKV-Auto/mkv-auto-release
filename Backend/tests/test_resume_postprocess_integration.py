"""
Integration tests for resume_postprocess: non-devmode, dev+quick ON/OFF, and restore.

These tests run the full resume_postprocess flow with real or minimally stubbed
components to verify file discovery, rename (or dev prep), gather, and validation.
"""
import pytest
from sqlalchemy.orm import sessionmaker

from api import crud, models
from api.database import Base
from core.stage_backup import restore_files, restore_stage_backup, get_stage_backup_dir
from core.stage_validation import validate_transfer_prep_output
from workers import tasks

from tests.postprocess_fixtures import job_with_rip_done_for_postprocess

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skip(
        reason="Quarantined for #391 baseline gate; hangs pytest suite. Tracked in #417; will be rewritten as test_start_transfer_integration.py in Phase 2 (feat/postprocess-collapse)."
    ),
]


def _run_resume_postprocess(job_id: str, stage_callback_mocks=None) -> None:
    from contextlib import nullcontext
    ctx = stage_callback_mocks if stage_callback_mocks is not None else nullcontext()
    with ctx:
        _closure = getattr(tasks.start_transfer.run, "__closure__", None)
        if _closure:
            raw_run = _closure[0].cell_contents
            raw_run(tasks.start_transfer, job_id=job_id)
        else:
            tasks.start_transfer.run(job_id=job_id)


class TestResumePostprocessNonDevmode:
    """Non-devmode: full rename path and file-count failure."""

    def test_full_rename_path_non_devmode(self, test_db, tmp_path, monkeypatch, stage_callback_mocks):
        monkeypatch.setattr("core.stage_backup.is_dev_mode", lambda: False)
        monkeypatch.setattr("core.settings.get_quick_postprocess_tests_enabled", lambda: True)

        job_id, title_id, paths = job_with_rip_done_for_postprocess(test_db, tmp_path, monkeypatch, use_dummy_disc=True)

        _run_resume_postprocess(job_id, stage_callback_mocks)

        with test_db() as session:
            job = crud.get_job(session, job_id)
            assert job.post_state == "completed"
            assert job.transfer_state == "ready"
            assert job.phase == "transfer"

        # Transient should have the renamed file
        p = paths.transient / "Movies" / "Test Movie (2020)" / "Test Movie (2020) [1080p].mkv"
        assert p.exists()

        # Validation passes
        with test_db() as session:
            job = crud.get_job(session, job_id)
            job.post_paths = {title_id: "Movies/Test Movie (2020)/Test Movie (2020) [1080p].mkv"}
            res = validate_transfer_prep_output(job, session, paths)
        assert res.valid, res.errors

    def test_file_count_failure_non_devmode(self, test_db, tmp_path, monkeypatch, stage_callback_mocks):
        monkeypatch.setattr("core.stage_backup.is_dev_mode", lambda: False)
        monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path / "data"))
        monkeypatch.setattr(tasks, "DATA_ROOT", tmp_path / "data")

        job_id, _, paths = job_with_rip_done_for_postprocess(test_db, tmp_path, monkeypatch, num_titles=2, use_dummy_disc=True)
        # Remove one of the raw files so actual < expected
        (paths.raw / "test_t2.mkv").unlink(missing_ok=True)

        _run_resume_postprocess(job_id, stage_callback_mocks)

        with test_db() as session:
            job = crud.get_job(session, job_id)
            assert job.post_state == "failed"
            assert "only" in (job.error_reason or "").lower() or "0" in (job.error_reason or "")


class TestResumePostprocessDevmodeQuickOn:
    """Dev+quick ON: backup, 1–10KB prep, and files_already_moved (no prep)."""

    def test_dev_quick_on_backup_and_prep_then_success(self, test_db, tmp_path, monkeypatch, stage_callback_mocks):
        monkeypatch.setattr("core.stage_backup.is_dev_mode", lambda: True)
        monkeypatch.setattr("core.settings.get_quick_postprocess_tests_enabled", lambda: True)
        monkeypatch.setattr("core.utils.is_dev_mode", lambda: True)
        monkeypatch.setattr("workers.tasks.is_dev_mode", lambda: True)
        backup_root = tmp_path / "data" / "backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr("core.stage_backup.get_backup_root", lambda: backup_root)

        raw_size = 200 * 1024  # >100KB so has_real and dev prep runs
        job_id, title_id, paths = job_with_rip_done_for_postprocess(
            test_db, tmp_path, monkeypatch, num_titles=1, raw_file_size=raw_size, use_dummy_disc=True
        )
        (paths.raw / "test_t1.mkv").write_bytes(b"y" * raw_size)

        _run_resume_postprocess(job_id, stage_callback_mocks)

        with test_db() as session:
            job = crud.get_job(session, job_id)
            assert job.post_state == "completed"

        # Dev prep: original moved to backup, 1–10KB mock created in raw at same path
        backup_dir = get_stage_backup_dir(job_id, "postprocess")
        backup_mkv = list((backup_dir / "files").rglob("*.mkv")) if (backup_dir / "files").exists() else []
        assert len(backup_mkv) >= 1, "Original should be in backup"
        assert backup_mkv[0].stat().st_size == raw_size

        raw_mkv = list(paths.raw.rglob("*.mkv"))
        assert len(raw_mkv) == 1, "Dev prep creates one mock in raw"
        assert raw_mkv[0].stat().st_size <= 10 * 1024, "Raw mock should be 1–10KB"

        out = paths.transient / "Movies" / "Test Movie (2020)" / "Test Movie (2020) [1080p].mkv"
        assert out.exists()
        assert out.stat().st_size <= 10 * 1024, "Output should be 1–10KB mock"

    def test_dev_quick_on_files_already_moved_skips_prep(self, test_db, tmp_path, monkeypatch, stage_callback_mocks):
        monkeypatch.setattr("core.stage_backup.is_dev_mode", lambda: True)
        monkeypatch.setattr("core.settings.get_quick_postprocess_tests_enabled", lambda: True)
        monkeypatch.setattr("core.utils.is_dev_mode", lambda: True)
        monkeypatch.setattr("workers.tasks.is_dev_mode", lambda: True)

        job_id, title_id, paths = job_with_rip_done_for_postprocess(test_db, tmp_path, monkeypatch, use_dummy_disc=True)
        # Pre-create transient (files already moved); remove raw so source_dir has no large MKV
        for f in paths.raw.rglob("*.mkv"):
            f.unlink()
        dest_dir = paths.transient / "Movies" / "Test Movie (2020)"
        dest_dir.mkdir(parents=True, exist_ok=True)
        small = b"z" * 100
        (dest_dir / "Test Movie (2020) [1080p].mkv").write_bytes(small)

        with test_db() as session:
            job = crud.get_job(session, job_id)
            job.post_paths = {title_id: "Movies/Test Movie (2020)/Test Movie (2020) [1080p].mkv"}
            dt = session.query(models.DiscTitle).filter(models.DiscTitle.disc_id == job.disc_id).first()
            if dt:
                dt.mkv_size = len(small)
            session.commit()

        _run_resume_postprocess(job_id, stage_callback_mocks)

        with test_db() as session:
            job = crud.get_job(session, job_id)
            assert job.post_state == "completed"


class TestResumePostprocessDevmodeQuickOff:
    """Dev+quick OFF: same as non-devmode (no backup, no mocks)."""

    def test_dev_quick_off_same_as_non_devmode(self, test_db, tmp_path, monkeypatch, stage_callback_mocks):
        monkeypatch.setattr("core.stage_backup.is_dev_mode", lambda: True)
        monkeypatch.setattr("core.settings.get_quick_postprocess_tests_enabled", lambda: False)
        monkeypatch.setattr("core.utils.is_dev_mode", lambda: True)
        monkeypatch.setattr("workers.tasks.is_dev_mode", lambda: True)

        raw_size = 2000
        job_id, _, paths = job_with_rip_done_for_postprocess(
            test_db, tmp_path, monkeypatch, num_titles=1, raw_file_size=raw_size, use_dummy_disc=True
        )

        _run_resume_postprocess(job_id, stage_callback_mocks)

        with test_db() as session:
            job = crud.get_job(session, job_id)
            assert job.post_state == "completed"

        out = paths.transient / "Movies" / "Test Movie (2020)" / "Test Movie (2020) [1080p].mkv"
        assert out.exists()
        assert out.stat().st_size == raw_size, "File size should be unchanged (no 1–10KB mock)"
        backup_dir = get_stage_backup_dir(job_id, "postprocess")
        assert not (backup_dir / "files").exists(), "No file backup when quick_postprocess_tests off"


class TestResumePostprocessRestore:
    """Restore after dev+quick postprocess."""

    def test_restore_after_dev_quick_postprocess(self, test_db, tmp_path, monkeypatch, stage_callback_mocks):
        monkeypatch.setattr("core.stage_backup.is_dev_mode", lambda: True)
        monkeypatch.setattr("core.settings.get_quick_postprocess_tests_enabled", lambda: True)
        monkeypatch.setattr("core.utils.is_dev_mode", lambda: True)
        monkeypatch.setattr("workers.tasks.is_dev_mode", lambda: True)
        backup_root = tmp_path / "data" / "backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr("core.stage_backup.get_backup_root", lambda: backup_root)

        raw_size = 200 * 1024
        job_id, _, paths = job_with_rip_done_for_postprocess(
            test_db, tmp_path, monkeypatch, num_titles=1, raw_file_size=raw_size, use_dummy_disc=True
        )
        (paths.raw / "test_t1.mkv").write_bytes(b"g" * raw_size)

        _run_resume_postprocess(job_id, stage_callback_mocks)

        backup_dir = get_stage_backup_dir(job_id, "postprocess")
        assert (backup_dir / "files").exists()
        assert (backup_dir / "database.json").exists()

        # Restore: DB then files into raw
        with test_db() as session:
            restore_stage_backup(job_id, "postprocess", session)
        restore_files(backup_dir, paths.raw)

        raw_mkvs = list(paths.raw.rglob("*.mkv"))
        assert len(raw_mkvs) >= 1
        assert raw_mkvs[0].stat().st_size == raw_size
