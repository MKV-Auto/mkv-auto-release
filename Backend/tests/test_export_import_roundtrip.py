"""#611 — Settings → Export/Import roundtrip regression.

The pre-#611 suite covered ``_model_to_dict`` and the individual endpoint
responses but never proved an end-to-end roundtrip: that a fully
populated DB serialized with ``serialize_database`` then fed to
``deserialize_database`` against a wiped DB produces the same row set
and FK chains.

After the wave of v1.0.1 polish commits (#593 transfer-history identity,
#594 previews ceiling, #595 ui-checkbox, #601 disc-drawer compact card,
#602 title-editor cleanup, #607 file_path writer + projection), the
schema is stable but the integration was un-tested. These specs pin it.
"""
import datetime
import uuid

import pytest

from api import models
from api.export_import import deserialize_database, serialize_database


def _seed_one_release(session):
    """A movie + a release + a disc + 4 titles + 4 streams + 1 job."""
    now = datetime.datetime.now(datetime.timezone.utc)
    movie = models.Movie(
        id=str(uuid.uuid4()),
        name="Test Movie",
        production_year=2024,
    )
    release = models.Release(
        id=str(uuid.uuid4()),
        slug=f"test-release-{uuid.uuid4().hex[:6]}",
        type="movie",
        name="Test Release",
        movie_id=movie.id,
    )
    disc = models.Disc(
        id=str(uuid.uuid4()),
        content_hash=f"hash-{uuid.uuid4().hex[:8]}",
        release_id=release.id,
        disc_number=1,
        format="Blu-Ray",
    )
    session.add_all([movie, release, disc])
    session.flush()

    titles = []
    streams = []
    for i in range(4):
        t = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            index=i,
            order_index=i,
            title=f"Title {i}",
            type="MainMovie" if i == 0 else "Extra",
            file_path=f"/library/Movie/Title {i}.mkv",
            file_path_stage="transfer",
        )
        titles.append(t)
        session.add(t)
    session.flush()

    for t in titles:
        s = models.TitleStream(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            title_id=t.id,
            stream_index=0,
            stream_type="video",
        )
        streams.append(s)
        session.add(s)

    job = models.Job(
        id=str(uuid.uuid4()),
        disc_id=disc.id,
        disc_num="1",
        mount_point="/dev/sr0",
        job_status="completed",
        rip_state="completed",
        transfer_state="completed",
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.commit()

    return {
        "movie_id": movie.id,
        "release_id": release.id,
        "disc_id": disc.id,
        "title_ids": [t.id for t in titles],
        "stream_ids": [s.id for s in streams],
        "job_id": job.id,
    }


def _seed_boxset(session, release_id: str):
    """A boxset linked to an existing release via Release.boxset_id
    (the Boxset → Release relationship is a direct FK on the release,
    not a separate join table)."""
    boxset = models.Boxset(
        id=str(uuid.uuid4()),
        slug=f"test-boxset-{uuid.uuid4().hex[:6]}",
        name="Test Boxset",
    )
    session.add(boxset)
    session.flush()
    release = session.query(models.Release).filter_by(id=release_id).first()
    release.boxset_id = boxset.id
    session.commit()
    return {"boxset_id": boxset.id}


def _wipe(session):
    """Truncate the export-scope tables in reverse-FK order so reseed
    + roundtrip start clean."""
    # Releases have an FK to Boxsets, so unlink first to avoid a
    # constraint violation when Boxsets get wiped before Releases.
    for r in session.query(models.Release).all():
        r.boxset_id = None
    session.commit()
    for table in (
        models.Job, models.TitleStream, models.DiscTitle,
        models.Disc, models.Boxset,
        models.Release, models.Movie,
    ):
        session.query(table).delete()
    session.commit()


# ──────────────────────────────────────────────────────────────────────────
# Roundtrip
# ──────────────────────────────────────────────────────────────────────────


def test_serialize_returns_all_seven_export_tables(test_db):
    """The export shape promises 7 tables — verify the serializer populates
    every key. Guards against a future refactor that silently drops a
    table from the export. (Pre-#611 the implementation tried to also
    export a `boxset_releases` join table that doesn't exist as a model;
    the dead reference was removed as part of #611.)"""
    session = test_db()
    try:
        ids = _seed_one_release(session)
        _seed_boxset(session, ids["release_id"])

        serialized = serialize_database(session)

        for table_key in (
            "movies", "releases", "discs", "jobs",
            "disc_titles", "title_streams",
            "boxsets",
        ):
            assert table_key in serialized, f"export missing table {table_key!r}"
            assert serialized[table_key], f"export table {table_key!r} is empty"
        assert "boxset_releases" not in serialized
    finally:
        session.close()


def test_export_then_import_restores_all_rows_and_fk_chains(test_db):
    """End-to-end: seed → serialize → wipe → deserialize → re-query.
    Every row count matches and a sampled FK chain (release → movie;
    release → boxset; disc → release; job → disc; title_stream → title)
    resolves correctly."""
    session = test_db()
    try:
        ids = _seed_one_release(session)
        bx = _seed_boxset(session, ids["release_id"])

        serialized = serialize_database(session)
        _wipe(session)

        # serialize_database returns the inner table dict; deserialize wraps
        # it under the "database" key.
        summary = deserialize_database({"database": serialized}, session)

        # Each table imported the expected number of rows.
        assert summary["movies_imported"] == 1
        assert summary["releases_imported"] == 1
        assert summary["discs_imported"] == 1
        assert summary["jobs_imported"] == 1
        assert summary["disc_titles_imported"] == 4
        assert summary["title_streams_imported"] == 4
        assert summary["boxsets_imported"] == 1

        # FK chains resolve after restore.
        m = session.query(models.Movie).filter_by(id=ids["movie_id"]).first()
        r = session.query(models.Release).filter_by(id=ids["release_id"]).first()
        d = session.query(models.Disc).filter_by(id=ids["disc_id"]).first()
        j = session.query(models.Job).filter_by(id=ids["job_id"]).first()
        assert m and r and d and j
        assert r.movie_id == m.id
        assert d.release_id == r.id
        assert j.disc_id == d.id

        # disc_titles + title_streams join through their FKs.
        titles = session.query(models.DiscTitle).filter_by(disc_id=d.id).all()
        assert {t.id for t in titles} == set(ids["title_ids"])
        streams = session.query(models.TitleStream).filter_by(disc_id=d.id).all()
        assert {s.id for s in streams} == set(ids["stream_ids"])
        # Each stream's title_id points back into the imported titles.
        assert all(s.title_id in {t.id for t in titles} for s in streams)

        # Boxset link is preserved via Release.boxset_id.
        assert r.boxset_id == bx["boxset_id"]

        # #607 follow-up: file_path / file_path_stage preserve through
        # the roundtrip so a re-imported Library disc renders correctly.
        assert all(t.file_path is not None for t in titles)
        assert all(t.file_path_stage == "transfer" for t in titles)
    finally:
        session.close()


def test_idempotent_second_import_skips_all_rows(test_db):
    """Re-running deserialize on the same payload after the rows are
    already present must not double-write — every row falls into the
    ``*_skipped`` bucket. This is the path the user hits when they
    import the same .zip twice by accident."""
    session = test_db()
    try:
        ids = _seed_one_release(session)
        _seed_boxset(session, ids["release_id"])

        serialized = serialize_database(session)

        # Don't wipe — just re-deserialize against the populated DB.
        summary = deserialize_database({"database": serialized}, session)

        assert summary["movies_imported"] == 0
        assert summary["releases_imported"] == 0
        assert summary["discs_imported"] == 0
        assert summary["jobs_imported"] == 0
        assert summary["disc_titles_imported"] == 0
        assert summary["title_streams_imported"] == 0
        assert summary["boxsets_imported"] == 0

        # And the skip counters got incremented to match the row counts.
        assert summary["movies_skipped"] == 1
        assert summary["releases_skipped"] == 1
        assert summary["discs_skipped"] == 1
        assert summary["jobs_skipped"] == 1
        assert summary["disc_titles_skipped"] == 4
        assert summary["title_streams_skipped"] == 4
        assert summary["boxsets_skipped"] == 1
    finally:
        session.close()


def test_partial_payload_only_movies_works(test_db):
    """A user editing a payload by hand might submit a subset (e.g. only
    movies). Deserialize must accept missing keys without raising."""
    session = test_db()
    try:
        ids = _seed_one_release(session)
        full = serialize_database(session)
        _wipe(session)

        # Only the movies key — every other table key is missing.
        partial = {"database": {"movies": full["movies"]}}
        summary = deserialize_database(partial, session)

        assert summary["movies_imported"] == 1
        # Nothing else should land.
        assert summary["releases_imported"] == 0
        assert summary["discs_imported"] == 0
        # And the movie is queryable.
        m = session.query(models.Movie).filter_by(id=ids["movie_id"]).first()
        assert m is not None
    finally:
        session.close()


def test_orphaned_release_skipped_when_movie_missing(test_db):
    """A release that points at a movie not in the payload is skipped
    with a warning — the explicit FK-validation behaviour the
    implementation promises at line 280-284."""
    session = test_db()
    try:
        ids = _seed_one_release(session)
        full = serialize_database(session)
        _wipe(session)

        # Drop the movies entry; the release will be orphaned.
        orphan = {"database": {
            "movies": [],
            "releases": full["releases"],
        }}
        summary = deserialize_database(orphan, session)

        assert summary["movies_imported"] == 0
        assert summary["releases_imported"] == 0
        # The release was skipped because its movie_id had no matching row.
        assert summary["releases_skipped"] == 1
        # And it really wasn't inserted.
        assert session.query(models.Release).filter_by(id=ids["release_id"]).first() is None
    finally:
        session.close()
