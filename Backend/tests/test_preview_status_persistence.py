import uuid

import pytest

from api import models
from workers import tasks


def test_preview_status_preserved_on_rip_progress_update(test_db):
    with test_db() as session:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="hash")
        session.add(disc)
        session.commit()

        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            job_status="running",
            rip_state="running",
            disc_payload={
                "previews": {
                    "status": "running",
                    "tracks": {
                        "t1": {
                            "status": "completed",
                            "manifest": "previews/t1/preview.m3u8",
                        }
                    },
                }
            },
        )
        session.add(job)
        session.commit()
        session.refresh(job)

        stale_payload = {
            **(job.disc_payload or {}),
            "previews": {
                "status": "queued",
                "tracks": {
                    "t1": {
                        "status": "queued",
                        "manifest": "previews/t1/preview.m3u8",
                    }
                },
            },
        }

        task = tasks.JobTask()
        task.set_status(
            job,
            session,
            rip_progress=10,
            current_title_id="t1",
            current_title_number=1,
            current_title_progress=10,
            titles_completed=0,
            total_titles=1,
            disc_payload=stale_payload,
        )

        session.refresh(job)
        assert job.disc_payload["previews"]["status"] == "running"
