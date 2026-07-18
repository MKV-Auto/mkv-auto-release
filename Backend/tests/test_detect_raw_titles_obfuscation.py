"""
Integration tests for #374: post-ffprobe duration-sanity obfuscation
detection inside run_detect_raw_titles_phase (via the detect_raw_titles
task), using a stub raw file and a mocked scan_file_metadata.

Covers the acceptance criterion "integration test using a stub preview
output": declared-short / actual-long titles get obfuscation_flag=True,
obfuscation_reason='duration_short', and a corrected duration — while
pre-existing stronger reasons and sub-threshold titles are untouched.
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from api import crud, models
from core.ffprobe_metadata import MetadataScanResult
from workers import tasks
from workers.tasks import detect_raw_titles


pytestmark = pytest.mark.integration


def _fake_scan_result(duration: float, bit_rate: int = 20_000_000) -> MetadataScanResult:
    return MetadataScanResult(
        format={"duration": duration, "size": 1_634_304, "bit_rate": bit_rate},
        stream_counts={"video": 1, "audio": 1, "subtitle": 0},
        chapters_count=1,
        attachments_count=0,
        video_hints={"codec_name": "hevc", "width": 3840, "height": 2160},
        audio_summary=[],
        subtitle_summary=[],
        warning=None,
    )


def _make_job_with_title(
    test_db,
    tmp_path,
    title_id: str,
    declared_duration: int,
    obfuscation_reason: str | None = None,
) -> str:
    with test_db() as session:
        disc = models.Disc(
            id=str(uuid.uuid4()), content_hash=f"h374-{title_id}", disc_number=1
        )
        title = models.DiscTitle(
            id=title_id,
            disc_id=disc.id,
            source_file="00001.mpls",
            title="Title 1",
            duration=declared_duration,
            obfuscation_flag=bool(obfuscation_reason),
            obfuscation_reason=obfuscation_reason,
        )
        session.add_all([disc, title])
        session.commit()
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            job_status="running",
            scan_state="completed",
            rip_state="completed",
            disc_payload={},
        )
        session.add(job)
        session.flush()
        job_id = str(job.id)
        raw_dir = tmp_path / "data" / job_id / "raw"
        raw_dir.mkdir(parents=True)
        (raw_dir / "t.mkv").write_bytes(b"x" * 1024)
        session.commit()
    return job_id


def _run_detect(job_id: str, title_id: str, actual_duration: float) -> None:
    with (
        patch("workers.preview_detect_phases.is_detection_disabled", return_value=True),
        patch("workers.preview_detect_phases.is_metadata_scan_disabled", return_value=False),
        patch(
            "workers.preview_detect_phases.scan_file_metadata",
            return_value=_fake_scan_result(actual_duration),
        ),
    ):
        detect_raw_titles.apply(
            args=[job_id, [title_id]],
            kwargs={"rel_path_overrides": {title_id: "raw/t.mkv"}},
        )


def test_duration_short_flags_and_corrects_duration(test_db, tmp_path, monkeypatch):
    """Midway repro: declared 10s, ffprobe 120s → duration_short fires,
    duration corrected, metadata_results recorded in disc_payload."""
    monkeypatch.setattr(tasks, "DATA_ROOT", tmp_path / "data")
    title_id = "37401"
    job_id = _make_job_with_title(test_db, tmp_path, title_id, declared_duration=10)

    _run_detect(job_id, title_id, actual_duration=120.0)

    with test_db() as session:
        tit = session.query(models.DiscTitle).filter(models.DiscTitle.id == title_id).first()
        assert tit is not None
        assert tit.obfuscation_flag is True
        assert tit.obfuscation_reason == "duration_short"
        assert tit.duration == 120, "declared duration must be corrected from ffprobe"
        job_row = crud.get_job(session, job_id)
        payload = job_row.disc_payload or {}
        assert title_id in (payload.get("metadata_results") or {})


def test_existing_stronger_reason_is_preserved(test_db, tmp_path, monkeypatch):
    """A pre-existing relational reason (segment_set_sibling) must not be
    overwritten by the post-ffprobe reason; flag stays set and duration is
    still corrected."""
    monkeypatch.setattr(tasks, "DATA_ROOT", tmp_path / "data")
    title_id = "37402"
    job_id = _make_job_with_title(
        test_db,
        tmp_path,
        title_id,
        declared_duration=10,
        obfuscation_reason="segment_set_sibling",
    )

    _run_detect(job_id, title_id, actual_duration=120.0)

    with test_db() as session:
        tit = session.query(models.DiscTitle).filter(models.DiscTitle.id == title_id).first()
        assert tit is not None
        assert tit.obfuscation_reason == "segment_set_sibling", (
            "relational reasons carry context post-ffprobe reasons can't; "
            "they must not be downgraded (#374)"
        )
        assert tit.obfuscation_flag is True
        assert tit.duration == 120


def test_sub_threshold_duration_left_untouched(test_db, tmp_path, monkeypatch):
    """Ratio below 1.5x (100s declared vs 120s actual) must not flag and must
    not rewrite the declared duration."""
    monkeypatch.setattr(tasks, "DATA_ROOT", tmp_path / "data")
    title_id = "37403"
    job_id = _make_job_with_title(test_db, tmp_path, title_id, declared_duration=100)

    _run_detect(job_id, title_id, actual_duration=120.0)

    with test_db() as session:
        tit = session.query(models.DiscTitle).filter(models.DiscTitle.id == title_id).first()
        assert tit is not None
        assert not tit.obfuscation_flag
        assert tit.obfuscation_reason is None
        assert tit.duration == 100, "sub-threshold titles keep their declared duration"
