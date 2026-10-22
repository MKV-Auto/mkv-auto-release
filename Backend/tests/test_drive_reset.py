"""#910: the drive reset re-adds the drive it removes.

Prod 2026-10-18: the reset deleted /dev/sr0, then looked up the SCSI host
from the deleted device's sysfs link, which no longer existed, so it never
rescanned and the drive stayed gone until it was physically replugged. The
recovery that worked by hand: delete the SCSI device (releases stuck I/O),
then de-authorize and re-authorize the USB port (a software replug).
"""
from __future__ import annotations

import pathlib

import pytest

import root_update_helper as helper


@pytest.fixture
def sysfs(tmp_path, monkeypatch):
    """A fake /sys shaped like the prod Pioneer: usb2/2-4 -> host3 -> sr0.

    Writing "1" to the device's ``delete`` removes /sys/block/sr0;
    re-authorizing the USB port or rescanning the host brings it back
    (unless ``state["comes_back"]`` is False)."""
    root = tmp_path / "sys"
    port = root / "devices/pci0000:00/0000:02:1b.0/usb2/2-4"
    dev = port / "2-4:1.0/host3/target3:0:0/3:0:0:0"
    dev.mkdir(parents=True)
    (dev / "delete").write_text("")
    (port / "authorized").write_text("1")
    scan = root / "class/scsi_host/host3/scan"
    scan.parent.mkdir(parents=True)
    scan.write_text("")
    block = root / "block/sr0"

    def plug():
        block.mkdir(parents=True, exist_ok=True)
        (block / "device").symlink_to(dev)

    plug()
    state = {"writes": [], "comes_back": True}
    real_write = pathlib.Path.write_text

    def write_text(self, data, *a, **kw):
        state["writes"].append((self.name, data))
        out = real_write(self, data, *a, **kw)
        if self.name == "delete" and data == "1":
            (block / "device").unlink()
            block.rmdir()
        elif (self.name == "authorized" and data == "1") or (self.name == "scan"):
            if state["comes_back"] and not block.exists():
                plug()
        return out

    monkeypatch.setattr(pathlib.Path, "write_text", write_text)
    monkeypatch.setattr(helper, "SYS_ROOT", root)
    monkeypatch.setattr(helper, "RESET_REAPPEAR_SECONDS", 0.5)
    monkeypatch.setattr(helper.time, "sleep", lambda s: None)
    return state


def test_a_usb_drive_is_deleted_then_replugged_and_comes_back(sysfs):
    result = helper._reset_device("/dev/sr0")
    assert result["status"] == "ok", result
    assert "USB port 2-4 re-authorized" in result["message"]
    assert sysfs["writes"] == [("delete", "1"), ("authorized", "0"), ("authorized", "1")]


def test_it_reports_a_drive_that_does_not_come_back(sysfs):
    sysfs["comes_back"] = False
    result = helper._reset_device("/dev/sr0")
    assert result["status"] == "error"
    assert "did not come back" in result["error"] and "replug" in result["error"]


def test_a_non_usb_drive_gets_a_host_rescan(sysfs, tmp_path):
    # Remove the USB port's authorized file: the drive is SATA-attached.
    (tmp_path / "sys/devices/pci0000:00/0000:02:1b.0/usb2/2-4/authorized").unlink()
    result = helper._reset_device("/dev/sr0")
    assert result["status"] == "ok", result
    assert "rescanned host3" in result["message"]
    assert ("scan", "- - -") in sysfs["writes"]


def test_an_unknown_device_is_refused(sysfs):
    assert helper._reset_device("/dev/sr9")["status"] == "error"


def test_the_mount_timeout_message_says_whether_the_reset_worked(monkeypatch):
    from core import utils

    monkeypatch.setattr(utils, "_root_helper_request", lambda payload: {"status": "ok", "message": "back"})
    assert utils._root_helper_reset_device("/dev/sr0") is True
    monkeypatch.setattr(utils, "_root_helper_request", lambda payload: {"status": "error", "error": "gone"})
    assert utils._root_helper_reset_device("/dev/sr0") is False
    monkeypatch.setattr(utils, "_root_helper_request", lambda payload: None)
    assert utils._root_helper_reset_device("/dev/sr0") is False
