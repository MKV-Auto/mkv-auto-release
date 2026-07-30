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
        "metadata.json",  # film-level, since this film is new to TheDiscDB
        "release.json", "disc01.json", "disc01-summary.txt",
    }


def test_bundle_parses_tracks_from_the_stored_scan_log(client, test_db):
    """Job artifacts get cleaned up, but the scan's log copy on the disc row
    outlives them. Without this fallback every title exported with an empty
    Tracks list — which, on an update, deletes the track data upstream has
    (found live on the Predators overlay)."""
    stored_log = "\n".join([
        'MSG:3307,0,2,"File 00800.mpls was added as title #0","%1","00800.mpls","0"',
        'TINFO:0,9,0,"1:46:53"',
        'TINFO:0,26,0,"800"',
        'SINFO:0,0,1,6201,"Video"',
        'SINFO:0,0,7,0,"Mpeg4 AVC High@L4.1"',
        'SINFO:0,0,19,0,"1920x1080"',
        'SINFO:0,1,1,6202,"Audio"',
        'SINFO:0,1,7,0,"DTS-HD Master Audio"',
        'SINFO:0,1,3,0,"eng"',
        'SINFO:0,1,4,0,"English"',
        # An angle copy — MakeMKV suffixes the source file. The Predators disc
        # proved the old regex never parsed these, so the title lost its tracks.
        'MSG:3307,0,2,"File 00312.mpls(2) was added as title #1","%1","00312.mpls(2)","1"',
        'SINFO:1,0,1,6201,"Video"',
        'SINFO:1,0,7,0,"Mpeg2"',
    ])
    with test_db() as session:
        disc_id, _ = _seed_labeled_disc(session)
        disc = session.get(models.Disc, disc_id)
        disc.disc_info = {"raw_info_log": stored_log}
        session.query(models.DiscTitle).filter_by(disc_id=disc_id).update({"index": 0})
        session.commit()

    resp = client.get(f"/discdb/contributions/{disc_id}/bundle?format=json")
    assert resp.status_code == 200, resp.text
    titles = resp.json()["disc"]["Titles"]
    main = next(t for t in titles if t.get("SourceFile") == "00800.mpls")
    types = {tr.get("Type") for tr in main["Tracks"]}
    assert {"Video", "Audio"} <= types, main["Tracks"]
    assert any(tr.get("Language") == "English" for tr in main["Tracks"])
    angle = next(t for t in titles if t.get("SourceFile") == "00312.mpls(2)")
    assert [tr.get("Type") for tr in angle["Tracks"]] == ["Video"]


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


def test_export_all_readme_separates_updates_from_new_entries(client, test_db):
    """A dirty hit's files land on top of files that already exist in the
    fork. The README must say exactly which ones get replaced — and warn that
    macOS Finder's drag-and-drop replaces folder contents, which would delete
    the rest of upstream's entry."""
    from datetime import datetime, timezone

    with test_db() as session:
        _seed_labeled_disc(session)                       # new entry
        disc_id, _ = _seed_labeled_disc(session)          # dirty hit
        disc = session.query(models.Disc).filter(models.Disc.id == disc_id).first()
        disc.discdb_disc_num = 2
        disc.user_edited_at = datetime.now(timezone.utc)
        disc.disc_info = {"discdb_upstream": {
            "film_title": "Midway", "film_year": 2019,
            "release_slug": "midway-4k-collection", "disc_index": 2}}
        session.commit()

    with patch("core.discdb_export._fetch_upstream_disc_json", return_value=None):
        job_id, status = _run_export(client)
    assert status["status"] == "completed", status
    assert status["included"] == 2

    resp = client.get(f"/discdb/contributions/export-all/{job_id}/download")
    names, readme = _read_zip(resp.content)
    assert "New entries" in readme
    assert "Updates — these files REPLACE upstream's copies" in readme
    assert "data/movie/Midway (2019)/midway-4k-collection" in readme
    assert "replaces: disc02.json" in readme
    assert "macOS Finder" in readme
    # The update itself landed on upstream's path, beside the new entry.
    assert "data/movie/Midway (2019)/midway-4k-collection/disc02.json" in names
    assert "data/movie/Midway (2019)/midway-4k/disc01.json" in names
    # The job payload carries the same material, so the page can tell the
    # user what will be replaced without making them open the zip.
    upd = status["updates"]
    assert len(upd) == 1
    assert upd[0]["target"] == "data/movie/Midway (2019)/midway-4k-collection"
    assert "disc02.json" in upd[0]["files"]
    assert upd[0]["subject"].startswith("Update Midway (2019)/midway-4k-collection")


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

    def slow(db, dest=None, progress=None, should_cancel=None, disc_ids=None):
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


# ── Scoped export + eligibility (library page, #741) ─────────────────────


def _poll_export(client, job_id, timeout=15.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get(f"/discdb/contributions/export-all/{job_id}").json()
        if st["status"] in ("completed", "failed"):
            return st
        time.sleep(0.05)
    raise AssertionError("export did not settle")


def test_eligible_endpoint_matches_what_export_all_would_do(client, test_db):
    """The strip's count and the export must agree by construction."""
    with test_db() as session:
        a, _ = _seed_labeled_disc(session)
        _seed_labeled_disc(session, job_status="running")   # not finished -> out
        c, _ = _seed_labeled_disc(session)
        disc = session.query(models.Disc).filter(models.Disc.id == c).first()
        disc.discdb_disc_num = 2                            # already upstream -> out
        session.commit()

    body = client.get("/discdb/contributions/export-all/eligible").json()
    assert body["count"] == 1
    assert body["disc_ids"] == [a]


def test_scoped_export_includes_only_the_requested_discs(client, test_db):
    with test_db() as session:
        a, _ = _seed_labeled_disc(session)
        b, _ = _seed_labeled_disc(session)

    started = client.post("/discdb/contributions/export-all", json={"disc_ids": [a]})
    assert started.status_code == 202
    st = _poll_export(client, started.json()["job_id"])
    assert st["status"] == "completed"
    assert st["included"] == 1

    with test_db() as session:
        exported = {d.id for d in session.query(models.Disc)
                    .filter(models.Disc.discdb_exported_at.isnot(None)).all()}
    assert exported == {a}


def test_scoping_cannot_bypass_eligibility(client, test_db):
    """An ineligible id in the scope is ignored, never exported."""
    with test_db() as session:
        a, _ = _seed_labeled_disc(session)
        ineligible, _ = _seed_labeled_disc(session, job_status="running")

    started = client.post("/discdb/contributions/export-all",
                          json={"disc_ids": [a, ineligible]})
    st = _poll_export(client, started.json()["job_id"])
    assert st["included"] == 1


def test_unscoped_export_still_takes_everything(client, test_db):
    """The plain POST (settings page) must be unchanged by the new body."""
    with test_db() as session:
        _seed_labeled_disc(session)
        _seed_labeled_disc(session)

    started = client.post("/discdb/contributions/export-all")
    assert started.status_code == 202
    st = _poll_export(client, started.json()["job_id"])
    assert st["included"] == 2


# ── Dirty-hit detection (#741: export hits whose data the user corrected) ─


def test_a_clean_hit_stays_excluded_from_the_automatic_set(client, test_db):
    with test_db() as session:
        hit_id, _ = _seed_labeled_disc(session)
        disc = session.query(models.Disc).filter(models.Disc.id == hit_id).first()
        disc.discdb_disc_num = 1
        session.commit()

    body = client.get("/discdb/contributions/export-all/eligible").json()
    assert body["count"] == 0


def test_a_user_edit_makes_a_hit_eligible_as_an_update(client, test_db):
    """The whole point: upstream was wrong, the user fixed it locally, so the
    corrected copy flows back as an update submission."""
    from datetime import datetime, timezone

    with test_db() as session:
        hit_id, _ = _seed_labeled_disc(session)
        disc = session.query(models.Disc).filter(models.Disc.id == hit_id).first()
        disc.discdb_disc_num = 1
        disc.user_edited_at = datetime.now(timezone.utc)
        session.commit()

    body = client.get("/discdb/contributions/export-all/eligible").json()
    assert body["count"] == 1
    assert body["update_disc_ids"] == [hit_id]
    assert body["new_count"] == 0 and body["update_count"] == 1


def test_title_patch_stamps_the_disc_as_user_edited(client, test_db):
    """Dirty detection hangs off this stamp — a title edit must set it."""
    with test_db() as session:
        disc_id, _ = _seed_labeled_disc(session)
        title = session.query(models.DiscTitle).filter(
            models.DiscTitle.disc_id == disc_id).first()
        title_id = title.id
        assert session.query(models.Disc).get(disc_id).user_edited_at is None

    resp = client.patch(f"/discs/{disc_id}/titles",
                        json={"title_id": title_id, "title": "Corrected name"})
    assert resp.status_code == 200, resp.text

    with test_db() as session:
        assert session.query(models.Disc).get(disc_id).user_edited_at is not None


def test_disc_metadata_patch_stamps_the_disc(client, test_db):
    with test_db() as session:
        disc_id, _ = _seed_labeled_disc(session)

    resp = client.patch(f"/releases/disc/{disc_id}", json={"disc_name": "Corrected"})
    assert resp.status_code == 200, resp.text

    with test_db() as session:
        assert session.query(models.Disc).get(disc_id).user_edited_at is not None


def test_technical_title_edits_do_not_dirty_the_disc(client, test_db):
    """The filename (comment) and other technical values differ between any
    two rips without upstream being wrong — they are not corrections, and a
    re-save of the same value is not an edit at all."""
    with test_db() as session:
        disc_id, _ = _seed_labeled_disc(session)
        title = session.query(models.DiscTitle).filter(
            models.DiscTitle.disc_id == disc_id).first()
        title_id = title.id

    resp = client.patch(f"/discs/{disc_id}/titles",
                        json={"title_id": title_id, "comment": "Midway_t00.mkv"})
    assert resp.status_code == 200, resp.text
    with test_db() as session:
        assert session.query(models.Disc).get(disc_id).user_edited_at is None

    resp = client.patch(f"/discs/{disc_id}/titles",
                        json={"title_id": title_id, "title": "Midway"})
    assert resp.status_code == 200, resp.text
    with test_db() as session:
        assert session.query(models.Disc).get(disc_id).user_edited_at is None

    # An actual correction on a user surface still stamps.
    resp = client.patch(f"/discs/{disc_id}/titles",
                        json={"title_id": title_id, "season": 1})
    assert resp.status_code == 200, resp.text
    with test_db() as session:
        assert session.query(models.Disc).get(disc_id).user_edited_at is not None


def test_organizational_disc_edits_do_not_dirty_the_disc(client, test_db):
    """Renumbering or re-slugging is local organization, not 'TheDiscDB has
    this wrong' — and saving the same name back is a no-op."""
    with test_db() as session:
        disc_id, _ = _seed_labeled_disc(session)

    resp = client.patch(f"/releases/disc/{disc_id}",
                        json={"disc_number": 3, "disc_slug": "midway-4k-d3"})
    assert resp.status_code == 200, resp.text
    with test_db() as session:
        assert session.query(models.Disc).get(disc_id).user_edited_at is None

    resp = client.patch(f"/releases/disc/{disc_id}", json={"disc_name": "MIDWAY_4K"})
    assert resp.status_code == 200, resp.text
    with test_db() as session:
        assert session.query(models.Disc).get(disc_id).user_edited_at is None


def test_export_all_skips_updates_that_match_upstream(client, test_db):
    """A dirty hit whose merged content equals TheDiscDB's committed file is
    churn, not a correction — nothing ships, nothing is stamped exported, and
    the job says why instead of a generic 'nothing eligible'."""
    from datetime import datetime, timezone

    with test_db() as session:
        disc_id, _ = _seed_labeled_disc(session)
        disc = session.query(models.Disc).filter(models.Disc.id == disc_id).first()
        disc.discdb_disc_num = 2
        disc.user_edited_at = datetime.now(timezone.utc)
        disc.disc_info = {"discdb_upstream": {
            "film_title": "Midway", "film_year": 2019,
            "release_slug": "midway-4k-collection", "disc_index": 2}}
        session.commit()

    with patch("core.discdb_export._fetch_upstream_disc_json",
               return_value={"Titles": []}), \
         patch("core.discdb_export._update_change_summary", return_value=[]):
        _, status = _run_export(client)

    assert status["status"] == "failed"
    assert "already match" in (status["error"] or "")
    with test_db() as session:
        assert session.get(models.Disc, disc_id).discdb_contribution_status is None


def test_scoping_allows_a_clean_hit_when_a_human_picked_it(client, test_db):
    """Detection must not override judgement — 'upstream is stale in ways we
    cannot detect' is a call only the user can make."""
    with test_db() as session:
        hit_id, _ = _seed_labeled_disc(session)
        disc = session.query(models.Disc).filter(models.Disc.id == hit_id).first()
        disc.discdb_disc_num = 1
        session.commit()

    # A hit without stored coordinates live-resolves against TheDiscDB and,
    # resolved, fetches their committed file — keep the test off the network.
    with patch("core.discdb_export._resolve_upstream_coords", return_value=None), \
         patch("core.discdb_export._fetch_upstream_disc_json", return_value=None):
        started = client.post("/discdb/contributions/export-all",
                              json={"disc_ids": [hit_id]})
        st = _poll_export(client, started.json()["job_id"])
    assert st["status"] == "completed"
    assert st["included"] == 1


def test_scoping_still_refuses_an_unfinished_job(client, test_db):
    """Human selection bypasses the hit rule, never the finished-job rule."""
    with test_db() as session:
        unfinished, _ = _seed_labeled_disc(session, job_status="running")

    started = client.post("/discdb/contributions/export-all",
                          json={"disc_ids": [unfinished]})
    st = _poll_export(client, started.json()["job_id"])
    assert st["status"] == "failed"          # nothing eligible -> failed job
