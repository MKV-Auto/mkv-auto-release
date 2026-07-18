"""Tests for GET /jobs/unfinished/summaries (carousel cards)."""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from api import database, models
from api.main import app


@pytest.fixture
def client(test_db):
    def override_get_db():
        try:
            session = test_db()
            yield session
        finally:
            session.close()

    from api.routers import jobs

    app.dependency_overrides[database.get_db] = override_get_db
    if hasattr(jobs, "get_db"):
        app.dependency_overrides[jobs.get_db] = override_get_db

    yield TestClient(app)
    app.dependency_overrides.clear()


def _minimal_movie_release(session):
    uid = str(uuid.uuid4())[:8]
    movie = models.Movie(id=str(uuid.uuid4()), name=f"Summary Movie {uid}")
    release = models.Release(
        id=str(uuid.uuid4()),
        slug=f"summary-rel-{uid}",
        type="movie",
        name="Summary Release",
        movie_id=movie.id,
    )
    session.add_all([movie, release])
    session.flush()
    return movie, release


def test_summaries_omit_failed_when_newer_active_job_same_disc(client, test_db):
    """Stale failed card is not returned if a newer running job exists for the same disc."""
    session = test_db()
    try:
        _, release = _minimal_movie_release(session)
        disc_id = str(uuid.uuid4())
        disc = models.Disc(
            id=disc_id,
            content_hash=f"hash-summaries-same-disc-{disc_id[:8]}",
            release_id=release.id,
        )
        t_failed = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
        t_active = datetime(2026, 4, 11, 12, 0, 0, tzinfo=timezone.utc)
        job_failed = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            mode="copy",
            job_status="failed",
            rip_state="failed",
            rip_progress=0,
            created_at=t_failed,
        )
        job_active = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            mode="copy",
            job_status="running",
            rip_state="completed",
            rip_progress=100,
            created_at=t_active,
        )
        session.add_all([disc, job_failed, job_active])
        session.commit()

        response = client.get("/jobs/unfinished/summaries")
        assert response.status_code == 200, response.text
        rows = response.json()
        job_ids = {r["job_id"] for r in rows}
        assert job_active.id in job_ids
        assert job_failed.id not in job_ids
        assert all(r["job_status"] != "failed" for r in rows)
    finally:
        session.close()


def test_summaries_includes_failed_when_no_newer_active(client, test_db):
    """Failed job appears when there is no newer active job for that disc."""
    session = test_db()
    try:
        _, release = _minimal_movie_release(session)
        disc_id = str(uuid.uuid4())
        disc = models.Disc(
            id=disc_id,
            content_hash=f"hash-summaries-failed-only-{disc_id[:8]}",
            release_id=release.id,
        )
        job_failed = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            mode="copy",
            job_status="failed",
            rip_state="failed",
            rip_progress=0,
        )
        session.add_all([disc, job_failed])
        session.commit()

        response = client.get("/jobs/unfinished/summaries")
        assert response.status_code == 200, response.text
        rows = response.json()
        job_ids = {r["job_id"] for r in rows}
        assert job_failed.id in job_ids
        failed_rows = [r for r in rows if r["job_id"] == job_failed.id]
        assert len(failed_rows) == 1
        assert failed_rows[0]["job_status"] == "failed"
    finally:
        session.close()


def test_summaries_shows_failed_when_active_is_older_than_failed(client, test_db):
    """If the running job is older than the failed job, failed is not suppressed (edge case)."""
    session = test_db()
    try:
        _, release = _minimal_movie_release(session)
        disc_id = str(uuid.uuid4())
        disc = models.Disc(
            id=disc_id,
            content_hash=f"hash-summaries-order-{disc_id[:8]}",
            release_id=release.id,
        )
        t_active = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
        t_failed = datetime(2026, 4, 12, 12, 0, 0, tzinfo=timezone.utc)
        job_active = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            mode="copy",
            job_status="running",
            rip_state="completed",
            rip_progress=100,
            created_at=t_active,
        )
        job_failed = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            mode="copy",
            job_status="failed",
            rip_state="failed",
            rip_progress=0,
            created_at=t_failed,
        )
        session.add_all([disc, job_active, job_failed])
        session.commit()

        response = client.get("/jobs/unfinished/summaries")
        assert response.status_code == 200, response.text
        rows = response.json()
        job_ids = {r["job_id"] for r in rows}
        assert job_active.id in job_ids
        assert job_failed.id in job_ids
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────
# Pending-after-rip jobs (#492) — restored to the carousel so transfer
# stalls don't make completed rip data invisible.
# ──────────────────────────────────────────────────────────────────────


def test_summaries_includes_pending_when_rip_completed(client, test_db):
    """Pending job whose rip finished must surface — the disc's rip
    artifacts exist on disk and the user needs an affordance to resume."""
    session = test_db()
    try:
        _, release = _minimal_movie_release(session)
        disc_id = str(uuid.uuid4())
        disc = models.Disc(
            id=disc_id,
            content_hash=f"hash-pending-rip-done-{disc_id[:8]}",
            release_id=release.id,
        )
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            mode="copy",
            job_status="pending",
            rip_state="completed",  # ← rip done, transfer/label not started
            rip_progress=100,
            created_at=datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc),
        )
        session.add_all([disc, job])
        session.commit()

        response = client.get("/jobs/unfinished/summaries")
        assert response.status_code == 200, response.text
        job_ids = {r["job_id"] for r in response.json()}
        assert job.id in job_ids


    finally:
        session.close()


def test_summaries_omits_pending_when_rip_not_started(client, test_db):
    """A pending job that hasn't ripped anything is the rip-queue's job to
    pick up; surfacing it would add empty cards to the carousel."""
    session = test_db()
    try:
        _, release = _minimal_movie_release(session)
        disc_id = str(uuid.uuid4())
        disc = models.Disc(
            id=disc_id,
            content_hash=f"hash-pending-norip-{disc_id[:8]}",
            release_id=release.id,
        )
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            mode="copy",
            job_status="pending",
            rip_state="pending",  # no rip data on disk yet
            rip_progress=0,
            created_at=datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc),
        )
        session.add_all([disc, job])
        session.commit()

        response = client.get("/jobs/unfinished/summaries")
        assert response.status_code == 200, response.text
        job_ids = {r["job_id"] for r in response.json()}
        assert job.id not in job_ids
    finally:
        session.close()
