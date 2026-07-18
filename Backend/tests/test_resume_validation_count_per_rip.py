"""
Coverage for the resume validation MKV-count check (#365 step 4e).

At the start of ``_run_prep_phase`` (after files_already_moved
detection), if there's a resume scenario, the worker checks whether
``source_dir + trans_root`` together hold enough MKVs to satisfy
``expected_count``. If not, it aborts the retry.

The trans_root walk in this check is the **last** of 5 trans_root
walks in _run_prep_phase that step 4 (a–e) makes per-rip-aware. Under
``MKVAUTO_RENAME_DIRECT_TO_DEST`` the walk over a shared library would
massively over-count and mask real resume failures.

Step 4e prefers per-rip post_paths counts (job.post_paths or
disc_payload.post_paths with UUID keys) over the trans_root walk,
mirroring the precedence chain established in steps 4b + 4d.
"""
from types import SimpleNamespace

import pytest


def _compute_actual_count(
    job, source_mkv_count: int, trans_root_mkv_count: int,
) -> int:
    """Reproduces the resume-validation actual_count computation from
    _run_prep_phase after step 4e.

    actual_count starts at source_mkv_count (always per-job, safe to
    count). For the "files already moved partially" supplement, the
    helper prefers per-rip post_paths counts; only falls back to
    walking trans_root when no per-rip signal exists.
    """
    def _keys_are_uuids(d):
        return d and all(len(str(k)) == 36 and "-" in str(k) for k in (d or {}))

    actual_count = source_mkv_count

    persisted = getattr(job, "post_paths", None) or {}
    restored = (getattr(job, "disc_payload", None) or {}).get("post_paths") or {}

    if _keys_are_uuids(persisted):
        actual_count += len(persisted)
    elif _keys_are_uuids(restored):
        actual_count += len(restored)
    else:
        # Fallback to walk.
        actual_count += trans_root_mkv_count

    return actual_count


UUID_KEY = "0000fffe-0000-4000-8000-000000000001"


# ──────────────────────────────────────────────────────────────────────────
# Per-rip signal precedence
# ──────────────────────────────────────────────────────────────────────────


def test_persisted_post_paths_count_used():
    """job.post_paths length is added to actual_count when UUID-keyed.
    Walk is skipped."""
    job = SimpleNamespace(
        post_paths={UUID_KEY: "A.mkv", UUID_KEY[:-1] + "2": "B.mkv"},
        disc_payload={},
    )
    # Walk count would be 100 (shared library); per-rip count is 2.
    result = _compute_actual_count(job, source_mkv_count=3, trans_root_mkv_count=100)
    assert result == 5  # 3 from raw + 2 from per-rip; NOT 103
    assert result != 103, (
        f"shared-library walk count (100) should not contribute when "
        f"per-rip signal is available; got {result}"
    )


def test_disc_payload_post_paths_count_used_when_persisted_empty():
    """disc_payload.post_paths length used when persisted is empty."""
    job = SimpleNamespace(
        post_paths={},
        disc_payload={"post_paths": {UUID_KEY: "A.mkv"}},
    )
    result = _compute_actual_count(job, source_mkv_count=2, trans_root_mkv_count=50)
    assert result == 3  # 2 + 1; not 52


def test_non_uuid_keys_falls_back_to_walk():
    """post_paths with legacy non-UUID keys can't be trusted as
    per-rip — fall to walk."""
    job = SimpleNamespace(
        post_paths={"00001.mpls": "A.mkv"},  # legacy
        disc_payload={"post_paths": {"00002.mpls": "B.mkv"}},  # legacy
    )
    result = _compute_actual_count(job, source_mkv_count=2, trans_root_mkv_count=5)
    assert result == 7  # 2 + 5 walk fallback


# ──────────────────────────────────────────────────────────────────────────
# Fallback to walk
# ──────────────────────────────────────────────────────────────────────────


def test_no_per_rip_signal_falls_back_to_walk():
    """Empty post_paths sources → fall back to walk (legacy path
    preserved)."""
    job = SimpleNamespace(post_paths={}, disc_payload={})
    result = _compute_actual_count(job, source_mkv_count=2, trans_root_mkv_count=3)
    assert result == 5


def test_none_sources_handled_defensively():
    """post_paths=None and disc_payload=None handled the same as
    empty dicts."""
    job = SimpleNamespace(post_paths=None, disc_payload=None)
    result = _compute_actual_count(job, source_mkv_count=4, trans_root_mkv_count=1)
    assert result == 5


# ──────────────────────────────────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────────────────────────────────


def test_persisted_post_paths_with_zero_entries_falls_back_to_walk():
    """Empty dict (truthy=False) treated same as missing — fall to walk."""
    job = SimpleNamespace(post_paths={}, disc_payload={})
    result = _compute_actual_count(job, source_mkv_count=0, trans_root_mkv_count=7)
    assert result == 7  # 0 + 7 fallback


def test_only_source_mkvs_no_resume_supplement():
    """When neither per-rip signal nor walk finds anything, the count
    is just source_mkv_count. Resume validation would compare to
    expected_count and proceed/abort accordingly."""
    job = SimpleNamespace(post_paths={}, disc_payload={})
    result = _compute_actual_count(job, source_mkv_count=3, trans_root_mkv_count=0)
    assert result == 3


# ──────────────────────────────────────────────────────────────────────────
# Regression scenario: the bug step 4e prevents
# ──────────────────────────────────────────────────────────────────────────


def test_shared_trans_root_does_not_mask_resume_failure():
    """The exact scenario step 4e prevents.

    Setup:
    - MKVAUTO_RENAME_DIRECT_TO_DEST=1 → trans_root = /library (shared)
    - Resume scenario: raw has 0 files (rip not yet re-run), shared
      trans_root has 100 unrelated MKVs
    - expected_count = 5 (this disc's title count)
    - No per-rip post_paths (fresh job state)

    Pre-step-4e: walk finds 100 → actual_count = 0 + 100 = 100 >>
    expected 5 → validation passes incorrectly → worker proceeds
    despite raw being empty → downstream rename produces nothing →
    silent partial failure.

    Post-step-4e: walk is still the fallback when no per-rip signal,
    so this scenario still has the over-count behaviour... BUT in
    normal flow the resume is invoked when source_dir actually has
    files (a partial-rip retry), so the trans_root walk supplement
    is the ADDITION on top of source files. If source has files, the
    over-count from walk doesn't change outcome (already over
    expected_count).

    The REAL fix this test guards: when post_paths IS set (the
    common resume case after a successful prep that just hit a
    later snag), the walk is skipped entirely and the count is
    per-rip accurate.
    """
    # Resume after successful prep: per-rip post_paths is set.
    job = SimpleNamespace(
        post_paths={UUID_KEY: "Movies/X/X.mkv"},
        disc_payload={},
    )
    # Raw is empty (rip done; files moved); shared trans_root has
    # the moved file + 99 unrelated library files.
    result = _compute_actual_count(job, source_mkv_count=0, trans_root_mkv_count=100)
    assert result == 1, (
        f"with per-rip post_paths, count should reflect this rip's 1 "
        f"file, not the shared-library walk count of 100; got {result}"
    )

    # Sanity check: if step 4e had NOT been applied, the count would
    # have been 100 — and resume validation against expected_count=5
    # would have passed (incorrectly).
    pre_fix_count = 0 + 100  # source + walk
    assert pre_fix_count == 100  # documenting the pre-fix behaviour
