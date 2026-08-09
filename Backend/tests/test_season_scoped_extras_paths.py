"""Season-scoped extras must land in the season folder, per server.

Both Plex and Jellyfin read an extras folder nested inside a season folder as
belonging to that season:

    Series/Show/Season 03/Behind The Scenes/Rebels Recon.mkv   (Plex)
    Series/Show/Season 03/behind the scenes/Rebels Recon.mkv   (Jellyfin)

An extra with no season stays at show level. This pins the layout because it is
a contract with the media servers, not an internal choice — the folder names and
their casing are what Plex and Jellyfin scan for.

Plex additionally requires the library setting "Assign Extras to Episodes,
Seasons or Shows based on folder structure" for season-level assignment to be
honoured; that is a server-side setting we document rather than something the
path can express.
"""
import pytest

from core.disc import compute_expected_path

RELEASE = {"release_type": "series", "name": "Star Wars Rebels"}
MOVIE = {"movie_name": "Star Wars Rebels", "production_year": 2014}


def _path(title_metadata, media_server):
    return compute_expected_path(title_metadata, RELEASE, MOVIE, media_server=media_server)


class TestSeasonScopedExtras:
    @pytest.mark.parametrize(
        "media_server,expected",
        [
            ("plex", "Series/Star Wars Rebels/Season 03/Behind The Scenes/Rebels Recon.mkv"),
            ("jellyfin", "Series/Star Wars Rebels/Season 03/behind the scenes/Rebels Recon.mkv"),
        ],
    )
    def test_extra_with_a_season_nests_under_that_season(self, media_server, expected):
        title = {"type": "BehindTheScenes", "title": "Rebels Recon", "season": 3}
        assert _path(title, media_server) == expected

    @pytest.mark.parametrize(
        "media_server,expected",
        [
            ("plex", "Series/Star Wars Rebels/Behind The Scenes/Rebels Recon.mkv"),
            ("jellyfin", "Series/Star Wars Rebels/behind the scenes/Rebels Recon.mkv"),
        ],
    )
    def test_extra_without_a_season_stays_at_show_level(self, media_server, expected):
        title = {"type": "BehindTheScenes", "title": "Rebels Recon"}
        assert _path(title, media_server) == expected

    @pytest.mark.parametrize(
        "canon_type,plex_folder,jellyfin_folder",
        [
            ("BehindTheScenes", "Behind The Scenes", "behind the scenes"),
            ("DeletedScene", "Deleted Scenes", "deleted scenes"),
            ("Featurette", "Featurettes", "featurettes"),
            ("Interview", "Interviews", "interviews"),
            ("Scene", "Scenes", "scenes"),
            ("Short", "Shorts", "shorts"),
            ("Trailer", "Trailers", "trailers"),
        ],
    )
    def test_every_extra_type_scopes_to_the_season(self, canon_type, plex_folder, jellyfin_folder):
        title = {"type": canon_type, "title": "Thing", "season": 2}
        assert _path(title, "plex") == f"Series/Star Wars Rebels/Season 02/{plex_folder}/Thing.mkv"
        assert _path(title, "jellyfin") == f"Series/Star Wars Rebels/Season 02/{jellyfin_folder}/Thing.mkv"

    def test_season_is_zero_padded(self):
        title = {"type": "Featurette", "title": "Thing", "season": 7}
        assert "/Season 07/" in _path(title, "plex")

    def test_season_zero_is_honoured_as_specials(self):
        # Season 0 is the specials folder on both servers; it must not be
        # confused with "no season".
        title = {"type": "Featurette", "title": "Thing", "season": 0}
        assert "/Season 00/Featurettes/" in _path(title, "plex")

    def test_episodes_are_unaffected(self):
        title = {"type": "Episode", "title": "Steps Into Shadow", "season": 3, "episode": 1}
        path = _path(title, "plex")
        assert "/Season 03/" in path
        assert "Behind The Scenes" not in path


class TestEpisodeScopedExtras:
    """Plex attaches an episode-level extra by FILENAME, not folder:

        Season 07/<episode filename>-<extra name>-<suffix>.mkv

    The episode filename is rebuilt from ``episode_ref_name`` (the sibling
    Episode row's title). Jellyfin has no episode-level extras, and without a
    resolvable sibling no reliable prefix exists — both degrade to the season
    extras folder, keeping the episode captured in the data either way.
    """

    GOT = {"release_type": "series", "name": "Game of Thrones"}
    GOT_MOVIE = {"movie_name": "Game of Thrones", "production_year": 2011}

    def _got(self, tm, ms, resolution=None):
        return compute_expected_path(tm, self.GOT, self.GOT_MOVIE, media_server=ms, resolution=resolution)

    def _extra(self, name="Winterfell", type_="DeletedScene", **over):
        base = {
            "type": type_, "title": name, "season": 7, "episode": 3,
            "episode_ref_name": "The Queen's Justice",
        }
        base.update(over)
        return base

    def test_plex_three_segment_form(self):
        assert self._got(self._extra(), "plex") == (
            "Series/Game of Thrones/Season 07/"
            "Game of Thrones - s07e03 - The Queen's Justice-Winterfell-deleted.mkv"
        )

    def test_two_named_scenes_on_one_episode_are_distinct_files(self):
        a = self._got(self._extra("Winterfell"), "plex")
        b = self._got(self._extra("Dragonpit"), "plex")
        assert a != b
        prefix = "Game of Thrones - s07e03 - The Queen's Justice-"
        assert prefix in a and prefix in b

    def test_jellyfin_degrades_to_the_season_folder(self):
        # Episode stays captured in the data; only the path degrades.
        assert self._got(self._extra(), "jellyfin") == (
            "Series/Game of Thrones/Season 07/deleted scenes/Winterfell.mkv"
        )

    def test_no_sibling_episode_falls_back_to_the_season_folder(self):
        tm = self._extra(episode_ref_name=None)
        assert self._got(tm, "plex") == (
            "Series/Game of Thrones/Season 07/Deleted Scenes/Winterfell.mkv"
        )

    @pytest.mark.parametrize(
        "type_,suffix",
        [
            ("BehindTheScenes", "behindthescenes"),
            ("DeletedScene", "deleted"),
            ("Featurette", "featurette"),
            ("Interview", "interview"),
            ("Trailer", "trailer"),
            ("Sample", "other"),  # no Plex episode suffix of its own
        ],
    )
    def test_suffix_vocabulary(self, type_, suffix):
        path = self._got(self._extra(type_=type_), "plex")
        assert path.endswith(f"-{suffix}.mkv")

    def test_no_resolution_suffix_on_episode_extras(self):
        # The prefix must stay identical to the episode filename; a res
        # decoration would break Plex's match.
        path = self._got(self._extra(), "plex", resolution="1080p")
        assert "1080p" not in path

    def test_episode_without_season_never_takes_the_episode_form(self):
        tm = self._extra(season=None)
        path = self._got(tm, "plex")
        assert "-deleted.mkv" not in path

    def test_an_actual_episode_row_is_untouched(self):
        tm = {
            "type": "Episode", "title": "The Queen's Justice", "season": 7,
            "episode": 3, "episode_ref_name": "The Queen's Justice",
        }
        assert self._got(tm, "plex") == (
            "Series/Game of Thrones/Season 07/Game of Thrones - s07e03 - The Queen's Justice.mkv"
        )
