"""Tests for ``core.usb_bus_saturation_policy`` (#578).

The policy fires when the target rip would result in 2+ active rips
on a sub-SuperSpeed USB bus. Each test stubs the three signals the
policy reads — ``bus_for_mount_point``, ``detect_optical_drives``, and
the active-rip query — so behavior is decoupled from sysfs + DB.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core import usb_bus_saturation_policy as pol
from core.usb_topology import OpticalDrive


def _mk_drive(bus: int, speed: int = 480) -> OpticalDrive:
    return OpticalDrive(
        bus=bus, speed_mbps=speed,
        product="X", manufacturer="", serial=f"S{bus}",
        sysfs_path="",
    )


def _mk_active_job(mount_point: str, rip_state: str = "running"):
    job = MagicMock()
    job.mount_point = mount_point
    job.rip_state = rip_state
    return job


@pytest.fixture
def db_with_active_rips(monkeypatch):
    """Factory: returns a fake Session whose query returns the given list."""
    def _make(active_mount_points: list[str]):
        session = MagicMock()
        session.query.return_value.filter.return_value.all.return_value = [
            _mk_active_job(mp) for mp in active_mount_points
        ]
        return session
    return _make


@pytest.fixture
def stub_bus_resolver(monkeypatch):
    """Factory: stub bus_for_mount_point to return a mapping."""
    def _make(mapping: dict[str, int | None]):
        monkeypatch.setattr(
            pol, "bus_for_mount_point",
            lambda mp, **kw: mapping.get(mp),
        )
    return _make


@pytest.fixture
def stub_topology_drives(monkeypatch):
    """Factory: stub detect_optical_drives to return the given list."""
    def _make(drives: list[OpticalDrive]):
        monkeypatch.setattr(pol, "detect_optical_drives", lambda: drives)
    return _make


class TestAllowed:
    def test_no_usb_bus_resolution_allows(self, stub_bus_resolver, db_with_active_rips):
        """ATAPI / SATA optical drives don't have a USB bus — fail open."""
        stub_bus_resolver({"/dev/sr0": None})
        d = pol.evaluate_bus_saturation("/dev/sr0", db_with_active_rips([]))
        assert d.allowed is True
        assert d.code is None

    def test_superspeed_bus_allows(self, stub_bus_resolver, stub_topology_drives, db_with_active_rips):
        stub_bus_resolver({"/dev/sr0": 3, "/dev/sr1": 3})
        stub_topology_drives([_mk_drive(3, 5000), _mk_drive(3, 5000)])
        # Even with a sibling rip in flight, SuperSpeed bus has the headroom.
        d = pol.evaluate_bus_saturation("/dev/sr0", db_with_active_rips(["/dev/sr1"]))
        assert d.allowed is True

    def test_no_competing_rip_allows(self, stub_bus_resolver, stub_topology_drives, db_with_active_rips):
        stub_bus_resolver({"/dev/sr0": 2, "/dev/sr1": 2})
        stub_topology_drives([_mk_drive(2, 480), _mk_drive(2, 480)])
        # Target drive on Bus 2 (480 Mbps), sibling exists on same bus,
        # but no active rip running on it.
        d = pol.evaluate_bus_saturation("/dev/sr0", db_with_active_rips([]))
        assert d.allowed is True

    def test_competing_rip_on_different_bus_allows(
        self, stub_bus_resolver, stub_topology_drives, db_with_active_rips
    ):
        stub_bus_resolver({"/dev/sr0": 2, "/dev/sr1": 4})
        stub_topology_drives([_mk_drive(2, 480), _mk_drive(4, 480)])
        d = pol.evaluate_bus_saturation("/dev/sr0", db_with_active_rips(["/dev/sr1"]))
        assert d.allowed is True

    def test_force_override_allows(
        self, stub_bus_resolver, stub_topology_drives, db_with_active_rips
    ):
        """The user explicitly clicked through the contention warning."""
        stub_bus_resolver({"/dev/sr0": 2, "/dev/sr1": 2})
        stub_topology_drives([_mk_drive(2, 480), _mk_drive(2, 480)])
        d = pol.evaluate_bus_saturation(
            "/dev/sr0", db_with_active_rips(["/dev/sr1"]),
            force_override=True,
        )
        assert d.allowed is True

    def test_db_query_failure_fails_open(self, stub_bus_resolver, stub_topology_drives, monkeypatch):
        """If the DB itself errors, the gate must not block — the user has
        already been warned via the Settings page."""
        stub_bus_resolver({"/dev/sr0": 2})
        stub_topology_drives([_mk_drive(2, 480)])

        broken_session = MagicMock()
        broken_session.query.side_effect = RuntimeError("DB unavailable")

        d = pol.evaluate_bus_saturation("/dev/sr0", broken_session)
        assert d.allowed is True


class TestRefused:
    def test_concurrent_rip_on_usb_2_bus_refuses(
        self, stub_bus_resolver, stub_topology_drives, db_with_active_rips
    ):
        stub_bus_resolver({"/dev/sr0": 2, "/dev/sr1": 2})
        stub_topology_drives([_mk_drive(2, 480), _mk_drive(2, 480)])

        d = pol.evaluate_bus_saturation(
            "/dev/sr0", db_with_active_rips(["/dev/sr1"]),
        )

        assert d.allowed is False
        assert d.code == "usb_bus_saturation_risk"
        assert d.bus == 2
        assert d.speed_mbps == 480
        assert d.competing_mount_points == ("/dev/sr1",)
        assert "USB Bus 2" in (d.message or "")
        assert "480 Mbps" in (d.message or "")

    def test_409_payload_shape(
        self, stub_bus_resolver, stub_topology_drives, db_with_active_rips
    ):
        stub_bus_resolver({"/dev/sr0": 2, "/dev/sr1": 2})
        stub_topology_drives([_mk_drive(2, 480), _mk_drive(2, 480)])
        d = pol.evaluate_bus_saturation("/dev/sr0", db_with_active_rips(["/dev/sr1"]))

        payload = d.to_409_payload()

        assert payload["code"] == "usb_bus_saturation_risk"
        assert payload["bus"] == 2
        assert payload["speed_mbps"] == 480
        assert payload["competing_mount_points"] == ["/dev/sr1"]
        assert "USB Bus 2" in payload["message"]
        # The frontend uses ``override_field`` to know which flag to flip
        # on retry — pinned name keeps it stable across renames.
        assert payload["override_field"] == "force_concurrent_on_saturated_bus"

    def test_target_rip_itself_does_not_count_as_competing(
        self, stub_bus_resolver, stub_topology_drives, db_with_active_rips
    ):
        """A re-dispatch of the same rip request shouldn't trigger the gate
        on its own mount_point — only OTHER active rips matter."""
        stub_bus_resolver({"/dev/sr0": 2})
        stub_topology_drives([_mk_drive(2, 480)])
        # The DB query returns the active job on /dev/sr0 (the target itself).
        d = pol.evaluate_bus_saturation("/dev/sr0", db_with_active_rips(["/dev/sr0"]))
        assert d.allowed is True

    def test_three_drives_one_bus_lists_all_competitors(
        self, stub_bus_resolver, stub_topology_drives, db_with_active_rips
    ):
        stub_bus_resolver({"/dev/sr0": 2, "/dev/sr1": 2, "/dev/sr2": 2})
        stub_topology_drives([_mk_drive(2, 480)] * 3)

        d = pol.evaluate_bus_saturation(
            "/dev/sr0",
            db_with_active_rips(["/dev/sr1", "/dev/sr2"]),
        )

        assert d.allowed is False
        assert set(d.competing_mount_points) == {"/dev/sr1", "/dev/sr2"}
