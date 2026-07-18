"""Tests for the TMDB label_draft seed behavior in persist_disc_scan_with_discdb (#388).

Uses the test_db fixture (SQLite-backed) to exercise the real persistence
path. The seed is only honored on first persist when the disc has no
existing label_draft and no linked release — re-scans must never trample
a user's labeling.
"""
import pytest

from api import crud, models


def _tmdb_seed(*, tmdb_id="119051", tmdb_type="tv", title="Wednesday",
               year=2022, cover_url="https://image.tmdb.org/t/p/w500/wed.jpg",
               group_type="series"):
    return {
        "tmdb_id": tmdb_id,
        "tmdb_type": tmdb_type,
        "title": title,
        "year": year,
        "cover_url": cover_url,
        "group_type": group_type,
        "source": "tmdb_auto",
    }


def _disc_info(*, hash_, seed=None, suggestion=None, info_title="Wednesday Season 1 Disc 2",
               discdb_hit=False):
    payload = {
        "disc_hash": hash_,
        "info_title": info_title,
        "discdb_hit": discdb_hit,
        "discdb_miss": not discdb_hit,
    }
    if suggestion is not None:
        payload["tmdb_suggestion"] = suggestion
    if seed is not None:
        payload["label_draft_seed"] = seed
    return payload


def test_seed_label_draft_on_first_persist(test_db):
    """Fresh disc + label_draft_seed → disc.label_draft populated."""
    session = test_db()
    try:
        info = _disc_info(hash_="hash-seed-1", seed=_tmdb_seed())
        disc = crud.persist_disc_scan_with_discdb(session, "hash-seed-1", info)
        session.refresh(disc)
        assert isinstance(disc.label_draft, dict)
        assert disc.label_draft["tmdb_id"] == "119051"
        assert disc.label_draft["tmdb_type"] == "tv"
        assert disc.label_draft["title"] == "Wednesday"
        assert disc.label_draft["year"] == 2022
        assert disc.label_draft["group_type"] == "series"
        assert disc.label_draft["source"] == "tmdb_auto"
    finally:
        session.close()


def test_seed_ignored_when_label_draft_already_present(test_db):
    """User-edited draft must NOT be overwritten by a re-scan's seed."""
    session = test_db()
    try:
        # First scan: insert + seed.
        info1 = _disc_info(hash_="hash-seed-2", seed=_tmdb_seed(title="Old Title"))
        disc = crud.persist_disc_scan_with_discdb(session, "hash-seed-2", info1)
        session.refresh(disc)
        assert disc.label_draft and disc.label_draft["title"] == "Old Title"

        # Simulate user editing the draft.
        disc.label_draft = {"tmdb_id": "999", "title": "User Picked", "source": "user"}
        session.commit()
        session.refresh(disc)

        # Re-scan with a fresh seed — must NOT clobber.
        info2 = _disc_info(hash_="hash-seed-2", seed=_tmdb_seed(title="Re-scan Suggestion"))
        crud.persist_disc_scan_with_discdb(session, "hash-seed-2", info2)
        session.refresh(disc)
        assert disc.label_draft["title"] == "User Picked"
        assert disc.label_draft["source"] == "user"
    finally:
        session.close()


def test_no_seed_no_label_draft(test_db):
    """Absence of seed → label_draft remains None (the default)."""
    session = test_db()
    try:
        info = _disc_info(hash_="hash-seed-3")  # no seed
        disc = crud.persist_disc_scan_with_discdb(session, "hash-seed-3", info)
        session.refresh(disc)
        assert disc.label_draft is None
    finally:
        session.close()


def test_tmdb_suggestion_persisted_to_disc_info_json(test_db):
    """The suggestion block survives persist into disc.disc_info (no migration)."""
    session = test_db()
    try:
        suggestion = {
            "tmdb_id": "438631",
            "tmdb_type": "movie",
            "title": "Dune",
            "year": 2021,
            "cover_url": "https://image.tmdb.org/t/p/w500/d.jpg",
            "score": 0.95,
            "normalized_query": "dune",
            "hints": {},
            "candidates": [],
        }
        info = _disc_info(hash_="hash-seed-4", suggestion=suggestion)

        disc = crud.persist_disc_scan_with_discdb(session, "hash-seed-4", info)

        # The disc_info column write happens via the _extract/_store helpers
        # that crud users invoke alongside persist_disc_scan_with_discdb.
        scan_info = crud._extract_disc_scan_info(info)
        crud._store_disc_scan_info(session, disc, scan_info)
        session.refresh(disc)

        assert isinstance(disc.disc_info, dict)
        assert disc.disc_info["tmdb_suggestion"]["tmdb_id"] == "438631"
        assert disc.disc_info["tmdb_suggestion"]["score"] == 0.95
        # The seed itself is transient and must not be persisted to disc_info.
        assert "label_draft_seed" not in disc.disc_info
    finally:
        session.close()


def test_seed_not_applied_when_release_already_linked(test_db, monkeypatch):
    """If DiscDB already linked a release for the disc, the TMDB seed
    must not interfere — release wins."""
    # Avoid TMDB-scrape network call during ensure_release_from_discdb.
    monkeypatch.setattr(crud, "fetch_tmdb_metadata_for_id", lambda tid, ttype=None, **kw: {
        "name": "DiscDB-linked Movie",
        "production_year": 2020,
        "cover_url": None,
        "tmdb_type": "movie",
        "tmdb_id": str(tid),
    })
    session = test_db()
    try:
        payload = {
            "discdb_hit": True,
            "tmdb_id": "tmdb-link-1",
            "movie_name": "DiscDB Movie",
            "release_year": 2020,
            "release_image": "https://thediscdb.com/img.jpg",
            "disc_hash": "hash-seed-5",
            "group_type": "movie",
            "info_title": "DiscDB Movie",
            "label_draft_seed": _tmdb_seed(tmdb_id="should-not-win"),
        }
        disc = crud.persist_disc_scan_with_discdb(session, "hash-seed-5", payload)
        session.refresh(disc)
        # release_id should be set by the DiscDB ingest path; the seed must be a no-op.
        # (We don't assert on disc.label_draft here — sync_disc_label_draft_with_release
        # owns it when a release is linked.)
        if disc.release_id is not None:
            assert not disc.label_draft or disc.label_draft.get("tmdb_id") != "should-not-win"
    finally:
        session.close()
