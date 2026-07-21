"""#711: a boxset-member release must never be created nameless.

'Add to existing boxset' sends only {movie_id, boxset_id} (no release_name).
The release must still get a name (from the boxset, else the movie) — a nameless
release silently stalls the label workflow with no error surfaced.
"""
import uuid

from api import models, crud


def _seed(session, boxset_name="John Wick: Chapters 1-3"):
    boxset = models.Boxset(
        id=str(uuid.uuid4()),
        slug="jw-chapters-1-3",
        name=boxset_name,
        title=boxset_name,
        year=2020,
        upc="883929609673",
        cover_front_url="https://example.com/c.jpg",
    )
    movie = models.Movie(
        id=str(uuid.uuid4()),
        name="John Wick: Chapter 2",
        production_year=2017,
        tmdb_id=324552,
    )
    session.add_all([boxset, movie])
    session.commit()
    return boxset, movie


def test_boxset_member_release_is_named_from_boxset(test_db):
    with test_db() as session:
        boxset, movie = _seed(session)
        rel = crud.get_or_create_release(
            session,
            {"movie_id": movie.id, "boxset_id": boxset.id},  # no release_name (the reported case)
            disc_hash="HASH-CH2",
        )
        assert rel is not None
        assert (rel.name or "").strip(), "boxset-member release must not be nameless"
        assert rel.name == "John Wick: Chapters 1-3"
        assert rel.boxset_id == boxset.id


def test_backstop_names_release_when_payload_name_blank(test_db):
    # Even if release_name arrives blank, the release is named from the boxset.
    with test_db() as session:
        boxset, movie = _seed(session)
        rel = crud.get_or_create_release(
            session,
            {"movie_id": movie.id, "boxset_id": boxset.id, "release_name": "   "},
            disc_hash="HASH-CH2B",
        )
        assert (rel.name or "").strip() == "John Wick: Chapters 1-3"


def test_unnamed_boxset_is_rejected_not_created_nameless(test_db):
    # Safety: a boxset with no name can't produce a release at all (validation
    # returns None → the endpoint 400s) — it must never yield a nameless release.
    with test_db() as session:
        boxset, movie = _seed(session, boxset_name="")
        rel = crud.get_or_create_release(
            session,
            {"movie_id": movie.id, "boxset_id": boxset.id},
            disc_hash="HASH-CH2C",
        )
        assert rel is None
