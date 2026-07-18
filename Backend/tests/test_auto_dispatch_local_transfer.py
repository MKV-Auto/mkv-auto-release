"""
Phase 2 § 6.1 finisher — local-mode auto-dispatch coverage (#365).

Companion to ``test_auto_dispatch_remote_transfer.py``. When the prep
phase completes for a job whose active TransferConfig is local AND the
job has a per-file mapping (``post_paths`` / ``ripped_files`` /
``output_files``), the worker runs the actual file copy inline via
``_execute_local_transfer_use_final_map`` — same body the
``POST /jobs/{id}/transfer`` endpoint runs.

This closes the last gap in the collapsed pipeline: before this helper
local-mode operators had to click "Start Transfer" twice (once for prep,
once to start the actual copy). Remote modes already collapsed to one
click in PR #473.

These tests pin the dispatch contract:

  * local-mode active config + use_final_map → helper invoked
  * remote modes (rsync / SMB / NFS) → no-op (handled elsewhere)
  * no active config → silent no-op
  * missing src_root → log + no-op
  * job with no per-file mapping → no-op (regular branch stays endpoint-driven)
  * HTTPException from the helper → ``_fail_transfer`` called

The helper sits at the end of ``_run_prep_phase`` (workers/tasks.py),
right after ``_maybe_auto_dispatch_remote_transfer``.
"""
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from api import database, models
from workers import tasks


@pytest.fixture
def job_in_db(test_db, tmp_path, monkeypatch):
    """Disc + Job + JobPaths layout on disk. Returns (job_id, src_root)."""
    monkeypatch.setattr(tasks, "DATA_ROOT", tmp_path / "data")
    jobs_root = tmp_path / "data" / "jobs"
    monkeypatch.setattr("core.job_paths.resolve_jobs_root", lambda _out=None: jobs_root)
    session = test_db()
    try:
        disc_id = str(uuid.uuid4())
        session.add(models.Disc(id=disc_id, content_hash=f"h-{uuid.uuid4().hex[:8]}"))
        tid = str(uuid.uuid4())
        session.add(models.DiscTitle(
            id=tid, disc_id=disc_id, title="T", source_file="00000.mpls",
        ))
        job_id = str(uuid.uuid4())
        session.add(models.Job(
            id=job_id, disc_id=disc_id,
            disc_num="1", mount_point="/mnt/sr0",
            rip_state="completed",
            phase="transfer",
            transfer_state="ready",
            post_paths={tid: "Movies/Film (2024)/Film.1080p.mkv"},
        ))
        session.commit()
    finally:
        session.close()

    # transient/ dir + a dummy MKV so the use_final_map helper has something
    # to copy. _execute_local_transfer_use_final_map walks src_root and
    # copies each post_paths entry that exists on disk.
    src_root = jobs_root / job_id / "transient"
    (src_root / "Movies" / "Film (2024)").mkdir(parents=True)
    (src_root / "Movies" / "Film (2024)" / "Film.1080p.mkv").write_bytes(b"\x00" * 64)
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


# ────────────────────────────────────────────────────────────────────────
# Happy path
# ────────────────────────────────────────────────────────────────────────


def test_auto_dispatch_local_calls_use_final_map_helper(
    job_in_db, test_db, tmp_path, monkeypatch
):
    """Local-mode active config + job with post_paths → the
    use_final_map helper is invoked with the right (job, config) pair.
    We assert by capturing the call rather than letting the helper
    actually run (its own coverage lives in
    test_execute_local_transfer_use_final_map.py).

    The src==dest shortcut is monkeypatched to return False so the test
    exercises the use_final_map path. The shortcut's own
    src_root==dest_root activation case is covered separately."""
    job_id, _src_root = job_in_db
    _patch_session(monkeypatch, test_db)

    dest = tmp_path / "library"
    dest.mkdir()
    monkeypatch.setattr(
        "core.transfer.service.get_active_config",
        lambda _db: _make_config("local", transfer_dir=str(dest)),
    )
    monkeypatch.setattr(
        "api.routers.jobs._try_src_equals_dest_shortcut",
        lambda *a, **k: False,
    )

    helper_calls = []

    def fake_helper(db, job, src, config, output_files, job_metadata, **kw):
        helper_calls.append({
            "job_id": str(job.id),
            "src_root": str(src),
            "config_id": str(config.id),
            "output_files": output_files,
            "has_callbacks": (
                "transfer_progress_callback" in kw
                and "hash_progress_callback" in kw
            ),
        })
        return [str(dest / "Movies")]

    monkeypatch.setattr(
        "api.routers.jobs._execute_local_transfer_use_final_map", fake_helper
    )

    tasks._maybe_auto_dispatch_local_transfer(job_id, MagicMock())

    assert len(helper_calls) == 1
    call = helper_calls[0]
    assert call["job_id"] == job_id
    assert call["output_files"] is None  # job has no disc_payload.output_files
    assert call["has_callbacks"] is True


def test_auto_dispatch_local_src_equals_dest_shortcut_skips_helper(
    job_in_db, test_db, tmp_path, monkeypatch
):
    """Under MKVAUTO_RENAME_DIRECT_TO_DEST + local mode, rename already
    wrote to config.transfer_dir, so the src==dest shortcut applies and
    the use_final_map helper must NOT be called (it would SameFileError
    on ``shutil.copy2(src, src)``)."""
    job_id, _ = job_in_db
    _patch_session(monkeypatch, test_db)
    library = tmp_path / "library"
    library.mkdir(exist_ok=True)
    monkeypatch.setattr(
        "core.transfer.service.get_active_config",
        lambda _db: _make_config("local", transfer_dir=str(library)),
    )

    shortcut_calls = []
    monkeypatch.setattr(
        "api.routers.jobs._try_src_equals_dest_shortcut",
        lambda *a, **k: (shortcut_calls.append(a), True)[1],
    )
    helper_calls = []
    monkeypatch.setattr(
        "api.routers.jobs._execute_local_transfer_use_final_map",
        lambda *a, **k: helper_calls.append((a, k)),
    )

    tasks._maybe_auto_dispatch_local_transfer(job_id, MagicMock())

    assert len(shortcut_calls) == 1
    assert helper_calls == []  # shortcut handled it; copy skipped


@pytest.mark.parametrize("mode", ["rsync", "smb", "nfs"])
def test_auto_dispatch_local_skipped_for_remote_modes(
    mode, job_in_db, test_db, monkeypatch
):
    """The local helper no-ops for remote modes — those are covered by
    _maybe_auto_dispatch_remote_transfer."""
    job_id, _ = job_in_db
    _patch_session(monkeypatch, test_db)
    monkeypatch.setattr(
        "core.transfer.service.get_active_config",
        lambda _db: _make_config(mode),
    )

    helper_calls = []
    monkeypatch.setattr(
        "api.routers.jobs._execute_local_transfer_use_final_map",
        lambda *a, **k: helper_calls.append((a, k)),
    )

    tasks._maybe_auto_dispatch_local_transfer(job_id, MagicMock())
    assert helper_calls == []


# ────────────────────────────────────────────────────────────────────────
# Silent no-op paths
# ────────────────────────────────────────────────────────────────────────


def test_auto_dispatch_local_no_active_config_silent_noop(
    job_in_db, test_db, monkeypatch
):
    """No active TransferConfig → silent no-op. The existing notification
    path on the POST endpoint handles the UX prompt when the user does
    click."""
    job_id, _ = job_in_db
    _patch_session(monkeypatch, test_db)
    monkeypatch.setattr(
        "core.transfer.service.get_active_config", lambda _db: None
    )

    helper_calls = []
    monkeypatch.setattr(
        "api.routers.jobs._execute_local_transfer_use_final_map",
        lambda *a, **k: helper_calls.append((a, k)),
    )

    tasks._maybe_auto_dispatch_local_transfer(job_id, MagicMock())
    assert helper_calls == []


def test_auto_dispatch_local_missing_src_root_silent_noop(
    test_db, tmp_path, monkeypatch
):
    """src_root doesn't exist on disk → log + skip. Manual trigger via
    POST /jobs/{id}/transfer still available."""
    monkeypatch.setattr(tasks, "DATA_ROOT", tmp_path / "data")
    jobs_root = tmp_path / "data" / "jobs"
    monkeypatch.setattr("core.job_paths.resolve_jobs_root", lambda _out=None: jobs_root)
    session = test_db()
    try:
        disc_id = str(uuid.uuid4())
        session.add(models.Disc(id=disc_id, content_hash=f"h-{uuid.uuid4().hex[:8]}"))
        job_id = str(uuid.uuid4())
        session.add(models.Job(
            id=job_id, disc_id=disc_id, disc_num="1", mount_point="/mnt/sr0",
            rip_state="completed", transfer_state="ready",
            post_paths={"t": "Movies/X.mkv"},  # has mapping; src just missing
        ))
        session.commit()
    finally:
        session.close()
    # Deliberately do NOT create transient/ dir.

    _patch_session(monkeypatch, test_db)
    monkeypatch.setattr(
        "core.transfer.service.get_active_config",
        lambda _db: _make_config("local", transfer_dir=str(tmp_path / "library")),
    )

    helper_calls = []
    monkeypatch.setattr(
        "api.routers.jobs._execute_local_transfer_use_final_map",
        lambda *a, **k: helper_calls.append((a, k)),
    )

    tasks._maybe_auto_dispatch_local_transfer(job_id, MagicMock())
    assert helper_calls == []


def test_auto_dispatch_local_no_post_paths_skips_use_final_map(
    test_db, tmp_path, monkeypatch
):
    """Job without post_paths / ripped_files / output_files → the
    use_final_map branch doesn't apply; helper no-ops and the operator
    can manually trigger the regular (non-final-map) branch via the
    endpoint. This is the 'out of scope' carve-out from the plan."""
    monkeypatch.setattr(tasks, "DATA_ROOT", tmp_path / "data")
    jobs_root = tmp_path / "data" / "jobs"
    monkeypatch.setattr("core.job_paths.resolve_jobs_root", lambda _out=None: jobs_root)
    session = test_db()
    try:
        disc_id = str(uuid.uuid4())
        session.add(models.Disc(id=disc_id, content_hash=f"h-{uuid.uuid4().hex[:8]}"))
        job_id = str(uuid.uuid4())
        session.add(models.Job(
            id=job_id, disc_id=disc_id, disc_num="1", mount_point="/mnt/sr0",
            rip_state="completed", transfer_state="ready",
            # No post_paths, no ripped_files, no disc_payload.output_files.
        ))
        session.commit()
    finally:
        session.close()

    src_root = jobs_root / job_id / "transient"
    src_root.mkdir(parents=True)

    _patch_session(monkeypatch, test_db)
    monkeypatch.setattr(
        "core.transfer.service.get_active_config",
        lambda _db: _make_config("local", transfer_dir=str(tmp_path / "library")),
    )

    helper_calls = []
    monkeypatch.setattr(
        "api.routers.jobs._execute_local_transfer_use_final_map",
        lambda *a, **k: helper_calls.append((a, k)),
    )

    tasks._maybe_auto_dispatch_local_transfer(job_id, MagicMock())
    assert helper_calls == []


# ────────────────────────────────────────────────────────────────────────
# Failure handling
# ────────────────────────────────────────────────────────────────────────


def test_auto_dispatch_local_helper_failure_calls_fail_transfer(
    job_in_db, test_db, tmp_path, monkeypatch
):
    """Helper raises HTTPException (e.g. insufficient space, destination
    verification failure) → caught + translated to _fail_transfer so the
    job lands in transfer_state='failed' with the error reason. Operator
    can retry via the existing endpoint."""
    job_id, _ = job_in_db
    _patch_session(monkeypatch, test_db)
    library = tmp_path / "library"
    library.mkdir(exist_ok=True)
    monkeypatch.setattr(
        "core.transfer.service.get_active_config",
        lambda _db: _make_config("local", transfer_dir=str(library)),
    )
    monkeypatch.setattr(
        "api.routers.jobs._try_src_equals_dest_shortcut",
        lambda *a, **k: False,
    )

    def boom(*_a, **_kw):
        raise HTTPException(400, detail="Not enough free space in target")

    monkeypatch.setattr(
        "api.routers.jobs._execute_local_transfer_use_final_map", boom
    )

    fail_calls = []
    monkeypatch.setattr(
        "api.routers.jobs._fail_transfer",
        lambda job, db, msg, dests: fail_calls.append({"job_id": str(job.id), "msg": msg, "dests": list(dests)}),
    )

    # Must not raise — failures are non-fatal at this layer.
    tasks._maybe_auto_dispatch_local_transfer(job_id, MagicMock())

    assert len(fail_calls) == 1
    assert fail_calls[0]["job_id"] == job_id
    assert "free space" in fail_calls[0]["msg"].lower()


def test_auto_dispatch_local_missing_job_does_not_dispatch(test_db, monkeypatch):
    """If the job row disappeared between prep complete and this helper
    (extremely rare; cleanup race), no dispatch."""
    _patch_session(monkeypatch, test_db)
    monkeypatch.setattr(
        "core.transfer.service.get_active_config",
        lambda _db: _make_config("local"),
    )

    helper_calls = []
    monkeypatch.setattr(
        "api.routers.jobs._execute_local_transfer_use_final_map",
        lambda *a, **k: helper_calls.append((a, k)),
    )

    tasks._maybe_auto_dispatch_local_transfer(str(uuid.uuid4()), MagicMock())
    assert helper_calls == []
