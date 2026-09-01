"""
Unit tests for core.job_cleanup: job_has_mkv_files, job_has_cleanable_files,
and remove_mkv_files_from_job (including preview file cleanup).
"""
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.job_paths import JobPaths
from core.job_cleanup import (
    job_has_mkv_files,
    job_has_cleanable_files,
    remove_mkv_files_from_job,
    MANIFEST_FILENAME,
)


@pytest.fixture
def mock_job():
    job = Mock()
    job.id = "test-job-cleanup-123"
    return job


@pytest.fixture
def job_paths(mock_job, tmp_path):
    """JobPaths under a temp directory for test isolation."""
    paths = JobPaths(tmp_path / "jobs", mock_job.id)
    paths.root.mkdir(parents=True, exist_ok=True)
    return paths


class TestJobHasMkvFiles:
    """Tests for job_has_mkv_files()."""

    def test_returns_false_when_no_dirs(self, mock_job, tmp_path):
        paths = JobPaths(tmp_path / "jobs", mock_job.id)
        assert job_has_mkv_files(paths) is False

    def test_returns_false_when_dirs_empty(self, job_paths):
        job_paths.raw.mkdir(parents=True, exist_ok=True)
        job_paths.transient.mkdir(parents=True, exist_ok=True)
        assert job_has_mkv_files(job_paths) is False

    def test_returns_false_when_only_non_mkv(self, job_paths):
        job_paths.raw.mkdir(parents=True, exist_ok=True)
        (job_paths.raw / "foo.txt").write_text("x")
        (job_paths.raw / "bar.mka").write_text("x")
        assert job_has_mkv_files(job_paths) is False

    def test_returns_true_when_raw_has_mkv(self, job_paths):
        job_paths.raw.mkdir(parents=True, exist_ok=True)
        (job_paths.raw / "title00.mkv").write_text("x")
        assert job_has_mkv_files(job_paths) is True

    def test_returns_true_when_transient_has_mkv(self, job_paths):
        job_paths.transient.mkdir(parents=True, exist_ok=True)
        (job_paths.transient / "movie.mkv").write_text("x")
        assert job_has_mkv_files(job_paths) is True

    def test_returns_true_when_both_have_mkv(self, job_paths):
        job_paths.raw.mkdir(parents=True, exist_ok=True)
        job_paths.transient.mkdir(parents=True, exist_ok=True)
        (job_paths.raw / "a.mkv").write_text("a")
        (job_paths.transient / "b.mkv").write_text("b")
        assert job_has_mkv_files(job_paths) is True

    def test_returns_true_when_previews_has_files(self, job_paths):
        """Preview files (.m3u8, .ts) in previews/ are detected as cleanable."""
        job_paths.previews.mkdir(parents=True, exist_ok=True)
        track_dir = job_paths.previews / "track_01"
        track_dir.mkdir()
        (track_dir / "preview.m3u8").write_text("#EXTM3U")
        (track_dir / "segment_000.ts").write_bytes(b"\x00" * 100)
        assert job_has_mkv_files(job_paths) is True

    def test_returns_true_when_only_previews_exist(self, job_paths):
        """Even without MKV files, preview files make the job cleanable."""
        job_paths.raw.mkdir(parents=True, exist_ok=True)
        job_paths.transient.mkdir(parents=True, exist_ok=True)
        job_paths.previews.mkdir(parents=True, exist_ok=True)
        track_dir = job_paths.previews / "track_01"
        track_dir.mkdir()
        (track_dir / "preview.m3u8").write_text("#EXTM3U")
        assert job_has_mkv_files(job_paths) is True

    def test_returns_false_when_previews_dir_exists_but_empty(self, job_paths):
        """Empty previews/ dir (no files, only subdirs) is not cleanable."""
        job_paths.previews.mkdir(parents=True, exist_ok=True)
        # Just an empty subdir
        (job_paths.previews / "track_01").mkdir()
        assert job_has_mkv_files(job_paths) is False

    def test_returns_false_when_only_metadata_exists(self, job_paths):
        """Files in metadata/ don't count as cleanable."""
        job_paths.metadata.mkdir(parents=True, exist_ok=True)
        (job_paths.metadata / "disc_info.json").write_text("{}")
        assert job_has_mkv_files(job_paths) is False

    def test_cleanable_files_include_previews_false(self, job_paths):
        """job_has_cleanable_files with include_previews=False ignores preview files."""
        job_paths.previews.mkdir(parents=True, exist_ok=True)
        track_dir = job_paths.previews / "track_01"
        track_dir.mkdir()
        (track_dir / "preview.m3u8").write_text("#EXTM3U")
        assert job_has_cleanable_files(job_paths, include_previews=False) is False

    def test_cleanable_files_include_previews_false_still_finds_mkv(self, job_paths):
        """job_has_cleanable_files with include_previews=False still detects MKV files."""
        job_paths.raw.mkdir(parents=True, exist_ok=True)
        (job_paths.raw / "title.mkv").write_text("x")
        assert job_has_cleanable_files(job_paths, include_previews=False) is True


class TestRemoveMkvFilesFromJob:
    """Tests for remove_mkv_files_from_job()."""

    def test_only_mkv_removed(self, job_paths):
        job_paths.raw.mkdir(parents=True, exist_ok=True)
        (job_paths.raw / "keep.txt").write_text("keep")
        (job_paths.raw / "remove.mkv").write_text("data")
        count, manifest_path = remove_mkv_files_from_job(job_paths, reason="test", write_manifest=True)
        assert count == 1
        assert (job_paths.raw / "remove.mkv").exists() is False
        assert (job_paths.raw / "keep.txt").exists() is True

    def test_manifest_content(self, job_paths):
        job_paths.raw.mkdir(parents=True, exist_ok=True)
        (job_paths.raw / "f.mkv").write_text("x")
        count, manifest_path = remove_mkv_files_from_job(job_paths, reason="user_finish", write_manifest=True)
        assert count == 1
        assert manifest_path is not None
        assert manifest_path == job_paths.metadata / MANIFEST_FILENAME
        import json
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert data["reason"] == "user_finish"
        assert "deleted_at" in data
        assert len(data["files"]) == 1
        assert data["files"][0]["name"] == "f.mkv"
        assert data["files"][0]["path"] == "raw/f.mkv" or "f.mkv" in data["files"][0]["path"]
        assert "size_bytes" in data["files"][0]
        assert "mtime_iso" in data["files"][0]

    def test_idempotent_second_run(self, job_paths):
        job_paths.raw.mkdir(parents=True, exist_ok=True)
        (job_paths.raw / "only.mkv").write_text("x")
        c1, p1 = remove_mkv_files_from_job(job_paths, reason="first", write_manifest=True)
        assert c1 == 1
        c2, p2 = remove_mkv_files_from_job(job_paths, reason="second", write_manifest=True)
        assert c2 == 0
        assert job_has_mkv_files(job_paths) is False

    def test_both_raw_and_transient_cleaned(self, job_paths):
        job_paths.raw.mkdir(parents=True, exist_ok=True)
        job_paths.transient.mkdir(parents=True, exist_ok=True)
        (job_paths.raw / "r.mkv").write_text("r")
        (job_paths.transient / "t.mkv").write_text("t")
        count, _ = remove_mkv_files_from_job(job_paths, reason="test", write_manifest=True)
        assert count == 2
        assert (job_paths.raw / "r.mkv").exists() is False
        assert (job_paths.transient / "t.mkv").exists() is False

    def test_write_manifest_false_no_manifest(self, job_paths):
        job_paths.raw.mkdir(parents=True, exist_ok=True)
        (job_paths.raw / "f.mkv").write_text("x")
        count, manifest_path = remove_mkv_files_from_job(job_paths, reason="test", write_manifest=False)
        assert count == 1
        assert manifest_path is None
        assert (job_paths.raw / "f.mkv").exists() is False

    def test_paths_must_be_job_paths(self):
        with pytest.raises(TypeError, match="paths must be JobPaths"):
            remove_mkv_files_from_job(None, reason="test")

    def test_preview_files_removed(self, job_paths):
        """Preview files (.m3u8, .ts) in previews/ are removed."""
        job_paths.previews.mkdir(parents=True, exist_ok=True)
        track_dir = job_paths.previews / "track_01"
        track_dir.mkdir()
        (track_dir / "preview.m3u8").write_text("#EXTM3U")
        (track_dir / "segment_000.ts").write_bytes(b"\x00" * 100)
        (track_dir / "segment_001.ts").write_bytes(b"\x00" * 200)
        count, manifest_path = remove_mkv_files_from_job(job_paths, reason="test", write_manifest=True)
        assert count == 3
        assert not (track_dir / "preview.m3u8").exists()
        assert not (track_dir / "segment_000.ts").exists()
        assert not (track_dir / "segment_001.ts").exists()

    def test_preview_and_mkv_files_removed_together(self, job_paths):
        """Both MKV and preview files are cleaned in a single call."""
        job_paths.raw.mkdir(parents=True, exist_ok=True)
        (job_paths.raw / "title.mkv").write_text("data")
        job_paths.previews.mkdir(parents=True, exist_ok=True)
        track_dir = job_paths.previews / "track_01"
        track_dir.mkdir()
        (track_dir / "preview.m3u8").write_text("#EXTM3U")
        (track_dir / "segment_000.ts").write_bytes(b"\x00" * 50)
        count, _ = remove_mkv_files_from_job(job_paths, reason="test", write_manifest=True)
        assert count == 3  # 1 mkv + 2 preview files
        assert not (job_paths.raw / "title.mkv").exists()
        assert not (track_dir / "preview.m3u8").exists()
        assert not (track_dir / "segment_000.ts").exists()

    def test_manifest_includes_preview_files(self, job_paths):
        """Cleanup manifest records metadata for both MKV and preview files."""
        import json
        job_paths.raw.mkdir(parents=True, exist_ok=True)
        (job_paths.raw / "title.mkv").write_text("data")
        job_paths.previews.mkdir(parents=True, exist_ok=True)
        track_dir = job_paths.previews / "track_01"
        track_dir.mkdir()
        (track_dir / "preview.m3u8").write_text("#EXTM3U")
        count, manifest_path = remove_mkv_files_from_job(job_paths, reason="user_finish", write_manifest=True)
        assert count == 2
        assert manifest_path is not None
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert len(data["files"]) == 2
        file_names = {f["name"] for f in data["files"]}
        assert "title.mkv" in file_names
        assert "preview.m3u8" in file_names
        # All entries have required fields
        for entry in data["files"]:
            assert "path" in entry
            assert "name" in entry
            assert "size_bytes" in entry
            assert "mtime_iso" in entry

    def test_include_previews_false_skips_previews(self, job_paths):
        """When include_previews=False, preview files are left alone."""
        job_paths.raw.mkdir(parents=True, exist_ok=True)
        (job_paths.raw / "title.mkv").write_text("data")
        job_paths.previews.mkdir(parents=True, exist_ok=True)
        track_dir = job_paths.previews / "track_01"
        track_dir.mkdir()
        (track_dir / "preview.m3u8").write_text("#EXTM3U")
        count, _ = remove_mkv_files_from_job(job_paths, reason="test", include_previews=False)
        assert count == 1  # Only the MKV
        assert not (job_paths.raw / "title.mkv").exists()
        assert (track_dir / "preview.m3u8").exists()  # Preview untouched

    def test_empty_preview_subdirs_removed(self, job_paths):
        """After removing preview files, empty subdirectories are cleaned up."""
        job_paths.previews.mkdir(parents=True, exist_ok=True)
        track_dir = job_paths.previews / "track_01"
        track_dir.mkdir()
        (track_dir / "preview.m3u8").write_text("#EXTM3U")
        count, _ = remove_mkv_files_from_job(job_paths, reason="test")
        assert count == 1
        assert not track_dir.exists()  # Empty subdir removed

    def test_multiple_preview_tracks_removed(self, job_paths):
        """Multiple track subdirectories in previews/ are all cleaned."""
        job_paths.previews.mkdir(parents=True, exist_ok=True)
        for track_name in ("track_01", "track_02", "track_03"):
            track_dir = job_paths.previews / track_name
            track_dir.mkdir()
            (track_dir / "preview.m3u8").write_text("#EXTM3U")
            (track_dir / "segment_000.ts").write_bytes(b"\x00" * 50)
        count, _ = remove_mkv_files_from_job(job_paths, reason="test")
        assert count == 6  # 3 tracks × 2 files each
        # All subdirs should be cleaned up
        for track_name in ("track_01", "track_02", "track_03"):
            assert not (job_paths.previews / track_name).exists()


class TestCleanupJobMkvTask:
    """Tests for cleanup_job_mkv Celery task: DB updated, helper called or skipped when no .mkv."""

    @pytest.fixture
    def job_with_dir(self, test_db, tmp_path, monkeypatch):
        """Create a completed job with directory under a temp jobs root."""
        import uuid
        from api import models

        jobs_root = tmp_path / "mkvauto_data"
        jobs_root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr("core.job_paths.resolve_jobs_root", lambda _out_dir=None: jobs_root)

        with test_db() as session:
            disc = models.Disc(
                id=str(uuid.uuid4()),
                content_hash="cleanup_test_disc",
                disc_number=1,
                format="BD",
            )
            session.add(disc)
            session.commit()
            session.refresh(disc)
            job_id = str(uuid.uuid4())
            job_dir = jobs_root / job_id
            job_dir.mkdir(parents=True)
            job = models.Job(
                id=job_id,
                disc_id=disc.id,
                disc_num="1",
                mount_point="/dev/sr0",
                job_status="completed",
                rip_state="completed",
                transfer_source_cleaned=False,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            yield job, session, job_dir

    def test_cleanup_job_mkv_sets_transfer_source_cleaned_when_no_mkv(
        self, job_with_dir,
    ):
        """When job has no .mkv files, task only sets transfer_source_cleaned=True."""
        from workers.tasks import cleanup_job_mkv

        job, session, job_dir = job_with_dir
        job_id = str(job.id)
        session.close()

        cleanup_job_mkv(job_id, "user_finish")

        from api import database
        db = database.SessionLocal()
        try:
            from sqlalchemy import text
            row = db.execute(
                text("SELECT transfer_source_cleaned FROM jobs WHERE id = :id"),
                {"id": job_id},
            ).fetchone()
            assert row is not None
            assert row[0] in (True, 1)  # SQLite may return 1 for boolean
        finally:
            db.close()

    def test_cleanup_job_mkv_removes_mkv_and_sets_cleaned(self, job_with_dir):
        """When job has .mkv files, task removes them and sets transfer_source_cleaned=True."""
        from workers.tasks import cleanup_job_mkv

        job, session, job_dir = job_with_dir
        raw_dir = job_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / "title00.mkv").write_text("data")
        job_id = str(job.id)
        session.close()

        cleanup_job_mkv(job_id, "transfer_cleanup")

        assert (raw_dir / "title00.mkv").exists() is False
        from api import database
        db = database.SessionLocal()
        try:
            from sqlalchemy import text
            row = db.execute(
                text("SELECT transfer_source_cleaned FROM jobs WHERE id = :id"),
                {"id": job_id},
            ).fetchone()
            assert row is not None
            assert row[0] in (True, 1)
        finally:
            db.close()

    def test_cleanup_job_mkv_removes_preview_files_and_sets_cleaned(self, job_with_dir):
        """When job has preview files, task removes them and sets transfer_source_cleaned=True."""
        from workers.tasks import cleanup_job_mkv

        job, session, job_dir = job_with_dir
        previews_dir = job_dir / "previews" / "track_01"
        previews_dir.mkdir(parents=True, exist_ok=True)
        (previews_dir / "preview.m3u8").write_text("#EXTM3U")
        (previews_dir / "segment_000.ts").write_bytes(b"\x00" * 100)
        job_id = str(job.id)
        session.close()

        cleanup_job_mkv(job_id, "user_finish")

        assert not (previews_dir / "preview.m3u8").exists()
        assert not (previews_dir / "segment_000.ts").exists()
        from api import database
        db = database.SessionLocal()
        try:
            from sqlalchemy import text
            row = db.execute(
                text("SELECT transfer_source_cleaned FROM jobs WHERE id = :id"),
                {"id": job_id},
            ).fetchone()
            assert row is not None
            assert row[0] in (True, 1)
        finally:
            db.close()

    def test_cleanup_job_mkv_marks_clean_when_no_job_dir(self, test_db):
        """When job directory doesn't exist, task marks transfer_source_cleaned=True (nothing to clean)."""
        import uuid
        from api import models
        from workers.tasks import cleanup_job_mkv

        with test_db() as session:
            disc = models.Disc(
                id=str(uuid.uuid4()),
                content_hash="no_dir_disc",
                disc_number=1,
                format="BD",
            )
            session.add(disc)
            session.commit()
            session.refresh(disc)
            job = models.Job(
                id=str(uuid.uuid4()),
                disc_id=disc.id,
                disc_num="1",
                mount_point="/dev/sr0",
                job_status="completed",
                rip_state="completed",
                transfer_source_cleaned=False,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            job_id = str(job.id)
        cleanup_job_mkv(job_id, "user_finish")
        from api import database
        db = database.SessionLocal()
        try:
            from sqlalchemy import text
            row = db.execute(
                text("SELECT transfer_source_cleaned FROM jobs WHERE id = :id"),
                {"id": job_id},
            ).fetchone()
            assert row is not None
            assert row[0] in (True, 1)
        finally:
            db.close()


class TestReconcileJobMkvCleanup:
    """Reconciler processes jobs inline (no per-job task enqueue); with .mkv → remove + mark; no .mkv → mark only."""

    @pytest.fixture
    def terminal_jobs_for_reconcile(self, test_db, tmp_path, monkeypatch):
        """Create two terminal jobs: one with .mkv, one without; both transfer_source_cleaned=False."""
        import uuid
        from datetime import datetime, timezone, timedelta
        from api import models

        jobs_root = tmp_path / "mkvauto_data"
        jobs_root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr("core.job_paths.resolve_jobs_root", lambda _out_dir=None: jobs_root)

        with test_db() as session:
            disc = models.Disc(
                id=str(uuid.uuid4()),
                content_hash="reconcile_disc",
                disc_number=1,
                format="BD",
            )
            session.add(disc)
            session.commit()
            session.refresh(disc)
            # Job 1: has directory and .mkv (path must end with job.id for JobPaths)
            job_id1 = str(uuid.uuid4())
            job_dir1 = tmp_path / "mkvauto_data" / job_id1
            job_dir1.mkdir(parents=True)
            (job_dir1 / "raw").mkdir(parents=True, exist_ok=True)
            (job_dir1 / "raw" / "a.mkv").write_text("a")
            job1 = models.Job(
                id=job_id1,
                disc_id=disc.id,
                disc_num="1",
                mount_point="/dev/sr0",
                job_status="completed",
                rip_state="completed",
                transfer_source_cleaned=False,
                updated_at=datetime.now(timezone.utc) - timedelta(hours=2),
            )
            session.add(job1)
            # Job 2: has directory, no .mkv
            job_id2 = str(uuid.uuid4())
            job_dir2 = tmp_path / "mkvauto_data" / job_id2
            job_dir2.mkdir(parents=True)
            (job_dir2 / "raw").mkdir(parents=True, exist_ok=True)
            # Failed AND transferred: still safely cleanable (marks-only —
            # no .mkv present). A failed job with transfer pending would be
            # SKIPPED now: its raw/ holds the only copy of the rip.
            job2 = models.Job(
                id=job_id2,
                disc_id=disc.id,
                disc_num="1",
                mount_point="/dev/sr0",
                job_status="failed",
                rip_state="completed",
                transfer_state="completed",
                transfer_source_cleaned=False,
                updated_at=datetime.now(timezone.utc) - timedelta(hours=2),
            )
            session.add(job2)
            session.commit()
            session.refresh(job1)
            session.refresh(job2)
            yield job1, job2, session, job_dir1, job_dir2

    def test_reconcile_processes_inline_removes_mkv_and_marks_both(
        self, terminal_jobs_for_reconcile,
    ):
        """Reconcile runs removal for job with .mkv and only marks the other; both end with transfer_source_cleaned=True."""
        from workers.tasks import reconcile_job_mkv_cleanup

        job1, job2, session, job_dir1, job_dir2 = terminal_jobs_for_reconcile
        session.close()

        result = reconcile_job_mkv_cleanup()

        assert result["processed"] == 2
        assert (job_dir1 / "raw" / "a.mkv").exists() is False
        from api import database
        db = database.SessionLocal()
        try:
            from sqlalchemy import text
            for job_id in (str(job1.id), str(job2.id)):
                row = db.execute(
                    text("SELECT transfer_source_cleaned FROM jobs WHERE id = :id"),
                    {"id": job_id},
                ).fetchone()
                assert row is not None, f"job {job_id}"
                assert row[0] in (True, 1)
        finally:
            db.close()


class TestStartupCleanupTerminalJobs:
    """Tests for _startup_cleanup_terminal_jobs: enqueues cleanup for uncleaned terminal jobs on startup."""

    @pytest.fixture
    def setup_jobs_for_startup(self, test_db, tmp_path):
        """Create a mix of jobs: terminal uncleaned, terminal cleaned, and running."""
        import uuid
        from api import models

        with test_db() as session:
            disc = models.Disc(
                id=str(uuid.uuid4()),
                content_hash="startup_cleanup_disc",
                disc_number=1,
                format="BD",
            )
            session.add(disc)
            session.commit()
            session.refresh(disc)

            # Job 1: completed, uncleaned → should get cleanup enqueued
            job_id1 = str(uuid.uuid4())
            job_dir1 = tmp_path / "mkvauto_data" / job_id1
            job_dir1.mkdir(parents=True)
            job1 = models.Job(
                id=job_id1,
                disc_id=disc.id,
                disc_num="1",
                mount_point="/dev/sr0",
                job_status="completed",
                rip_state="completed",
                transfer_source_cleaned=False,
            )
            session.add(job1)

            # Job 2: failed, transfer never ran → must be SKIPPED (raw/
            # holds the only copy of the rip; cleaning it at boot destroyed
            # a 48GB UHD rip on prod)
            job_id2 = str(uuid.uuid4())
            job_dir2 = tmp_path / "mkvauto_data" / job_id2
            job_dir2.mkdir(parents=True)
            job2 = models.Job(
                id=job_id2,
                disc_id=disc.id,
                disc_num="1",
                mount_point="/dev/sr0",
                job_status="failed",
                rip_state="failed",
                transfer_source_cleaned=False,
            )
            session.add(job2)

            # Job 3: completed, already cleaned → should NOT get cleanup
            job_id3 = str(uuid.uuid4())
            job_dir3 = tmp_path / "mkvauto_data" / job_id3
            job_dir3.mkdir(parents=True)
            job3 = models.Job(
                id=job_id3,
                disc_id=disc.id,
                disc_num="1",
                mount_point="/dev/sr0",
                job_status="completed",
                rip_state="completed",
                transfer_source_cleaned=True,
            )
            session.add(job3)

            # Job 4: running → should NOT get cleanup
            job_id4 = str(uuid.uuid4())
            job_dir4 = tmp_path / "mkvauto_data" / job_id4
            job_dir4.mkdir(parents=True)
            job4 = models.Job(
                id=job_id4,
                disc_id=disc.id,
                disc_num="1",
                mount_point="/dev/sr0",
                job_status="running",
                rip_state="running",
                transfer_source_cleaned=False,
            )
            session.add(job4)

            session.commit()
            for j in (job1, job2, job3, job4):
                session.refresh(j)
            yield {
                "should_clean": [str(job1.id)],
                "should_skip": [str(job2.id), str(job3.id), str(job4.id)],
            }, session

    def test_startup_cleanup_enqueues_for_uncleaned_terminal_jobs(
        self, setup_jobs_for_startup, monkeypatch,
    ):
        """Startup cleanup enqueues cleanup_job_mkv only for jobs whose
        source is safe to remove — completed, or failed WITH a completed
        transfer. A failed job that never transferred keeps its rip."""
        job_info, session = setup_jobs_for_startup
        session.close()

        enqueued = []

        def mock_delay(job_id, reason):
            enqueued.append((job_id, reason))

        # Patch cleanup_job_mkv.delay
        import workers.tasks as tasks_module
        monkeypatch.setattr(tasks_module.cleanup_job_mkv, "delay", mock_delay)

        from api.main import _startup_cleanup_terminal_jobs
        _startup_cleanup_terminal_jobs()

        enqueued_ids = {job_id for job_id, _ in enqueued}
        # Should have enqueued cleanup for both uncleaned terminal jobs
        for job_id in job_info["should_clean"]:
            assert job_id in enqueued_ids, f"Expected cleanup for {job_id}"
        # Should NOT have enqueued cleanup for already-cleaned or running jobs
        for job_id in job_info["should_skip"]:
            assert job_id not in enqueued_ids, f"Did not expect cleanup for {job_id}"
        # All enqueued with reason "startup_cleanup"
        for _, reason in enqueued:
            assert reason == "startup_cleanup"


class TestRecoverInflightJobsValidationGuard:
    """Tests that _recover_inflight_jobs does NOT spawn validation threads for
    running jobs (race condition fix), but DOES for validating jobs."""

    @pytest.fixture
    def setup_jobs_for_recovery(self, test_db, tmp_path, monkeypatch):
        """Create jobs in various states to test startup recovery validation guard."""
        import uuid
        from api import models

        jobs_root = tmp_path / "mkvauto_data"
        jobs_root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr("core.job_paths.resolve_jobs_root", lambda _out_dir=None: jobs_root)

        with test_db() as session:
            disc = models.Disc(
                id=str(uuid.uuid4()),
                content_hash="recovery_guard_disc",
                disc_number=1,
                format="BD",
            )
            session.add(disc)
            session.commit()
            session.refresh(disc)

            # Job 1: running, rip completed, no post_paths — should NOT be validated
            # (this is the race condition scenario: label done, waiting for postprocess)
            job_id1 = str(uuid.uuid4())
            job_dir1 = tmp_path / "mkvauto_data" / job_id1
            job_dir1.mkdir(parents=True)
            raw1 = job_dir1 / "raw"
            raw1.mkdir()
            (raw1 / "title_t00.mkv").write_text("data")
            job1 = models.Job(
                id=job_id1,
                disc_id=disc.id,
                disc_num="1",
                mount_point="/dev/sr0",
                job_status="running",
                rip_state="completed",
                rip_progress=100,
                ripped_files={"tid1": "title_t00.mkv"},
            )
            session.add(job1)

            # Job 2: validating, rip completed — SHOULD be validated
            # (this is a job stuck from a previous interrupted startup validation)
            job_id2 = str(uuid.uuid4())
            job_dir2 = tmp_path / "mkvauto_data" / job_id2
            job_dir2.mkdir(parents=True)
            raw2 = job_dir2 / "raw"
            raw2.mkdir()
            (raw2 / "title_t00.mkv").write_text("data")
            job2 = models.Job(
                id=job_id2,
                disc_id=disc.id,
                disc_num="1",
                mount_point="/dev/sr0",
                job_status="validating",
                rip_state="completed",
                rip_progress=100,
                ripped_files={"tid1": "title_t00.mkv"},
            )
            session.add(job2)

            session.commit()
            session.refresh(job1)
            session.refresh(job2)
            yield {
                "should_skip_validation": str(job1.id),
                "should_validate": str(job2.id),
            }, session

    def test_running_job_not_validated_on_startup(
        self, setup_jobs_for_recovery, monkeypatch,
    ):
        """Running jobs with completed rip are NOT validated on startup (prevents race with postprocess)."""
        job_info, session = setup_jobs_for_recovery
        session.close()

        validated_jobs = []

        # Patch threading.Thread to capture what gets spawned
        import threading
        original_thread_init = threading.Thread.__init__

        def tracking_thread_init(self_thread, *args, **kwargs):
            original_thread_init(self_thread, *args, **kwargs)
            if kwargs.get("target") and "validate" in str(kwargs.get("target", "")):
                # Extract job_id from args
                thread_args = kwargs.get("args", args[1] if len(args) > 1 else ())
                if thread_args:
                    validated_jobs.append(thread_args[0])  # job_id_str is first arg

        monkeypatch.setattr(threading.Thread, "__init__", tracking_thread_init)

        # Also patch Thread.start to be a no-op (don't actually run validation)
        monkeypatch.setattr(threading.Thread, "start", lambda self: None)

        # Patch _fail_orphaned_rip_jobs_on_startup to be a no-op
        monkeypatch.setattr(
            "api.routers.jobs._fail_orphaned_rip_jobs_on_startup",
            lambda db: [],
        )

        from api.main import _recover_inflight_jobs
        _recover_inflight_jobs()

        # Running job should NOT have been validated
        assert job_info["should_skip_validation"] not in validated_jobs, \
            "Running job should not be validated on startup (race condition with postprocess)"

        # Validating job SHOULD have been validated
        assert job_info["should_validate"] in validated_jobs, \
            "Validating job (stuck from previous crash) should be validated on startup"
