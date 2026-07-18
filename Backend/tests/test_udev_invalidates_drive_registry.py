"""Regression test for #562 PR 4: every udev event (add/change/remove)
invalidates the drive_registry cache.

The registry's TTL-cached snapshot is the new source of truth for
"which drives have media" — without invalidation, an add/remove event
would not be reflected for up to ``ttl_seconds`` and the UI / rip-start
gate / startup warmup would see pre-event state.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from api.main import _handle_udev_event


@pytest.fixture
def _stub_handlers(monkeypatch):
    """Stub the downstream handlers so we only exercise the udev plumbing."""
    monkeypatch.setattr(
        "core._drive_operations.handle_disc_eject",
        lambda *a, **k: {"status": "ok"},
    )
    monkeypatch.setattr(
        "core._drive_operations.handle_disc_eject_for_device",
        lambda *a, **k: {"status": "ok"},
    )
    monkeypatch.setattr(
        "core._drive_operations.handle_disc_insert",
        lambda *a, **k: {"status": "ok"},
    )


def test_eject_event_invalidates_registry(_stub_handlers):
    with patch("core.drive_registry.invalidate") as mock_invalidate:
        _handle_udev_event("eject", "/dev/sr0", disc_num="0")
    mock_invalidate.assert_called_once()


def test_insert_event_invalidates_registry(_stub_handlers, monkeypatch):
    # The 'insert' branch exercises some slot-state plumbing; pre-stub it
    # so we don't accidentally exercise a real cache lookup.
    monkeypatch.setattr(
        "core.disc_cache.get", lambda *a, **k: None
    )
    with patch("core.drive_registry.invalidate") as mock_invalidate:
        _handle_udev_event("insert", "/dev/sr1", disc_num="1")
    mock_invalidate.assert_called_once()


def test_invalidate_failure_does_not_abort_event_handler(monkeypatch, _stub_handlers):
    """A faulty registry invalidation must not prevent the downstream
    disc-insert/eject handler from running — udev events are load-bearing."""

    def boom():
        raise RuntimeError("simulated cache fault")

    monkeypatch.setattr("core.drive_registry.invalidate", boom)
    # If the handler aborts, this call raises; assertion is implicit.
    _handle_udev_event("eject", "/dev/sr0", disc_num="0")
