"""Path resolution for the postprocess→transfer boundary (#365).

Local-mode jobs resolve to the active ``TransferConfig``'s
``transfer_dir`` so rename writes directly to the final library path —
the architectural goal of the transient/-drop. Remote modes
(rsync/smb/nfs) and jobs without a usable active config fall back to
``paths.transient``, which serves as local staging for atomic uploads
in the remote case.

The three helpers (``resolve_rename_dest_root``,
``resolve_transfer_src_root``, ``resolve_transfer_prep_validation_root``)
are **mirror twins by construction**: any divergence means rename
writes where transfer doesn't read (or vice versa), or the postprocess
validator looks in the wrong place. The symmetry-regression tests
under ``Backend/tests/test_resolve_*.py`` fail loud on drift.

This module lives under ``core/`` so workers (``workers.tasks``), the
API (``api.routers.jobs``), and the stage validator
(``core.stage_validation``) can all call it without circular imports.

**History:** #439/#440 introduced these helpers gated by an opt-in
``MKVAUTO_RENAME_DIRECT_TO_DEST`` env var; #454 added the src==dest
short-circuit that made the flag-on flow production-safe; #457 flipped
the default to flag-on; **5d (this module's current state) removed the
env-var conditional entirely** — operator opt-out is no longer
available. If a future regression in the active-config-driven path
needs an escape hatch, prefer fixing the helper directly rather than
re-adding a global env-var gate.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


def _resolve_flag_on_local_dest(db: Session) -> Path | None:
    """Return ``Path(config.transfer_dir)`` when the active TransferConfig
    is local-mode with a valid transfer_dir. Returns None otherwise so
    callers can fall back to ``paths.transient``.

    Defensive: any failure to read the TransferConfig (DB transient
    error, schema mismatch) is logged at WARNING and returns None — a
    transient lookup glitch must never turn a successful rip into a
    rename/transfer/validation failure.
    """
    try:
        from core.transfer.service import get_active_config
        cfg = get_active_config(db)
        if cfg is None:
            return None
        mode = (getattr(cfg, "mode", None) or "").strip().lower()
        transfer_dir = (getattr(cfg, "transfer_dir", None) or "").strip()
        if mode != "local" or not transfer_dir:
            return None
        return Path(transfer_dir)
    except Exception as exc:
        log.warning(
            "%s: TransferConfig lookup failed, falling back to staging: %s",
            __name__, exc,
        )
        return None


def resolve_rename_dest_root(job: Any, paths: Any, db: Session) -> Path:
    """Decide where ``rename_outputs`` should write for this job.

    Local mode with a valid ``transfer_dir`` → returns
    ``Path(transfer_dir)`` so rename writes directly to the final
    library path. Remote modes (rsync/smb/nfs), no-active-config, and
    DB lookup failures fall back to ``paths.transient`` (local staging
    for atomic uploads in the remote case; safe default in the
    failure case).

    See ``docs/ADR-001-postprocess-collapse.md`` and
    ``docs/plans/postprocess-collapse-handoff.md``.
    """
    dest = _resolve_flag_on_local_dest(db)
    return dest if dest is not None else paths.transient


def resolve_transfer_src_root(job: Any, paths: Any, db: Session) -> Path:
    """Decide where the transfer step should read its source files from.

    Symmetric companion to :func:`resolve_rename_dest_root`. Both
    return the same path under the same inputs, so transfer reads what
    rename wrote. Drift between the two helpers means silent file loss
    (rename writes to A, transfer reads from B).

    See :func:`resolve_rename_dest_root` for full semantics and the
    symmetry-regression test guards under
    ``tests/test_resolve_transfer_src_root.py``.
    """
    dest = _resolve_flag_on_local_dest(db)
    return dest if dest is not None else paths.transient


def resolve_transfer_prep_validation_root(job: Any, paths: Any, db: Session) -> Path:
    """Decide where ``validate_transfer_prep_output`` should look for the
    files that ``rename_outputs`` wrote.

    By construction this is the same as :func:`resolve_rename_dest_root`
    — the validator is checking the very files rename just placed.
    Exposed as its own name so the call-site intent is explicit and a
    future divergence in resolution rules (e.g. if validation grows
    additional fallbacks) is straightforward to introduce.

    Returns the same path as ``resolve_rename_dest_root(job, paths, db)``.
    """
    return resolve_rename_dest_root(job, paths, db)
