"""Two titles must never resolve to one output file silently.

Regression test for the Star Wars Rebels S3 D1 case: the disc splits one
TMDB episode ("Steps Into Shadow") across two physical files, so both
title rows carry season=3 episode=1 and render the same filename. The
second file used to hit the `elif dst_exists:` branch, be treated as
"already processed", and be skipped — never moved out of transient, never
recorded in renamed_paths, never present in expected_files, with the job
still reporting success.
"""
import pytest

from core.disc import Disc, OutputCollisionError


@pytest.fixture
def temp_dirs(tmp_path):
    source_dir = tmp_path / "source"
    dest_dir = tmp_path / "dest"
    source_dir.mkdir()
    dest_dir.mkdir()
    return source_dir, dest_dir


def _disc_with_two_episodes(ep_a: int, ep_b: int) -> Disc:
    disc = Disc("0", "/dev/dvd")
    disc.titles = {1: {"file": "00100.mpls"}, 2: {"file": "00101.mpls"}}
    disc.db_mapping = {
        "00100.mpls": {
            "season": 3, "episode": ep_a, "episode_name": "Steps Into Shadow",
            "type": "Episode", "format": "MainFeature",
        },
        "00101.mpls": {
            "season": 3, "episode": ep_b, "episode_name": "Steps Into Shadow",
            "type": "Episode", "format": "MainFeature",
        },
    }
    disc.title_type = "Series"
    disc.resolution = "1080p"
    disc.movie_name = "Star Wars Rebels"
    disc.errors = {}
    return disc


def _sources(source_dir):
    a = source_dir / "00001_t1.mkv"
    b = source_dir / "00002_t2.mkv"
    a.write_bytes(b"part one")
    b.write_bytes(b"part two")
    return a, b


def _run(disc, source_dir, dest_dir, progress_cb=lambda _d, _t, _f: None):
    show_folder = dest_dir / "Star Wars Rebels (2014)"
    show_folder.mkdir(parents=True, exist_ok=True)
    return disc._rename_series(
        str(source_dir), str(show_folder),
        movie_name="Star Wars Rebels", production_year=2014,
        source_hashes=None, transient_root=None, progress_cb=progress_cb,
    )


def test_colliding_episode_numbers_raise_instead_of_dropping_a_file(temp_dirs):
    source_dir, dest_dir = temp_dirs
    a, b = _sources(source_dir)
    disc = _disc_with_two_episodes(ep_a=1, ep_b=1)

    with pytest.raises(OutputCollisionError) as exc:
        _run(disc, source_dir, dest_dir)

    msg = str(exc.value)
    assert "same output file" in msg
    # Must name both claimants so the user knows which rows to fix.
    assert "00001_t1.mkv" in msg and "00002_t2.mkv" in msg


def test_distinct_episode_numbers_still_process_both(temp_dirs):
    source_dir, dest_dir = temp_dirs
    a, b = _sources(source_dir)
    disc = _disc_with_two_episodes(ep_a=1, ep_b=2)

    _run(disc, source_dir, dest_dir)

    season = dest_dir / "Star Wars Rebels (2014)" / "Season 03"
    written = sorted(p.name for p in season.glob("*.mkv"))
    assert len(written) == 2, written
    # Both sources actually left the transient folder.
    assert not a.exists() and not b.exists()


def test_files_move_even_without_a_progress_callback(temp_dirs):
    """progress_cb is Optional and defaults to None.

    The move used to sit inside `if progress_cb and total_files > 0:`, so
    omitting the callback moved nothing while still returning cleanly —
    _rename_movie always had it outside the guard. Existing tests worked
    around this by always passing a no-op callback.
    """
    source_dir, dest_dir = temp_dirs
    a, b = _sources(source_dir)
    disc = _disc_with_two_episodes(ep_a=1, ep_b=2)

    _run(disc, source_dir, dest_dir, progress_cb=None)

    season = dest_dir / "Star Wars Rebels (2014)" / "Season 03"
    assert len(list(season.glob("*.mkv"))) == 2
    assert not a.exists() and not b.exists()
