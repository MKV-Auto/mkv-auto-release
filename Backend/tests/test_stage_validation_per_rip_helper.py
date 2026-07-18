"""
Coverage for ``stage_validation._per_rip_mkv_count_or_walk`` (#365 step 5a).

Mirrors the per-rip count precedence pattern established in the prep-body
walks (steps 4b, 4d, 4e). The helper is used by the 3 walks in
``stage_validation.py``:

  * Pre-flight: ``files_already_moved`` check
  * Pre-flight: ``transient_files_count`` detail reporting
  * Post-process: empty-expected_files fallback

Same safety properties:
- Per-rip post_paths preferred over directory walk
- Walk fallback only when no per-rip signal exists
- Defensive against None / non-UUID-keyed inputs
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.stage_validation import _per_rip_mkv_count_or_walk


UUID_KEY = "0000fffe-0000-4000-8000-000000000001"


# ──────────────────────────────────────────────────────────────────────────
# Per-rip precedence
# ──────────────────────────────────────────────────────────────────────────


def test_persisted_post_paths_count_returned(tmp_path):
    """job.post_paths length is returned when UUID-keyed; walk skipped."""
    transient_dir = tmp_path / "transient"
    transient_dir.mkdir()
    # Seed unrelated MKVs in transient_dir to prove walk is skipped.
    for i in range(5):
        (transient_dir / f"other_{i}.mkv").touch()
    job = SimpleNamespace(
        post_paths={UUID_KEY: "Movies/A/A.mkv"},
        disc_payload={},
        id="j-1",
    )
    assert _per_rip_mkv_count_or_walk(job, transient_dir) == 1


def test_disc_payload_used_when_persisted_empty(tmp_path):
    transient_dir = tmp_path / "transient"
    job = SimpleNamespace(
        post_paths={},
        disc_payload={"post_paths": {UUID_KEY: "Movies/B/B.mkv"}},
        id="j-1",
    )
    assert _per_rip_mkv_count_or_walk(job, transient_dir) == 1


def test_non_uuid_keys_falls_back_to_walk(tmp_path):
    transient_dir = tmp_path / "transient"
    transient_dir.mkdir()
    (transient_dir / "a.mkv").touch()
    (transient_dir / "b.mkv").touch()
    job = SimpleNamespace(
        post_paths={"00001.mpls": "A.mkv"},  # legacy non-UUID
        disc_payload={},
        id="j-1",
    )
    assert _per_rip_mkv_count_or_walk(job, transient_dir) == 2  # walk fallback


# ──────────────────────────────────────────────────────────────────────────
# Walk fallback
# ──────────────────────────────────────────────────────────────────────────


def test_walk_fallback_when_no_per_rip_signal(tmp_path):
    transient_dir = tmp_path / "transient"
    transient_dir.mkdir()
    (transient_dir / "a.mkv").touch()
    job = SimpleNamespace(post_paths={}, disc_payload={}, id="j-1")
    assert _per_rip_mkv_count_or_walk(job, transient_dir) == 1


def test_missing_transient_dir_returns_zero(tmp_path):
    """Defensive: when transient_dir doesn't exist and no per-rip
    signal, return 0 (don't crash)."""
    transient_dir = tmp_path / "nonexistent"
    job = SimpleNamespace(post_paths={}, disc_payload={}, id="j-1")
    assert _per_rip_mkv_count_or_walk(job, transient_dir) == 0


def test_none_post_paths_handled_defensively(tmp_path):
    """post_paths=None and disc_payload=None handled."""
    transient_dir = tmp_path / "transient"
    job = SimpleNamespace(post_paths=None, disc_payload=None, id="j-1")
    assert _per_rip_mkv_count_or_walk(job, transient_dir) == 0


# ──────────────────────────────────────────────────────────────────────────
# Regression: shared-library trans_dir scenario
# ──────────────────────────────────────────────────────────────────────────


def test_shared_library_returns_per_rip_count_not_library_count(tmp_path):
    """Documents the bug step 5a fixes: under
    MKVAUTO_RENAME_DIRECT_TO_DEST=1 the transient_dir for a job can be
    the user's library. Without the per-rip preference, the validation
    would over-count by hundreds (the library's existing MKVs)."""
    shared_library = tmp_path / "library"
    shared_library.mkdir()
    for i in range(50):
        (shared_library / f"old_film_{i}.mkv").touch()
    (shared_library / "this_rip_output.mkv").touch()

    job = SimpleNamespace(
        post_paths={UUID_KEY: "this_rip_output.mkv"},
        disc_payload={},
        id="j-1",
    )
    # With per-rip signal: 1 (the file this rip moved).
    # Without (pre-step-5a): 51 (every MKV in the library).
    assert _per_rip_mkv_count_or_walk(job, shared_library) == 1, (
        "per-rip preference must not be diluted by unrelated library files"
    )
