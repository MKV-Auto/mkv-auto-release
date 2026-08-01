"""Media-presence probing for optical drives (#766).

The bug this guards against shipped to production: `sg_turs` was missing from
the container image, so the check silently fell through to `BLKGETSIZE64` —
and a USB Blu-ray drive reports a phantom ~1GiB size with an empty tray. Every
empty drive therefore reported media present, which drives `DriveSnapshot
.loaded`, the frontend's "Insert Disc" signal, and the rip-start gate.
"""
from __future__ import annotations

import subprocess
from unittest.mock import Mock

import pytest

from core import utils


@pytest.fixture(autouse=True)
def _reset_warn_state():
    utils._media_probe_warned.clear()
    yield
    utils._media_probe_warned.clear()


def _no_sg_turs(monkeypatch):
    monkeypatch.setattr(utils, "SG_TURS_BIN", None)


def _fake_ioctl(monkeypatch, status):
    """Make the CDROM_DRIVE_STATUS ioctl return `status`."""
    monkeypatch.setattr(utils.os, "open", lambda *a, **k: 99)
    monkeypatch.setattr(utils.os, "close", lambda fd: None)
    monkeypatch.setattr(utils.fcntl, "ioctl", lambda fd, req, *a: status)


class TestSgTurs:
    """When sg3-utils is installed its answer is definitive."""

    def test_status_good_means_media_present(self, monkeypatch):
        monkeypatch.setattr(utils, "SG_TURS_BIN", "/usr/bin/sg_turs")
        monkeypatch.setattr(utils.os.path, "exists", lambda p: True)
        monkeypatch.setattr(utils.subprocess, "run", Mock(return_value=Mock(returncode=0)))
        assert utils._drive_has_media("/dev/sr0") is True

    def test_nonzero_status_means_empty(self, monkeypatch):
        # rc=2 "device not ready" is what the real drive returns on an empty
        # tray — measured on the Pioneer that exhibited #766.
        monkeypatch.setattr(utils, "SG_TURS_BIN", "/usr/bin/sg_turs")
        monkeypatch.setattr(utils.os.path, "exists", lambda p: True)
        monkeypatch.setattr(utils.subprocess, "run", Mock(return_value=Mock(returncode=2)))
        assert utils._drive_has_media("/dev/sr0") is False

    def test_timeout_is_not_media(self, monkeypatch):
        monkeypatch.setattr(utils, "SG_TURS_BIN", "/usr/bin/sg_turs")
        monkeypatch.setattr(utils.os.path, "exists", lambda p: True)
        monkeypatch.setattr(
            utils.subprocess, "run",
            Mock(side_effect=subprocess.TimeoutExpired(cmd="sg_turs", timeout=1.5)),
        )
        assert utils._drive_has_media("/dev/sr0") is False


class TestCdromStatusFallback:
    """Without sg_turs, the kernel's cdrom API answers — not device size."""

    def test_disc_ok_means_media_present(self, monkeypatch):
        _no_sg_turs(monkeypatch)
        _fake_ioctl(monkeypatch, utils.CDS_DISC_OK)
        assert utils._drive_has_media("/dev/sr0") is True

    @pytest.mark.parametrize("status,label", [
        (utils.CDS_NO_DISC, "no disc"),
        (utils.CDS_TRAY_OPEN, "tray open"),
        (utils.CDS_DRIVE_NOT_READY, "spinning up — no usable media yet"),
    ])
    def test_states_without_usable_media(self, monkeypatch, status, label):
        _no_sg_turs(monkeypatch)
        _fake_ioctl(monkeypatch, status)
        assert utils._drive_has_media("/dev/sr0") is False, label

    def test_phantom_device_size_is_never_consulted(self, monkeypatch):
        """The #766 regression, stated directly.

        The drive reports a nonzero BLKGETSIZE64 with an empty tray. If size
        were consulted at all, this returns True and the bug is back.
        """
        _no_sg_turs(monkeypatch)
        calls: list[int] = []

        def ioctl(fd, request, *args):
            calls.append(request)
            if request == utils.CDROM_DRIVE_STATUS:
                return utils.CDS_NO_DISC
            # BLKGETSIZE64 — the phantom 1073741312 measured on the real drive.
            return (1073741312).to_bytes(8, "little")

        monkeypatch.setattr(utils.os, "open", lambda *a, **k: 99)
        monkeypatch.setattr(utils.os, "close", lambda fd: None)
        monkeypatch.setattr(utils.fcntl, "ioctl", ioctl)

        assert utils._drive_has_media("/dev/sr0") is False
        assert calls == [utils.CDROM_DRIVE_STATUS], \
            f"only the cdrom status ioctl may be issued, got {calls}"


class TestUnknownIsLoudNotSilent:
    """An unanswerable probe stays permissive — but says so."""

    def test_falls_back_to_true_when_nothing_can_answer(self, monkeypatch):
        _no_sg_turs(monkeypatch)
        monkeypatch.setattr(utils.os, "open", Mock(side_effect=OSError("no such device")))
        assert utils._drive_has_media("/dev/sr9") is True

    def test_no_info_from_the_drive_is_unknown_not_empty(self, monkeypatch):
        _no_sg_turs(monkeypatch)
        _fake_ioctl(monkeypatch, utils.CDS_NO_INFO)
        assert utils._drive_has_media("/dev/sr0") is True

    def test_the_optimistic_default_is_logged_once_per_device(self, monkeypatch, caplog):
        _no_sg_turs(monkeypatch)
        monkeypatch.setattr(utils.os, "open", Mock(side_effect=OSError("boom")))

        with caplog.at_level("WARNING"):
            utils._drive_has_media("/dev/sr0")
            utils._drive_has_media("/dev/sr0")
            utils._drive_has_media("/dev/sr1")

        warnings = [r for r in caplog.records if "Media presence" in r.message]
        assert len(warnings) == 2, "one warning per device, not per call"
        assert {"/dev/sr0", "/dev/sr1"} == {w.args[0] for w in warnings}
