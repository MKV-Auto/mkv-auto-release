"""labelForm.tmdb_id must follow the disc's own movie link.

The TMDB episode picker renders nothing without a catalog, and the
frontend only prefetches one when `labelForm.tmdb_id` is set
(`_prefetchTmdbEpisodeCatalog` returns early on `if (!tmdb_id) return`).
That field used to be sourced only from `disc_info`, so a disc labeled
through the normal flow — which records the series on the linked Movie —
came back with "" and the picker was silently absent on every Episode row.

Observed on Star Wars Rebels S3: disc -> release -> movie held tmdb_id
60554 and the episodes endpoint served the full season, while
labelForm.tmdb_id was empty.
"""
import uuid

import pytest

from api import models
from api.routers.discs import _build_labelform_from_disc, _linked_movie_tmdb_id


def _disc_linked_to(session, *, tmdb_id, disc_info=None):
    movie = models.Movie(id=str(uuid.uuid4()), name="Star Wars Rebels", tmdb_id=tmdb_id)
    release = models.Release(id=str(uuid.uuid4()), name="Rebels: Complete", type="series",
                             slug="rebels", movie_id=movie.id)
    disc = models.Disc(id=str(uuid.uuid4()), content_hash=str(uuid.uuid4()),
                       release_id=release.id, disc_name="Rebels S3 D1")
    session.add_all([movie, release, disc])
    session.commit()
    session.refresh(disc)
    return disc


class TestLinkedMovieTmdbId:
    def test_reads_through_release_to_movie(self, test_db):
        s = test_db()
        try:
            disc = _disc_linked_to(s, tmdb_id="60554")
            assert _linked_movie_tmdb_id(disc) == "60554"
        finally:
            s.close()

    def test_unlinked_disc_yields_empty_not_error(self, test_db):
        s = test_db()
        try:
            disc = models.Disc(id=str(uuid.uuid4()), content_hash=str(uuid.uuid4()))
            s.add(disc); s.commit(); s.refresh(disc)
            assert _linked_movie_tmdb_id(disc) == ""
        finally:
            s.close()


class TestLabelFormResolution:
    def test_falls_back_to_the_movie_link_when_disc_info_has_none(self, test_db):
        s = test_db()
        try:
            disc = _disc_linked_to(s, tmdb_id="60554")
            form = _build_labelform_from_disc(disc, {}, db=s)
            assert form["tmdb_id"] == "60554", "picker gets no catalog without this"
        finally:
            s.close()

    def test_disc_info_still_wins(self, test_db):
        """Lowest precedence: an explicit value must never be overridden."""
        s = test_db()
        try:
            disc = _disc_linked_to(s, tmdb_id="60554")
            form = _build_labelform_from_disc(disc, {"tmdb_id": "99999"}, db=s)
            assert form["tmdb_id"] == "99999"
        finally:
            s.close()

    def test_unlinked_disc_still_yields_empty_string(self, test_db):
        s = test_db()
        try:
            disc = models.Disc(id=str(uuid.uuid4()), content_hash=str(uuid.uuid4()))
            s.add(disc); s.commit(); s.refresh(disc)
            form = _build_labelform_from_disc(disc, {}, db=s)
            assert form["tmdb_id"] == ""
        finally:
            s.close()
