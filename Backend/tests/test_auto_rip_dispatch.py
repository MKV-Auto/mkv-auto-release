"""
#331 — auto-rip dispatch on scan completion.

maybe_auto_start_rip reuses the start_rip route logic, so these tests pin
the dispatch policy around it: toggle gating, unhashed-disc skip, Path A
deferral (notification, no rip), duplicate-start silence, and the
rip-first miss landing on the `film` workflow step.
"""
import uuid
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from api import models
from api.schemas import JobStatus
from core.auto_rip import maybe_auto_start_rip


pytestmark = pytest.mark.integration


def _disc_info(**over):
    info = {
        "mount_point": "/dev/sr0",
        "disc_hash": "hash-331",
        "disc_num": "0",
        "disc_id": str(uuid.uuid4()),
        "discdb_hit": False,
        "info_title": "Midway",
    }
    info.update(over)
    return info


def _status(job_id: str) -> JobStatus:
    return JobStatus(jobId=job_id, job_status="running", rip_progress=0, post_progress=0, transfer_progress=0, logs=[])


def test_toggle_off_skips_dispatch(test_db):
    with (
        patch("core.settings.get_auto_rip_enabled", return_value=False),
        patch("api.routers.jobs.start_rip") as start_mock,
    ):
        assert maybe_auto_start_rip(_disc_info()) is None
        start_mock.assert_not_called()


def test_unhashed_disc_skips_dispatch(test_db):
    with (
        patch("core.settings.get_auto_rip_enabled", return_value=True),
        patch("api.routers.jobs.start_rip") as start_mock,
    ):
        assert maybe_auto_start_rip(_disc_info(disc_hash=None)) is None
        start_mock.assert_not_called()


def test_hit_dispatches_and_keeps_summary_step(test_db):
    with test_db() as session:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="hash-331-hit", disc_number=1)
        session.add(disc)
        session.flush()
        job = models.Job(
            id=str(uuid.uuid4()), disc_id=disc.id, disc_num="0",
            mount_point="/dev/sr0", job_status="running", rip_state="running",
            stage_profile="hit", workflow_step="summary",
        )
        session.add(job)
        session.commit()
        job_id, disc_id = str(job.id), disc.id

    with (
        patch("core.settings.get_auto_rip_enabled", return_value=True),
        patch("api.routers.jobs.start_rip", return_value=_status(job_id)) as start_mock,
        patch("core.notifications.emit_notification_sync") as notify_mock,
    ):
        result = maybe_auto_start_rip(_disc_info(disc_id=disc_id, discdb_hit=True))

    assert result == job_id
    start_mock.assert_called_once()
    req = start_mock.call_args[0][0]
    assert req.mount_point == "/dev/sr0"
    assert req.disc_id == disc_id
    # start_rip already set summary for the hit profile — no override.
    with test_db() as session:
        j = session.query(models.Job).filter(models.Job.id == job_id).first()
        assert j.workflow_step == "summary"
    assert notify_mock.call_args[0][2] == "rip_start"


def test_miss_dispatch_lands_on_film_step(test_db):
    with test_db() as session:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="hash-331-miss", disc_number=1)
        session.add(disc)
        session.flush()
        # No release_id: rip-first miss has no linked release yet.
        job = models.Job(
            id=str(uuid.uuid4()), disc_id=disc.id, disc_num="0",
            mount_point="/dev/sr0", job_status="running", rip_state="running",
            stage_profile="miss", workflow_step="boxset",
        )
        session.add(job)
        session.commit()
        job_id, disc_id = str(job.id), disc.id

    with (
        patch("core.settings.get_auto_rip_enabled", return_value=True),
        patch("api.routers.jobs.start_rip", return_value=_status(job_id)),
        patch("core.notifications.emit_notification_sync") as notify_mock,
    ):
        result = maybe_auto_start_rip(_disc_info(disc_id=disc_id, discdb_hit=False))

    assert result == job_id
    with test_db() as session:
        j = session.query(models.Job).filter(models.Job.id == job_id).first()
        assert j.workflow_step == "film", (
            "rip-first miss must land on the film step — the user hasn't "
            "selected a movie yet (#331)"
        )
        assert j.disc.release_id is None
    body = notify_mock.call_args[0][0]
    assert "link the disc" in body


def test_path_a_choice_emits_action_required_and_skips(test_db):
    exc = HTTPException(status_code=409, detail={
        "code": "needs_user_choice",
        "reason": "duplicate segment groups over threshold",
    })
    with (
        patch("core.settings.get_auto_rip_enabled", return_value=True),
        patch("api.routers.jobs.start_rip", side_effect=exc),
        patch("core.notifications.emit_notification_sync") as notify_mock,
    ):
        assert maybe_auto_start_rip(_disc_info()) is None

    notify_mock.assert_called_once()
    assert notify_mock.call_args[0][2] == "action_required"
    assert "rip-mode choice" in notify_mock.call_args[0][0]


def test_existing_job_409_is_silent(test_db):
    exc = HTTPException(status_code=409, detail="Cannot start rip: a job already exists")
    with (
        patch("core.settings.get_auto_rip_enabled", return_value=True),
        patch("api.routers.jobs.start_rip", side_effect=exc),
        patch("core.notifications.emit_notification_sync") as notify_mock,
    ):
        assert maybe_auto_start_rip(_disc_info()) is None
    notify_mock.assert_not_called()


def test_start_rip_crash_never_raises(test_db):
    with (
        patch("core.settings.get_auto_rip_enabled", return_value=True),
        patch("api.routers.jobs.start_rip", side_effect=RuntimeError("boom")),
    ):
        assert maybe_auto_start_rip(_disc_info()) is None
