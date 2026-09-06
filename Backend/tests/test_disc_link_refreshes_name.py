"""#860: linking a disc to a release/boxset must refresh the auto disc name.

The four #845 write sites all refresh; the disc-link endpoints
(POST /discs/{id}/releases and /discs/{id}/boxsets) synced label_draft and
assigned the disc number but never called refresh_auto_disc_identity, so the
disc kept its scan-time name ("DVD") until some later label event. Seen live
on prod disc 18 (v1.6.13): boxset link at 13:55:38 left "DVD"; only the
season pick at 13:58:23 renamed it.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api import models


@pytest.fixture(autouse=True)
def _bypass_db_readiness_gate(monkeypatch):
    from api import main as api_main
    monkeypatch.setattr(api_main, "_check_db_ready", lambda: (True, None))


@pytest.fixture
def client(test_db, monkeypatch):
    from api import database

    def override_get_db():
        session = test_db()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[database.get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed_series_disc(test_db, *, season=5):
    """A tv movie row plus a disc frozen at the scan-time name 'DVD', with the
    movie and season already picked (the state right before the user selects
    the release/boxset)."""
    session = test_db()
    try:
        movie = models.Movie(
            id=str(uuid.uuid4()), name="Star Wars: The Clone Wars", tmdb_type="tv",
        )
        draft = {"movie_id": movie.id, "group_type": "series"}
        if season is not None:
            draft["primary_season"] = season
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=f"hash-{uuid.uuid4().hex[:16]}",
            format="DVD",
            disc_name="DVD",
            disc_slug="dvd",
            label_draft=draft,
        )
        session.add_all([movie, disc])
        session.commit()
        return movie.id, disc.id
    finally:
        session.close()


def _disc_row(test_db, disc_id):
    session = test_db()
    try:
        return session.query(models.Disc).filter(models.Disc.id == disc_id).first()
    finally:
        session.close()


def test_release_link_refreshes_auto_disc_name(client, test_db):
    movie_id, disc_id = _seed_series_disc(test_db)
    resp = client.post(
        f"/discs/{disc_id}/releases",
        json={
            "release_name": "Season 1-5 Collector's Edition",
            "release_year": 2013,
            "upc": "036000291452",
            "cover_front_url": "https://example.test/cover.jpg",
        },
    )
    assert resp.status_code == 200, resp.text
    disc = _disc_row(test_db, disc_id)
    assert disc.release_id is not None
    # Identity completed by the link — the name renders NOW, not at the next
    # unrelated label event.
    assert disc.disc_name == "Star Wars: The Clone Wars: Season 5 - Disc 1 - DVD"
    assert disc.disc_slug != "dvd"


def test_boxset_link_refreshes_auto_disc_name(client, test_db):
    movie_id, disc_id = _seed_series_disc(test_db)
    resp = client.post(
        f"/discs/{disc_id}/boxsets?movie_id={movie_id}",
        json={
            "name": "Star Wars: The Clone Wars - Season 1-5 Collector's Edition",
            "year": 2013,
            "upc": "036000291452",
            "cover_front_url": "https://example.test/box.jpg",
        },
    )
    assert resp.status_code == 200, resp.text
    disc = _disc_row(test_db, disc_id)
    assert disc.release_id is not None
    assert disc.disc_name == "Star Wars: The Clone Wars: Season 5 - Disc 1 - DVD"


def test_name_evolves_link_first_season_later(client, test_db):
    """The desired two-stage lifecycle (user-stated on #860): the link alone
    renders a proper season-less convention name IMMEDIATELY, and the later
    season pick re-renders it to the seasoned form — the first render must
    never freeze the name (that is the ef91ee9 recognition guarantee)."""
    movie_id, disc_id = _seed_series_disc(test_db, season=None)
    resp = client.post(
        f"/discs/{disc_id}/releases",
        json={
            "release_name": "Season 1-5 Collector's Edition",
            "release_year": 2013,
            "upc": "036000291452",
            "cover_front_url": "https://example.test/cover.jpg",
        },
    )
    assert resp.status_code == 200, resp.text
    # Stage 1: named at link time, without a season.
    assert _disc_row(test_db, disc_id).disc_name == "Star Wars: The Clone Wars - Disc 1 - DVD"

    # Stage 2: the season pick (the client's setPrimarySeason PATCH) re-renders.
    resp = client.patch(
        f"/discs/{disc_id}/workflow-context",
        json={"labelForm": {"primary_season": 5}},
    )
    assert resp.status_code == 200, resp.text
    disc = _disc_row(test_db, disc_id)
    assert disc.label_draft.get("primary_season") == 5
    assert disc.disc_name == "Star Wars: The Clone Wars: Season 5 - Disc 1 - DVD"


def test_movie_release_link_names_the_disc(client, test_db):
    """Movies are the WORST case of #860: they have no season pick, so
    nothing after the link ever regenerates the name — a movie disc stayed
    at its bare-format name ('Blu-Ray') until label completion. The link is
    the moment identity completes for a movie, so the name renders there."""
    session = test_db()
    try:
        movie = models.Movie(id=str(uuid.uuid4()), name="Thor: Ragnarok", tmdb_type="movie")
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=f"hash-{uuid.uuid4().hex[:16]}",
            format="Blu-Ray",
            disc_name="Blu-Ray",
            disc_slug="blu-ray",
            label_draft={"movie_id": movie.id, "group_type": "movie"},
        )
        session.add_all([movie, disc])
        session.commit()
        movie_id, disc_id = movie.id, disc.id
    finally:
        session.close()

    resp = client.post(
        f"/discs/{disc_id}/releases",
        json={
            "release_name": "Thor: Ragnarok",
            "release_year": 2017,
            "upc": "036000291452",
            "cover_front_url": "https://example.test/thor.jpg",
        },
    )
    assert resp.status_code == 200, resp.text
    disc = _disc_row(test_db, disc_id)
    assert disc.release_id is not None
    assert disc.disc_name == "Thor: Ragnarok - Blu-Ray"
    assert disc.disc_slug != "blu-ray"


def test_release_link_never_touches_user_typed_names(client, test_db):
    movie_id, disc_id = _seed_series_disc(test_db)
    session = test_db()
    try:
        disc = session.query(models.Disc).filter(models.Disc.id == disc_id).first()
        disc.disc_name = "My Special Disc"
        session.commit()
    finally:
        session.close()
    resp = client.post(
        f"/discs/{disc_id}/releases",
        json={
            "release_name": "Season 1-5 Collector's Edition",
            "release_year": 2013,
            "upc": "036000291452",
            "cover_front_url": "https://example.test/cover.jpg",
        },
    )
    assert resp.status_code == 200, resp.text
    assert _disc_row(test_db, disc_id).disc_name == "My Special Disc"
