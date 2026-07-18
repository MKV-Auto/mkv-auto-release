"""Unit tests for coordinator disc metadata (api.routers.websockets._build_disc_metadata)."""

import datetime
import uuid

import pytest
from sqlalchemy.orm import joinedload

from api import database, models
from api.routers.websockets import _build_disc_metadata


@pytest.fixture
def test_db_session(test_db):
    session = test_db()
    try:
        yield session
    finally:
        session.close()


def test_build_disc_metadata_includes_disc_number(test_db_session):
    """_build_disc_metadata includes disc_number when disc has it (for carousel title)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    movie = models.Movie(
        id=str(uuid.uuid4()),
        name="Test Movie",
    )
    release = models.Release(
        id=str(uuid.uuid4()),
        slug="test-release",
        type="movie",
        name="Test Release",
        movie_id=movie.id,
    )
    disc = models.Disc(
        id=str(uuid.uuid4()),
        content_hash="hash-1",
        release_id=release.id,
        disc_number=2,
        info_title="MakeMKV Title",
        format="Blu-Ray",
        created_at=now,
        updated_at=now,
    )
    test_db_session.add_all([movie, release, disc])
    test_db_session.commit()

    disc_loaded = (
        test_db_session.query(models.Disc)
        .options(
            joinedload(models.Disc.release).joinedload(models.Release.movie),
        )
        .filter(models.Disc.id == disc.id)
        .first()
    )
    assert disc_loaded is not None

    meta = _build_disc_metadata(
        disc_loaded,
        disc_state="unfinished",
        job_id="job-123",
        created_at=now,
    )
    assert meta.disc_number == 2
    assert meta.discdb_disc_num is None
    assert meta.movie_name == "Test Movie"
    assert meta.release_name == "Test Release"
    assert meta.disc_id == str(disc.id)


def test_build_disc_metadata_omits_finalized_signal_when_not_finalized(test_db_session):
    """#603: finalized=False leaves all four finalized_* fields None so the
    carousel falls through to the regular "Now Reading" drive card."""
    now = datetime.datetime.now(datetime.timezone.utc)
    movie = models.Movie(id=str(uuid.uuid4()), name="Test Movie")
    release = models.Release(
        id=str(uuid.uuid4()),
        slug="test-release",
        type="movie",
        name="Test Release",
        movie_id=movie.id,
    )
    disc = models.Disc(
        id=str(uuid.uuid4()),
        content_hash="hash-not-finalized",
        release_id=release.id,
        disc_number=1,
        info_title="MakeMKV Title",
        format="Blu-Ray",
        created_at=now,
        updated_at=now,
        # explicit: finalized=False (the server_default)
    )
    test_db_session.add_all([movie, release, disc])
    test_db_session.commit()

    disc_loaded = (
        test_db_session.query(models.Disc)
        .options(joinedload(models.Disc.release).joinedload(models.Release.movie))
        .filter(models.Disc.id == disc.id)
        .first()
    )
    meta = _build_disc_metadata(disc_loaded, disc_state="in_drive", job_id=None, created_at=now)
    assert meta.finalized is None
    assert meta.finalized_release_id is None
    assert meta.finalized_release_name is None
    assert meta.finalized_release_slug is None


def test_build_disc_metadata_finalized_signal_from_completed_job_alone(test_db_session):
    """#603: the "already in Library" framing also fires when the disc has a
    completed job attached, even if `disc.finalized` is False — both signals
    mean "user has taken this disc through to completion at least once".
    Mirrors the real-world repro where finalized=False but has_completed_job=True."""
    now = datetime.datetime.now(datetime.timezone.utc)
    movie = models.Movie(id=str(uuid.uuid4()), name="Repro Movie")
    release = models.Release(
        id=str(uuid.uuid4()),
        slug="repro-release",
        type="movie",
        name="Repro Release",
        movie_id=movie.id,
    )
    disc = models.Disc(
        id=str(uuid.uuid4()),
        content_hash="hash-repro",
        release_id=release.id,
        disc_number=1,
        format="Blu-Ray",
        finalized=False,  # explicit: NOT formally finalized
        created_at=now,
        updated_at=now,
    )
    # A completed job exists for this disc — that's the broader signal.
    job = models.Job(
        id=str(uuid.uuid4()),
        disc_id=disc.id,
        disc_num="1",
        mount_point="/dev/sr0",
        job_status="completed",
        created_at=now,
        updated_at=now,
    )
    test_db_session.add_all([movie, release, disc, job])
    test_db_session.commit()

    disc_loaded = (
        test_db_session.query(models.Disc)
        .options(joinedload(models.Disc.release).joinedload(models.Release.movie))
        .filter(models.Disc.id == disc.id)
        .first()
    )
    meta = _build_disc_metadata(
        disc_loaded,
        disc_state="in_drive",
        job_id=None,
        created_at=now,
        db=test_db_session,
    )
    assert meta.has_completed_job is True
    assert meta.finalized is True
    assert meta.finalized_release_name == "Repro Movie"


def test_build_disc_metadata_surfaces_finalized_signal_with_release_name(test_db_session):
    """#603: when disc.finalized=True and the release joins are present, the
    carousel's drive card collapses to the "Already in Library" treatment.
    Prefer movie.name for finalized_release_name (matches Library card title)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    movie = models.Movie(id=str(uuid.uuid4()), name="The Goonies")
    release = models.Release(
        id=str(uuid.uuid4()),
        slug="the-goonies",
        type="movie",
        name="The Goonies (1985) — Director's Cut",
        movie_id=movie.id,
    )
    disc = models.Disc(
        id=str(uuid.uuid4()),
        content_hash="hash-finalized",
        release_id=release.id,
        disc_number=1,
        format="Blu-Ray",
        finalized=True,
        created_at=now,
        updated_at=now,
    )
    test_db_session.add_all([movie, release, disc])
    test_db_session.commit()

    disc_loaded = (
        test_db_session.query(models.Disc)
        .options(joinedload(models.Disc.release).joinedload(models.Release.movie))
        .filter(models.Disc.id == disc.id)
        .first()
    )
    meta = _build_disc_metadata(disc_loaded, disc_state="in_drive", job_id=None, created_at=now)
    assert meta.finalized is True
    assert meta.finalized_release_id == release.id
    assert meta.finalized_release_slug == "the-goonies"
    # Prefer movie.name over release.name so the carousel shows the same title
    # the Library card uses.
    assert meta.finalized_release_name == "The Goonies"


