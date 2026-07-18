"""Tests for #635 commit B: destination capability probe + strategy selector.

Covers:
  * Probe cleanup even when an intermediate op raises.
  * Strategy-selector truth table across all intents × capabilities.
  * Reactive fallback compat: when capabilities is ``None`` the overwrite
    intent still returns ``direct_write`` and emits a warning log.
  * ``TransferCapabilities`` dataclass serialization round-trip.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from core.transfer.capabilities import (
    PROBE_PREFIX,
    TransferCapabilities,
    _probe_local,
    probe,
)
from core.transfer.service import TransferPlanError, resolve_transfer_plan


def _caps(
    *,
    write: bool = True,
    overwrite: bool = False,
    delete: bool = False,
    rename: bool = False,
) -> TransferCapabilities:
    return TransferCapabilities(
        can_write_new=write,
        can_overwrite_in_place=overwrite,
        can_delete=delete,
        can_rename=rename,
        probed_at="2026-07-07T00:00:00+00:00",
    )


# ────────────────────────────── Serialization ──────────────────────────────


def test_capabilities_to_from_dict_roundtrip() -> None:
    caps = TransferCapabilities(
        can_write_new=True,
        can_overwrite_in_place=False,
        can_delete=True,
        can_rename=False,
        probed_at="2026-07-07T12:00:00+00:00",
        probe_error=None,
        notes={"share": "PLEX Media", "flag": True},
    )
    data = caps.to_dict()
    assert data["can_write_new"] is True
    assert data["can_overwrite_in_place"] is False
    assert data["notes"] == {"share": "PLEX Media", "flag": True}

    restored = TransferCapabilities.from_dict(data)
    assert restored == caps


def test_capabilities_from_dict_missing_fields_defaults() -> None:
    restored = TransferCapabilities.from_dict({})
    assert restored.can_write_new is False
    assert restored.can_overwrite_in_place is False
    assert restored.can_delete is False
    assert restored.can_rename is False
    assert restored.probed_at == ""
    assert restored.probe_error is None
    assert restored.notes == {}


def test_capabilities_from_dict_rejects_non_dict() -> None:
    with pytest.raises(TypeError):
        TransferCapabilities.from_dict("not-a-dict")  # type: ignore[arg-type]


# ────────────────────────── Local probe (real fs) ──────────────────────────


def test_probe_local_success_on_writable_dir(tmp_path) -> None:
    cfg = SimpleNamespace(mode="local", transfer_dir=str(tmp_path))
    caps = probe(cfg)
    assert caps.can_write_new is True
    assert caps.can_overwrite_in_place is True
    assert caps.can_delete is True
    assert caps.can_rename is True
    assert caps.probe_error is None
    # No probe artifacts left behind.
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(PROBE_PREFIX)]
    assert leftovers == []


def test_probe_local_missing_transfer_dir_returns_pessimistic() -> None:
    cfg = SimpleNamespace(mode="local", transfer_dir=None)
    caps = probe(cfg)
    assert caps.can_write_new is False
    assert caps.can_overwrite_in_place is False
    assert caps.can_delete is False
    assert caps.can_rename is False
    assert caps.probe_error is not None
    assert "transfer_dir" in caps.probe_error


def test_probe_local_cleans_up_when_rename_raises(tmp_path, monkeypatch) -> None:
    """Even when an intermediate op raises (here: rename), no probe
    artifacts must remain under ``transfer_dir``. Guards the try/finally
    cleanup contract in ``_probe_local``.
    """
    from pathlib import Path as _Path

    original_rename = _Path.rename

    def _boom(self, target):  # type: ignore[no-untyped-def]
        if self.name.startswith(PROBE_PREFIX):
            raise OSError("simulated rename failure")
        return original_rename(self, target)

    monkeypatch.setattr(_Path, "rename", _boom)
    cfg = SimpleNamespace(mode="local", transfer_dir=str(tmp_path))
    caps = probe(cfg)
    # rename op failed, but write/overwrite/delete still succeeded.
    assert caps.can_write_new is True
    assert caps.can_rename is False
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(PROBE_PREFIX)]
    assert leftovers == [], f"probe artifacts leaked: {leftovers}"


def test_probe_dispatch_unknown_mode_pessimistic() -> None:
    cfg = SimpleNamespace(mode="ftp", transfer_dir="/anywhere")
    caps = probe(cfg)
    assert caps.can_write_new is False
    assert caps.probe_error is not None
    assert "unknown transfer mode" in caps.probe_error


def test_probe_dispatcher_wraps_exceptions(monkeypatch, tmp_path) -> None:
    """If a per-protocol probe raises, ``probe()`` returns pessimistic
    caps rather than propagating — the celery task must not fail because
    a share went offline mid-probe.
    """
    from core.transfer import capabilities as caps_mod

    def _raise(_config):
        raise RuntimeError("probe blew up")

    monkeypatch.setattr(caps_mod, "_probe_local", _raise)
    cfg = SimpleNamespace(mode="local", transfer_dir=str(tmp_path))
    caps = probe(cfg)
    assert caps.can_write_new is False
    assert caps.probe_error is not None
    assert "probe raised" in caps.probe_error


# ─────────────────────────── SMB probe cleanup ────────────────────────────


def test_probe_smb_guarantees_cleanup_on_midway_failure(monkeypatch) -> None:
    """Simulate a subprocess.run that succeeds for the first put then
    raises. The finally-block must still issue delete attempts for both
    probe filenames so no ``.mkvauto-probe-*`` files leak to the share.
    """
    from core.transfer import capabilities as caps_mod

    calls: list[str] = []
    step = {"i": 0}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        step["i"] += 1
        try:
            flag_idx = cmd.index("-c")
            calls.append(cmd[flag_idx + 1])
        except ValueError:
            pass
        # Fail the second smbclient invocation (the overwrite put) to
        # exercise the middle-of-probe error path.
        if step["i"] == 2:
            raise RuntimeError("simulated network blip")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(caps_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(caps_mod.shutil, "which", lambda _bin: "/usr/bin/smbclient")

    config = SimpleNamespace(
        mode="smb",
        id="cfg-1",
        transfer_dir="PLEX",
        config_data={"host": "unraid.local", "share": "Media", "path": "PLEX", "port": 445},
    )
    caps = probe(config, db=None)
    # Probe blew up on op 2 → dispatcher wraps as probe_error.
    assert caps.probe_error is not None
    # The finally cleanup MUST have issued delete for both probe filenames.
    del_calls = [c for c in calls if c.startswith("del ")]
    assert len(del_calls) >= 2, f"expected two delete attempts, saw {del_calls!r}"


# ─────────────────── Strategy selector truth table ────────────────────


@pytest.mark.parametrize(
    "intent,caps_kwargs,expected",
    [
        # Overwrite intent × capability combinations.
        ("overwrite", {"overwrite": True}, "direct_write"),
        ("overwrite", {"overwrite": True, "delete": True, "rename": True}, "direct_write"),
        ("overwrite", {"overwrite": False, "delete": True}, "delete_then_write"),
        ("overwrite", {"overwrite": False, "delete": True, "rename": True}, "delete_then_write"),
        ("overwrite", {"overwrite": False, "delete": False, "rename": True}, "rename_source"),
        # Skip intent short-circuits.
        ("skip", {"overwrite": True}, "skip"),
        ("skip", {"overwrite": False, "delete": False, "rename": False}, "skip"),
        # Fail intent = "let caller precheck existence".
        ("fail", {"overwrite": True, "delete": True, "rename": True}, "precheck_fail"),
        # Rename intent requires can_rename.
        ("rename", {"rename": True}, "rename_source"),
        ("rename", {"overwrite": True, "delete": True, "rename": True}, "rename_source"),
    ],
)
def test_resolve_transfer_plan_truth_table(intent, caps_kwargs, expected) -> None:
    config = SimpleNamespace(id="cfg", mode="smb")
    caps = _caps(**caps_kwargs)
    assert resolve_transfer_plan(config, intent, caps) == expected


def test_resolve_transfer_plan_overwrite_impossible_raises() -> None:
    config = SimpleNamespace(id="cfg", mode="smb")
    caps = _caps(overwrite=False, delete=False, rename=False)
    with pytest.raises(TransferPlanError):
        resolve_transfer_plan(config, "overwrite", caps)


def test_resolve_transfer_plan_rename_without_capability_raises() -> None:
    config = SimpleNamespace(id="cfg", mode="smb")
    caps = _caps(overwrite=True, delete=True, rename=False)
    with pytest.raises(TransferPlanError):
        resolve_transfer_plan(config, "rename", caps)


# ─────────────── Reactive fallback interop (caps unknown) ─────────────


def test_resolve_transfer_plan_caps_none_overwrite_direct_write(caplog) -> None:
    """When capabilities is None, ``overwrite`` still returns
    ``direct_write`` so the reactive SMB delete+retry fallback (commit A)
    can take over on ``NT_STATUS_ACCESS_DENIED``. A warning is logged.
    """
    config = SimpleNamespace(id="cfg-none", mode="smb")
    with caplog.at_level(logging.WARNING, logger="core.transfer.service"):
        plan = resolve_transfer_plan(config, "overwrite", None)
    assert plan == "direct_write"
    warning_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("no capabilities probed" in m for m in warning_messages)


def test_resolve_transfer_plan_caps_none_rename_returns_rename() -> None:
    """Unknown caps + rename intent still proceeds — the transfer layer
    will surface the failure if the rename actually fails. Symmetric to
    the overwrite behavior above.
    """
    config = SimpleNamespace(id="cfg-none", mode="smb")
    plan = resolve_transfer_plan(config, "rename", None)
    assert plan == "rename_source"


def test_resolve_transfer_plan_caps_none_skip_returns_skip() -> None:
    config = SimpleNamespace(id="cfg-none", mode="smb")
    assert resolve_transfer_plan(config, "skip", None) == "skip"
