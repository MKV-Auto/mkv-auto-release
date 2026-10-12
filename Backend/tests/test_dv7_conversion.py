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
        "enabled": False, "convert_new_rips": False, "library_paths": [],
    }


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
        assert rows["D broken.mkv"].status == lc.FAILED
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
    return lambda: {"enabled": enabled, "convert_new_rips": True, "library_paths": [str(lib)]}


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
        other = lambda: {"enabled": True, "convert_new_rips": True, "library_paths": [str(tmp_path / "elsewhere")]}
        assert lc.convert_catalog_file(db, row.id, remux=_fake_remux(), settings_getter=other) == lc.SKIPPED
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
        title = models.DiscTitle(id="t-1", disc_id=disc.id, index=0, mkv_size=1000)
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


def test_api_config_round_trip_and_folder_validation(client, tmp_path):
    r = client.get("/system/dv7/config")
    assert r.status_code == 200 and r.json()["enabled"] is False

    r = client.put("/system/dv7/config", json={"library_paths": [str(tmp_path / "missing")]})
    assert r.status_code == 400 and "doesn't exist" in r.json()["detail"]

    lib = tmp_path / "lib"
    lib.mkdir()
    r = client.put("/system/dv7/config", json={"library_paths": [str(lib)]})
    assert r.status_code == 200 and r.json()["folders"] == [{"path": str(lib), "ok": True, "problem": None}]
    assert client.dispatched == []                         # still off: no scan

    r = client.put("/system/dv7/config", json={"enabled": True})
    assert r.status_code == 200 and client.dispatched == [True]


def test_api_scan_requires_enabled_and_a_folder(client, tmp_path):
    assert client.post("/system/dv7/scan").status_code == 409
    client.put("/system/dv7/config", json={"enabled": True})
    assert client.post("/system/dv7/scan").status_code == 409   # no folder yet


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
        assert _maybe_convert_dv7_outputs(task, SimpleNamespace(id="job-1"), db, root, {"t-1": "Movie/Movie.mkv"}) == {}
        assert called == [] and any("transfer as Dolby Vision 7" in n for n in notes)
    finally:
        db.close()
