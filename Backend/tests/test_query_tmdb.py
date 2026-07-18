"""Tests for disc_manager.query_tmdb and the scan-completion wiring (#388).

These exercise the auto-lookup path that runs in parallel to TheDiscDB on
disc-scan completion. The real TMDB client is monkeypatched — we never
make network calls in tests.
"""
from unittest.mock import MagicMock

import pytest

from core import disc_manager, tmdb_client
from core.tmdb_client import TmdbCandidate, TmdbNetworkError


@pytest.fixture(autouse=True)
def _clear_caches():
    tmdb_client.clear_cache()
    yield
    tmdb_client.clear_cache()


@pytest.fixture
def with_key(monkeypatch):
    """Pretend a TMDB key is configured and devmode toggle is off."""
    monkeypatch.setattr(disc_manager.app_settings, "get_tmdb_api_key", lambda: "fake-key")
    monkeypatch.setattr(disc_manager.app_settings, "get_tmdb_disabled", lambda: False)
    monkeypatch.setattr(tmdb_client.settings, "get_tmdb_api_key", lambda: "fake-key")
    monkeypatch.setattr(tmdb_client.settings, "get_tmdb_disabled", lambda: False)


def _fake_search(*candidates):
    """Build a search_title replacement that returns the given candidates."""
    def impl(query, *, year_hint=None, media_type=None, limit=3):
        return list(candidates)
    return impl


# ──────────────────────────────────────────────────────────────────────────
# Guard-rails: short-circuit paths that never touch the network
# ──────────────────────────────────────────────────────────────────────────

def test_returns_empty_when_no_key(monkeypatch):
    monkeypatch.setattr(disc_manager.app_settings, "get_tmdb_api_key", lambda: None)
    monkeypatch.setattr(disc_manager.app_settings, "get_tmdb_disabled", lambda: False)
    result = disc_manager.query_tmdb({"info_title": "Dune"}, content_hash="abc")
    assert result == {}


def test_returns_empty_when_devmode_disabled(monkeypatch, with_key):
    monkeypatch.setattr(disc_manager.app_settings, "get_tmdb_disabled", lambda: True)
    result = disc_manager.query_tmdb({"info_title": "Dune"}, content_hash="abc")
    assert result == {}


def test_returns_empty_when_no_usable_title(with_key, monkeypatch):
    calls = {"n": 0}

    def boom(*a, **kw):
        calls["n"] += 1
        return []

    monkeypatch.setattr(disc_manager.tmdb_client, "search_title", boom)
    result = disc_manager.query_tmdb({}, content_hash="abc")
    assert result == {}
    assert calls["n"] == 0, "no title means no search call"


# ──────────────────────────────────────────────────────────────────────────
# Error handling: failures never propagate to the scan pipeline
# ──────────────────────────────────────────────────────────────────────────

def test_swallows_network_error(with_key, monkeypatch):
    def raises(query, *, year_hint=None, media_type=None, limit=3):
        raise TmdbNetworkError("connection refused")

    monkeypatch.setattr(disc_manager.tmdb_client, "search_title", raises)
    result = disc_manager.query_tmdb({"info_title": "Dune"}, content_hash="abc")
    assert result == {}


def test_empty_results_returns_empty(with_key, monkeypatch):
    monkeypatch.setattr(disc_manager.tmdb_client, "search_title", _fake_search())
    result = disc_manager.query_tmdb({"info_title": "Nonexistent Movie"}, content_hash="abc")
    assert result == {}


# ──────────────────────────────────────────────────────────────────────────
# Happy paths
# ──────────────────────────────────────────────────────────────────────────

def test_returns_normalized_query_and_top_candidate(with_key, monkeypatch):
    candidate = TmdbCandidate(
        tmdb_id="119051", tmdb_type="tv", title="Wednesday",
        year=2022, cover_url="https://image.tmdb.org/t/p/w500/wed.jpg", score=0.91,
    )
    monkeypatch.setattr(disc_manager.tmdb_client, "search_title", _fake_search(candidate))

    result = disc_manager.query_tmdb(
        {"info_title": "Wednesday Season 1 Disc 2"}, content_hash="abc",
    )

    assert result["tmdb_id"] == "119051"
    assert result["tmdb_type"] == "tv"
    assert result["title"] == "Wednesday"
    assert result["year"] == 2022
    assert result["score"] == 0.91
    assert result["normalized_query"] == "wednesday"
    assert result["hints"] == {"season": 1, "disc_num": 2}
    assert len(result["candidates"]) == 1


def test_returns_top_3_candidates_for_disambiguation(with_key, monkeypatch):
    """The candidates list lets the UI offer alternatives when the top
    score isn't confident — critical for remake-year disambiguation."""
    candidates = [
        TmdbCandidate("522162", "movie", "Midway", 2019, "/c1.jpg", 0.88),
        TmdbCandidate("11778", "movie", "Midway", 1976, "/c2.jpg", 0.80),
        TmdbCandidate("42", "movie", "Midway (TV)", 1942, "/c3.jpg", 0.50),
    ]
    monkeypatch.setattr(disc_manager.tmdb_client, "search_title", _fake_search(*candidates))

    result = disc_manager.query_tmdb({"info_title": "Midway"}, content_hash="abc")

    assert len(result["candidates"]) == 3
    assert result["candidates"][0]["tmdb_id"] == "522162"


def test_info_title_falls_back_to_info_label(with_key, monkeypatch):
    """When `info_title` is missing, the next candidate fields are tried."""
    candidate = TmdbCandidate("1", "movie", "Joker", 2019, None, 0.95)
    monkeypatch.setattr(disc_manager.tmdb_client, "search_title", _fake_search(candidate))

    result = disc_manager.query_tmdb({"info_label": "Joker"}, content_hash="abc")
    assert result["title"] == "Joker"


def test_info_title_derived_from_raw_log_when_other_fields_missing(with_key, monkeypatch):
    """When the upstream parser didn't set info_title, fall back to CINFO parse."""
    candidate = TmdbCandidate("1", "movie", "1917", 2019, None, 0.95)
    monkeypatch.setattr(disc_manager.tmdb_client, "search_title", _fake_search(candidate))

    raw_log = 'MSG:1005,...\nCINFO:2,0,"1917"\nDRV:0,...'
    result = disc_manager.query_tmdb({"raw_info_log": raw_log}, content_hash="abc")
    assert result["title"] == "1917"


# ──────────────────────────────────────────────────────────────────────────
# Scan-completion wiring (on_disc_scan_complete)
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture
def captured_disc_info(monkeypatch):
    """Capture the disc_info dict that on_disc_scan_complete hands to the
    backend callback so we can assert what was built."""
    captured = {}

    def fake_callback(disc_info):
        captured["disc_info"] = disc_info

    monkeypatch.setattr(disc_manager, "_backend_notify_callback", fake_callback)
    monkeypatch.setattr(disc_manager, "cache_set", lambda *a, **kw: None)
    return captured


def test_scan_complete_discdb_miss_tmdb_hit_seeds_label_draft(
    with_key, monkeypatch, captured_disc_info,
):
    """The headline case: DiscDB miss + confident TMDB hit → label_draft_seed set."""
    monkeypatch.setattr(disc_manager, "query_discdb", lambda h: {"discdb_hit": False})
    monkeypatch.setattr(
        disc_manager.tmdb_client, "search_title",
        _fake_search(TmdbCandidate("119051", "tv", "Wednesday", 2022, "/w.jpg", 0.91)),
    )

    disc_manager.on_disc_scan_complete({
        "disc_num": "1",
        "mount_point": "/dev/sr1",
        "disc_hash": "deadbeef",
        "info_title": "Wednesday Season 1 Disc 2",
    })

    info = captured_disc_info["disc_info"]
    assert info["discdb_miss"] is True
    assert info["tmdb_suggestion"]["tmdb_id"] == "119051"
    assert info["label_draft_seed"]["tmdb_id"] == "119051"
    assert info["label_draft_seed"]["group_type"] == "series"
    assert info["label_draft_seed"]["source"] == "tmdb_auto"


def test_scan_complete_low_score_persists_suggestion_but_no_seed(
    with_key, monkeypatch, captured_disc_info,
):
    """Below threshold (0.75) we still surface the candidates for the user
    to pick from, but don't pre-fill label_draft — better to start blank
    than primed with a likely-wrong match."""
    monkeypatch.setattr(disc_manager, "query_discdb", lambda h: {"discdb_hit": False})
    monkeypatch.setattr(
        disc_manager.tmdb_client, "search_title",
        _fake_search(TmdbCandidate("1", "movie", "Maybe", 2020, None, 0.5)),
    )

    disc_manager.on_disc_scan_complete({
        "disc_num": "1", "mount_point": "/dev/sr1", "disc_hash": "abc",
        "info_title": "Ambiguous",
    })

    info = captured_disc_info["disc_info"]
    assert "tmdb_suggestion" in info
    assert "label_draft_seed" not in info


def test_scan_complete_discdb_hit_does_not_seed_label_draft(
    with_key, monkeypatch, captured_disc_info,
):
    """DiscDB hits already auto-link a release — TMDB suggestion is stored
    as enrichment but must never overwrite or compete with that linkage."""
    monkeypatch.setattr(
        disc_manager, "query_discdb",
        lambda h: {"discdb_hit": True, "movie_name": "Dune", "tmdb_id": "438631"},
    )
    monkeypatch.setattr(
        disc_manager.tmdb_client, "search_title",
        _fake_search(TmdbCandidate("438631", "movie", "Dune", 2021, "/d.jpg", 0.95)),
    )

    disc_manager.on_disc_scan_complete({
        "disc_num": "1", "mount_point": "/dev/sr1", "disc_hash": "abc",
        "info_title": "Dune",
    })

    info = captured_disc_info["disc_info"]
    assert info["discdb_hit"] is True
    assert info["tmdb_suggestion"]["tmdb_id"] == "438631"
    assert "label_draft_seed" not in info


def test_scan_complete_with_tmdb_disabled_skips_suggestion_entirely(
    monkeypatch, captured_disc_info,
):
    """When TMDB is off (no key OR devmode toggle), the disc_info dict
    must look identical to the legacy non-TMDB path."""
    monkeypatch.setattr(disc_manager.app_settings, "get_tmdb_api_key", lambda: None)
    monkeypatch.setattr(disc_manager.app_settings, "get_tmdb_disabled", lambda: False)
    monkeypatch.setattr(disc_manager, "query_discdb", lambda h: {"discdb_hit": False})

    # search_title should NOT be called when the key is missing.
    def boom(*a, **kw):
        raise AssertionError("search_title called even though key is missing")

    monkeypatch.setattr(disc_manager.tmdb_client, "search_title", boom)

    disc_manager.on_disc_scan_complete({
        "disc_num": "1", "mount_point": "/dev/sr1", "disc_hash": "abc",
        "info_title": "Anything",
    })

    info = captured_disc_info["disc_info"]
    assert "tmdb_suggestion" not in info
    assert "label_draft_seed" not in info


# ──────────────────────────────────────────────────────────────────────────
# Key-save backfill: TMDB lookup for existing discs (#388 follow-up)
# ──────────────────────────────────────────────────────────────────────────

def test_backfill_populates_suggestions_for_unlabeled_discs(test_db, with_key, monkeypatch):
    """Existing user plugs in a key — backfill should hit every unlabeled disc."""
    from api import crud
    candidate = TmdbCandidate("119051", "tv", "Wednesday", 2022, "/w.jpg", 0.91)
    monkeypatch.setattr(disc_manager.tmdb_client, "search_title", _fake_search(candidate))

    session = test_db()
    try:
        # Two unlabeled discs — both should be processed and updated.
        crud.persist_disc_scan_with_discdb(session, "hash-bf-1", {
            "info_title": "Wednesday Season 1 Disc 1", "disc_hash": "hash-bf-1",
        })
        crud.persist_disc_scan_with_discdb(session, "hash-bf-2", {
            "info_title": "Wednesday Season 1 Disc 2", "disc_hash": "hash-bf-2",
        })

        result = disc_manager.backfill_tmdb_suggestions_for_unlabeled_discs(session)

        assert result["scanned"] == 2
        assert result["updated"] == 2
        assert result["seeded"] == 2  # score 0.91 > 0.75 threshold

        # Both discs should now carry the tmdb_suggestion + label_draft seed.
        from api import models
        for h in ("hash-bf-1", "hash-bf-2"):
            d = session.query(models.Disc).filter(models.Disc.content_hash == h).first()
            assert d.disc_info["tmdb_suggestion"]["tmdb_id"] == "119051"
            assert d.label_draft["tmdb_id"] == "119051"
            assert d.label_draft["source"] == "tmdb_auto"
    finally:
        session.close()


def test_backfill_skips_discs_with_existing_suggestion(test_db, with_key, monkeypatch):
    """Idempotent: re-running the backfill must not re-query or re-write."""
    from api import crud, models
    calls = {"n": 0}

    def counting_search(query, *, year_hint=None, media_type=None, limit=3):
        calls["n"] += 1
        return [TmdbCandidate("1", "movie", "Foo", 2020, None, 0.9)]

    monkeypatch.setattr(disc_manager.tmdb_client, "search_title", counting_search)

    session = test_db()
    try:
        crud.persist_disc_scan_with_discdb(session, "hash-bf-3", {
            "info_title": "Foo", "disc_hash": "hash-bf-3",
        })

        first = disc_manager.backfill_tmdb_suggestions_for_unlabeled_discs(session)
        second = disc_manager.backfill_tmdb_suggestions_for_unlabeled_discs(session)

        assert first["updated"] == 1
        assert second["updated"] == 0
        # search_title called exactly once across both runs.
        assert calls["n"] == 1
    finally:
        session.close()


def test_backfill_no_op_when_key_missing(test_db, monkeypatch):
    """Without a key configured the backfill returns zero counts and never
    touches TMDB or the DB."""
    monkeypatch.setattr(disc_manager.app_settings, "get_tmdb_api_key", lambda: None)
    monkeypatch.setattr(disc_manager.app_settings, "get_tmdb_disabled", lambda: False)

    def boom(*a, **kw):
        raise AssertionError("search_title called with no key configured")

    monkeypatch.setattr(disc_manager.tmdb_client, "search_title", boom)

    session = test_db()
    try:
        result = disc_manager.backfill_tmdb_suggestions_for_unlabeled_discs(session)
        assert result == {"scanned": 0, "updated": 0, "seeded": 0}
    finally:
        session.close()


def test_backfill_skips_discs_with_linked_release(test_db, with_key, monkeypatch):
    """Already-labeled discs (release_id IS NOT NULL) must be left alone —
    backfill only helps the unlabeled cohort."""
    from api import crud, models
    candidate = TmdbCandidate("1", "movie", "Foo", 2020, None, 0.9)
    monkeypatch.setattr(disc_manager.tmdb_client, "search_title", _fake_search(candidate))

    session = test_db()
    try:
        # Insert an unlabeled disc, then create a release and link it
        # explicitly so we can assert the backfill's filter — independent
        # of whether DiscDB-driven ingest also link-attaches in the fixture.
        crud.persist_disc_scan_with_discdb(session, "hash-bf-linked", {
            "info_title": "Foo", "disc_hash": "hash-bf-linked",
        })
        disc = session.query(models.Disc).filter(
            models.Disc.content_hash == "hash-bf-linked"
        ).first()
        movie = models.Movie(name="Foo", production_year=2020, tmdb_id="2020")
        session.add(movie)
        session.flush()
        release = models.Release(slug="foo-2020", type="movie",
                                 name="Foo", release_year=2020, movie_id=movie.id)
        session.add(release)
        session.flush()
        disc.release_id = release.id
        session.commit()

        result = disc_manager.backfill_tmdb_suggestions_for_unlabeled_discs(session)
        # The linked disc is filtered out by release_id IS NULL — scanned=0.
        assert result["scanned"] == 0
    finally:
        session.close()
