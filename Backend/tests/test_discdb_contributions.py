"""
#85/#86/#741 — contribution endpoints: submission export + status tracking.

`GET /discdb/contributions/{disc_id}/bundle` must return a
TheDiscDB-shaped in-memory bundle (release.json / discNN.json / summary
text) built from persisted disc+release state, and stamp
`discdb_contribution_status='exported'` + `discdb_exported_at` so the
contributions list can track what still needs submitting upstream.
"""
import uuid
from unittest.mock import patch

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


def _seed_labeled_disc(session, *, with_job=True, rip_state="completed", contribution_status=None, job_status="completed"):
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
            job_status=job_status,
            rip_state=rip_state,
            stage_profile="miss",
        )
        session.add(job)
    session.commit()
    return disc.id, (str(job.id) if job else None)


def test_bundle_returns_discdb_shapes_and_marks_exported(client, test_db):
    with test_db() as session:
        disc_id, _job_id = _seed_labeled_disc(session)

    resp = client.get(f"/discdb/contributions/{disc_id}/bundle?format=json")
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

    resp = client.get(f"/discdb/contributions/{disc_id}/bundle?format=json")
    assert resp.status_code == 200, resp.text
    assert resp.json()["info_log_included"] is True


def test_bundle_degrades_without_artifacts(client, test_db, monkeypatch, tmp_path):
    monkeypatch.setenv("MKVAUTO_JOBS_DIR", str(tmp_path / "nonexistent-jobs"))
    with test_db() as session:
        disc_id, _ = _seed_labeled_disc(session)

    resp = client.get(f"/discdb/contributions/{disc_id}/bundle?format=json")
    assert resp.status_code == 200, resp.text
    assert resp.json()["info_log_included"] is False


def test_bundle_defaults_to_a_zip_laid_out_for_upstream(client, test_db):
    """#741: the default response is a submission, not a blob to disassemble."""
    import io
    import zipfile

    with test_db() as session:
        disc_id, _ = _seed_labeled_disc(session)

    resp = client.get(f"/discdb/contributions/{disc_id}/bundle")
    assert resp.status_code == 200, resp.status_code
    assert resp.headers["content-type"] == "application/zip"
    assert ".zip" in resp.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
    assert "README.txt" in names
    # Every data file sits under one upstream release directory, so unzipping
    # into a fork of TheDiscDb/data drops them exactly where they belong.
    data_files = [n for n in names if n != "README.txt"]
    assert data_files, names
    assert all(n.startswith("data/") for n in data_files), names
    assert {n.rsplit("/", 1)[1] for n in data_files} == {
        "release.json", "disc01.json", "disc01-summary.txt",
    }


def test_bundle_400_without_a_finished_job(client, test_db):
    """Export is gated on Finish, not on the rip: finishing is only offered once
    rip, post-processing and transfer are all done, so it is the point at which
    the disc's data has stopped moving."""
    with test_db() as session:
        disc_id, _ = _seed_labeled_disc(session, with_job=False)

    resp = client.get(f"/discdb/contributions/{disc_id}/bundle?format=json")
    assert resp.status_code == 400
    assert "finish the job" in resp.json()["detail"].lower()


def test_bundle_400_when_the_job_ripped_but_was_never_finished(client, test_db):
    with test_db() as session:
        disc_id, _ = _seed_labeled_disc(session, job_status="running")

    resp = client.get(f"/discdb/contributions/{disc_id}/bundle?format=json")
    assert resp.status_code == 400


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

    resp = client.get(f"/discdb/contributions/{disc_id}/bundle?format=json")
    assert resp.status_code == 400
    assert "not linked to a release" in resp.json()["detail"]


def test_export_does_not_downgrade_submitted_status(client, test_db):
    with test_db() as session:
        disc_id, _ = _seed_labeled_disc(session, contribution_status="submitted")

    resp = client.get(f"/discdb/contributions/{disc_id}/bundle?format=json")
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


# ── Export all (#741) — background job ───────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_export_jobs():
    """The job registry is process-global and archives outlive a request by
    design, so without this a completed export leaks into the next test's view
    of "what is downloadable"."""
    from core import discdb_export_jobs as jobs

    jobs._jobs.clear()
    yield
    # Join before clearing. A worker that outlives its test re-resolves
    # api.database.SessionLocal — which monkeypatch has by then restored to the
    # real one — and opens a session against CI's actual Postgres.
    assert jobs.await_all_jobs(timeout=30), "an export worker outlived its test"
    for job in list(jobs._jobs.values()):
        jobs._discard(job)
    jobs._jobs.clear()



def _run_export(client, timeout=20.0):
    """Start the export and poll to completion, as the UI does."""
    import time

    started = client.post("/discdb/contributions/export-all")
    assert started.status_code == 202, started.text
    job_id = started.json()["job_id"]

    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/discdb/contributions/export-all/{job_id}").json()
        if body["status"] in ("completed", "failed"):
            return job_id, body
        time.sleep(0.05)
    raise AssertionError(f"export {job_id} did not finish within {timeout}s")


def _read_zip(content):
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return zf.namelist(), zf.read("README.txt").decode("utf-8")


def test_export_all_returns_every_eligible_disc_in_one_tree(client, test_db):
    """The point of the bulk export: unpack once over a fork of TheDiscDb/data."""
    with test_db() as session:
        _seed_labeled_disc(session)
        _seed_labeled_disc(session)

    job_id, status = _run_export(client)
    assert status["status"] == "completed", status
    assert status["included"] == 2

    resp = client.get(f"/discdb/contributions/export-all/{job_id}/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    names, _ = _read_zip(resp.content)
    data_files = [n for n in names if n != "README.txt"]
    assert data_files and all(n.startswith("data/") for n in data_files)


def test_export_reports_progress_against_a_total(client, test_db):
    with test_db() as session:
        _seed_labeled_disc(session)

    _, status = _run_export(client)
    assert status["total"] == 1
    assert status["done"] == status["total"]


def test_export_all_skips_discs_that_are_already_upstream(client, test_db):
    """discdb_disc_num is only ever set on a TheDiscDB match — re-submitting
    those would open duplicate pull requests."""
    with test_db() as session:
        _seed_labeled_disc(session)
        disc_id, _ = _seed_labeled_disc(session)
        disc = session.query(models.Disc).filter(models.Disc.id == disc_id).first()
        disc.discdb_disc_num = 1
        session.commit()

    _, status = _run_export(client)
    assert status["included"] == 1


def test_export_all_skips_unfinished_jobs(client, test_db):
    with test_db() as session:
        _seed_labeled_disc(session)
        _seed_labeled_disc(session, job_status="running")

    _, status = _run_export(client)
    assert status["included"] == 1


def test_export_fails_when_nothing_is_eligible(client, test_db):
    """A "completed" job with an empty archive would read as success."""
    with test_db() as session:
        _seed_labeled_disc(session, with_job=False)

    job_id, status = _run_export(client)
    assert status["status"] == "failed"
    assert "finished job" in status["error"]
    # And nothing to download.
    assert client.get(f"/discdb/contributions/export-all/{job_id}/download").status_code == 409


def test_export_all_marks_each_included_disc_exported(client, test_db):
    """Same bookkeeping as the single export — they are equally handed over."""
    with test_db() as session:
        disc_id, _ = _seed_labeled_disc(session)

    _run_export(client)

    with test_db() as session:
        disc = session.query(models.Disc).filter(models.Disc.id == disc_id).first()
        assert disc.discdb_contribution_status == "exported"
        assert disc.discdb_exported_at is not None


def test_a_failed_export_does_not_mark_anything_exported(client, test_db):
    """Otherwise the discs vanish from status=not_submitted without a zip."""
    with test_db() as session:
        disc_id, _ = _seed_labeled_disc(session)

    with patch("core.discdb_export.build_discdb_bulk_zip", side_effect=OSError("disk full")):
        _, status = _run_export(client)
    assert status["status"] == "failed"

    with test_db() as session:
        disc = session.query(models.Disc).filter(models.Disc.id == disc_id).first()
        assert disc.discdb_contribution_status != "exported"


def test_export_all_does_not_downgrade_a_submitted_disc(client, test_db):
    with test_db() as session:
        disc_id, _ = _seed_labeled_disc(session, contribution_status="submitted")

    _run_export(client)

    with test_db() as session:
        disc = session.query(models.Disc).filter(models.Disc.id == disc_id).first()
        assert disc.discdb_contribution_status == "submitted"


def test_export_all_survives_one_bad_disc(client, test_db):
    """One failure must not cost the whole archive."""
    with test_db() as session:
        _seed_labeled_disc(session)
        bad_id, _ = _seed_labeled_disc(session)
        disc = session.query(models.Disc).filter(models.Disc.id == bad_id).first()
        disc.release_id = None
        session.commit()

    _, status = _run_export(client)
    assert status["included"] == 1


def test_status_404s_for_an_unknown_job(client):
    assert client.get("/discdb/contributions/export-all/nope").status_code == 404


def test_export_all_is_not_captured_by_the_disc_id_route(client, test_db):
    """`/contributions/export-all` must not be read as a disc id of that name."""
    with test_db() as session:
        _seed_labeled_disc(session)

    job_id, _ = _run_export(client)
    resp = client.get(f"/discdb/contributions/export-all/{job_id}/download")
    assert resp.headers["content-type"] == "application/zip"


def test_a_finished_export_is_still_reachable_after_a_reload(client, test_db):
    """The point of the background job is that you can walk away — so an export
    that finished unwatched must be collectable, not rebuilt from scratch."""
    with test_db() as session:
        _seed_labeled_disc(session)

    job_id, status = _run_export(client)
    assert status["status"] == "completed"

    # What a freshly-loaded page asks for.
    active = client.get("/discdb/contributions/export-all/active").json()
    assert active["job_id"] == job_id
    assert active["status"] == "completed"
    assert active["download_ready"] is True
    assert active["included"] == 1

    # And it downloads without starting anything.
    assert client.get(
        f"/discdb/contributions/export-all/{job_id}/download"
    ).status_code == 200


def test_a_failed_export_is_not_offered_as_a_download(client, test_db):
    with test_db() as session:
        _seed_labeled_disc(session, with_job=False)

    _run_export(client)

    assert client.get("/discdb/contributions/export-all/active").json()["status"] == "idle"


def test_a_running_export_takes_precedence_over_a_finished_one(client, test_db):
    """A page that loads mid-run should rejoin the run, not offer a stale zip."""
    import threading

    with test_db() as session:
        _seed_labeled_disc(session)

    _run_export(client)  # leaves a completed, downloadable archive

    release = threading.Event()

    def slow(db, dest=None, progress=None, should_cancel=None):
        release.wait(5)
        return "f.zip", None, {"included": 0, "skipped": 0, "disc_ids": [],
                               "total": 0, "cancelled": False}

    with patch("core.discdb_export.build_discdb_bulk_zip", slow):
        second = client.post("/discdb/contributions/export-all").json()
        active = client.get("/discdb/contributions/export-all/active").json()
        assert active["job_id"] == second["job_id"]
        assert active["status"] in ("pending", "running")
        release.set()


def test_the_archive_is_gone_after_retention_and_410s(client, test_db):
    """A download button that 500s is worse than one that explains itself."""
    from core import discdb_export_jobs as jobs

    with test_db() as session:
        _seed_labeled_disc(session)

    job_id, _ = _run_export(client)
    jobs.get_job(job_id).path.unlink()

    resp = client.get(f"/discdb/contributions/export-all/{job_id}/download")
    assert resp.status_code == 410
    assert "run it again" in resp.json()["detail"]
    # And it stops being offered.
    assert client.get("/discdb/contributions/export-all/active").json()["status"] == "idle"
