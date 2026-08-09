"""#498 — coordinator initial_state surfaces pending + failed unfinished jobs.

Before the fix, ``_build_initial_coordinator_state_sync`` used a narrower
filter (``job_status in {running, validating}``) than the
``/jobs/unfinished/summaries`` HTTP endpoint. After first page-load (which
fetched summaries over HTTP), navigating Ripper -> Settings -> Ripper
triggered a WS resync; the snapshot dropped any ``pending`` or ``failed``
cards and the frontend blanket-overwrote ``_discs`` with the smaller set,
making them vanish from the carousel.

These tests confirm both code paths now share
``query_unfinished_jobs(db)`` so the snapshot matches what the summaries
endpoint returns.
"""

import uuid
from datetime import datetime, timezone
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


def _make_release(uid: str):
    movie = models.Movie(id=str(uuid.uuid4()), name=f"Movie {uid}")
    release = models.Release(
        id=str(uuid.uuid4()),
        slug=f"rel-{uid}",
        type="movie",
        name=f"Release {uid}",
        movie_id=movie.id,
    )
    return movie, release


@patch("api.routers.websockets.get_cached_discs")
def test_snapshot_includes_pending_unfinished_job(mock_cached, test_db_session):
    """Worker exited mid-flow (``job_status='pending'`` after rip): card
    must appear in the snapshot so the user can resume."""
    uid = uuid.uuid4().hex[:8]
    movie, release = _make_release(uid)
    disc = models.Disc(
        id=str(uuid.uuid4()),
        content_hash=f"h-pending-{uid}",
        release_id=release.id,
    )
    job = models.Job(
        id=str(uuid.uuid4()),
        disc_id=disc.id,
        disc_num="1",
        mount_point="/mnt/sr0",
        mode="copy",
        job_status="pending",
        rip_state="completed",
        rip_progress=100,
        created_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    test_db_session.add_all([movie, release, disc, job])
    test_db_session.commit()

    mock_cached.return_value = []

    state = _build_initial_coordinator_state_sync()
    discs = state.get("discs") or []
    # Since the active-rip fallback ("emit in-drive card for active rips even
    # with empty cache"), a pending/running/validating job WITH a mount_point
    # surfaces as an `in_drive` card (and is deduped out of the `unfinished`
    # list). The invariant that matters is unchanged: the job must appear in
    # the snapshot so the user can resume it.
    matches = [d for d in discs if d.get("job_id") == job.id]
    assert len(matches) == 1, (
        "pending unfinished job must appear in coordinator snapshot "
        "(parity with /jobs/unfinished/summaries)"
    )
    # The rip is already complete and the drive cache is empty, so this is an
    # unfinished job — not a disc sitting in the drive. The active-rip fallback
    # used to claim it as `in_drive` because it only looked at `job_status`,
    # which produced phantom "Now Reading" cards on an empty drive.
    assert matches[0].get("disc_state") == "unfinished"
    assert matches[0].get("job_status") == "pending"


@patch("api.routers.websockets.get_cached_discs")
def test_snapshot_includes_failed_unfinished_job(mock_cached, test_db_session):
    """Latest failed job (no completed re-rip, no newer active) must appear."""
    uid = uuid.uuid4().hex[:8]
    movie, release = _make_release(uid)
    disc = models.Disc(
        id=str(uuid.uuid4()),
        content_hash=f"h-failed-{uid}",
        release_id=release.id,
    )
    job = models.Job(
        id=str(uuid.uuid4()),
        disc_id=disc.id,
        disc_num="1",
        mount_point="/mnt/sr0",
        mode="copy",
        job_status="failed",
        rip_state="completed",
        rip_progress=100,
        created_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    test_db_session.add_all([movie, release, disc, job])
    test_db_session.commit()

    mock_cached.return_value = []

    state = _build_initial_coordinator_state_sync()
    discs = state.get("discs") or []
    matches = [
        d for d in discs
        if d.get("disc_state") == "unfinished" and d.get("job_id") == job.id
    ]
    assert len(matches) == 1
    assert matches[0].get("job_status") == "failed"


@patch("api.routers.websockets.get_cached_discs")
def test_snapshot_excludes_failed_when_completed_exists(mock_cached, test_db_session):
    """Successful re-rip supersedes the earlier failed job."""
    uid = uuid.uuid4().hex[:8]
    movie, release = _make_release(uid)
    disc = models.Disc(
        id=str(uuid.uuid4()),
        content_hash=f"h-superseded-{uid}",
        release_id=release.id,
    )
    failed = models.Job(
        id=str(uuid.uuid4()),
        disc_id=disc.id,
        disc_num="1",
        mount_point="/mnt/sr0",
        mode="copy",
        job_status="failed",
        rip_state="completed",
        created_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    completed = models.Job(
        id=str(uuid.uuid4()),
        disc_id=disc.id,
        disc_num="1",
        mount_point="/mnt/sr0",
        mode="copy",
        job_status="completed",
        rip_state="completed",
        created_at=datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc),
    )
    test_db_session.add_all([movie, release, disc, failed, completed])
    test_db_session.commit()

    mock_cached.return_value = []

    state = _build_initial_coordinator_state_sync()
    discs = state.get("discs") or []
    unfinished_ids = {d.get("job_id") for d in discs if d.get("disc_state") == "unfinished"}
    assert failed.id not in unfinished_ids
    assert completed.id not in unfinished_ids  # completed is also not unfinished


@patch("api.routers.websockets.get_cached_discs")
def test_snapshot_running_unfinished_still_present(mock_cached, test_db_session):
    """Regression guard: the previously-supported running case still works."""
    uid = uuid.uuid4().hex[:8]
    movie, release = _make_release(uid)
    disc = models.Disc(
        id=str(uuid.uuid4()),
        content_hash=f"h-running-{uid}",
        release_id=release.id,
    )
    job = models.Job(
        id=str(uuid.uuid4()),
        disc_id=disc.id,
        disc_num="1",
        mount_point="/mnt/sr0",
        mode="copy",
        job_status="running",
        rip_state="completed",
        created_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    test_db_session.add_all([movie, release, disc, job])
    test_db_session.commit()

    mock_cached.return_value = []

    state = _build_initial_coordinator_state_sync()
    discs = state.get("discs") or []
    # Presence in the snapshot is the invariant under guard here. The rip has
    # finished, so the card is `unfinished` rather than `in_drive` — see the
    # pending test above.
    matches = [d for d in discs if d.get("job_id") == job.id]
    assert len(matches) == 1
    assert matches[0].get("disc_state") == "unfinished"
    assert matches[0].get("job_status") == "running"
