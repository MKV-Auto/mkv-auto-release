"""
#363 Horizon 1 — pipeline guard on title edits.

Matrix under test (assert_title_patch_allowed, wired into
PATCH /discs/{id}/titles, /titles/batch, and the releases ops route):

- no active job                                  → everything allowed
- active job, full rip, label pending            → rename + retype (incl.
  un-ignore) allowed — this is the normal miss labeling flow
- active job, selective rip (rip_set), rip
  started, ignored title NOT in rip_set          → un-ignore blocked
  (type_change_locked); rename still allowed
- ignored title IN rip_set                       → un-ignore allowed
- label_state completed / postprocess running    → all edits blocked
  (labels_locked)
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from api import database, models
from api.main import app


pytestmark = pytest.mark.integration


@pytest.fixture
def client(test_db):
    from api.routers import releases as releases_router

    def override_get_db():
        session = test_db()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[database.get_db] = override_get_db
    if hasattr(releases_router, "get_db"):
        app.dependency_overrides[releases_router.get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed(session, *, job_kwargs=None, title_kwargs=None):
    """Disc + one title (+ optional active job). Returns (disc_id, title_id)."""
    disc = models.Disc(
        id=str(uuid.uuid4()), content_hash=f"h363-{uuid.uuid4().hex[:8]}", disc_number=1
    )
    session.add(disc)
    session.flush()
    tkw = dict(
        id=str(uuid.uuid4()),
        disc_id=disc.id,
        source_file="00800.mpls",
        title="Feature",
        type=None,
        index=0,
    )
    tkw.update(title_kwargs or {})
    title = models.DiscTitle(**tkw)
    session.add(title)
    if job_kwargs is not None:
        jkw = dict(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/bd",
            job_status="running",
            rip_state="pending",
            stage_profile="miss",
        )
        jkw.update(job_kwargs)
        session.add(models.Job(**jkw))
    session.commit()
    return disc.id, title.id


def _patch_title(client, disc_id, title_id, **fields):
    return client.patch(f"/discs/{disc_id}/titles", json={"title_id": title_id, **fields})


def test_no_active_job_allows_everything(client, test_db):
    with test_db() as session:
        disc_id, title_id = _seed(session, job_kwargs=None, title_kwargs={"type": "ignore"})
    resp = _patch_title(client, disc_id, title_id, title="Renamed", type="extra")
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["success"] is True


def test_full_rip_allows_unignore_after_rip_completes(client, test_db):
    """all-mode rips produce every title; un-ignore post-rip is the main
    mistake-fixing flow #363 H1 exists for."""
    with test_db() as session:
        disc_id, title_id = _seed(
            session,
            job_kwargs={"rip_state": "completed", "rip_set": None},
            title_kwargs={"type": "ignore"},
        )
    resp = _patch_title(client, disc_id, title_id, type="extra")
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["success"] is True
    with test_db() as session:
        t = session.query(models.DiscTitle).filter(models.DiscTitle.id == title_id).first()
        # The PATCH path normalizes type casing; only the semantic matters here.
        assert (t.type or "").lower() == "extra"


def test_selective_rip_blocks_unignore_of_unripped_title(client, test_db):
    with test_db() as session:
        disc_id, title_id = _seed(
            session,
            job_kwargs={"rip_state": "running", "rip_set": [1, 2]},
            title_kwargs={"type": "ignore", "index": 7},
        )
    resp = _patch_title(client, disc_id, title_id, type="extra")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error_code"] == "type_change_locked"
    with test_db() as session:
        t = session.query(models.DiscTitle).filter(models.DiscTitle.id == title_id).first()
        assert (t.type or "").lower() == "ignore", "blocked edit must not persist"


def test_selective_rip_allows_rename_of_unripped_title(client, test_db):
    with test_db() as session:
        disc_id, title_id = _seed(
            session,
            job_kwargs={"rip_state": "running", "rip_set": [1, 2]},
            title_kwargs={"type": "ignore", "index": 7},
        )
    resp = _patch_title(client, disc_id, title_id, title="Better name")
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["success"] is True


def test_selective_rip_allows_unignore_when_title_in_rip_set(client, test_db):
    with test_db() as session:
        disc_id, title_id = _seed(
            session,
            job_kwargs={"rip_state": "completed", "rip_set": [1, 7]},
            title_kwargs={"type": "ignore", "index": 7},
        )
    resp = _patch_title(client, disc_id, title_id, type="extra")
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["success"] is True


def test_label_completed_alone_does_not_lock(client, test_db):
    """label_state='completed' + transfer still pending is the post-Continue,
    pre-Start-Transfer window. Postprocess is deferred until the user
    clicks Start Transfer, so nothing has consumed the labels yet — the
    user must still be able to Back-navigate to Titles and fix mistakes
    discovered on the Transfer preview. See workflow.service.ts:
    areLabelsLocked docstring for the rationale."""
    with test_db() as session:
        disc_id, title_id = _seed(
            session,
            job_kwargs={"rip_state": "completed", "label_state": "completed", "phase": "postprocess"},
        )
    resp = _patch_title(client, disc_id, title_id, title="Fix on the Transfer preview")
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["success"] is True


def test_postprocess_preparing_blocks_all_title_edits(client, test_db):
    """transfer_phase='preparing' derives post_state='running' (#365)."""
    with test_db() as session:
        disc_id, title_id = _seed(
            session,
            job_kwargs={
                "rip_state": "completed",
                "label_state": "skipped",
                "stage_profile": "hit",
                "transfer_phase": "preparing",
            },
        )
    resp = _patch_title(client, disc_id, title_id, title="Too late")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error_code"] == "labels_locked"


def test_completed_job_does_not_lock_library_edits(client, test_db):
    """Library drawer edits happen on completed jobs — those are governed by
    finalize state, not the pipeline guard."""
    with test_db() as session:
        disc_id, title_id = _seed(
            session,
            job_kwargs={
                "job_status": "completed",
                "rip_state": "completed",
                "label_state": "completed",
                "transfer_state": "completed",
            },
        )
    resp = _patch_title(client, disc_id, title_id, title="Post-ship cleanup", type="extra")
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["success"] is True


def test_batch_reports_locked_titles_softly(client, test_db):
    with test_db() as session:
        disc_id, title_id = _seed(
            session,
            job_kwargs={"rip_state": "running", "rip_set": [1, 2]},
            title_kwargs={"type": "ignore", "index": 7},
        )
        other = models.DiscTitle(
            id=str(uuid.uuid4()), disc_id=disc_id, source_file="00801.mpls",
            title="Extra", type="extra", index=1,
        )
        session.add(other)
        session.commit()
        other_id = other.id

    resp = client.patch(
        f"/discs/{disc_id}/titles/batch",
        json={"patches": [
            {"title_id": title_id, "type": "extra"},
            {"title_id": other_id, "title": "Renamed extra"},
        ]},
    )
    assert resp.status_code == 200, resp.text
    results = {r["title_id"]: r for r in resp.json()["results"]}
    assert results[title_id]["success"] is False
    assert results[title_id]["error_code"] == "type_change_locked"
    assert results[other_id]["success"] is True


def test_ops_route_enforces_guard(client, test_db):
    """Ops route also honors the labels_locked guard once the pipeline has
    actually consumed the labels. Use transfer_phase='preparing' (which
    derives post_state='running') to put the job in the locked window —
    label_state='completed' alone no longer locks; that's the deferred-
    postprocess window covered by test_label_completed_alone_does_not_lock."""
    with test_db() as session:
        disc_id, title_id = _seed(
            session,
            job_kwargs={
                "rip_state": "completed",
                "label_state": "completed",
                "stage_profile": "miss",
                "transfer_phase": "preparing",
            },
        )
    resp = client.patch(
        f"/releases/disc/{disc_id}/ops",
        json={"ops": [{"target": "title", "id": title_id, "fields": {"title": "Too late"}}]},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error_code"] == "labels_locked"
