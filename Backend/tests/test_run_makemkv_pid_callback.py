"""Regression test for #541: ``run_makemkv`` must fire ``pid_callback`` the
moment the subprocess is spawned, before any blocking wait.

The 2026-06 diagnostic observed two concurrent rips with non-NULL
``makemkvcon`` PIDs in ``ps`` while ``Job.rip_pid`` stayed NULL the entire
time. Root cause: the worker was only writing ``rip_pid`` after
``disc.rip()`` returned, which is hours later. With a spawn-time callback
the worker can persist the PID immediately.

This test isolates the contract — it does not actually invoke makemkvcon.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from core import utils


@pytest.fixture
def fake_popen(monkeypatch):
    """Replace ``subprocess.Popen`` with a mock that returns immediately."""

    fake_proc = MagicMock()
    fake_proc.pid = 424242
    # Provide an iterable stdout so the reader loop exits.
    fake_proc.stdout = iter([])
    fake_proc.wait.return_value = 0
    fake_proc.poll.return_value = 0
    fake_proc.returncode = 0

    popen_mock = MagicMock(return_value=fake_proc)
    monkeypatch.setattr("core.utils.subprocess.Popen", popen_mock)
    return popen_mock, fake_proc


class TestPidCallback:
    def test_callback_invoked_with_pid_after_spawn(self, fake_popen, tmp_path):
        _, fake_proc = fake_popen
        captured: list[int] = []
        utils.run_makemkv(
            "info disc:9999",
            log_path=tmp_path / "makemkvcon.log",
            pid_callback=captured.append,
        )

        assert captured == [424242]

    def test_callback_exception_does_not_abort_rip(self, fake_popen, tmp_path):
        """If the callback raises, run_makemkv must still finish — we don't
        want a transient DB hiccup to take down the rip."""

        _, _ = fake_popen

        def boom(_pid: int) -> None:
            raise RuntimeError("simulated callback failure")

        log, pid = utils.run_makemkv(
            "info disc:9999",
            log_path=tmp_path / "makemkvcon.log",
            pid_callback=boom,
        )

        assert pid == 424242

    def test_no_callback_keeps_legacy_behaviour(self, fake_popen, tmp_path):
        _, _ = fake_popen
        log, pid = utils.run_makemkv(
            "info disc:9999",
            log_path=tmp_path / "makemkvcon.log",
        )

        assert pid == 424242
