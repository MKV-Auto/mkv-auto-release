"""Regression test for ``core.drive_gatekeeper.is_pid_alive``: must remain
truthful when psutil is unavailable.

The live verification of the multi-drive PR cluster (2026-06-17) caught a
production bug where the official container ships without psutil. The
old implementation caught the ``ImportError`` in a bare ``except Exception``
and silently returned False. Six call sites — including the rip_disc
revoke handler (#544) — early-returned thinking processes were dead when
they were actually alive, leaving makemkvcon subprocess orphans.
"""

from __future__ import annotations

import os
import sys
import types

import pytest


def _reload_module_without_psutil(monkeypatch):
    """Make ``import psutil`` raise ImportError, then reimport gatekeeper."""

    monkeypatch.setitem(sys.modules, "psutil", None)
    sys.modules.pop("core.drive_gatekeeper", None)
    from core import drive_gatekeeper

    return drive_gatekeeper


class TestPsutilFallback:
    """When psutil is missing, fall back to ``os.kill(pid, 0)`` POSIX check."""

    def test_self_pid_is_alive(self, monkeypatch):
        gk = _reload_module_without_psutil(monkeypatch)
        # Current process is definitely alive.
        assert gk.is_pid_alive(os.getpid()) is True

    def test_nonexistent_pid_is_not_alive(self, monkeypatch):
        gk = _reload_module_without_psutil(monkeypatch)
        # Pick a PID that's very unlikely to exist.
        assert gk.is_pid_alive(999_999) is False

    def test_zero_pid_is_not_alive(self, monkeypatch):
        gk = _reload_module_without_psutil(monkeypatch)
        assert gk.is_pid_alive(0) is False

    def test_negative_pid_is_not_alive(self, monkeypatch):
        gk = _reload_module_without_psutil(monkeypatch)
        assert gk.is_pid_alive(-1) is False

    def test_none_pid_is_not_alive(self, monkeypatch):
        gk = _reload_module_without_psutil(monkeypatch)
        assert gk.is_pid_alive(None) is False  # type: ignore[arg-type]


class TestPsutilPathPreferredWhenAvailable:
    """If psutil IS importable, prefer ``psutil.pid_exists`` for richer info."""

    def test_psutil_pid_exists_consulted_when_available(self, monkeypatch):
        fake_psutil = types.SimpleNamespace(
            pid_exists=lambda pid: pid == 42,
        )
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
        sys.modules.pop("core.drive_gatekeeper", None)
        from core import drive_gatekeeper

        assert drive_gatekeeper.is_pid_alive(42) is True
        assert drive_gatekeeper.is_pid_alive(43) is False


class TestUnexpectedExceptionStillSafe:
    """OS errors from the fallback path should not blow up the caller."""

    def test_oserror_returns_false(self, monkeypatch):
        gk = _reload_module_without_psutil(monkeypatch)

        def boom(_pid: int, _sig: int) -> None:
            raise OSError(99, "bizarre")

        monkeypatch.setattr("os.kill", boom)
        # Should swallow the unfamiliar OSError and report not alive.
        assert gk.is_pid_alive(12345) is False


@pytest.fixture(autouse=True)
def _restore_gatekeeper_import():
    """Avoid leaking the test's monkeypatched module into later tests."""

    yield
    sys.modules.pop("core.drive_gatekeeper", None)
    from core import drive_gatekeeper  # noqa: F401  (force fresh re-import)
