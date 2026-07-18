"""
Tests for the shared path resolution helpers in
``core/transfer/path_resolution.py`` (#365 — transient/-drop migration).

The helpers are the single source of truth for "where does rename
write / transfer read / the postprocess validator look." The original
``test_resolve_rename_dest_root.py`` and ``test_resolve_transfer_src_root.py``
files import the same helpers via re-exports from ``workers.tasks``;
those tests continue to cover the rename/transfer pair end-to-end.

This file adds:

  * Direct-import tests against the canonical module location so a
    future refactor that removes the workers/tasks re-exports doesn't
    lose coverage.
  * Specific coverage for ``resolve_transfer_prep_validation_root``, the
    third caller of the same logic (the postprocess validator was
    previously hardcoded to ``paths.transient`` and would report
    "0 of N expected files" when rename had written elsewhere; see
    #453).
  * A three-way symmetry guard verifying rename/transfer/validation
    all agree under every TransferConfig configuration — the
    load-bearing invariant of the transient/-drop architecture. Drift
    in any one means the pipeline silently loses files.

**History:** the env-var conditional (``MKVAUTO_RENAME_DIRECT_TO_DEST``)
introduced in #440 and flipped to default-on in #457 was removed
entirely in 5d. Behavior is now determined solely by the active
TransferConfig: local mode with a valid ``transfer_dir`` resolves to
the library; everything else (remote, no config, DB failure) falls
back to ``paths.transient``.
"""
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from api import models
from core.transfer.path_resolution import (
    resolve_transfer_prep_validation_root,
    resolve_rename_dest_root,
    resolve_transfer_src_root,
)


# ──────────────────────────────────────────────────────────────────────────
# resolve_transfer_prep_validation_root — third public caller of the helper
# ──────────────────────────────────────────────────────────────────────────


def test_validation_root_no_active_config_returns_transient(tmp_path):
    """No active TransferConfig → fall back to staging."""
    fake_paths = SimpleNamespace(transient=tmp_path / "jobs" / "j-1" / "transient")
    fake_job = SimpleNamespace(id="j-1")
    assert resolve_transfer_prep_validation_root(fake_job, fake_paths, db=None) == fake_paths.transient


def test_validation_root_local_config_returns_transfer_dir(test_db, tmp_path):
    """Local TransferConfig → validator looks at ``config.transfer_dir``,
    where rename actually wrote. Without this the validator would
    report "0 of N expected files" even though rename succeeded — the
    bug-fix that motivated extracting the shared resolver in #453."""
    transfer_dir = "/library/media"
    session = test_db()
    try:
        session.add(models.TransferConfig(
            id=str(uuid.uuid4()), mode="local", is_active=True,
            transfer_dir=transfer_dir,
        ))
        session.commit()
    finally:
        session.close()
    fake_paths = SimpleNamespace(transient=tmp_path / "transient")
    fake_job = SimpleNamespace(id="j-1")
    with test_db() as session:
        result = resolve_transfer_prep_validation_root(fake_job, fake_paths, session)
    assert result == Path(transfer_dir)


@pytest.mark.parametrize("mode", ["rsync", "smb", "nfs"])
def test_validation_root_remote_config_returns_transient(test_db, tmp_path, mode):
    """Remote modes always use transient/ (local staging for atomic
    upload). Validator must look there to find what rename wrote."""
    session = test_db()
    try:
        session.add(models.TransferConfig(
            id=str(uuid.uuid4()), mode=mode, is_active=True,
            transfer_dir="/remote/path",
        ))
        session.commit()
    finally:
        session.close()
    fake_paths = SimpleNamespace(transient=tmp_path / "transient")
    fake_job = SimpleNamespace(id="j-1")
    with test_db() as session:
        result = resolve_transfer_prep_validation_root(fake_job, fake_paths, session)
    assert result == fake_paths.transient


def test_validation_root_db_failure_falls_back_to_transient(tmp_path, monkeypatch):
    """Defensive: any TransferConfig query glitch must not break the
    validator's source-path resolution. Falls back to staging and logs.
    Same fallback contract as the rename/transfer helpers."""

    def boom(*args, **kwargs):
        raise RuntimeError("simulated DB explosion")

    monkeypatch.setattr("core.transfer.service.get_active_config", boom)
    fake_paths = SimpleNamespace(transient=tmp_path / "transient")
    fake_job = SimpleNamespace(id="j-1")
    assert resolve_transfer_prep_validation_root(fake_job, fake_paths, db=None) == fake_paths.transient


# ──────────────────────────────────────────────────────────────────────────
# Three-way symmetry: rename / transfer / validation must all agree
#
# This is the architectural invariant of the transient/-drop work. Any
# divergence means:
#   - rename writes where transfer doesn't read → silent file loss in
#     the transfer step
#   - rename writes where the validator doesn't look → "0 of N expected
#     files" false failure (the exact bug that motivated this audit)
#   - transfer reads where the validator doesn't look → transfer
#     succeeds but downstream consumers see stale validation state
# ──────────────────────────────────────────────────────────────────────────


def test_three_way_symmetry_local_config(test_db, tmp_path):
    """Local config: all three return Path(transfer_dir). This is THE
    invariant that makes the direct-to-dest flow correct — without it
    the audit's bug fix would surface as the validator looking at one
    place while rename writes to another."""
    transfer_dir = "/library/media"
    session = test_db()
    try:
        session.add(models.TransferConfig(
            id=str(uuid.uuid4()), mode="local", is_active=True,
            transfer_dir=transfer_dir,
        ))
        session.commit()
    finally:
        session.close()
    fake_paths = SimpleNamespace(transient=tmp_path / "transient")
    fake_job = SimpleNamespace(id="j-1")

    with test_db() as session:
        rename = resolve_rename_dest_root(fake_job, fake_paths, session)
    with test_db() as session:
        transfer = resolve_transfer_src_root(fake_job, fake_paths, session)
    with test_db() as session:
        validation = resolve_transfer_prep_validation_root(fake_job, fake_paths, session)
    assert rename == transfer == validation == Path(transfer_dir)


@pytest.mark.parametrize("mode", ["rsync", "smb", "nfs"])
def test_three_way_symmetry_remote_config(test_db, tmp_path, mode):
    """Remote modes: all three fall back to paths.transient because
    remote modes use local staging for atomic upload."""
    session = test_db()
    try:
        session.add(models.TransferConfig(
            id=str(uuid.uuid4()), mode=mode, is_active=True,
            transfer_dir="/remote/path",
        ))
        session.commit()
    finally:
        session.close()
    fake_paths = SimpleNamespace(transient=tmp_path / "transient")
    fake_job = SimpleNamespace(id="j-1")

    with test_db() as session:
        rename = resolve_rename_dest_root(fake_job, fake_paths, session)
    with test_db() as session:
        transfer = resolve_transfer_src_root(fake_job, fake_paths, session)
    with test_db() as session:
        validation = resolve_transfer_prep_validation_root(fake_job, fake_paths, session)
    assert rename == transfer == validation == fake_paths.transient


def test_three_way_symmetry_no_active_config(tmp_path):
    """No active TransferConfig: all three fall back to paths.transient
    (no destination known). Safe-default contract."""
    fake_paths = SimpleNamespace(transient=tmp_path / "transient")
    fake_job = SimpleNamespace(id="j-1")
    rename = resolve_rename_dest_root(fake_job, fake_paths, db=None)
    transfer = resolve_transfer_src_root(fake_job, fake_paths, db=None)
    validation = resolve_transfer_prep_validation_root(fake_job, fake_paths, db=None)
    assert rename == transfer == validation == fake_paths.transient


# ──────────────────────────────────────────────────────────────────────────
# Backward-compat: workers/tasks re-exports continue to work
# ──────────────────────────────────────────────────────────────────────────


def test_workers_tasks_reexports_are_canonical():
    """``workers.tasks._resolve_rename_dest_root`` and
    ``_resolve_transfer_src_root`` are re-exports of the canonical
    helpers in ``core/transfer/path_resolution.py``. Tests and call
    sites that imported them by the old name must still work. A
    future PR can drop the underscore aliases entirely once all call
    sites move over."""
    from workers.tasks import _resolve_rename_dest_root, _resolve_transfer_src_root
    assert _resolve_rename_dest_root is resolve_rename_dest_root
    assert _resolve_transfer_src_root is resolve_transfer_src_root
