"""
Coverage for the flag-aware behaviour of the devmode revert endpoints
(#365 transient/-drop audit follow-up — deferred item #1).

Before this fix, ``/reset-postprocess?clear_files=true`` and
``/restore-postprocess`` only cleared ``paths.transient``. Under
``MKVAUTO_RENAME_DIRECT_TO_DEST=1`` (local mode) the rename writes
directly to ``config.transfer_dir`` and ``transient/`` stays empty —
so the "clear" actions silently left the post-processed files in the
library, and the next postprocess run saw ``files_already_moved=True``
from those leftover files and skipped rename. Operator's "revert and
try again" did nothing.

Fix: each endpoint now additionally walks ``job.post_paths`` and
unlinks the per-rip files at the flag-aware rename destination (the
new ``_clear_per_rip_postprocess_output`` helper). Per-rip safety is
the same pattern the postprocess validator and the transfer-step
shortcut use — only this rip's slots are touched, never a bulk walk
of the shared library.

``/revert-transfer`` is intentionally not changed: under flag-on
src==dest the transfer was a no-op, the transfer-stage backup is
empty (because ``paths.transient`` was empty when the backup ran),
and ``restore_files(empty_backup, transient_dir)`` is correctly a
no-op. The state-revert portion of the endpoint still works the
same under both flag branches.
"""
from pathlib import Path
import uuid

import pytest
from fastapi.testclient import TestClient

from api import database, models
from api.main import app
from api.routers.jobs import _clear_per_rip_postprocess_output


# ──────────────────────────────────────────────────────────────────────────
# Direct unit tests for _clear_per_rip_postprocess_output
# ──────────────────────────────────────────────────────────────────────────


def test_clear_per_rip_flag_off_uses_transient(test_db, tmp_path, monkeypatch):
    """Flag off: helper resolves to paths.transient, unlinks per-rip
    files there. Walks only post_paths entries, not the directory."""
    from types import SimpleNamespace

    transient = tmp_path / "jobs" / "j-1" / "transient"
    transient.mkdir(parents=True)
    # This rip's files
    f1 = transient / "Movies" / "X" / "X.mkv"
    f1.parent.mkdir(parents=True)
    f1.write_bytes(b"x")
    # An unrelated file the helper must NOT touch
    other = transient / "Movies" / "Other" / "other.mkv"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"x")

    job = SimpleNamespace(id="j-1", post_paths={"t1": "Movies/X/X.mkv"})
    paths = SimpleNamespace(transient=transient)

    removed = _clear_per_rip_postprocess_output(job, paths, db=None)

    assert removed == 1
    assert not f1.exists()
    assert other.exists(), "Per-rip walk must not delete unrelated files"


def test_clear_per_rip_flag_on_local_uses_transfer_dir(test_db, tmp_path, monkeypatch):
    """Flag on + local: helper resolves to config.transfer_dir, unlinks
    per-rip files there. transient/ is left alone (typically empty
    under flag-on; the bulk shutil.rmtree above the helper call site
    handles transient cleanup for the flag-off and remote cases)."""
    from types import SimpleNamespace
    transfer_dir = tmp_path / "library"
    transfer_dir.mkdir()
    # This rip's file at the library
    f1 = transfer_dir / "Movies" / "X" / "X.mkv"
    f1.parent.mkdir(parents=True)
    f1.write_bytes(b"x")
    # An unrelated existing library file the helper must NOT touch
    unrelated = transfer_dir / "Movies" / "Unrelated Title" / "unrelated.mkv"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"x")

    with test_db() as session:
        session.add(models.TransferConfig(
            id=str(uuid.uuid4()), mode="local", is_active=True,
            transfer_dir=str(transfer_dir),
        ))
        session.commit()

    job = SimpleNamespace(id="j-1", post_paths={"t1": "Movies/X/X.mkv"})
    paths = SimpleNamespace(transient=tmp_path / "fake_transient")  # not used under flag-on local

    with test_db() as session:
        removed = _clear_per_rip_postprocess_output(job, paths, session)

    assert removed == 1
    assert not f1.exists()
    assert unrelated.exists(), "Per-rip walk must not delete unrelated library files"


def test_clear_per_rip_returns_zero_when_post_paths_empty(test_db, tmp_path, monkeypatch):
    """No post_paths → nothing to clear. Defensive guard for the case
    where the endpoint is called on a job that hasn't run postprocess."""
    from types import SimpleNamespace
    job = SimpleNamespace(id="j-1", post_paths={})
    paths = SimpleNamespace(transient=tmp_path / "transient")
    assert _clear_per_rip_postprocess_output(job, paths, db=None) == 0


def test_clear_per_rip_missing_file_is_skipped(test_db, tmp_path, monkeypatch):
    """post_paths references a file that doesn't exist on disk → skip.
    Doesn't raise; doesn't decrement the counter. Helper survives
    partial state (e.g. failed postprocess where only some files moved)."""
    from types import SimpleNamespace
    transient = tmp_path / "transient"
    transient.mkdir()
    job = SimpleNamespace(id="j-1", post_paths={"t1": "Movies/Ghost/Ghost.mkv"})
    paths = SimpleNamespace(transient=transient)
    assert _clear_per_rip_postprocess_output(job, paths, db=None) == 0


# ──────────────────────────────────────────────────────────────────────────
# Integration via the /reset-postprocess endpoint
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def client(test_db, monkeypatch):
    """TestClient wired to the test DB. Forces ENABLE_DEVMODE=1 so the
    devmode-only endpoints are reachable."""
    from api.routers import jobs as jobs_router
    monkeypatch.setenv("ENABLE_DEVMODE", "1")

    def override_get_db():
        session = test_db()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[database.get_db] = override_get_db
    if hasattr(jobs_router, "get_db"):
        app.dependency_overrides[jobs_router.get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed_job_with_post_paths(session, *, post_paths: dict):
    disc_id = str(uuid.uuid4())
    session.add(models.Disc(id=disc_id, content_hash=f"h-{uuid.uuid4().hex[:8]}"))
    job_id = str(uuid.uuid4())
    session.add(models.Job(
        id=job_id, disc_id=disc_id, disc_num="1", mount_point="/mnt/sr0",
        rip_state="completed",
        job_status="running",
        post_paths=post_paths,
    ))
    session.commit()
    return job_id


def test_reset_postprocess_clear_files_flag_on_local_clears_library(
    client, test_db, tmp_path, monkeypatch,
):
    """Audit-bug-fix path: under flag-on local mode,
    ``/reset-postprocess?clear_files=true`` must remove the
    post-processed files from the library, not just from the (empty)
    transient/. Pre-fix the library files survived and the next
    postprocess run saw files_already_moved=True."""
    transfer_dir = tmp_path / "library"
    transfer_dir.mkdir()
    f1 = transfer_dir / "Movies" / "X" / "X.mkv"
    f1.parent.mkdir(parents=True)
    f1.write_bytes(b"x")

    monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path / "data"))

    session = test_db()
    try:
        session.add(models.TransferConfig(
            id=str(uuid.uuid4()), mode="local", is_active=True,
            transfer_dir=str(transfer_dir),
        ))
        session.commit()
        job_id = _seed_job_with_post_paths(
            session, post_paths={"t1": "Movies/X/X.mkv"},
        )
    finally:
        session.close()

    resp = client.post(f"/jobs/{job_id}/reset-postprocess?clear_files=true")
    assert resp.status_code == 200, resp.text
    assert not f1.exists(), "Library file should be removed under flag-on local clear"


def test_reset_postprocess_clear_files_flag_off_preserves_library(
    client, test_db, tmp_path, monkeypatch,
):
    """Regression guard: flag-off behaviour is unchanged. The
    library/non-transient location is never touched (only transient).
    Confirms the helper's safety contract holds when the flag is off."""
    monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path / "data"))

    # Simulate a flag-off rip: transient has the files (typical
    # pre-collapse layout). The "library" path here represents an
    # already-transferred copy from a previous run — must NOT be
    # touched by reset_postprocess under flag-off.
    library_pretend = tmp_path / "elsewhere" / "library" / "Movies" / "X" / "X.mkv"
    library_pretend.parent.mkdir(parents=True)
    library_pretend.write_bytes(b"already-transferred")

    session = test_db()
    try:
        job_id = _seed_job_with_post_paths(
            session, post_paths={"t1": "Movies/X/X.mkv"},
        )
    finally:
        session.close()

    # transient/ has nothing in it yet — clear is essentially a no-op,
    # which is fine; we just need to confirm the endpoint doesn't
    # delete the unrelated library file under flag-off.
    resp = client.post(f"/jobs/{job_id}/reset-postprocess?clear_files=true")
    assert resp.status_code == 200, resp.text
    assert library_pretend.exists(), (
        "Flag-off must not delete files outside transient (no active local config)"
    )
