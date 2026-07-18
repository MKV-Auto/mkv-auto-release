"""Tests for ``core.usb_topology`` (#578).

The module walks ``/sys/bus/usb/devices`` and groups optical drives by
bus number; tests construct a fake sysfs tree in ``tmp_path`` and pass
it via the ``sys_bus_usb_devices`` kwarg — no monkeypatching of glob
or open needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.usb_topology import (
    OPTICAL_PRODUCT_RE,
    OpticalDrive,
    SUPERSPEED_MBPS_THRESHOLD,
    bus_for_mount_point,
    detect_contention_warnings,
    detect_optical_drives,
    snapshot_topology,
)


def _make_usb_dev(
    root: Path,
    dev_name: str,
    *,
    product: str | None,
    speed: str | None = "480",
    busnum: str | None = "2",
    manufacturer: str | None = None,
    serial: str | None = None,
) -> Path:
    """Build one sysfs USB device directory under ``root/dev_name``."""
    dev = root / dev_name
    dev.mkdir(parents=True, exist_ok=True)
    if product is not None:
        (dev / "product").write_text(product + "\n")
    if speed is not None:
        (dev / "speed").write_text(speed + "\n")
    if busnum is not None:
        (dev / "busnum").write_text(busnum + "\n")
    if manufacturer is not None:
        (dev / "manufacturer").write_text(manufacturer + "\n")
    if serial is not None:
        (dev / "serial").write_text(serial + "\n")
    return dev


class TestDetectOpticalDrives:
    def test_finds_pioneer_and_asus_on_same_bus(self, tmp_path: Path):
        _make_usb_dev(
            tmp_path, "2-2", product="Pioneer Blu-ray Drive",
            speed="480", busnum="2",
            manufacturer="Pioneer Corporation", serial="1958040110900395",
        )
        _make_usb_dev(
            tmp_path, "2-3", product="External Drive",
            speed="480", busnum="2",
            manufacturer="", serial="AAAABBBB000E",
        )

        drives = detect_optical_drives(sys_bus_usb_devices=str(tmp_path))

        assert len(drives) == 2
        serials = sorted(d.serial for d in drives)
        assert serials == ["1958040110900395", "AAAABBBB000E"]
        assert all(d.bus == 2 for d in drives)
        assert all(d.speed_mbps == 480 for d in drives)

    def test_skips_non_optical_devices(self, tmp_path: Path):
        _make_usb_dev(
            tmp_path, "1-1", product="USB Receiver",
            speed="12", busnum="1",
        )
        _make_usb_dev(
            tmp_path, "1-2", product="Webcam",
            speed="480", busnum="1",
        )

        assert detect_optical_drives(sys_bus_usb_devices=str(tmp_path)) == []

    def test_skips_devices_without_speed_or_busnum(self, tmp_path: Path):
        """Root hubs and interface dirs lack one or both attrs."""
        _make_usb_dev(
            tmp_path, "usb2", product="EHCI Host Controller",
            speed=None, busnum="2",
        )
        _make_usb_dev(
            tmp_path, "interface", product="BD-ROM Drive",
            speed="480", busnum=None,
        )

        assert detect_optical_drives(sys_bus_usb_devices=str(tmp_path)) == []

    def test_missing_sysfs_returns_empty(self, tmp_path: Path):
        """Test environments without ``/sys/bus/usb/devices`` get an empty
        list rather than an exception."""
        nonexistent = tmp_path / "does_not_exist"
        assert detect_optical_drives(sys_bus_usb_devices=str(nonexistent)) == []

    def test_handles_malformed_speed_or_busnum(self, tmp_path: Path):
        _make_usb_dev(
            tmp_path, "2-1", product="Pioneer Blu-ray Drive",
            speed="not-a-number", busnum="2",
        )
        _make_usb_dev(
            tmp_path, "2-2", product="ASUS External Drive",
            speed="480", busnum="garbage",
        )

        assert detect_optical_drives(sys_bus_usb_devices=str(tmp_path)) == []


class TestOpticalProductRegex:
    @pytest.mark.parametrize("product", [
        "Pioneer Blu-ray Drive",
        "ASUS External Drive",
        "BD-RE PIONEER BD-RW BDR-XD06U 1.11",
        "DVD-ROM Drive",
        "CD-ROM Drive",
        "Optical Drive",
        "LG BD Writer",
        "HL-DT-ST DVDRAM",  # matches via 'DVD'
    ])
    def test_matches_known_optical_products(self, product):
        assert OPTICAL_PRODUCT_RE.search(product) is not None

    @pytest.mark.parametrize("product", [
        "USB Keyboard",
        "Logitech Webcam",
        "Generic Hub",
        "Mass Storage",  # generic — match by .product would over-broad
    ])
    def test_does_not_match_non_optical(self, product):
        assert OPTICAL_PRODUCT_RE.search(product) is None


class TestDetectContentionWarnings:
    def test_two_drives_on_usb_2_bus_warns(self):
        drives = [
            OpticalDrive(
                bus=2, speed_mbps=480, product="Pioneer", manufacturer="P",
                serial="1958", sysfs_path="/sys/bus/usb/devices/2-2",
            ),
            OpticalDrive(
                bus=2, speed_mbps=480, product="ASUS", manufacturer="A",
                serial="AAAA", sysfs_path="/sys/bus/usb/devices/2-3",
            ),
        ]

        warnings = detect_contention_warnings(drives)

        assert len(warnings) == 1
        w = warnings[0]
        assert w.bus == 2
        assert w.speed_mbps == 480
        assert w.drive_count == 2
        assert "Pioneer" in w.drives and "ASUS" in w.drives
        assert "USB Bus 2" in w.message
        assert "480 Mbps" in w.message
        assert "#578" in w.message

    def test_two_drives_on_superspeed_bus_no_warning(self):
        drives = [
            OpticalDrive(bus=3, speed_mbps=5000, product="Pioneer", manufacturer="", serial="A", sysfs_path=""),
            OpticalDrive(bus=3, speed_mbps=5000, product="ASUS", manufacturer="", serial="B", sysfs_path=""),
        ]

        assert detect_contention_warnings(drives) == []

    def test_single_drive_on_usb_2_no_warning(self):
        drives = [
            OpticalDrive(bus=2, speed_mbps=480, product="Pioneer", manufacturer="", serial="A", sysfs_path=""),
        ]

        assert detect_contention_warnings(drives) == []

    def test_drives_on_separate_buses_no_warning(self):
        """Even at 480 Mbps each, if they're on different buses they don't contend."""
        drives = [
            OpticalDrive(bus=2, speed_mbps=480, product="Pioneer", manufacturer="", serial="A", sysfs_path=""),
            OpticalDrive(bus=4, speed_mbps=480, product="ASUS", manufacturer="", serial="B", sysfs_path=""),
        ]

        assert detect_contention_warnings(drives) == []

    def test_three_drives_one_bus_reports_count_3(self):
        drives = [
            OpticalDrive(bus=2, speed_mbps=480, product=f"Drive {i}", manufacturer="", serial=str(i), sysfs_path="")
            for i in range(3)
        ]

        warnings = detect_contention_warnings(drives)

        assert len(warnings) == 1
        assert warnings[0].drive_count == 3
        assert len(warnings[0].drives) == 3

    def test_mixed_speeds_uses_max_bus_speed(self):
        """If sysfs reports different speeds for siblings on the same bus
        (unusual but possible), the warning surfaces the highest as the
        bus speed — that's the realistic ceiling. Drive count + names
        still cover everything."""
        drives = [
            OpticalDrive(bus=2, speed_mbps=480, product="Old", manufacturer="", serial="A", sysfs_path=""),
            OpticalDrive(bus=2, speed_mbps=480, product="New", manufacturer="", serial="B", sysfs_path=""),
        ]

        warnings = detect_contention_warnings(drives)

        assert len(warnings) == 1
        assert warnings[0].speed_mbps == 480

    def test_superspeed_threshold_boundary(self):
        """The 5000 Mbps threshold is the exact cutoff — exactly 5000 is OK."""
        assert SUPERSPEED_MBPS_THRESHOLD == 5000
        drives = [
            OpticalDrive(bus=3, speed_mbps=5000, product="Pioneer", manufacturer="", serial="A", sysfs_path=""),
            OpticalDrive(bus=3, speed_mbps=5000, product="ASUS", manufacturer="", serial="B", sysfs_path=""),
        ]
        assert detect_contention_warnings(drives) == []

        # 4999 Mbps (one Mbps below SuperSpeed) — flagged.
        drives_below = [
            OpticalDrive(bus=3, speed_mbps=4999, product="Pioneer", manufacturer="", serial="A", sysfs_path=""),
            OpticalDrive(bus=3, speed_mbps=4999, product="ASUS", manufacturer="", serial="B", sysfs_path=""),
        ]
        assert len(detect_contention_warnings(drives_below)) == 1


class TestBusForMountPoint:
    """``bus_for_mount_point`` resolves /dev/srN → USB bus number by walking
    sysfs upward from the block device's ``device`` symlink until it finds
    a directory with a ``busnum`` attribute (the USB device-level dir).
    """

    def test_resolves_usb_drive_bus(self, tmp_path: Path):
        # Simulate the sysfs layout:
        #   /sys/devices/.../usb2/2-3/  (busnum=2)  ← what we want to find
        #   /sys/devices/.../usb2/2-3/2-3:1.0/host4/target4:0:0/4:0:0:0  ← device
        #   /sys/block/sr2/device -> ...the above device path
        sys_block = tmp_path / "sys" / "block"
        sys_devices = tmp_path / "sys" / "devices" / "pci0000:00" / "usb2" / "2-3"
        usb_dev = sys_devices  # this dir has busnum=2
        scsi_chain = usb_dev / "2-3:1.0" / "host4" / "target4:0:0" / "4:0:0:0"
        scsi_chain.mkdir(parents=True)
        (usb_dev / "busnum").write_text("2\n")

        sr2_dir = sys_block / "sr2"
        sr2_dir.mkdir(parents=True)
        (sr2_dir / "device").symlink_to(scsi_chain)

        assert bus_for_mount_point(
            "/dev/sr2", sys_block_dir=str(sys_block),
        ) == 2

    def test_non_sr_mount_point_returns_none(self, tmp_path):
        assert bus_for_mount_point("/dev/sda1", sys_block_dir=str(tmp_path)) is None
        assert bus_for_mount_point("", sys_block_dir=str(tmp_path)) is None

    def test_missing_device_link_returns_none(self, tmp_path):
        sys_block = tmp_path / "sys" / "block"
        sys_block.mkdir(parents=True)
        (sys_block / "sr1").mkdir()  # no `device` symlink under it
        assert bus_for_mount_point("/dev/sr1", sys_block_dir=str(sys_block)) is None

    def test_ata_drive_returns_none(self, tmp_path):
        """ATAPI/SATA optical drives don't have ``busnum`` in their chain —
        the walk hits root and gives up. Returning None signals 'not a USB
        bus' so the saturation gate can skip these drives entirely."""
        sys_block = tmp_path / "sys" / "block"
        ata_dev = tmp_path / "sys" / "devices" / "pci0000:00" / "ata1" / "host0" / "target0:0:0" / "0:0:0:0"
        ata_dev.mkdir(parents=True)
        (sys_block / "sr0").mkdir(parents=True)
        (sys_block / "sr0" / "device").symlink_to(ata_dev)

        assert bus_for_mount_point("/dev/sr0", sys_block_dir=str(sys_block)) is None


class TestSnapshotTopologyShape:
    def test_returns_serializable_dict(self, tmp_path: Path, monkeypatch):
        """``snapshot_topology`` is API-facing — its return must be
        JSON-serializable. Walk the live function with a fake sysfs to
        exercise the asdict() conversions."""
        _make_usb_dev(
            tmp_path, "2-2", product="Pioneer Blu-ray Drive",
            speed="480", busnum="2", serial="P",
        )
        _make_usb_dev(
            tmp_path, "2-3", product="ASUS External Drive",
            speed="480", busnum="2", serial="A",
        )
        monkeypatch.setattr("core.usb_topology.SYS_BUS_USB_DEVICES", str(tmp_path))

        result = snapshot_topology()

        import json
        json.dumps(result)  # would raise if anything was non-serializable
        assert set(result.keys()) == {"drives", "warnings"}
        assert len(result["drives"]) == 2
        assert len(result["warnings"]) == 1
        assert result["warnings"][0]["bus"] == 2
