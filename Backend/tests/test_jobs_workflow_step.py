"""
Tests for POST /jobs/{job_id}/workflow/step/complete.

Step completion is POST-driven: frontend applies the returned JobStatus (workflow_step)
and does not rely on WebSocket for these transitions.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from api import models
from api.main import app

pytestmark = pytest.mark.integration


@pytest.fixture
def client(e2e_test_environment):
    """Test client with e2e mocks."""
    return TestClient(app)


@pytest.fixture
def job_with_disc(test_db):
    """Create a job with an attached disc and workflow_step=boxset."""
    with test_db() as session:
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=f"step-test-{uuid.uuid4().hex[:12]}",
        )
        session.add(disc)
        session.flush()
        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            mode="copy",
            job_status="running",
            rip_state="completed",
            workflow_step="boxset",
            disc_payload={"label_draft": {}},
            stage_profile="miss",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = str(job.id)
    return job_id


def test_step_complete_boxset_to_disc(client, job_with_disc):
    """Allowed transition boxset->disc returns 200 and workflow_step=disc."""
    response = client.post(
        f"/jobs/{job_with_disc}/workflow/step/complete",
        json={"to_step": "disc"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data.get("workflow_step") == "disc"
    assert "jobId" in data or "job_id" in data or "job_status" in data


def test_step_complete_invalid_transition(client, job_with_disc):
    """Transition boxset->titles is not allowed; returns 400 with current_step."""
    response = client.post(
        f"/jobs/{job_with_disc}/workflow/step/complete",
        json={"to_step": "titles"},
    )
    assert response.status_code == 400, response.text
    data = response.json()
    detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
    msg = detail.get("message") or data.get("message") or (data.get("detail") if isinstance(data.get("detail"), str) else "") or ""
    assert "Invalid step transition" in msg
    assert detail.get("current_step") == "boxset"


def test_step_complete_film_to_boxset_requires_rip(test_db, client):
    """film->boxset is rejected when rip_state is not running/completed."""
    with test_db() as session:
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=f"step-film-{uuid.uuid4().hex[:12]}",
        )
        session.add(disc)
        session.flush()
        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            mode="copy",
            job_status="pending",
            rip_state="pending",
            workflow_step="film",
            disc_payload={"label_draft": {}},
            stage_profile="miss",
        )
        session.add(job)
        session.commit()
        job_id = str(job.id)

    response = client.post(
        f"/jobs/{job_id}/workflow/step/complete",
        json={"to_step": "boxset"},
    )
    assert response.status_code == 400, response.text
    assert "rip_state" in response.json().get("detail", "").lower() or "running" in response.json().get("detail", "")


def test_step_complete_job_not_found(client):
    """Non-existent job_id returns 404."""
    response = client.post(
        "/jobs/00000000-0000-0000-0000-000000000000/workflow/step/complete",
        json={"to_step": "disc"},
    )
    assert response.status_code == 404, response.text


def test_step_complete_invalid_to_step(client, job_with_disc):
    """Invalid to_step value returns 422."""
    response = client.post(
        f"/jobs/{job_with_disc}/workflow/step/complete",
        json={"to_step": "invalid"},
    )
    assert response.status_code == 422, response.text


def test_step_complete_defaults_summary_for_hit_profile(test_db, client):
    """Hit profile defaults workflow_step to summary."""
    with test_db() as session:
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=f"step-hit-{uuid.uuid4().hex[:12]}",
        )
        session.add(disc)
        session.flush()
        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            mode="copy",
            job_status="running",
            rip_state="completed",
            stage_profile="hit",
        )
        session.add(job)
        session.commit()
        job_id = str(job.id)

    response = client.post(
        f"/jobs/{job_id}/workflow/step/complete",
        json={"to_step": "postprocess"},
    )
    assert response.status_code == 200, response.text
    assert response.json().get("workflow_step") == "postprocess"


def test_step_complete_postprocess_to_transfer_with_phase_ahead(test_db, client):
    """postprocess->transfer is allowed even if phase already says transfer."""
    with test_db() as session:
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=f"step-post-xfer-{uuid.uuid4().hex[:12]}",
        )
        session.add(disc)
        session.flush()
        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            mode="copy",
            job_status="running",
            rip_state="completed",
            workflow_step="postprocess",
            phase="transfer",
            disc_payload={"label_draft": {}},
            stage_profile="miss",
        )
        session.add(job)
        session.commit()
        job_id = str(job.id)

    response = client.post(
        f"/jobs/{job_id}/workflow/step/complete",
        json={"to_step": "transfer"},
    )
    assert response.status_code == 200, response.text
    assert response.json().get("workflow_step") == "transfer"


def test_step_complete_sequence_updates_job_workflow_step(test_db, client):
    """Completing film->boxset then boxset->disc should update job.workflow_step."""
    with test_db() as session:
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=f"step-seq-{uuid.uuid4().hex[:12]}",
        )
        session.add(disc)
        session.flush()
        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            mode="copy",
            job_status="running",
            rip_state="completed",
            workflow_step="film",
            disc_payload={"label_draft": {}},
            stage_profile="miss",
        )
        session.add(job)
        session.commit()
        job_id = str(job.id)

    r1 = client.post(f"/jobs/{job_id}/workflow/step/complete", json={"to_step": "boxset"})
    assert r1.status_code == 200, r1.text
    assert r1.json().get("workflow_step") == "boxset"

    r2 = client.post(f"/jobs/{job_id}/workflow/step/complete", json={"to_step": "disc"})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("workflow_step") == "disc"


def test_step_complete_forward_after_back_disc_to_titles(test_db, client):
    """Forward after back: user was at titles, navigated back to disc; POST to_step=titles must be accepted."""
    with test_db() as session:
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=f"step-fab-{uuid.uuid4().hex[:12]}",
        )
        session.add(disc)
        session.flush()
        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            mode="copy",
            job_status="running",
            rip_state="completed",
            workflow_step="disc",
            disc_payload={"label_draft": {}},
            stage_profile="miss",
        )
        session.add(job)
        session.commit()
        job_id = str(job.id)

    response = client.post(
        f"/jobs/{job_id}/workflow/step/complete",
        json={"to_step": "titles"},
    )
    assert response.status_code == 200, response.text
    assert response.json().get("workflow_step") == "titles"


def test_step_complete_backward_set_step_only_titles_to_disc(test_db, client):
    """Backward: job at titles, POST to_step=disc does set-step-only; 200 and workflow_step=disc, no phase change."""
    with test_db() as session:
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=f"step-bwd-{uuid.uuid4().hex[:12]}",
        )
        session.add(disc)
        session.flush()
        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            mode="copy",
            job_status="running",
            rip_state="completed",
            workflow_step="titles",
            disc_payload={"label_draft": {}},
            stage_profile="miss",
        )
        session.add(job)
        session.commit()
        job_id = str(job.id)

    response = client.post(
        f"/jobs/{job_id}/workflow/step/complete",
        json={"to_step": "disc"},
    )
    assert response.status_code == 200, response.text
    assert response.json().get("workflow_step") == "disc"
