"""
#449 — coverage for ``POST /releases/library/reattach``.

The endpoint walks a TransferConfig's ``transfer_dir``, identifies MKVs
by Matroska Segment UID (deterministic) or filename (heuristic
fallback), and re-attaches them to ``DiscTitle.file_path``. Self-heals
the "imported a DB export onto a fresh install" and "moved files in
Plex" cases without re-rip.

These tests drive the endpoint via an on-disk synthetic library +
``read_segment_uid`` monkeypatched to return a predictable map from
file path → UID. The real binary integration is covered by the manual
smoke-test in the issue.
"""
import os
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api import database, models
from api.main import app
from core import mkv_identity


@pytest.fixture
def client(test_db):
    """TestClient wired to the test DB. Overrides both the global ``get_db``
    and the releases router's local ``get_db`` (it defines its own)."""
    from api.routers import releases as releases_router

    def override_get_db():
        session = test_db()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[database.get_db] = override_get_db
    if hasattr(releases_router, "get_db"):
        app.dependency_overrides[releases_router.get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed_disc_with_titles(session, *, n_titles=2, segment_uids=None):
    """Disc + N titles. ``segment_uids`` is an optional list parallel to the
    titles — None entries mean leave the column NULL (legacy row). Returns
    ``(disc_id, [title_ids])``."""
    disc_id = str(uuid.uuid4())
    session.add(models.Disc(id=disc_id, content_hash=f"h-{uuid.uuid4().hex[:8]}"))
    tids = []
    for i in range(n_titles):
        tid = str(uuid.uuid4())
        kwargs = dict(
            id=tid, disc_id=disc_id,
            title=f"T{i}", source_file=f"0000{i}.mpls",
        )
        if segment_uids and segment_uids[i]:
            kwargs["segment_uid"] = segment_uids[i]
        session.add(models.DiscTitle(**kwargs))
        tids.append(tid)
    session.commit()
    return disc_id, tids


def _make_local_config(session, transfer_dir: str):
    """Active local-mode TransferConfig pointing at ``transfer_dir``."""
    cfg = models.TransferConfig(
        id=str(uuid.uuid4()),
        name="reattach-test",
        mode="local",
        transfer_dir=transfer_dir,
        is_active=True,
        config_data={"transfer_dir": transfer_dir},
    )
    session.add(cfg)
    session.commit()
    session.refresh(cfg)
    return str(cfg.id)


def _put_mkv(library: Path, rel: str) -> Path:
    """Create a dummy MKV file at ``library/rel``. Returns the absolute path."""
    p = library / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x1a\x45\xdf\xa3" * 16)  # EBML magic header for realism
    return p


def _uid_lookup(file_to_uid):
    """Build a fake ``read_segment_uid`` that returns the mapped UID per
    absolute path, or None for unknown files."""
    norm = {os.path.realpath(str(k)): v for k, v in file_to_uid.items()}
    def fake(abs_path):
        return norm.get(os.path.realpath(str(abs_path)))
    return fake


# ────────────────────────────────────────────────────────────────────────
# Primary match path
# ────────────────────────────────────────────────────────────────────────


def test_deterministic_match_via_segment_uid(client, test_db, tmp_path, monkeypatch):
    """A title with segment_uid set + an on-disk MKV with the same UID →
    one deterministic match, no heuristic match, no orphans."""
    library = tmp_path / "library"
    library.mkdir()

    session = test_db()
    try:
        disc_id, [tid_a, tid_b] = _seed_disc_with_titles(
            session, segment_uids=[
                "a" * 32,
                "b" * 32,
            ],
        )
        _make_local_config(session, str(library))
    finally:
        session.close()

    file_a = _put_mkv(library, "Movies/Film A (2024)/Film A.1080p.mkv")
    file_b = _put_mkv(library, "Movies/Film B (2024)/Film B.1080p.mkv")
    monkeypatch.setattr(
        mkv_identity, "read_segment_uid",
        _uid_lookup({file_a: "a" * 32, file_b: "b" * 32}),
    )

    resp = client.post("/releases/library/reattach?dry_run=true")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert {m["title_id"] for m in body["deterministic_matches"]} == {tid_a, tid_b}
    assert body["heuristic_matches"] == []
    assert body["orphan_files"] == []
    assert body["orphan_titles"] == []
    assert body["conflicts"] == []
    assert body["dry_run"] is True
    assert body["applied"] is False
    # Tier reported correctly.
    assert all(m["tier"] == "segment_uid" for m in body["deterministic_matches"])


def test_wet_run_writes_file_path_via_update_title_file_paths(
    client, test_db, tmp_path, monkeypatch
):
    """``dry_run=false`` actually applies the matches: DiscTitle.file_path
    populated with the absolute on-disk path, file_path_stage='transfer'.
    Mirrors the wipe-and-reimport recovery flow."""
    library = tmp_path / "library"
    library.mkdir()

    session = test_db()
    try:
        disc_id, [tid] = _seed_disc_with_titles(
            session, n_titles=1, segment_uids=["c" * 32],
        )
        _make_local_config(session, str(library))
    finally:
        session.close()

    file_path = _put_mkv(library, "Movies/Test.mkv")
    monkeypatch.setattr(
        mkv_identity, "read_segment_uid",
        _uid_lookup({file_path: "c" * 32}),
    )

    resp = client.post("/releases/library/reattach?dry_run=false")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert len(body["deterministic_matches"]) == 1

    # DB now has DiscTitle.file_path pointing at the absolute path.
    session = test_db()
    try:
        title = session.query(models.DiscTitle).filter_by(id=tid).first()
        assert title.file_path == str(file_path)
        assert title.file_path_stage == "transfer"
    finally:
        session.close()


# ────────────────────────────────────────────────────────────────────────
# Heuristic fallback (segment_uid IS NULL — legacy rows from before #451)
# ────────────────────────────────────────────────────────────────────────


def test_heuristic_match_by_filename_when_segment_uid_null(
    client, test_db, tmp_path, monkeypatch
):
    """Legacy DB rows produced before PR #451 don't have segment_uid set.
    Filename match against source_file falls back deterministically when
    the file basenames are unique."""
    library = tmp_path / "library"
    library.mkdir()

    session = test_db()
    try:
        # Both titles: segment_uid=None (legacy). Override source_file to
        # the .mkv names the endpoint actually walks (rglob *.mkv).
        disc_id = str(uuid.uuid4())
        session.add(models.Disc(id=disc_id, content_hash=f"h-{uuid.uuid4().hex[:8]}"))
        tid_a = str(uuid.uuid4())
        tid_b = str(uuid.uuid4())
        session.add(models.DiscTitle(
            id=tid_a, disc_id=disc_id, title="T0", source_file="Film A.mkv",
        ))
        session.add(models.DiscTitle(
            id=tid_b, disc_id=disc_id, title="T1", source_file="Film B.mkv",
        ))
        session.commit()
        _make_local_config(session, str(library))
    finally:
        session.close()

    # On-disk filenames match the titles' source_file values exactly.
    _put_mkv(library, "Movies/A/Film A.mkv")
    _put_mkv(library, "Movies/B/Film B.mkv")
    # read_segment_uid returns None for both (legacy artifact, no UID in
    # container). The heuristic tier should activate.
    monkeypatch.setattr(mkv_identity, "read_segment_uid", lambda _p: None)

    resp = client.post("/releases/library/reattach?dry_run=true")
    body = resp.json()

    assert body["deterministic_matches"] == []
    assert {m["title_id"] for m in body["heuristic_matches"]} == {tid_a, tid_b}
    assert all(m["tier"] == "filename" for m in body["heuristic_matches"])


def test_heuristic_match_uses_existing_file_path_basename(
    client, test_db, tmp_path, monkeypatch
):
    """Heuristic fallback also matches when source_file doesn't align but
    a previously-recorded ``DiscTitle.file_path`` does — covers the
    "moved files in Plex" case for legacy titles."""
    library = tmp_path / "library"
    library.mkdir()

    session = test_db()
    try:
        disc_id, [tid] = _seed_disc_with_titles(
            session, n_titles=1, segment_uids=[None],
        )
        # Pretend an older run wrote a file_path; the file has since moved.
        title = session.query(models.DiscTitle).filter_by(id=tid).first()
        title.file_path = "/old/location/Film A.1080p.mkv"
        title.file_path_stage = "transfer"
        session.commit()
        _make_local_config(session, str(library))
    finally:
        session.close()

    # File at the new location, basename matches the old file_path basename.
    new_path = _put_mkv(library, "Movies/A/Film A.1080p.mkv")
    monkeypatch.setattr(mkv_identity, "read_segment_uid", lambda _p: None)

    resp = client.post("/releases/library/reattach?dry_run=true")
    body = resp.json()

    assert len(body["heuristic_matches"]) == 1
    assert body["heuristic_matches"][0]["title_id"] == tid
    assert body["heuristic_matches"][0]["tier"] == "filename"
    assert body["heuristic_matches"][0]["old_path"] == "/old/location/Film A.1080p.mkv"
    assert body["heuristic_matches"][0]["new_path"] == str(new_path)


# ────────────────────────────────────────────────────────────────────────
# Conflict + orphan reporting
# ────────────────────────────────────────────────────────────────────────


def test_conflict_when_filename_matches_multiple_titles(
    client, test_db, tmp_path, monkeypatch
):
    """If a single on-disk file's basename matches two titles' source_file
    (e.g. two different discs ripped to the same filename), it lands in
    ``conflicts`` and neither title is reattached — the operator
    disambiguates.

    Titles must live on different discs because the schema has
    UNIQUE(disc_id, source_file) — the realistic scenario is also two
    discs (a re-rip of the same source on a fresh job)."""
    library = tmp_path / "library"
    library.mkdir()

    session = test_db()
    try:
        # Two discs, each with one title whose source_file is "DUPLICATE.mkv".
        disc_a = str(uuid.uuid4())
        disc_b = str(uuid.uuid4())
        session.add(models.Disc(id=disc_a, content_hash=f"h-{uuid.uuid4().hex[:8]}"))
        session.add(models.Disc(id=disc_b, content_hash=f"h-{uuid.uuid4().hex[:8]}"))
        tid_a = str(uuid.uuid4())
        tid_b = str(uuid.uuid4())
        session.add(models.DiscTitle(
            id=tid_a, disc_id=disc_a, title="T", source_file="DUPLICATE.mkv",
        ))
        session.add(models.DiscTitle(
            id=tid_b, disc_id=disc_b, title="T", source_file="DUPLICATE.mkv",
        ))
        session.commit()
        _make_local_config(session, str(library))
    finally:
        session.close()

    on_disk = _put_mkv(library, "Movies/X/DUPLICATE.mkv")
    monkeypatch.setattr(mkv_identity, "read_segment_uid", lambda _p: None)

    resp = client.post("/releases/library/reattach?dry_run=true")
    body = resp.json()

    assert body["heuristic_matches"] == []
    assert body["deterministic_matches"] == []
    assert len(body["conflicts"]) == 1
    conflict = body["conflicts"][0]
    assert conflict["file_path"] == str(on_disk)
    assert set(conflict["candidate_title_ids"]) == {tid_a, tid_b}
    assert conflict["tier"] == "filename"


def test_orphan_files_and_titles_reported(client, test_db, tmp_path, monkeypatch):
    """Files at the destination without a matching title → ``orphan_files``.
    Titles without an on-disk match → ``orphan_titles``."""
    library = tmp_path / "library"
    library.mkdir()

    session = test_db()
    try:
        disc_id, [tid] = _seed_disc_with_titles(
            session, n_titles=1, segment_uids=["d" * 32],
        )
        _make_local_config(session, str(library))
    finally:
        session.close()

    # An unrelated file with a UID that nothing in the DB claims.
    orphan_file = _put_mkv(library, "Movies/Mystery/Unknown.mkv")
    monkeypatch.setattr(
        mkv_identity, "read_segment_uid",
        _uid_lookup({orphan_file: "z" * 32}),
    )

    resp = client.post("/releases/library/reattach?dry_run=true")
    body = resp.json()

    assert body["orphan_files"] == [str(orphan_file)]
    assert body["orphan_titles"] == [tid]
    assert body["deterministic_matches"] == []
    assert body["heuristic_matches"] == []


# ────────────────────────────────────────────────────────────────────────
# HTTP-level guards
# ────────────────────────────────────────────────────────────────────────


def test_returns_400_when_no_active_config(client, test_db, tmp_path):
    """No active TransferConfig → 400 with a helpful hint."""
    resp = client.post("/releases/library/reattach?dry_run=true")
    assert resp.status_code == 400
    assert "no active transferconfig" in resp.text.lower()


def test_returns_400_when_active_config_is_remote_mode(client, test_db, tmp_path):
    """SMB / NFS / rsync configs can't be walked locally — return 400 with
    the "mount locally" hint, per the issue's Phase 3 deferral."""
    session = test_db()
    try:
        cfg = models.TransferConfig(
            id=str(uuid.uuid4()), name="smb-test", mode="smb",
            transfer_dir="", is_active=True, config_data={},
        )
        session.add(cfg)
        session.commit()
    finally:
        session.close()

    resp = client.post("/releases/library/reattach?dry_run=true")
    assert resp.status_code == 400
    assert "local-mode" in resp.text


def test_returns_400_when_transfer_dir_does_not_exist(client, test_db, tmp_path):
    """Active config's transfer_dir is set but the path doesn't exist on
    disk → 400. (Common if the volume isn't mounted at request time.)"""
    session = test_db()
    try:
        _make_local_config(session, str(tmp_path / "does" / "not" / "exist"))
    finally:
        session.close()

    resp = client.post("/releases/library/reattach?dry_run=true")
    assert resp.status_code == 400
    assert "not found on disk" in resp.text.lower()


def test_returns_404_when_explicit_config_id_unknown(client, test_db, tmp_path):
    """transfer_config_id query param given but no row matches → 404."""
    bogus = str(uuid.uuid4())
    resp = client.post(f"/releases/library/reattach?transfer_config_id={bogus}")
    assert resp.status_code == 404
    assert "not found" in resp.text.lower()


# ────────────────────────────────────────────────────────────────────────
# End-to-end: wipe-and-reimport scenario
# ────────────────────────────────────────────────────────────────────────


def test_wipe_and_reimport_round_trip(client, test_db, tmp_path, monkeypatch):
    """The canonical use case from the issue: dump DB, drop schema,
    re-import (or seed fresh from external metadata), call reattach
    against the same destination → every previously-tagged title comes
    back attached deterministically by segment_uid."""
    library = tmp_path / "library"
    library.mkdir()

    # Imagine these came from a fresh DB import — titles have segment_uid
    # but no file_path yet (just like after a reimport that knows the UIDs
    # from the dump but doesn't know the current on-disk paths).
    titles_data = [
        ("a" * 32, "Movies/Film A/Film A.1080p.mkv"),
        ("b" * 32, "Movies/Film B/Film B.1080p.mkv"),
        ("c" * 32, "Series/Show/S01E01.mkv"),
    ]

    session = test_db()
    try:
        disc_id = str(uuid.uuid4())
        session.add(models.Disc(id=disc_id, content_hash=f"h-{uuid.uuid4().hex[:8]}"))
        title_uid_paths = {}
        for uid, rel in titles_data:
            tid = str(uuid.uuid4())
            session.add(models.DiscTitle(
                id=tid, disc_id=disc_id,
                title=os.path.basename(rel), source_file=os.path.basename(rel),
                segment_uid=uid,
                # file_path: deliberately None — this is the post-reimport
                # state we're recovering from.
            ))
            title_uid_paths[uid] = (tid, rel)
        session.commit()
        _make_local_config(session, str(library))
    finally:
        session.close()

    # Place files at the destination + wire the UID lookup.
    file_to_uid = {}
    for uid, (_tid, rel) in title_uid_paths.items():
        p = _put_mkv(library, rel)
        file_to_uid[p] = uid
    monkeypatch.setattr(mkv_identity, "read_segment_uid", _uid_lookup(file_to_uid))

    # Wet-run: apply the matches.
    resp = client.post("/releases/library/reattach?dry_run=false")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert len(body["deterministic_matches"]) == 3
    assert body["heuristic_matches"] == []
    assert body["orphan_files"] == []
    assert body["orphan_titles"] == []
    assert body["applied"] is True

    # Every title has file_path populated.
    session = test_db()
    try:
        for uid, (tid, rel) in title_uid_paths.items():
            title = session.query(models.DiscTitle).filter_by(id=tid).first()
            assert title.file_path is not None
            assert title.file_path.endswith(rel)
            assert title.file_path_stage == "transfer"
    finally:
        session.close()


def test_dry_run_does_not_write_to_db(client, test_db, tmp_path, monkeypatch):
    """``dry_run=true`` produces the same report but leaves
    DiscTitle.file_path untouched. Operator preview before commit."""
    library = tmp_path / "library"
    library.mkdir()

    session = test_db()
    try:
        disc_id, [tid] = _seed_disc_with_titles(
            session, n_titles=1, segment_uids=["e" * 32],
        )
        _make_local_config(session, str(library))
    finally:
        session.close()

    file_path = _put_mkv(library, "Movies/Test.mkv")
    monkeypatch.setattr(
        mkv_identity, "read_segment_uid",
        _uid_lookup({file_path: "e" * 32}),
    )

    resp = client.post("/releases/library/reattach?dry_run=true")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["deterministic_matches"]) == 1
    assert body["applied"] is False

    # DB still has file_path=None — no write occurred.
    session = test_db()
    try:
        title = session.query(models.DiscTitle).filter_by(id=tid).first()
        assert title.file_path is None
    finally:
        session.close()
