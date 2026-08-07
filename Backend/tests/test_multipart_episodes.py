"""Multi-part episode layout: naming and TMDB two-parter detection (#796)."""
import pytest

from core.disc import (
    compute_expected_path,
    format_episode_designator,
    format_part_suffix,
)
from core.tmdb_two_parter import detect_two_parters, resolve_layout


class TestEpisodeDesignator:
    @pytest.mark.parametrize("ms,expected", [("plex", "s03e01"), ("jellyfin", "S03E01")])
    def test_single_episode(self, ms, expected):
        assert format_episode_designator(3, 1, None, ms) == expected

    @pytest.mark.parametrize("ms,expected", [("plex", "s03e20-e21"), ("jellyfin", "S03E20-E21")])
    def test_range_when_one_file_covers_two(self, ms, expected):
        assert format_episode_designator(3, 20, 21, ms) == expected

    def test_end_equal_to_start_is_not_a_range(self):
        assert format_episode_designator(3, 20, 20) == "s03e20"

    def test_end_below_start_is_ignored(self):
        assert format_episode_designator(3, 20, 19) == "s03e20"


class TestPartSuffix:
    def test_stacking_suffix(self):
        assert format_part_suffix(1) == " - part1"
        assert format_part_suffix(2) == " - part2"

    @pytest.mark.parametrize("bad", [None, 0, -1, "", "abc"])
    def test_absent_or_nonsense_yields_nothing(self, bad):
        assert format_part_suffix(bad) == ""


class TestComputeExpectedPath:
    def _path(self, **title):
        base = {"title": "Steps Into Shadow", "type": "Episode", "season": 3, "episode": 1}
        base.update(title)
        return compute_expected_path(
            base,
            {"release_type": "series", "release_name": "Star Wars Rebels"},
            {"movie_name": "Star Wars Rebels", "production_year": 2014},
            media_server="plex", resolution="1080p",
        )

    def test_split_across_files_gets_stacking_suffixes(self):
        one = self._path(part=1)
        two = self._path(part=2)
        assert "s03e01 - Steps Into Shadow - part1" in one
        assert "s03e01 - Steps Into Shadow - part2" in two
        # Plex stacks on identical basename + partN, so everything before the
        # suffix must match exactly.
        assert one.replace("part1", "") == two.replace("part2", "")

    def test_one_file_spanning_two_episodes_uses_a_range(self):
        assert "s03e01-e02 - Steps Into Shadow" in self._path(episode_end=2)

    def test_plain_episode_is_unchanged(self):
        assert "s03e01 - Steps Into Shadow" in self._path()
        assert "part" not in self._path()


class TestTwoParterDetection:
    ZERO_HOUR = [
        {"episode_number": 19, "name": "Twin Suns"},
        {"episode_number": 20, "name": "Zero Hour (1)"},
        {"episode_number": 21, "name": "Zero Hour (2)"},
    ]

    def test_adjacent_pair_is_detected(self):
        found = detect_two_parters(self.ZERO_HOUR)
        assert set(found) == {20, 21}
        assert found[20].base_name == "Zero Hour" and found[20].part == 1
        assert found[21].base_name == "Zero Hour" and found[21].part == 2

    @pytest.mark.parametrize("name", ["Zero Hour Part 1", "Zero Hour - Part One", "Zero Hour Pt. 1"])
    def test_other_marker_spellings(self, name):
        eps = [{"episode_number": 20, "name": name},
               {"episode_number": 21, "name": name.replace("1", "2").replace("One", "Two")}]
        assert set(detect_two_parters(eps)) == {20, 21}

    def test_a_lone_marker_with_no_sibling_is_left_alone(self):
        """Adjacency is the whole safeguard against false positives."""
        assert detect_two_parters([{"episode_number": 20, "name": "Zero Hour (1)"}]) == {}

    def test_non_adjacent_same_base_is_not_a_two_parter(self):
        eps = [{"episode_number": 3, "name": "Zero Hour (1)"},
               {"episode_number": 17, "name": "Zero Hour (2)"}]
        assert detect_two_parters(eps) == {}

    @pytest.mark.parametrize("name", ["Catch-22", "Episode 2", "Nightfall", "Apollo 13"])
    def test_ordinary_titles_are_never_touched(self, name):
        eps = [{"episode_number": 1, "name": name}, {"episode_number": 2, "name": name}]
        assert detect_two_parters(eps) == {}


class TestResolveLayoutFollowsTheDisc:
    """The naming decision depends on what is physically on the disc."""

    EPS = TestTwoParterDetection.ZERO_HOUR

    def test_two_files_stay_separate_episodes_with_the_marker_stripped(self):
        assert resolve_layout(self.EPS, 20, disc_file_count=2) == {"title": "Zero Hour"}
        assert resolve_layout(self.EPS, 21, disc_file_count=2) == {"title": "Zero Hour"}

    def test_one_file_covering_both_becomes_a_range(self):
        assert resolve_layout(self.EPS, 20, disc_file_count=1) == {
            "title": "Zero Hour", "episode_end": 21,
        }

    def test_an_ordinary_episode_yields_nothing_to_write(self):
        assert resolve_layout(self.EPS, 19, disc_file_count=1) == {}


class TestProvenance:
    def test_the_new_fields_route_through_the_chokepoint(self):
        """auto_* writes must never clobber a hand-correction."""
        from api import models
        from api.crud import PROVENANCED_TITLE_FIELDS, set_title_field

        for field in ("part", "part_of", "episode_end"):
            assert field in PROVENANCED_TITLE_FIELDS
            t = models.DiscTitle(id="t", disc_id="d", index=1)
            set_title_field(t, field, 2, source="user")
            set_title_field(t, field, 9, source="auto")
            assert getattr(t, field) == 2, f"{field}: automation overwrote the user"
            # Retracting the user value falls back to automation's answer.
            set_title_field(t, field, None, source="user")
            assert getattr(t, field) == 9


class TestMultipartFieldsRoundTrip:
    """The Layout control reverts if these don't come back in the payload.

    Symptom seen in UI testing: pick "Split across files", it saves, then the
    control snaps back to "Single episode". The PATCH persisted fine — but
    the workflow-context refetch returned title rows WITHOUT part/part_of/
    episode_end, and the frontend derives the layout from those fields, so it
    resolved to 'single' again on every refresh.
    """

    @pytest.mark.parametrize("router", ["discs.py", "jobs.py"])
    @pytest.mark.parametrize("field", ["part", "part_of", "episode_end"])
    def test_serializers_expose_the_layout_fields(self, router, field):
        """Both routers serialize titles, and the UI reads the JOBS one.

        Only discs.py was fixed first, so the Layout control still reverted on
        reload: the write persisted (part=1 in the DB) but
        /jobs/{id}/workflow-context — what the labeling view actually fetches —
        returned the row without it, and the derived layout reset to 'single'.
        """
        import re
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "api" / "routers" / router
        text = src.read_text()
        # Every place that ships `episode` must also ship the layout fields,
        # or the value silently fails to round-trip.
        episode_sites = len(re.findall(r'"episode":\s*\w+\.episode,', text))
        field_sites = len(re.findall(rf'"{field}":\s*\w+\.{field},', text))
        assert field_sites >= episode_sites, (
            f'"{field}" is serialized {field_sites}x but "episode" {episode_sites}x — '
            "a payload that omits it will make the Layout control revert"
        )
