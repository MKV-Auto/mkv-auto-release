"""#578: ``run_startup_drive_warmup_if_makemkv_ready`` emits a notification
when USB bandwidth contention is detected.

The warmup runs after the drive snapshot lands at startup and is the
earliest opportunity to warn the user before they kick off concurrent
rips that would saturate the bus. The notification rides the existing
``emit_notification_sync`` channel so Discord + WebUI toast see it via
the same path as #553's swap alerts.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import core.startup_discs as startup_discs
from core.usb_topology import BusContentionWarning, OpticalDrive


def _mk_warning(bus: int = 2) -> BusContentionWarning:
    return BusContentionWarning(
        bus=bus,
        speed_mbps=480,
        drive_count=2,
        drives=["Pioneer Blu-ray Drive", "ASUS External Drive"],
        message=f"USB Bus {bus} (480 Mbps) hosts 2 optical drives. ... #578",
    )


@pytest.fixture
def _no_makemkv_state(monkeypatch):
    """Stub the deps so the warmup function reaches the topology check."""
    monkeypatch.setattr(
        "core.makemkv_updater.validate_makemkv_installation",
        lambda: {"is_valid": True, "can_rip": True, "missing_components": []},
    )
    monkeypatch.setattr(
        startup_discs, "startup_enumerate_and_rescan_loaded_discs",
        lambda **kw: [("0", "/dev/sr0"), ("1", "/dev/sr1")],
    )
    monkeypatch.setattr(
        startup_discs, "record_drive_warmup_result", lambda *a, **k: None,
    )


def test_contention_warning_emits_notification(_no_makemkv_state, monkeypatch):
    monkeypatch.setattr(
        "core.usb_topology.detect_optical_drives",
        lambda: [
            OpticalDrive(bus=2, speed_mbps=480, product="Pioneer", manufacturer="P", serial="A", sysfs_path=""),
            OpticalDrive(bus=2, speed_mbps=480, product="ASUS", manufacturer="A", serial="B", sysfs_path=""),
        ],
    )
    monkeypatch.setattr(
        "core.usb_topology.detect_contention_warnings",
        lambda drives: [_mk_warning()],
    )

    emit = MagicMock()
    monkeypatch.setattr("core.notifications.emit_notification_sync", emit)

    startup_discs.run_startup_drive_warmup_if_makemkv_ready()

    assert emit.call_count == 1
    call = emit.call_args
    # First positional arg is the message text.
    msg = call.args[0] if call.args else call.kwargs.get("message")
    assert "USB Bus 2" in msg or "Bus 2" in msg or "480 Mbps" in msg


def test_no_warnings_no_notification(_no_makemkv_state, monkeypatch):
    """When the topology check returns no warnings (single drive, or all
    on SuperSpeed), no notification fires."""
    monkeypatch.setattr(
        "core.usb_topology.detect_optical_drives",
        lambda: [
            OpticalDrive(bus=3, speed_mbps=5000, product="Pioneer", manufacturer="P", serial="A", sysfs_path=""),
        ],
    )
    monkeypatch.setattr(
        "core.usb_topology.detect_contention_warnings",
        lambda drives: [],
    )

    emit = MagicMock()
    monkeypatch.setattr("core.notifications.emit_notification_sync", emit)

    startup_discs.run_startup_drive_warmup_if_makemkv_ready()

    # The "no drives detected" branch (which would emit) shouldn't fire
    # either because we returned 2 drives from the warmup.
    assert emit.call_count == 0


def test_topology_exception_does_not_break_warmup(_no_makemkv_state, monkeypatch, caplog):
    """If the topology check itself errors (e.g., sysfs unavailable on a
    weird host), the warmup must still return its drive list normally."""
    monkeypatch.setattr(
        "core.usb_topology.detect_optical_drives",
        lambda: (_ for _ in ()).throw(RuntimeError("sysfs unreadable")),
    )

    emit = MagicMock()
    monkeypatch.setattr("core.notifications.emit_notification_sync", emit)

    result = startup_discs.run_startup_drive_warmup_if_makemkv_ready()

    assert result == [("0", "/dev/sr0"), ("1", "/dev/sr1")]
    # And no notification fired.
    assert emit.call_count == 0


def test_multiple_warnings_emit_separately(_no_makemkv_state, monkeypatch):
    """Two buses each with contention → two separate notifications, each
    with a unique ``id_key`` so they don't deduplicate at the toast layer."""
    monkeypatch.setattr(
        "core.usb_topology.detect_optical_drives",
        lambda: [],
    )
    monkeypatch.setattr(
        "core.usb_topology.detect_contention_warnings",
        lambda drives: [_mk_warning(bus=2), _mk_warning(bus=4)],
    )

    emit = MagicMock()
    monkeypatch.setattr("core.notifications.emit_notification_sync", emit)

    startup_discs.run_startup_drive_warmup_if_makemkv_ready()

    assert emit.call_count == 2
    id_keys = [c.kwargs.get("id_key") for c in emit.call_args_list]
    assert id_keys == ["usb_bus_contention:2", "usb_bus_contention:4"]
