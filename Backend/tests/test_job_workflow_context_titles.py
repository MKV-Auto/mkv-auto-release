import uuid

import pytest
from fastapi.testclient import TestClient

from api import database, models
from api.main import app


@pytest.fixture
def client(test_db):
    def override_get_db():
        try:
            session = test_db()
            yield session
        finally:
            session.close()

    from api.routers import jobs

    app.dependency_overrides[database.get_db] = override_get_db
    if hasattr(jobs, "get_db"):
        app.dependency_overrides[jobs.get_db] = override_get_db

    yield TestClient(app)
    app.dependency_overrides.clear()


def test_job_workflow_context_get_includes_release_id_when_label_draft_matches_disc(client, test_db):
    """labelForm must expose release_id when label_draft matches disc.release (stable PATCH merge)."""
    session = test_db()
    try:
        movie = models.Movie(id=str(uuid.uuid4()), name="Draft Match Movie")
        box = models.Boxset(
            id=str(uuid.uuid4()),
            slug="draft-match-box",
            name="Draft Match Box",
        )
        release = models.Release(
            id=str(uuid.uuid4()),
            slug="draft-match-rel",
            type="movie",
            name="Draft Match Release",
            movie_id=movie.id,
            boxset_id=box.id,
        )
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-draft-match",
            release_id=release.id,
            label_draft={
                "movie_id": movie.id,
                "release_id": str(release.id),
                "boxset_id": str(box.id),
                "group_type": "movie",
            },
        )
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            job_status="running",
            rip_state="completed",
        )
        session.add_all([movie, box, release, disc, job])
        session.commit()

        response = client.get(f"/jobs/{job.id}/workflow-context")
        assert response.status_code == 200, response.text
        lf = response.json()["labelForm"]
        assert lf.get("release_id") == str(release.id)
        assert lf.get("boxset_id") == str(box.id)
    finally:
        session.close()


def test_job_workflow_context_prefers_db_disc_fields_over_stale_label_payload(client, test_db):
    """labelForm disc_name/slug/number come from Disc row, not stale label_payload in disc_payload."""
    session = test_db()
    try:
        movie = models.Movie(id=str(uuid.uuid4()), name="WF Disc Fields Movie")
        release = models.Release(
            id=str(uuid.uuid4()),
            slug="wf-disc-fields-rel",
            type="movie",
            name="WF Disc Fields Release",
            movie_id=movie.id,
        )
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-wf-disc-fields",
            release_id=release.id,
            disc_name="Canonical Disc Name",
            disc_slug="canonical-slug",
            disc_number=2,
            format="UHD",
        )
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            job_status="running",
            rip_state="completed",
            disc_payload={
                "label_payload": {
                    "disc_name": "Stale Payload Name",
                    "disc_slug": "stale-slug",
                    "disc_number": 9,
                    "disc_format": "DVD",
                    "group_type": "movie",
                }
            },
        )
        session.add_all([movie, release, disc, job])
        session.commit()

        response = client.get(f"/jobs/{job.id}/workflow-context")
        assert response.status_code == 200, response.text
        lf = response.json()["labelForm"]
        assert lf.get("disc_name") == "Canonical Disc Name"
        assert lf.get("disc_slug") == "canonical-slug"
        assert lf.get("disc_number") == 2
        assert lf.get("disc_format") == "UHD"
    finally:
        session.close()


def test_job_workflow_context_merges_payload_titles(client, test_db):
    session = test_db()
    try:
        movie = models.Movie(id=str(uuid.uuid4()), name="Test Movie")
        release = models.Release(
            id=str(uuid.uuid4()),
            slug="test-release",
            type="movie",
            name="Test Release",
            movie_id=movie.id,
        )
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-1",
            release_id=release.id,
        )
        title = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="00001.mpls",
            title="Episode 1",
        )
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            job_status="running",
            rip_state="completed",
            disc_payload={
                "titles": {
                    "00001.mpls": {
                        "file": "00001.mpls",
                        "chapters": {"count": 12},
                        "streams": {"video": ["h264"]},
                        "output_file": "movie_t01.mkv",
                    }
                }
            },
        )
        session.add_all([movie, release, disc, title, job])
        session.commit()

        response = client.get(f"/jobs/{job.id}/workflow-context")
        assert response.status_code == 200, response.text
        data = response.json()
        titles = data.get("titles", [])
        assert titles, "Expected titles in workflow context"
        target = next((t for t in titles if str(t.get("title_id")) == str(title.id)), None)
        assert target, "Expected title_id to be present in workflow context titles"
        assert target.get("chapters", {}).get("count") == 12
        assert target.get("streams") == {"video": ["h264"]}
        assert target.get("output_file") == "movie_t01.mkv"
    finally:
        session.close()


def test_job_workflow_context_no_auto_create_release(client, test_db):
    """Saving job workflow context with movie_id but no release_id does NOT create a release or set disc.release_id."""
    session = test_db()
    try:
        movie = models.Movie(id=str(uuid.uuid4()), name="Test Movie")
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-no-rel",
            release_id=None,
        )
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            job_status="running",
            rip_state="completed",
        )
        session.add_all([movie, disc, job])
        session.commit()
        session.refresh(disc)

        release_count_before = session.query(models.Release).count()

        payload = {
            "labelForm": {
                "movie_id": movie.id,
                "workflow_step": "boxset",
            },
        }
        response = client.put(f"/jobs/{job.id}/workflow-context", json=payload)
        assert response.status_code == 200, response.text

        session.refresh(disc)
        release_count_after = session.query(models.Release).count()

        assert disc.release_id is None
        assert release_count_after == release_count_before, "No release should be created"
    finally:
        session.close()


def test_job_workflow_context_assigns_release_when_release_id_provided(client, test_db):
    """Saving job workflow context with release_id assigns that release to the disc (no new release created)."""
    session = test_db()
    try:
        movie = models.Movie(id=str(uuid.uuid4()), name="Test Movie")
        release = models.Release(
            id=str(uuid.uuid4()),
            slug="existing-release",
            type="movie",
            name="Existing Release",
            movie_id=movie.id,
        )
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-assign",
            release_id=None,
        )
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            job_status="running",
            rip_state="completed",
        )
        session.add_all([movie, release, disc, job])
        session.commit()
        session.refresh(disc)
        session.refresh(release)

        payload = {
            "labelForm": {
                "movie_id": movie.id,
                "release_id": release.id,
                "workflow_step": "boxset",
            },
        }
        response = client.put(f"/jobs/{job.id}/workflow-context", json=payload)
        assert response.status_code == 200, response.text

        session.refresh(disc)
        assert disc.release_id == release.id
    finally:
        session.close()


def test_job_workflow_context_clear_release_deletes_orphan(client, test_db):
    """Saving job workflow context with release_id: null clears disc.release_id and deletes the release if it had no other discs."""
    session = test_db()
    try:
        movie = models.Movie(id=str(uuid.uuid4()), name="Orphan Movie")
        release = models.Release(
            id=str(uuid.uuid4()),
            slug="orphan-release",
            type="movie",
            name="Orphan Release",
            movie_id=movie.id,
        )
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-orphan",
            release_id=release.id,
        )
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            job_status="running",
            rip_state="completed",
        )
        session.add_all([movie, release, disc, job])
        session.commit()
        session.refresh(disc)
        session.refresh(release)
        old_release_id = release.id

        release_count_before = session.query(models.Release).count()

        payload = {
            "labelForm": {
                "movie_id": movie.id,
                "release_id": None,
                "workflow_step": "boxset",
            },
        }
        response = client.put(f"/jobs/{job.id}/workflow-context", json=payload)
        assert response.status_code == 200, response.text

        session.refresh(disc)
        release_count_after = session.query(models.Release).count()
        old_release_gone = session.query(models.Release).filter(models.Release.id == old_release_id).first() is None

        # Disc must remain after unlinking; only the release may be deleted when orphaned
        disc_after = session.query(models.Disc).filter(models.Disc.id == disc.id).first()
        assert disc_after is not None, "Disc must not be deleted when release is unlinked"
        assert disc_after.release_id is None
        assert release_count_after == release_count_before - 1
        assert old_release_gone
    finally:
        session.close()


def test_job_workflow_context_reassign_release_deletes_orphan(client, test_db):
    """Reassigning disc to a different release deletes the previous release if it had no other discs."""
    session = test_db()
    try:
        movie = models.Movie(id=str(uuid.uuid4()), name="Movie")
        release_a = models.Release(
            id=str(uuid.uuid4()),
            slug="release-a",
            type="movie",
            name="Release A",
            movie_id=movie.id,
        )
        release_b = models.Release(
            id=str(uuid.uuid4()),
            slug="release-b",
            type="movie",
            name="Release B",
            movie_id=movie.id,
        )
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-reassign",
            release_id=release_a.id,
        )
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            job_status="running",
            rip_state="completed",
        )
        session.add_all([movie, release_a, release_b, disc, job])
        session.commit()
        session.refresh(disc)
        session.refresh(release_a)
        session.refresh(release_b)
        release_a_id = release_a.id

        payload = {
            "labelForm": {
                "movie_id": movie.id,
                "release_id": release_b.id,
                "workflow_step": "boxset",
            },
        }
        response = client.put(f"/jobs/{job.id}/workflow-context", json=payload)
        assert response.status_code == 200, response.text

        session.refresh(disc)
        release_a_gone = session.query(models.Release).filter(models.Release.id == release_a_id).first() is None

        assert disc.release_id == release_b.id
        assert release_a_gone
    finally:
        session.close()


def test_job_workflow_context_cleared_movie_id_stays_cleared(client, test_db):
    """Test that when movie_id is explicitly cleared (set to None), it stays None and doesn't revert to disc's old value."""
    session = test_db()
    try:
        # Create a movie and disc with that movie
        movie = models.Movie(id=str(uuid.uuid4()), name="Original Movie")
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-clear-test",
            release_id=None,
        )
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            job_status="running",
            rip_state="completed",
            disc_payload={
                "movie_id": movie.id,  # Disc payload has a movie_id
            },
        )
        session.add_all([movie, disc, job])
        session.commit()

        # First, set movie_id in label_draft
        response = client.patch(
            f"/jobs/{job.id}/workflow-context",
            json={"labelForm": {"movie_id": movie.id}},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["labelForm"]["movie_id"] == movie.id

        # Now explicitly clear movie_id by setting it to None
        response = client.patch(
            f"/jobs/{job.id}/workflow-context",
            json={"labelForm": {"movie_id": None}},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        
        # The bug was: movie_id would revert to disc_payload's movie_id
        # After fix: movie_id should be None
        assert data["labelForm"]["movie_id"] is None, (
            f"Expected movie_id to be None after clearing, but got {data['labelForm']['movie_id']}. "
            f"This indicates the old value from disc_payload is being returned instead of the cleared value."
        )

        # Verify the disc's label_draft was actually updated
        session.refresh(disc)
        label_draft = disc.label_draft or {}
        assert "movie_id" in label_draft, "movie_id should be explicitly stored in label_draft"
        assert label_draft["movie_id"] is None, "label_draft should have movie_id=None"

    finally:
        session.close()


def test_job_workflow_context_clear_movie_does_not_relink_stale_release_id(client, test_db):
    """
    PATCH merge can leave release_id from GET while movie_id is cleared (film-step Change).
    The disc must stay unlinked and label_draft must not keep the old release_id.
    """
    session = test_db()
    try:
        movie = models.Movie(id=str(uuid.uuid4()), name="Relink Guard Movie")
        release = models.Release(
            id=str(uuid.uuid4()),
            slug="relink-guard-release",
            type="movie",
            name="Relink Guard Release",
            movie_id=movie.id,
        )
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-relink-guard",
            release_id=release.id,
        )
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            job_status="running",
            rip_state="completed",
        )
        session.add_all([movie, release, disc, job])
        session.commit()
        session.refresh(disc)

        response = client.patch(
            f"/jobs/{job.id}/workflow-context",
            json={"labelForm": {"movie_id": None}},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["labelForm"]["movie_id"] is None
        rid = data["labelForm"].get("release_id")
        assert rid is None or rid == "", f"expected release cleared in response, got {rid!r}"

        session.refresh(disc)
        assert disc.release_id is None
        ld = disc.label_draft or {}
        assert ld.get("release_id") in (None, ""), f"label_draft should drop release_id, got {ld.get('release_id')!r}"
        assert ld.get("boxset_id") in (None, ""), f"label_draft should drop boxset_id, got {ld.get('boxset_id')!r}"
    finally:
        session.close()


def test_job_workflow_context_unlinked_disc_preserves_boxset_when_release_id_null_in_payload(client, test_db):
    """Pending disc: merged labelForm includes release_id null; boxset_id must stay in label_draft (no spurious clear)."""
    session = test_db()
    try:
        movie = models.Movie(id=str(uuid.uuid4()), name="Box Movie")
        box = models.Boxset(
            id=str(uuid.uuid4()),
            slug="test-boxset-slug",
            name="Test Boxset",
        )
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-pending-box",
            release_id=None,
        )
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            job_status="running",
            rip_state="completed",
        )
        session.add_all([movie, box, disc, job])
        session.commit()

        payload = {
            "labelForm": {
                "movie_id": movie.id,
                "release_id": None,
                "boxset_id": box.id,
                "workflow_step": "boxset",
            },
        }
        response = client.put(f"/jobs/{job.id}/workflow-context", json=payload)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["labelForm"]["boxset_id"] == box.id

        session.refresh(disc)
        ld = disc.label_draft or {}
        assert ld.get("boxset_id") == box.id
        assert disc.release_id is None
    finally:
        session.close()


def test_job_workflow_context_patch_boxset_only_preserves_after_merge(client, test_db):
    """PATCH merges server labelForm (release_id null) with patch; boxset_id must persist."""
    session = test_db()
    try:
        movie = models.Movie(id=str(uuid.uuid4()), name="Patch Movie")
        box = models.Boxset(
            id=str(uuid.uuid4()),
            slug="patch-boxset-slug",
            name="Patch Boxset",
        )
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-patch-box",
            release_id=None,
        )
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            job_status="running",
            rip_state="completed",
        )
        session.add_all([movie, box, disc, job])
        session.commit()

        r1 = client.put(
            f"/jobs/{job.id}/workflow-context",
            json={"labelForm": {"movie_id": movie.id, "workflow_step": "film"}},
        )
        assert r1.status_code == 200, r1.text

        r2 = client.patch(
            f"/jobs/{job.id}/workflow-context",
            json={"labelForm": {"boxset_id": box.id}},
        )
        assert r2.status_code == 200, r2.text
        data = r2.json()
        assert data["labelForm"]["boxset_id"] == box.id
        assert data["labelForm"]["movie_id"] == movie.id

        session.refresh(disc)
        ld = disc.label_draft or {}
        assert ld.get("boxset_id") == box.id
    finally:
        session.close()


def test_job_workflow_context_unlink_release_preserves_boxset_in_label_draft(client, test_db):
    """Unlinking disc from release while keeping boxset_id in the form must not wipe draft boxset."""
    session = test_db()
    try:
        movie = models.Movie(id=str(uuid.uuid4()), name="Unlink Movie")
        box = models.Boxset(
            id=str(uuid.uuid4()),
            slug="unlink-box-slug",
            name="Unlink Boxset",
        )
        release = models.Release(
            id=str(uuid.uuid4()),
            slug="standalone-for-unlink",
            type="movie",
            name="Standalone",
            movie_id=movie.id,
        )
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-unlink-box",
            release_id=release.id,
        )
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            job_status="running",
            rip_state="completed",
        )
        session.add_all([movie, box, release, disc, job])
        session.commit()
        session.refresh(disc)

        payload = {
            "labelForm": {
                "movie_id": movie.id,
                "release_id": None,
                "boxset_id": box.id,
                "workflow_step": "boxset",
            },
        }
        response = client.put(f"/jobs/{job.id}/workflow-context", json=payload)
        assert response.status_code == 200, response.text
        data = response.json()
        # API may omit release_id when unset rather than returning null
        assert data["labelForm"].get("release_id") in (None, "")
        assert data["labelForm"]["boxset_id"] == box.id

        session.refresh(disc)
        assert disc.release_id is None
        ld = disc.label_draft or {}
        assert ld.get("boxset_id") == box.id
    finally:
        session.close()


def test_job_workflow_context_auto_sluggifies_disc_name_when_slug_blank(client, test_db):
    """Blank disc_slug in labelForm should persist slug derived from disc_name (PUT workflow-context)."""
    from core.utils import slugify_disc_name

    session = test_db()
    try:
        movie = models.Movie(id=str(uuid.uuid4()), name="Slug Auto Movie")
        release = models.Release(
            id=str(uuid.uuid4()),
            slug="slug-auto-rel",
            type="movie",
            name="Slug Auto Release",
            movie_id=movie.id,
        )
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-slug-auto",
            release_id=release.id,
            disc_name=None,
            disc_slug=None,
        )
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            job_status="running",
            rip_state="completed",
        )
        session.add_all([movie, release, disc, job])
        session.commit()

        name = "Blu-Ray - Bonus Features"
        payload = {
            "labelForm": {
                "movie_id": movie.id,
                "release_id": release.id,
                "disc_name": name,
                "disc_slug": "",
                "disc_format": "Blu-Ray",
                "workflow_step": "disc",
            },
        }
        response = client.put(f"/jobs/{job.id}/workflow-context", json=payload)
        assert response.status_code == 200, response.text
        session.refresh(disc)
        assert disc.disc_name == name
        assert disc.disc_slug == slugify_disc_name(name)
    finally:
        session.close()


def test_job_workflow_context_explicit_disc_slug_wins_over_auto(client, test_db):
    session = test_db()
    try:
        movie = models.Movie(id=str(uuid.uuid4()), name="Explicit Slug Movie")
        release = models.Release(
            id=str(uuid.uuid4()),
            slug="explicit-slug-rel",
            type="movie",
            name="Explicit Slug Release",
            movie_id=movie.id,
        )
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-explicit-slug",
            release_id=release.id,
            disc_name=None,
            disc_slug=None,
        )
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            job_status="running",
            rip_state="completed",
        )
        session.add_all([movie, release, disc, job])
        session.commit()

        payload = {
            "labelForm": {
                "movie_id": movie.id,
                "release_id": release.id,
                "disc_name": "Any Name Here",
                "disc_slug": "custom-slug-from-user",
                "disc_format": "DVD",
                "workflow_step": "disc",
            },
        }
        response = client.put(f"/jobs/{job.id}/workflow-context", json=payload)
        assert response.status_code == 200, response.text
        session.refresh(disc)
        assert disc.disc_slug == "custom-slug-from-user"
    finally:
        session.close()


def test_job_workflow_context_patch_ignores_regressive_workflow_step(client, test_db):
    """PATCH must not move job.workflow_step backward vs persisted row (stale client after UI-only back)."""
    session = test_db()
    try:
        movie = models.Movie(id=str(uuid.uuid4()), name="Step Guard Movie")
        release = models.Release(
            id=str(uuid.uuid4()),
            slug="step-guard-rel",
            type="movie",
            name="Step Guard Release",
            movie_id=movie.id,
        )
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-step-guard",
            release_id=release.id,
            disc_name="Disc A",
            disc_slug="disc-a",
        )
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            job_status="running",
            rip_state="completed",
            stage_profile="miss",  # 'titles'/'disc' steps only exist in the miss step order
            workflow_step="titles",
        )
        session.add_all([movie, release, disc, job])
        session.commit()

        response = client.patch(
            f"/jobs/{job.id}/workflow-context",
            json={
                "labelForm": {
                    "movie_id": movie.id,
                    "release_id": release.id,
                    "workflow_step": "disc",
                    "disc_name": "Disc A",
                    "disc_slug": "disc-a",
                    "disc_format": "Blu-Ray",
                }
            },
        )
        assert response.status_code == 200, response.text
        session.refresh(job)
        assert job.workflow_step == "titles"
    finally:
        session.close()
