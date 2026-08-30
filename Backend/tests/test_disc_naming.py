"""#845 — auto disc name/slug from labeled identity; #846 — disc_season."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

from core.disc_naming import (
    is_auto_disc_name,
    labeled_disc_name,
    refresh_auto_disc_identity,
)


def _disc(movie_name=None, tmdb_type="movie", fmt=None, disc_number=None,
          season=None, disc_name=None, disc_slug=None, info_title=None):
    movie = SimpleNamespace(name=movie_name, tmdb_type=tmdb_type) if movie_name else None
    release = SimpleNamespace(movie=movie) if movie else None
    return SimpleNamespace(
        id="d1", release=release, format=fmt, disc_number=disc_number,
        label_draft=({"primary_season": season} if season is not None else {}),
        disc_name=disc_name, disc_slug=disc_slug, info_title=info_title,
    )


def test_conventions_match_prod_formatting():
    # movies: "{Movie} - {Format}"
    assert labeled_disc_name(_disc("Thor: Ragnarok", "movie", fmt="blu-ray")) == "Thor: Ragnarok - Blu-Ray"
    assert labeled_disc_name(_disc("Iron Man 3", "movie", fmt="DVD")) == "Iron Man 3 - DVD"
    # series: "{Show}: Season {N} - Disc {M} - {Format}"
    assert labeled_disc_name(_disc("Star Wars Rebels", "tv", fmt="DVD", disc_number=2, season=2)) \
        == "Star Wars Rebels: Season 2 - Disc 2 - DVD"
    # the Clone Wars case: multi-season box, disc's own season names the disc
    assert labeled_disc_name(_disc("Star Wars: The Clone Wars", "tv", fmt="DVD", disc_number=4, season=2)) \
        == "Star Wars: The Clone Wars: Season 2 - Disc 4 - DVD"
    # degenerate forms stay sane
    assert labeled_disc_name(_disc("Thor", "movie")) == "Thor"
    assert labeled_disc_name(_disc("Rebels", "tv", disc_number=1)) == "Rebels - Disc 1"
    assert labeled_disc_name(_disc(None)) is None


def test_auto_detection_never_claims_user_names():
    for auto in (None, "", "DVD", "Blu-Ray", "bluray", "UHD"):
        assert is_auto_disc_name(_disc("Thor", disc_name=auto)), auto
    # scan-time default is auto
    assert is_auto_disc_name(_disc("Thor", fmt="DVD", info_title="LOGICAL_VOLUME", disc_name="LOGICAL_VOLUME - DVD"))
    # a previous convention output is auto (regenerates when identity changes)
    assert is_auto_disc_name(_disc("Thor", fmt="DVD", disc_name="Thor - DVD"))
    # anything the user typed is not
    assert not is_auto_disc_name(_disc("Tropic Thunder", fmt="Blu-Ray", disc_name="Tropic Thunder - Director's Cut"))
    assert not is_auto_disc_name(_disc("Thor", disc_name="My special disc"))


def test_refresh_replaces_auto_and_tracks_slug():
    d = _disc("Star Wars Rebels", "tv", fmt="DVD", disc_number=2, season=2,
              disc_name="DVD", disc_slug="dvd")
    assert refresh_auto_disc_identity(d) is True
    assert d.disc_name == "Star Wars Rebels: Season 2 - Disc 2 - DVD"
    assert d.disc_slug == "star_wars_rebels-_season_2_-_disc_2_-_dvd"
    # idempotent
    assert refresh_auto_disc_identity(d) is False


def test_refresh_leaves_user_slug_and_user_name():
    d = _disc("Thor", "movie", fmt="DVD", disc_name="DVD", disc_slug="my-own-slug")
    assert refresh_auto_disc_identity(d) is True
    assert d.disc_name == "Thor - DVD"
    assert d.disc_slug == "my-own-slug"  # user slug untouched
    u = _disc("Thor", "movie", fmt="DVD", disc_name="Thor SteelBook")
    assert refresh_auto_disc_identity(u) is False
    assert u.disc_name == "Thor SteelBook"


def test_disc_season_only_when_release_spans_seasons(test_db):
    from api import models
    from api.routers.websockets import _build_disc_metadata

    session = test_db()
    try:
        movie = models.Movie(id=str(uuid.uuid4()), name="Clone Wars", tmdb_type="tv")
        rel = models.Release(id=str(uuid.uuid4()), movie_id=movie.id, name="Season 1-5 Collector's Edition",
                             slug=f"cw-{uuid.uuid4().hex[:6]}")
        session.add_all([movie, rel]); session.flush()
        discs = []
        for i, season in enumerate((1, 1, 2), start=1):
            d = models.Disc(id=str(uuid.uuid4()), content_hash=f"cw{i}-{uuid.uuid4().hex[:6]}",
                            release_id=rel.id, disc_number=i, format="DVD",
                            label_draft={"primary_season": season})
            session.add(d); discs.append(d)
        session.commit()
        for d in discs:
            session.refresh(d)
        meta = _build_disc_metadata(discs[2], disc_state="unfinished", db=session)
        assert meta.disc_season == 2
        # Single-season release → no season chip.
        solo_rel = models.Release(id=str(uuid.uuid4()), movie_id=movie.id, name="Complete Season Two",
                                  slug=f"cw2-{uuid.uuid4().hex[:6]}")
        session.add(solo_rel); session.flush()
        solo = models.Disc(id=str(uuid.uuid4()), content_hash=f"cws-{uuid.uuid4().hex[:6]}",
                           release_id=solo_rel.id, disc_number=1, format="DVD",
                           label_draft={"primary_season": 2})
        session.add(solo); session.commit(); session.refresh(solo)
        meta2 = _build_disc_metadata(solo, disc_state="unfinished", db=session)
        assert meta2.disc_season is None
    finally:
        session.close()
