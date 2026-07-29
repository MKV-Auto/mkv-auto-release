"""#607: ``_update_title_file_paths_after_transfer`` regression matrix.

The pre-#607 implementation in ``Backend/api/routers/jobs.py`` walked the
destination with ``Path(dest_root).rglob('*.mkv')`` — only worked for
local filesystem destinations. For SMB / rsync / NFS the dest_path is a
URI string (``smb://...``, ``user@host:...``) that ``pathlib.Path``
can't walk, so the title-id → dest map stayed empty and
``DiscTitle.file_path_stage`` never advanced past ``'postprocess'``.

These tests confirm the rewritten writer constructs the per-title
destination string deterministically from ``Job.post_paths`` joined to
the protocol's ``dest_root``, regardless of mode. One spec per mode +
edge cases (single-file vs directory, idempotent re-run, missing
data, trailing slash on dest_root).
"""
import uuid

import pytest

from api import models
from api.routers.jobs import _update_title_file_paths_after_transfer


@pytest.fixture
def disc_with_titles(test_db):
    """A disc + 3 titles seeded so the writer has something to update."""
    session = test_db()
    try:
        disc_id = str(uuid.uuid4())
        session.add(models.Disc(id=disc_id, content_hash=f"h-{uuid.uuid4().hex[:8]}"))
        tids = [str(uuid.uuid4()) for _ in range(3)]
        for i, tid in enumerate(tids):
            session.add(models.DiscTitle(
                id=tid, disc_id=disc_id,
                title=f"Title {i}",
                source_file=f"0000{i}.mpls",
                file_path=f"/jobs/J/transient/Movies/X/Title {i}.mkv",
                file_path_stage="postprocess",
            ))
        session.commit()
        yield session, disc_id, tids
    finally:
        session.close()


def _attach_job(session, disc_id, *, post_paths, transfer_paths):
    """Build a Job pinned to disc_id with the given post_paths +
    transfer_paths. Returns the job after commit + refresh so
    ``job.disc`` and ``job.post_paths`` work as the writer expects."""
    job = models.Job(
        disc_id=disc_id,
        disc_num="1",
        mount_point="/mnt/dvd",
        job_status="completed",
        transfer_state="completed",
        post_paths=post_paths,
        transfer_paths=transfer_paths,
        stage_profile="hit",
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


# ──────────────────────────────────────────────────────────────────────────
# Per-mode coverage
# ──────────────────────────────────────────────────────────────────────────


def test_local_mode_directory_dest_root(disc_with_titles):
    """Local mode returns a directory path; per-title dest is
    ``<dest_root>/<rel_path>``."""
    session, disc_id, [t0, t1, t2] = disc_with_titles
    post_paths = {
        t0: "Movies/Goonies (1985)/Goonies (1985).mkv",
        t1: "Movies/Goonies (1985)/Goonies (1985) - extras/Trailer.mkv",
        t2: "Movies/Goonies (1985)/Goonies (1985) - extras/Featurette.mkv",
    }
    transfer_paths = ["/mnt/library/Movies/Goonies (1985)"]
    job = _attach_job(
        session, disc_id, post_paths=post_paths, transfer_paths=transfer_paths
    )

    _update_title_file_paths_after_transfer(job, session, transfer_paths)
    session.commit()

    for tid, rel in post_paths.items():
        t = session.query(models.DiscTitle).filter_by(id=tid).first()
        assert t.file_path_stage == "transfer"
        assert t.file_path == f"/mnt/library/Movies/Goonies (1985)/{rel}"


def test_smb_mode_uri_dest_root(disc_with_titles):
    """The exact regression: ``smb://host/share/...`` is a URI that
    ``Path().rglob`` would treat as empty; the writer must concatenate
    instead of walking."""
    session, disc_id, [t0, t1, t2] = disc_with_titles
    post_paths = {
        t0: "Movies/Goonies (1985)/Goonies.1080p.mkv",
        t1: "Movies/Goonies (1985)/extras/Trailer.mkv",
        t2: "Movies/Goonies (1985)/extras/Featurette.mkv",
    }
    transfer_paths = ["smb://192.0.2.10/PLEX Media/"]
    job = _attach_job(
        session, disc_id, post_paths=post_paths, transfer_paths=transfer_paths
    )

    _update_title_file_paths_after_transfer(job, session, transfer_paths)
    session.commit()

    for tid, rel in post_paths.items():
        t = session.query(models.DiscTitle).filter_by(id=tid).first()
        assert t.file_path_stage == "transfer"
        assert t.file_path == f"smb://192.0.2.10/PLEX Media/{rel}"


def test_rsync_mode_user_at_host_dest_root(disc_with_titles):
    """rsync uses ``user@host:/path`` — same constraint, different
    syntax. Per-title dest must still concatenate cleanly."""
    session, disc_id, [t0, t1, _t2] = disc_with_titles
    post_paths = {
        t0: "Movies/Wednesday (2022) S01E01.mkv",
        t1: "Movies/Wednesday (2022) S01E02.mkv",
    }
    transfer_paths = ["plex@192.0.2.20:/mnt/library"]
    job = _attach_job(
        session, disc_id, post_paths=post_paths, transfer_paths=transfer_paths
    )

    _update_title_file_paths_after_transfer(job, session, transfer_paths)
    session.commit()

    for tid, rel in post_paths.items():
        t = session.query(models.DiscTitle).filter_by(id=tid).first()
        assert t.file_path_stage == "transfer"
        assert t.file_path == f"plex@192.0.2.20:/mnt/library/{rel}"


def test_nfs_mode_uri_dest_root(disc_with_titles):
    """NFS uses an ``nfs://`` URI form for completeness."""
    session, disc_id, [t0, *_] = disc_with_titles
    post_paths = {t0: "Movies/Test Movie/Test Movie.mkv"}
    transfer_paths = ["nfs://192.0.2.30/exports/library"]
    job = _attach_job(
        session, disc_id, post_paths=post_paths, transfer_paths=transfer_paths
    )

    _update_title_file_paths_after_transfer(job, session, transfer_paths)
    session.commit()

    t = session.query(models.DiscTitle).filter_by(id=t0).first()
    assert t.file_path_stage == "transfer"
    assert t.file_path == "nfs://192.0.2.30/exports/library/Movies/Test Movie/Test Movie.mkv"


# ──────────────────────────────────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────────────────────────────────


def test_trailing_slash_on_dest_root_is_normalised(disc_with_titles):
    """Protocols are inconsistent about trailing slashes — the writer
    must handle both ``/share/dir`` and ``/share/dir/`` identically."""
    session, disc_id, [t0, *_] = disc_with_titles
    post_paths = {t0: "Movies/X/X.mkv"}
    job_no_slash = _attach_job(
        session, disc_id,
        post_paths=post_paths,
        transfer_paths=["smb://host/share"],
    )

    _update_title_file_paths_after_transfer(job_no_slash, session, ["smb://host/share"])
    session.commit()
    t = session.query(models.DiscTitle).filter_by(id=t0).first()
    assert t.file_path == "smb://host/share/Movies/X/X.mkv"

    # Same input but with trailing slash — must produce the same result.
    _update_title_file_paths_after_transfer(job_no_slash, session, ["smb://host/share/"])
    session.commit()
    session.refresh(t)
    assert t.file_path == "smb://host/share/Movies/X/X.mkv"


def test_single_file_dest_root_matches_by_basename(disc_with_titles):
    """When ``dest_root`` ends in ``.mkv`` it IS the destination file
    (single-file transfer). Match by basename against post_paths."""
    session, disc_id, [t0, t1, _t2] = disc_with_titles
    post_paths = {
        t0: "Movies/Goonies/Goonies.mkv",
        t1: "Movies/Goonies/extras/Trailer.mkv",  # different basename, ignored
    }
    transfer_paths = ["smb://host/share/Movies/Goonies/Goonies.mkv"]
    job = _attach_job(
        session, disc_id, post_paths=post_paths, transfer_paths=transfer_paths
    )

    _update_title_file_paths_after_transfer(job, session, transfer_paths)
    session.commit()

    a = session.query(models.DiscTitle).filter_by(id=t0).first()
    b = session.query(models.DiscTitle).filter_by(id=t1).first()
    assert a.file_path_stage == "transfer"
    assert a.file_path == "smb://host/share/Movies/Goonies/Goonies.mkv"
    # Only the matched title moves; the other stays at the prior stage.
    assert b.file_path_stage == "postprocess"


def test_idempotent_rerun_overwrites_with_same_value(disc_with_titles):
    """The back-fill script may run the writer over already-correct
    rows. That must be safe — second pass writes the same value."""
    session, disc_id, [t0, *_] = disc_with_titles
    post_paths = {t0: "Movies/X/X.mkv"}
    transfer_paths = ["smb://host/share"]
    job = _attach_job(
        session, disc_id, post_paths=post_paths, transfer_paths=transfer_paths
    )

    _update_title_file_paths_after_transfer(job, session, transfer_paths)
    session.commit()
    t = session.query(models.DiscTitle).filter_by(id=t0).first()
    first = t.file_path

    _update_title_file_paths_after_transfer(job, session, transfer_paths)
    session.commit()
    session.refresh(t)
    assert t.file_path == first
    assert t.file_path_stage == "transfer"


def test_missing_post_paths_is_no_op(disc_with_titles):
    """No post_paths → nothing to write; row stays at the prior stage."""
    session, disc_id, [t0, *_] = disc_with_titles
    job = _attach_job(
        session, disc_id, post_paths=None, transfer_paths=["smb://host/share"]
    )

    _update_title_file_paths_after_transfer(job, session, ["smb://host/share"])
    session.commit()

    t = session.query(models.DiscTitle).filter_by(id=t0).first()
    assert t.file_path_stage == "postprocess"  # unchanged


def test_missing_dest_paths_is_no_op(disc_with_titles):
    """Empty/None dest_paths → nothing to write."""
    session, disc_id, [t0, *_] = disc_with_titles
    job = _attach_job(
        session, disc_id,
        post_paths={t0: "Movies/X/X.mkv"},
        transfer_paths=[],
    )

    _update_title_file_paths_after_transfer(job, session, [])
    session.commit()

    t = session.query(models.DiscTitle).filter_by(id=t0).first()
    assert t.file_path_stage == "postprocess"  # unchanged
