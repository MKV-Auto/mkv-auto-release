"""Disc-listing queries against real PostgreSQL (#741).

The rest of the suite runs on SQLite, which permits `SELECT DISTINCT` over any
column type. PostgreSQL does not: `discs` carries `json` columns
(`label_payload`, `disc_info`, …) and `json` has no equality operator, so a
`DISTINCT` over the whole entity fails at runtime with

    could not identify an equality operator for type json

Both the contributions listing and the bulk-export eligibility query were
written that way and were **completely broken on PostgreSQL** — a 500 and an
always-empty export — while every SQLite test passed. Found only by running a
release candidate against a restored production database.

These tests exist so the same class of bug fails in CI, which has a real
PostgreSQL service, rather than in a user's container.
"""
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from api import models
from tests.test_migration_data_safety import _base_url


@pytest.fixture()
def pg_session():
    """A real PostgreSQL session on a throwaway database, or skip."""
    base = make_url(_base_url())
    if not base.get_backend_name().startswith("postgresql"):
        pytest.skip("requires PostgreSQL")
    admin_url = base.set(database="postgres")
    dbname = f"discdb_q_{uuid.uuid4().hex[:12]}"
    try:
        admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        conn = admin.connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no PostgreSQL server reachable: {exc}")
    try:
        conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    finally:
        conn.close()

    url = base.set(database=dbname).render_as_string(hide_password=False)
    engine = create_engine(url, future=True)
    models.Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        conn = admin.connect()
        try:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
        finally:
            conn.close()
            admin.dispose()


def _disc_with_two_jobs(session, *, json_populated=True):
    """A disc with more than one job — the case that motivated DISTINCT.

    Its json columns are populated on purpose: an empty json column would not
    exercise the equality-operator failure.
    """
    movie = models.Movie(name="Cinderella Man", production_year=2005)
    session.add(movie)
    session.flush()
    release = models.Release(slug="2025-4k", type="movie", name="4K",
                             movie_id=movie.id, release_year=2025)
    session.add(release)
    session.flush()
    disc = models.Disc(
        content_hash=f"h-{uuid.uuid4().hex[:8]}",
        release_id=release.id,
        label_payload={"titles": [{"a": 1}]} if json_populated else None,
        disc_info={"raw_info_log": "MSG:1005"} if json_populated else None,
    )
    session.add(disc)
    session.flush()
    for _ in range(2):
        # disc_num and mount_point are NOT NULL — PostgreSQL enforces that where
        # SQLite lets the other suites get away with omitting them.
        session.add(models.Job(disc_id=disc.id, disc_num="0", mount_point="/dev/sr0",
                               job_status="completed", rip_state="completed"))
    session.commit()
    return disc


def test_bulk_export_eligibility_runs_on_postgres(pg_session):
    """Was: "could not identify an equality operator for type json"."""
    from core.discdb_export import _eligible_contribution_discs

    disc = _disc_with_two_jobs(pg_session)
    found = _eligible_contribution_discs(pg_session)

    assert [d.id for d in found] == [disc.id]


def test_eligibility_returns_a_disc_once_despite_multiple_jobs(pg_session):
    """The reason DISTINCT was there. EXISTS has to preserve that."""
    from core.discdb_export import _eligible_contribution_discs

    _disc_with_two_jobs(pg_session)
    assert len(_eligible_contribution_discs(pg_session)) == 1


def test_a_disc_already_in_thediscdb_is_excluded(pg_session):
    from core.discdb_export import _eligible_contribution_discs

    disc = _disc_with_two_jobs(pg_session)
    disc.discdb_disc_num = 1
    pg_session.commit()

    assert _eligible_contribution_discs(pg_session) == []


def test_an_unlinked_disc_is_excluded(pg_session):
    from core.discdb_export import _eligible_contribution_discs

    disc = _disc_with_two_jobs(pg_session)
    disc.release_id = None
    pg_session.commit()

    assert _eligible_contribution_discs(pg_session) == []


def test_contributions_listing_runs_on_postgres(pg_session):
    """Was a 500 on PostgreSQL, and had been shipping that way."""
    from api.routers.discdb import list_contributions

    disc = _disc_with_two_jobs(pg_session)
    rows = list_contributions(status=None, db=pg_session)

    assert [r["disc_id"] for r in rows] == [str(disc.id)]
