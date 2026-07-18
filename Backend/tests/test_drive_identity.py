"""Tests for ``core.drive_identity``.

Covers resolution via ``/dev/disk/by-id`` (all bus types), the by-path and
sysfs fallbacks, the ``multi_drive_safe`` policy, and the symlink-name
parser. Filesystem under test is constructed in ``tmp_path`` — no
monkeypatching of ``os`` is needed; the helper accepts directory
overrides as kwargs.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.drive_identity import (
    BY_ID_PRECEDENCE,
    DriveIdentity,
    _parse_by_id_name,
    build_identity_map,
    resolve_drive_identity,
)


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def fake_fs(tmp_path: Path) -> dict[str, Path]:
    """Build an empty ``/dev/`` + ``/dev/disk/by-id`` + ``/sys/block`` tree."""

    dev = tmp_path / "dev"
    dev.mkdir()
    by_id = tmp_path / "dev" / "disk" / "by-id"
    by_id.mkdir(parents=True)
    by_path = tmp_path / "dev" / "disk" / "by-path"
    by_path.mkdir(parents=True)
    sys_block = tmp_path / "sys" / "block"
    sys_block.mkdir(parents=True)

    return {
        "root": tmp_path,
        "dev": dev,
        "by_id": by_id,
        "by_path": by_path,
        "sys_block": sys_block,
    }


def _make_sr(fake_fs: dict[str, Path], name: str) -> Path:
    """Create a fake ``/dev/srN`` and return its path."""

    path = fake_fs["dev"] / name
    path.touch()
    sys_dir = fake_fs["sys_block"] / name / "device"
    sys_dir.mkdir(parents=True, exist_ok=True)
    return path


def _link(by_dir: Path, link_name: str, target: Path) -> None:
    """Create ``by_dir/link_name`` → relative path to ``target``."""

    rel_target = os.path.relpath(target, start=by_dir)
    os.symlink(rel_target, by_dir / link_name)


def _resolve(
    fake_fs: dict[str, Path],
    mount_point: Path,
    **kwargs,
) -> DriveIdentity:
    return resolve_drive_identity(
        str(mount_point),
        by_id_dir=str(fake_fs["by_id"]),
        by_path_dir=str(fake_fs["by_path"]),
        sys_block_dir=str(fake_fs["sys_block"]),
        **kwargs,
    )


# --- by-id resolution ------------------------------------------------------


class TestByIdResolution:
    """Drives resolved via ``/dev/disk/by-id/`` should be multi-drive safe."""

    def test_usb_drive(self, fake_fs):
        sr = _make_sr(fake_fs, "sr1")
        _link(
            fake_fs["by_id"],
            "usb-PIONEER_BD-RW_BDR-XD06U_1958040110900395-0:0",
            sr,
        )

        identity = _resolve(fake_fs, sr, hardware_name="MakeMKV PIONEER")

        assert identity.identity_source == "by-id"
        assert identity.multi_drive_safe is True
        assert identity.bus == "usb"
        assert identity.vendor == "PIONEER"
        assert identity.model == "BD-RW BDR-XD06U"
        assert identity.by_id_serial == "1958040110900395"
        assert identity.hardware_name == "MakeMKV PIONEER"

    def test_ata_drive(self, fake_fs):
        sr = _make_sr(fake_fs, "sr0")
        _link(fake_fs["by_id"], "ata-QEMU_DVD-ROM_QM00003", sr)

        identity = _resolve(fake_fs, sr)

        assert identity.identity_source == "by-id"
        assert identity.multi_drive_safe is True
        assert identity.bus == "ata"
        assert identity.vendor == "QEMU"
        assert identity.model == "DVD-ROM"
        assert identity.by_id_serial == "QM00003"

    def test_asus_usb(self, fake_fs):
        """The other real drive from the diagnostic."""

        sr = _make_sr(fake_fs, "sr2")
        _link(
            fake_fs["by_id"],
            "usb-ASUS_BW-16D1HT_AAAABBBB000E-0:0",
            sr,
        )

        identity = _resolve(fake_fs, sr)

        assert identity.bus == "usb"
        assert identity.vendor == "ASUS"
        assert identity.model == "BW-16D1HT"
        assert identity.by_id_serial == "AAAABBBB000E"


class TestByIdPrecedence:
    """When multiple by-id symlinks point at the same drive, prefer the
    most stable. Order from ``BY_ID_PRECEDENCE``: wwn > wwid > ata > usb > scsi > nvme."""

    def test_wwn_wins_over_usb(self, fake_fs):
        sr = _make_sr(fake_fs, "sr1")
        _link(fake_fs["by_id"], "wwn-0x5000c500abcdef01", sr)
        _link(fake_fs["by_id"], "usb-VENDOR_MODEL_SERIAL-0:0", sr)

        identity = _resolve(fake_fs, sr)

        assert identity.bus == "wwn"
        assert identity.by_id_name == "wwn-0x5000c500abcdef01"

    def test_ata_wins_over_scsi(self, fake_fs):
        sr = _make_sr(fake_fs, "sr0")
        _link(fake_fs["by_id"], "scsi-0VENDOR_MODEL_SERIAL", sr)
        _link(fake_fs["by_id"], "ata-VENDOR_MODEL_SERIAL", sr)

        identity = _resolve(fake_fs, sr)

        assert identity.bus == "ata"

    def test_precedence_constant_ordering(self):
        """Guard against accidental re-ordering of BY_ID_PRECEDENCE."""

        assert BY_ID_PRECEDENCE == (
            "wwn-",
            "wwid-",
            "ata-",
            "usb-",
            "scsi-",
            "nvme-",
        )


# --- fallback paths --------------------------------------------------------


class TestByPathFallback:
    """When by-id has no entry but by-path does, fall back and mark unsafe."""

    def test_by_path_only(self, fake_fs):
        sr = _make_sr(fake_fs, "sr3")
        _link(
            fake_fs["by_path"],
            "pci-0000:02:1b.0-usb-0:1:1.0-scsi-0:0:0:0",
            sr,
        )

        identity = _resolve(fake_fs, sr)

        assert identity.identity_source == "by-path"
        assert identity.multi_drive_safe is False
        assert identity.bus == "by-path"
        assert identity.by_id_serial.startswith("by-path:")


class TestSysfsFallback:
    """Last resort: sysfs vendor+model. Multi-drive blocked."""

    def test_sysfs_only(self, fake_fs):
        sr = _make_sr(fake_fs, "sr4")
        device_dir = fake_fs["sys_block"] / "sr4" / "device"
        (device_dir / "vendor").write_text("EXOTIC\n")
        (device_dir / "model").write_text("DRIVE-XYZ\n")

        identity = _resolve(fake_fs, sr)

        assert identity.identity_source == "sysfs"
        assert identity.multi_drive_safe is False
        assert identity.vendor == "EXOTIC"
        assert identity.model == "DRIVE-XYZ"
        assert identity.by_id_serial.startswith("sysfs:EXOTIC:DRIVE-XYZ:sr4")

    def test_sysfs_empty_attrs_falls_through_to_unknown(self, fake_fs):
        sr = _make_sr(fake_fs, "sr5")
        # device dir exists but vendor/model empty
        identity = _resolve(fake_fs, sr)

        assert identity.identity_source == "unknown"
        assert identity.multi_drive_safe is False


class TestUnknownFallback:
    """No resolution at all — identity_source=unknown, gatekeeper blocks."""

    def test_no_signal_anywhere(self, fake_fs):
        # Don't create the sysfs device dir
        sr = fake_fs["dev"] / "sr9"
        sr.touch()

        identity = _resolve(fake_fs, sr)

        assert identity.identity_source == "unknown"
        assert identity.multi_drive_safe is False
        assert identity.by_id_serial == "unknown:sr9"


# --- multi_drive_safe policy ----------------------------------------------


class TestMultiDriveSafetyPolicy:
    """``multi_drive_safe`` is True iff identity_source == 'by-id'."""

    @pytest.mark.parametrize(
        "source,expected",
        [
            ("by-id", True),
            ("by-path", False),
            ("sysfs", False),
            ("unknown", False),
        ],
    )
    def test_safety_bit(self, source, expected):
        identity = DriveIdentity(
            by_id_serial="x",
            vendor="v",
            model="m",
            bus="b",
            by_id_name="",
            hardware_name=None,
            identity_source=source,
        )
        assert identity.multi_drive_safe is expected


# --- parser ----------------------------------------------------------------


class TestParseByIdName:
    """Direct unit tests for the symlink-name parser."""

    def test_usb_strips_interface_suffix(self):
        vendor, model, serial = _parse_by_id_name(
            "usb-PIONEER_BD-RW_BDR-XD06U_1958040110900395-0:0",
            "usb-",
        )
        assert vendor == "PIONEER"
        assert model == "BD-RW BDR-XD06U"
        assert serial == "1958040110900395"

    def test_ata_three_tokens(self):
        vendor, model, serial = _parse_by_id_name(
            "ata-QEMU_DVD-ROM_QM00003",
            "ata-",
        )
        assert vendor == "QEMU"
        assert model == "DVD-ROM"
        assert serial == "QM00003"

    def test_scsi_four_tokens(self):
        vendor, model, serial = _parse_by_id_name(
            "scsi-0QEMU_QEMU_HARDDISK_drive-scsi0",
            "scsi-",
        )
        assert vendor == "0QEMU"
        assert model == "QEMU HARDDISK"
        assert serial == "drive-scsi0"

    def test_wwn_single_opaque_token(self):
        vendor, model, serial = _parse_by_id_name(
            "wwn-0x5000c500abcdef01",
            "wwn-",
        )
        assert vendor == ""
        assert model == ""
        assert serial == "0x5000c500abcdef01"

    def test_two_token_fallback(self):
        vendor, model, serial = _parse_by_id_name(
            "ata-MODEL_SERIAL",
            "ata-",
        )
        assert vendor == ""
        assert model == "MODEL"
        assert serial == "SERIAL"


# --- enumeration -----------------------------------------------------------


class TestBuildIdentityMap:
    """``build_identity_map`` returns one entry per sr* in sys_block_dir."""

    def test_enumerates_present_drives(self, fake_fs):
        sr1 = _make_sr(fake_fs, "sr1")
        sr2 = _make_sr(fake_fs, "sr2")
        _link(fake_fs["by_id"], "usb-VENDOR_MODEL_SR1SERIAL-0:0", sr1)
        _link(fake_fs["by_id"], "ata-VENDOR_MODEL_SR2SERIAL", sr2)

        mapping = build_identity_map(
            by_id_dir=str(fake_fs["by_id"]),
            by_path_dir=str(fake_fs["by_path"]),
            sys_block_dir=str(fake_fs["sys_block"]),
            dev_dir=str(fake_fs["dev"]),
        )

        dev_sr1 = str(fake_fs["dev"] / "sr1")
        dev_sr2 = str(fake_fs["dev"] / "sr2")
        assert set(mapping.keys()) == {dev_sr1, dev_sr2}
        assert mapping[dev_sr1].by_id_serial == "SR1SERIAL"
        assert mapping[dev_sr2].by_id_serial == "SR2SERIAL"

    def test_skips_non_sr_block_devices(self, fake_fs):
        (fake_fs["sys_block"] / "sda" / "device").mkdir(parents=True)
        (fake_fs["dev"] / "sda").touch()

        mapping = build_identity_map(
            by_id_dir=str(fake_fs["by_id"]),
            by_path_dir=str(fake_fs["by_path"]),
            sys_block_dir=str(fake_fs["sys_block"]),
            dev_dir=str(fake_fs["dev"]),
        )

        assert mapping == {}

    def test_skips_sysfs_entries_without_corresponding_dev(self, fake_fs):
        """sysfs may know about a drive whose ``/dev/srN`` was never created —
        the asymmetric partial-visibility state observed in the 2026-06
        diagnostic (Pre-S1 Finding #1). Such drives are excluded from the map."""

        (fake_fs["sys_block"] / "sr7" / "device").mkdir(parents=True)
        # NOTE: no fake_fs["dev"] / "sr7" — the asymmetric state.

        mapping = build_identity_map(
            by_id_dir=str(fake_fs["by_id"]),
            by_path_dir=str(fake_fs["by_path"]),
            sys_block_dir=str(fake_fs["sys_block"]),
            dev_dir=str(fake_fs["dev"]),
        )

        assert mapping == {}


# --- integration touchpoints ----------------------------------------------


class TestRealWorldExamples:
    """Smoke tests with the exact symlink names observed in the 2026-06
    diagnostic session — ensures the parser keeps working as new hardware
    surfaces."""

    def test_pioneer_xd06u(self, fake_fs):
        sr = _make_sr(fake_fs, "sr1")
        _link(
            fake_fs["by_id"],
            "usb-PIONEER_BD-RW_BDR-XD06U_1958040110900395-0:0",
            sr,
        )
        identity = _resolve(fake_fs, sr)
        assert identity.multi_drive_safe is True
        assert identity.by_id_serial == "1958040110900395"

    def test_asus_bw_16d1ht(self, fake_fs):
        sr = _make_sr(fake_fs, "sr2")
        _link(
            fake_fs["by_id"],
            "usb-ASUS_BW-16D1HT_AAAABBBB000E-0:0",
            sr,
        )
        identity = _resolve(fake_fs, sr)
        assert identity.multi_drive_safe is True
        assert identity.by_id_serial == "AAAABBBB000E"

    def test_qemu_virtual_dvd(self, fake_fs):
        sr = _make_sr(fake_fs, "sr0")
        _link(fake_fs["by_id"], "ata-QEMU_DVD-ROM_QM00003", sr)
        identity = _resolve(fake_fs, sr)
        assert identity.multi_drive_safe is True
        assert identity.by_id_serial == "QM00003"
