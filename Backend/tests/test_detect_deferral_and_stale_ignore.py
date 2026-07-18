"""#518 + #517 — detect deferral and stale auto-ignore healing.

#518 (Option B): the during-rip incremental preview dispatch must NOT chain
``detect_raw_titles`` — detection under rip I/O contention produced
false-positive junk verdicts that auto-ignored real episodes (Fallout S2,
2026-06-10: ~7 detect passes fired during the rip; an early one flagged a
52.8-min episode). Detection now runs once per job via the
post-rip-verification preview pass, which keeps ``chain_detect=True``.

#517 (folded in): when a detect re-run computes ``detection_warning=False``
for a title whose ``auto_type='ignore'`` came from a previous detection pass,
the stale ignore is cleared. Guards ensure user decisions, obfuscation-driven
ignores, m2ts subsumption, and demoted duplicate secondaries are untouched.
"""
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api import models
from workers import tasks


# ──────────────────────────────────────────────────────────────────────────
# #518 — chain_detect flag
# ──────────────────────────────────────────────────────────────────────────


def test_preview_chain_detect_false_does_not_enqueue_detect(monkeypatch):
    """During-rip dispatch shape: chain_detect=False → no detect enqueue."""
    detect_spy = MagicMock()
    monkeypatch.setattr(tasks, "run_preview_raw_titles_phase", lambda *a, **k: ["t1"])
    monkeypatch.setattr(tasks.detect_raw_titles, "delay", detect_spy)
    monkeypatch.setattr(tasks, "is_metadata_scan_disabled", lambda: False)
    monkeypatch.setattr(tasks, "is_detection_disabled", lambda: False)

    tasks.preview_raw_titles.run("job-1", ["t1"], None, chain_detect=False)

    detect_spy.assert_not_called()


def test_preview_chain_detect_default_enqueues_detect(monkeypatch):
    """Post-verification dispatch shape: default chain_detect → detect enqueued."""
    detect_spy = MagicMock()
    monkeypatch.setattr(tasks, "run_preview_raw_titles_phase", lambda *a, **k: ["t1", "t2"])
    monkeypatch.setattr(tasks.detect_raw_titles, "delay", detect_spy)
    monkeypatch.setattr(tasks, "is_metadata_scan_disabled", lambda: False)
    monkeypatch.setattr(tasks, "is_detection_disabled", lambda: False)

    tasks.preview_raw_titles.run("job-1", ["t1", "t2"], None)

    detect_spy.assert_called_once()
    args, _ = detect_spy.call_args
    assert args[0] == "job-1"
    assert sorted(args[1]) == ["t1", "t2"]


def test_during_rip_dispatch_passes_chain_detect_false():
    """Source-level guard: the per-title during-rip dispatch site must pass
    chain_detect=False. Cheap regression net against the kwarg being dropped
    in a refactor (the call site is deep inside the rip-progress parser and
    has no isolated seam to drive in a unit test)."""
    import inspect

    src = inspect.getsource(tasks)
    during_rip_idx = src.find("preview_tracks_enqueued.add(tk)")
    assert during_rip_idx != -1, "during-rip dispatch site moved — update this test"
    window = src[max(0, during_rip_idx - 1200):during_rip_idx]
    assert "chain_detect=False" in window, (
        "during-rip preview dispatch must pass chain_detect=False (#518)"
    )


# ──────────────────────────────────────────────────────────────────────────
# #517 — stale auto-ignore healing on clean re-run
# ──────────────────────────────────────────────────────────────────────────


class _FakeDetectResult:
    def __init__(self, confidence: float):
        self.confidence = confidence
        self.warnings = []

    def to_flags_dict(self):
        return {"bitrate_mbps": 69.7, "is_suspicious_bitrate": False}


def _seed_job_with_title(session, tmp_path, *, auto_type=None, user_type=None,
                         obfuscation_reason=None, subsumed_by=None, active=None):
    disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:8]}")
    session.add(disc)
    session.flush()
    title = models.DiscTitle(
        id=str(uuid.uuid4()),
        disc_id=disc.id,
        source_file="00803.mpls",
        segment_map="20",
        duration=3168.0,
        type=user_type if user_type is not None else auto_type,
        auto_type=auto_type,
        user_type=user_type,
        obfuscation_reason=obfuscation_reason,
        subsumed_by_title_id=subsumed_by,
        active=active,
        order_index=0,
    )
    session.add(title)
    session.flush()
    raw_rel = "ep3_t00.mkv"
    job = models.Job(
        disc_id=disc.id,
        disc_num="1",
        mount_point="/dev/sr0",
        job_status="running",
        rip_state="completed",
        ripped_files={str(title.id): raw_rel},
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    session.refresh(title)
    return job, title, raw_rel


def _run_detect(job, title, raw_rel, tmp_path, *, confidence: float):
    from workers.preview_detect_phases import run_detect_raw_titles_phase
    from core.job_paths import JobPaths

    paths = JobPaths.from_job(job, out_dir=str(tmp_path))
    paths.ensure_layout()
    (paths.raw / raw_rel).write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 64)

    # Production JobTask.set_status commits the session it's handed; the
    # phase's title mutations ride that commit (db_session() itself only
    # closes). Mirror that or the writes evaporate.
    task_self = MagicMock()
    task_self.set_status.side_effect = lambda _j, _db, **_kw: _db.commit()
    with patch("workers.preview_detect_phases.detect_padding_junk",
               return_value=_FakeDetectResult(confidence)), \
         patch("workers.preview_detect_phases.is_detection_disabled", return_value=False), \
         patch("workers.preview_detect_phases.is_metadata_scan_disabled", return_value=True), \
         patch("workers.tasks.DATA_ROOT", Path(tmp_path)):
        run_detect_raw_titles_phase(task_self, str(job.id), [str(title.id)], None)


def test_clean_rerun_clears_stale_detection_auto_ignore(test_db, tmp_path):
    """auto_type='ignore' from a prior detect pass + clean re-run → cleared."""
    session = test_db()
    try:
        job, title, rel = _seed_job_with_title(
            session, tmp_path, auto_type="ignore", active=True,
        )
        _run_detect(job, title, rel, tmp_path, confidence=0.0)
        session.expire_all()
        row = session.get(models.DiscTitle, title.id)
        assert row.detection_warning is False
        assert row.auto_type is None, (
            f"stale detection auto-ignore must be cleared on clean re-run; got {row.auto_type!r}"
        )
        assert row.type is None
    finally:
        session.close()


def test_clean_rerun_respects_user_type(test_db, tmp_path):
    session = test_db()
    try:
        job, title, rel = _seed_job_with_title(
            session, tmp_path, auto_type="ignore", user_type="Episode", active=True,
        )
        _run_detect(job, title, rel, tmp_path, confidence=0.0)
        session.expire_all()
        row = session.get(models.DiscTitle, title.id)
        # user_type set → guard skips; auto_type untouched, effective type stays user's.
        assert row.auto_type == "ignore"
        assert row.user_type == "Episode"
        assert row.type == "Episode"
    finally:
        session.close()


def test_clean_rerun_respects_obfuscation_reason(test_db, tmp_path):
    session = test_db()
    try:
        job, title, rel = _seed_job_with_title(
            session, tmp_path, auto_type="ignore",
            obfuscation_reason="makemkv_msg3307", active=True,
        )
        _run_detect(job, title, rel, tmp_path, confidence=0.0)
        session.expire_all()
        row = session.get(models.DiscTitle, title.id)
        assert row.auto_type == "ignore", "obfuscation-driven ignore must survive a clean detect"
    finally:
        session.close()


def test_clean_rerun_respects_demoted_secondary(test_db, tmp_path):
    session = test_db()
    try:
        job, title, rel = _seed_job_with_title(
            session, tmp_path, auto_type="ignore", active=False,
        )
        _run_detect(job, title, rel, tmp_path, confidence=0.0)
        session.expire_all()
        row = session.get(models.DiscTitle, title.id)
        assert row.auto_type == "ignore", "demoted duplicate secondary must stay ignored"
    finally:
        session.close()


def test_warning_rerun_still_sets_ignore(test_db, tmp_path):
    """Existing behavior preserved: a positive verdict still auto-ignores an untyped title."""
    from workers.preview_detect_phases import FFMPEG_DETECTION_CONFIDENCE_THRESHOLD

    session = test_db()
    try:
        job, title, rel = _seed_job_with_title(session, tmp_path, auto_type=None, active=True)
        _run_detect(job, title, rel, tmp_path,
                    confidence=FFMPEG_DETECTION_CONFIDENCE_THRESHOLD + 0.01)
        session.expire_all()
        row = session.get(models.DiscTitle, title.id)
        assert row.detection_warning is True
        assert row.auto_type == "ignore"
    finally:
        session.close()
