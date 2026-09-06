"""Tests for ``core.drive_registry``.

Covers the OS-level drive snapshot facade introduced by GitHub issue #562
PR 1: enumeration shape, loaded-flag wiring, udev property parsing,
TTL + force + invalidate cache semantics, concurrent-caller coalescing,
identity integration, and graceful degradation when udev is unavailable.

All tests monkeypatch the four module-level indirection points
(``_enumerate_devices``, ``_media_present``, ``_resolve_identity``,
``_run_udevadm``) so the suite never touches real ``/dev``, ``/sys``, or
the ``udevadm`` binary.
"""

from __future__ import annotations

import threading
import time

import pytest

from core import drive_registry
from core.drive_identity import DriveIdentity
from core.drive_registry import (
    DriveSnapshot,
    get_snapshot_for_mount,
    invalidate,
    loaded_drives,
    snapshot_drives,
)


def _id(serial: str = "SERIAL", source: str = "by-id") -> DriveIdentity:
    return DriveIdentity(
        by_id_serial=serial,
        vendor="V",
        model="M",
        bus="b",
        by_id_name=f"usb-V_M_{serial}-0:0",
        hardware_name=None,
        identity_source=source,  # type: ignore[arg-type]
    )


@pytest.fixture(autouse=True)
def _reset_cache():
    invalidate()
    yield
    invalidate()


def _patch_baseline(
    monkeypatch,
    *,
    devices: list[str],
    loaded: dict[str, bool] | None = None,
    identity: DriveIdentity | None = None,
    udev: dict[str, str] | None = None,
):
    """Install simple deterministic stand-ins for the four indirection points."""

    loaded = loaded or {dev: True for dev in devices}
    udev = udev or {}
    identity_obj = identity or _id()

    monkeypatch.setattr(drive_registry, "_enumerate_devices", lambda: list(devices))
    monkeypatch.setattr(drive_registry, "_media_present", lambda dev: loaded[dev])
    monkeypatch.setattr(drive_registry, "_resolve_identity", lambda dev: identity_obj)
    monkeypatch.setattr(drive_registry, "_run_udevadm", lambda dev: dict(udev))


class TestSnapshotShape:
    def test_returns_one_snapshot_per_device(self, monkeypatch):
        _patch_baseline(monkeypatch, devices=["/dev/sr0", "/dev/sr1"])

        snaps = snapshot_drives(force=True)

        assert [s.mount_point for s in snaps] == ["/dev/sr0", "/dev/sr1"]
        assert all(isinstance(s, DriveSnapshot) for s in snaps)

    def test_empty_when_no_devices(self, monkeypatch):
        _patch_baseline(monkeypatch, devices=[])

        assert snapshot_drives(force=True) == []
        assert loaded_drives() == []


class TestLoadedFlag:
    def test_returns_loaded_only_when_filtered(self, monkeypatch):
        _patch_baseline(
            monkeypatch,
            devices=["/dev/sr0", "/dev/sr1"],
            loaded={"/dev/sr0": True, "/dev/sr1": False},
        )

        all_snaps = snapshot_drives(force=True)
        only_loaded = loaded_drives()

        assert [s.loaded for s in all_snaps] == [True, False]
        assert [s.mount_point for s in only_loaded] == ["/dev/sr0"]


class TestUdevParsing:
    def test_parses_udevadm_volume_label_and_bd_media_kind(self, monkeypatch):
        udev = {
            "ID_FS_LABEL": "VENOM_2018",
            "ID_CDROM_MEDIA_BD": "1",
            "ID_CDROM_MEDIA": "1",
        }
        _patch_baseline(monkeypatch, devices=["/dev/sr0"], udev=udev)

        snap = snapshot_drives(force=True)[0]

        assert snap.volume_label == "VENOM_2018"
        assert snap.media_kind == "BD"
        assert snap.udev_state == udev

    def test_dvd_media_kind(self, monkeypatch):
        _patch_baseline(
            monkeypatch,
            devices=["/dev/sr0"],
            udev={"ID_CDROM_MEDIA_DVD": "1", "ID_CDROM_MEDIA": "1"},
        )
        assert snapshot_drives(force=True)[0].media_kind == "DVD"

    def test_cd_media_kind(self, monkeypatch):
        _patch_baseline(
            monkeypatch,
            devices=["/dev/sr0"],
            udev={"ID_CDROM_MEDIA_CD": "1", "ID_CDROM_MEDIA": "1"},
        )
        assert snapshot_drives(force=True)[0].media_kind == "CD"

    def test_unknown_media_kind_when_media_flag_only(self, monkeypatch):
        _patch_baseline(
            monkeypatch,
            devices=["/dev/sr0"],
            udev={"ID_CDROM_MEDIA": "1"},
        )
        assert snapshot_drives(force=True)[0].media_kind == "unknown"

    def test_fs_label_enc_used_when_id_fs_label_absent(self, monkeypatch):
        _patch_baseline(
            monkeypatch,
            devices=["/dev/sr0"],
            udev={"ID_FS_LABEL_ENC": "Encoded\\x20Label"},
        )
        assert snapshot_drives(force=True)[0].volume_label == "Encoded\\x20Label"

    def test_no_media_no_label_no_kind(self, monkeypatch):
        _patch_baseline(
            monkeypatch,
            devices=["/dev/sr0"],
            loaded={"/dev/sr0": False},
        )
        snap = snapshot_drives(force=True)[0]
        assert snap.loaded is False
        assert snap.volume_label is None
        assert snap.media_kind is None


class TestCacheBehavior:
    def test_ttl_cache_returns_same_list_under_ttl(self, monkeypatch):
        calls = {"build": 0}

        def fake_enum():
            calls["build"] += 1
            return ["/dev/sr0"]

        monkeypatch.setattr(drive_registry, "_enumerate_devices", fake_enum)
        monkeypatch.setattr(drive_registry, "_media_present", lambda dev: True)
        monkeypatch.setattr(drive_registry, "_resolve_identity", lambda dev: _id())
        monkeypatch.setattr(drive_registry, "_run_udevadm", lambda dev: {})

        snapshot_drives(ttl_seconds=10.0)
        snapshot_drives(ttl_seconds=10.0)
        snapshot_drives(ttl_seconds=10.0)

        assert calls["build"] == 1

    def test_force_bypasses_cache(self, monkeypatch):
        calls = {"build": 0}

        def fake_enum():
            calls["build"] += 1
            return ["/dev/sr0"]

        monkeypatch.setattr(drive_registry, "_enumerate_devices", fake_enum)
        monkeypatch.setattr(drive_registry, "_media_present", lambda dev: True)
        monkeypatch.setattr(drive_registry, "_resolve_identity", lambda dev: _id())
        monkeypatch.setattr(drive_registry, "_run_udevadm", lambda dev: {})

        snapshot_drives(ttl_seconds=10.0)
        snapshot_drives(force=True, ttl_seconds=10.0)

        assert calls["build"] == 2

    def test_invalidate_forces_rescan(self, monkeypatch):
        calls = {"build": 0}

        def fake_enum():
            calls["build"] += 1
            return ["/dev/sr0"]

        monkeypatch.setattr(drive_registry, "_enumerate_devices", fake_enum)
        monkeypatch.setattr(drive_registry, "_media_present", lambda dev: True)
        monkeypatch.setattr(drive_registry, "_resolve_identity", lambda dev: _id())
        monkeypatch.setattr(drive_registry, "_run_udevadm", lambda dev: {})

        snapshot_drives(ttl_seconds=10.0)
        invalidate()
        snapshot_drives(ttl_seconds=10.0)

        assert calls["build"] == 2

    def test_ttl_expiry_triggers_rescan(self, monkeypatch):
        calls = {"build": 0}

        def fake_enum():
            calls["build"] += 1
            return ["/dev/sr0"]

        monkeypatch.setattr(drive_registry, "_enumerate_devices", fake_enum)
        monkeypatch.setattr(drive_registry, "_media_present", lambda dev: True)
        monkeypatch.setattr(drive_registry, "_resolve_identity", lambda dev: _id())
        monkeypatch.setattr(drive_registry, "_run_udevadm", lambda dev: {})

        snapshot_drives(ttl_seconds=0.001)
        time.sleep(0.02)
        snapshot_drives(ttl_seconds=0.001)

        assert calls["build"] == 2

    def test_returned_list_is_a_copy_not_shared_cache_reference(self, monkeypatch):
        """Callers must not be able to mutate the cached list."""
        _patch_baseline(monkeypatch, devices=["/dev/sr0", "/dev/sr1"])

        first = snapshot_drives(force=True)
        first.clear()
        second = snapshot_drives(ttl_seconds=10.0)

        assert len(second) == 2


class TestConcurrency:
    def test_concurrent_callers_coalesce(self, monkeypatch):
        """Four threads racing into snapshot_drives() share one underlying
        build call when the lock serializes them through a populated cache."""
        calls = {"build": 0}
        gate = threading.Event()

        def slow_enum():
            calls["build"] += 1
            # Only the first call blocks; later callers see the cache.
            if calls["build"] == 1:
                gate.wait(timeout=2.0)
            return ["/dev/sr0"]

        monkeypatch.setattr(drive_registry, "_enumerate_devices", slow_enum)
        monkeypatch.setattr(drive_registry, "_media_present", lambda dev: True)
        monkeypatch.setattr(drive_registry, "_resolve_identity", lambda dev: _id())
        monkeypatch.setattr(drive_registry, "_run_udevadm", lambda dev: {})

        results: list[list[DriveSnapshot]] = []

        def worker():
            results.append(snapshot_drives(ttl_seconds=10.0))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(0.05)
        gate.set()
        for t in threads:
            t.join(timeout=3.0)

        assert calls["build"] == 1
        assert len(results) == 4
        assert all(r and r[0].mount_point == "/dev/sr0" for r in results)


class TestIdentityIntegration:
    def test_snapshot_drives_identity_uses_resolve_drive_identity(self, monkeypatch):
        called_with: list[str] = []

        def fake_resolver(dev):
            called_with.append(dev)
            return _id(serial="PIONEER-1958040110900395")

        monkeypatch.setattr(drive_registry, "_enumerate_devices", lambda: ["/dev/sr0"])
        monkeypatch.setattr(drive_registry, "_media_present", lambda dev: True)
        monkeypatch.setattr(drive_registry, "_resolve_identity", fake_resolver)
        monkeypatch.setattr(drive_registry, "_run_udevadm", lambda dev: {})

        snap = snapshot_drives(force=True)[0]

        assert called_with == ["/dev/sr0"]
        assert snap.identity.by_id_serial == "PIONEER-1958040110900395"
        assert snap.identity.identity_source == "by-id"

    def test_identity_failure_falls_through_to_unknown_without_aborting(
        self, monkeypatch
    ):
        def boom(dev):
            raise RuntimeError("simulated identity-layer fault")

        monkeypatch.setattr(drive_registry, "_enumerate_devices", lambda: ["/dev/sr0"])
        monkeypatch.setattr(drive_registry, "_media_present", lambda dev: True)
        monkeypatch.setattr(drive_registry, "_resolve_identity", boom)
        monkeypatch.setattr(drive_registry, "_run_udevadm", lambda dev: {})

        snap = snapshot_drives(force=True)[0]

        assert snap.identity.identity_source == "unknown"
        assert snap.identity.by_id_serial == "unknown:sr0"


class TestUdevFailureGraceful:
    def test_snapshot_drives_handles_udevadm_failure_gracefully(self, monkeypatch):
        """Empty udev result is allowed — snapshot still builds with None
        label/kind and the rest of the data intact."""
        _patch_baseline(monkeypatch, devices=["/dev/sr0"], udev={})

        snap = snapshot_drives(force=True)[0]

        assert snap.volume_label is None
        assert snap.media_kind is None
        assert snap.udev_state == {}
        assert snap.loaded is True  # other fields unaffected by udev failure

    def test_run_udevadm_returns_empty_when_binary_missing(self, monkeypatch):
        def fake_run(*a, **k):
            raise FileNotFoundError("udevadm not installed")

        monkeypatch.setattr("core.drive_registry.subprocess.run", fake_run)

        assert drive_registry._run_udevadm("/dev/sr0") == {}

    def test_run_udevadm_returns_empty_on_timeout(self, monkeypatch):
        import subprocess as _sp

        def fake_run(*a, **k):
            raise _sp.TimeoutExpired(cmd="udevadm", timeout=2.0)

        monkeypatch.setattr("core.drive_registry.subprocess.run", fake_run)

        assert drive_registry._run_udevadm("/dev/sr0") == {}

    def test_run_udevadm_returns_empty_on_nonzero_returncode(self, monkeypatch):
        class FakeProc:
            returncode = 4
            stdout = ""

        monkeypatch.setattr(
            "core.drive_registry.subprocess.run", lambda *a, **k: FakeProc()
        )

        assert drive_registry._run_udevadm("/dev/sr0") == {}

    def test_run_udevadm_parses_property_output(self, monkeypatch):
        """``udevadm info -q property -n /dev/sr0`` emits one KEY=value per line."""

        class FakeProc:
            returncode = 0
            stdout = (
                "ID_FS_LABEL=VENOM_2018\n"
                "ID_CDROM_MEDIA_BD=1\n"
                "ID_CDROM_MEDIA=1\n"
                "DEVNAME=/dev/sr0\n"
                "\n"  # blank line — must be skipped
                "MALFORMED_LINE_NO_EQUALS\n"
            )

        monkeypatch.setattr(
            "core.drive_registry.subprocess.run", lambda *a, **k: FakeProc()
        )

        assert drive_registry._run_udevadm("/dev/sr0") == {
            "ID_FS_LABEL": "VENOM_2018",
            "ID_CDROM_MEDIA_BD": "1",
            "ID_CDROM_MEDIA": "1",
            "DEVNAME": "/dev/sr0",
        }


class TestGetSnapshotForMount:
    def test_returns_matching_snapshot(self, monkeypatch):
        _patch_baseline(monkeypatch, devices=["/dev/sr0", "/dev/sr1"])

        snap = get_snapshot_for_mount("/dev/sr1")

        assert snap is not None
        assert snap.mount_point == "/dev/sr1"

    def test_returns_none_for_missing_mount(self, monkeypatch):
        _patch_baseline(monkeypatch, devices=["/dev/sr0"])

        assert get_snapshot_for_mount("/dev/sr99") is None


# ---- #862: probe deadline + not-responding cooldown ----------------------

@pytest.fixture
def _reset_wedge_state(monkeypatch):
    """Isolate the per-device cooldown/last-known maps and shrink the deadline."""
    monkeypatch.setattr(drive_registry, "_unresponsive_until", {})
    monkeypatch.setattr(drive_registry, "_last_known", {})
    monkeypatch.setattr(drive_registry, "_PROBE_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(drive_registry, "_PROBE_COOLDOWN_SECONDS", 30.0)


def test_wedged_probe_returns_within_deadline(monkeypatch, _reset_wedge_state):
    """A D-state-style stuck probe must never block the caller — the 2026-09-06
    outage pinned every uvicorn worker behind exactly this call."""
    monkeypatch.setattr(drive_registry, "_enumerate_devices", lambda: ["/dev/sr0"])

    def _stuck(dev):
        time.sleep(5)  # stands in for an unkillable sg_turs
        return True

    monkeypatch.setattr(drive_registry, "_media_present", _stuck)
    monkeypatch.setattr(drive_registry, "_resolve_identity", lambda d: _id())
    monkeypatch.setattr(drive_registry, "_run_udevadm", lambda d: {})

    start = time.monotonic()
    snaps = snapshot_drives(force=True)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"snapshot blocked for {elapsed:.1f}s"
    assert len(snaps) == 1
    # Not-responding fallback: present but never claiming media.
    assert snaps[0].mount_point == "/dev/sr0"
    assert snaps[0].loaded is False


def test_cooldown_skips_hardware_until_expiry(monkeypatch, _reset_wedge_state):
    monkeypatch.setattr(drive_registry, "_enumerate_devices", lambda: ["/dev/sr0"])
    calls = {"n": 0}

    def _stuck(dev):
        calls["n"] += 1
        time.sleep(5)
        return True

    monkeypatch.setattr(drive_registry, "_media_present", _stuck)
    monkeypatch.setattr(drive_registry, "_resolve_identity", lambda d: _id())
    monkeypatch.setattr(drive_registry, "_run_udevadm", lambda d: {})

    snapshot_drives(force=True)
    assert calls["n"] == 1
    # Within the cooldown the hardware is never touched again...
    invalidate()
    drive_registry._unresponsive_until["/dev/sr0"] = time.monotonic() + 30
    snaps = snapshot_drives()
    assert calls["n"] == 1
    assert snaps[0].loaded is False
    # ...but a udev-driven force retries immediately (the event means the
    # drive talked), and a healthy probe repopulates last-known state.
    monkeypatch.setattr(drive_registry, "_media_present", lambda d: True)
    snaps = snapshot_drives(force=True)
    assert snaps[0].loaded is True
    assert drive_registry._unresponsive_until == {}


def test_not_responding_keeps_last_known_identity(monkeypatch, _reset_wedge_state):
    monkeypatch.setattr(drive_registry, "_enumerate_devices", lambda: ["/dev/sr0"])
    monkeypatch.setattr(drive_registry, "_media_present", lambda d: True)
    monkeypatch.setattr(drive_registry, "_resolve_identity", lambda d: _id("PIONEER123"))
    monkeypatch.setattr(drive_registry, "_run_udevadm", lambda d: {"ID_FS_LABEL": "MOVIE"})
    snaps = snapshot_drives(force=True)
    assert snaps[0].identity.by_id_serial == "PIONEER123"

    def _stuck(dev):
        time.sleep(5)
        return True

    monkeypatch.setattr(drive_registry, "_media_present", _stuck)
    invalidate()
    snaps = snapshot_drives(force=True)
    # Identity survives (cards stay tellable-apart); media claim does not.
    assert snaps[0].identity.by_id_serial == "PIONEER123"
    assert snaps[0].loaded is False
    assert snaps[0].volume_label is None
