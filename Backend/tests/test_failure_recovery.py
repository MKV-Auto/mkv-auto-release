"""Unit tests for core.failure_recovery: should_attempt_recovery, get_recovery_strategy."""
import pytest
from types import SimpleNamespace

from core import failure_recovery


@pytest.fixture(autouse=True)
def _clear_recovery_attempts():
    failure_recovery._recovery_attempts.clear()
    yield
    failure_recovery._recovery_attempts.clear()


# --- should_attempt_recovery ---


def test_should_attempt_recovery_empty_reason():
    job = SimpleNamespace(id="j1")
    assert failure_recovery.should_attempt_recovery(job, "") is False


def test_should_attempt_recovery_max_attempts_reached():
    job = SimpleNamespace(id="j1")
    failure_recovery._recovery_attempts["j1"] = 3
    assert failure_recovery.should_attempt_recovery(job, "timeout") is False


def test_should_attempt_recovery_timeout():
    job = SimpleNamespace(id="j1")
    assert failure_recovery.should_attempt_recovery(job, "timeout") is True


def test_should_attempt_recovery_stuck():
    job = SimpleNamespace(id="j1")
    assert failure_recovery.should_attempt_recovery(job, "stuck") is True


def test_should_attempt_recovery_postprocess():
    job = SimpleNamespace(id="j1")
    assert failure_recovery.should_attempt_recovery(job, "postprocess") is True


# --- get_recovery_strategy ---


# #365 step 5 — post_state column dropped; readers use job.derived_post_state.
# SimpleNamespace doesn't evaluate hybrid properties, so the fixtures set
# derived_post_state explicitly to mirror what the live column-less derivation
# would compute from rip_state / transfer_state / label_state / job_status.


def test_get_recovery_strategy_postprocess_running_rip_completed():
    job = SimpleNamespace(
        id="j1", derived_post_state="running", rip_state="completed", disc_payload=None
    )
    assert failure_recovery.get_recovery_strategy(job, "postprocess") == "resume_postprocess"


def test_get_recovery_strategy_preview_rip_completed_previews_queued():
    job = SimpleNamespace(
        id="j1",
        derived_post_state="",
        rip_state="completed",
        disc_payload={"previews": {"status": "queued"}},
    )
    assert failure_recovery.get_recovery_strategy(job, "preview") == "regenerate_previews"


def test_get_recovery_strategy_state_violation_post_running():
    job = SimpleNamespace(
        id="j1", derived_post_state="running", rip_state="", disc_payload=None
    )
    assert failure_recovery.get_recovery_strategy(job, "state violation") == "reset_postprocess_state"


def test_get_recovery_strategy_no_match_returns_none():
    job = SimpleNamespace(
        id="j1", derived_post_state="", rip_state="", disc_payload=None
    )
    assert failure_recovery.get_recovery_strategy(job, "some other error") is None
