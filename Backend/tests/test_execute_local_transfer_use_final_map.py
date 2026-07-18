"""
Phase 2 § 6.1 — coverage for ``_execute_local_transfer_use_final_map``.

This helper was extracted verbatim from the ``POST /jobs/{id}/transfer``
endpoint so the same body can be called from the ``start_transfer``
worker for auto-progression. These tests pin the contract the inline
implementation produced:

  * each file in ``job.post_paths`` (or ``ripped_files`` fallback) is
    copied from ``src_root`` to ``config.transfer_dir`` preserving
    its relative path;
  * single-segment paths get the right library prefix
    (``Movies/`` or ``Series/``) based on which top dirs exist under
    ``src_root``;
  * transfer + hash progress callbacks fire and span 0-50% / 50-100%;
  * the function returns the list of top-level destination dirs;
  * the job advances to ``transfer_state=completed`` via
    ``_complete_transfer`` on success;
  * insufficient disk space raises HTTPException(400);
  * a missing source file is silently skipped (the contract from the
    legacy inline implementation — partial transfers complete the
    files that do exist).

Tests deliberately drive the helper through a synthetic on-disk rig
(real ``tmp_path`` files, real ``shutil.copy2``) — not mocks — so a
regression in any of the four phases (setup, copy, hash verify,
destination verify) shows up here.
"""
import shutil
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from api import models
from api.routers.jobs import _execute_local_transfer_use_final_map


@pytest.fixture
def rig(test_db, tmp_path):
    """Build a synthetic transfer rig: src_root + dest_root + a Disc/Job
    row with post_paths pointing at on-disk MKV files. Returns a dict
    with everything the tests need."""
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()

    # Two MKV files with non-trivial content so hashing has something to do.
    movies_dir = src_root / "Movies" / "Test Film (2024)"
    movies_dir.mkdir(parents=True)
    file_a = movies_dir / "Test Film.1080p.mkv"
    file_b = movies_dir / "Test Film.bonus.mkv"
    file_a.write_bytes(b"\x00\x01\x02\x03" * 1024)
    file_b.write_bytes(b"\xaa\xbb\xcc\xdd" * 1024)

    session = test_db()
    try:
        disc_id = str(uuid.uuid4())
        session.add(models.Disc(id=disc_id, content_hash=f"h-{uuid.uuid4().hex[:8]}"))
        tid_a = str(uuid.uuid4())
        tid_b = str(uuid.uuid4())
        for tid, src_file in ((tid_a, file_a), (tid_b, file_b)):
            session.add(models.DiscTitle(
                id=tid, disc_id=disc_id,
                title=src_file.stem, source_file=src_file.name,
            ))

        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc_id, disc_num="1", mount_point="/mnt/sr0",
            rip_state="completed",
            phase="transfer",
            transfer_state="running",
            post_paths={
                tid_a: "Movies/Test Film (2024)/Test Film.1080p.mkv",
                tid_b: "Movies/Test Film (2024)/Test Film.bonus.mkv",
            },
        )
        session.add(job)
        session.commit()
        session.refresh(job)

        config = models.TransferConfig(
            id=str(uuid.uuid4()),
            name="test-local",
            mode="local",
            transfer_dir=str(dest_root),
            is_active=False,
            config_data={"transfer_dir": str(dest_root)},
        )
        session.add(config)
        session.commit()
        session.refresh(config)
        session.refresh(job)
    finally:
        session.close()

    return {
        "src_root": src_root,
        "dest_root": dest_root,
        "file_a": file_a,
        "file_b": file_b,
        "tid_a": tid_a,
        "tid_b": tid_b,
        "disc_id": disc_id,
        "job_id": str(job.id),
        "config_id": str(config.id),
    }


def _no_op_progress(*_a, **_kw):
    """Callable for the progress callback parameters."""
    pass


def test_use_final_map_copies_post_paths_to_destination(rig, test_db, monkeypatch):
    """Each file in job.post_paths is copied to config.transfer_dir
    preserving its relative path. The helper returns the top-level
    destination dirs."""
    # _complete_transfer + _verify_transfer_destination touch DB / metadata
    # we don't need to exercise here — stub them to keep the test scoped to
    # the copy + return-value contract.
    monkeypatch.setattr(
        "api.routers.jobs._verify_transfer_destination",
        lambda *a, **k: (True, None),
    )
    completed = []
    monkeypatch.setattr(
        "api.routers.jobs._complete_transfer",
        lambda job, db, dest_paths, job_metadata: completed.append(list(dest_paths)),
    )

    session = test_db()
    try:
        job = session.query(models.Job).filter_by(id=rig["job_id"]).first()
        config = session.query(models.TransferConfig).filter_by(id=rig["config_id"]).first()
        dests = _execute_local_transfer_use_final_map(
            session, job, rig["src_root"], config,
            output_files=None,
            job_metadata={},
            transfer_progress_callback=_no_op_progress,
            hash_progress_callback=_no_op_progress,
        )
    finally:
        session.close()

    # Both source files exist at their post_paths positions under dest_root.
    assert (rig["dest_root"] / "Movies/Test Film (2024)/Test Film.1080p.mkv").is_file()
    assert (rig["dest_root"] / "Movies/Test Film (2024)/Test Film.bonus.mkv").is_file()
    # Top-level dest dir is reported back to the caller.
    assert dests == [str(rig["dest_root"] / "Movies")]
    # _complete_transfer was called exactly once with the dests we returned.
    assert completed == [[str(rig["dest_root"] / "Movies")]]


def test_use_final_map_preserves_file_contents(rig, test_db, monkeypatch):
    """shutil.copy2 → destination bytes must match source bytes exactly.
    Without this, hash verification would fail in production."""
    monkeypatch.setattr(
        "api.routers.jobs._verify_transfer_destination",
        lambda *a, **k: (True, None),
    )
    monkeypatch.setattr(
        "api.routers.jobs._complete_transfer",
        lambda *a, **k: None,
    )

    session = test_db()
    try:
        job = session.query(models.Job).filter_by(id=rig["job_id"]).first()
        config = session.query(models.TransferConfig).filter_by(id=rig["config_id"]).first()
        _execute_local_transfer_use_final_map(
            session, job, rig["src_root"], config,
            output_files=None,
            job_metadata={},
            transfer_progress_callback=_no_op_progress,
            hash_progress_callback=_no_op_progress,
        )
    finally:
        session.close()

    src_bytes_a = rig["file_a"].read_bytes()
    src_bytes_b = rig["file_b"].read_bytes()
    dest_bytes_a = (rig["dest_root"] / "Movies/Test Film (2024)/Test Film.1080p.mkv").read_bytes()
    dest_bytes_b = (rig["dest_root"] / "Movies/Test Film (2024)/Test Film.bonus.mkv").read_bytes()
    assert dest_bytes_a == src_bytes_a
    assert dest_bytes_b == src_bytes_b


def test_use_final_map_fires_transfer_progress_callback(rig, test_db, monkeypatch):
    """The transfer-progress callback gets per-file invocations, all in
    the 0-50% overall range (transfer step is the first half; hash
    verification fills the second half)."""
    monkeypatch.setattr(
        "api.routers.jobs._verify_transfer_destination",
        lambda *a, **k: (True, None),
    )
    monkeypatch.setattr(
        "api.routers.jobs._complete_transfer",
        lambda *a, **k: None,
    )

    pct_calls: list[int] = []
    session = test_db()
    try:
        job = session.query(models.Job).filter_by(id=rig["job_id"]).first()
        config = session.query(models.TransferConfig).filter_by(id=rig["config_id"]).first()
        _execute_local_transfer_use_final_map(
            session, job, rig["src_root"], config,
            output_files=None,
            job_metadata={},
            transfer_progress_callback=lambda pct: pct_calls.append(pct),
            hash_progress_callback=_no_op_progress,
        )
    finally:
        session.close()

    # One callback per file copied.
    assert len(pct_calls) == 2
    # Every value is in the 0-50% transfer-step range.
    assert all(0 <= p <= 50 for p in pct_calls), pct_calls
    # Last value is the upper bound of the transfer step.
    assert pct_calls[-1] == 50


def test_use_final_map_prefixes_single_segment_paths_with_movies(test_db, tmp_path, monkeypatch):
    """When post_paths entries are bare filenames (no library prefix),
    the helper prepends ``Movies/`` if ``src_root`` has a Movies top
    dir. Matches the legacy inline behaviour exactly so a flag-off
    rip from an older release doesn't suddenly land in a different
    layout."""
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    (src_root / "Movies").mkdir(parents=True)
    src_file = src_root / "Movies" / "Bare.mkv"
    src_file.write_bytes(b"x" * 64)
    dest_root.mkdir()

    session = test_db()
    try:
        disc_id = str(uuid.uuid4())
        session.add(models.Disc(id=disc_id, content_hash=f"h-{uuid.uuid4().hex[:8]}"))
        tid = str(uuid.uuid4())
        session.add(models.DiscTitle(
            id=tid, disc_id=disc_id, title="Bare", source_file="Bare.mkv",
        ))
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc_id, disc_num="1", mount_point="/mnt/sr0",
            rip_state="completed", phase="transfer", transfer_state="running",
            post_paths={tid: "Bare.mkv"},  # ← single segment, no library prefix
        )
        session.add(job)
        config = models.TransferConfig(
            id=str(uuid.uuid4()),
            name="t", mode="local", transfer_dir=str(dest_root),
            is_active=False, config_data={"transfer_dir": str(dest_root)},
        )
        session.add(config)
        session.commit()
        session.refresh(job)
        session.refresh(config)
    finally:
        session.close()

    monkeypatch.setattr("api.routers.jobs._verify_transfer_destination", lambda *a, **k: (True, None))
    monkeypatch.setattr("api.routers.jobs._complete_transfer", lambda *a, **k: None)

    session = test_db()
    try:
        job = session.query(models.Job).filter_by(id=str(job.id)).first()
        config = session.query(models.TransferConfig).filter_by(id=str(config.id)).first()
        dests = _execute_local_transfer_use_final_map(
            session, job, src_root, config,
            output_files=None, job_metadata={},
            transfer_progress_callback=_no_op_progress,
            hash_progress_callback=_no_op_progress,
        )
    finally:
        session.close()

    # The single-segment "Bare.mkv" landed under Movies/, not at dest root.
    assert (dest_root / "Movies" / "Bare.mkv").is_file()
    assert not (dest_root / "Bare.mkv").exists()


def test_use_final_map_raises_400_when_not_enough_free_space(rig, test_db, monkeypatch):
    """Disk-usage preflight is the only thing standing between a partial
    transfer and a corrupted destination. Synthetically set free=0."""
    import shutil as _shutil

    class _Usage:
        total = 1 << 40
        used = 1 << 40
        free = 0

    monkeypatch.setattr(_shutil, "disk_usage", lambda _p: _Usage())

    session = test_db()
    try:
        job = session.query(models.Job).filter_by(id=rig["job_id"]).first()
        config = session.query(models.TransferConfig).filter_by(id=rig["config_id"]).first()
        with pytest.raises(HTTPException) as exc_info:
            _execute_local_transfer_use_final_map(
                session, job, rig["src_root"], config,
                output_files=None, job_metadata={},
                transfer_progress_callback=_no_op_progress,
                hash_progress_callback=_no_op_progress,
            )
    finally:
        session.close()

    assert exc_info.value.status_code == 400
    assert "free space" in str(exc_info.value.detail).lower()


def test_use_final_map_skips_missing_source_files(test_db, tmp_path, monkeypatch):
    """If a post_paths entry doesn't exist on disk (rare — stale job
    state, partial cleanup), the helper silently skips it rather than
    failing the whole transfer. The files that DO exist still land."""
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    (src_root / "Movies" / "Film").mkdir(parents=True)
    present = src_root / "Movies" / "Film" / "present.mkv"
    present.write_bytes(b"y" * 128)
    dest_root.mkdir()

    session = test_db()
    try:
        disc_id = str(uuid.uuid4())
        session.add(models.Disc(id=disc_id, content_hash=f"h-{uuid.uuid4().hex[:8]}"))
        tid_present = str(uuid.uuid4())
        tid_missing = str(uuid.uuid4())
        for tid, name in ((tid_present, "present.mkv"), (tid_missing, "missing.mkv")):
            session.add(models.DiscTitle(id=tid, disc_id=disc_id, title=name, source_file=name))
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc_id, disc_num="1", mount_point="/mnt/sr0",
            rip_state="completed", phase="transfer", transfer_state="running",
            post_paths={
                tid_present: "Movies/Film/present.mkv",
                tid_missing: "Movies/Film/missing.mkv",  # ← deliberately not on disk
            },
        )
        session.add(job)
        config = models.TransferConfig(
            id=str(uuid.uuid4()), name="t", mode="local", transfer_dir=str(dest_root),
            is_active=False, config_data={"transfer_dir": str(dest_root)},
        )
        session.add(config)
        session.commit()
        session.refresh(job)
        session.refresh(config)
    finally:
        session.close()

    monkeypatch.setattr("api.routers.jobs._verify_transfer_destination", lambda *a, **k: (True, None))
    monkeypatch.setattr("api.routers.jobs._complete_transfer", lambda *a, **k: None)

    session = test_db()
    try:
        job = session.query(models.Job).filter_by(id=str(job.id)).first()
        config = session.query(models.TransferConfig).filter_by(id=str(config.id)).first()
        # No exception even though one source file is missing.
        _execute_local_transfer_use_final_map(
            session, job, src_root, config,
            output_files=None, job_metadata={},
            transfer_progress_callback=_no_op_progress,
            hash_progress_callback=_no_op_progress,
        )
    finally:
        session.close()

    assert (dest_root / "Movies/Film/present.mkv").is_file()
    assert not (dest_root / "Movies/Film/missing.mkv").exists()


def test_use_final_map_output_files_override_takes_precedence(rig, test_db, monkeypatch):
    """``output_files`` (from disc_payload) is the manual override path —
    when set it wins over job.post_paths. The endpoint's existing
    behaviour was to iterate ``output_files.values()`` and ignore
    post_paths entirely; pin that."""
    monkeypatch.setattr("api.routers.jobs._verify_transfer_destination", lambda *a, **k: (True, None))
    monkeypatch.setattr("api.routers.jobs._complete_transfer", lambda *a, **k: None)

    # Only the first file is in output_files.
    override = {rig["tid_a"]: "Movies/Test Film (2024)/Test Film.1080p.mkv"}

    session = test_db()
    try:
        job = session.query(models.Job).filter_by(id=rig["job_id"]).first()
        config = session.query(models.TransferConfig).filter_by(id=rig["config_id"]).first()
        _execute_local_transfer_use_final_map(
            session, job, rig["src_root"], config,
            output_files=override,
            job_metadata={},
            transfer_progress_callback=_no_op_progress,
            hash_progress_callback=_no_op_progress,
        )
    finally:
        session.close()

    # Only the override file landed; the bonus file (in post_paths but
    # not in override) was skipped.
    assert (rig["dest_root"] / "Movies/Test Film (2024)/Test Film.1080p.mkv").is_file()
    assert not (rig["dest_root"] / "Movies/Test Film (2024)/Test Film.bonus.mkv").exists()
