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
          season=None, disc_name=None, disc_slug=None, info_title=None,
          release_name=None, sibling_seasons=None):
    """sibling_seasons: [(disc_number, season), ...] for the release's other
    discs — drives the within-season ordinal."""
    movie = SimpleNamespace(name=movie_name, tmdb_type=tmdb_type) if movie_name else None
    release = SimpleNamespace(movie=movie, name=release_name, discs=[]) if movie else None
    disc = SimpleNamespace(
        id="d1", release=release, format=fmt, disc_number=disc_number,
        label_draft=({"primary_season": season} if season is not None else {}),
        disc_name=disc_name, disc_slug=disc_slug, info_title=info_title,
    )
    if release is not None:
        release.discs = [disc] + [
            SimpleNamespace(id=f"sib{i}", disc_number=n,
                            label_draft={"primary_season": s})
            for i, (n, s) in enumerate(sibling_seasons or [])
        ]
    return disc


def test_conventions_match_prod_formatting():
    # movies: "{Movie} - {Format}"
    assert labeled_disc_name(_disc("Thor: Ragnarok", "movie", fmt="blu-ray")) == "Thor: Ragnarok - Blu-Ray"
    assert labeled_disc_name(_disc("Iron Man 3", "movie", fmt="DVD")) == "Iron Man 3 - DVD"
    # series: "{Show}: Season {N} - Disc {M} - {Format}" — M is the
    # within-season ordinal (the user's call: disc_number stays the boxset
    # position; the name counts inside the season).
    assert labeled_disc_name(_disc("Star Wars Rebels", "tv", fmt="DVD", disc_number=2, season=2,
                                   sibling_seasons=[(1, 2), (3, 2), (4, 2)])) \
        == "Star Wars Rebels: Season 2 - Disc 2 - DVD"
    # the Clone Wars case: multi-season box. disc_number stays the RELEASE-
    # wide position (the boxset total); the NAME uses the within-season
    # ordinal — "Season 4 - Disc 1", not "Season 4 - Disc 12".
    assert labeled_disc_name(_disc(
        "Star Wars: The Clone Wars", "tv", fmt="DVD", disc_number=12, season=4,
        sibling_seasons=[(1, 1), (2, 1), (13, 4), (14, 4)],
    )) == "Star Wars: The Clone Wars: Season 4 - Disc 1 - DVD"
    # …and the box's disc 13 (second S4 disc) names as Disc 2.
    assert labeled_disc_name(_disc(
        "Star Wars: The Clone Wars", "tv", fmt="DVD", disc_number=13, season=4,
        sibling_seasons=[(1, 1), (2, 1), (12, 4), (14, 4)],
    )) == "Star Wars: The Clone Wars: Season 4 - Disc 2 - DVD"
    # No sibling info (unsaved disc, plain fixture): fall back to disc_number.
    assert labeled_disc_name(_disc("Star Wars: The Clone Wars", "tv", fmt="DVD", disc_number=4, season=2)) \
        == "Star Wars: The Clone Wars: Season 2 - Disc 1 - DVD"
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
              disc_name="DVD", disc_slug="dvd",
              sibling_seasons=[(1, 2), (3, 2), (4, 2)])
    assert refresh_auto_disc_identity(d) is True
    assert d.disc_name == "Star Wars Rebels: Season 2 - Disc 2 - DVD"
    assert d.disc_slug == "star_wars_rebels-_season_2_-_disc_2_-_dvd"
    # idempotent
    assert refresh_auto_disc_identity(d) is False


def test_prior_convention_renders_stay_reclaimable():
    """Live incident: 'Star Wars: The Clone Wars - Disc 5 - DVD' (generated
    before the season was set) froze once identity changed, because only the
    CURRENT render was recognized as auto. Any past render must stay
    machine-owned."""
    d = _disc("Star Wars: The Clone Wars", "tv", fmt="DVD", disc_number=12, season=4,
              disc_name="Star Wars: The Clone Wars - Disc 5 - DVD",
              sibling_seasons=[(5, None), (13, 4)])
    # Hmm — 'Disc 5' was the disc's OLD number; recognize via old-number variant:
    d.release.discs[1].disc_number = 5
    assert is_auto_disc_name(_disc(
        "Star Wars: The Clone Wars", "tv", fmt="DVD", disc_number=12, season=4,
        disc_name="Star Wars: The Clone Wars - Disc 12 - DVD",
        sibling_seasons=[(13, 4)],
    ))
    # Season-less past render with the CURRENT number is reclaimable.
    assert refresh_auto_disc_identity(_disc(
        "Star Wars: The Clone Wars", "tv", fmt="DVD", disc_number=12, season=4,
        disc_name="Star Wars: The Clone Wars - Disc 12 - DVD",
        sibling_seasons=[(13, 4)],
    )) is True


def test_discdb_style_composite_is_machine_owned():
    """Live incident: '{movie} - {release name} - Season 4 - Disc 1 - DVD'
    (TheDiscDB's display convention) overwrote the convention name and then
    masqueraded as user-typed, freezing auto-naming for the disc."""
    d = _disc("Star Wars: The Clone Wars", "tv", fmt="DVD", disc_number=12, season=4,
              release_name="Season 1-5 Collector's Edition",
              disc_name="Star Wars: The Clone Wars - Season 1-5 Collector's Edition - Season 4 - Disc 1 - DVD",
              disc_slug="star_wars-_the_clone_wars_-_season_1-5_collector's_edition_-_season_4_-_disc_1_-_dvd",
              sibling_seasons=[(13, 4)])
    assert is_auto_disc_name(d)
    assert refresh_auto_disc_identity(d) is True
    assert d.disc_name == "Star Wars: The Clone Wars: Season 4 - Disc 1 - DVD"
    # A name that just happens to start with the movie but not the release
    # stays the user's.
    assert not is_auto_disc_name(_disc(
        "Thor", "movie", fmt="DVD", release_name="Phase One",
        disc_name="Thor - My Custom Cut"))


def test_movie_prefixed_release_composite_is_machine_owned():
    """rc.4 rig: real imports store the release name already movie-prefixed
    ('Star Wars: The Clone Wars - Season 1-5 Collector's Edition'), so the
    composite disc name is '{release.name} - Season N - Disc M - Fmt' — the
    '{movie} - {release}' guess double-counted the movie and never matched."""
    d = _disc("Star Wars: The Clone Wars", "tv", fmt="DVD", disc_number=12, season=4,
              release_name="Star Wars: The Clone Wars - Season 1-5 Collector's Edition",
              disc_name="Star Wars: The Clone Wars - Season 1-5 Collector's Edition - Season 4 - Disc 1 - DVD",
              disc_slug="star_wars-_the_clone_wars_-_season_1-5_collectors_edition_-_season_4_-_disc_1_-_dvd",
              sibling_seasons=[(13, 4)])
    assert is_auto_disc_name(d)
    assert refresh_auto_disc_identity(d) is True
    assert d.disc_name == "Star Wars: The Clone Wars: Season 4 - Disc 1 - DVD"
    # The release-name-alone prefix is honored ONLY when it is itself
    # movie-prefixed — a user name starting with a plain release name stays.
    assert not is_auto_disc_name(_disc(
        "Thor", "movie", fmt="DVD", release_name="Phase One",
        disc_name="Phase One - Steelbook"))


def test_format_change_rerenders_the_name():
    """rc.1 rig finding: flipping the format froze the name, because variants
    were rendered only with the CURRENT format — the '- DVD' render stopped
    matching the moment format became Blu-Ray."""
    d = _disc("Star Wars: The Clone Wars", "tv", fmt="blu-ray", disc_number=14, season=4,
              disc_name="Star Wars: The Clone Wars: Season 4 - Disc 3 - DVD",
              disc_slug="star_wars-_the_clone_wars-_season_4_-_disc_3_-_dvd",
              sibling_seasons=[(12, 4), (13, 4)])
    assert is_auto_disc_name(d)
    assert refresh_auto_disc_identity(d) is True
    assert d.disc_name == "Star Wars: The Clone Wars: Season 4 - Disc 3 - Blu-Ray"
    assert d.disc_slug == "star_wars-_the_clone_wars-_season_4_-_disc_3_-_blu-ray"


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
