from core.utils import (
    slugify,
    build_release_slug,
    default_disc_name,
    normalize_disc_format,
    slugify_disc_name,
)


def test_slugify_basic_replacements():
    assert slugify("My Movie Title") == "my-movie-title"
    assert slugify("Spaces  and  tabs") == "spaces--and--tabs"
    assert slugify("Rock & Roll") == "rock-and-roll"
    assert slugify("Æon Flux") == "aeon-flux"
    assert slugify("") == ""
    assert slugify(None) == ""


def test_build_release_slug_with_and_without_year():
    assert build_release_slug("My Movie", 2024) == "my-movie-2024"
    assert build_release_slug("My Movie", None) == "my-movie"
    # fallback when name missing
    assert build_release_slug("", 2024) == "release-2024"


def test_slugify_disc_name_spaces_underscores_separators_hyphens():
    assert (
        slugify_disc_name("Blu-Ray - Sas Rogue Heroes S2 D1")
        == "blu-ray_-_sas_rogue_heroes_s2_d1"
    )
    assert slugify_disc_name("Blu-Ray") == "blu-ray"
    assert slugify_disc_name("Blu Ray") == "blu_ray"
    assert slugify_disc_name("UHD 4K") == "uhd_4k"
    assert slugify_disc_name("A & B / C") == "a_b_-_c"
    assert slugify_disc_name("") == ""
    assert slugify_disc_name(None) == ""


def test_slugify_disc_name_unicode_dash():
    # U+2010 hyphen (Pd)
    assert slugify_disc_name("read‐error") == "read-error"


def test_default_disc_name_composes_format_and_title():
    assert default_disc_name("Blu-Ray", "Sas Rogue Heroes S2 D1") == "Sas Rogue Heroes S2 D1 - Blu-Ray"
    assert default_disc_name("bluray", "Title") == "Title - Blu-Ray"
    assert default_disc_name("UHD", None) == "UHD"
    assert default_disc_name(None, "Only Title") == "Only Title"
    assert default_disc_name(None, None) is None


def test_normalize_disc_format_matches_crud():
    assert normalize_disc_format("bd") == "Blu-Ray"
    assert normalize_disc_format("uhd") == "UHD"
    assert normalize_disc_format(None) is None
