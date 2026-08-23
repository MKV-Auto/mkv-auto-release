"""#831 — extras on sibling discs of one release must not clobber each other.

"Rebels Recon – Play All" on all four Season Two discs renders the same
filename on each; the transfer copies every job's tree into one library
folder and the last disc wins. Disc N's extra gets " (Disc N)" when a
lower-numbered sibling already uses the name.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.disc import Disc
from core.extra_name_collisions import (
    disambiguate_extra_name,
    extra_name_key,
    reserved_extra_names_for_disc,
)


def _title(name, type_="Featurette"):
    return SimpleNamespace(title=name, type=type_)


def _disc(id_, number, titles):
    return SimpleNamespace(id=id_, disc_number=number, titles=titles, release=None)


def _release(*discs):
    rel = SimpleNamespace(discs=list(discs))
    for d in discs:
        d.release = rel
    return rel


def test_key_is_case_and_suffix_insensitive():
    assert extra_name_key("Rebels Recon - Play All") == extra_name_key("rebels recon - play all")
    assert extra_name_key("Rebels Recon - Play All (Disc 2)") == extra_name_key("Rebels Recon - Play All")
    assert extra_name_key("") is None and extra_name_key(None) is None


def test_reserved_names_come_only_from_lower_numbered_siblings():
    d1 = _disc("a", 1, [_title("Rebels Recon - Play All"), _title("Gag Reel")])
    d2 = _disc("b", 2, [_title("Rebels Recon - Play All")])
    d3 = _disc("c", 3, [_title("Rebels Recon - Play All"), _title("Rebels Recon - Play All")])
    d4 = _disc("d", 4, [_title("Something Else"), _title("An Episode", type_="Episode")])
    _release(d1, d2, d3, d4)
    assert reserved_extra_names_for_disc(d1) == set()
    assert reserved_extra_names_for_disc(d2) == {"rebels recon - play all", "gag reel"}
    assert reserved_extra_names_for_disc(d3) == {"rebels recon - play all", "gag reel"}
    # Episodes are not extras and never reserve a name.
    d5 = _disc("e", 5, [])
    _release(d1, d4, d5)
    assert reserved_extra_names_for_disc(d5) == {"rebels recon - play all", "gag reel", "something else"}


def test_disc_without_number_or_release_reserves_nothing():
    lonely = _disc("x", None, [])
    assert reserved_extra_names_for_disc(lonely) == set()
    lonely.disc_number = 2
    assert reserved_extra_names_for_disc(lonely) == set()  # no release


def test_disambiguate_appends_once_and_only_on_collision():
    reserved = {"rebels recon - play all"}
    assert disambiguate_extra_name("Rebels Recon - Play All", reserved, 2) == "Rebels Recon - Play All (Disc 2)"
    assert disambiguate_extra_name("Rebels Recon - Play All (Disc 2)", reserved, 2) == "Rebels Recon - Play All (Disc 2)"
    assert disambiguate_extra_name("Gag Reel", reserved, 2) == "Gag Reel"
    assert disambiguate_extra_name("Rebels Recon - Play All", reserved, None) == "Rebels Recon - Play All"
    assert disambiguate_extra_name("Rebels Recon - Play All", set(), 2) == "Rebels Recon - Play All"


@pytest.fixture
def temp_dirs(tmp_path):
    source_dir = tmp_path / "source"
    dest_dir = tmp_path / "dest"
    source_dir.mkdir()
    dest_dir.mkdir()
    return source_dir, dest_dir


def _series_disc_with_extra(name: str) -> Disc:
    disc = Disc("0", "/dev/dvd")
    disc.titles = {1: {"file": "title-7"}}
    disc.db_mapping = {
        "title-7": {"season": 2, "episode": None, "episode_name": name, "type": "Featurette", "format": ""},
    }
    disc.title_type = "Series"
    disc.resolution = "480p"
    disc.movie_name = "Star Wars Rebels"
    disc.errors = {}
    return disc


def _run_series(disc, source_dir, dest_dir, **kw):
    show_folder = dest_dir / "Star Wars Rebels (2014)"
    show_folder.mkdir(parents=True, exist_ok=True)
    return disc._rename_series(
        str(source_dir), str(show_folder),
        movie_name="Star Wars Rebels", production_year=2014,
        source_hashes=None, transient_root=None, progress_cb=lambda *_: None,
        **kw,
    )


def test_series_renamer_suffixes_colliding_extra_only(temp_dirs):
    source_dir, dest_dir = temp_dirs
    (source_dir / "00001_t1.mkv").write_bytes(b"play all")
    disc = _series_disc_with_extra("Rebels Recon - Play All")
    _run_series(disc, source_dir, dest_dir, disc_number=2,
                reserved_extra_names={"rebels recon - play all"})
    produced = sorted(p.name for p in dest_dir.rglob("*.mkv"))
    assert produced == ["Rebels Recon - Play All (Disc 2).480p.mkv"], produced

    # Same disc, nothing reserved → plain name.
    (source_dir / "00001_t1.mkv").write_bytes(b"play all")
    disc2 = _series_disc_with_extra("Rebels Recon - Play All")
    (dest_dir / "b").mkdir()
    _run_series(disc2, source_dir, dest_dir / "b", disc_number=2, reserved_extra_names=set())
    produced2 = sorted(p.name for p in (dest_dir / "b").rglob("*.mkv"))
    assert produced2 == ["Rebels Recon - Play All.480p.mkv"], produced2
