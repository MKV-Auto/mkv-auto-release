"""Reuse must never overwrite a field someone already set (#821).

``get_or_create_release`` can resolve to an existing release — by disc hash, by
(movie, UPC), or by (movie, boxset). It then used to merge the caller's payload
into that release with ``rel.x = payload.get("x") or rel.x`` for every field,
which meant resolving to a release *rewrote* it.

That destroyed real data in production: a Library edit was saved at 17:07 and
reverted at 17:08 by a disc-labeling create that resolved to the same release
and carried the pre-edit snapshot. Both requests returned 200 and nothing
surfaced the loss.

Blank fields are still filled — that is the useful half of the behaviour (DiscDB
backfilling a release that has no cover yet).
"""
import uuid

import pytest

from api import crud, models


def _movie(session, name="Star Wars Rebels"):
    m = models.Movie(id=str(uuid.uuid4()), name=name)
    session.add(m)
    session.flush()
    return m


def _release(session, movie, **over):
    fields = dict(
        id=str(uuid.uuid4()),
        slug=f"rel-{uuid.uuid4().hex[:8]}",
        type="series",
        name="Star Wars Rebels: Complete Season Three",
        movie_id=movie.id,
        upc="786936850840",
        asin="B01GDJZJZ2",
        release_year=2016,
        cover_front_url="https://example.com/front.jpg",
    )
    fields.update(over)
    rel = models.Release(**fields)
    session.add(rel)
    session.flush()
    return rel


def _disc(session, release=None, content_hash=None):
    d = models.Disc(
        id=str(uuid.uuid4()),
        content_hash=content_hash or f"h-{uuid.uuid4().hex[:12]}",
        release_id=release.id if release else None,
    )
    session.add(d)
    session.flush()
    return d


class TestReuseDoesNotOverwrite:
    def test_the_production_timeline(self, test_db):
        """Library edit, then a create carrying the pre-edit snapshot."""
        session = test_db()
        try:
            movie = _movie(session)
            rel = _release(session, movie)
            disc = _disc(session, release=rel)
            session.commit()

            # 17:07 — the user edits the release in the Library.
            rel.asin = "B0EDITED01"
            rel.release_year = 2017
            rel.cover_front_url = "https://example.com/user-chosen.jpg"
            session.commit()

            # 17:08 — a create resolves to this release, carrying stale values.
            stale = {
                "movie_id": movie.id,
                "release_name": "Star Wars Rebels: Complete Season Three",
                "upc": "786936850840",
                "asin": "B01GDJZJZ2",                       # pre-edit
                "release_year": 2016,                        # pre-edit
                "cover_front_url": "https://example.com/front.jpg",  # pre-edit
            }
            got = crud.get_or_create_release(session, stale, disc.content_hash)

            assert got is not None and got.id == rel.id, "should still resolve to the same release"
            session.refresh(rel)
            assert rel.asin == "B0EDITED01", "the Library edit must survive"
            assert rel.release_year == 2017
            assert rel.cover_front_url == "https://example.com/user-chosen.jpg"
        finally:
            session.close()

    def test_upc_match_path_does_not_overwrite(self, test_db):
        # Standalone (movie, UPC) reuse — the path the reported case hit.
        session = test_db()
        try:
            movie = _movie(session)
            rel = _release(session, movie, asin="B0KEEPME01")
            session.commit()

            payload = {
                "movie_id": movie.id,
                "upc": "786936850840",          # matches -> resolves to rel
                "asin": "B0DIFFERENT",          # must NOT win
                "release_name": "Something Else",
                "release_year": 2099,
                "cover_front_url": "https://example.com/other.jpg",
            }
            got = crud.get_or_create_release(session, payload, None)

            assert got is not None and got.id == rel.id
            session.refresh(rel)
            assert rel.asin == "B0KEEPME01"
            assert rel.release_year == 2016
            assert rel.name == "Star Wars Rebels: Complete Season Three"
            assert rel.cover_front_url == "https://example.com/front.jpg"
        finally:
            session.close()

    @pytest.mark.parametrize("blank", [None, ""])
    def test_blank_fields_are_still_filled(self, test_db, blank):
        # The useful half: DiscDB backfilling what the release does not have.
        session = test_db()
        try:
            movie = _movie(session)
            rel = _release(session, movie, asin=blank, cover_back_url=blank)
            disc = _disc(session, release=rel)
            session.commit()

            crud.get_or_create_release(
                session,
                {
                    "movie_id": movie.id,
                    "asin": "B0FILLED001",
                    "cover_back_url": "https://example.com/back.jpg",
                },
                disc.content_hash,
            )
            session.refresh(rel)
            assert rel.asin == "B0FILLED001", "an empty field should still be filled"
            assert rel.cover_back_url == "https://example.com/back.jpg"
        finally:
            session.close()

    def test_creating_a_genuinely_new_release_still_works(self, test_db):
        # Guard against over-correcting into "never create".
        session = test_db()
        try:
            movie = _movie(session)
            existing = _release(session, movie)
            session.commit()

            got = crud.get_or_create_release(
                session,
                {
                    "movie_id": movie.id,
                    "release_name": "Star Wars Rebels: Complete Season Four",
                    "release_year": 2017,
                    "upc": "786936857788",   # different UPC -> no match
                    "cover_front_url": "https://example.com/s4.jpg",
                },
                None,
            )
            assert got is not None
            assert got.id != existing.id, "a distinct UPC must create a new release"
            assert got.name == "Star Wars Rebels: Complete Season Four"
        finally:
            session.close()


class TestHelperDirectly:
    def test_reports_only_the_fields_it_filled(self, test_db):
        session = test_db()
        try:
            movie = _movie(session)
            rel = _release(session, movie, asin=None)
            session.commit()

            filled = crud._fill_blank_release_fields(
                rel, {"asin": "B0NEW00001", "upc": "999999999999"}
            )
            assert filled == ["asin"], "upc was already set, so it is not reported"
            assert rel.upc == "786936850840"
        finally:
            session.close()

    def test_empty_payload_values_never_blank_an_existing_field(self, test_db):
        session = test_db()
        try:
            movie = _movie(session)
            rel = _release(session, movie)
            session.commit()

            crud._fill_blank_release_fields(rel, {"asin": "", "upc": None})
            assert rel.asin == "B01GDJZJZ2"
            assert rel.upc == "786936850840"
        finally:
            session.close()
