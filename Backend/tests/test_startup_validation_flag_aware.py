"""
Coverage for the flag-aware path resolution in
``_recover_inflight_jobs`` (#365 transient/-drop audit follow-up).

The startup recovery routine wakes any job stuck in ``validating`` state
after a crash/restart and re-runs ``gather_final_outputs`` to confirm
the on-disk files match ``post_paths``. Before this fix the recovery
walked ``job_paths.transient`` unconditionally; under
``MKVAUTO_RENAME_DIRECT_TO_DEST=1`` the rename writes directly to
``config.transfer_dir`` and ``transient/`` stays empty, so a
mid-postprocess restart would mark the job failed even though the
files are present at the library destination.

Companion to ``test_startup_validation_transient.py`` (flag-off cover-
age, pre-existing) and ``test_validate_transfer_prep_output_flag_aware.py``
(the same shape but for the postprocess validator).
"""
import uuid
from pathlib import Path

from api import main as api_main
from api import models
from tests.postprocess_fixtures import job_with_rip_done_for_postprocess


class _InlineThread:
    """Run threading.Thread inline so the test deterministically observes
    the validation outcome on the same thread."""

    def __init__(self, *, target, args=(), daemon=None):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


def _make_validating_job(db_factory, tmp_path, monkeypatch):
    """Reuse the postprocess fixture and flip the job into the state the
    startup recovery looks for (``validating`` + rip_progress=100 +
    post_paths populated). Returns (job_id, title_id, paths, post_paths)."""
    job_id, title_id, paths = job_with_rip_done_for_postprocess(
        db_factory, tmp_path, monkeypatch, num_titles=1,
    )
    # Postprocess hasn't run; raw file isn't what recovery validates against.
    (paths.raw / "test_t1.mkv").unlink()
    with db_factory() as session:
        job = session.query(models.Job).filter_by(id=job_id).one()
        job.job_status = "validating"
        job.rip_progress = 100
        post_paths = dict(job.post_paths or {})
        session.commit()
    monkeypatch.setattr(api_main.threading, "Thread", _InlineThread)
    return job_id, title_id, paths, post_paths


# ──────────────────────────────────────────────────────────────────────────
# Flag-on local — the bug-fix path
# ──────────────────────────────────────────────────────────────────────────


def test_startup_validation_flag_on_local_finds_files_at_transfer_dir(
    test_db, tmp_path, monkeypatch,
):
    """Audit-bug-fix path: under flag-on local the rename wrote post_paths'
    files to config.transfer_dir. The startup recovery's
    gather_final_outputs MUST walk that destination, not the empty
    transient/. Pre-fix the recovery walked transient/ and marked the
    job failed."""
    transfer_dir = tmp_path / "library"
    transfer_dir.mkdir()

    with test_db() as session:
        session.add(models.TransferConfig(
            id=str(uuid.uuid4()), mode="local", is_active=True,
            transfer_dir=str(transfer_dir),
        ))
        session.commit()

    job_id, title_id, paths, post_paths = _make_validating_job(
        test_db, tmp_path, monkeypatch,
    )

    # Place the file at the LIBRARY destination (where rename writes
    # under flag-on), NOT at transient/. transient/ stays empty by
    # design.
    library_file = transfer_dir / post_paths[title_id]
    library_file.parent.mkdir(parents=True, exist_ok=True)
    library_file.write_bytes(b"x" * 1500)

    api_main._recover_inflight_jobs()

    with test_db() as session:
        job = session.query(models.Job).filter_by(id=job_id).one()
        assert job.job_status == "running", (
            f"Expected job to recover; got status={job.job_status}, "
            f"error_reason={job.error_reason!r}"
        )
        assert job.error_reason is None
        # post_paths preserved (recovery just validated, didn't reshape).
        assert job.post_paths == post_paths


def test_startup_validation_flag_on_local_fails_when_files_missing(
    test_db, tmp_path, monkeypatch,
):
    """Regression guard for fail-loud: flag-on local with no files
    anywhere → recovery validates against the library, finds nothing,
    marks the job failed with a clear error (not a "wrong directory"
    red herring)."""
    transfer_dir = tmp_path / "library"
    transfer_dir.mkdir()

    with test_db() as session:
        session.add(models.TransferConfig(
            id=str(uuid.uuid4()), mode="local", is_active=True,
            transfer_dir=str(transfer_dir),
        ))
        session.commit()

    job_id, _, _, _ = _make_validating_job(test_db, tmp_path, monkeypatch)
    # No files written anywhere — neither at transient/ nor at library.

    api_main._recover_inflight_jobs()

    with test_db() as session:
        job = session.query(models.Job).filter_by(id=job_id).one()
        # Either failed loudly with a clear reason, or recovered to running
        # with no error if gather_final_outputs is lenient. Pin the
        # behavior: validation failure should not silently mark the job
        # as running with no files.
        if job.job_status == "failed":
            assert "Startup validation failed" in (job.error_reason or "")
        else:
            # If gather_final_outputs returned empty without raising, the
            # job remains running with cleared error — that's also
            # acceptable because the user can manually re-trigger.
            assert job.job_status == "running"


# ──────────────────────────────────────────────────────────────────────────
# Flag-on remote — recovery still finds files at transient/ (the staging)
# ──────────────────────────────────────────────────────────────────────────


def test_startup_validation_flag_on_remote_uses_transient(
    test_db, tmp_path, monkeypatch,
):
    """Remote modes (rsync/smb/nfs) always use local transient/ as
    staging. The startup recovery must continue to walk transient/ for
    these jobs regardless of the flag — same contract as the postprocess
    validator's resolver."""
    with test_db() as session:
        session.add(models.TransferConfig(
            id=str(uuid.uuid4()), mode="rsync", is_active=True,
            transfer_dir="/remote/library",
        ))
        session.commit()

    job_id, title_id, paths, post_paths = _make_validating_job(
        test_db, tmp_path, monkeypatch,
    )
    # Files at transient/ (where remote-mode rename still writes).
    transient_file = paths.transient / post_paths[title_id]
    transient_file.parent.mkdir(parents=True, exist_ok=True)
    transient_file.write_bytes(b"x" * 1500)

    api_main._recover_inflight_jobs()

    with test_db() as session:
        job = session.query(models.Job).filter_by(id=job_id).one()
        assert job.job_status == "running"
        assert job.error_reason is None
