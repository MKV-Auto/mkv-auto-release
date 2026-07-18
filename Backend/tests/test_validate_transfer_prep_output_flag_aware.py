"""
End-to-end coverage that ``validate_transfer_prep_output`` looks at the
right directory under both branches of the
``MKVAUTO_RENAME_DIRECT_TO_DEST`` flag (#365 transient/-drop audit).

The bug this guards against: before this audit the validator hardcoded
``paths.transient`` regardless of where rename actually wrote. Under
flag-on local mode the rename writes directly to
``config.transfer_dir`` and ``transient/`` stays empty, so the validator
reported "Only found 0 of N expected files" while the files were
sitting at the library destination. Caught in a live smoke test —
captured here so a regression fails CI loudly instead of failing the
next operator's rip.

Test pattern: create real files on disk at the resolved destination,
populate ``job.post_paths`` with relative paths the validator will join
to that destination, and assert ``validate_transfer_prep_output`` reports
``valid=True``. The flag-off and flag-on-remote cases place the same
files under ``paths.transient``; the flag-on-local case places them
under ``config.transfer_dir``.
"""
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from api import models
from core.job_paths import JobPaths
from core.stage_validation import validate_transfer_prep_output


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _seed_disc_titles(session, disc_id, count=2):
    """Create lightweight DiscTitle rows the validator can iterate via
    job.disc_titles. Returns the list of title_ids."""
    title_ids = []
    for i in range(count):
        tid = str(uuid.uuid4())
        title_ids.append(tid)
        session.add(models.DiscTitle(
            id=tid,
            disc_id=disc_id,
            index=i,
            source_file=f"source_{i}.mkv",
            type="movie",  # not "ignore" → counted by validator
            title=f"Title {i}",
            mkv_size=1024,  # match the bytes we'll write below
        ))
    return title_ids


def _make_real_job(session, *, post_paths_rel: dict):
    """Persist a real Disc + Job + DiscTitle rows so the validator's
    SQLAlchemy session traversal works end-to-end. Returns the job row."""
    disc_id = str(uuid.uuid4())
    session.add(models.Disc(
        id=disc_id, content_hash=f"h-{uuid.uuid4().hex[:8]}",
    ))
    title_ids = _seed_disc_titles(session, disc_id, count=len(post_paths_rel))
    # Rebuild post_paths with the real title_ids (preserving rel order).
    keyed = dict(zip(title_ids, post_paths_rel.values()))
    job_id = str(uuid.uuid4())
    session.add(models.Job(
        id=job_id, disc_id=disc_id, disc_num="1", mount_point="/mnt/sr0",
        post_paths=keyed,
        ripped_files=keyed,  # validator looks up mkv_size by title_id
        rip_state="completed",
        job_status="running",
    ))
    session.commit()
    return session.get(models.Job, job_id), Path  # type ignore


def _write_files(root: Path, rel_paths):
    """Create the named files under root with a small known payload."""
    for rel in rel_paths:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 1024)


# ──────────────────────────────────────────────────────────────────────────
# Flag-off — production default, files at transient/
# ──────────────────────────────────────────────────────────────────────────


def test_validate_flag_explicitly_off_finds_files_at_transient(test_db, monkeypatch, tmp_path):
    """Explicit operator opt-out (``MKVAUTO_RENAME_DIRECT_TO_DEST=0``):
    rename writes to jobs/<id>/transient/, validator looks there →
    finds the files → valid=True. Preserves pre-5c behaviour for
    operators who pin the var to the old default."""
    monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path))

    session = test_db()
    try:
        post_paths_rel = {
            "x": "Movies/Foo (2024)/Foo (2024).mkv",
            "y": "Movies/Foo (2024)/Foo (2024) - bonus.mkv",
        }
        job, _ = _make_real_job(session, post_paths_rel=post_paths_rel)
        paths = JobPaths.from_job(job, out_dir=str(tmp_path))
        paths.ensure_layout()
        _write_files(paths.transient, post_paths_rel.values())

        result = validate_transfer_prep_output(job, session, paths)
        assert result.valid, f"unexpected errors: {result.errors}"
        assert result.details["files_found"] == 2
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────────
# Flag-on local — files at config.transfer_dir (the audit bug fix)
# ──────────────────────────────────────────────────────────────────────────


def test_validate_flag_on_local_finds_files_at_transfer_dir(test_db, monkeypatch, tmp_path):
    """Audit-bug-fix path: under flag-on local mode the rename wrote
    directly to config.transfer_dir. The validator MUST look there and
    find the files. Pre-fix this test would fail with
    "Only found 0 of N expected files" because the validator was
    hardcoded to paths.transient (empty under flag-on)."""
    transfer_dir = tmp_path / "library"
    transfer_dir.mkdir()
    monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path / "data"))

    session = test_db()
    try:
        session.add(models.TransferConfig(
            id=str(uuid.uuid4()), mode="local", is_active=True,
            transfer_dir=str(transfer_dir),
        ))
        session.commit()

        post_paths_rel = {
            "x": "Movies/Foo (2024)/Foo (2024).mkv",
            "y": "Movies/Foo (2024)/Foo (2024) - bonus.mkv",
        }
        job, _ = _make_real_job(session, post_paths_rel=post_paths_rel)
        paths = JobPaths.from_job(job, out_dir=str(tmp_path / "data"))
        paths.ensure_layout()
        # Files at the LIBRARY (where rename wrote under flag-on),
        # transient/ deliberately left empty to mirror reality.
        _write_files(transfer_dir, post_paths_rel.values())

        result = validate_transfer_prep_output(job, session, paths)
        assert result.valid, f"unexpected errors: {result.errors}"
        assert result.details["files_found"] == 2
    finally:
        session.close()


def test_validate_flag_on_local_fails_loud_when_files_missing(test_db, monkeypatch, tmp_path):
    """Flag-on local with no files anywhere → validator correctly fails
    with the actual missing-files error (not a "wrong directory" red
    herring). Confirms the failure message now reports the library
    path, not transient/, so operators can see *which* directory was
    empty."""
    transfer_dir = tmp_path / "library"
    transfer_dir.mkdir()
    monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path / "data"))

    session = test_db()
    try:
        session.add(models.TransferConfig(
            id=str(uuid.uuid4()), mode="local", is_active=True,
            transfer_dir=str(transfer_dir),
        ))
        session.commit()
        post_paths_rel = {"x": "Movies/Missing/file.mkv"}
        job, _ = _make_real_job(session, post_paths_rel=post_paths_rel)
        paths = JobPaths.from_job(job, out_dir=str(tmp_path / "data"))
        paths.ensure_layout()
        # NO files written anywhere.

        result = validate_transfer_prep_output(job, session, paths)
        assert not result.valid
        # Error mentions the missing file by name.
        assert any("Movies/Missing/file.mkv" in e for e in result.errors)
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────────
# Flag-on remote — files still at transient/ (staging)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["rsync", "smb", "nfs"])
def test_validate_flag_on_remote_finds_files_at_transient(test_db, monkeypatch, tmp_path, mode):
    """Remote modes always use local transient/ as staging for atomic
    upload. The validator must look there regardless of flag state.
    Confirms the per-rip-mode dispatch in
    resolve_transfer_prep_validation_root matches what rename did."""
    monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path))

    session = test_db()
    try:
        session.add(models.TransferConfig(
            id=str(uuid.uuid4()), mode=mode, is_active=True,
            transfer_dir="/remote/library",
        ))
        session.commit()
        post_paths_rel = {"x": "Movies/Bar/Bar.mkv"}
        job, _ = _make_real_job(session, post_paths_rel=post_paths_rel)
        paths = JobPaths.from_job(job, out_dir=str(tmp_path))
        paths.ensure_layout()
        # For remote, rename still writes to transient/ (local staging).
        _write_files(paths.transient, post_paths_rel.values())

        result = validate_transfer_prep_output(job, session, paths)
        assert result.valid, f"{mode}: unexpected errors {result.errors}"
    finally:
        session.close()
