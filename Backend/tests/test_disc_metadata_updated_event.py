"""#832 — a label save tells the card carousel, without going through
``disc_updated`` (whose client handler merges disc_state and dedupes
unfinished cards — wrong for a label save on an ejected job)."""
from __future__ import annotations

import uuid
from unittest.mock import patch

from api import models
from api.routers.websockets import (
    DISC_METADATA_UPDATED_FIELDS,
    _build_disc_metadata_updated_payload,
)


def _series_disc(session):
    movie = models.Movie(id=str(uuid.uuid4()), name="Star Wars Rebels",
                         tmdb_type="tv", production_year=2014)
    release = models.Release(id=str(uuid.uuid4()), movie_id=movie.id, name="Season Two", release_year=2016,
                             slug=f"season-two-{uuid.uuid4().hex[:6]}", upc="012345678905",
                             cover_front_url="https://example.invalid/front.jpg")
    disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:8]}", release_id=release.id,
                       disc_number=2, format="DVD",
                       disc_name="Star Wars Rebels: Season 2 - Disc 2 - DVD",
                       disc_slug="star_wars_rebels-_season_2_-_disc_2_-_dvd")
    session.add_all([movie, release, disc])
    session.commit()
    return disc.id


def test_payload_carries_identity_fields_and_nothing_stateful(test_db):
    session = test_db()
    try:
        disc_id = _series_disc(session)
    finally:
        session.close()
    payload = _build_disc_metadata_updated_payload(disc_id, "job-1")
    assert payload is not None
    assert payload["disc_id"] == disc_id and payload["job_id"] == "job-1"
    assert payload["movie_name"] == "Star Wars Rebels"
    assert payload["release_name"] == "Season Two"
    assert payload["disc_number"] == 2
    assert payload["disc_format"] == "DVD"
    # #845: label saves auto-rename the disc; the event must carry the new
    # name/slug or the client shows the stale one until a hard refresh.
    assert payload["disc_name"] == "Star Wars Rebels: Season 2 - Disc 2 - DVD"
    assert payload["disc_slug"] == "star_wars_rebels-_season_2_-_disc_2_-_dvd"
    for stateful in ("disc_state", "scan_state", "mount_point", "disc_num"):
        assert stateful not in payload
    assert set(payload) == {"disc_id", "job_id", *DISC_METADATA_UPDATED_FIELDS}


def test_unknown_disc_yields_none(test_db):
    assert _build_disc_metadata_updated_payload(str(uuid.uuid4()), None) is None


def test_disc_ops_patch_schedules_the_event_for_disc_level_ops(test_db):
    """Disc/release-level ops schedule it; title-only ops do not."""
    from api.routers.releases import _patch_disc_ops_internal
    session = test_db()
    try:
        disc_id = _series_disc(session)
        with patch("api.routers.websockets.schedule_disc_metadata_updated") as sched:
            _patch_disc_ops_internal(disc_id, [{"target": "disc", "fields": {"disc_number": 3}}], session)
            assert sched.call_count == 1
            assert sched.call_args.args[0] == disc_id
        with patch("api.routers.websockets.schedule_disc_metadata_updated") as sched:
            _patch_disc_ops_internal(disc_id, [], session)
            assert sched.call_count == 0
    finally:
        session.close()
