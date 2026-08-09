"""A finished rip is never a running rip, whatever ``job_status`` says.

``job_status`` stays ``'running'`` through labeling, postprocess and transfer,
long after ``rip_state`` reaches ``'completed'``. Four decision points in the
gatekeeper read it as "a rip is in flight":

- ``is_rip_running_for_disc`` check 3 (DB fallback)
- ``can_start_rip``'s ``existing_is_running``
- two stale-job reconcilers that mark the job **failed** when the DB says
  running but no process is alive

So a job parked awaiting labels could block a new rip on the same mount, and
could be failed out while it was only waiting for the user.
"""
import pytest

from core.drive_gatekeeper import RIP_FINISHED_STATES, rip_state_says_running


class TestFinishedRipsAreNotRunning:
    @pytest.mark.parametrize("rip_state", RIP_FINISHED_STATES)
    def test_terminal_rip_state_beats_running_job_status(self, rip_state):
        # The production shape: parked awaiting labeling.
        assert rip_state_says_running("running", rip_state) is False

    @pytest.mark.parametrize("job_status", ["pending", "running", "validating", "completed", "failed"])
    def test_completed_rip_is_never_running_for_any_job_status(self, job_status):
        assert rip_state_says_running(job_status, "completed") is False

    def test_skipped_rip_is_not_running(self):
        assert rip_state_says_running("running", "skipped") is False


class TestGenuineRipsStillCount:
    def test_running_rip_state_is_running(self):
        assert rip_state_says_running("running", "running") is True

    def test_running_job_with_unstarted_rip_is_running(self):
        # rip_state not yet set — the job is dispatching; still counts.
        assert rip_state_says_running("running", None) is True

    def test_rip_running_under_non_running_job_status(self):
        assert rip_state_says_running("validating", "running") is True

    def test_pending_rip_under_pending_job_is_not_yet_running(self):
        # Matches the pre-existing rule: pending jobs shouldn't block new rips.
        assert rip_state_says_running("pending", "pending") is False

    def test_idle_job_is_not_running(self):
        assert rip_state_says_running("pending", None) is False
        assert rip_state_says_running("completed", "completed") is False
