"""
Coverage for the ``_resolve_rename_dest_root`` helper (#365 transient/ drop).

History: #439 added the helper as a seam; #440 added an env-var-gated
direct-to-destination branch; #457 (step 5c) flipped the default to
direct-to-destination; **5d** removed the env-var conditional entirely.
Behavior is now determined solely by the active TransferConfig: local
mode with a valid ``transfer_dir`` resolves directly to the library
path; remote modes (rsync/smb/nfs), no active config, empty
``transfer_dir``, and DB-lookup failures all fall back to
``paths.transient`` (which serves as local staging for atomic uploads
in the remote case).
"""
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from api import models
from workers.tasks import _resolve_rename_dest_root


# ──────────────────────────────────────────────────────────────────────────
# Active local config → direct-to-destination
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def active_local_config(test_db):
    """Active TransferConfig in local mode with a valid transfer_dir.
    Returns (db_session_factory, transfer_dir_path)."""
    transfer_dir = "/library/media"
    session = test_db()
    try:
        cfg = models.TransferConfig(
            id=str(uuid.uuid4()),
            mode="local",
            is_active=True,
            transfer_dir=transfer_dir,
        )
        session.add(cfg)
        session.commit()
    finally:
        session.close()
    return test_db, transfer_dir


def test_local_config_returns_transfer_dir(active_local_config, tmp_path):
    """Active local TransferConfig → helper returns the configured
    ``transfer_dir`` as the rename destination."""
    test_db, transfer_dir = active_local_config
    fake_paths = SimpleNamespace(transient=tmp_path / "transient")
    fake_job = SimpleNamespace(id="j-1")
    with test_db() as session:
        result = _resolve_rename_dest_root(fake_job, fake_paths, session)
    assert result == Path(transfer_dir)


def test_returns_path_type(active_local_config, tmp_path):
    """Result is a ``Path`` instance — callers depend on ``.mkdir`` /
    ``.rglob``."""
    test_db, _ = active_local_config
    fake_paths = SimpleNamespace(transient=tmp_path / "transient")
    with test_db() as session:
        result = _resolve_rename_dest_root(SimpleNamespace(id="j-1"), fake_paths, session)
    assert isinstance(result, Path)


# ──────────────────────────────────────────────────────────────────────────
# Safety: remote modes never use direct-to-dest (need local staging)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["rsync", "smb", "nfs"])
def test_remote_config_falls_back_to_transient(test_db, tmp_path, mode):
    """Remote modes (rsync/smb/nfs) fall back to ``paths.transient``
    because they need a local staging area for atomic uploads. The
    direct-to-dest behaviour intentionally only takes effect for
    local mode."""
    session = test_db()
    try:
        cfg = models.TransferConfig(
            id=str(uuid.uuid4()),
            mode=mode,
            is_active=True,
            transfer_dir="/remote/path",
        )
        session.add(cfg)
        session.commit()
    finally:
        session.close()
    fake_paths = SimpleNamespace(transient=tmp_path / "transient")
    fake_job = SimpleNamespace(id="j-1")
    with test_db() as s:
        result = _resolve_rename_dest_root(fake_job, fake_paths, s)
    assert result == fake_paths.transient, (
        f"{mode} mode must NOT use direct-to-dest (no staging would break "
        "atomic upload semantics)"
    )


def test_no_active_config_falls_back_to_transient(test_db, tmp_path):
    """No active TransferConfig → no destination known → fall back to
    staging. Don't crash, don't write to a random location."""
    fake_paths = SimpleNamespace(transient=tmp_path / "transient")
    fake_job = SimpleNamespace(id="j-1")
    with test_db() as session:
        result = _resolve_rename_dest_root(fake_job, fake_paths, session)
    assert result == fake_paths.transient


def test_empty_transfer_dir_falls_back_to_transient(test_db, tmp_path):
    """Local mode but transfer_dir is empty (misconfigured) → fall back
    to staging. Defensive against partially-configured TransferConfigs."""
    session = test_db()
    try:
        cfg = models.TransferConfig(
            id=str(uuid.uuid4()),
            mode="local",
            is_active=True,
            transfer_dir="",
        )
        session.add(cfg)
        session.commit()
    finally:
        session.close()
    fake_paths = SimpleNamespace(transient=tmp_path / "transient")
    fake_job = SimpleNamespace(id="j-1")
    with test_db() as s:
        result = _resolve_rename_dest_root(fake_job, fake_paths, s)
    assert result == fake_paths.transient


# ──────────────────────────────────────────────────────────────────────────
# Failure safety
# ──────────────────────────────────────────────────────────────────────────


def test_db_query_failure_falls_back_to_transient(tmp_path, monkeypatch):
    """A transient TransferConfig query glitch (DB down, schema mismatch,
    anything) must not turn a successful rip into a rename failure —
    fall back to staging and log."""

    def boom(*args, **kwargs):
        raise RuntimeError("simulated DB explosion")

    monkeypatch.setattr("core.transfer.service.get_active_config", boom)
    fake_paths = SimpleNamespace(transient=tmp_path / "transient")
    fake_job = SimpleNamespace(id="j-1")
    result = _resolve_rename_dest_root(fake_job, fake_paths, db=None)
    assert result == fake_paths.transient
