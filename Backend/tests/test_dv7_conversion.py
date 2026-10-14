"""#894: Dolby Vision 7 -> 8.1 conversion — settings, library catalog,
conversion task, postprocess hook and API.

The conversion engine itself (core.dv_remux) is covered by test_dv_remux.py;
here it is faked so these tests exercise the orchestration on real files.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from api import models
from core import library_catalog as lc
from core import library_destination as ld


@pytest.fixture(autouse=True)
def tiny_test_videos_count(monkeypatch):
    """The fixtures' fake videos are a few bytes; the real floor is tested below."""
    monkeypatch.setattr(lc, "MIN_VIDEO_BYTES", 0)


@pytest.fixture(autouse=True)
def library_is_tmp_library(monkeypatch, tmp_path):
    """Conversions resolve the library from the transfer destination (#902);
    here it is a local destination at ``tmp_path / "library"``."""
    root = str(tmp_path / "library")
    monkeypatch.setattr(ld, "ensure_root", lambda db: ld.LibraryDestination(True, "local", "Library", root, root))


@pytest.fixture
def settings_file(monkeypatch, tmp_path):
    from core import settings
    monkeypatch.setattr(settings, "_SETTINGS_FILE", tmp_path / "settings.json")
    return settings


def _video(folder: Path, name: str, content: bytes = b"x" * 100, age: float = 3600) -> Path:
    p = folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def _probe_by_name(path: Path):
    if "broken" in path.name:
        return None
    return {
        "video_codec": "hevc", "width": 3840, "height": 2160,
        "dv_profile": 7 if "dv7" in path.name else (8 if "dv8" in path.name else None),
        "duration": 7200.0,
    }


# ── settings ────────────────────────────────────────────────────────────────

def test_everything_is_off_by_default(settings_file):
    assert settings_file.get_dv7_conversion() == {
        "enabled": False, "convert_new_rips": False, "last_scan": None,
    }


def test_a_1_7_0_library_paths_setting_is_ignored(settings_file):
    settings_file.save_settings({"dv7_conversion": {"enabled": True, "library_paths": ["/dv7-test"]}})
    assert "library_paths" not in settings_file.get_dv7_conversion()
    assert settings_file.get_dv7_conversion()["enabled"] is True


def test_last_scan_record_replaces_the_previous_one(settings_file):
    settings_file.record_dv7_last_scan(at="t1", ok=False, destination="smb://h/s", problem="Couldn't mount")
    settings_file.record_dv7_last_scan(at="t2", ok=True, destination="smb://h/s", files=10, dv7=1, queued=1)
    last = settings_file.get_dv7_conversion()["last_scan"]
    assert last["at"] == "t2" and last["ok"] is True and last["problem"] is None and last["files"] == 10


# ── scan ───────────────────────────────────────────────────────────────────

def test_scan_catalogs_formats_and_queues_only_dv7_mkv(test_db, tmp_path):
    lib = tmp_path / "library"
    _video(lib, "Movies/A (2020)/A dv7.mkv")
    _video(lib, "Movies/B (2021)/B dv8.mkv")
    _video(lib, "Movies/C (2022)/C.mp4")
    _video(lib, "TV/S01/E01 dv7.mp4")              # DV7 but not MKV: catalogued, not queued
    _video(lib, "Movies/D/D broken.mkv")
    _video(lib, "Movies/E/E dv7 recent.mkv", age=10)  # still being written
    _video(lib, ".dv7remux-x/out dv7.mkv")         # an in-flight work dir
    (lib / "notes.txt").write_text("not a video")
    db = test_db()
    try:
        s = lc.scan_library(db, [str(lib)], probe=_probe_by_name)
        rows = {Path(r.path).name: r for r in db.query(models.LibraryMediaFile).all()}
        assert set(rows) == {"A dv7.mkv", "B dv8.mkv", "C.mp4", "E01 dv7.mp4", "D broken.mkv"}
        assert rows["A dv7.mkv"].status == lc.QUEUED and rows["A dv7.mkv"].dv_profile == 7
        assert rows["B dv8.mkv"].status == lc.CATALOGUED
        assert rows["E01 dv7.mp4"].status == lc.CATALOGUED
        assert rows["D broken.mkv"].status == lc.UNREADABLE
        assert rows["D broken.mkv"].error == lc.UNREADABLE_NOTE
        assert s["unreadable"] == 1 and s["errors"] == []   # a damaged file isn't a scan error
        assert s["queued_ids"] == [rows["A dv7.mkv"].id]
        assert s["recent"] == 1 and s["probed"] == 5
    finally:
        db.close()


def test_rescan_only_reprobes_changed_files_and_drops_deleted_ones(test_db, tmp_path):
    lib = tmp_path / "library"
    a = _video(lib, "A dv7.mkv")
    b = _video(lib, "B dv8.mkv")
    db = test_db()
    probes = []

    def counting_probe(p):
        probes.append(p.name)
        return _probe_by_name(p)
    try:
        lc.scan_library(db, [str(lib)], probe=counting_probe)
        probes.clear()
        s = lc.scan_library(db, [str(lib)], probe=counting_probe)
        assert probes == [] and s["unchanged"] == 2
        assert len(s["queued_ids"]) == 1            # still queued, re-dispatched
        b.unlink()
        _video(lib, "A dv7.mkv", content=b"y" * 200)  # changed size
        s = lc.scan_library(db, [str(lib)], probe=counting_probe)
        assert probes == ["A dv7.mkv"] and s["removed"] == 1
    finally:
        db.close()


def test_a_1_7_0_unreadable_failure_is_reprobed_as_unreadable(test_db, tmp_path):
    lib = tmp_path / "library"
    _video(lib, "D broken.mkv")
    db = test_db()
    try:
        lc.scan_library(db, [str(lib)], probe=_probe_by_name)
        row = db.query(models.LibraryMediaFile).one()
        row.status, row.error = lc.FAILED, "Could not read this file's format (ffprobe failed)."
        db.commit()
        s = lc.scan_library(db, [str(lib)], probe=_probe_by_name)
        db.refresh(row)
        assert row.status == lc.UNREADABLE and s["probed"] == 1 and s["unreadable"] == 1
        s = lc.scan_library(db, [str(lib)], probe=_probe_by_name)
        assert s["probed"] == 0 and s["unreadable"] == 1         # counted, not re-probed
    finally:
        db.close()


def test_failed_file_is_not_requeued_by_a_rescan(test_db, tmp_path):
    lib = tmp_path / "library"
    _video(lib, "A dv7.mkv")
    db = test_db()
    try:
        lc.scan_library(db, [str(lib)], probe=_probe_by_name)
        row = db.query(models.LibraryMediaFile).one()
        row.status = lc.FAILED
        db.commit()
        s = lc.scan_library(db, [str(lib)], probe=_probe_by_name)
        assert s["queued_ids"] == [] and db.query(models.LibraryMediaFile).one().status == lc.FAILED
    finally:
        db.close()


def test_interrupted_conversion_is_marked_failed_after_the_stale_window(test_db, tmp_path):
    lib = tmp_path / "library"
    _video(lib, "A dv7.mkv")
    db = test_db()
    try:
        lc.scan_library(db, [str(lib)], probe=_probe_by_name)
        row = db.query(models.LibraryMediaFile).one()
        row.status = lc.CONVERTING
        row.updated_at = datetime.now(timezone.utc) - timedelta(hours=10)
        db.commit()
        s = lc.scan_library(db, [str(lib)], probe=_probe_by_name)
        row = db.query(models.LibraryMediaFile).one()
        assert s["interrupted"] == 1 and row.status == lc.FAILED and "Interrupted" in row.error
    finally:
        db.close()


def test_missing_folder_is_reported_not_scanned(test_db, tmp_path):
    db = test_db()
    try:
        s = lc.scan_library(db, [str(tmp_path / "nope")], probe=_probe_by_name)
        assert s["roots"][0]["ok"] is False and "doesn't exist" in s["roots"][0]["problem"]
    finally:
        db.close()


# ── conversion task ────────────────────────────────────────────────────────

def _queued_row(db, lib):
    _video(lib, "A dv7.mkv", content=b"o" * 1000)
    lc.scan_library(db, [str(lib)], probe=_probe_by_name)
    return db.query(models.LibraryMediaFile).one()


def _cfg(lib, enabled=True):
    return lambda: {"enabled": enabled, "convert_new_rips": True, "last_scan": None}


def _fake_remux(status="converted", reason=None, new_content=b"n" * 600):
    calls = []

    def remux(path, progress_cb=None, **kw):
        calls.append(path)
        remux.kwargs = kw
        if progress_cb:
            progress_cb(0.5)
        if status == "converted":
            Path(path).write_bytes(new_content)
        return SimpleNamespace(status=status, reason=reason, converted=status == "converted")
    remux.calls = calls
    return remux


def test_convert_success_updates_the_catalog(test_db, tmp_path):
    lib = tmp_path / "library"
    db = test_db()
    try:
        row = _queued_row(db, lib)
        remux = _fake_remux()
        assert lc.convert_catalog_file(db, row.id, remux=remux, settings_getter=_cfg(lib)) == lc.CONVERTED
        db.refresh(row)
        assert row.status == lc.CONVERTED and row.dv_profile == 8 and row.size == 600
        assert row.progress == 1.0 and row.converted_at is not None
    finally:
        db.close()


@pytest.mark.parametrize("status, expected", [("failed", lc.FAILED), ("skipped", lc.SKIPPED)])
def test_convert_failure_keeps_the_reason(test_db, tmp_path, status, expected):
    lib = tmp_path / "library"
    db = test_db()
    try:
        row = _queued_row(db, lib)
        remux = _fake_remux(status=status, reason="not enough free space")
        assert lc.convert_catalog_file(db, row.id, remux=remux, settings_getter=_cfg(lib)) == expected
        db.refresh(row)
        assert row.error == "not enough free space" and row.progress is None
        assert Path(row.path).read_bytes() == b"o" * 1000
    finally:
        db.close()


def test_convert_does_nothing_when_off_or_not_queued(test_db, tmp_path):
    lib = tmp_path / "library"
    db = test_db()
    try:
        row = _queued_row(db, lib)
        remux = _fake_remux()
        assert lc.convert_catalog_file(db, row.id, remux=remux, settings_getter=_cfg(lib, enabled=False)) == "disabled"
        row.status = lc.CONVERTED
        db.commit()
        assert lc.convert_catalog_file(db, row.id, remux=remux, settings_getter=_cfg(lib)).startswith("not queued")
        assert remux.calls == []
    finally:
        db.close()


def test_convert_skips_a_file_that_changed_or_left_the_library(test_db, tmp_path):
    lib = tmp_path / "library"
    db = test_db()
    try:
        row = _queued_row(db, lib)
        _video(lib, "A dv7.mkv", content=b"z" * 50)
        assert lc.convert_catalog_file(db, row.id, remux=_fake_remux(), settings_getter=_cfg(lib)) == lc.SKIPPED
        assert "changed after it was scanned" in db.query(models.LibraryMediaFile).one().error

        row.status = lc.QUEUED
        db.commit()
        elsewhere = str(tmp_path / "elsewhere")
        moved = lambda _db: ld.LibraryDestination(True, "local", "Other", elsewhere, elsewhere)
        assert lc.convert_catalog_file(
            db, row.id, remux=_fake_remux(), settings_getter=_cfg(lib), destination=moved,
        ) == lc.SKIPPED
        assert "transfer destination changed" in db.query(models.LibraryMediaFile).one().error

        row.status = lc.QUEUED
        db.commit()
        down = lambda _db: ld.LibraryDestination(False, "smb", "Plex", "smb://h/s", problem="Couldn't mount smb://h/s")
        assert lc.convert_catalog_file(
            db, row.id, remux=_fake_remux(), settings_getter=_cfg(lib), destination=down,
        ) == lc.SKIPPED
        assert db.query(models.LibraryMediaFile).one().error == "Couldn't mount smb://h/s"
    finally:
        db.close()


def test_retry_requeues_only_failed_dv7(test_db, tmp_path):
    lib = tmp_path / "library"
    db = test_db()
    try:
        row = _queued_row(db, lib)
        assert lc.retry_catalog_file(db, row.id) is None      # queued, not failed
        row.status = lc.FAILED
        row.error = "boom"
        db.commit()
        assert lc.retry_catalog_file(db, row.id) == row.id
        db.refresh(row)
        assert row.status == lc.QUEUED and row.error is None
    finally:
        db.close()


# ── postprocess hook ───────────────────────────────────────────────────────

def test_postprocess_hook_converts_and_records_the_new_hash(test_db, tmp_path, monkeypatch, settings_file):
    from core import dv_remux
    from workers.tasks import _maybe_convert_dv7_outputs

    settings_file.set_dv7_conversion(enabled=True, convert_new_rips=True)
    disc = models.Disc(id="d-894", content_hash="h-894")
    db = test_db()
    try:
        db.add(disc)
        title = models.DiscTitle(id="t-1", disc_id=disc.id, index=0, mkv_size=1000, type="movie")
        db.add(title)
        db.commit()
        root = tmp_path / "out"
        _video(root, "Movie/Movie.mkv", content=b"o" * 1000)
        _video(root, "Movie/Extra.mkv", content=b"e" * 10)
        monkeypatch.setattr(dv_remux, "needs_dv7_remux", lambda p: p.name == "Movie.mkv")
        monkeypatch.setattr(dv_remux, "remux_dv7_to_dv81", _fake_remux(new_content=b"n" * 600))
        task = SimpleNamespace(add_log=lambda *a, **k: None)
        job = SimpleNamespace(id="job-1")
        hashes = _maybe_convert_dv7_outputs(task, job, db, root, {"t-1": "Movie/Movie.mkv", "t-2": "Movie/Extra.mkv"})
        import hashlib
        assert hashes == {"t-1": hashlib.sha256(b"n" * 600).hexdigest()}
        db.refresh(title)
        assert title.mkv_size == 600
    finally:
        db.close()


def test_postprocess_hook_is_off_unless_enabled_with_new_rips(test_db, tmp_path, monkeypatch, settings_file):
    from core import dv_remux
    from workers.tasks import _maybe_convert_dv7_outputs

    monkeypatch.setattr(dv_remux, "needs_dv7_remux", lambda p: (_ for _ in ()).throw(AssertionError("probed")))
    task = SimpleNamespace(add_log=lambda *a, **k: None)
    job = SimpleNamespace(id="job-1")
    assert _maybe_convert_dv7_outputs(task, job, None, tmp_path, {"t": "a.mkv"}) == {}
    settings_file.set_dv7_conversion(enabled=True)          # new rips stay off by default
    assert _maybe_convert_dv7_outputs(task, job, None, tmp_path, {"t": "a.mkv"}) == {}


# ── API ────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(test_db, settings_file, monkeypatch):
    from fastapi.testclient import TestClient
    from api.routers import dv7
    dispatched = []
    monkeypatch.setattr(dv7, "_dispatch_scan", lambda: dispatched.append(True))
    from api.main import app
    c = TestClient(app)
    c.dispatched = dispatched
    return c


def _active_local_destination(test_db, folder):
    folder.mkdir(parents=True, exist_ok=True)
    db = test_db()
    try:
        db.add(models.TransferConfig(mode="local", name="Library", is_active=True, transfer_dir=str(folder)))
        db.commit()
    finally:
        db.close()


def test_api_config_shows_the_transfer_destination(client, test_db, tmp_path):
    r = client.get("/system/dv7/config")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False and "library_paths" not in body and "folders" not in body
    assert body["destination"]["available"] is False
    assert body["destination"]["problem"] == ld.NO_DESTINATION

    lib = tmp_path / "media"
    _active_local_destination(test_db, lib)
    dest = client.get("/system/dv7/config").json()["destination"]
    assert dest == {"available": True, "mode": "local", "name": "Library", "display": str(lib), "problem": None}


def test_api_turning_on_scans_only_a_reachable_destination(client, test_db, tmp_path):
    r = client.put("/system/dv7/config", json={"enabled": True})
    assert r.status_code == 200 and client.dispatched == []       # no destination yet
    client.put("/system/dv7/config", json={"enabled": False})

    _active_local_destination(test_db, tmp_path / "media")
    r = client.put("/system/dv7/config", json={"enabled": True})
    assert r.status_code == 200 and client.dispatched == [True]


def test_api_scan_requires_enabled_and_a_destination(client, test_db, tmp_path):
    assert client.post("/system/dv7/scan").status_code == 409
    client.put("/system/dv7/config", json={"enabled": True})
    r = client.post("/system/dv7/scan")
    assert r.status_code == 409 and r.json()["detail"] == ld.NO_DESTINATION
    _active_local_destination(test_db, tmp_path / "media")
    assert client.post("/system/dv7/scan").status_code == 200


def test_api_file_list_includes_unreadable_files(client, test_db):
    db = test_db()
    try:
        db.add_all([
            models.LibraryMediaFile(path="/l/a.mkv", library_root="/l", dv_profile=7, status=lc.QUEUED),
            models.LibraryMediaFile(path="/l/b.mp4", library_root="/l", status=lc.UNREADABLE, error=lc.UNREADABLE_NOTE),
            models.LibraryMediaFile(path="/l/c.mkv", library_root="/l", dv_profile=None, status=lc.CATALOGUED),
        ])
        db.commit()
    finally:
        db.close()
    paths = {f["path"]: f["status"] for f in client.get("/system/dv7/library/files").json()}
    assert paths == {"/l/a.mkv": "queued", "/l/b.mp4": "unreadable"}


def test_api_file_list_filters_by_several_statuses_and_dv_profile(client, test_db):
    db = test_db()
    try:
        db.add_all([
            models.LibraryMediaFile(path="/l/q.mkv", library_root="/l", dv_profile=7, status=lc.QUEUED),
            models.LibraryMediaFile(path="/l/c.mkv", library_root="/l", dv_profile=7, status=lc.CONVERTING),
            models.LibraryMediaFile(path="/l/f.mkv", library_root="/l", dv_profile=7, status=lc.FAILED),
            models.LibraryMediaFile(path="/l/h.mkv", library_root="/l", dv_profile=None, status=lc.CATALOGUED),
        ])
        db.commit()
    finally:
        db.close()
    get = lambda qs: sorted(f["path"] for f in client.get(f"/system/dv7/library/files?{qs}").json())
    assert get("status=queued,converting&dv7_only=false") == ["/l/c.mkv", "/l/q.mkv"]
    assert get("dv_profile=7&dv7_only=false") == ["/l/c.mkv", "/l/f.mkv", "/l/q.mkv"]
    assert get("status=catalogued&dv7_only=false") == ["/l/h.mkv"]
    assert get("dv7_only=false") == ["/l/c.mkv", "/l/f.mkv", "/l/h.mkv", "/l/q.mkv"]


def test_api_ignores_a_library_paths_update(client):
    r = client.put("/system/dv7/config", json={"library_paths": ["/anything"]})
    assert r.status_code == 200 and "library_paths" not in r.json()


# ── #895 / #896: space reservations and local scratch ──────────────────────

def test_waiting_for_space_keeps_the_file_queued(test_db, tmp_path):
    lib = tmp_path / "library"
    db = test_db()
    try:
        row = _queued_row(db, lib)
        remux = _fake_remux()
        out = lc.convert_catalog_file(
            db, row.id, remux=remux, settings_getter=_cfg(lib),
            planner=lambda *a: (None, "this needs 2.0 GB"),
        )
        db.refresh(row)
        assert out == lc.WAITING_FOR_SPACE and row.status == lc.QUEUED
        assert row.error.startswith(lc.WAITING_PREFIX) and remux.calls == []
    finally:
        db.close()


def test_network_file_converts_in_local_scratch_and_releases_the_space(test_db, tmp_path, monkeypatch):
    from core import space_reservations as sr
    import core.storage_location as sl
    lib = tmp_path / "library"
    monkeypatch.setattr(sl, "is_network_path", lambda p: True)
    monkeypatch.setenv("MKVAUTO_DV7_SCRATCH_DIR", str(tmp_path / "scratch"))
    db = test_db()
    try:
        row = _queued_row(db, lib)
        seen = {}

        def remux(path, progress_cb=None, **kw):
            seen.update(kw)
            seen["reservations"] = sorted(r.kind for r in db.query(models.SpaceReservation).all())
            Path(path).write_bytes(b"n" * 600)
            return SimpleNamespace(status="converted", reason=None, converted=True)

        assert lc.convert_catalog_file(db, row.id, remux=remux, settings_getter=_cfg(lib)) == lc.CONVERTED
        assert seen["scratch_dir"] == str(tmp_path / "scratch") and seen["check_space"] is False
        assert seen["reservations"] == [sr.KIND_DV7_PUSH, sr.KIND_DV7_SCRATCH]
        assert db.query(models.SpaceReservation).count() == 0     # released afterwards
    finally:
        db.close()


def test_local_file_converts_in_place(test_db, tmp_path, monkeypatch):
    import core.storage_location as sl
    lib = tmp_path / "library"
    monkeypatch.setattr(sl, "is_network_path", lambda p: False)
    db = test_db()
    try:
        row = _queued_row(db, lib)
        remux = _fake_remux()
        assert lc.convert_catalog_file(db, row.id, remux=remux, settings_getter=_cfg(lib)) == lc.CONVERTED
        assert remux.kwargs["scratch_dir"] is None
    finally:
        db.close()


def test_postprocess_conversion_is_skipped_when_space_cant_be_reserved(test_db, tmp_path, monkeypatch, settings_file):
    from core import dv_remux, space_reservations as sr
    from workers.tasks import _maybe_convert_dv7_outputs

    settings_file.set_dv7_conversion(enabled=True, convert_new_rips=True)
    root = tmp_path / "out"
    _video(root, "Movie/Movie.mkv", content=b"o" * 1000)
    monkeypatch.setattr(dv_remux, "needs_dv7_remux", lambda p: True)
    called = []
    monkeypatch.setattr(dv_remux, "remux_dv7_to_dv81", lambda *a, **k: called.append(1))
    monkeypatch.setattr(sr, "try_reserve", lambda *a, **k: SimpleNamespace(granted=False, detail="Not enough disk space."))
    notes = []
    task = SimpleNamespace(add_log=lambda job, db, msg: notes.append(msg))
    db = test_db()
    try:
        disc = models.Disc(content_hash="h-space")
        db.add(disc)
        db.flush()
        db.add(models.DiscTitle(id="t-1", disc_id=disc.id, index=0, type="movie"))
        db.commit()
        assert _maybe_convert_dv7_outputs(task, SimpleNamespace(id="job-1"), db, root, {"t-1": "Movie/Movie.mkv"}) == {}
        assert called == [] and any("transfer as Dolby Vision 7" in n for n in notes)
    finally:
        db.close()


# ── #902: the scan task follows the destination and records its outcome ────

def test_scan_task_records_an_unreachable_destination(test_db, settings_file, monkeypatch):
    import contextlib
    from workers import tasks

    settings_file.set_dv7_conversion(enabled=True)
    monkeypatch.setattr(tasks, "db_session", lambda: contextlib.nullcontext(test_db()))
    monkeypatch.setattr(ld, "ensure_root", lambda db: ld.LibraryDestination(
        False, "smb", "Plex", "smb://nas/PLEX Media", problem="Couldn't mount //nas/PLEX Media: Host is down"))
    out = tasks.library_scan.run()
    assert out == {"skipped": "Couldn't mount //nas/PLEX Media: Host is down"}
    last = settings_file.get_dv7_conversion()["last_scan"]
    assert last["ok"] is False and last["destination"] == "smb://nas/PLEX Media"
    assert last["problem"].startswith("Couldn't mount")


def test_scan_task_scans_the_destination_and_records_counts(test_db, settings_file, monkeypatch, tmp_path):
    import contextlib
    from workers import tasks

    lib = tmp_path / "library"
    _video(lib, "Movies/A (2020)/A dv7.mkv")
    _video(lib, "Movies/B (2021)/B dv8.mkv")
    settings_file.set_dv7_conversion(enabled=True)
    monkeypatch.setattr(tasks, "db_session", lambda: contextlib.nullcontext(test_db()))
    from core import dv_remux
    monkeypatch.setattr(dv_remux, "probe_media_info", _probe_by_name)
    queued = []
    monkeypatch.setattr(tasks.convert_library_file, "apply_async", lambda args: queued.append(args[0]))
    out = tasks.library_scan.run()
    assert out["files"] == 2 and out["queued"] == 1 and len(queued) == 1
    last = settings_file.get_dv7_conversion()["last_scan"]
    assert last["ok"] is True and last["files"] == 2 and last["dv7"] == 1 and last["queued"] == 1
    assert last["destination"] == str(lib)



# ── #902: size floor, progress and interrupted scans ───────────────────────

def test_tiny_files_are_not_catalogued_and_old_rows_for_them_go(test_db, tmp_path, monkeypatch):
    monkeypatch.setattr(lc, "MIN_VIDEO_BYTES", 1000)
    lib = tmp_path / "library"
    _video(lib, "Books/app/node_modules/types/index.ts", content=b"x" * 50)   # TypeScript
    _video(lib, "Movies/A dv7.mkv", content=b"x" * 5000)
    db = test_db()
    try:
        db.add(models.LibraryMediaFile(path=str(lib / "Books/app/node_modules/types/index.ts"),
                                       library_root=str(lib.resolve()), size=50, status=lc.FAILED))
        db.commit()
        s = lc.scan_library(db, [str(lib)], probe=_probe_by_name)
        paths = [Path(r.path).name for r in db.query(models.LibraryMediaFile).all()]
        assert paths == ["A dv7.mkv"] and s["files"] == 1 and s["removed"] == 1
    finally:
        db.close()


def test_scan_reports_progress(test_db, tmp_path, monkeypatch):
    monkeypatch.setattr(lc, "PROGRESS_EVERY_SECONDS", 0)
    lib = tmp_path / "library"
    for i in range(3):
        _video(lib, f"M{i}.mkv")
    seen = []
    db = test_db()
    try:
        lc.scan_library(db, [str(lib)], probe=_probe_by_name, progress_cb=seen.append)
    finally:
        db.close()
    assert seen == [1, 2, 3]


def test_a_running_scan_that_went_silent_reads_as_interrupted(settings_file):
    from datetime import datetime, timedelta, timezone as tz

    old = (datetime.now(tz.utc) - timedelta(seconds=settings_file.SCAN_STALE_SECONDS + 60)).isoformat()
    settings_file.record_dv7_last_scan(at=old, ok=None, destination="smb://h/s", heartbeat=old)
    last = settings_file.get_dv7_conversion()["last_scan"]
    assert last["ok"] is False and last["problem"] == settings_file.SCAN_INTERRUPTED

    fresh = datetime.now(tz.utc).isoformat()
    settings_file.record_dv7_last_scan(at=old, ok=None, destination="smb://h/s", files=40, heartbeat=fresh)
    last = settings_file.get_dv7_conversion()["last_scan"]
    assert last["ok"] is None and last["files"] == 40


def test_postprocess_converts_only_labeled_non_ignored_titles(test_db, tmp_path, monkeypatch, settings_file):
    from core import dv_remux
    from workers.tasks import _maybe_convert_dv7_outputs

    settings_file.set_dv7_conversion(enabled=True, convert_new_rips=True)
    db = test_db()
    try:
        disc = models.Disc(content_hash="h-labeled")
        db.add(disc)
        db.flush()
        ids = {}
        for name, title_type in (("movie", "movie"), ("ignored", "ignore"), ("unlabeled", None)):
            t = models.DiscTitle(disc_id=disc.id, index=len(ids), type=title_type)
            db.add(t)
            db.flush()
            ids[name] = t.id
            (tmp_path / f"{name}.mkv").write_bytes(b"x")
        db.commit()
        checked = []
        monkeypatch.setattr(dv_remux, "needs_dv7_remux", lambda p: checked.append(p.name) or False)
        task = SimpleNamespace(add_log=lambda *a, **k: None)
        _maybe_convert_dv7_outputs(task, SimpleNamespace(id="job-1"), db, tmp_path,
                                   {ids[n]: f"{n}.mkv" for n in ids})
        assert checked == ["movie.mkv"]
    finally:
        db.close()


def test_typescript_ts_files_are_not_videos(test_db, tmp_path, monkeypatch):
    monkeypatch.setattr(lc, "MIN_VIDEO_BYTES", 10)
    lib = tmp_path / "library"
    code = _video(lib, "Books/app/src/index.ts", content=b"export const x = 1;\n" * 20)
    stream = _video(lib, "Recordings/show.ts", content=b"\x47" + b"\x00" * 400)
    db = test_db()
    try:
        db.add(models.LibraryMediaFile(path=str(code), library_root=str(lib.resolve()), size=400,
                                       status=lc.FAILED))
        db.commit()
        s = lc.scan_library(db, [str(lib)], probe=lambda p: {"video_codec": "mpeg2video"})
        names = [Path(r.path).name for r in db.query(models.LibraryMediaFile).all()]
        assert names == ["show.ts"] and s["files"] == 1 and s["unreadable"] == 0 and s["removed"] == 1
    finally:
        db.close()


def test_scan_problem_is_one_short_sentence(test_db, settings_file, monkeypatch, tmp_path):
    import contextlib
    from workers import tasks

    settings_file.set_dv7_conversion(enabled=True)
    (tmp_path / "library" / "Movies").mkdir(parents=True)
    monkeypatch.setattr(tasks, "db_session", lambda: contextlib.nullcontext(test_db()))
    errors = [f"/lib/f{i}.mkv: Permission denied" for i in range(500)]
    monkeypatch.setattr(lc, "scan_library", lambda db, roots, **k: {
        "roots": [{"path": roots[0], "ok": True, "problem": None}], "files": 600, "probed": 600,
        "unchanged": 0, "recent": 0, "removed": 0, "dv7": 0, "unreadable": 0, "queued_ids": [],
        "errors": errors, "interrupted": 0})
    tasks.library_scan.run()
    last = settings_file.get_dv7_conversion()["last_scan"]
    assert last["problem"] == "500 files couldn't be read from the share; details are in the app log."
    assert last["ok"] is True          # the scan finished; only some files had problems



# ── only the folders MKV-Auto writes ──────────────────────────────────────

def test_scan_folders_are_movies_series_and_a_fixed_template_folder(settings_file, tmp_path):
    root = tmp_path / "share"
    for name in ("Movies", "Series", "Books", "Kids", "Music"):
        (root / name).mkdir(parents=True)
    assert [Path(p).name for p in lc.scan_folders(str(root))] == ["Movies", "Series"]
    settings_file.save_settings({"path_template_movie": "Kids/{movie} ({year})/{title}.mkv",
                                 "path_template_series": "{type_dir}/{movie}/{title}.mkv"})
    assert [Path(p).name for p in lc.scan_folders(str(root))] == ["Movies", "Series", "Kids"]


def test_a_whole_share_catalog_is_narrowed_to_the_app_folders(test_db, tmp_path):
    """1.7.0-rc.2 catalogued the whole share with the share as the root; the
    next scan keeps the Movies rows (re-keyed, no duplicate path) and drops
    everything else."""
    share = tmp_path / "share"
    movie = _video(share, "Movies/A (2020)/A dv7.mkv")
    book = _video(share, "Books/course/video1.mp4")
    db = test_db()
    try:
        lc.scan_library(db, [str(share)], probe=_probe_by_name)          # the old whole-share scan
        assert db.query(models.LibraryMediaFile).count() == 2
        s = lc.scan_library(db, lc.scan_folders(str(share)), probe=_probe_by_name)
        rows = db.query(models.LibraryMediaFile).all()
        assert [r.path for r in rows] == [str(movie.resolve())]
        assert rows[0].library_root == str((share / "Movies").resolve())
        assert s["removed"] == 1 and s["unchanged"] == 1 and len(s["queued_ids"]) == 1
    finally:
        db.close()
