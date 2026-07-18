"""
Coverage for the ``dest_root`` parameter on ``Disc.rename_outputs`` (#365
cleanup — transient/ drop foundation).

The parameter is purely additive: when not supplied (the default for all
existing callers), the function picks ``jobs/<job_id>/transient/`` exactly
as before. When supplied, the destination root is the given path.

These tests pin the contract so the next-stage transient/ drop work can
flip callers from "use the default" to "pass an explicit dest_root" with
confidence that the parameter is wired through end-to-end.
"""
import os
from pathlib import Path
import uuid

import pytest

from core.disc import Disc


def _seed_minimal_disc(disc_num="1", mount_point="/mnt/sr0"):
    """Disc with the minimum metadata rename_outputs needs to compute a
    destination — movie_name + title_type."""
    d = Disc(disc_num, mount_point)
    d.movie_name = "Test Movie"
    d.title_type = "movie"  # Drives `_is_series()` to False → movie path
    d.disc_slug = "test-movie"
    d.resolution = "1080p"
    return d


@pytest.fixture
def empty_source_dir(tmp_path, monkeypatch):
    """A source directory with no MKV files. rename_outputs returns an
    empty dict; the value we care about for these tests is what
    destination directory it created."""
    src = tmp_path / "raw"
    src.mkdir()
    # Make MAKEMKV_LIBRARY_ROOT a tmp area so legacy fallback doesn't
    # touch the real home dir if our test hits it.
    monkeypatch.setenv("MAKEMKV_LIBRARY_ROOT", str(tmp_path / "library"))
    return src


def _capture_jobs_root(tmp_path, monkeypatch):
    """Pin resolve_jobs_root to a tmp directory so we can assert the
    default branch writes there."""
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir()
    monkeypatch.setattr("core.utils.resolve_jobs_root", lambda _: jobs_root)
    return jobs_root


# ──────────────────────────────────────────────────────────────────────────
# Default branch (no dest_root) preserves the pre-collapse layout
# ──────────────────────────────────────────────────────────────────────────


def test_default_dest_writes_to_jobs_transient(
    tmp_path, monkeypatch, empty_source_dir,
):
    """Without dest_root, rename_outputs creates the destination under
    jobs/<job_id>/transient/ — the pre-collapse default."""
    jobs_root = _capture_jobs_root(tmp_path, monkeypatch)
    disc = _seed_minimal_disc()
    job_id = str(uuid.uuid4())

    disc.rename_outputs(
        base_directory=str(empty_source_dir),
        job_id=job_id,
        release_type="movie",
        movie_name="Test Movie",
        production_year=2024,
    )

    expected = jobs_root / job_id / "transient" / "Movies" / "Test Movie (2024)"
    assert expected.exists(), (
        f"default branch should write under jobs/<id>/transient/; expected "
        f"{expected} to exist after rename_outputs"
    )


# ──────────────────────────────────────────────────────────────────────────
# dest_root override path (the new affordance)
# ──────────────────────────────────────────────────────────────────────────


def test_dest_root_override_writes_to_given_path(
    tmp_path, monkeypatch, empty_source_dir,
):
    """When dest_root is supplied, rename_outputs uses it verbatim as
    the destination root — bypassing the jobs/<id>/transient/ default.

    This is the affordance the next-stage transient/ drop work uses to
    redirect rename outputs to the final destination (or pre-transfer
    staging area) instead of the historical transient/ directory."""
    _capture_jobs_root(tmp_path, monkeypatch)  # default would land here if used
    custom_dest = tmp_path / "library_staging"
    disc = _seed_minimal_disc()
    job_id = str(uuid.uuid4())

    disc.rename_outputs(
        base_directory=str(empty_source_dir),
        job_id=job_id,
        release_type="movie",
        movie_name="Test Movie",
        production_year=2024,
        dest_root=custom_dest,
    )

    expected = custom_dest / "Movies" / "Test Movie (2024)"
    assert expected.exists()
    # And the default location must NOT have been created.
    default_loc = tmp_path / "jobs" / job_id / "transient"
    assert not default_loc.exists(), (
        "supplying dest_root should bypass the jobs/<id>/transient/ "
        "default — saw the default location created anyway"
    )


def test_dest_root_accepts_string_path(
    tmp_path, monkeypatch, empty_source_dir,
):
    """dest_root accepts a str (cast to Path inside) — not just Path
    instances. Defensive: callers that build paths via os.path.join
    pass strings."""
    _capture_jobs_root(tmp_path, monkeypatch)
    custom_dest = tmp_path / "string_dest"
    disc = _seed_minimal_disc()
    job_id = str(uuid.uuid4())

    disc.rename_outputs(
        base_directory=str(empty_source_dir),
        job_id=job_id,
        release_type="movie",
        movie_name="Test Movie",
        production_year=2024,
        dest_root=str(custom_dest),  # ← string, not Path
    )

    expected = custom_dest / "Movies" / "Test Movie (2024)"
    assert expected.exists()


def test_dest_root_series_branch(
    tmp_path, monkeypatch, empty_source_dir,
):
    """Series profile uses the same dest_root, organized under Series/
    instead of Movies/. The dest_root override applies symmetrically."""
    _capture_jobs_root(tmp_path, monkeypatch)
    custom_dest = tmp_path / "series_dest"
    disc = _seed_minimal_disc()
    disc.title_type = "Series"
    disc.movie_name = "Test Series"

    disc.rename_outputs(
        base_directory=str(empty_source_dir),
        job_id=str(uuid.uuid4()),
        release_type="series",
        movie_name="Test Series",
        production_year=2024,
        dest_root=custom_dest,
    )

    # folder_name still uses (year) — that's the existing rename_outputs
    # convention regardless of profile.
    expected = custom_dest / "Series" / "Test Series (2024)"
    assert expected.exists()
