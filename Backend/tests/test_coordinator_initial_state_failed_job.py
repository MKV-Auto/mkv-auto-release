"""Coordinator initial_state attaches latest failed job to in-drive disc when no active job."""

import uuid
from unittest.mock import patch

import pytest

from api import models
from api.routers.websockets import _build_initial_coordinator_state_sync


@pytest.fixture
def test_db_session(test_db):
    session = test_db()
    try:
        yield session
    finally:
        session.close()


@patch("api.routers.websockets.get_cached_discs")
def test_initial_state_in_drive_includes_failed_job_when_no_active(
    mock_cached, test_db_session
):
    """Inserted disc with only a failed job gets job_id + job_status on in_drive metadata."""
    uid = str(uuid.uuid4())[:8]
    movie = models.Movie(id=str(uuid.uuid4()), name=f"Fail Movie {uid}")
    release = models.Release(
        id=str(uuid.uuid4()),
        slug=f"fail-rel-{uid}",
        type="movie",
        name="Fail Release",
        movie_id=movie.id,
    )
    disc = models.Disc(
        id=str(uuid.uuid4()),
        content_hash=f"hash-fail-in-drive-{uid}",
        release_id=release.id,
    )
    job = models.Job(
        id=str(uuid.uuid4()),
        disc_id=disc.id,
        disc_num="1",
        mount_point="/dev/sr0",
        mode="copy",
        job_status="failed",
        rip_state="failed",
        rip_progress=0,
    )
    test_db_session.add_all([movie, release, disc, job])
    test_db_session.commit()

    mock_cached.return_value = [
        {
            "disc_id": disc.id,
            "disc_num": "1",
            "mount_point": "/dev/sr0",
        }
    ]

    state = _build_initial_coordinator_state_sync()
    assert state.get("type") == "initial_state"
    discs = state.get("discs") or []
    in_drive = [d for d in discs if d.get("disc_state") == "in_drive" and d.get("disc_id") == disc.id]
    assert len(in_drive) == 1
    row = in_drive[0]
    assert row.get("job_id") == job.id
    assert row.get("job_status") == "failed"
    assert row.get("created_at") is not None


@patch("api.routers.websockets.get_cached_discs")
def test_initial_state_prefers_active_job_over_failed(mock_cached, test_db_session):
    """When both active and failed exist, in_drive row uses the active job."""
    uid = str(uuid.uuid4())[:8]
    movie = models.Movie(id=str(uuid.uuid4()), name=f"Both Movie {uid}")
    release = models.Release(
        id=str(uuid.uuid4()),
        slug=f"both-rel-{uid}",
        type="movie",
        name="Both Release",
        movie_id=movie.id,
    )
    disc = models.Disc(
        id=str(uuid.uuid4()),
        content_hash=f"hash-both-{uid}",
        release_id=release.id,
    )
    from datetime import datetime, timezone

    t_old = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    t_new = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
    job_failed = models.Job(
        id=str(uuid.uuid4()),
        disc_id=disc.id,
        disc_num="1",
        mount_point="/dev/sr0",
        mode="copy",
        job_status="failed",
        rip_state="failed",
        rip_progress=0,
        created_at=t_old,
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
        created_at=t_new,
    )
    test_db_session.add_all([movie, release, disc, job_failed, job_active])
    test_db_session.commit()

    mock_cached.return_value = [
        {"disc_id": disc.id, "disc_num": "1", "mount_point": "/dev/sr0"}
    ]

    state = _build_initial_coordinator_state_sync()
    discs = state.get("discs") or []
    in_drive = [d for d in discs if d.get("disc_state") == "in_drive" and d.get("disc_id") == disc.id]
    assert len(in_drive) == 1
    assert in_drive[0].get("job_id") == job_active.id
    assert in_drive[0].get("job_status") == "running"
