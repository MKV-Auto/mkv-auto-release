"""#590 — POST /discdb/library-matches returns which of the requested
TheDiscDB search titles correspond to a movie already in the user's library.

Normalisation must collapse case + leading articles ("the"/"a"/"an") so
"The Goonies" matches a "Goonies" library entry and vice versa.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from api import database, models
from api.main import app


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


def _seed_movie(session, name: str):
    session.add(models.Movie(id=str(uuid.uuid4()), name=name, production_year=2000))
    session.commit()


def test_empty_titles_returns_empty(client):
    resp = client.post("/discdb/library-matches", json={"titles": []})
    assert resp.status_code == 200
    assert resp.json() == {"matched_titles": []}


def test_no_library_movies_returns_empty(client, test_db):
    resp = client.post("/discdb/library-matches", json={"titles": ["The Goonies", "Joker"]})
    assert resp.status_code == 200
    assert resp.json() == {"matched_titles": []}


def test_exact_case_match(client, test_db):
    session = test_db()
    try:
        _seed_movie(session, "Joker")
    finally:
        session.close()
    resp = client.post("/discdb/library-matches", json={"titles": ["Joker", "Inception"]})
    assert resp.status_code == 200
    assert resp.json() == {"matched_titles": ["Joker"]}


def test_case_insensitive_match(client, test_db):
    """Library may persist as 'Joker' while the search hit is 'JOKER'."""
    session = test_db()
    try:
        _seed_movie(session, "Joker")
    finally:
        session.close()
    resp = client.post("/discdb/library-matches", json={"titles": ["JOKER"]})
    assert resp.json() == {"matched_titles": ["JOKER"]}


def test_leading_article_normalisation(client, test_db):
    """The library entry 'Goonies' should match a search hit 'The Goonies'."""
    session = test_db()
    try:
        _seed_movie(session, "Goonies")  # library is missing the 'The'
    finally:
        session.close()
    resp = client.post("/discdb/library-matches", json={"titles": ["The Goonies"]})
    assert resp.json() == {"matched_titles": ["The Goonies"]}


def test_leading_article_in_library_matches_bare_search(client, test_db):
    """Reverse direction: library has 'The Matrix', search returns 'Matrix'."""
    session = test_db()
    try:
        _seed_movie(session, "The Matrix")
    finally:
        session.close()
    resp = client.post("/discdb/library-matches", json={"titles": ["Matrix"]})
    assert resp.json() == {"matched_titles": ["Matrix"]}


def test_an_apple_normalises_to_apple(client, test_db):
    """The 'an' article is also stripped (defensive — most movies don't use
    it, but the rule should be consistent across 'the' / 'a' / 'an')."""
    session = test_db()
    try:
        _seed_movie(session, "Apple")
    finally:
        session.close()
    resp = client.post("/discdb/library-matches", json={"titles": ["An Apple"]})
    assert resp.json() == {"matched_titles": ["An Apple"]}


def test_partial_string_does_not_match(client, test_db):
    """Only normalised-equal titles match; the user library having
    'Harry Potter and the Goblet of Fire' must NOT match a search for
    'Goblet of Fire' alone."""
    session = test_db()
    try:
        _seed_movie(session, "Harry Potter and the Goblet of Fire")
    finally:
        session.close()
    resp = client.post("/discdb/library-matches", json={"titles": ["Goblet of Fire"]})
    assert resp.json() == {"matched_titles": []}


def test_multiple_mixed_results(client, test_db):
    """Realistic shape: a search returns several titles, only a couple of
    which the user owns."""
    session = test_db()
    try:
        _seed_movie(session, "Joker")
        _seed_movie(session, "Goonies")
    finally:
        session.close()
    resp = client.post(
        "/discdb/library-matches",
        json={"titles": ["The Goonies", "Inception", "Joker", "The Matrix"]},
    )
    matched = set(resp.json()["matched_titles"])
    assert matched == {"The Goonies", "Joker"}
