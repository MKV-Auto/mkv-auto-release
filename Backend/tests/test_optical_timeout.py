"""#881: the app raises optical drives' SCSI command timeout from inside the
container, never lowering one that is already higher."""
from __future__ import annotations

import logging
import os
import stat

import pytest

import core.optical_timeout as ot


@pytest.fixture
def sysfs(tmp_path, monkeypatch):
    """A fake /sys/block with two optical drives at the kernel default and a
    non-optical disk that must be ignored."""
    root = tmp_path / "block"
    for name, value in (("sr0", "30"), ("sr1", "30"), ("sda", "30")):
        d = root / name / "device"
        d.mkdir(parents=True)
        (d / "timeout").write_text(f"{value}\n")
    monkeypatch.setattr(ot, "SYS_BLOCK", str(root))
    monkeypatch.delenv(ot.ENV_VAR, raising=False)
    ot._warned.clear()
    return root


def _timeout(root, name):
    return (root / name / "device" / "timeout").read_text().strip()


def test_startup_raises_every_optical_drive_to_the_default(sysfs):
    assert ot.ensure_all_drive_timeouts() == {"sr0": 180, "sr1": 180}
    assert _timeout(sysfs, "sr0") == "180"
    assert _timeout(sysfs, "sr1") == "180"
    assert _timeout(sysfs, "sda") == "30"


def test_a_higher_value_is_never_lowered(sysfs):
    (sysfs / "sr0" / "device" / "timeout").write_text("600\n")
    assert ot.ensure_drive_timeout("/dev/sr0") == 600
    assert _timeout(sysfs, "sr0") == "600"


def test_replugged_drive_is_raised_again(sysfs):
    ot.ensure_drive_timeout("/dev/sr1")
    (sysfs / "sr1" / "device" / "timeout").write_text("30\n")   # replug resets it
    assert ot.ensure_drive_timeout("/dev/sr1") == 180
    assert _timeout(sysfs, "sr1") == "180"


def test_env_overrides_and_zero_disables(sysfs, monkeypatch):
    monkeypatch.setenv(ot.ENV_VAR, "240")
    assert ot.ensure_drive_timeout("sr0") == 240
    monkeypatch.setenv(ot.ENV_VAR, "0")
    assert ot.ensure_drive_timeout("sr1") == 30
    assert _timeout(sysfs, "sr1") == "30"
    monkeypatch.setenv(ot.ENV_VAR, "three minutes")
    assert ot.configured_timeout_seconds() == ot.DEFAULT_TIMEOUT_SECONDS


def test_non_optical_or_missing_devices_are_ignored(sysfs):
    assert ot.ensure_drive_timeout("/dev/sda") is None
    assert ot.ensure_drive_timeout("/dev/sr7") is None
    assert ot.ensure_drive_timeout("") is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_read_only_sysfs_warns_once_per_drive_and_changes_nothing(sysfs, caplog):
    path = sysfs / "sr0" / "device" / "timeout"
    path.chmod(stat.S_IRUSR)
    with caplog.at_level(logging.WARNING, logger=ot.__name__):
        assert ot.ensure_drive_timeout("/dev/sr0") == 30
        assert ot.ensure_drive_timeout("/dev/sr0") == 30
    assert _timeout(sysfs, "sr0") == "30"
    warnings = [r for r in caplog.records if "sr0" in r.getMessage()]
    assert len(warnings) == 1
    assert ot.ENV_VAR in warnings[0].getMessage()


def test_registry_refresh_reapplies_the_timeout(sysfs, monkeypatch):
    from core import drive_registry

    monkeypatch.setattr(drive_registry, "_enumerate_devices", lambda: [])
    drive_registry._build_snapshots()
    assert _timeout(sysfs, "sr0") == "180"
