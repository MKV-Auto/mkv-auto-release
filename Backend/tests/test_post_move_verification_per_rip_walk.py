"""
Coverage for the post-move verification per-rip walk (#365 step 4a).

Previously the verification did ``trans_root.rglob("*.mkv")`` to count
files moved by the current rip. That works when ``trans_root`` is a
per-job ``transient/`` directory (the historical default — empty at the
start of each job, contains only this rip's output at the end). It
*fails wildly* when ``trans_root`` is a shared library path (the
``MKVAUTO_RENAME_DIRECT_TO_DEST`` flag's behaviour, step 3b #440) —
rglob then finds every MKV in the library, not just this rip's files,
and the verification reports a wildly wrong count.

The fix walks the keys in ``renamed_paths`` instead — the exact files
``rename_outputs`` actually moved — so the count is per-rip regardless
of what else lives under ``trans_root``.

These tests cover the helper *logic* in isolation (the helper is
embedded inline in ``_run_prep_phase`` so the tests reproduce its
shape rather than invoke it directly). When the verification block
later gets extracted to a named helper (likely in step 4d when all
five trans_root walks get unified), these can move to test that
helper directly.
"""
from pathlib import Path

import pytest


def _verify_per_rip(trans_root: Path, renamed_paths: dict):
    """Reproduces the per-rip walk from
    ``workers.tasks._run_prep_phase`` post-move verification.

    Returns the list of mkv files found under trans_root that match
    entries in renamed_paths. This is the shape the production code
    computes; pinning the behaviour here lets us add safety tests
    without needing the full _run_prep_phase fixture stack."""
    if renamed_paths:
        return [
            trans_root / rel for rel in renamed_paths.values()
            if (trans_root / rel).exists()
        ]
    return list(trans_root.rglob("*.mkv"))


def _seed_mkvs(root: Path, rel_paths: list[str]) -> list[Path]:
    """Create empty .mkv files at each rel_path under root."""
    created = []
    for rel in rel_paths:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        created.append(p)
    return created


# ──────────────────────────────────────────────────────────────────────────
# Per-rip walk (new behaviour)
# ──────────────────────────────────────────────────────────────────────────


def test_walk_returns_only_files_listed_in_renamed_paths(tmp_path):
    """When renamed_paths is supplied, the walk returns ONLY files
    listed there, even if more files exist under trans_root."""
    # Pretend trans_root is a shared library that already has unrelated MKVs.
    _seed_mkvs(tmp_path, [
        "Movies/Other Movie (2020)/Other Movie.mkv",
        "Movies/Some Other Thing/file.mkv",
        "Movies/My Film (2024)/My Film.1080p.mkv",
    ])
    # This rip only moved one file under My Film.
    renamed_paths = {
        "title-1": "Movies/My Film (2024)/My Film.1080p.mkv",
    }
    found = _verify_per_rip(tmp_path, renamed_paths)
    assert len(found) == 1, (
        f"per-rip walk should return only the 1 file listed in "
        f"renamed_paths, not all 3 MKVs under trans_root; got {found}"
    )
    assert found[0] == tmp_path / "Movies/My Film (2024)/My Film.1080p.mkv"


def test_walk_skips_missing_files_in_renamed_paths(tmp_path):
    """When renamed_paths lists files that don't actually exist on
    disk (rename failed mid-batch), the walk skips those and returns
    only the successfully-moved ones. The caller compares count to
    expected and reports the gap."""
    _seed_mkvs(tmp_path, [
        "Movies/Film A/A.mkv",
        # B was supposed to be moved but isn't on disk.
    ])
    renamed_paths = {
        "title-a": "Movies/Film A/A.mkv",
        "title-b": "Movies/Film B/B.mkv",  # NOT on disk
    }
    found = _verify_per_rip(tmp_path, renamed_paths)
    assert len(found) == 1
    assert found[0] == tmp_path / "Movies/Film A/A.mkv"


def test_walk_handles_unicode_paths(tmp_path):
    """Paths with non-ASCII characters round-trip correctly through
    the per-rip walk."""
    _seed_mkvs(tmp_path, [
        "Movies/Amélie Poulain (2001)/Amélie.1080p.mkv",
    ])
    renamed_paths = {
        "title-1": "Movies/Amélie Poulain (2001)/Amélie.1080p.mkv",
    }
    found = _verify_per_rip(tmp_path, renamed_paths)
    assert len(found) == 1


# ──────────────────────────────────────────────────────────────────────────
# Legacy fallback (rglob when no renamed_paths)
# ──────────────────────────────────────────────────────────────────────────


def test_walk_falls_back_to_rglob_when_renamed_paths_empty(tmp_path):
    """When renamed_paths is empty (legacy MAKEMKV_LIBRARY_ROOT path —
    no rename ran), the walk falls back to rglob. This is the only
    way to discover what's on disk in that scenario."""
    _seed_mkvs(tmp_path, [
        "Movies/A/A.mkv",
        "Movies/B/B.mkv",
    ])
    found = _verify_per_rip(tmp_path, renamed_paths={})
    assert len(found) == 2


def test_walk_falls_back_to_rglob_when_renamed_paths_none(tmp_path):
    """None is treated the same as empty dict — fall back to rglob."""
    _seed_mkvs(tmp_path, ["Movies/A/A.mkv"])
    found = _verify_per_rip(tmp_path, renamed_paths=None)
    assert len(found) == 1


# ──────────────────────────────────────────────────────────────────────────
# Safety guard: the previous (rglob-only) approach would have failed
# this case. Documenting the regression we're fixing.
# ──────────────────────────────────────────────────────────────────────────


def test_per_rip_walk_safe_when_trans_root_is_shared_library(tmp_path):
    """Regression: under MKVAUTO_RENAME_DIRECT_TO_DEST=1 + local mode,
    trans_root resolves to the user's library — a directory that may
    contain hundreds of MKVs from prior rips. The pre-fix rglob walk
    would count ALL of them and the verification would fail with a
    spurious "wildly wrong count" error.

    The per-rip walk via renamed_paths only counts what THIS rip
    moved — same answer whether trans_root has 1 file or 10,000."""
    # Simulate a fat library directory.
    _seed_mkvs(tmp_path, [
        f"Movies/Existing Film {i}/Film {i}.mkv" for i in range(100)
    ] + ["Movies/My New Film (2024)/My New Film.1080p.mkv"])

    renamed_paths = {
        "title-1": "Movies/My New Film (2024)/My New Film.1080p.mkv",
    }

    found_per_rip = _verify_per_rip(tmp_path, renamed_paths)
    # Should report 1 (the file this rip moved), not 101.
    assert len(found_per_rip) == 1, (
        "per-rip walk must not count pre-existing library files — "
        f"found {len(found_per_rip)} but only 1 was moved by this rip"
    )

    # Document the regression: the previous rglob-based walk would
    # have returned 101 in this scenario, breaking verification.
    rglob_found = list(tmp_path.rglob("*.mkv"))
    assert len(rglob_found) == 101, (
        "sanity check on the test fixture — rglob should see all 101 "
        "files in the simulated shared library"
    )
