"""Tests for api.crud disc creation (get_or_create_disc), DiscDB auto-create (ensure_release_from_discdb),
and DB-backed DiscDB lookup cache (get_discdb_data_from_db)."""
import uuid

import pytest

from api import crud, models


def _fake_fetch_tmdb_default(tmdb_id, tmdb_type=None, **kwargs):
    """Deterministic TMDB scrape stand-in for DiscDB ingest tests (avoids network)."""
    return {
        "name": f"Scraped {tmdb_id}",
        "production_year": 2024,
        "cover_url": f"https://tmdb.fake/{tmdb_id}.jpg",
        "tmdb_type": "movie",
        "tmdb_id": str(tmdb_id),
    }


def test_ensure_release_from_discdb_creates_movie_and_release(test_db, monkeypatch):
    """DiscDB ingest: Movie name/year/poster from TMDB scrape; release cover stays DiscDB scan art."""
    monkeypatch.setattr(crud, "fetch_tmdb_metadata_for_id", lambda tid, ttype=None, **kw: {
        "name": "TMDB Canonical Title",
        "production_year": 1999,
        "cover_url": "https://image.tmdb.org/p/w500/example.jpg",
        "tmdb_type": "movie",
        "tmdb_id": str(tid),
    })
    session = test_db()
    try:
        payload = {
            "discdb_hit": True,
            "tmdb_id": "tmdb-discdb-1",
            "movie_name": "DiscDB Movie",
            "production_year": 2020,
            "release_year": 2020,
            "release_image": "https://thediscdb.com/images/cover123.jpg",
            "disc_hash": "hash-discdb-1",
            "group_type": "movie",
        }
        release = crud.ensure_release_from_discdb(session, payload)
        assert release is not None
        session.refresh(release)
        assert release.movie_id is not None
        assert release.cover_front_url == "https://thediscdb.com/images/cover123.jpg"
        movie = session.query(models.Movie).filter(models.Movie.id == release.movie_id).first()
        assert movie is not None
        assert movie.name == "TMDB Canonical Title"
        assert movie.production_year == 1999
        assert movie.tmdb_id == "tmdb-discdb-1"
        assert movie.cover_url == "https://image.tmdb.org/p/w500/example.jpg"
        release2 = crud.ensure_release_from_discdb(session, payload)
        assert release2 is not None
        assert release2.id == release.id
    finally:
        session.close()


def test_ensure_release_from_discdb_falls_back_when_scrape_fails(test_db, monkeypatch):
    """When TMDB scrape fails, Movie uses DiscDB title/year; poster backfills from release cover."""
    monkeypatch.setattr(crud, "fetch_tmdb_metadata_for_id", lambda *a, **k: None)
    session = test_db()
    try:
        payload = {
            "discdb_hit": True,
            "tmdb_id": "tmdb-fallback-1",
            "movie_name": "DiscDB Only Title",
            "production_year": 2018,
            "release_year": 2018,
            "release_image": "https://thediscdb.com/images/fallback.jpg",
            "disc_hash": "hash-fallback-1",
            "group_type": "movie",
        }
        release = crud.ensure_release_from_discdb(session, payload)
        assert release is not None
        movie = session.query(models.Movie).filter(models.Movie.id == release.movie_id).first()
        assert movie.name == "DiscDB Only Title"
        assert movie.production_year == 2018
        assert movie.cover_url == "https://thediscdb.com/images/fallback.jpg"
    finally:
        session.close()


def test_ensure_movie_from_discdb_scrape_with_empty_discdb_title(test_db, monkeypatch):
    """Regression: empty DiscDB title still creates Movie when scrape returns a name."""
    monkeypatch.setattr(crud, "fetch_tmdb_metadata_for_id", lambda tid, ttype=None, **kw: {
        "name": "Title From TMDB",
        "production_year": 2005,
        "cover_url": "https://tmdb.fake/poster.jpg",
        "tmdb_type": "movie",
        "tmdb_id": str(tid),
    })
    session = test_db()
    try:
        mid = crud._ensure_movie_from_discdb(
            session,
            {
                "tmdb_id": "tmdb-empty-discdb-title",
                "movie_name": "",
                "production_year": None,
                "group_type": "movie",
            },
        )
        assert mid
        m = session.query(models.Movie).filter(models.Movie.id == mid).first()
        assert m.name == "Title From TMDB"
        assert m.production_year == 2005
        assert m.cover_url == "https://tmdb.fake/poster.jpg"
    finally:
        session.close()


def test_ensure_release_from_discdb_returns_none_without_discdb_hit_or_tmdb_id(test_db):
    """ensure_release_from_discdb returns None when discdb_hit is false or tmdb_id missing."""
    session = test_db()
    try:
        assert crud.ensure_release_from_discdb(session, {"discdb_hit": False, "tmdb_id": "x"}) is None
        assert crud.ensure_release_from_discdb(session, {"discdb_hit": True}) is None
    finally:
        session.close()


def test_ensure_release_from_discdb_creates_boxset_and_sets_release_boxset_id(test_db, monkeypatch):
    """DiscDB payload with discdb_boxset upserts Boxset and ties Release.boxset_id (Phase B)."""
    monkeypatch.setattr(crud, "fetch_tmdb_metadata_for_id", _fake_fetch_tmdb_default)
    session = test_db()
    try:
        discdb_boxset = {
            "slug": "discdb-boxset-phase-b-1",
            "title": "Test Collection",
            "name": "Test Collection",
            "year": 2020,
            "upc": "012345678905",
            "cover_front_url": "https://example.com/boxset-cover.jpg",
        }
        payload = {
            "discdb_hit": True,
            "tmdb_id": "tmdb-phase-b-bs",
            "movie_name": "Movie In Box",
            "production_year": 2020,
            "release_year": 2020,
            "release_image": "https://example.com/release-cover.jpg",
            "disc_hash": "hash-phase-b-bs",
            "group_type": "movie",
            "discdb_boxset": discdb_boxset,
        }
        release = crud.ensure_release_from_discdb(session, payload)
        assert release is not None
        session.refresh(release)
        assert release.boxset_id is not None
        box = session.query(models.Boxset).filter(models.Boxset.id == release.boxset_id).first()
        assert box is not None
        assert box.slug == "discdb-boxset-phase-b-1"
        assert box.name == "Test Collection"
        release2 = crud.ensure_release_from_discdb(session, payload)
        assert release2 is not None
        assert release2.id == release.id
        assert release2.boxset_id == release.boxset_id
    finally:
        session.close()


def test_get_discdb_data_from_db_includes_discdb_boxset_for_boxset_release(test_db, monkeypatch):
    """DB DiscDB cache includes discdb_boxset so rescans keep ensure_release_from_discdb boxset path."""
    monkeypatch.setattr(crud, "fetch_tmdb_metadata_for_id", _fake_fetch_tmdb_default)
    session = test_db()
    try:
        discdb_boxset = {
            "slug": "discdb-boxset-cache-1",
            "title": "Cached Box",
            "name": "Cached Box",
            "year": 2021,
            "upc": "012345678905",
            "cover_front_url": "https://example.com/box.jpg",
        }
        payload = {
            "discdb_hit": True,
            "tmdb_id": "tmdb-boxset-cache",
            "movie_name": "Cached Movie",
            "production_year": 2021,
            "release_year": 2021,
            "release_image": "https://example.com/rel.jpg",
            "disc_hash": "boxset-cache-hash",
            "group_type": "movie",
            "upc": "012345678905",
            "discdb_boxset": discdb_boxset,
        }
        disc = crud.persist_disc_scan_with_discdb(session, "boxset-cache-hash", payload)
        session.commit()
        session.refresh(disc)
        assert disc.release_id is not None
        rel = session.query(models.Release).filter(models.Release.id == disc.release_id).first()
        assert rel and rel.boxset_id is not None

        cached = crud.get_discdb_data_from_db(session, "boxset-cache-hash")
        assert cached is not None
        assert cached.get("discdb_boxset")
        assert cached["discdb_boxset"]["slug"] == "discdb-boxset-cache-1"
    finally:
        session.close()


def test_get_or_create_disc_ignores_label_draft_in_payload_on_create(test_db):
    """
    When creating a new disc, get_or_create_disc must not persist label_draft from the payload.
    label_draft is for user draft only; scan/DiscDB payloads must not fill it.
    """
    session = test_db()
    try:
        content_hash = "test-hash-no-label-draft"
        payload = {
            "disc_hash": content_hash,
            "disc_format": "Blu-Ray",
            "release_slug": "harry-potter-8-film-collection",
            "release_name": "Harry Potter 8-Film Collection",
            "release_year": 2017,
            "group_type": "movie",
            "label_draft": {
                "movie_id": None,
                "group_type": "movie",
                "release_slug": "harry-potter-8-film-collection",
                "release_name": "Harry Potter 8-Film Collection",
                "release_year": 2017,
                "boxset_id": None,
            },
        }
        disc = crud.get_or_create_disc(session, content_hash, None, payload)
        session.refresh(disc)
        assert disc.content_hash == content_hash
        assert disc.label_draft is None
    finally:
        session.close()


# --- get_discdb_data_from_db tests (#77) ---


def test_get_discdb_data_from_db_returns_none_for_unknown_hash(test_db):
    """get_discdb_data_from_db returns None when no disc with the hash exists."""
    session = test_db()
    try:
        result = crud.get_discdb_data_from_db(session, "nonexistent-hash")
        assert result is None
    finally:
        session.close()


def test_get_discdb_data_from_db_returns_none_for_miss(test_db):
    """get_discdb_data_from_db returns None when disc exists but has no release (miss)."""
    session = test_db()
    try:
        disc = crud.get_or_create_disc(session, "miss-hash-001", None, {
            "disc_hash": "miss-hash-001",
        })
        session.commit()
        assert disc.release_id is None

        result = crud.get_discdb_data_from_db(session, "miss-hash-001")
        assert result is None
    finally:
        session.close()


def test_get_discdb_data_from_db_returns_hit_data(test_db, monkeypatch):
    """get_discdb_data_from_db returns DiscDB-like data when disc is linked to a link-ready release."""
    monkeypatch.setattr(crud, "fetch_tmdb_metadata_for_id", _fake_fetch_tmdb_default)
    session = test_db()
    try:
        payload = {
            "discdb_hit": True,
            "tmdb_id": "tmdb-cache-test",
            "movie_name": "Cache Test Movie",
            "production_year": 2024,
            "release_year": 2024,
            "release_image": "https://example.com/cover.jpg",
            "disc_hash": "hit-hash-001",
            "group_type": "movie",
            "upc": "012345678905",
        }
        disc = crud.persist_disc_scan_with_discdb(session, "hit-hash-001", payload)
        session.commit()
        session.refresh(disc)
        assert disc.release_id is not None

        result = crud.get_discdb_data_from_db(session, "hit-hash-001")
        assert result is not None
        assert result["discdb_hit"] is True
        assert result["label_required"] is False
        assert result["label_ready"] is True
        assert result["movie_name"] == "Scraped tmdb-cache-test"
        assert result["production_year"] == 2024
        assert result["release_year"] == 2024
        assert result["tmdb_id"] == "tmdb-cache-test"
        assert result["title_type"] == "movie"
    finally:
        session.close()


def test_get_discdb_data_from_db_pending_when_release_incomplete(test_db, monkeypatch):
    """Unlinked disc with pending_release_id returns hit-shaped cache with label_required True."""
    monkeypatch.setattr(crud, "fetch_tmdb_metadata_for_id", _fake_fetch_tmdb_default)
    session = test_db()
    try:
        payload = {
            "discdb_hit": True,
            "tmdb_id": "tmdb-pending-1",
            "movie_name": "Pending Movie",
            "production_year": 2023,
            "release_year": 2023,
            "disc_hash": "pending-hash-001",
            "group_type": "movie",
        }
        disc = crud.persist_disc_scan_with_discdb(session, "pending-hash-001", payload)
        session.commit()
        session.refresh(disc)
        assert disc.release_id is None
        info = disc.disc_info or {}
        assert info.get("pending_release_id")

        result = crud.get_discdb_data_from_db(session, "pending-hash-001")
        assert result is not None
        assert result["discdb_hit"] is True
        assert result["label_required"] is True
        assert result["label_ready"] is False
        assert result["release_link_ready"] is False
        assert result.get("pending_release_id")
    finally:
        session.close()


def test_apply_discdb_metadata_to_titles_sets_description_preserves_comment(test_db):
    """Overlay should set description from DiscDB and not touch MakeMKV comment."""
    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="meta-desc-hash", format="UHD")
        session.add(disc)
        session.commit()
        session.refresh(disc)
        title = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            index=0,
            order_index=0,
            source_file="00800.mpls",
            comment="MakeMKV output name hint",
            type=None,
        )
        session.add(title)
        session.commit()

        disc = session.query(models.Disc).filter(models.Disc.id == disc.id).first()
        crud._apply_discdb_metadata_to_titles(
            disc,
            {
                "00800.mpls": {
                    "type": "MainMovie",
                    "title": "Episode Name",
                    "description": "Long synopsis",
                }
            },
        )
        session.commit()
        session.refresh(title)
        assert title.type == "MainMovie"
        assert title.title == "Episode Name"
        assert title.description == "Long synopsis"
        assert title.comment == "MakeMKV output name hint"
    finally:
        session.close()


def test_apply_discdb_metadata_prefill_miss_skips_ignore_and_mass_default(test_db, monkeypatch):
    """With discdb_miss_workflow_with_prefill, do not apply DiscDB ignore or default empty types to ignore."""
    monkeypatch.setattr(crud, "get_discdb_miss_workflow_with_prefill", lambda: True)
    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="prefill-ignore-hash", format="Blu-Ray")
        session.add(disc)
        session.commit()
        session.refresh(disc)
        t_ignore = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            index=0,
            order_index=0,
            source_file="00001.mpls",
            type=None,
        )
        t_other = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            index=1,
            order_index=1,
            source_file="00002.mpls",
            type=None,
        )
        session.add_all([t_ignore, t_other])
        session.commit()

        disc = session.query(models.Disc).filter(models.Disc.id == disc.id).first()
        crud._apply_discdb_metadata_to_titles(
            disc,
            {
                "00001.mpls": {"type": "ignore", "title": "Junk"},
                "00002.mpls": {"type": "MainMovie", "title": "Film"},
            },
        )
        session.commit()
        session.refresh(t_ignore)
        session.refresh(t_other)
        assert t_ignore.type is None
        assert t_ignore.title == "Junk"
        assert t_other.type == "MainMovie"
        assert t_other.title == "Film"
    finally:
        session.close()


def test_apply_discdb_metadata_prefill_miss_blank_type_skips_type_assignment(test_db, monkeypatch):
    """Blank DiscDB type (defaults to ignore in normal path) must not set type when prefill miss is on."""
    monkeypatch.setattr(crud, "get_discdb_miss_workflow_with_prefill", lambda: True)
    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="prefill-blank-type-hash", format="Blu-Ray")
        session.add(disc)
        session.commit()
        session.refresh(disc)
        title = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            index=0,
            order_index=0,
            source_file="00800.mpls",
            type=None,
        )
        session.add(title)
        session.commit()

        disc = session.query(models.Disc).filter(models.Disc.id == disc.id).first()
        crud._apply_discdb_metadata_to_titles(
            disc,
            {"00800.mpls": {"type": "", "description": "synopsis"}},
        )
        session.commit()
        session.refresh(title)
        assert title.type is None
        assert title.description == "synopsis"
    finally:
        session.close()


def test_discdb_disc_num_persisted_duplicate_index_unique_disc_numbers(test_db, monkeypatch):
    """Same TheDiscDB index on two discs in one release: both store discdb_disc_num; disc_number unique."""
    monkeypatch.setattr(crud, "fetch_tmdb_metadata_for_id", _fake_fetch_tmdb_default)
    session = test_db()
    try:
        base = {
            "discdb_hit": True,
            "tmdb_id": "tmdb-dup-discdb-idx",
            "movie_name": "Dup Idx Movie",
            "production_year": 2020,
            "release_year": 2020,
            "release_image": "https://example.com/cover.jpg",
            "group_type": "movie",
            "upc": "012345678905",
            "discdb_disc_num": 3,
        }
        d1 = crud.persist_disc_scan_with_discdb(
            session, "hash-dup-idx-a", {**base, "disc_hash": "hash-dup-idx-a"}
        )
        session.refresh(d1)
        d2 = crud.persist_disc_scan_with_discdb(
            session, "hash-dup-idx-b", {**base, "disc_hash": "hash-dup-idx-b"}
        )
        session.refresh(d2)
        assert d1.release_id == d2.release_id
        assert d1.discdb_disc_num == 3 and d2.discdb_disc_num == 3
        assert {d1.disc_number, d2.disc_number} == {1, 2}
    finally:
        session.close()


def test_discdb_rescan_does_not_revert_disc_number_via_payload_disc_number(test_db, monkeypatch):
    """Legacy payload key disc_number (DiscDB index) must not stomp normalized disc_number on rescan."""
    monkeypatch.setattr(crud, "fetch_tmdb_metadata_for_id", _fake_fetch_tmdb_default)
    session = test_db()
    try:
        base = {
            "discdb_hit": True,
            "tmdb_id": "tmdb-rescan-stab",
            "movie_name": "Rescan Movie",
            "production_year": 2021,
            "release_year": 2021,
            "release_image": "https://example.com/c2.jpg",
            "group_type": "movie",
            "upc": "012345678905",
            "discdb_disc_num": 1,
        }
        d1 = crud.persist_disc_scan_with_discdb(
            session, "hash-rescan-a", {**base, "disc_hash": "hash-rescan-a"}
        )
        session.refresh(d1)
        d2 = crud.persist_disc_scan_with_discdb(
            session, "hash-rescan-b", {**base, "disc_hash": "hash-rescan-b", "discdb_disc_num": 1}
        )
        session.refresh(d2)
        assert d1.disc_number == 1 and d2.disc_number == 2
        # Simulate old client/cache sending DiscDB index as disc_number
        crud.get_or_create_disc(
            session,
            "hash-rescan-b",
            session.query(models.Release).filter(models.Release.id == d2.release_id).first(),
            {"disc_number": 1, "discdb_disc_num": 1, "disc_format": "Blu-Ray"},
        )
        session.refresh(d2)
        assert d2.disc_number == 2
    finally:
        session.close()


def test_get_discdb_data_from_db_includes_discdb_disc_num(test_db, monkeypatch):
    monkeypatch.setattr(crud, "fetch_tmdb_metadata_for_id", _fake_fetch_tmdb_default)
    session = test_db()
    try:
        payload = {
            "discdb_hit": True,
            "tmdb_id": "tmdb-ddn-cache",
            "movie_name": "DDN Movie",
            "production_year": 2022,
            "release_year": 2022,
            "release_image": "https://example.com/r.jpg",
            "disc_hash": "hash-ddn-1",
            "group_type": "movie",
            "upc": "012345678905",
            "discdb_disc_num": 4,
        }
        disc = crud.persist_disc_scan_with_discdb(session, "hash-ddn-1", payload)
        session.commit()
        session.refresh(disc)
        assert disc.discdb_disc_num == 4
        cached = crud.get_discdb_data_from_db(session, "hash-ddn-1")
        assert cached is not None
        assert cached.get("discdb_disc_num") == 4
        assert cached.get("disc_number") == 1
    finally:
        session.close()


def test_get_discdb_data_from_db_keyed_per_hash(test_db, monkeypatch):
    """Two linked discs with different content hashes resolve independently —
    a lookup by hash A returns A's data, by hash B returns B's data, and
    neither leaks into the other."""
    monkeypatch.setattr(crud, "fetch_tmdb_metadata_for_id", _fake_fetch_tmdb_default)
    session = test_db()
    try:
        payload_a = {
            "discdb_hit": True,
            "tmdb_id": "tmdb-keyed-a",
            "movie_name": "Movie A",
            "production_year": 2020,
            "release_year": 2020,
            "release_image": "https://example.com/a.jpg",
            "disc_hash": "hash-keyed-a",
            "group_type": "movie",
        }
        payload_b = {
            "discdb_hit": True,
            "tmdb_id": "tmdb-keyed-b",
            "movie_name": "Movie B",
            "production_year": 2021,
            "release_year": 2021,
            "release_image": "https://example.com/b.jpg",
            "disc_hash": "hash-keyed-b",
            "group_type": "movie",
        }
        crud.persist_disc_scan_with_discdb(session, "hash-keyed-a", payload_a)
        crud.persist_disc_scan_with_discdb(session, "hash-keyed-b", payload_b)
        session.commit()

        a = crud.get_discdb_data_from_db(session, "hash-keyed-a")
        b = crud.get_discdb_data_from_db(session, "hash-keyed-b")
        unknown = crud.get_discdb_data_from_db(session, "hash-not-present")

        assert a is not None and b is not None
        assert a["tmdb_id"] == "tmdb-keyed-a"
        assert b["tmdb_id"] == "tmdb-keyed-b"
        assert a["release_year"] == 2020
        assert b["release_year"] == 2021
        # Unrelated hash must still miss — cache is per-hash, not global.
        assert unknown is None
    finally:
        session.close()


def test_get_discdb_data_from_db_invalidates_when_disc_unlinked(test_db, monkeypatch):
    """Cache hit depends on the disc's release link. Clearing the disc's
    release_id (user unlinks the release) flips the cache result back to None
    so the next lookup re-queries the API."""
    monkeypatch.setattr(crud, "fetch_tmdb_metadata_for_id", _fake_fetch_tmdb_default)
    session = test_db()
    try:
        payload = {
            "discdb_hit": True,
            "tmdb_id": "tmdb-invalidate-1",
            "movie_name": "Invalidate Movie",
            "production_year": 2019,
            "release_year": 2019,
            "release_image": "https://example.com/inv.jpg",
            "disc_hash": "hash-invalidate-1",
            "group_type": "movie",
        }
        disc = crud.persist_disc_scan_with_discdb(session, "hash-invalidate-1", payload)
        session.commit()
        session.refresh(disc)

        hit = crud.get_discdb_data_from_db(session, "hash-invalidate-1")
        assert hit is not None
        assert hit["discdb_hit"] is True

        disc.release_id = None
        info = dict(disc.disc_info) if isinstance(disc.disc_info, dict) else {}
        info.pop("pending_release_id", None)
        disc.disc_info = info
        session.commit()

        miss = crud.get_discdb_data_from_db(session, "hash-invalidate-1")
        assert miss is None
    finally:
        session.close()
