"""
Coverage for the new Phase 2 surface area (#365 postprocess collapse):

  * ``POST /jobs/{job_id}/transfer/start`` — single entry point for the
    collapsed rename + hash + transfer sequence
  * ``Job.transfer_phase`` column — sub-phase indicator for the UI
  * ``start_transfer`` Celery task — currently delegates to the existing
    prep logic but is the canonical entry point going forward

These tests pin the preconditions and shape that subsequent Phase 2
follow-up commits (the full body extraction, the final UI collapse, the
``post_state`` column drop) will refactor against.

See ``docs/plans/postprocess-collapse-325-365.md`` and
``docs/ADR-001-postprocess-collapse.md``.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from api import database, models
from api.main import app


@pytest.fixture
def client(test_db):
    """TestClient wired to the test DB. Overrides both the global ``get_db``
    and the jobs router's local ``get_db`` (it defines its own)."""
    from api.routers import jobs as jobs_router

    def override_get_db():
        session = test_db()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[database.get_db] = override_get_db
    if hasattr(jobs_router, "get_db"):
        app.dependency_overrides[jobs_router.get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed_job(session, *, rip_state="completed", label_state="skipped",
              transfer_state=None, transfer_phase=None):
    """Disc + Job at the boundary each test cares about."""
    disc_id = str(uuid.uuid4())
    session.add(models.Disc(id=disc_id, content_hash=f"h-{uuid.uuid4().hex[:8]}"))
    job_id = str(uuid.uuid4())
    session.add(models.Job(
        id=job_id, disc_id=disc_id, disc_num="1", mount_point="/mnt/sr0",
        rip_state=rip_state, label_state=label_state,
        transfer_state=transfer_state, transfer_phase=transfer_phase,
    ))
    session.commit()
    return job_id, disc_id


@pytest.fixture
def mock_start_transfer(monkeypatch):
    """Capture every ``start_transfer.delay(job_id)`` call without actually
    enqueueing or running the task. Returns the list of recorded calls."""
    calls = []

    class _FakeAsyncResult:
        def __init__(self):
            self.id = f"task-{uuid.uuid4().hex[:8]}"

    def fake_delay(job_id):
        result = _FakeAsyncResult()
        calls.append({"job_id": job_id, "task_id": result.id})
        return result

    monkeypatch.setattr(
        "workers.tasks.start_transfer.delay", fake_delay
    )
    return calls


# ──────────────────────────────────────────────────────────────────────────
# Happy path
# ──────────────────────────────────────────────────────────────────────────


def test_transfer_start_enqueues_start_transfer_and_returns_task_id(
    client, test_db, mock_start_transfer
):
    """Preconditions met → start_transfer is enqueued with the job id.
    (#863 changed the response contract: the endpoint reports the admission
    outcome instead of a raw Celery task id — dispatch goes through the
    stage gatekeeper, which owns the enqueue.)"""
    session = test_db()
    try:
        job_id, _ = _seed_job(session)
    finally:
        session.close()

    resp = client.post(f"/jobs/{job_id}/transfer/start")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["admission"] == "dispatched"
    # Single task enqueued for the right job id.
    assert len(mock_start_transfer) == 1
    assert mock_start_transfer[0]["job_id"] == job_id


def test_transfer_start_accepts_label_state_completed(client, test_db, mock_start_transfer):
    """Hit profile sends label_state='skipped'; miss profile lands here with
    label_state='completed' after labeling. Both must clear the gate."""
    session = test_db()
    try:
        job_id, _ = _seed_job(session, label_state="completed")
    finally:
        session.close()

    resp = client.post(f"/jobs/{job_id}/transfer/start")
    assert resp.status_code == 200
    assert len(mock_start_transfer) == 1


def test_transfer_start_accepts_legacy_jobs_without_label_state(client, test_db, mock_start_transfer):
    """Pre-label-state jobs from the migration window carry ``label_state=None``.
    They predate the labeling gate and must still be allowed to transfer."""
    session = test_db()
    try:
        job_id, _ = _seed_job(session, label_state=None)
    finally:
        session.close()

    resp = client.post(f"/jobs/{job_id}/transfer/start")
    assert resp.status_code == 200
    assert len(mock_start_transfer) == 1


# ──────────────────────────────────────────────────────────────────────────
# HTTP-level error paths
# ──────────────────────────────────────────────────────────────────────────


def test_transfer_start_returns_404_for_unknown_job(client, mock_start_transfer):
    resp = client.post(f"/jobs/{uuid.uuid4()}/transfer/start")
    assert resp.status_code == 404
    assert mock_start_transfer == []


def test_transfer_start_returns_409_when_rip_not_complete(client, test_db, mock_start_transfer):
    """Cannot transfer files that haven't been ripped — the gate is
    ``rip_state == 'completed'`` exactly."""
    session = test_db()
    try:
        job_id, _ = _seed_job(session, rip_state="running")
    finally:
        session.close()

    resp = client.post(f"/jobs/{job_id}/transfer/start")
    assert resp.status_code == 409
    assert "rip" in resp.json()["detail"].lower()
    assert mock_start_transfer == []


def test_transfer_start_returns_409_when_label_pending(client, test_db, mock_start_transfer):
    """Miss profile must finish labeling before transfer can start."""
    session = test_db()
    try:
        job_id, _ = _seed_job(session, label_state="pending")
    finally:
        session.close()

    resp = client.post(f"/jobs/{job_id}/transfer/start")
    assert resp.status_code == 409
    assert "label" in resp.json()["detail"].lower()
    assert mock_start_transfer == []


def test_transfer_start_returns_409_when_transfer_already_running(
    client, test_db, mock_start_transfer
):
    """Idempotency guard: a transfer that's already running must not be
    re-enqueued (would race against the in-flight worker)."""
    session = test_db()
    try:
        job_id, _ = _seed_job(session, transfer_state="running")
    finally:
        session.close()

    resp = client.post(f"/jobs/{job_id}/transfer/start")
    assert resp.status_code == 409
    assert "already in progress" in resp.json()["detail"].lower()
    assert mock_start_transfer == []


def test_transfer_start_returns_409_when_transfer_pending(
    client, test_db, mock_start_transfer
):
    """``transfer_state='pending'`` is the brief window between enqueue and
    worker pickup — same race-avoidance reasoning as the running case."""
    session = test_db()
    try:
        job_id, _ = _seed_job(session, transfer_state="pending")
    finally:
        session.close()

    resp = client.post(f"/jobs/{job_id}/transfer/start")
    assert resp.status_code == 409
    assert mock_start_transfer == []


# ──────────────────────────────────────────────────────────────────────────
# Schema + column surface
# ──────────────────────────────────────────────────────────────────────────


def test_get_status_exposes_transfer_phase_field(client, test_db):
    """The Pydantic JobStatus response includes the new ``transfer_phase``
    field. UI sub-phase rendering depends on this being threaded through."""
    session = test_db()
    try:
        job_id, _ = _seed_job(session, transfer_phase="preparing")
    finally:
        session.close()

    resp = client.get(f"/jobs/{job_id}/status")
    assert resp.status_code == 200, resp.text
    assert resp.json().get("transfer_phase") == "preparing"


def test_get_status_transfer_phase_null_on_legacy_jobs(client, test_db):
    """Jobs that predate the collapse have ``transfer_phase=None``. The
    field is exposed but null — the frontend's transferPhaseLabel falls
    back to the legacy transferState-based heuristic in that case."""
    session = test_db()
    try:
        job_id, _ = _seed_job(session, transfer_phase=None)
    finally:
        session.close()

    resp = client.get(f"/jobs/{job_id}/status")
    assert resp.status_code == 200
    # Field is present (not stripped) but null.
    body = resp.json()
    assert "transfer_phase" in body
    assert body["transfer_phase"] is None


# ──────────────────────────────────────────────────────────────────────────
# Task wiring smoke test
# ──────────────────────────────────────────────────────────────────────────


def test_start_transfer_task_is_registered_under_canonical_name():
    """The Celery task is registered under the name 'start_transfer'. The
    auto-progression sites (rip-verification-complete handler etc.) call
    ``start_transfer.delay`` by the imported reference, but the task name
    is what survives serialization to Redis — a rename would break
    in-flight tasks across a deploy."""
    from workers.tasks import start_transfer
    assert start_transfer.name == "start_transfer"


def test_resume_postprocess_no_longer_registered():
    """``resume_postprocess`` Celery task was removed in #365 step 6
    (Phase 2 § 6.7) once the post_state column drop landed and the rollout
    window had closed. Callers use ``start_transfer``; the shared prep
    body remains via the un-tasked ``_run_prep_phase`` helper. This guard
    ensures the task name doesn't accidentally come back."""
    from workers import tasks
    assert not hasattr(tasks, "resume_postprocess")
