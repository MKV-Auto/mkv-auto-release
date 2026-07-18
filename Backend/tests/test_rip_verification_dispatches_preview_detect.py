"""Asserts the MISS-branch dispatch wiring at
``Backend/api/routers/jobs.py:rip_verification_complete_callback``.

Backfills the coverage gap the ``e2e_bootstrap.py`` no-op patch creates
(#195/#196): the e2e specs can no longer cross-check that
``preview_raw_titles.delay`` actually fires after rip-verification, because
the bootstrap stubs out the dispatch to keep the eager Celery chain from
hanging the POST /jobs/rip handler. This test verifies the wiring directly,
in pytest, in <1s.

Two cases:

1. **Success path** — body.success=True with preview_detect_keys; the handler
   should call ``preview_raw_titles.delay(job_id, keys, rel_path_overrides=...)``
   exactly once (jobs.py:3265-3269).

2. **Failure-heal path** — body.success=False but rip clearly done (progress
   >= 100); the handler should still call the same dispatch via the heal
   branch (jobs.py:3313-3317).
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from api import models
from api.routers.jobs import (
    RipVerificationCompleteRequest,
    rip_verification_complete_callback,
)
from workers import tasks as worker_tasks


def _seed_miss_job(session, *, rip_progress: int = 0) -> tuple[str, str]:
    """Seed a movie/release/disc/title/job tuple in the MISS state and return
    ``(job_id, title_id)``. ``rip_progress`` defaults to 0 (success path); set
    to 100 for the failure-heal precondition."""
    movie = models.Movie(id=str(uuid.uuid4()), name="Dispatch Test")
    release = models.Release(
        id=str(uuid.uuid4()),
        slug="dispatch-test",
        type="movie",
        name="Dispatch Test",
        movie_id=movie.id,
    )
    disc = models.Disc(
        id=str(uuid.uuid4()),
        content_hash="hash-dispatch",
        release_id=release.id,
        disc_number=1,
    )
    title = models.DiscTitle(
        id=str(uuid.uuid4()),
        disc_id=disc.id,
        source_file="00001.mpls",
        title="Main Feature",
        type="movie",
    )
    job = models.Job(
        id=str(uuid.uuid4()),
        disc_id=disc.id,
        disc_num="1",
        mount_point="/dev/sr0",
        job_status="running",
        scan_state="completed",
        rip_state="running",
        rip_progress=rip_progress,
        stage_profile="miss",  # drives ``_infer_profile`` to the MISS branch
        disc_payload={},
    )
    session.add_all([movie, release, disc, title, job])
    session.commit()
    return str(job.id), str(title.id)


@pytest.mark.integration
def test_rip_verification_success_dispatches_preview_raw_titles(
    test_db, monkeypatch
):
    """MISS-branch success path (jobs.py:3265-3269): callback dispatches
    ``preview_raw_titles.delay(job_id, keys, rel_path_overrides=overrides)``."""
    SessionLocal = test_db
    with SessionLocal() as session:
        job_id, title_id = _seed_miss_job(session)

    dispatch_mock = MagicMock(return_value=MagicMock(id="mock-task-id"))
    monkeypatch.setattr(worker_tasks.preview_raw_titles, "delay", dispatch_mock)

    body = RipVerificationCompleteRequest(
        success=True,
        ripped_files={title_id: "raw/test_t1.mkv"},
        source_hashes={title_id: "deadbeef"},
        preview_detect_keys=[title_id],
        preview_detect_overrides={title_id: "raw/test_t1.mkv"},
    )

    with SessionLocal() as session:
        result = rip_verification_complete_callback(
            job_id=job_id, body=body, db=session
        )

    assert result == {"ok": True}
    dispatch_mock.assert_called_once_with(
        job_id,
        [title_id],
        rel_path_overrides={title_id: "raw/test_t1.mkv"},
    )


@pytest.mark.integration
def test_rip_verification_failure_heal_dispatches_preview_raw_titles(
    test_db, monkeypatch
):
    """MISS-branch failure-heal path (jobs.py:3313-3317): when the worker
    reports failure but rip is clearly done (progress >= 100), the heal
    branch still dispatches preview/detect."""
    SessionLocal = test_db
    with SessionLocal() as session:
        # rip_progress=100 satisfies ``rip_clearly_done`` (jobs.py:3274-3278)
        # which is the gate for the failure-heal branch.
        job_id, title_id = _seed_miss_job(session, rip_progress=100)

    dispatch_mock = MagicMock(return_value=MagicMock(id="mock-task-id"))
    monkeypatch.setattr(worker_tasks.preview_raw_titles, "delay", dispatch_mock)

    body = RipVerificationCompleteRequest(
        success=False,
        ripped_files={title_id: "raw/test_t1.mkv"},
        source_hashes={title_id: "deadbeef"},
        preview_detect_keys=[title_id],
        preview_detect_overrides={title_id: "raw/test_t1.mkv"},
        error_reason="false-positive verification failure",
    )

    with SessionLocal() as session:
        result = rip_verification_complete_callback(
            job_id=job_id, body=body, db=session
        )

    assert result == {"ok": True}
    dispatch_mock.assert_called_once_with(
        job_id,
        [title_id],
        rel_path_overrides={title_id: "raw/test_t1.mkv"},
    )
