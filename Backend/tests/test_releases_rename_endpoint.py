"""
Coverage for ``POST /releases/disc/{disc_id}/rename``.

This is the post-process / post-transfer rename endpoint referenced by issues
#325 and #336 (and reused by the history page). It was shipped without any test
coverage; this file is the Phase 0 backfill for the postprocess collapse
(`docs/plans/postprocess-collapse-325-365.md`) so the refactor in Phase 2 has
a regression net to land against.

Implementation under test: ``rename_disc_titles`` in
``Backend/api/routers/releases.py``. Behavioral contract captured here:

  * dry_run=True  → returns a preview of (old_path, new_path, changed, status)
    per title with **no** file I/O and **no** DB writes.
  * dry_run=False → moves files on disk and updates ``DiscTitle.file_path``.
    ``file_path_stage`` is intentionally NOT updated by the rename — that
    field tracks which *pipeline* stage last wrote the path; user-driven
    rename keeps the existing stage marker.
  * Titles with ``type == "ignore"`` or ``file_path is None`` are skipped
    silently (not in the results list).
  * Collisions and missing-source errors are reported per-title in the
    results array (not as HTTP failures) so a partial batch can proceed.
  * 404 on unknown disc, 400 on disc without release, 409 when a transfer
    is currently running or pending for the disc.
"""
import os
import shutil
import uuid

import pytest
from fastapi.testclient import TestClient

from api import database, models
from api.main import app


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def client(test_db):
    """TestClient wired to the test DB. Overrides both the global ``get_db``
    and the releases router's local one (the router defines its own copy)."""
    from api.routers import releases

    def override_get_db():
        session = test_db()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[database.get_db] = override_get_db
    if hasattr(releases, "get_db"):
        app.dependency_overrides[releases.get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed_disc_with_titles(session, tmp_path, *, n_titles=2, file_stage="postprocess",
                           release_type="movie", movie_name="Sample Movie",
                           production_year=2020, with_files=True):
    """Create a Movie + Release + Disc + N DiscTitles for rename tests.

    Returns (disc_id, [title_ids], [absolute_file_paths]).

    Each title's ``file_path`` points to a real MKV file under tmp_path so the
    execute path can actually move bytes. Set ``with_files=False`` for tests
    that need to simulate a missing source.
    """
    movie_id = str(uuid.uuid4())
    session.add(models.Movie(
        id=movie_id, name=movie_name, production_year=production_year,
        tmdb_id=f"tmdb-{uuid.uuid4().hex[:8]}",
    ))
    release_id = str(uuid.uuid4())
    session.add(models.Release(
        id=release_id, slug=f"rel-{uuid.uuid4().hex[:8]}",
        type=release_type, name=movie_name, movie_id=movie_id,
        release_year=production_year, resolution="1080p",
    ))
    disc_id = str(uuid.uuid4())
    session.add(models.Disc(
        id=disc_id, content_hash=f"hash-{uuid.uuid4().hex[:8]}",
        release_id=release_id, format="Blu-Ray",
    ))

    title_ids = []
    file_paths = []
    type_dir = "Movies" if release_type == "movie" else "Series"
    root = tmp_path / "transient"
    for i in range(n_titles):
        title_id = str(uuid.uuid4())
        # Use a different starting filename so the rename produces a real
        # delta (rename target is computed from movie metadata; current name
        # must differ for ``changed=True``).
        rel = f"{type_dir}/Old Name {i}/old-name-{i}.mkv"
        abs_path = str(root / rel)
        if with_files:
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "wb") as fh:
                fh.write(b"x" * 1024)  # 1KB stub
        title = models.DiscTitle(
            id=title_id, disc_id=disc_id,
            title=f"Sample Title {i}",
            type="movie" if release_type == "movie" else "episode",
            file_path=abs_path,
            file_path_stage=file_stage,
            source_file=f"0000{i}.mpls",
        )
        if release_type != "movie":
            title.season = 1
            title.episode = i + 1
        session.add(title)
        title_ids.append(title_id)
        file_paths.append(abs_path)
    session.commit()
    return disc_id, title_ids, file_paths


# ──────────────────────────────────────────────────────────────────────────
# Happy paths
# ──────────────────────────────────────────────────────────────────────────


def test_rename_dry_run_returns_preview_without_moving_files(client, test_db, tmp_path):
    """Dry-run returns the (old → new) preview, leaves files in place, and
    does not touch ``DiscTitle.file_path`` in the DB. Also verifies the
    response parses cleanly via :class:`api.schemas.RenameResponse` so
    schema drift between the route and the Pydantic model is caught here
    (#325 close-out)."""
    from api.schemas import RenameResponse

    session = test_db()
    try:
        disc_id, title_ids, file_paths = _seed_disc_with_titles(session, tmp_path)
    finally:
        session.close()

    resp = client.post(f"/releases/disc/{disc_id}/rename?dry_run=true")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Schema-drift canary: any new field on the route or any rename of an
    # existing field will fail validation here before silently dropping
    # data via FastAPI's response_model filter.
    parsed = RenameResponse.model_validate(body)
    assert parsed.disc_id == disc_id
    assert parsed.dry_run is True
    assert len(parsed.results) == 2
    for entry in parsed.results:
        assert entry.status == "preview"
        assert entry.changed is True
        assert entry.old_path != entry.new_path
        assert entry.new_path.endswith(".mkv")

    assert body["disc_id"] == disc_id
    assert body["dry_run"] is True
    assert len(body["results"]) == 2
    for entry in body["results"]:
        assert entry["status"] == "preview"
        assert entry["changed"] is True
        assert entry["old_path"] != entry["new_path"]
        assert entry["new_path"].endswith(".mkv")

    # Files untouched + DB untouched.
    for path in file_paths:
        assert os.path.exists(path)
    session = test_db()
    try:
        for title_id, original_path in zip(title_ids, file_paths):
            t = session.query(models.DiscTitle).filter_by(id=title_id).first()
            assert t.file_path == original_path
    finally:
        session.close()


def test_rename_execute_moves_files_and_updates_disc_title_file_path(client, test_db, tmp_path):
    """Execute mode moves bytes and persists the new path on each DiscTitle row."""
    session = test_db()
    try:
        disc_id, title_ids, old_paths = _seed_disc_with_titles(session, tmp_path)
    finally:
        session.close()

    resp = client.post(f"/releases/disc/{disc_id}/rename?dry_run=false")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is False

    new_paths_by_title = {e["title_id"]: e["new_path"] for e in body["results"]}
    for entry in body["results"]:
        assert entry["status"] == "renamed"

    # Old paths are gone, new paths exist.
    for old_path in old_paths:
        assert not os.path.exists(old_path), f"{old_path} should have been moved"
    for new_path in new_paths_by_title.values():
        assert os.path.exists(new_path), f"{new_path} should exist after rename"

    # DB reflects the new path.
    session = test_db()
    try:
        for title_id, new_path in new_paths_by_title.items():
            t = session.query(models.DiscTitle).filter_by(id=title_id).first()
            assert t.file_path == new_path
    finally:
        session.close()


def test_rename_preserves_file_path_stage(client, test_db, tmp_path):
    """Rename updates ``file_path`` but **not** ``file_path_stage``. The stage
    column tracks which *pipeline* stage last set the path (rip / postprocess
    / transfer); a user-driven rename is not a pipeline stage and must not
    clobber that marker."""
    session = test_db()
    try:
        disc_id, title_ids, _ = _seed_disc_with_titles(
            session, tmp_path, file_stage="transfer",
        )
    finally:
        session.close()

    resp = client.post(f"/releases/disc/{disc_id}/rename?dry_run=false")
    assert resp.status_code == 200

    session = test_db()
    try:
        for title_id in title_ids:
            t = session.query(models.DiscTitle).filter_by(id=title_id).first()
            assert t.file_path_stage == "transfer", (
                "rename must not change the stage marker"
            )
    finally:
        session.close()


def test_rename_idempotent_when_path_already_matches(client, test_db, tmp_path):
    """If a title's ``file_path`` is already at the expected target, the
    endpoint reports ``changed=False`` and does not move anything."""
    session = test_db()
    try:
        disc_id, title_ids, _ = _seed_disc_with_titles(session, tmp_path)
    finally:
        session.close()

    # First pass — actually rename.
    first = client.post(f"/releases/disc/{disc_id}/rename?dry_run=false").json()
    expected_new = {e["title_id"]: e["new_path"] for e in first["results"]}

    # Second pass — should be a no-op.
    second = client.post(f"/releases/disc/{disc_id}/rename?dry_run=false").json()
    for entry in second["results"]:
        assert entry["changed"] is False, entry
        assert entry["new_path"] == expected_new[entry["title_id"]]


# ──────────────────────────────────────────────────────────────────────────
# Partial-failure semantics (each title independently reports its outcome)
# ──────────────────────────────────────────────────────────────────────────


def test_rename_collision_at_destination_surfaces_per_title_error(client, test_db, tmp_path):
    """When the rename target already exists (and is not the source), the
    endpoint reports per-title ``status='collision'`` instead of overwriting
    or failing the whole batch."""
    session = test_db()
    try:
        disc_id, title_ids, _ = _seed_disc_with_titles(session, tmp_path)
    finally:
        session.close()

    # Discover what the new paths will be, then pre-occupy the first one.
    preview = client.post(f"/releases/disc/{disc_id}/rename?dry_run=true").json()
    target = preview["results"][0]["new_path"]
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as fh:
        fh.write(b"existing destination")

    resp = client.post(f"/releases/disc/{disc_id}/rename?dry_run=false").json()
    by_title = {e["title_id"]: e for e in resp["results"]}
    colliding = by_title[preview["results"][0]["title_id"]]
    assert colliding["status"] == "collision"
    assert "already exists" in colliding["error"].lower()

    # The other title in the batch must still rename successfully — partial
    # failure is per-title, not batch-fatal.
    other = by_title[preview["results"][1]["title_id"]]
    assert other["status"] == "renamed"


def test_rename_missing_source_file_surfaces_per_title_error(client, test_db, tmp_path):
    """When the source file is gone, the endpoint reports per-title
    ``status='missing'`` and leaves the other titles unaffected."""
    session = test_db()
    try:
        disc_id, title_ids, file_paths = _seed_disc_with_titles(session, tmp_path)
    finally:
        session.close()

    # Delete one source file to simulate a missing source.
    os.unlink(file_paths[0])

    resp = client.post(f"/releases/disc/{disc_id}/rename?dry_run=false").json()
    by_title = {e["title_id"]: e for e in resp["results"]}
    missing = by_title[title_ids[0]]
    assert missing["status"] == "missing"
    assert "not found" in missing["error"].lower()

    other = by_title[title_ids[1]]
    assert other["status"] == "renamed"


# ──────────────────────────────────────────────────────────────────────────
# HTTP-level error paths
# ──────────────────────────────────────────────────────────────────────────


def test_rename_returns_404_for_unknown_disc(client):
    """Unknown disc id → 404, no DB writes."""
    resp = client.post(f"/releases/disc/{uuid.uuid4()}/rename?dry_run=true")
    assert resp.status_code == 404


def test_rename_returns_400_for_disc_without_release(client, test_db):
    """A disc that has not been linked to a release cannot have an expected
    path computed — 400 with a clear error."""
    session = test_db()
    try:
        disc_id = str(uuid.uuid4())
        session.add(models.Disc(id=disc_id, content_hash="orphan-hash"))
        session.commit()
    finally:
        session.close()

    resp = client.post(f"/releases/disc/{disc_id}/rename?dry_run=true")
    assert resp.status_code == 400
    assert "release" in resp.json()["detail"].lower()


def test_rename_returns_409_when_transfer_in_progress(client, test_db, tmp_path):
    """A rename racing with an in-flight transfer would silently move bytes
    out from under the transfer worker. The endpoint must refuse with 409."""
    session = test_db()
    try:
        disc_id, _, _ = _seed_disc_with_titles(session, tmp_path)
        session.add(models.Job(
            id=str(uuid.uuid4()), disc_id=disc_id, disc_num="1",
            mount_point="/mnt/sr0",
            rip_state="completed",
            transfer_state="running",
        ))
        session.commit()
    finally:
        session.close()

    resp = client.post(f"/releases/disc/{disc_id}/rename?dry_run=false")
    assert resp.status_code == 409
    assert "transfer" in resp.json()["detail"].lower()


# ──────────────────────────────────────────────────────────────────────────
# Filtering rules
# ──────────────────────────────────────────────────────────────────────────


def test_rename_skips_titles_marked_ignore(client, test_db, tmp_path):
    """Titles marked ``type='ignore'`` are excluded from the rename — the user
    has already said they don't care about these files; renaming would only
    create unnecessary on-disk churn."""
    session = test_db()
    try:
        disc_id, title_ids, _ = _seed_disc_with_titles(session, tmp_path)
        # Mark the first title as ignored.
        t = session.query(models.DiscTitle).filter_by(id=title_ids[0]).first()
        t.type = "ignore"
        session.commit()
    finally:
        session.close()

    resp = client.post(f"/releases/disc/{disc_id}/rename?dry_run=true").json()
    returned = {e["title_id"] for e in resp["results"]}
    assert title_ids[0] not in returned
    assert title_ids[1] in returned


def test_rename_skips_titles_without_file_path(client, test_db, tmp_path):
    """Titles with ``file_path is None`` have no source to rename — they
    silently drop out of the results array."""
    session = test_db()
    try:
        disc_id, title_ids, _ = _seed_disc_with_titles(session, tmp_path)
        t = session.query(models.DiscTitle).filter_by(id=title_ids[0]).first()
        t.file_path = None
        session.commit()
    finally:
        session.close()

    resp = client.post(f"/releases/disc/{disc_id}/rename?dry_run=true").json()
    returned = {e["title_id"] for e in resp["results"]}
    assert title_ids[0] not in returned
    assert title_ids[1] in returned


# ──────────────────────────────────────────────────────────────────────────
# Unicode / encoding regression guard
# ──────────────────────────────────────────────────────────────────────────


def test_rename_with_unicode_title_metadata_succeeds(client, test_db, tmp_path):
    """Movie names with non-ASCII characters (e.g. ``è``) must round-trip
    through path computation, file move, and DB commit without
    UnicodeEncodeError. See ``Backend/api/database.py`` UTF-8 client_encoding
    comment for the historical regression that motivated this guard."""
    session = test_db()
    try:
        disc_id, title_ids, _ = _seed_disc_with_titles(
            session, tmp_path, movie_name="Amélie Poulain", production_year=2001,
        )
    finally:
        session.close()

    resp = client.post(f"/releases/disc/{disc_id}/rename?dry_run=false")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for entry in body["results"]:
        assert entry["status"] == "renamed"
        assert "Amélie" in entry["new_path"]
        assert os.path.exists(entry["new_path"])
