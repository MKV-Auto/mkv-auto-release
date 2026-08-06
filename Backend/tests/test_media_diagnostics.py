"""Telling an empty tray apart from a disc the drive cannot engage.

Written from a real incident: a disc was inserted, the drive sensed it but
could not read it, and the app logged an ordinary eject and said nothing. The
user's only symptom was "it never scanned".
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from core import media_diagnostics as md


def _status(monkeypatch, value):
    monkeypatch.setattr(md, "_drive_status", lambda dev: value)


def _readable(monkeypatch, value):
    monkeypatch.setattr(md, "_medium_is_readable", lambda dev: value)


class TestMisseatedDiscDetection:
    @pytest.mark.parametrize("status,label", [
        (md.CDS_DISC_OK, "drive reports disc OK but cannot read it"),
        (md.CDS_DRIVE_NOT_READY, "drive sensing media it cannot spin up"),
    ])
    def test_senses_media_but_cannot_read_it(self, monkeypatch, status, label):
        _status(monkeypatch, status)
        _readable(monkeypatch, False)
        assert md.medium_present_but_unreadable("/dev/sr0") is True, label

    @pytest.mark.parametrize("status", [md.CDS_NO_DISC, md.CDS_TRAY_OPEN])
    def test_empty_tray_is_not_an_unreadable_disc(self, monkeypatch, status):
        """The alert must never fire on an empty drive — a false 'reseat the
        disc' teaches the user to ignore the message."""
        _status(monkeypatch, status)
        _readable(monkeypatch, False)
        assert md.medium_present_but_unreadable("/dev/sr0") is False

    def test_readable_disc_is_not_a_problem(self, monkeypatch):
        _status(monkeypatch, md.CDS_DISC_OK)
        _readable(monkeypatch, True)
        assert md.medium_present_but_unreadable("/dev/sr0") is False

    def test_unanswerable_drive_stays_quiet(self, monkeypatch):
        """No status means no diagnosis. Guessing here would alert on drives
        that simply cannot report, which is most of the unusual ones."""
        _status(monkeypatch, None)
        _readable(monkeypatch, False)
        assert md.medium_present_but_unreadable("/dev/sr0") is False


class TestReadProbe:
    def test_blocking_open_is_used_so_enomedium_surfaces(self, monkeypatch):
        """O_NONBLOCK deliberately succeeds on an empty optical drive, so the
        probe must open *blocking* — otherwise 'opened fine' is mistaken for
        'media present', which is the #766 class of bug all over again.
        """
        seen: list[int] = []

        def fake_open(path, flags):
            seen.append(flags)
            raise OSError(123, "No medium found")

        monkeypatch.setattr(md.os, "open", fake_open)
        assert md._medium_is_readable("/dev/sr0") is False
        assert seen and not (seen[0] & md.os.O_NONBLOCK), \
            f"probe must not pass O_NONBLOCK, got flags={seen[0]}"

    def test_short_read_means_unreadable(self, monkeypatch):
        monkeypatch.setattr(md.os, "open", lambda p, f: 42)
        monkeypatch.setattr(md.os, "read", lambda fd, n: b"")
        monkeypatch.setattr(md.os, "close", lambda fd: None)
        assert md._medium_is_readable("/dev/sr0") is False

    def test_successful_read_means_readable(self, monkeypatch):
        monkeypatch.setattr(md.os, "open", lambda p, f: 42)
        monkeypatch.setattr(md.os, "read", lambda fd, n: b"\x00" * 2048)
        monkeypatch.setattr(md.os, "close", lambda fd: None)
        assert md._medium_is_readable("/dev/sr0") is True


class TestNotification:
    def test_message_tells_the_user_what_to_do(self, monkeypatch):
        sent = {}

        def fake_emit(**kwargs):
            sent.update(kwargs)

        monkeypatch.setattr("core.notifications.emit_notification_sync", fake_emit)
        md.notify_unreadable_medium("/dev/sr0")

        assert "reinsert" in sent["message"] or "reseat" in sent["title"].lower()
        assert "/dev/sr0" in sent["message"]
        assert sent["level"] == "action_required"
        # Deduped per device: a disc left misseated must not alert on every
        # udev event (#723, #724 established this discipline).
        assert sent["id_key"] == "unreadable_medium:/dev/sr0"

    def test_a_failing_notifier_never_breaks_scanning(self, monkeypatch):
        monkeypatch.setattr(
            "core.notifications.emit_notification_sync",
            Mock(side_effect=RuntimeError("bus down")),
        )
        md.notify_unreadable_medium("/dev/sr0")  # must not raise
