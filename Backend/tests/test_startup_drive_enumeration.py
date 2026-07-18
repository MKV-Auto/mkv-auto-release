"""Tests for API-startup drive enumeration and per-disc rescan.

After #562 PR 3, startup enumeration runs through the OS-level drive
registry (``startup_enumerate_drives_via_registry``) rather than ``info
disc:9999``. The per-disc ``info dev:`` scans still execute, but skip
drives where ``_recover_inflight_jobs`` has already restarted a rip.
"""

import pytest
from unittest.mock import Mock

from fastapi import HTTPException

import core.utils as u
import core.startup_discs as startup_discs


@pytest.fixture
def reset_drive_scan():
    saved = {k: v for k, v in u._last_drive_scan.items()}
    u._last_drive_scan.update({"ts": 0, "drives": [], "fail_count": 0, "drive_hardware": {}})
    yield
    u._last_drive_scan.clear()
    u._last_drive_scan.update(saved)


def _registry_snapshots(devices_loaded):
    """Build DriveSnapshot fixtures keyed by mount_point → loaded(bool)."""
    from core.drive_identity import DriveIdentity
    from core.drive_registry import DriveSnapshot

    return [
        DriveSnapshot(
            mount_point=mp,
            loaded=loaded,
            volume_label="LBL" if loaded else None,
            media_kind="BD" if loaded else None,
            identity=DriveIdentity(
                by_id_serial=f"S{mp}", vendor="V", model="M", bus="usb",
                by_id_name="", hardware_name=None, identity_source="by-id",
            ),
            udev_state={},
            observed_at=0.0,
        )
        for mp, loaded in devices_loaded
    ]


def test_startup_enumerate_via_registry_returns_loaded_only(monkeypatch, reset_drive_scan):
    """The registry-based startup enumeration sorts by mount_point, filters
    to loaded drives only, and synthesizes the ordinal index — no MakeMKV."""
    from core import drive_registry

    snapshots = _registry_snapshots(
        [("/dev/sr1", True), ("/dev/sr0", True), ("/dev/sr2", False)]
    )
    monkeypatch.setattr(drive_registry, "snapshot_drives", lambda **kw: snapshots)

    makemkv_called = Mock()
    monkeypatch.setattr(u, "run_makemkv", makemkv_called)

    out = u.startup_enumerate_drives_via_registry()

    assert out == [("0", "/dev/sr0"), ("1", "/dev/sr1")]
    makemkv_called.assert_not_called()


def test_startup_warm_drive_cache_is_thin_wrapper_on_registry(monkeypatch, reset_drive_scan):
    """``startup_warm_drive_cache`` no longer falls back through MakeMKV —
    it just delegates to the registry path."""
    from core import drive_registry

    snapshots = _registry_snapshots([("/dev/sr0", True)])
    monkeypatch.setattr(drive_registry, "snapshot_drives", lambda **kw: snapshots)

    makemkv_called = Mock()
    monkeypatch.setattr(u, "run_makemkv", makemkv_called)

    # ``reraise_if_registration_required`` is now a no-op; pass it to confirm
    # the signature still accepts the kwarg (call-site compatibility).
    out = u.startup_warm_drive_cache(reraise_if_registration_required=True)

    assert out == [("0", "/dev/sr0")]
    makemkv_called.assert_not_called()


def test_startup_warm_drive_cache_empty_when_no_loaded_drives(monkeypatch, reset_drive_scan):
    from core import drive_registry

    monkeypatch.setattr(drive_registry, "snapshot_drives", lambda **kw: [])

    assert u.startup_warm_drive_cache() == []


def test_parse_drv_skips_empty_volume_label(monkeypatch, reset_drive_scan):
    monkeypatch.setattr(u, "_drive_has_media", Mock(return_value=True))
    sample = (
        'DRV:0,2,999,12,"BD-RE ASUS","THE_MOVIE","/dev/sr2"\n'
        'DRV:1,0,999,0,"QEMU DVD-ROM","","/dev/sr0"\n'
    )
    drives, hw = u._parse_drv_output_for_loaded_discs(sample)
    assert drives == [("0", "/dev/sr2")]
    assert hw == {"/dev/sr2": "BD-RE ASUS"}


def test_upsert_makemkv_drive_cache_for_mount_replaces_stale_index(monkeypatch, reset_drive_scan):
    u._last_drive_scan["drives"] = [("2", "/dev/sr1"), ("0", "/dev/sr0")]
    u._last_drive_scan["drive_hardware"] = {"/dev/sr1": "OLD", "/dev/sr0": "ATAPI"}
    u.upsert_makemkv_drive_cache_for_mount("/dev/sr1", "0", "USBDVD")
    assert u._last_drive_scan["drives"] == [("0", "/dev/sr1"), ("0", "/dev/sr0")]
    assert u._last_drive_scan["drive_hardware"]["/dev/sr1"] == "USBDVD"
    assert u._last_drive_scan["drive_hardware"]["/dev/sr0"] == "ATAPI"


def test_ensure_makemkv_index_for_mount_refresh_enumeration_first(monkeypatch, reset_drive_scan):
    u._last_drive_scan["drives"] = [("9", "/dev/sr1")]
    calls: list[str] = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        return 'DRV:0,0,256,1,"BD-RE","VOL","/dev/sr1"\n', 1

    monkeypatch.setattr(u, "run_makemkv", fake_run)
    out = u.ensure_makemkv_index_for_mount("/dev/sr1", refresh_enumeration_first=True)
    assert out == "0"
    assert len(calls) >= 1
    assert any("disc:9999" in c for c in calls)
    assert u.makemkv_index_for_mount("/dev/sr1") == "0"


def test_get_drives_delegates_to_registry_no_makemkv(monkeypatch, reset_drive_scan):
    """After #562 PR 2, ``get_drives()`` returns OS-level snapshots and must
    never invoke MakeMKV — that was the contention root cause."""

    from core import drive_registry
    from core.drive_identity import DriveIdentity
    from core.drive_registry import DriveSnapshot

    def _id(serial):
        return DriveIdentity(
            by_id_serial=serial, vendor="V", model="M", bus="b",
            by_id_name="", hardware_name=None, identity_source="by-id",
        )

    snapshots = [
        DriveSnapshot(
            mount_point="/dev/sr1", loaded=True, volume_label="MY_DISC",
            media_kind="BD", identity=_id("S1"), udev_state={}, observed_at=0.0,
        ),
        DriveSnapshot(
            mount_point="/dev/sr0", loaded=True, volume_label="OTHER",
            media_kind="DVD", identity=_id("S0"), udev_state={}, observed_at=0.0,
        ),
        DriveSnapshot(
            mount_point="/dev/sr2", loaded=False, volume_label=None,
            media_kind=None, identity=_id("S2"), udev_state={}, observed_at=0.0,
        ),
    ]

    monkeypatch.setattr(drive_registry, "snapshot_drives", lambda **kw: snapshots)

    makemkv_called = Mock()
    monkeypatch.setattr(u, "run_makemkv", makemkv_called)

    out = u.get_drives()

    # Sorted by mount_point, loaded-only, synthesized ordinals.
    assert out == [("0", "/dev/sr0"), ("1", "/dev/sr1")]
    makemkv_called.assert_not_called()


def test_startup_enumerate_and_rescan_calls_handle_disc_insert_per_drive(monkeypatch, reset_drive_scan):
    drives = [("0", "/dev/sr0"), ("1", "/dev/sr1")]
    mock_warm = Mock(return_value=drives)
    mock_insert = Mock(return_value={"status": "ok"})
    monkeypatch.delenv("MKVAUTO_SKIP_STARTUP_DISC_RESCAN", raising=False)
    monkeypatch.setattr(startup_discs, "startup_warm_drive_cache", mock_warm)
    monkeypatch.setattr("core._drive_operations.handle_disc_insert", mock_insert)

    out = startup_discs.startup_enumerate_and_rescan_loaded_discs()
    assert out == drives
    mock_warm.assert_called_once()
    assert mock_insert.call_count == 2
    mock_insert.assert_any_call("0", "/dev/sr0")
    mock_insert.assert_any_call("1", "/dev/sr1")


def test_startup_enumerate_and_rescan_skips_insert_when_env_set(monkeypatch, reset_drive_scan):
    monkeypatch.setenv("MKVAUTO_SKIP_STARTUP_DISC_RESCAN", "1")
    mock_warm = Mock(return_value=[("0", "/dev/sr0")])
    mock_insert = Mock()
    monkeypatch.setattr(startup_discs, "startup_warm_drive_cache", mock_warm)
    monkeypatch.setattr("core._drive_operations.handle_disc_insert", mock_insert)

    assert startup_discs.startup_enumerate_and_rescan_loaded_discs() == [("0", "/dev/sr0")]
    mock_insert.assert_not_called()


def test_startup_enumerate_and_rescan_continues_after_http_exception(monkeypatch, reset_drive_scan):
    monkeypatch.delenv("MKVAUTO_SKIP_STARTUP_DISC_RESCAN", raising=False)
    monkeypatch.setattr(
        startup_discs,
        "startup_warm_drive_cache",
        Mock(return_value=[("0", "/dev/sr0"), ("1", "/dev/sr1")]),
    )
    mock_insert = Mock(
        side_effect=[
            HTTPException(status_code=500, detail="first failed"),
            {"status": "ok"},
        ]
    )
    monkeypatch.setattr("core._drive_operations.handle_disc_insert", mock_insert)

    out = startup_discs.startup_enumerate_and_rescan_loaded_discs()
    assert len(out) == 2
    assert mock_insert.call_count == 2


def test_startup_rescan_serial_env_skips_thread_pool(monkeypatch, reset_drive_scan):
    monkeypatch.setenv("MKVAUTO_STARTUP_DISC_RESCAN_SERIAL", "1")
    monkeypatch.delenv("MKVAUTO_SKIP_STARTUP_DISC_RESCAN", raising=False)

    def boom(*_a, **_kw):
        raise AssertionError("ThreadPoolExecutor must not be used in serial mode")

    monkeypatch.setattr(startup_discs, "ThreadPoolExecutor", boom)
    mock_insert = Mock(return_value={"status": "ok"})
    monkeypatch.setattr(
        startup_discs,
        "startup_warm_drive_cache",
        Mock(return_value=[("0", "/dev/sr0"), ("1", "/dev/sr1")]),
    )
    monkeypatch.setattr("core._drive_operations.handle_disc_insert", mock_insert)

    startup_discs.startup_enumerate_and_rescan_loaded_discs()
    assert mock_insert.call_count == 2


def test_startup_rescan_parallel_uses_thread_pool(monkeypatch, reset_drive_scan):
    monkeypatch.delenv("MKVAUTO_SKIP_STARTUP_DISC_RESCAN", raising=False)
    monkeypatch.delenv("MKVAUTO_STARTUP_DISC_RESCAN_SERIAL", raising=False)

    from concurrent.futures import ThreadPoolExecutor as RealTPE

    created: list = []
    real_tpe = RealTPE

    def recording_tpe(*args, **kwargs):
        created.append(kwargs.get("max_workers"))
        return real_tpe(*args, **kwargs)

    monkeypatch.setattr(startup_discs, "ThreadPoolExecutor", recording_tpe)
    mock_insert = Mock(return_value={"status": "ok"})
    monkeypatch.setattr(
        startup_discs,
        "startup_warm_drive_cache",
        Mock(return_value=[("0", "/dev/sr0"), ("1", "/dev/sr1")]),
    )
    monkeypatch.setattr("core._drive_operations.handle_disc_insert", mock_insert)

    startup_discs.startup_enumerate_and_rescan_loaded_discs()
    assert mock_insert.call_count == 2
    assert created == [2]


def test_startup_rescan_single_drive_no_thread_pool(monkeypatch, reset_drive_scan):
    monkeypatch.delenv("MKVAUTO_SKIP_STARTUP_DISC_RESCAN", raising=False)
    monkeypatch.delenv("MKVAUTO_STARTUP_DISC_RESCAN_SERIAL", raising=False)

    def boom(*_a, **_kw):
        raise AssertionError("ThreadPoolExecutor must not be used for a single drive")

    monkeypatch.setattr(startup_discs, "ThreadPoolExecutor", boom)
    mock_insert = Mock(return_value={"status": "ok"})
    monkeypatch.setattr(
        startup_discs,
        "startup_warm_drive_cache",
        Mock(return_value=[("0", "/dev/sr0")]),
    )
    monkeypatch.setattr("core._drive_operations.handle_disc_insert", mock_insert)

    startup_discs.startup_enumerate_and_rescan_loaded_discs()
    mock_insert.assert_called_once_with("0", "/dev/sr0")


def test_startup_rescan_defers_drive_with_active_rip(monkeypatch, reset_drive_scan):
    """#562 PR 3: ``_recover_inflight_jobs`` restarts rips before the
    deferred startup warmup runs. If a drive's rip is already in flight,
    its per-disc ``info dev:`` scan would race with the live ``mkv dev:``
    and emit MSG:5010 — defer that drive's scan; let the other drive's
    insert handler run normally."""

    monkeypatch.delenv("MKVAUTO_SKIP_STARTUP_DISC_RESCAN", raising=False)
    monkeypatch.setenv("MKVAUTO_STARTUP_DISC_RESCAN_SERIAL", "1")

    monkeypatch.setattr(
        startup_discs,
        "startup_warm_drive_cache",
        Mock(return_value=[("0", "/dev/sr0"), ("1", "/dev/sr1")]),
    )

    busy_mounts = {"/dev/sr0"}
    monkeypatch.setattr(
        startup_discs,
        "_rip_active_at_mount",
        lambda mp: mp in busy_mounts,
    )

    mock_insert = Mock(return_value={"status": "ok"})
    monkeypatch.setattr("core._drive_operations.handle_disc_insert", mock_insert)

    startup_discs.startup_enumerate_and_rescan_loaded_discs()

    # /dev/sr0 was deferred (rip in progress); /dev/sr1 ran.
    mock_insert.assert_called_once_with("1", "/dev/sr1")
