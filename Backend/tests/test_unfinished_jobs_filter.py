"""Regression tests for #543: ``query_unfinished_jobs`` must exclude jobs
that the user has dismissed.

Before this fix the endpoint kept returning dismissed jobs, which left
"in progress" cards stuck in the UI forever and blocked the **Start rip**
button on inserted discs. The 2026-06 diagnostic reproduced this live.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from api import models
from api.unfinished_jobs import query_unfinished_jobs


def _make_disc(test_db) -> str:
    """Create a Disc row and return its id (FK target for Job.disc_id)."""

    with test_db() as db:
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=str(uuid.uuid4()).replace("-", "")[:32].upper(),
        )
        db.add(disc)
        db.commit()
        return disc.id


def _make_job(
    test_db,
    disc_id: str,
    *,
    job_status: str,
    rip_state: str | None,
    dismissed: bool = False,
) -> str:
    with test_db() as db:
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc_id,
            disc_num="0",
            mount_point="/dev/sr1",
            mode="copy",
            job_status=job_status,
            rip_state=rip_state,
            dismissed=dismissed,
        )
        db.add(job)
        db.commit()
        return job.id


class TestDismissedFilter:
    """The query must skip jobs with ``dismissed=True``, regardless of state."""

    def test_active_dismissed_job_excluded(self, test_db):
        disc_id = _make_disc(test_db)
        kept = _make_job(
            test_db, disc_id,
            job_status="running", rip_state="completed", dismissed=False,
        )
        _make_job(
            test_db, disc_id,
            job_status="running", rip_state="completed", dismissed=True,
        )

        with test_db() as db:
            result_ids = {str(j.id) for j in query_unfinished_jobs(db)}

        assert kept in result_ids
        assert len(result_ids) == 1

    def test_failed_dismissed_job_excluded(self, test_db):
        disc_id = _make_disc(test_db)
        _make_job(
            test_db, disc_id,
            job_status="failed", rip_state="failed", dismissed=True,
        )

        with test_db() as db:
            assert query_unfinished_jobs(db) == []

    def test_non_dismissed_active_job_kept(self, test_db):
        disc_id = _make_disc(test_db)
        kept = _make_job(
            test_db, disc_id,
            job_status="pending", rip_state="completed", dismissed=False,
        )

        with test_db() as db:
            ids = [str(j.id) for j in query_unfinished_jobs(db)]
        assert ids == [kept]

    def test_dismissed_default_is_false(self, test_db):
        """New Job rows that don't pass ``dismissed`` default to False and
        therefore surface in the query."""

        disc_id = _make_disc(test_db)
        with test_db() as db:
            job = models.Job(
                id=str(uuid.uuid4()),
                disc_id=disc_id,
                disc_num="0",
                mount_point="/dev/sr2",
                mode="copy",
                job_status="running",
                rip_state="completed",
            )
            db.add(job)
            db.commit()
            jid = job.id

        with test_db() as db:
            ids = [str(j.id) for j in query_unfinished_jobs(db)]
        assert jid in ids
