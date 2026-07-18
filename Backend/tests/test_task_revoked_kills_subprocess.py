"""Tests for #544: revoked rip_disc must SIGTERM its makemkvcon subprocess.

The 2026-06 diagnostic observed a revoked rip leave its
``makemkvcon mkv dev:/dev/sr1`` subprocess alive for 30+ seconds after
the Celery task itself was killed, holding the device handle. With
``Job.rip_pid`` now persisted at spawn time (#541), the revoke handler
can look up the PID and propagate the kill.
"""

from __future__ import annotations

import os
import signal
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_request():
    """Build a Celery-like request object for a revoked rip_disc task."""

    req = MagicMock()
    req.task = "workers.tasks.rip_disc"
    req.id = "rip_disc:test-job-uuid"
    req.args = ("test-job-uuid",)
    req.hostname = "test-worker"
    return req


class TestRevokeKillsMakemkvcon:
    def test_sigterm_sent_when_rip_pid_alive(self, mock_request, monkeypatch):
        """Job has a persisted, alive rip_pid → SIGTERM is sent."""

        from workers.tasks import task_revoked_handler

        fake_job = MagicMock(rip_pid=12345)
        sessions: list[MagicMock] = []

        def _fake_session():
            session = MagicMock()
            session.__enter__ = MagicMock(return_value=session)
            session.__exit__ = MagicMock(return_value=False)
            sessions.append(session)
            return session

        with (
            patch("workers.tasks.db_session", _fake_session),
            patch("workers.tasks.crud.get_job", return_value=fake_job),
            patch("core.drive_gatekeeper.is_pid_alive", side_effect=[True, False]),
            patch("os.kill") as kill_mock,
        ):
            task_revoked_handler(request=mock_request)

        # SIGTERM sent once with the persisted PID. is_pid_alive returns False
        # immediately after SIGTERM so the kill loop exits before SIGKILL.
        kill_mock.assert_called_once_with(12345, signal.SIGTERM)

    def test_no_op_when_no_rip_pid(self, mock_request):
        """Pre-#541 jobs and not-yet-spawned tasks have NULL rip_pid → no kill."""

        from workers.tasks import task_revoked_handler

        fake_job = MagicMock(rip_pid=None)

        def _fake_session():
            session = MagicMock()
            session.__enter__ = MagicMock(return_value=session)
            session.__exit__ = MagicMock(return_value=False)
            return session

        with (
            patch("workers.tasks.db_session", _fake_session),
            patch("workers.tasks.crud.get_job", return_value=fake_job),
            patch("os.kill") as kill_mock,
        ):
            task_revoked_handler(request=mock_request)

        kill_mock.assert_not_called()

    def test_no_op_when_pid_already_dead(self, mock_request):
        """rip_pid stored but the process is already gone → don't signal."""

        from workers.tasks import task_revoked_handler

        fake_job = MagicMock(rip_pid=12345)

        def _fake_session():
            session = MagicMock()
            session.__enter__ = MagicMock(return_value=session)
            session.__exit__ = MagicMock(return_value=False)
            return session

        with (
            patch("workers.tasks.db_session", _fake_session),
            patch("workers.tasks.crud.get_job", return_value=fake_job),
            patch("core.drive_gatekeeper.is_pid_alive", return_value=False),
            patch("os.kill") as kill_mock,
        ):
            task_revoked_handler(request=mock_request)

        kill_mock.assert_not_called()

    def test_no_op_when_not_rip_disc_task(self):
        """Non-rip tasks are revoked frequently (post-process, transfer, ...);
        the kill path must only fire for rip_disc."""

        from workers.tasks import task_revoked_handler

        req = MagicMock()
        req.task = "workers.tasks.postprocess"
        req.id = "postprocess:somejob"
        req.args = ("somejob",)

        with patch("os.kill") as kill_mock:
            task_revoked_handler(request=req)

        kill_mock.assert_not_called()

    def test_no_op_when_no_request(self):
        from workers.tasks import task_revoked_handler

        with patch("os.kill") as kill_mock:
            task_revoked_handler(request=None)

        kill_mock.assert_not_called()

    def test_sigkill_after_grace_period(self, mock_request, monkeypatch):
        """If SIGTERM doesn't take effect within the grace window, escalate."""

        from workers.tasks import task_revoked_handler

        fake_job = MagicMock(rip_pid=12345)

        # is_pid_alive returns True the entire time so the SIGKILL path fires.
        # Patch time.sleep + time.monotonic so the test doesn't block 10s.
        monotonic_times = iter([0.0, 0.6, 1.2, 11.0])

        def _fake_session():
            session = MagicMock()
            session.__enter__ = MagicMock(return_value=session)
            session.__exit__ = MagicMock(return_value=False)
            return session

        with (
            patch("workers.tasks.db_session", _fake_session),
            patch("workers.tasks.crud.get_job", return_value=fake_job),
            patch("core.drive_gatekeeper.is_pid_alive", return_value=True),
            patch("time.monotonic", side_effect=lambda: next(monotonic_times)),
            patch("time.sleep"),
            patch("os.kill") as kill_mock,
        ):
            task_revoked_handler(request=mock_request)

        # Both SIGTERM and SIGKILL sent.
        sent_signals = [call.args[1] for call in kill_mock.call_args_list]
        assert signal.SIGTERM in sent_signals
        assert signal.SIGKILL in sent_signals
