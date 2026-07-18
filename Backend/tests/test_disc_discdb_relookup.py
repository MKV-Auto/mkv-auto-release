"""Tests for `POST /discs/{disc_id}/relookup-discdb`.

The endpoint re-runs the DiscDB lookup against an existing disc's content
hash without a full disc re-scan. This lets the user recover from a stale
DiscDB miss (e.g. one taken while the devmode "DiscDB Miss" simulation was
active) without deleting and re-inserting the disc. Internal `is_dev_mode()`
gate — production returns 403.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from api import database, models
from api.main import app


@pytest.fixture
def client(test_db):
    def override_get_db():
        session = test_db()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[database.get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed_disc_with_title(session, *, content_hash="RELOOKHASH"):
    disc_id = str(uuid.uuid4())
    title_id = str(uuid.uuid4())
    disc = models.Disc(id=disc_id, content_hash=content_hash, disc_info={"raw_info_log": ""})
    title = models.DiscTitle(
        id=title_id, disc_id=disc_id, index=109, source_file="00539.mpls",
        title="", type=None,
    )
    session.add(disc)
    session.add(title)
    session.commit()
    return disc_id, title_id


class TestRelookupDiscdbEndpoint:
    def test_403_when_not_in_dev_mode(self, client, test_db, monkeypatch):
        # `is_dev_mode` is imported lazily inside the handler, so patch the
        # module-level reference in core.utils where the endpoint resolves it.
        monkeypatch.setattr("core.utils.is_dev_mode", lambda: False)
        session = test_db()
        try:
            disc_id, _ = _seed_disc_with_title(session)
        finally:
            session.close()

        resp = client.post(f"/discs/{disc_id}/relookup-discdb")
        assert resp.status_code == 403

    def test_404_when_disc_missing(self, client, monkeypatch):
        monkeypatch.setattr("core.utils.is_dev_mode", lambda: True)
        resp = client.post(f"/discs/{uuid.uuid4()}/relookup-discdb")
        assert resp.status_code == 404

    def test_returns_miss_when_lookup_raises(self, client, test_db, monkeypatch):
        # Endpoint catches any lookup exception and reports miss.
        monkeypatch.setattr("core.utils.is_dev_mode", lambda: True)
        monkeypatch.setattr(
            "core.settings.get_discdb_disabled", lambda: False,
        )
        def _raises(_content_hash):
            raise Exception("DiscDB unreachable")
        monkeypatch.setattr("core.utils.retrieve_discdb_data", _raises)
        session = test_db()
        try:
            disc_id, title_id = _seed_disc_with_title(session)
        finally:
            session.close()

        resp = client.post(f"/discs/{disc_id}/relookup-discdb")
        assert resp.status_code == 200
        body = resp.json()
        assert body["result"] == "miss"
        assert body["disc_id"] == disc_id

        # Title row untouched on miss.
        session = test_db()
        try:
            title = session.query(models.DiscTitle).filter_by(id=title_id).first()
            assert title.title == ""
            assert title.type is None
        finally:
            session.close()

    def test_hit_overlays_title_metadata_and_flips_job_result(self, client, test_db, monkeypatch):
        monkeypatch.setattr("core.utils.is_dev_mode", lambda: True)
        # workflow_mode_discdb_hit=True so the devmode simulated-miss
        # short-circuit inside the handler stays out of the way.
        monkeypatch.setattr(
            "core.settings.get_discdb_disabled", lambda: False,
        )

        # Fake `retrieve_discdb_data` returns an opaque payload — its
        # actual shape doesn't matter because we also stub the parser to
        # return a fixed tuple.
        monkeypatch.setattr(
            "core.utils.retrieve_discdb_data", lambda _h: {"opaque": True},
        )
        # parse_discdb_data unpacks to a 19-tuple per the source order in
        # disc_manager.query_discdb. Only fields the handler reads need
        # plausible values.
        db_mapping = {
            "00539.mpls": {
                "type": "movie",
                "title": "Midway",
                "season": None,
                "episode": None,
                "description": "WWII epic",
            },
        }
        parsed_tuple = (
            "Midway",  # movie_name
            None,      # release_image
            "midway-2020",  # disc_slug
            db_mapping,
            "2160p",   # resolution
            "Blu-Ray", # disc_format
            "movie",   # title_type
            "midway-2020",  # disc_group
            2020,      # release_year
            None,      # release_date
            None,      # original_year
            None,      # original_release_date
            None,      # release_discs
            12345,     # tmdb_id
            "2160p",   # release_resolution
            "movie",   # tmdb_type
            2020,      # production_year
            1,         # matched_disc_index
            None,      # discdb_boxset
        )
        monkeypatch.setattr(
            "core.utils.parse_discdb_data", lambda _raw, _hash: parsed_tuple,
        )

        session = test_db()
        try:
            disc_id, title_id = _seed_disc_with_title(session)
            # A job with discdb_result='miss' that should be flipped.
            job_id = str(uuid.uuid4())
            job = models.Job(
                id=job_id, disc_id=disc_id, mode="copy", mount_point="/dev/sr0",
                disc_num="1", discdb_result="miss",
            )
            session.add(job)
            session.commit()
        finally:
            session.close()

        resp = client.post(f"/discs/{disc_id}/relookup-discdb")
        assert resp.status_code == 200
        body = resp.json()
        assert body["result"] == "hit"

        session = test_db()
        try:
            title = session.query(models.DiscTitle).filter_by(id=title_id).first()
            # _apply_discdb_metadata_to_titles overlayed track metadata
            # onto the existing row (matched by source_file).
            assert title.title == "Midway"
            # The default-when-blank rule sets type from the track entry,
            # normalized to the canonical form ("movie" → "mainmovie").
            assert (title.type or "").lower() == "mainmovie"

            job = session.query(models.Job).filter_by(id=job_id).first()
            assert job.discdb_result == "hit"

            disc = session.query(models.Disc).filter_by(id=disc_id).first()
            assert disc.disc_info.get("discdb_hit") is True
            assert disc.disc_info.get("movie_name") == "Midway"
        finally:
            session.close()
