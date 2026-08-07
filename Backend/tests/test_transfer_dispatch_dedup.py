"""A job's transfer must be dispatched exactly once.

Regression test for the 2026-08-06 production incident: two independent
paths enqueue ``transfer_remote`` — the post-process auto-dispatch helper
and ``POST /jobs/{id}/transfer``. Neither claimed the job first, so
auto-dispatch left ``transfer_state='ready'`` and the API happily enqueued
a second task 91 seconds later. Two ``smbclient`` processes then wrote the
same destination file concurrently. Captain America's Blu-Ray was
dispatched four times.
"""
import pytest

from api import models
from core.job_state import claim_transfer_for_dispatch


@pytest.fixture
def db(test_db):
    session = test_db()
    yield session
    session.close()


def _job(db, **kw):
    job = models.Job(
        id=kw.pop("id", "job-dedup-1"),
        disc_id="disc-1",
        disc_num="1",
        mount_point="/dev/sr0",
        **kw,
    )
    db.add(job)
    db.commit()
    return job


@pytest.mark.parametrize("startable", ["pending", "ready"])
def test_first_claim_wins_from_a_startable_state(db, startable):
    _job(db, transfer_state=startable)
    assert claim_transfer_for_dispatch(db, "job-dedup-1") is True


@pytest.mark.parametrize("startable", ["pending", "ready"])
def test_second_claim_is_refused(db, startable):
    _job(db, transfer_state=startable)
    assert claim_transfer_for_dispatch(db, "job-dedup-1") is True
    # This is the call that used to enqueue the duplicate.
    assert claim_transfer_for_dispatch(db, "job-dedup-1") is False


@pytest.mark.parametrize("terminal", ["running", "completed", "failed"])
def test_cannot_claim_a_job_already_running_or_finished(db, terminal):
    _job(db, transfer_state=terminal)
    assert claim_transfer_for_dispatch(db, "job-dedup-1") is False


def test_claim_marks_the_job_running_so_other_dispatchers_can_see_it(db):
    # The bug was precisely that the claim was NOT visible: auto-dispatch
    # enqueued but left the row at 'ready'.
    _job(db, transfer_state="ready")
    claim_transfer_for_dispatch(db, "job-dedup-1")
    row = db.query(models.Job).filter_by(id="job-dedup-1").one()
    assert row.transfer_state == "running"


def test_unknown_job_is_not_claimable(db):
    assert claim_transfer_for_dispatch(db, "no-such-job") is False
