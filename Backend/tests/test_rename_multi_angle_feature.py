"""#903: the rename must find a multi-angle main feature.

Star Wars: A New Hope, prod 2026-10-13. The disc scan lists the main feature
(00850.mpls, a 9-angle playlist) at title index 10, but MakeMKV numbers the
rip by its own index and wrote it as ``_t02.mkv``. ``_rename_movie`` looked
``_t02`` up in the scan's title map, found nothing, and skipped the file at
debug level. Only the backdrop was renamed and transferred, the job reported
success, and Finish deleted the 42 GB rip. Five films in one evening.

The rip's own record (``final_paths``: title_id -> ripped filename) is the
authority for which file is which title; the scan index is a fallback.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from core.disc import Disc

MAIN = b"MAIN-FEATURE" * 600
BACKDROP = b"backdrop" * 50
JUNK = b"j" * 40

# The real scan: indices 0, 1, 10 (main, 00850.mpls), 11, 16 (the feature's raw stream).
SCAN = {0: {"file": "00650.mpls"}, 1: {"file": "00020.mpls"}, 10: {"file": "00850.mpls"},
        11: {"file": "00011.m2ts"}, 16: {"file": "00301.m2ts"}}
# What the rip wrote: the feature as _t02 (angle 1), everything else under its scan index.
RIP = {"00650.mpls": ("A New Hope_t00.mkv", JUNK), "00020.mpls": ("A New Hope_t01.mkv", BACKDROP),
       "00850.mpls": ("A New Hope_t02.mkv", MAIN), "00011.m2ts": ("A New Hope_t11.mkv", JUNK),
       "00301.m2ts": ("A New Hope_t16.mkv", JUNK)}
TYPES = {"00650.mpls": "ignore", "00020.mpls": "Backdrop", "00850.mpls": "MainMovie",
         "00011.m2ts": "ignore", "00301.m2ts": "ignore"}


def _setup(tmp_path, scan=SCAN, rip=RIP):
    origin = tmp_path / "raw"
    origin.mkdir()
    for name, content in rip.values():
        (origin / name).write_bytes(content)
    show = tmp_path / "transient" / "Movies" / "Star Wars (1977)"
    show.mkdir(parents=True)

    disc = Disc(disc_num="1", mount_point="/dev/sr0")
    disc.movie_name = "Star Wars"
    disc.resolution = "1080p"
    disc.title_type = "Movie"
    disc.titles = dict(scan)
    disc.db_mapping = {src: {"type": TYPES[src], "title": "Star Wars" if TYPES[src] == "MainMovie" else None}
                       for src in rip}
    ids = {src: str(uuid.uuid4()) for src in rip}
    kwargs = dict(
        final_paths={ids[src]: rip[src][0] for src in rip},                 # the rip's own record
        title_id_to_type={ids[src]: TYPES[src] for src in rip},
        title_id_to_title={ids[src]: ("Star Wars" if TYPES[src] == "MainMovie" else "Backdrop" if TYPES[src] == "Backdrop" else None) for src in rip},
        title_id_to_source_file={ids[src]: src for src in rip},
        movie_name="Star Wars", production_year=1977, media_server="plex",
        transient_root=tmp_path / "transient",   # paths are recorded relative to it, as in production
    )
    return disc, origin, show, ids, kwargs


def _written(show: Path) -> dict[str, bytes]:
    return {p.relative_to(show).as_posix(): p.read_bytes() for p in show.rglob("*.mkv")}


def test_multi_angle_feature_numbered_by_the_rip_is_renamed(tmp_path):
    disc, origin, show, ids, kwargs = _setup(tmp_path)
    renamed = disc._rename_movie(origin_folder=str(origin), show_folder=str(show), **kwargs)

    assert set(renamed) == {ids["00850.mpls"], ids["00020.mpls"]}, renamed
    out = _written(show)
    assert MAIN in out.values(), f"the main feature was not renamed: {sorted(out)}"
    assert BACKDROP in out.values()
    assert disc.unmatched_outputs == []
    assert not (origin / "A New Hope_t02.mkv").exists()        # moved, not copied


def test_rip_record_beats_a_scan_index_that_exists_for_another_title(tmp_path):
    """Revenge of the Sith wrote its feature as _t03; a disc whose scan has a
    title #3 would have renamed the 42 GB feature as that other title."""
    scan = {**SCAN, 3: {"file": "00011.m2ts"}}       # scan index 3 is an ignored stub
    rip = {**RIP, "00850.mpls": ("A New Hope_t03.mkv", MAIN)}
    disc, origin, show, ids, kwargs = _setup(tmp_path, scan=scan, rip=rip)
    renamed = disc._rename_movie(origin_folder=str(origin), show_folder=str(show), **kwargs)

    assert ids["00850.mpls"] in renamed
    main_rel = renamed[ids["00850.mpls"]]
    assert MAIN in _written(show).values()
    assert "Other" not in main_rel and "Extras" not in main_rel, main_rel   # named as the feature, not a stub


def test_a_file_in_neither_record_is_reported_not_silently_dropped(tmp_path):
    disc, origin, show, ids, kwargs = _setup(tmp_path)
    (origin / "A New Hope_t07.mkv").write_bytes(b"stray")      # no scan #7, not in the rip's record
    logs = []
    disc.log_fn = logs.append
    disc._rename_movie(origin_folder=str(origin), show_folder=str(show), **kwargs)

    assert disc.unmatched_outputs == ["A New Hope_t07.mkv"]
    assert any("could not be matched" in m for m in logs)
    assert (origin / "A New Hope_t07.mkv").exists()             # left in raw/


def test_without_a_rip_record_the_scan_index_still_works(tmp_path):
    """Legacy jobs with no ripped_files: files numbered by the scan rename as before."""
    rip = {src: (f"A New Hope_t{idx:02d}.mkv", RIP[src][1]) for idx, info in SCAN.items() for src in [info["file"]]}
    disc, origin, show, ids, kwargs = _setup(tmp_path, rip=rip)
    kwargs["final_paths"] = None
    disc._rename_movie(origin_folder=str(origin), show_folder=str(show), **kwargs)
    out = _written(show)
    assert MAIN in out.values() and BACKDROP in out.values()
    assert disc.unmatched_outputs == []


def test_series_rename_finds_an_episode_numbered_by_the_rip(tmp_path):
    """Same bug in ``_rename_series``: it warned and skipped a file whose
    ``_tNN`` had no scan title, without consulting the rip's record."""
    import uuid as _uuid

    source = tmp_path / "raw"
    source.mkdir()
    ep = source / "Rebels_t02.mkv"                      # the rip's numbering
    ep.write_bytes(MAIN)
    show = tmp_path / "dest" / "Star Wars Rebels (2014)"
    show.mkdir(parents=True)

    disc = Disc("0", "/dev/dvd")
    disc.titles = {10: {"file": "00100.mpls"}}          # the scan's numbering
    disc.db_mapping = {"00100.mpls": {"season": 3, "episode": 1, "episode_name": "Steps Into Shadow",
                                      "type": "Episode", "format": "MainFeature"}}
    disc.title_type = "Series"
    disc.resolution = "1080p"
    disc.movie_name = "Star Wars Rebels"
    disc.errors = {}
    tid = str(_uuid.uuid4())
    renamed = disc._rename_series(
        str(source), str(show), movie_name="Star Wars Rebels", production_year=2014,
        source_hashes=None, transient_root=None, progress_cb=lambda _d, _t, _f: None,
        final_paths={tid: "Rebels_t02.mkv"}, title_id_to_source_file={tid: "00100.mpls"},
        title_id_to_type={tid: "Episode"},
    )
    written = sorted(p.name for p in (show / "Season 03").glob("*.mkv"))
    assert written and "s03e01" in written[0].lower(), (written, renamed)
    assert not ep.exists() and disc.unmatched_outputs == []
