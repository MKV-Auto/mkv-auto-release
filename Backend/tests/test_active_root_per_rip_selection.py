"""
Coverage for the active_root selection (#365 transient/ drop step 4c).

``active_root`` is the directory the rest of ``_run_prep_phase`` reads
from to gather final outputs + compute hashes. Previously it was
computed with a ternary that walked ``trans_root.rglob("*.mkv")`` to
decide whether to use trans_root or fall back to source_dir:

    active_root = trans_root if files_already_moved else (
        trans_root if any(trans_root.rglob("*.mkv")) else (
            source_dir if source_dir and source_dir.exists() else trans_root
        )
    )

The rglob walk is unsafe under the ``MKVAUTO_RENAME_DIRECT_TO_DEST``
flag because trans_root may be a shared library — rglob always sees
MKVs (from prior rips) and we'd never fall back to source_dir even
when rename actually produced nothing for THIS rip.

Step 4c replaces the rglob check with ``bool(renamed_paths)`` — the
per-rip authoritative signal of "did rename move anything for this
job."
"""
from pathlib import Path
from types import SimpleNamespace

import pytest


def _select_active_root(files_already_moved: bool, renamed_paths: dict,
                        trans_root: Path, source_dir: Path | None) -> Path:
    """Reproduces the active_root selection from _run_prep_phase
    after step 4c. Pure function — easy to test branches."""
    if files_already_moved or renamed_paths:
        return trans_root
    if source_dir and source_dir.exists():
        return source_dir
    return trans_root


# ──────────────────────────────────────────────────────────────────────────
# Highest-priority branches: files_already_moved + renamed_paths
# ──────────────────────────────────────────────────────────────────────────


def test_files_already_moved_returns_trans_root(tmp_path):
    """Resume path: files are already at the destination, use it."""
    trans_root = tmp_path / "transient"
    source_dir = tmp_path / "raw"
    source_dir.mkdir()  # exists but irrelevant
    result = _select_active_root(
        files_already_moved=True, renamed_paths={}, trans_root=trans_root, source_dir=source_dir,
    )
    assert result == trans_root


def test_renamed_paths_present_returns_trans_root(tmp_path):
    """Normal path: rename produced output for this rip → read from
    trans_root."""
    trans_root = tmp_path / "transient"
    source_dir = tmp_path / "raw"
    source_dir.mkdir()
    result = _select_active_root(
        files_already_moved=False, renamed_paths={"t1": "Movies/A/A.mkv"},
        trans_root=trans_root, source_dir=source_dir,
    )
    assert result == trans_root


# ──────────────────────────────────────────────────────────────────────────
# Fallback to source_dir when rename produced nothing for this rip
# ──────────────────────────────────────────────────────────────────────────


def test_no_renames_falls_back_to_source_dir(tmp_path):
    """The scenario step 4c fixes: rename produced nothing (e.g. all
    titles ignored), so reads should come from the source dir, NOT
    from trans_root — which might be a shared library with other
    rips' files. The pre-step-4c rglob check would have wrongly
    returned trans_root if any library file matched."""
    trans_root = tmp_path / "transient"
    source_dir = tmp_path / "raw"
    source_dir.mkdir()
    result = _select_active_root(
        files_already_moved=False, renamed_paths={},
        trans_root=trans_root, source_dir=source_dir,
    )
    assert result == source_dir


def test_no_renames_and_no_source_dir_returns_trans_root(tmp_path):
    """Defensive fallback: when source_dir is missing or doesn't exist
    (very rare; would indicate a corrupted job state), return
    trans_root anyway. Downstream code handles the "no files" case."""
    trans_root = tmp_path / "transient"
    # source_dir not created
    source_dir = tmp_path / "raw"
    result = _select_active_root(
        files_already_moved=False, renamed_paths={},
        trans_root=trans_root, source_dir=source_dir,
    )
    assert result == trans_root


def test_no_renames_with_source_dir_none(tmp_path):
    """Defensive: source_dir=None handled the same as a missing dir."""
    trans_root = tmp_path / "transient"
    result = _select_active_root(
        files_already_moved=False, renamed_paths={},
        trans_root=trans_root, source_dir=None,
    )
    assert result == trans_root


# ──────────────────────────────────────────────────────────────────────────
# Regression: the previous rglob check would have failed under flag
# ──────────────────────────────────────────────────────────────────────────


def test_step_4c_safe_when_trans_root_is_shared_library(tmp_path):
    """Documents the regression step 4c prevents.

    Scenario:
    - MKVAUTO_RENAME_DIRECT_TO_DEST=1 → trans_root = /library (shared)
    - This rip's titles all ignored → renamed_paths = {}
    - source_dir = jobs/<id>/raw/ (where the rip output landed)

    Pre-step-4c: ``any(trans_root.rglob("*.mkv"))`` returns True
    because the library has other MKVs. active_root = trans_root.
    Downstream gather_final_outputs walks the library and breaks.

    Post-step-4c: ``bool(renamed_paths)`` is False (empty dict).
    active_root = source_dir. Downstream reads from the rip output
    only. Correct."""
    # Set up a shared trans_root with many pre-existing MKVs.
    trans_root = tmp_path / "library"
    trans_root.mkdir()
    for i in range(10):
        (trans_root / f"Other Film {i}.mkv").touch()

    source_dir = tmp_path / "raw"
    source_dir.mkdir()
    (source_dir / "rip_output.mkv").touch()

    # This rip had no titles to rename.
    result = _select_active_root(
        files_already_moved=False, renamed_paths={},
        trans_root=trans_root, source_dir=source_dir,
    )
    # Should fall back to source_dir, NOT pick up the shared library.
    assert result == source_dir, (
        "step 4c must return source_dir when this rip moved nothing, "
        f"even if the shared trans_root has other MKVs; got {result}"
    )

    # Document the pre-fix behaviour for context:
    pre_fix_would_return = trans_root if any(trans_root.rglob("*.mkv")) else source_dir
    assert pre_fix_would_return == trans_root, (
        "sanity: the pre-step-4c rglob check would have returned trans_root "
        "in this scenario (because the shared library has MKVs)"
    )
