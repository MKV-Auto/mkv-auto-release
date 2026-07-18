"""Regression tests for POST /discs/{disc_id}/movies upsert behavior (#389 follow-up).

The endpoint used to 400 on duplicate ``tmdb_id``, which broke the Use This
suggestion flow whenever the TMDB backfill (#388) or a prior label had
already created the movie row. It now ensures-and-links instead.
"""
import pytest
from fastapi.testclient import TestClient

from api.main import app
from api import crud, models


@pytest.fixture(autouse=True)
def _bypass_db_readiness_gate(monkeypatch):
    from api import main as api_main
    monkeypatch.setattr(api_main, "_check_db_ready", lambda: (True, None))


@pytest.fixture
def client(test_db, monkeypatch):
    """TestClient wired to the per-test SQLite DB."""
    from api import database
    from api.routers import discs as discs_router
    from api.routers import movies as movies_router

    def override_get_db():
        session = test_db()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[database.get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_disc(test_db) -> str:
    session = test_db()
    try:
        disc = crud.persist_disc_scan_with_discdb(session, "hash-upsert-1", {
            "info_title": "Fallout Season Two Disc 1",
            "disc_hash": "hash-upsert-1",
        })
        return disc.id
    finally:
        session.close()


def test_first_call_creates_movie(client, test_db):
    disc_id = _make_disc(test_db)
    resp = client.post(
        f"/discs/{disc_id}/movies",
        json={"name": "Fallout", "production_year": 2024, "tmdb_id": "106379", "tmdb_type": "tv"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["movie"]["tmdb_id"] == "106379"
    assert body["movie"]["name"] == "Fallout"


def test_duplicate_tmdb_id_returns_existing_movie_no_400(client, test_db):
    """The headline regression: a second POST with the same tmdb_id used to
    400 and break the Use This flow. It should now return the existing
    movie row instead."""
    disc_id = _make_disc(test_db)
    first = client.post(
        f"/discs/{disc_id}/movies",
        json={"name": "Fallout", "production_year": 2024, "tmdb_id": "106379", "tmdb_type": "tv"},
    )
    assert first.status_code == 200, first.text
    first_id = first.json()["movie"]["id"]

    # Second call with the same tmdb_id — must NOT 400, must return the
    # same Movie row (no duplicate created).
    second = client.post(
        f"/discs/{disc_id}/movies",
        json={"name": "Fallout", "production_year": 2024, "tmdb_id": "106379", "tmdb_type": "tv"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["movie"]["id"] == first_id


def test_duplicate_tmdb_id_links_movie_to_new_disc(client, test_db):
    """Upsert semantics: a different disc can link the same existing movie."""
    disc1_id = _make_disc(test_db)
    client.post(
        f"/discs/{disc1_id}/movies",
        json={"name": "Fallout", "production_year": 2024, "tmdb_id": "106379", "tmdb_type": "tv"},
    )

    # Insert a second disc, link the same movie.
    session = test_db()
    try:
        disc2 = crud.persist_disc_scan_with_discdb(session, "hash-upsert-2", {
            "info_title": "Fallout Season Two Disc 2",
            "disc_hash": "hash-upsert-2",
        })
        disc2_id = disc2.id
    finally:
        session.close()

    resp = client.post(
        f"/discs/{disc2_id}/movies",
        json={"name": "Fallout", "production_year": 2024, "tmdb_id": "106379", "tmdb_type": "tv"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["movie"]["tmdb_id"] == "106379"
    # The second disc's label_draft now has the movie_id.
    session = test_db()
    try:
        disc2 = session.query(models.Disc).filter(models.Disc.id == disc2_id).first()
        assert disc2.label_draft is not None
        assert disc2.label_draft.get("movie_id") == resp.json()["movie"]["id"]
    finally:
        session.close()
