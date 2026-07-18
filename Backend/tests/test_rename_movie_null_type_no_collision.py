"""``_rename_movie`` must produce a unique destination filename for every
input — even when multiple titles arrive with no ``title_type`` and no
resolved ``title_name``.

Earlier revisions fell back to ``movie_name + (year)`` ("avoid Track{tid}"),
which silently collapsed N NULL-typed primaries onto the same destination
path and overwrote each other (the "17/20 collision" regression). The
upstream consensus-fill in ``apply_primary_duplicate_row`` papered over
this by marking the NULL primaries ignore so postprocess skipped them — at
the cost of hiding real episodes (Fallout S2). With both layers fixed,
NULL-typed titles must now fall through to ``Track{tid}`` directly,
matching ``_rename_series`` behavior.
"""
import shutil
import uuid
from pathlib import Path

import pytest

from core.disc import Disc


@pytest.fixture
def four_null_typed_mkvs(tmp_path):
    """Create 4 MKV files matching the rename pattern (``*_tNNN.mkv``)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    paths = []
    for tid in (3, 7, 18, 26):
        p = origin / f"Disc_t{tid:03d}.mkv"
        p.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 32)  # minimal-ish header
        paths.append((tid, p))
    return origin, paths


def test_rename_movie_unique_paths_for_null_typed_titles(four_null_typed_mkvs, tmp_path):
    """4 NULL-typed primaries -> 4 distinct destination filenames.

    Regression guard: dropping the ``movie_name`` fallback for NULL-typed
    titles forces each through to ``Track{tid}``; collision is impossible
    because the tid is part of the input filename and embedded in the
    output base name.
    """
    origin, paths = four_null_typed_mkvs
    show_folder = tmp_path / "show"
    show_folder.mkdir()

    disc = Disc(disc_num="1", mount_point="/dev/sr0")
    disc.movie_name = "Fake Series Disc"
    disc.resolution = "2160p"
    disc.title_type = "Movie"  # forces _rename_movie path
    # Populate the title map _rename_movie needs to resolve `file` per tid.
    disc.titles = {
        tid: {"file": f"clip-{tid}.m2ts"} for tid, _ in paths
    }
    # db_mapping: empty 'type' so the legacy fallback chain has to choose
    # the Track{tid} branch.
    disc.db_mapping = {
        f"clip-{tid}.m2ts": {"type": "", "title": None}
        for tid, _ in paths
    }
    # title_id mapping: all NULL types, no resolved titles.
    title_ids = {tid: str(uuid.uuid4()) for tid, _ in paths}
    title_id_to_type = {tid_uuid: None for tid_uuid in title_ids.values()}
    title_id_to_title = {tid_uuid: None for tid_uuid in title_ids.values()}
    title_id_to_source_file = {
        title_ids[tid]: f"clip-{tid}.m2ts" for tid, _ in paths
    }
    final_paths = {title_ids[tid]: p.name for tid, p in paths}

    disc._rename_movie(
        origin_folder=str(origin),
        show_folder=str(show_folder),
        final_paths=final_paths,
        title_id_to_title=title_id_to_title,
        title_id_to_type=title_id_to_type,
        title_id_to_source_file=title_id_to_source_file,
        movie_name="Fake Series Disc",
        production_year=2026,
        media_server="plex",
    )

    written = sorted(p.name for p in show_folder.rglob("*.mkv"))
    assert len(written) == 4, (
        f"expected 4 distinct output files, got {len(written)}: {written}. "
        "If you see 1, the movie_name fallback is back — multiple NULL-typed "
        "titles collapsed onto the same destination."
    )
    assert len(set(written)) == 4, (
        f"output filenames must all differ; got duplicates: {written}"
    )
    # Every written name must contain a unique Track{tid} marker — confirms
    # the fallback chain reached the Track{tid} branch (not the dropped
    # movie_name branch).
    for tid, _ in paths:
        assert any(f"Track{tid}" in name for name in written), (
            f"missing Track{tid} in outputs: {written}"
        )
