"""#605 — suppress the intermediate "Ready to transfer" toast when
auto-dispatch will run.

The notification at ``core.job_state`` line ~254 fires when
``StageState.postprocess_complete`` writes ``transfer_state='ready'`` +
``phase='transfer'``. In the standard flow the auto-dispatch helpers in
``workers.tasks`` (``_maybe_auto_dispatch_local_transfer`` /
``_maybe_auto_dispatch_remote_transfer``) fire microseconds later in the
same Celery worker, so the toast reads as a misleading terminal signal.
The user-facing complaint: getting a "Ready to transfer" notification then
another "Job complete" notification for what was meant to be one
auto-dispatched workflow.

These tests assert the gate works:
- auto-dispatch-capable config (local / rsync / smb / nfs) → toast suppressed
- no active config → toast fires (user must click to start transfer manually)
- unknown / future mode → toast fires (defensive — never silently drop UX)
- get_active_config raises → toast fires (don't silently swallow errors)
"""
from unittest.mock import MagicMock, patch

import pytest

from core.job_state import _auto_dispatch_will_run


class _Cfg:
    def __init__(self, mode):
        self.mode = mode


@pytest.fixture
def db():
    """A minimal session stand-in — _auto_dispatch_will_run only passes
    it through to ``get_active_config``, which we patch in each test."""
    return MagicMock(name="Session")


@pytest.fixture
def job():
    """The function reads nothing off the job today; reserved for future
    job-level gating (e.g. checking post_paths). Provide a placeholder
    so the signature matches the call site."""
    return MagicMock(name="Job")


def test_auto_dispatch_will_run_for_local_config(db, job):
    """Local mode auto-dispatches inline → suppress the toast."""
    with patch(
        "core.transfer.service.get_active_config", return_value=_Cfg("local")
    ):
        assert _auto_dispatch_will_run(db, job) is True


@pytest.mark.parametrize("mode", ["rsync", "smb", "nfs"])
def test_auto_dispatch_will_run_for_remote_configs(db, job, mode):
    """Remote modes auto-dispatch via the remote helper → suppress."""
    with patch(
        "core.transfer.service.get_active_config", return_value=_Cfg(mode)
    ):
        assert _auto_dispatch_will_run(db, job) is True


def test_no_active_config_keeps_notification(db, job):
    """No destination configured — the user actively has to click again."""
    with patch(
        "core.transfer.service.get_active_config", return_value=None
    ):
        assert _auto_dispatch_will_run(db, job) is False


def test_unknown_mode_keeps_notification(db, job):
    """Defensive: a future TransferConfig mode the auto-dispatch helpers
    don't know about should not silently swallow the user toast. Better
    a redundant notification than a silently-dropped one."""
    with patch(
        "core.transfer.service.get_active_config", return_value=_Cfg("future_mode")
    ):
        assert _auto_dispatch_will_run(db, job) is False


def test_lookup_exception_keeps_notification(db, job):
    """A transient DB / import failure inside the lookup must not silently
    swallow the toast either — same principle as the unknown-mode case."""
    with patch(
        "core.transfer.service.get_active_config",
        side_effect=RuntimeError("boom"),
    ):
        assert _auto_dispatch_will_run(db, job) is False
