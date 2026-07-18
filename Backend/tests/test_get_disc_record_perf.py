"""#600: trimmed projection for `GET /releases/disc/{disc_id}`.

Live measurement on a 309-title disc clocked the endpoint at 30+ seconds
because the projection lazy-loaded `title_streams` mid-loop AND shipped the
full per-title JSON columns (`chapters`, `streams`, `metadata_scan`,
`detection_flags`) the Library drawer never reads.

Same playbook as #530 → #532 for the page endpoint: `load_only` on the
columns the drawer actually uses, `selectinload` to keep relations
batched. These tests pin both the query-count bound and the lean payload
contract so a future regression (e.g. someone adds back a getattr on a
deferred field) fails loudly.
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


def _seed_disc_with_titles(session, *, n_titles: int, n_streams_per_title: int = 1):
    """Build movie → release → disc → N titles + (N×k) streams.

    Each title has the four heavy JSON columns populated so the test can
    confirm that the trimmed response returns `None` for them — proving
    `load_only` is engaged (a regression that re-introduces the field
    would show real values).
    """
    movie = models.Movie(id=str(uuid.uuid4()), name="Perf Disc Movie")
    release = models.Release(
        id=str(uuid.uuid4()),
        slug=f"perf-disc-{uuid.uuid4().hex[:8]}",
        type="movie",
        name="Perf Disc Release",
        movie_id=movie.id,
    )
    disc = models.Disc(
        id=str(uuid.uuid4()),
        content_hash=f"hash-perf-{uuid.uuid4().hex[:8]}",
        release_id=release.id,
        disc_number=1,
        disc_name="Perf Disc",
        format="Blu-Ray",
        # Big-ish JSON blobs on the Disc itself — these were also shipped
        # to the drawer before the trim. The endpoint shouldn't load them.
        disc_info={"raw_scan_log": "x" * 4096, "tracks": list(range(50))},
        label_payload={"unused": "label_payload should not load"},
        label_draft={"unused": "label_draft should not load"},
    )
    session.add_all([movie, release, disc])
    session.flush()
    titles: list[str] = []
    for i in range(n_titles):
        t = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            index=i,
            order_index=i,
            title=f"Title {i}",
            type="Other" if i % 4 else "MainMovie",
            duration=60.0 + i,
            description=f"Desc {i}",
            edition=f"Ed {i}" if i % 3 == 0 else None,
            # Heavy JSON columns we're trimming. Populated specifically so
            # the response-shape assertions below can prove they were NOT
            # shipped despite being present in the database.
            chapters={"chapters": [{"start": j} for j in range(5)]},
            streams=[{"codec": "h264", "language": "en"} for _ in range(3)],
            metadata_scan={"format": {"duration": 60.0 + i}, "tags": ["a"] * 20},
            detection_flags={"silence_pct": 0.1, "black_pct": 0.05, "padding": False},
            detection_confidence=0.5,
            detection_warning=False,
        )
        session.add(t)
        titles.append(t.id)
    for ti, title_id in enumerate(titles):
        for si in range(n_streams_per_title):
            session.add(
                models.TitleStream(
                    disc_id=disc.id,
                    title_id=title_id,
                    stream_index=si,
                    stream_type="video" if si == 0 else "audio",
                    codec_short="h264" if si == 0 else "ac3",
                    language="en",
                )
            )
    session.commit()
    return disc.id, titles


def _attach_statement_counter(engine):
    """Returns (counts: dict[str, int], detach) — call detach() in a finally."""
    counts = {"discs": 0, "disc_titles": 0, "title_streams": 0, "releases": 0, "movies": 0, "other": 0, "total": 0}

    @event.listens_for(engine, "after_cursor_execute")
    def _count(conn, cursor, statement, *a):
        lower = statement.lower()
        counts["total"] += 1
        # The first FROM that matches in the SELECT determines the bucket.
        # Joined queries can mention multiple tables — first-match keeps it
        # simple without conflating.
        if " from disc_titles" in lower:
            counts["disc_titles"] += 1
        elif " from title_streams" in lower:
            counts["title_streams"] += 1
        elif " from discs" in lower:
            counts["discs"] += 1
        elif " from releases" in lower:
            counts["releases"] += 1
        elif " from movies" in lower:
            counts["movies"] += 1
        else:
            counts["other"] += 1

    def detach():
        event.remove(engine, "after_cursor_execute", _count)

    return counts, detach


def test_get_disc_record_query_count_is_bounded_on_large_disc(client, test_db):
    """#600: 300 titles must not multiply per-title queries.

    Before the trim, hitting a 309-title disc was super-linear (~99 ms/title)
    because `disc.title_streams` lazy-loaded mid-projection AND every
    deferred JSON column on `disc_titles` could fire its own SELECT when
    accessed.
    """
    session = test_db()
    try:
        disc_id, _ = _seed_disc_with_titles(session, n_titles=300, n_streams_per_title=1)
    finally:
        session.close()

    engine = test_db.kw["bind"]
    counts, detach = _attach_statement_counter(engine)
    try:
        response = client.get(f"/releases/disc/{disc_id}")
    finally:
        detach()

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["titles"]) == 300

    # Hard bound: 1 disc + 1 titles (selectinload) + 1 title_streams
    # (selectinload) + 1 release (joinedload — single LEFT OUTER JOIN, so
    # often part of the disc query, not separate) + a tiny budget for
    # FastAPI/SQLAlchemy startup queries (`SELECT version`, savepoints).
    assert counts["disc_titles"] <= 1, (
        f"disc_titles fired {counts['disc_titles']} queries (expected ≤1 batched selectinload). "
        f"All counts: {counts}"
    )
    assert counts["title_streams"] <= 1, (
        f"title_streams fired {counts['title_streams']} queries (expected ≤1). All counts: {counts}"
    )
    # 6 is a generous total — current actual is ~3–5 depending on
    # SQLAlchemy's housekeeping. If this grows past 6 someone re-introduced
    # the kind of per-title lazy load this test guards against.
    assert counts["total"] <= 6, f"too many queries total: {counts['total']} (counts: {counts})"


def test_get_disc_record_strips_heavy_json_columns(client, test_db):
    """#600: chapters / streams / detection_flags / metadata_scan must come
    back as None even when the rows have values.

    This is the contract that `load_only` enforces — if a future PR adds back
    a getattr on a deferred column inside `_disc_record`, SQLAlchemy will
    happily fire one SELECT per row, the perf regression returns, and this
    assertion catches it because the now-populated columns aren't None.
    """
    session = test_db()
    try:
        disc_id, _ = _seed_disc_with_titles(session, n_titles=4, n_streams_per_title=1)
    finally:
        session.close()

    response = client.get(f"/releases/disc/{disc_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["titles"]) == 4

    for row in body["titles"]:
        # These four are populated in the seed but excluded by load_only.
        # The handler / _disc_record helper must pass None literals so we
        # never trigger a lazy fetch.
        assert row["chapters"] is None, f"chapters leaked into the response: {row['chapters']}"
        assert row["streams"] is None, f"streams leaked into the response: {row['streams']}"
        assert row["detection_flags"] is None, f"detection_flags leaked: {row['detection_flags']}"
        # metadata_scan is not surfaced by the constructor at all, but the
        # schema field exists — keep it None too.
        assert row.get("metadata_scan") is None, f"metadata_scan leaked: {row.get('metadata_scan')}"
        # Lean fields the drawer DOES use must still arrive intact.
        assert row["title"]
        assert row["duration"] is not None


def test_get_disc_record_returns_all_titles_and_basic_shape(client, test_db):
    """Sanity: the trim hasn't accidentally dropped a title row or scalar
    field the drawer relies on."""
    session = test_db()
    try:
        disc_id, title_ids = _seed_disc_with_titles(session, n_titles=10)
    finally:
        session.close()

    response = client.get(f"/releases/disc/{disc_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == disc_id
    assert body["disc_name"] == "Perf Disc"
    assert body["format"] == "Blu-Ray"
    assert {row["id"] for row in body["titles"]} == set(title_ids)
    for row in body["titles"]:
        # 11 fields the drawer's buildTitleRowsFromRecord reads.
        for f in ("id", "title", "type", "season", "episode", "edition",
                  "description", "duration"):
            assert f in row, f"missing field {f!r} in trimmed response"


def test_get_disc_record_returns_file_path_and_stage(client, test_db):
    """#607 follow-up: the projection eager-loads `file_path` /
    `file_path_stage` so the Library disc drawer can render the
    "At destination" / "In transient" indicator. Before this spec, the
    columns were loaded by SQLAlchemy but silently dropped by
    `_disc_record()`'s `DiscTitleRecord(...)` call — every drawer row
    fell through to "Path unknown" regardless of what the DB recorded.

    Seed a disc with one title at each pipeline stage and assert the
    endpoint round-trips both fields exactly."""
    session = test_db()
    try:
        movie = models.Movie(id=str(uuid.uuid4()), name="Stage Movie")
        release = models.Release(
            id=str(uuid.uuid4()),
            slug=f"stage-disc-{uuid.uuid4().hex[:8]}",
            type="movie",
            name="Stage Release",
            movie_id=movie.id,
        )
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=f"hash-stage-{uuid.uuid4().hex[:8]}",
            release_id=release.id,
            disc_number=1,
            disc_name="Stage Disc",
            format="Blu-Ray",
        )
        session.add_all([movie, release, disc])
        session.flush()

        stage_specs = [
            ("Rip stage",       "/jobs/J/raw/00001.mkv",                                              "rip"),
            ("Postprocess stage","/jobs/J/transient/Movies/Stage Movie (2024)/Stage Movie.mkv",        "postprocess"),
            ("Transfer local",  "/mnt/library/Movies/Stage Movie (2024)/Stage Movie.mkv",             "transfer"),
            ("Transfer smb",    "smb://10.0.6.11/PLEX Media/Movies/Stage Movie (2024)/Stage Movie.mkv","transfer"),
            ("Transfer rsync",  "plex@10.0.6.50:/mnt/library/Movies/Stage Movie/Stage Movie.mkv",     "transfer"),
            ("No path yet",     None,                                                                  None),
        ]
        title_ids: list[str] = []
        for i, (title, file_path, stage) in enumerate(stage_specs):
            t = models.DiscTitle(
                id=str(uuid.uuid4()),
                disc_id=disc.id,
                index=i,
                order_index=i,
                title=title,
                file_path=file_path,
                file_path_stage=stage,
            )
            session.add(t)
            title_ids.append(t.id)
        session.commit()
        disc_id = disc.id
    finally:
        session.close()

    response = client.get(f"/releases/disc/{disc_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    rows_by_title = {row["title"]: row for row in body["titles"]}
    for title, expected_path, expected_stage in stage_specs:
        row = rows_by_title[title]
        assert "file_path" in row, "file_path missing from DiscTitleRecord projection"
        assert "file_path_stage" in row, "file_path_stage missing from DiscTitleRecord projection"
        assert row["file_path"] == expected_path, (
            f"{title!r}: expected file_path {expected_path!r}, got {row['file_path']!r}"
        )
        assert row["file_path_stage"] == expected_stage, (
            f"{title!r}: expected file_path_stage {expected_stage!r}, got {row['file_path_stage']!r}"
        )
