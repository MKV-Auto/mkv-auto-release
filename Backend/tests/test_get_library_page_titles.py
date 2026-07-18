"""#380 / #530: per-title data on library responses.

#380 projected DiscTitle rows onto each DiscSummary as a typed
`titles: List[TitleSummary]` array. #530 split the contract:

- `GET /releases/library/page` ships `title_count` only — the page reads
  just the count and the drawer fetches its own DiscRecord. The inline
  arrays were ~1.1MB on real data and their hydration dominated the
  request (~4s); `label_present` additionally lazy-loaded
  `disc.title_streams` per disc (~4.6s). Both pinned here.
- `GET /releases/{slug}/discs` keeps the full #380 titles projection
  (file_path/file_path_stage/title_seq, sort order).
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from api import database, models
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


def _seed_release_with_disc_and_titles(session, *, titles_data, with_stream=False):
    """Build a movie → release → disc → titles graph for one library page request."""
    movie = models.Movie(id=str(uuid.uuid4()), name="Test Movie 380")
    release = models.Release(
        id=str(uuid.uuid4()),
        slug=f"test-rel-380-{uuid.uuid4().hex[:8]}",
        type="movie",
        name="Test Release 380",
        movie_id=movie.id,
    )
    disc = models.Disc(
        id=str(uuid.uuid4()),
        content_hash=f"hash380-{uuid.uuid4().hex[:8]}",
        release_id=release.id,
    )
    session.add_all([movie, release, disc])
    session.flush()
    title_ids = []
    for tdata in titles_data:
        t = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            **tdata,
        )
        session.add(t)
        title_ids.append(t.id)
    if with_stream:
        session.add(
            models.TitleStream(
                disc_id=disc.id,
                title_id=title_ids[0] if title_ids else str(uuid.uuid4()),
                title="Stream-only row",
                stream_index=0,
                streams=[{"codec": "dts"}],
            )
        )
    session.commit()
    return release.id, release.slug, disc.id, title_ids


# ── Library page: count-only contract (#530) ─────────────────────────────


def test_library_page_ships_title_count_not_inline_titles(client, test_db):
    session = test_db()
    try:
        release_id, _slug, _disc_id, _ = _seed_release_with_disc_and_titles(
            session,
            titles_data=[
                {"title": "Feature", "order_index": 0, "index": 0, "title_seq": 0},
                {"title": "Extra", "order_index": 1, "index": 1, "title_seq": 0},
            ],
        )
    finally:
        session.close()

    response = client.get("/releases/library/page?limit=10")
    assert response.status_code == 200, response.text
    discs = response.json()["release_discs"][release_id]
    assert len(discs) == 1
    assert discs[0]["titles"] is None, "#530: page response must not inline titles"
    assert discs[0]["title_count"] == 2
    assert discs[0]["label_present"] is True


def test_library_page_title_count_zero_for_disc_without_titles(client, test_db):
    session = test_db()
    try:
        release_id, _slug, _disc_id, _ = _seed_release_with_disc_and_titles(
            session, titles_data=[],
        )
    finally:
        session.close()

    response = client.get("/releases/library/page?limit=10")
    assert response.status_code == 200, response.text
    discs = response.json()["release_discs"][release_id]
    assert discs[0]["titles"] is None
    assert discs[0]["title_count"] == 0
    assert discs[0]["label_present"] is False


def test_library_page_label_present_via_streams_only_without_lazy_load(client, test_db):
    """A disc with TitleStream rows but no DiscTitles must still report
    label_present=True — via the single batched title_streams lookup, never
    a per-disc lazy load (#530: 37 lazy queries ≈ 4.6s)."""
    session = test_db()
    try:
        release_id, _slug, _disc_id, _ = _seed_release_with_disc_and_titles(
            session, titles_data=[], with_stream=True,
        )
        # A second release on the same page so per-disc scaling would show.
        _seed_release_with_disc_and_titles(session, titles_data=[], with_stream=True)
    finally:
        session.close()

    engine = test_db.kw["bind"]
    stream_selects = []

    @event.listens_for(engine, "after_cursor_execute")
    def _count(conn, cursor, statement, *a):
        if "FROM title_streams" in statement or "from title_streams" in statement:
            stream_selects.append(statement)

    try:
        response = client.get("/releases/library/page?limit=10")
    finally:
        event.remove(engine, "after_cursor_execute", _count)

    assert response.status_code == 200, response.text
    discs = response.json()["release_discs"][release_id]
    assert discs[0]["label_present"] is True
    assert len(stream_selects) == 1, (
        f"expected exactly one batched title_streams lookup for the whole "
        f"page, got {len(stream_selects)} (per-disc lazy loads are back?)"
    )


# ── /releases/{slug}/discs: full #380 titles projection lives on ─────────


def test_release_discs_endpoint_still_projects_titles_with_file_path(client, test_db):
    session = test_db()
    try:
        _release_id, slug, _disc_id, title_ids = _seed_release_with_disc_and_titles(
            session,
            titles_data=[
                {
                    "title": "Feature",
                    "type": "Episode",
                    "season": 1,
                    "episode": 1,
                    "duration": 3600.0,
                    "size": 4_000_000_000,
                    "mkv_size": 3_900_000_000,
                    "file_path": "/library/Movies/Feature (2024)/Feature.1080p.mkv",
                    "file_path_stage": "transfer",
                    "title_seq": 5,
                    "order_index": 0,
                    "index": 0,
                },
                {
                    "title": "Behind the Scenes",
                    "type": "Featurette",
                    "duration": 600.0,
                    "size": 800_000_000,
                    "file_path": "/data/jobs/<job_id>/transient/Movies/Feature (2024)/Featurette.mkv",
                    "file_path_stage": "postprocess",
                    "title_seq": 0,
                    "order_index": 1,
                    "index": 1,
                },
            ],
        )
    finally:
        session.close()

    response = client.get(f"/releases/{slug}/discs")
    assert response.status_code == 200, response.text
    discs = response.json()
    assert len(discs) == 1
    titles = discs[0]["titles"]
    assert isinstance(titles, list)
    assert len(titles) == 2
    assert discs[0]["title_count"] == 2

    feature = titles[0]
    assert feature["title"] == "Feature"
    assert feature["season"] == 1
    assert feature["episode"] == 1
    assert feature["file_path"].endswith("Feature.1080p.mkv")
    assert feature["file_path_stage"] == "transfer"
    assert feature["title_seq"] == 5
    assert feature["title_id"] == title_ids[0]

    featurette = titles[1]
    assert featurette["title"] == "Behind the Scenes"
    assert featurette["file_path_stage"] == "postprocess"
    assert featurette["title_seq"] == 0


def test_release_discs_titles_sort_order_matches_discs_titles_endpoint(client, test_db):
    """Sort order is order_index → index → created_at, nulls last. Matches
    `GET /discs/{disc_id}/titles` so users see the same sequence in both views."""
    session = test_db()
    try:
        _release_id, slug, _disc_id, _title_ids = _seed_release_with_disc_and_titles(
            session,
            titles_data=[
                # Intentionally seed out-of-target-order:
                {"title": "Z-third",  "order_index": 2, "index": 2, "title_seq": 0},
                {"title": "X-first",  "order_index": 0, "index": 0, "title_seq": 0},
                {"title": "Y-second", "order_index": 1, "index": 1, "title_seq": 0},
            ],
        )
    finally:
        session.close()

    response = client.get(f"/releases/{slug}/discs")
    titles = response.json()[0]["titles"]
    assert [t["title"] for t in titles] == ["X-first", "Y-second", "Z-third"]
