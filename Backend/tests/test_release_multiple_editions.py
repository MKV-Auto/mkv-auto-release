"""A show may hold several standalone releases — one per season, say.

`uq_releases_movie_standalone` (movie_id WHERE boxset_id IS NULL) allowed
exactly ONE. A series is a single ``movies`` row, so its seasons are separate
releases of the same movie and the second could never be inserted. The failed
INSERT fell into the IntegrityError recovery, which re-selected the existing
release, wrote the new edition's UPC/ASIN/year over it, and returned it as
though it had been created. That is how a user's Season Three ended up holding
the Season Two values they had just typed (mkv-auto#821).

These tests REQUIRE PostgreSQL. The indexes are declared with
``postgresql_where``, so SQLAlchemy skips them on the SQLite ``test_db``
fixture — which is exactly why the original bug shipped with a green suite.
"""
import os
import pathlib
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from api import crud, models


BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]


def _alembic_cfg():
    from alembic.config import Config
    return Config(str(BACKEND_DIR / "alembic.ini"))


@pytest.fixture
def session(monkeypatch):
    """A throwaway database built by the MIGRATIONS, not by create_all.

    This matters. Building the schema from the model would mean the test's
    "before" state is whatever the model happens to declare — and the model
    declared no unique index at all, so a regression test written that way
    passes just as happily on the broken code. Running alembic gives the
    schema production actually has.

    The shared `test_db` fixture is SQLite, so gating on its dialect can never
    pass: `test_release_duplicate_prevention.py` gates that way and its
    constraint tests have consequently never run, in CI or locally. CI sets
    DATABASE_URL to its postgres:17 service, which is what this uses.
    """
    base_url = os.getenv("DATABASE_URL") or ""
    if not base_url.startswith("postgresql"):
        pytest.skip("needs PostgreSQL: partial unique indexes do not exist on SQLite")

    base = make_url(base_url)
    admin_url = base.set(database="postgres")
    dbname = f"editions_{uuid.uuid4().hex[:12]}"
    try:
        admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{dbname}"'))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no PostgreSQL server reachable: {exc}")

    temp_url = base.set(database=dbname).render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", temp_url)

    from alembic import command
    command.upgrade(_alembic_cfg(), "head")

    engine = create_engine(temp_url, future=True)
    s = sessionmaker(bind=engine, future=True)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()
        with create_engine(admin_url, isolation_level="AUTOCOMMIT").connect() as c:
            c.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :d AND pid <> pg_backend_pid()"
            ), {"d": dbname})
            c.execute(text(f'DROP DATABASE IF EXISTS "{dbname}"'))
        admin.dispose()


def _movie(session, name="Star Wars Rebels"):
    m = models.Movie(id=str(uuid.uuid4()), name=name, tmdb_id=str(uuid.uuid4())[:8])
    session.add(m)
    session.commit()
    return m


def _payload(name, year, upc, **over):
    p = {
        "release_name": name,
        "release_year": year,
        "upc": upc,
        "cover_front_url": "https://example.invalid/front.jpg",
    }
    p.update(over)
    return p


# The reporter's actual data.
S3 = ("Star Wars Rebels: Complete Season Three", 2017, "786936854626", "B074V7RBJY")
S2 = ("Star Wars Rebels: Complete Season Two", 2016, "786936850840", "B01GDJZJZ2")


class TestSeveralEditionsPerShow:
    def test_two_seasons_of_one_show_coexist(self, session):
        movie = _movie(session)
        three = crud.get_or_create_release(
            session, _payload(S3[0], S3[1], S3[2], asin=S3[3], movie_id=movie.id))
        two = crud.get_or_create_release(
            session, _payload(S2[0], S2[1], S2[2], asin=S2[3], movie_id=movie.id))

        assert three is not None and two is not None
        assert three.id != two.id, "the second season resolved to the first"
        rows = session.query(models.Release).filter(models.Release.movie_id == movie.id).all()
        assert len(rows) == 2

    def test_creating_season_two_leaves_season_three_alone(self, session):
        """The regression this whole change exists for."""
        movie = _movie(session)
        three = crud.get_or_create_release(
            session, _payload(S3[0], S3[1], S3[2], asin=S3[3], movie_id=movie.id))
        three_id = three.id

        crud.get_or_create_release(
            session, _payload(S2[0], S2[1], S2[2], asin=S2[3], movie_id=movie.id))

        session.expire_all()
        kept = session.query(models.Release).filter(models.Release.id == three_id).one()
        assert kept.name == S3[0]
        assert kept.release_year == S3[1], "Season Two's year overwrote Season Three's"
        assert kept.upc == S3[2], "Season Two's UPC overwrote Season Three's"
        assert kept.asin == S3[3], "Season Two's ASIN overwrote Season Three's"

    def test_conflicting_create_does_not_clobber_the_existing_row(self, session):
        """Exercise the IntegrityError recovery directly.

        `test_creating_season_two_leaves_season_three_alone` above no longer
        reaches this path — with the widened index the two seasons simply both
        insert. So force a genuine collision (same movie, name AND upc) and
        assert the recovery fills blanks instead of writing the payload over
        the existing row, which is what corrupted Season Three.
        """
        movie = _movie(session)
        first = crud.get_or_create_release(
            session, _payload(S3[0], S3[1], S3[2], asin=S3[3], movie_id=movie.id,
                              cover_back_url="https://example.invalid/original-back.jpg"))
        first_id = first.id

        # Same name + upc => collides. Different asin/year/cover in the payload.
        again = crud.get_or_create_release(
            session, _payload(S3[0], 1999, S3[2], asin="B0DIFFERENT", movie_id=movie.id,
                              cover_back_url="https://example.invalid/replacement-back.jpg"))

        assert again is not None and again.id == first_id, "should resolve to the conflicting row"
        session.expire_all()
        kept = session.query(models.Release).filter(models.Release.id == first_id).one()
        assert kept.asin == S3[3], "recovery overwrote a populated ASIN"
        assert kept.release_year == S3[1], "recovery overwrote a populated year"
        assert kept.cover_back_url == "https://example.invalid/original-back.jpg", \
            "recovery overwrote a populated cover"

    def test_conflicting_create_still_fills_genuinely_blank_fields(self, session):
        """Fill-blanks must still fill blanks, or resolving loses metadata."""
        movie = _movie(session)
        first = crud.get_or_create_release(
            session, _payload(S3[0], S3[1], S3[2], movie_id=movie.id))
        first_id = first.id
        assert not first.asin

        crud.get_or_create_release(
            session, _payload(S3[0], S3[1], S3[2], asin=S3[3], movie_id=movie.id))

        session.expire_all()
        kept = session.query(models.Release).filter(models.Release.id == first_id).one()
        assert kept.asin == S3[3], "a blank field should still be filled from the payload"

    def test_identical_name_and_upc_still_collides(self, session):
        """Race protection from 202602080000 must survive the widening."""
        movie = _movie(session)
        session.add(models.Release(
            id=str(uuid.uuid4()), slug="dup-a", movie_id=movie.id,
            name=S2[0], upc=S2[2], type="series"))
        session.commit()

        session.add(models.Release(
            id=str(uuid.uuid4()), slug="dup-b", movie_id=movie.id,
            name=S2[0], upc=S2[2], type="series"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    def test_blank_names_still_collide(self, session):
        """COALESCE, not raw NULLs — Postgres treats NULLs as distinct, which
        would let unlimited unnamed rows through and lose the protection."""
        movie = _movie(session)
        session.add(models.Release(
            id=str(uuid.uuid4()), slug="anon-a", movie_id=movie.id, name=None, upc=None))
        session.commit()

        session.add(models.Release(
            id=str(uuid.uuid4()), slug="anon-b", movie_id=movie.id, name=None, upc=None))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


class TestForceNew:
    def test_force_new_ignores_the_discs_current_release(self, session):
        movie = _movie(session)
        first = crud.get_or_create_release(
            session, _payload(S3[0], S3[1], S3[2], movie_id=movie.id))
        disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:10]}",
                           release_id=first.id)
        session.add(disc)
        session.commit()

        second = crud.get_or_create_release(
            session, _payload(S2[0], S2[1], S2[2], movie_id=movie.id),
            disc.content_hash, force_new=True)

        assert second is not None
        assert second.id != first.id, "force_new still resolved to the disc's release"

    def test_default_still_attaches(self, session):
        """Every existing caller — the boxset flow especially — must not start
        creating duplicates."""
        movie = _movie(session)
        first = crud.get_or_create_release(
            session, _payload(S3[0], S3[1], S3[2], movie_id=movie.id))
        disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:10]}",
                           release_id=first.id)
        session.add(disc)
        session.commit()

        again = crud.get_or_create_release(
            session, _payload(S3[0], S3[1], S3[2], movie_id=movie.id), disc.content_hash)

        assert again is not None and again.id == first.id


class TestSelectingTheRightSeason:
    """Selecting Season Two must not snap the disc back to Season One.

    `_patch_disc_ops_internal` initialises its working `release` from
    `disc.release`, which is None on a fresh disc. A disc op sets
    `disc.release_id` but only the UNLINK branch ever updated that variable, so
    the movie-only fallback further down still ran and reassigned the disc to
    whichever standalone release came first for the movie.

    That was invisible while `uq_releases_movie_standalone` guaranteed one row
    per movie — `.first()` could only return the right one. Widening the key in
    202608220000 turned it into a live bug, reported independently as
    mkv-auto-release#9.
    """

    def _two_seasons(self, session):
        movie = _movie(session)
        three = crud.get_or_create_release(
            session, _payload(S3[0], S3[1], S3[2], asin=S3[3], movie_id=movie.id))
        two = crud.get_or_create_release(
            session, _payload(S2[0], S2[1], S2[2], asin=S2[3], movie_id=movie.id))
        assert three.id != two.id
        return movie, three, two

    def test_a_fresh_disc_stays_on_the_season_that_was_selected(self, session):
        from api.routers.releases import _patch_disc_ops_internal

        movie, three, two = self._two_seasons(session)
        disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:10]}")
        session.add(disc)
        session.commit()
        assert disc.release_id is None, "must start unlinked — that is the reported case"

        _patch_disc_ops_internal(
            str(disc.id),
            [
                # What the UI actually sends: release metadata for the season
                # chosen, plus the disc link. The release op is what populates
                # release_data_from_ops and arms the movie-only fallback.
                {"target": "release", "fields": {
                    "movie_id": movie.id, "release_name": S2[0], "release_year": S2[1]}},
                {"target": "disc", "fields": {"release_id": two.id}},
            ],
            session,
        )
        session.commit()

        session.refresh(disc)
        assert disc.release_id == two.id, "the disc was reassigned to another season"

    def test_moving_a_linked_disc_between_seasons_sticks(self, session):
        from api.routers.releases import _patch_disc_ops_internal

        movie, three, two = self._two_seasons(session)
        disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:10]}",
                           release_id=three.id)
        session.add(disc)
        session.commit()

        _patch_disc_ops_internal(
            str(disc.id),
            [
                {"target": "release", "fields": {
                    "movie_id": movie.id, "release_name": S2[0], "release_year": S2[1]}},
                {"target": "disc", "fields": {"release_id": two.id}},
            ],
            session,
        )
        session.commit()

        session.refresh(disc)
        assert disc.release_id == two.id

    def test_an_unknown_release_id_is_reported_not_guessed(self, session):
        from fastapi import HTTPException
        from api.routers.releases import _patch_disc_ops_internal

        movie, three, two = self._two_seasons(session)
        disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:10]}")
        session.add(disc)
        session.commit()

        with pytest.raises(HTTPException) as exc:
            _patch_disc_ops_internal(
                str(disc.id),
                [{"target": "disc", "fields": {"release_id": "does-not-exist"}}],
                session,
            )
        assert exc.value.status_code == 400
        session.rollback()


class TestFindStandaloneRelease:
    def test_declines_when_several_could_match(self, session):
        movie, *_ = None, None
        movie = _movie(session)
        crud.get_or_create_release(session, _payload(S3[0], S3[1], S3[2], movie_id=movie.id))
        crud.get_or_create_release(session, _payload(S2[0], S2[1], S2[2], movie_id=movie.id))

        assert crud.find_standalone_release(session, movie.id) is None, \
            "guessing here is what reassigned a disc to the wrong season"

    def test_narrows_by_upc(self, session):
        movie = _movie(session)
        crud.get_or_create_release(session, _payload(S3[0], S3[1], S3[2], movie_id=movie.id))
        two = crud.get_or_create_release(session, _payload(S2[0], S2[1], S2[2], movie_id=movie.id))

        found = crud.find_standalone_release(session, movie.id, upc=S2[2])
        assert found is not None and found.id == two.id

    def test_returns_the_only_one_when_unambiguous(self, session):
        movie = _movie(session)
        only = crud.get_or_create_release(session, _payload(S3[0], S3[1], S3[2], movie_id=movie.id))

        found = crud.find_standalone_release(session, movie.id)
        assert found is not None and found.id == only.id
