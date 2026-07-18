import pytest
import uuid
from unittest.mock import patch
from fastapi.testclient import TestClient

from api import models, database
from api.main import app
from core.utils import slugify_disc_name


@pytest.fixture
def client(test_db, monkeypatch):
    def override_get_db():
        try:
            session = test_db()
            yield session
        finally:
            session.close()

    from api.routers import discs, jobs, events

    app.dependency_overrides[database.get_db] = override_get_db
    if hasattr(discs, "get_db"):
        app.dependency_overrides[discs.get_db] = override_get_db
    if hasattr(jobs, "get_db"):
        app.dependency_overrides[jobs.get_db] = override_get_db
    if hasattr(events, "get_db"):
        app.dependency_overrides[events.get_db] = override_get_db

    yield TestClient(app)
    app.dependency_overrides.clear()


def test_disc_workflow_clears_release_and_label_draft(client, test_db):
    session = test_db()
    try:
        movie = models.Movie(name="Old Movie", tmdb_id="old-tmdb")
        new_movie = models.Movie(name="New Movie", tmdb_id="new-tmdb")
        release = models.Release(slug="old-release", movie=movie)
        disc = models.Disc(
            content_hash="hash-1",
            release=release,
            disc_number=2,
            label_draft={
                "movie_id": movie.id,
                "tmdb_id": "old-tmdb",
                "release_id": release.id,
                "release_slug": "old-release",
                "release_name": "Old Release",
                "release_year": 2001,
                "boxset_id": "box-1",
                "boxset_slug": "old-boxset",
            },
        )
        session.add_all([movie, new_movie, release, disc])
        session.commit()
        session.refresh(disc)
        session.refresh(release)

        payload = {
            "labelForm": {
                "movie_id": new_movie.id,
                "tmdb_id": "new-tmdb",
                "release_id": None,
                "release_slug": None,
                "release_name": None,
                "release_year": None,
                "boxset_id": None,
                "boxset_slug": None,
            }
        }
        response = client.patch(f"/discs/{disc.id}/workflow-context", json=payload)
        assert response.status_code == 200

        session.refresh(disc)

        # Disc must remain after unlinking; only the release may be deleted when orphaned
        disc_after = session.query(models.Disc).filter(models.Disc.id == disc.id).first()
        assert disc_after is not None, "Disc must not be deleted when release is unlinked"
        assert disc_after.release_id is None
        assert disc_after.disc_number is None
        # Backend may delete the orphaned release when disc is unlinked (no other discs assigned)

        label_draft = disc.label_draft or {}
        assert label_draft.get("movie_id") == new_movie.id
        assert label_draft.get("tmdb_id") is None
        assert label_draft.get("release_id") is None
        assert label_draft.get("release_slug") is None
        assert label_draft.get("release_name") is None
        assert label_draft.get("release_year") is None
        assert label_draft.get("boxset_id") is None
        assert label_draft.get("boxset_slug") is None
    finally:
        session.close()


def test_disc_workflow_context_persists_group_type_on_label_draft(client, test_db):
    """PATCH disc workflow-context with group_type stores it on disc.label_draft (label_draft holds only movie_id and group_type)."""
    session = test_db()
    try:
        movie = models.Movie(name="Test Movie", tmdb_id="tmdb-1")
        # Release must satisfy release_link_ready (name + year + upc + cover_front_url)
        # so the PATCH endpoint accepts the labelForm — these fields are required by
        # crud.release_link_ready when the disc.release is touched.
        release = models.Release(
            slug="test-release",
            movie=movie,
            name="Test Release",
            release_year=2020,
            upc="012345678901",
            cover_front_url="https://example.com/cover.jpg",
        )
        disc = models.Disc(
            content_hash="hash-gt",
            release=release,
            disc_number=1,
            label_draft={"movie_id": movie.id},
        )
        session.add_all([movie, release, disc])
        session.commit()
        session.refresh(disc)

        payload = {
            "labelForm": {
                "movie_id": movie.id,
                "group_type": "series",
            }
        }
        response = client.patch(f"/discs/{disc.id}/workflow-context", json=payload)
        assert response.status_code == 200, response.text

        session.refresh(disc)
        label_draft = disc.label_draft or {}
        assert label_draft.get("group_type") == "series"
        assert label_draft.get("movie_id") == movie.id
    finally:
        session.close()


def test_disc_workflow_context_by_mount_discdb_hit_minimal_label_form(client):
    """GET /discs/workflow-context?mount_point=X with discdb_hit and no disc_record returns workflow_step summary and metadata from disc_info."""
    cached_disc = {
        "mount_point": "/mnt/sr1",
        "disc_num": "1",
        "discdb_hit": True,
        "movie_name": "Test Movie",
        "production_year": 2021,
        "title_type": "movie",
        "disc_format": "Blu-ray",
        "info_title": "Test Movie",
    }
    with patch("api.routers.discs.get_cached_discs", return_value=[cached_disc]):
        response = client.get("/discs/workflow-context", params={"mount_point": "/mnt/sr1"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["discdbHit"] is True
    assert data["labelForm"] is not None
    assert data["labelForm"]["workflow_step"] == "summary"
    assert data["movieName"] == "Test Movie"
    assert data["productionYear"] == 2021


def test_disc_workflow_context_miss_prefill_discdb_hit_still_miss_path(client):
    """discdb_miss_workflow_with_prefill: disc_info has discdb_hit but label_required forces full workflow."""
    cached_disc = {
        "mount_point": "/mnt/sr2",
        "disc_num": "1",
        "discdb_hit": True,
        "label_required": True,
        "movie_name": "Prefilled",
        "production_year": 1999,
        "title_type": "movie",
        "disc_format": "Blu-ray",
        "info_title": "Prefilled",
    }
    with patch("api.routers.discs.get_cached_discs", return_value=[cached_disc]):
        response = client.get("/discs/workflow-context", params={"mount_point": "/mnt/sr2"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["discdbHit"] is False
    assert data["discdb_result"] == "hit"
    assert data["labelForm"]["workflow_step"] == "film"
    assert data["movieName"] == "Prefilled"


def test_disc_workflow_context_movie_cover_fallback_to_release_cover(client, test_db):
    """When disc has a release with movie.cover_url null and release.cover_front_url set, movieCover uses release cover."""
    session = test_db()
    try:
        movie = models.Movie(name="No Poster Movie", tmdb_id="tmdb-noposter", cover_url=None)
        session.add(movie)
        session.flush()
        release = models.Release(
            slug="release-cover-test",
            movie=movie,
            cover_front_url="https://example.com/release-cover.jpg",
        )
        session.add(release)
        session.flush()
        disc = models.Disc(
            content_hash="hash-cover-fallback",
            release=release,
            disc_number=1,
        )
        session.add(disc)
        session.commit()
        session.refresh(disc)
        disc_id = str(disc.id)
    finally:
        session.close()

    cached_disc = {
        "mount_point": "/mnt/sr3",
        "disc_num": "3",
        "disc_hash": "hash-cover-fallback",
        "disc_id": disc_id,
        "discdb_hit": True,
        "movie_name": "No Poster Movie",
        "title_type": "movie",
    }
    with patch("api.routers.discs.get_cached_discs", return_value=[cached_disc]):
        response = client.get("/discs/workflow-context", params={"mount_point": "/mnt/sr3"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["movieCover"] == "https://example.com/release-cover.jpg"


def test_disc_workflow_context_regenerates_disc_slug_when_name_changes_and_slug_cleared(client, test_db):
    """Drive/disc workflow-context: changing disc_name with blank slug must not keep a stale disc_slug."""
    session = test_db()
    try:
        movie = models.Movie(id=str(uuid.uuid4()), name="Slug Regen Movie")
        release = models.Release(
            id=str(uuid.uuid4()),
            slug="slug-regen-rel",
            type="movie",
            name="Slug Regen Release",
            movie_id=movie.id,
            release_year=2020,
            upc="012345678901",
            cover_front_url="https://example.com/cover.jpg",
        )
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-slug-regen",
            release_id=release.id,
            disc_info=None,
            disc_name="Old Name",
            disc_slug="stale-slug-value",
        )
        session.add_all([movie, release, disc])
        session.commit()

        new_name = "Completely New Disc Title"
        payload = {
            "labelForm": {
                "movie_id": movie.id,
                "release_id": release.id,
                "disc_name": new_name,
                "disc_slug": "",
                "disc_format": "DVD",
                "group_type": "movie",
            },
        }
        response = client.put(f"/discs/{disc.id}/workflow-context", json=payload)
        assert response.status_code == 200, response.text
        session.refresh(disc)
        assert disc.disc_name == new_name
        assert disc.disc_slug == slugify_disc_name(new_name)
    finally:
        session.close()


def test_disc_workflow_context_slugs_disc_name_without_disc_info(client, test_db):
    """Disc PUT workflow-context must slugify disc_name when disc_info is absent (ops path)."""
    session = test_db()
    try:
        movie = models.Movie(id=str(uuid.uuid4()), name="No Info Movie")
        release = models.Release(
            id=str(uuid.uuid4()),
            slug="no-info-rel",
            type="movie",
            name="No Info Release",
            movie_id=movie.id,
            release_year=2020,
            upc="012345678901",
            cover_front_url="https://example.com/cover.jpg",
        )
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-no-disc-info",
            release_id=release.id,
            disc_info=None,
            disc_name=None,
            disc_slug=None,
        )
        session.add_all([movie, release, disc])
        session.commit()
        session.refresh(disc)

        name = "DVD Side One"
        payload = {
            "labelForm": {
                "movie_id": movie.id,
                "release_id": release.id,
                "disc_name": name,
                "disc_format": "DVD",
                "group_type": "movie",
            },
        }
        response = client.put(f"/discs/{disc.id}/workflow-context", json=payload)
        assert response.status_code == 200, response.text
        session.refresh(disc)
        assert disc.disc_name == name
        assert disc.disc_slug == slugify_disc_name(name)
    finally:
        session.close()


def test_mount_workflow_context_db_title_type_overrides_stale_disc_info_cache(client, test_db):
    """Persisted DiscTitle.type must win over disc_info.titles cache (e.g. DiscDB MainMovie vs user ignore)."""
    session = test_db()
    try:
        disc_id = str(uuid.uuid4())
        title_id = str(uuid.uuid4())
        disc = models.Disc(id=disc_id, content_hash="hash-stale-type-merge")
        title = models.DiscTitle(
            id=title_id,
            disc_id=disc_id,
            source_file="00800.mpls",
            type="ignore",
            order_index=0,
            index=0,
            comment="cached branch",
        )
        session.add_all([disc, title])
        session.commit()
    finally:
        session.close()

    cached_disc = {
        "mount_point": "/mnt/stale-type-merge",
        "disc_num": "1",
        "disc_hash": "hash-stale-type-merge",
        "titles": [
            {
                "source_file": "00800.mpls",
                "type": "MainMovie",
                "index": 0,
                "order_index": 0,
                "comment": "from cache",
            },
        ],
    }
    with patch("api.routers.discs.get_cached_discs", return_value=[cached_disc]):
        response = client.get(
            "/discs/workflow-context",
            params={"mount_point": "/mnt/stale-type-merge"},
        )
    assert response.status_code == 200, response.text
    data = response.json()
    by_src = {t["source_file"]: t for t in data["titles"]}
    assert by_src["00800.mpls"]["type"] == "ignore"
    disc_info_titles = data.get("discInfo", {}).get("titles") or {}
    if isinstance(disc_info_titles, dict):
        row = next((v for v in disc_info_titles.values() if v.get("source_file") == "00800.mpls"), None)
        assert row is not None
        assert row["type"] == "ignore"
