"""
Phase 2 § 6.1 auto-dispatch coverage (#365).

When the post-process prep phase completes for a job whose active
TransferConfig is a remote mode (rsync / SMB / NFS), the worker should
automatically enqueue ``transfer_remote`` instead of waiting for a
manual "Start Transfer" click. Local mode is intentionally skipped
because the POST /jobs/{id}/transfer endpoint accepts a ``target_dir``
override that the operator may want to pick at click time.

These tests pin the dispatch contract:

  * remote-mode active config → ``transfer_remote.delay(job_id, src_root, config_id)``
  * local-mode active config → no dispatch (user trigger preserved)
  * no active config → no dispatch (notification path handles UX elsewhere)
  * missing src_root → no dispatch (logged + manual trigger still available)
  * exceptions in the helper are swallowed so the prep success isn't undone

The helper sits at the end of ``_run_prep_phase`` (workers/tasks.py),
right after ``_post_postprocess_complete_callback`` succeeds.
"""
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api import database, models
from workers import tasks


@pytest.fixture
def job_in_db(test_db, tmp_path, monkeypatch):
    """Disc + Job + JobPaths layout on disk. Returns (job_id, src_root)."""
    monkeypatch.setattr(tasks, "DATA_ROOT", tmp_path / "data")
    # JobPaths.for_id calls resolve_jobs_root(None) which reads
    # MKVAUTO_JOBS_DIR or falls back to ~/MakeMKV-Auto/jobs. Pin to tmp.
    jobs_root = tmp_path / "data" / "jobs"
    monkeypatch.setattr("core.job_paths.resolve_jobs_root", lambda _out=None: jobs_root)
    session = test_db()
    try:
        disc_id = str(uuid.uuid4())
        session.add(models.Disc(id=disc_id, content_hash=f"h-{uuid.uuid4().hex[:8]}"))
        job_id = str(uuid.uuid4())
        session.add(models.Job(
            id=job_id, disc_id=disc_id,
            disc_num="1", mount_point="/mnt/sr0",
            rip_state="completed",
            phase="transfer",
            transfer_state="ready",
        ))
        session.commit()
    finally:
        session.close()

    # Create the transient/ directory so _resolve_transfer_src_root finds it.
    src_root = jobs_root / job_id / "transient"
    src_root.mkdir(parents=True)
    (src_root / "dummy.mkv").write_bytes(b"x")
    return job_id, src_root


def _patch_session(monkeypatch, session_factory):
    """Replace database.SessionLocal so the helper opens our test session."""
    monkeypatch.setattr(database, "SessionLocal", session_factory)


def _make_config(mode: str, *, transfer_dir: str | None = None):
    """Lightweight TransferConfig stand-in for monkeypatching get_active_config."""
    cfg = MagicMock()
    cfg.id = uuid.uuid4()
    cfg.mode = mode
    cfg.transfer_dir = transfer_dir
    return cfg


def test_auto_dispatch_for_rsync_enqueues_transfer_remote(
    job_in_db, test_db, monkeypatch
):
    """rsync is a remote mode → transfer_remote.delay is called with
    (job_id, str(src_root), str(config.id))."""
    job_id, src_root = job_in_db
    _patch_session(monkeypatch, test_db)
    cfg = _make_config("rsync")
    monkeypatch.setattr(
        "core.transfer.service.get_active_config", lambda _db: cfg
    )

    delay_calls = []

    def fake_delay(*args):
        delay_calls.append(args)
        result = MagicMock()
        result.id = f"task-{uuid.uuid4().hex[:8]}"
        return result

    monkeypatch.setattr(tasks.transfer_remote, "delay", fake_delay)

    task_self = MagicMock()
    tasks._maybe_auto_dispatch_remote_transfer(job_id, task_self)

    assert len(delay_calls) == 1
    delivered_job_id, delivered_src, delivered_cfg_id = delay_calls[0]
    assert delivered_job_id == job_id
    assert delivered_src == str(src_root.resolve())
    assert delivered_cfg_id == str(cfg.id)


@pytest.mark.parametrize("mode", ["smb", "nfs"])
def test_auto_dispatch_for_smb_and_nfs_also_dispatch(
    mode, job_in_db, test_db, monkeypatch
):
    """SMB + NFS are remote modes — same auto-dispatch contract as rsync."""
    job_id, _ = job_in_db
    _patch_session(monkeypatch, test_db)
    monkeypatch.setattr(
        "core.transfer.service.get_active_config",
        lambda _db: _make_config(mode),
    )

    delay_calls = []
    monkeypatch.setattr(
        tasks.transfer_remote, "delay",
        lambda *args: delay_calls.append(args) or MagicMock(id="t"),
    )

    tasks._maybe_auto_dispatch_remote_transfer(job_id, MagicMock())
    assert len(delay_calls) == 1


def test_local_mode_does_not_auto_dispatch(job_in_db, test_db, monkeypatch):
    """Local mode preserves the user's chance to pick target_dir at click
    time — the helper must NOT auto-dispatch for local configs."""
    job_id, _ = job_in_db
    _patch_session(monkeypatch, test_db)
    monkeypatch.setattr(
        "core.transfer.service.get_active_config",
        lambda _db: _make_config("local", transfer_dir="/tmp/lib"),
    )

    delay_calls = []
    monkeypatch.setattr(
        tasks.transfer_remote, "delay",
        lambda *args: delay_calls.append(args) or MagicMock(id="t"),
    )

    tasks._maybe_auto_dispatch_remote_transfer(job_id, MagicMock())
    assert delay_calls == []


def test_no_active_config_does_not_auto_dispatch(job_in_db, test_db, monkeypatch):
    """If the user hasn't set up any TransferConfig, dispatch silently
    no-ops. UX prompt is handled by the existing notification path in
    transfer_job — not this helper."""
    job_id, _ = job_in_db
    _patch_session(monkeypatch, test_db)
    monkeypatch.setattr(
        "core.transfer.service.get_active_config", lambda _db: None
    )

    delay_calls = []
    monkeypatch.setattr(
        tasks.transfer_remote, "delay",
        lambda *args: delay_calls.append(args) or MagicMock(id="t"),
    )

    tasks._maybe_auto_dispatch_remote_transfer(job_id, MagicMock())
    assert delay_calls == []


def test_missing_src_root_does_not_dispatch(test_db, tmp_path, monkeypatch):
    """If src_root doesn't exist on disk (no transient/ dir), the helper
    must not dispatch — transfer_remote would just fail and the user
    can still trigger the transfer manually after fixing the layout."""
    monkeypatch.setattr(tasks, "DATA_ROOT", tmp_path / "data")
    session = test_db()
    try:
        disc_id = str(uuid.uuid4())
        session.add(models.Disc(id=disc_id, content_hash=f"h-{uuid.uuid4().hex[:8]}"))
        job_id = str(uuid.uuid4())
        session.add(models.Job(
            id=job_id, disc_id=disc_id, disc_num="1", mount_point="/mnt/sr0",
            rip_state="completed", transfer_state="ready",
        ))
        session.commit()
    finally:
        session.close()
    # Deliberately do NOT create the transient dir.

    _patch_session(monkeypatch, test_db)
    monkeypatch.setattr(
        "core.transfer.service.get_active_config",
        lambda _db: _make_config("rsync"),
    )

    delay_calls = []
    monkeypatch.setattr(
        tasks.transfer_remote, "delay",
        lambda *args: delay_calls.append(args) or MagicMock(id="t"),
    )

    tasks._maybe_auto_dispatch_remote_transfer(job_id, MagicMock())
    assert delay_calls == []


def test_helper_swallows_exceptions(job_in_db, test_db, monkeypatch):
    """The prep work succeeded by the time this helper runs. Any failure
    here (e.g. transient DB error, broken config lookup) must NOT
    propagate — the user can always trigger the transfer manually."""
    job_id, _ = job_in_db
    _patch_session(monkeypatch, test_db)

    def boom(_db):
        raise RuntimeError("synthetic config lookup failure")

    monkeypatch.setattr("core.transfer.service.get_active_config", boom)

    delay_calls = []
    monkeypatch.setattr(
        tasks.transfer_remote, "delay",
        lambda *args: delay_calls.append(args) or MagicMock(id="t"),
    )

    # Must not raise.
    tasks._maybe_auto_dispatch_remote_transfer(job_id, MagicMock())
    assert delay_calls == []


def test_missing_job_does_not_dispatch(test_db, monkeypatch):
    """If the job row disappeared between prep complete and this helper
    (extremely rare; cleanup race), no dispatch."""
    _patch_session(monkeypatch, test_db)
    monkeypatch.setattr(
        "core.transfer.service.get_active_config",
        lambda _db: _make_config("rsync"),
    )

    delay_calls = []
    monkeypatch.setattr(
        tasks.transfer_remote, "delay",
        lambda *args: delay_calls.append(args) or MagicMock(id="t"),
    )

    tasks._maybe_auto_dispatch_remote_transfer(str(uuid.uuid4()), MagicMock())
    assert delay_calls == []
