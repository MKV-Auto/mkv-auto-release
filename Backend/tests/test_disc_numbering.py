import datetime
import uuid

import pytest
from fastapi.testclient import TestClient

from api import database, models
from api.schemas import DiscMetadataUpdate, ReleaseMetadataPatch
from api.main import app


@pytest.fixture
def client(test_db):
    def override_get_db():
        try:
            session = test_db()
            yield session
        finally:
            session.close()

    from api.routers import releases

    app.dependency_overrides[database.get_db] = override_get_db
    if hasattr(releases, "get_db"):
        app.dependency_overrides[releases.get_db] = override_get_db

    yield TestClient(app)
    app.dependency_overrides.clear()


def test_disc_number_release_only_on_label_save(client, test_db):
    session = test_db()
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        movie = models.Movie(
            id=str(uuid.uuid4()),
            name="Movie 1",
        )
        release = models.Release(
            id=str(uuid.uuid4()),
            slug="release-1",
            type="movie",
            name="Release 1",
            movie_id=movie.id,
        )
        disc1 = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-1",
            release_id=release.id,
            disc_number=1,
            created_at=now - datetime.timedelta(seconds=5),
        )
        disc2 = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-2",
            release_id=release.id,
            disc_number=None,
            created_at=now,
        )
        session.add_all([movie, release, disc1, disc2])
        session.commit()

        payload = {
            "mode": "movie",
            "disc_format": "Blu-Ray",
            "titles": [],
        }
        response = client.post(f"/releases/disc/{disc2.id}/label", json=payload)
        assert response.status_code == 200

        session.refresh(disc2)
        assert disc2.disc_number == 2
    finally:
        session.close()


def test_disc_number_boxset_wide_on_label_save(client, test_db):
    session = test_db()
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        boxset = models.Boxset(
            id=str(uuid.uuid4()),
            slug="boxset-1",
            name="Boxset 1",
        )
        movie = models.Movie(
            id=str(uuid.uuid4()),
            name="Movie 1",
        )
        release1 = models.Release(
            id=str(uuid.uuid4()),
            slug="release-1",
            type="movie",
            name="Release 1",
            boxset_id=boxset.id,
            movie_id=movie.id,
        )
        release2 = models.Release(
            id=str(uuid.uuid4()),
            slug="release-2",
            type="movie",
            name="Release 2",
            boxset_id=boxset.id,
            movie_id=movie.id,
        )
        disc1 = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-1",
            release_id=release1.id,
            disc_number=1,
            created_at=now - datetime.timedelta(seconds=5),
        )
        disc2 = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-2",
            release_id=release2.id,
            disc_number=None,
            created_at=now,
        )
        session.add_all([boxset, movie, release1, release2, disc1, disc2])
        session.commit()

        payload = {
            "mode": "movie",
            "disc_format": "Blu-Ray",
            "titles": [],
        }
        response = client.post(f"/releases/disc/{disc2.id}/label", json=payload)
        assert response.status_code == 200

        session.refresh(disc2)
        assert disc2.disc_number == 2
    finally:
        session.close()


@pytest.mark.xfail(reason="staging baseline fail; tracked in #397", strict=True)
def test_disc_number_normalizes_created_at_on_release_change(client, test_db):
    session = test_db()
    try:
        now = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
        movie = models.Movie(
            id=str(uuid.uuid4()),
            name="Movie 1",
        )
        release = models.Release(
            id=str(uuid.uuid4()),
            slug="release-1",
            type="movie",
            name="Release 1",
            movie_id=movie.id,
        )
        disc_early = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-early",
            release_id=release.id,
            disc_number=2,
            created_at=now,
        )
        disc_late = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-late",
            release_id=release.id,
            disc_number=1,
            created_at=now + datetime.timedelta(seconds=2),
        )
        disc_current = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="hash-current",
            release_id=None,
            disc_number=None,
            created_at=now + datetime.timedelta(seconds=1),
        )
        session.add_all([movie, release, disc_early, disc_late, disc_current])
        session.commit()

        from api.routers import releases
        payload = DiscMetadataUpdate(
            release=ReleaseMetadataPatch(
                release_id=release.id,
                release_slug=release.slug,
                release_name=release.name,
            )
        )
        response = releases.update_disc_metadata(disc_current.id, payload, db=session)
        assert response["disc"].release_id == release.id

        session.refresh(disc_early)
        session.refresh(disc_late)
        session.refresh(disc_current)
        assert disc_early.disc_number == 1
        assert disc_current.disc_number == 2
        assert disc_late.disc_number == 3
    finally:
        session.close()
