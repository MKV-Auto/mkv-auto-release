"""
Coverage for ``_update_title_file_paths`` in ``Backend/workers/tasks.py:774``.

The function persists ``DiscTitle.file_path`` and ``file_path_stage`` for a
batch of titles at the end of each pipeline stage (rip / postprocess /
transfer). It's a small helper but it sits on the critical path of every
file-state-aware feature: the history page reads ``file_path`` to know where
files ended up, the rename endpoint uses ``file_path_stage`` to decide which
parent directory to compute the new path against, and Phase 2 of the
postprocess collapse will swap one of the stage values out from under it
(``postprocess`` → folded into ``transfer``).

Phase 0 backfill for the postprocess collapse plan
(``docs/plans/postprocess-collapse-325-365.md``). Captures the current
contract so Phase 2 can refactor with confidence.
"""
import uuid

import pytest

from api import models
from workers.tasks import _update_title_file_paths


def _seed_disc_with_titles(session, *, disc_content_hash="hash-fp", n=2):
    """Minimal seed: one disc + N titles. No release/movie required —
    this helper exercises the DB write path only."""
    disc_id = str(uuid.uuid4())
    session.add(models.Disc(id=disc_id, content_hash=disc_content_hash))
    title_ids = []
    for i in range(n):
        tid = str(uuid.uuid4())
        session.add(models.DiscTitle(
            id=tid, disc_id=disc_id,
            title=f"Title {i}",
            source_file=f"0000{i}.mpls",
        ))
        title_ids.append(tid)
    session.commit()
    return disc_id, title_ids


# ──────────────────────────────────────────────────────────────────────────
# Stage marker semantics
# ──────────────────────────────────────────────────────────────────────────


def test_rip_stage_sets_file_path_and_stage(test_db):
    """After rip completion, the helper records the absolute MKV path and
    tags it with stage='rip' so downstream stages can detect un-postprocessed
    files."""
    session = test_db()
    try:
        disc_id, [tid_a, tid_b] = _seed_disc_with_titles(session)
        path_map = {tid_a: "/jobs/J/raw/00001.mkv", tid_b: "/jobs/J/raw/00002.mkv"}

        _update_title_file_paths(session, disc_id, path_map, "rip")
        session.commit()

        a = session.query(models.DiscTitle).filter_by(id=tid_a).first()
        b = session.query(models.DiscTitle).filter_by(id=tid_b).first()
        assert a.file_path == "/jobs/J/raw/00001.mkv"
        assert a.file_path_stage == "rip"
        assert b.file_path == "/jobs/J/raw/00002.mkv"
        assert b.file_path_stage == "rip"
    finally:
        session.close()


def test_later_stage_overwrites_earlier_stage_for_same_title(test_db):
    """Each pipeline stage transitions the title's file_path forward. Calling
    the helper with a later stage overwrites both the path and the stage
    marker — there's no "preserve earlier stage" logic."""
    session = test_db()
    try:
        disc_id, [tid] = _seed_disc_with_titles(session, n=1)

        _update_title_file_paths(session, disc_id, {tid: "/jobs/J/raw/01.mkv"}, "rip")
        session.commit()
        t = session.query(models.DiscTitle).filter_by(id=tid).first()
        assert t.file_path_stage == "rip"

        _update_title_file_paths(
            session, disc_id, {tid: "/jobs/J/transient/Movies/X/X.mkv"}, "postprocess",
        )
        session.commit()
        session.refresh(t)
        assert t.file_path == "/jobs/J/transient/Movies/X/X.mkv"
        assert t.file_path_stage == "postprocess"

        _update_title_file_paths(
            session, disc_id, {tid: "/library/Movies/X/X.mkv"}, "transfer",
        )
        session.commit()
        session.refresh(t)
        assert t.file_path == "/library/Movies/X/X.mkv"
        assert t.file_path_stage == "transfer"
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────────
# Disc isolation — the disc_id filter must scope updates correctly
# ──────────────────────────────────────────────────────────────────────────


def test_only_affects_specified_disc(test_db):
    """A path_map keyed by title_id alone is ambiguous if two discs ever had
    the same title id (they shouldn't, but the SQL filter is the safety
    net). The helper must filter by disc_id so it can never cross-write."""
    session = test_db()
    try:
        disc_a, [tid_a] = _seed_disc_with_titles(session, disc_content_hash="hash-a", n=1)
        disc_b, [tid_b] = _seed_disc_with_titles(session, disc_content_hash="hash-b", n=1)

        # Call against disc_a but include disc_b's title id in the map —
        # the disc_id filter should reject it.
        _update_title_file_paths(
            session, disc_a,
            {tid_a: "/disc_a/file.mkv", tid_b: "/should-not-apply.mkv"},
            "rip",
        )
        session.commit()

        a = session.query(models.DiscTitle).filter_by(id=tid_a).first()
        b = session.query(models.DiscTitle).filter_by(id=tid_b).first()
        assert a.file_path == "/disc_a/file.mkv"
        assert b.file_path is None, "disc_b's title must not have been written"
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────────
# Path resolution
# ──────────────────────────────────────────────────────────────────────────


def test_relative_paths_resolve_against_base_dir(test_db):
    """When ``base_dir`` is supplied, relative entries in the path_map are
    joined onto it. This is how postprocess passes ``post_paths`` (which are
    relative to the job's transient root) to the helper."""
    session = test_db()
    try:
        disc_id, [tid] = _seed_disc_with_titles(session, n=1)
        _update_title_file_paths(
            session, disc_id,
            {tid: "Movies/My Film (2024)/My Film.1080p.mkv"},
            "postprocess",
            base_dir="/jobs/J/transient",
        )
        session.commit()
        t = session.query(models.DiscTitle).filter_by(id=tid).first()
        assert t.file_path == "/jobs/J/transient/Movies/My Film (2024)/My Film.1080p.mkv"
        assert t.file_path_stage == "postprocess"
    finally:
        session.close()


def test_absolute_paths_ignore_base_dir(test_db):
    """If a path is already absolute, ``base_dir`` is not prepended — caller
    can mix absolute and relative entries in the same map."""
    session = test_db()
    try:
        disc_id, [tid_abs, tid_rel] = _seed_disc_with_titles(session, n=2)
        _update_title_file_paths(
            session, disc_id,
            {
                tid_abs: "/already/absolute/A.mkv",
                tid_rel: "relative/B.mkv",
            },
            "rip",
            base_dir="/jobs/J/raw",
        )
        session.commit()
        a = session.query(models.DiscTitle).filter_by(id=tid_abs).first()
        b = session.query(models.DiscTitle).filter_by(id=tid_rel).first()
        assert a.file_path == "/already/absolute/A.mkv"
        assert b.file_path == "/jobs/J/raw/relative/B.mkv"
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────────────────────────────────


def test_empty_path_map_is_no_op(test_db):
    """An empty path_map exits immediately — no DB query, no flush, no
    side effects."""
    session = test_db()
    try:
        disc_id, [tid] = _seed_disc_with_titles(session, n=1)
        _update_title_file_paths(session, disc_id, {}, "rip")
        session.commit()
        t = session.query(models.DiscTitle).filter_by(id=tid).first()
        assert t.file_path is None
        assert t.file_path_stage is None
    finally:
        session.close()


def test_unknown_title_ids_are_silently_skipped(test_db):
    """If the path_map references a title id that isn't on the disc (stale
    job state, dropped title, etc.), the helper logs and moves on rather
    than raising. The other titles in the same batch still get written."""
    session = test_db()
    try:
        disc_id, [tid] = _seed_disc_with_titles(session, n=1)
        unknown = str(uuid.uuid4())

        _update_title_file_paths(
            session, disc_id,
            {tid: "/jobs/J/raw/known.mkv", unknown: "/jobs/J/raw/ghost.mkv"},
            "rip",
        )
        session.commit()

        t = session.query(models.DiscTitle).filter_by(id=tid).first()
        assert t.file_path == "/jobs/J/raw/known.mkv"
        assert t.file_path_stage == "rip"
    finally:
        session.close()
