"""Tests for preview auto-recovery helpers and regeneration state."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.job_paths import JobPaths
from core.preview_recovery import (
    active_generate_previews_job_ids,
    build_preview_regeneration_state,
    user_reset_preview_auto_recovery_metadata,
)
from workers.tasks import _safe_track_folder


def test_user_reset_preview_auto_recovery_metadata():
    p = {"status": "failed", "auto_recovery_attempts": 3, "auto_recovery_last_error": "x"}
    user_reset_preview_auto_recovery_metadata(p)
    assert p["auto_recovery_attempts"] == 0
    assert "auto_recovery_last_error" not in p


@pytest.mark.xfail(reason="staging baseline fail; tracked in #418", strict=True)
def test_active_generate_previews_job_ids(monkeypatch):
    class _Insp:
        def active(self):
            return {
                "w1": [
                    {"name": "workers.tasks.generate_previews", "args": ["job-aaa"]},
                ]
            }

        def reserved(self):
            return {
                "w2": [
                    {"name": "mkv.generate_previews", "args": ["job-bbb"]},
                ]
            }

    class _Ctl:
        def inspect(self):
            return _Insp()

    class _App:
        control = _Ctl()

    monkeypatch.setattr("core.preview_recovery.celery_app", _App())
    ids = active_generate_previews_job_ids()
    assert "job-aaa" in ids
    assert "job-bbb" in ids


@pytest.mark.xfail(reason="staging baseline fail; tracked in #418", strict=True)
def test_build_preview_regeneration_state_respects_existing_manifests(tmp_path, monkeypatch):
    jobs_root = tmp_path / "jobs"
    monkeypatch.setattr("core.utils.resolve_jobs_root", lambda _od=None: jobs_root)
    jid = "job-manifest-test"
    jp = JobPaths(jobs_root, jid)
    jp.ensure_layout()

    tid_done = "11111111-1111-1111-1111-111111111111"
    tid_missing = "22222222-2222-2222-2222-222222222222"
    safe_done = _safe_track_folder(tid_done)
    man_dir = jp.previews / safe_done
    man_dir.mkdir(parents=True, exist_ok=True)
    (man_dir / "preview.m3u8").write_text("#EXTM3U\nsegment_000.ts\n", encoding="utf-8")

    job = SimpleNamespace(
        id=jid,
        post_paths={tid_done: "a.mkv", tid_missing: "b.mkv"},
        ripped_files={},
        disc_payload={
            "previews": {
                "tracks": {
                    tid_done: {"status": "queued"},
                    tid_missing: {"status": "queued"},
                }
            }
        },
        disc=None,
    )
    db = MagicMock()
    tracks, regen, overall = build_preview_regeneration_state(job, db)

    assert tid_done in tracks
    assert tracks[tid_done]["status"] == "completed"
    assert tid_missing in tracks
    assert tracks[tid_missing]["status"] == "queued"
    assert regen == [tid_missing]
    assert overall in ("running", "queued")


def _job_with_pending_track(jobs_root, jid, tid, source):
    JobPaths(jobs_root, jid).ensure_layout()
    return SimpleNamespace(
        id=jid,
        post_paths={tid: source},
        ripped_files={},
        disc_payload={"previews": {"tracks": {tid: {"status": "queued"}}}},
        disc=None,
    )


def test_build_preview_regeneration_state_fails_tracks_whose_source_is_gone(tmp_path, monkeypatch):
    """Raw output cleaned up after transfer: nothing to encode from, so the
    track is terminal-failed instead of re-queued forever (2026-09-12 prod:
    thousands of ffmpeg "No such file" runs on finished jobs)."""
    jobs_root = tmp_path / "jobs"
    monkeypatch.setenv("MKVAUTO_JOBS_DIR", str(jobs_root))
    tid = "33333333-3333-3333-3333-333333333333"
    job = _job_with_pending_track(jobs_root, "job-src-gone", tid, "Movies/gone.mkv")

    tracks, regen, overall = build_preview_regeneration_state(job, MagicMock())

    assert regen == []
    assert tracks[tid]["status"] == "failed"
    assert tracks[tid]["retryable"] is False
    assert "gone.mkv" in tracks[tid]["error"]
    assert overall == "failed"


def test_build_preview_regeneration_state_requeues_when_source_exists(tmp_path, monkeypatch):
    jobs_root = tmp_path / "jobs"
    monkeypatch.setenv("MKVAUTO_JOBS_DIR", str(jobs_root))
    tid = "44444444-4444-4444-4444-444444444444"
    job = _job_with_pending_track(jobs_root, "job-src-present", tid, "Movies/here.mkv")
    src = JobPaths(jobs_root, "job-src-present").raw / "Movies" / "here.mkv"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"\x1a\x45\xdf\xa3")

    tracks, regen, overall = build_preview_regeneration_state(job, MagicMock())

    assert regen == [tid]
    assert tracks[tid]["status"] == "queued"
    assert overall == "running"


def test_build_preview_regeneration_state_fails_all_when_raw_dir_is_gone(tmp_path, monkeypatch):
    jobs_root = tmp_path / "jobs"
    monkeypatch.setenv("MKVAUTO_JOBS_DIR", str(jobs_root))
    tid = "55555555-5555-5555-5555-555555555555"
    job = _job_with_pending_track(jobs_root, "job-raw-gone", tid, None)
    raw = JobPaths(jobs_root, "job-raw-gone").raw
    raw.rmdir()

    tracks, regen, overall = build_preview_regeneration_state(job, MagicMock())

    assert regen == []
    assert tracks[tid]["status"] == "failed"
    assert "raw directory missing" in tracks[tid]["error"]
    assert overall == "failed"
