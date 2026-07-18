"""Unit tests for api.crud pure helpers (no Session/DB)."""
from __future__ import annotations

import pytest

from api.crud import (
    _normalize_format,
    _format_rank,
    _disc_name_sluggify,
    _format_slug,
    _best_format,
    _title_case,
)


class TestNormalizeFormat:
    def test_none_returns_none(self):
        assert _normalize_format(None) is None

    def test_empty_string_returns_none(self):
        assert _normalize_format("") is None

    def test_whitespace_only_returns_empty_after_strip(self):
        # strip() yields "" which is returned as-is (not None; "if not fmt" only hits for None/empty)
        assert _normalize_format("   ") == ""

    def test_uhd_variants_return_uhd(self):
        assert _normalize_format("UHD") == "UHD"
        assert _normalize_format("4K") == "UHD"
        assert _normalize_format("uhd") == "UHD"
        assert _normalize_format("  4K  ") == "UHD"

    def test_bluray_variants_return_blu_ray(self):
        assert _normalize_format("BLU-RAY") == "Blu-Ray"
        assert _normalize_format("BD") == "Blu-Ray"
        assert _normalize_format("blu-ray") == "Blu-Ray"

    def test_dvd_returns_dvd(self):
        assert _normalize_format("DVD") == "DVD"
        assert _normalize_format("dvd") == "DVD"

    def test_unknown_unchanged(self):
        assert _normalize_format("Other") == "Other"
        assert _normalize_format("  LaserDisc  ") == "LaserDisc"


class TestFormatRank:
    def test_none_returns_zero(self):
        assert _format_rank(None) == 0

    def test_uhd_and_4k_return_3(self):
        assert _format_rank("UHD") == 3
        assert _format_rank("4K") == 3
        assert _format_rank("uhd") == 3

    def test_bluray_variants_return_2(self):
        assert _format_rank("BLU-RAY") == 2
        assert _format_rank("BD") == 2
        assert _format_rank("BLURAY") == 2

    def test_dvd_returns_1(self):
        assert _format_rank("DVD") == 1

    def test_unknown_returns_0(self):
        assert _format_rank("Other") == 0
        assert _format_rank("LaserDisc") == 0


class TestDiscNameSluggify:
    def test_empty_returns_empty(self):
        assert _disc_name_sluggify("") == ""

    def test_none_returns_empty(self):
        assert _disc_name_sluggify(None) == ""

    def test_blu_ray_preserves_hyphen(self):
        assert _disc_name_sluggify("Blu-Ray") == "blu-ray"

    def test_spaces_become_underscores(self):
        assert _disc_name_sluggify("Blu Ray") == "blu_ray"
        assert _disc_name_sluggify("UHD 4K") == "uhd_4k"

    def test_lowercase_and_numbers(self):
        assert _disc_name_sluggify("Disc 01") == "disc_01"

    def test_format_dash_title(self):
        assert (
            _disc_name_sluggify("Blu-Ray - Sas Rogue Heroes S2 D1")
            == "blu-ray_-_sas_rogue_heroes_s2_d1"
        )

    def test_ampersand_and_slash_dropped(self):
        assert _disc_name_sluggify("A & B / C") == "a_b_-_c"


class TestFormatSlug:
    def test_none_returns_none(self):
        assert _format_slug(None) is None

    def test_uhd_returns_4k(self):
        assert _format_slug("UHD") == "4k"
        assert _format_slug("4K") == "4k"

    def test_blu_ray_returns_blu_ray(self):
        assert _format_slug("Blu-Ray") == "blu-ray"
        assert _format_slug("BD") == "blu-ray"

    def test_dvd_returns_dvd(self):
        assert _format_slug("DVD") == "dvd"

    def test_other_uses_slugify(self):
        # slugify from core.utils normalizes to lowercase hyphenated
        assert _format_slug("Other") == "other"
        assert _format_slug("LaserDisc") == "laserdisc"


class TestBestFormat:
    def test_none_uhd_returns_uhd(self):
        assert _best_format(None, "UHD") == "UHD"

    def test_bluray_uhd_returns_uhd(self):
        assert _best_format("Blu-Ray", "UHD") == "UHD"

    def test_uhd_dvd_returns_uhd(self):
        assert _best_format("UHD", "DVD") == "UHD"

    def test_none_none_returns_none(self):
        assert _best_format(None, None) is None

    def test_same_rank_returns_existing(self):
        assert _best_format("Blu-Ray", "DVD") == "Blu-Ray"
        assert _best_format("UHD", "4K") == "UHD"


class TestTitleCase:
    def test_none_returns_none(self):
        assert _title_case(None) is None

    def test_empty_string_returns_empty(self):
        assert _title_case("") == ""

    def test_simple_title_case(self):
        assert _title_case("hello world") == "Hello World"

    def test_single_word(self):
        assert _title_case("hello") == "Hello"

    def test_multiple_words(self):
        # str.split() collapses runs of whitespace
        assert _title_case("a  b   c") == "A B C"
