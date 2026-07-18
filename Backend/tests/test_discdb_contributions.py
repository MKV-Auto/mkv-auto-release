"""
#85/#86 — contribution endpoints: exportable JSON bundle + status tracking.

`GET /discdb/contributions/{disc_id}/bundle` must return a
TheDiscDB-shaped in-memory bundle (release.json / discNN.json / summary
text) built from persisted disc+release state, and stamp
`discdb_contribution_status='exported'` + `discdb_exported_at` so the
contributions list can track what still needs submitting upstream.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from api import database, models
from api.main import app


pytestmark = pytest.mark.integration


@pytest.fixture
def client(test_db):
    from api.routers import discdb as discdb_router

    def override_get_db():
        session = test_db()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[database.get_db] = override_get_db
    app.dependency_overrides[discdb_router.get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed_labeled_disc(session, *, with_job=True, rip_state="completed", contribution_status=None):
    movie = models.Movie(id=str(uuid.uuid4()), name="Midway", production_year=2019)
    release = models.Release(
        id=str(uuid.uuid4()),
        slug="midway-4k",
        type="movie",
        name="Midway 4K",
        movie_id=movie.id,
    )
    disc = models.Disc(
        id=str(uuid.uuid4()),
        content_hash=f"hash-{uuid.uuid4().hex[:8]}",
        release_id=release.id,
        disc_number=1,
        disc_slug="midway-4k-disc1",
        disc_name="MIDWAY_4K",
        format="UHD",
        discdb_contribution_status=contribution_status,
    )
    title = models.DiscTitle(
        id=str(uuid.uuid4()),
        disc_id=disc.id,
        source_file="00800.mpls",
        title="Midway",
        type="mainmovie",
        duration=8364,
        segment_map="1,2,3",
    )
    session.add_all([movie, release, disc, title])
    session.flush()
    job = None
    if with_job:
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/bd",
            job_status="completed",
            rip_state=rip_state,
            stage_profile="miss",
        )
        session.add(job)
    session.commit()
    return disc.id, (str(job.id) if job else None)


def test_bundle_returns_discdb_shapes_and_marks_exported(client, test_db):
    with test_db() as session:
        disc_id, _job_id = _seed_labeled_disc(session)

    resp = client.get(f"/discdb/contributions/{disc_id}/bundle")
    assert resp.status_code == 200, resp.text
    bundle = resp.json()

    assert bundle["schema"] == "thediscdb-bundle/v1"
    assert bundle["disc_id"] == disc_id
    assert bundle["release_slug"] == "midway-4k"
    assert isinstance(bundle["release"], dict)
    assert isinstance(bundle["disc"], dict)
    titles = bundle["disc"].get("Titles") or []
    assert any(
        (t.get("SourceFile") or (t.get("Item") or {}).get("SourceFile")) == "00800.mpls"
        for t in titles
    ), f"disc JSON must carry the labeled title: {titles}"
    assert "Midway" in bundle["summary"]
    assert "Segment map: 1,2,3" in bundle["summary"]

    with test_db() as session:
        disc = session.query(models.Disc).filter(models.Disc.id == disc_id).first()
        assert disc.discdb_contribution_status == "exported"
        assert disc.discdb_exported_at is not None


def test_bundle_includes_info_log_when_artifacts_exist(client, test_db, tmp_path, monkeypatch):
    monkeypatch.setenv("MKVAUTO_JOBS_DIR", str(tmp_path / "jobs"))
    with test_db() as session:
        disc_id, job_id = _seed_labeled_disc(session)
    raw_dir = tmp_path / "jobs" / job_id / "raw"
    raw_dir.mkdir(parents=True)
    (raw_dir / "makemkv_info.log").write_text(
        'TINFO:0,9,0,"2:19:24"\nTINFO:0,27,0,"00800.mpls"\n', encoding="utf-8"
    )

    resp = client.get(f"/discdb/contributions/{disc_id}/bundle")
    assert resp.status_code == 200, resp.text
    assert resp.json()["info_log_included"] is True


def test_bundle_degrades_without_artifacts(client, test_db, monkeypatch, tmp_path):
    monkeypatch.setenv("MKVAUTO_JOBS_DIR", str(tmp_path / "nonexistent-jobs"))
    with test_db() as session:
        disc_id, _ = _seed_labeled_disc(session)

    resp = client.get(f"/discdb/contributions/{disc_id}/bundle")
    assert resp.status_code == 200, resp.text
    assert resp.json()["info_log_included"] is False


def test_bundle_400_without_completed_job(client, test_db):
    with test_db() as session:
        disc_id, _ = _seed_labeled_disc(session, with_job=False)

    resp = client.get(f"/discdb/contributions/{disc_id}/bundle")
    assert resp.status_code == 400
    assert "No completed rip job" in resp.json()["detail"]


def test_bundle_400_without_release_link(client, test_db):
    with test_db() as session:
        disc = models.Disc(
            id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:8]}", disc_number=1
        )
        session.add(disc)
        session.flush()
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/bd",
            job_status="completed",
            rip_state="completed",
        )
        session.add(job)
        session.commit()
        disc_id = disc.id

    resp = client.get(f"/discdb/contributions/{disc_id}/bundle")
    assert resp.status_code == 400
    assert "not linked to a release" in resp.json()["detail"]


def test_export_does_not_downgrade_submitted_status(client, test_db):
    with test_db() as session:
        disc_id, _ = _seed_labeled_disc(session, contribution_status="submitted")

    resp = client.get(f"/discdb/contributions/{disc_id}/bundle")
    assert resp.status_code == 200

    with test_db() as session:
        disc = session.query(models.Disc).filter(models.Disc.id == disc_id).first()
        assert disc.discdb_contribution_status == "submitted", (
            "re-exporting must not downgrade an already-submitted disc"
        )
        assert disc.discdb_exported_at is not None


def test_patch_contribution_status_validation(client, test_db):
    with test_db() as session:
        disc_id, _ = _seed_labeled_disc(session)

    bad = client.patch(f"/discdb/contributions/{disc_id}", json={"status": "bogus"})
    assert bad.status_code == 400

    ok = client.patch(f"/discdb/contributions/{disc_id}", json={"status": "submitted", "notes": "PR opened"})
    assert ok.status_code == 200
    with test_db() as session:
        disc = session.query(models.Disc).filter(models.Disc.id == disc_id).first()
        assert disc.discdb_contribution_status == "submitted"
        assert disc.discdb_submitted_at is not None
        assert disc.discdb_contribution_notes == "PR opened"
