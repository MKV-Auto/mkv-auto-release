"""
Coverage for the ``_resolve_transfer_src_root`` helper (#365 step 5b).

This helper is the symmetric companion to ``_resolve_rename_dest_root``:
both return the same path under the same inputs, so transfer reads
from wherever rename wrote. The test cases here mirror
``test_resolve_rename_dest_root.py`` 1:1 — any divergence between the
two helpers is a bug, and these parallel tests are the regression
guard that surfaces it.

History: #439 introduced the rename-side helper; #450 added this
transfer-side companion; #457 (step 5c) flipped the default to
direct-to-destination; **5d** removed the env-var conditional
entirely. Behavior is now determined solely by the active
TransferConfig (see the rename-side test module's docstring for
details).
"""
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from api import models
from workers.tasks import _resolve_transfer_src_root


# ──────────────────────────────────────────────────────────────────────────
# Active local config → direct-from-destination
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
    """Active local TransferConfig → transfer src resolves to
    ``Path(transfer_dir)`` so the src==dest shortcut (#454) fires.
    MUST match what ``_resolve_rename_dest_root`` returns under the
    same conditions — that equality is the load-bearing invariant of
    the transient/-drop."""
    test_db, transfer_dir = active_local_config
    fake_paths = SimpleNamespace(transient=tmp_path / "transient")
    fake_job = SimpleNamespace(id="j-1")
    with test_db() as session:
        result = _resolve_transfer_src_root(fake_job, fake_paths, session)
    assert result == Path(transfer_dir)


def test_returns_path_type(active_local_config, tmp_path):
    """Result is a ``Path`` instance — callers depend on ``.resolve``
    and ``.exists`` in the transfer_job endpoint preflight."""
    test_db, _ = active_local_config
    fake_paths = SimpleNamespace(transient=tmp_path / "transient")
    with test_db() as session:
        result = _resolve_transfer_src_root(SimpleNamespace(id="j-1"), fake_paths, session)
    assert isinstance(result, Path)


# ──────────────────────────────────────────────────────────────────────────
# Safety: remote modes never use direct-from-dest (need staging)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["rsync", "smb", "nfs"])
def test_remote_config_falls_back_to_transient(test_db, tmp_path, mode):
    """Remote modes (rsync/smb/nfs) fall back to ``paths.transient``
    because their rename step also writes to transient/ (the local
    staging area for atomic uploads). Direct-from-dest intentionally
    only takes effect for local mode in both helpers."""
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
        result = _resolve_transfer_src_root(fake_job, fake_paths, s)
    assert result == fake_paths.transient, (
        f"{mode} mode must NOT use direct-from-dest (transfer src must "
        "match the rename helper's dest, which is also transient for remote)"
    )


def test_no_active_config_falls_back_to_transient(test_db, tmp_path):
    """No active TransferConfig → no destination known → fall back to
    staging. Same defensive fallback as the rename helper."""
    fake_paths = SimpleNamespace(transient=tmp_path / "transient")
    fake_job = SimpleNamespace(id="j-1")
    with test_db() as session:
        result = _resolve_transfer_src_root(fake_job, fake_paths, session)
    assert result == fake_paths.transient


def test_empty_transfer_dir_falls_back_to_transient(test_db, tmp_path):
    """Local mode but transfer_dir is empty (misconfigured) → fall back
    to staging. Same defensive fallback as the rename helper —
    partially-configured TransferConfigs must not steer either helper
    into an unsafe path."""
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
        result = _resolve_transfer_src_root(fake_job, fake_paths, s)
    assert result == fake_paths.transient


# ──────────────────────────────────────────────────────────────────────────
# Failure safety
# ──────────────────────────────────────────────────────────────────────────


def test_db_query_failure_falls_back_to_transient(tmp_path, monkeypatch):
    """A transient TransferConfig query glitch (DB down, schema mismatch,
    anything) must not break the transfer-step source resolution —
    fall back to staging and log. Same fallback contract as the
    rename helper."""

    def boom(*args, **kwargs):
        raise RuntimeError("simulated DB explosion")

    monkeypatch.setattr("core.transfer.service.get_active_config", boom)
    fake_paths = SimpleNamespace(transient=tmp_path / "transient")
    fake_job = SimpleNamespace(id="j-1")
    result = _resolve_transfer_src_root(fake_job, fake_paths, db=None)
    assert result == fake_paths.transient


# ──────────────────────────────────────────────────────────────────────────
# Symmetry regression guard: the two helpers must agree by construction
# ──────────────────────────────────────────────────────────────────────────


def test_symmetric_with_rename_helper_no_config(tmp_path):
    """No active config: both helpers return paths.transient. Regression
    guard against drift — if a future refactor changes one but not the
    other, rename will write somewhere transfer doesn't read from (or
    vice versa) and the pipeline will lose files silently."""
    from workers.tasks import _resolve_rename_dest_root
    fake_paths = SimpleNamespace(transient=tmp_path / "transient")
    fake_job = SimpleNamespace(id="j-1")
    assert (
        _resolve_transfer_src_root(fake_job, fake_paths, db=None)
        == _resolve_rename_dest_root(fake_job, fake_paths, db=None)
    )


def test_symmetric_with_rename_helper_local_config(active_local_config, tmp_path):
    """Active local config: both helpers return Path(transfer_dir).
    This equality IS the load-bearing invariant of the
    transient/-drop — without it rename would write somewhere transfer
    doesn't read from (silent file loss) or transfer would re-copy
    files that are already at the destination."""
    from workers.tasks import _resolve_rename_dest_root
    test_db, _transfer_dir = active_local_config
    fake_paths = SimpleNamespace(transient=tmp_path / "transient")
    fake_job = SimpleNamespace(id="j-1")
    with test_db() as session:
        rename_dest = _resolve_rename_dest_root(fake_job, fake_paths, session)
    with test_db() as session:
        transfer_src = _resolve_transfer_src_root(fake_job, fake_paths, session)
    assert rename_dest == transfer_src


@pytest.mark.parametrize("mode", ["rsync", "smb", "nfs"])
def test_symmetric_with_rename_helper_remote_config(test_db, tmp_path, mode):
    """Remote modes: both helpers return paths.transient. Same
    regression guard as the no-config case — they must agree."""
    from workers.tasks import _resolve_rename_dest_root
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
        rename_dest = _resolve_rename_dest_root(fake_job, fake_paths, s)
    with test_db() as s:
        transfer_src = _resolve_transfer_src_root(fake_job, fake_paths, s)
    assert rename_dest == transfer_src == fake_paths.transient
