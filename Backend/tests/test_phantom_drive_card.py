"""The active-rip fallback must not claim a drive for a finished rip.

``_build_initial_coordinator_state_sync`` emits an ``in_drive`` card for a job
whose mount isn't in the drive cache, covering the window where a rip is running
but MakeMKV hasn't let us scan the disc. It filtered on ``job_status`` alone —
which stays ``running`` through labeling, postprocess and transfer, long after
``rip_state`` reaches ``completed``.

On the box that hit this, 31 jobs sat in ``job_status='running'`` with
``rip_state='completed'``, all on ``/dev/sr0``. With the drive empty, every one
of them qualified, and the carousel showed a "Now Reading" card for whichever
the database returned first — a disc that had not been in the drive for days.
It vanished as soon as a real disc was inserted, because the real disc claimed
the mount.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from api import models
from api.routers.websockets import _build_initial_coordinator_state_sync


@pytest.fixture
def session(test_db):
    s = test_db()
    try:
        yield s
    finally:
        s.close()


def _job(session, *, rip_state, job_status="running", mount_point="/dev/sr0", created_at=None, name="Disc"):
    uid = uuid.uuid4().hex[:8]
    disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uid}", disc_name=name)
    job = models.Job(
        id=str(uuid.uuid4()),
        disc_id=disc.id,
        disc_num="1",
        mount_point=mount_point,
        mode="copy",
        job_status=job_status,
        rip_state=rip_state,
        created_at=created_at or datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    session.add_all([disc, job])
    session.commit()
    return job


def _cards(state, job_id):
    return [d for d in (state.get("discs") or []) if d.get("job_id") == job_id]


@patch("api.routers.websockets.get_cached_discs")
class TestFinishedRipsDoNotClaimTheDrive:
    def test_completed_rip_on_empty_drive_is_not_in_drive(self, mock_cached, session):
        job = _job(session, rip_state="completed")
        mock_cached.return_value = []

        cards = _cards(_build_initial_coordinator_state_sync(), job.id)
        assert len(cards) == 1, "job must still appear so the user can resume it"
        assert cards[0]["disc_state"] == "unfinished"

    @pytest.mark.parametrize("rip_state", ["completed", "skipped", "failed"])
    def test_no_terminal_rip_state_claims_the_drive(self, mock_cached, session, rip_state):
        job = _job(session, rip_state=rip_state)
        mock_cached.return_value = []

        cards = _cards(_build_initial_coordinator_state_sync(), job.id)
        assert all(c["disc_state"] != "in_drive" for c in cards)

    def test_many_parked_jobs_on_one_mount_produce_no_phantom(self, mock_cached, session):
        # The production shape: a pile of finished jobs all pinned to one mount.
        jobs = [
            _job(
                session,
                rip_state="completed",
                created_at=datetime(2026, 5, day, 12, 0, 0, tzinfo=timezone.utc),
                name=f"Parked {day}",
            )
            for day in range(1, 8)
        ]
        mock_cached.return_value = []

        state = _build_initial_coordinator_state_sync()
        in_drive = [d for d in (state.get("discs") or []) if d.get("disc_state") == "in_drive"]
        assert in_drive == [], "an empty drive must show no in_drive card at all"
        # Every job still reachable as unfinished work.
        for job in jobs:
            assert len(_cards(state, job.id)) == 1


@patch("api.routers.websockets.get_cached_discs")
class TestGenuineActiveRipsStillSurface:
    @pytest.mark.parametrize("rip_state", [None, "pending", "running"])
    def test_unfinished_rip_still_claims_the_drive(self, mock_cached, session, rip_state):
        # The case the fallback exists for: rip in flight, drive cache empty.
        job = _job(session, rip_state=rip_state)
        mock_cached.return_value = []

        cards = _cards(_build_initial_coordinator_state_sync(), job.id)
        assert len(cards) == 1
        assert cards[0]["disc_state"] == "in_drive"

    def test_newest_job_wins_when_two_contend_for_a_mount(self, mock_cached, session):
        older = _job(
            session, rip_state="running", created_at=datetime(2026, 5, 1, tzinfo=timezone.utc), name="Older"
        )
        newer = _job(
            session, rip_state="running", created_at=datetime(2026, 5, 9, tzinfo=timezone.utc), name="Newer"
        )
        mock_cached.return_value = []

        state = _build_initial_coordinator_state_sync()
        in_drive = [d for d in (state.get("discs") or []) if d.get("disc_state") == "in_drive"]
        assert len(in_drive) == 1, "one card per mount"
        assert in_drive[0]["job_id"] == newer.id, "ordering must be deterministic, newest first"
        assert in_drive[0]["job_id"] != older.id

    def test_terminal_and_active_together_picks_the_active_one(self, mock_cached, session):
        parked = _job(
            session, rip_state="completed", created_at=datetime(2026, 5, 9, tzinfo=timezone.utc), name="Parked"
        )
        ripping = _job(
            session, rip_state="running", created_at=datetime(2026, 5, 1, tzinfo=timezone.utc), name="Ripping"
        )
        mock_cached.return_value = []

        state = _build_initial_coordinator_state_sync()
        in_drive = [d for d in (state.get("discs") or []) if d.get("disc_state") == "in_drive"]
        assert len(in_drive) == 1
        # Newer, but finished — must lose to the older job that is actually ripping.
        assert in_drive[0]["job_id"] == ripping.id
        assert in_drive[0]["job_id"] != parked.id
