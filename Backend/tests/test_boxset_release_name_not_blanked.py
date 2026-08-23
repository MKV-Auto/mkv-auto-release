"""A stale label form must not blank a boxset-member release's name.

Creating a boxset during labelling does everything server-side in one call:
boxset row, a release named after it, disc linked, label_draft synced. The
client discarded that response and waited for a WebSocket patch that, for a
job context, deliberately carries none of those fields — so its form still
said release_name="". The user's next action (assigning the boxset by hand)
autosaved that form; the jobs PATCH merges client-over-server, and
_apply_label_to_records wrote rel.name = "" over the name the create had set.
The boxset step's Continue requires a non-blank name, so it went dead and
stayed dead through every refresh.

Blank-as-edition-name is documented and intentional for STANDALONE releases.
A boxset member's name comes from the boxset, so a blank there is only ever a
stale form.
"""
import uuid

import pytest

from api import models
from api.routers.jobs import _apply_label_to_records


@pytest.fixture
def session(test_db):
    s = test_db()
    try:
        yield s
    finally:
        s.close()


def _movie(session):
    m = models.Movie(id=str(uuid.uuid4()), name="The Matrix", tmdb_id=str(uuid.uuid4())[:8])
    session.add(m)
    session.flush()
    return m


def _boxset_member(session, movie, name="The Matrix 4-Film Déjà vu Collection"):
    bx = models.Boxset(id=str(uuid.uuid4()), name=name, slug="the-matrix-4-film-deja-vu-collection", year=2022)
    session.add(bx)
    session.flush()
    rel = models.Release(
        id=str(uuid.uuid4()), slug=bx.slug, movie_id=movie.id, boxset_id=bx.id,
        name=name, release_year=2018, type="movie",
    )
    session.add(rel)
    session.flush()
    disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:10]}", release_id=rel.id)
    session.add(disc)
    session.commit()
    session.refresh(disc)
    return disc, rel, bx


def _standalone(session, movie, name="Collector's Edition"):
    rel = models.Release(id=str(uuid.uuid4()), slug="the-matrix-2021", movie_id=movie.id,
                         boxset_id=None, name=name, release_year=2021, type="movie")
    session.add(rel)
    session.flush()
    disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:10]}", release_id=rel.id)
    session.add(disc)
    session.commit()
    session.refresh(disc)
    return disc, rel


class TestBoxsetMemberNameSurvivesStaleForm:
    def test_empty_string_does_not_blank_it(self, session):
        """The reported case exactly: release_name="" from a stale client form."""
        disc, rel, bx = _boxset_member(session, _movie(session))

        _apply_label_to_records(disc, {"release_name": "", "release_id": rel.id, "boxset_id": bx.id}, session)
        session.commit()

        session.refresh(rel)
        assert rel.name == bx.name, "a blank from a stale form overwrote the boxset name"

    def test_none_does_not_blank_it(self, session):
        disc, rel, bx = _boxset_member(session, _movie(session))

        _apply_label_to_records(disc, {"release_name": None, "release_id": rel.id}, session)
        session.commit()

        session.refresh(rel)
        assert rel.name == bx.name

    def test_a_real_rename_still_applies(self, session):
        """The guard must only catch blanks — renaming a member is still allowed."""
        disc, rel, bx = _boxset_member(session, _movie(session))

        _apply_label_to_records(disc, {"release_name": "Renamed Collection", "release_id": rel.id}, session)
        session.commit()

        session.refresh(rel)
        assert rel.name == "Renamed Collection"

    def test_standalone_blank_edition_name_is_still_honoured(self, session):
        """Blank-as-edition-name is intentional for standalone releases; do not
        widen the guard into behaviour the user relies on elsewhere."""
        disc, rel = _standalone(session, _movie(session))

        _apply_label_to_records(disc, {"release_name": "", "release_id": rel.id}, session)
        session.commit()

        session.refresh(rel)
        assert not (rel.name or "").strip(), "standalone blank must still clear the edition name"
