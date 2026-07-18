import pytest

from core.title_type_extras_layout import extras_subfolder_for_type


@pytest.mark.parametrize(
    "canon,server,expected",
    [
        ("MainMovie", "plex", None),
        ("Episode", "jellyfin", None),
        ("ignore", "plex", None),
        ("Trailer", "plex", "Trailers"),
        ("Trailer", "jellyfin", "trailers"),
        ("BehindTheScenes", "plex", "Behind The Scenes"),
        ("BehindTheScenes", "jellyfin", "behind the scenes"),
        ("Extra", "plex", "Other"),
        ("Extra", "jellyfin", "extras"),
        ("Sample", "plex", "Other"),
        ("Sample", "jellyfin", "samples"),
        ("Clip", "plex", "Other"),
        ("Clip", "jellyfin", "clips"),
        ("ThemeMusic", "plex", "Other"),
        ("ThemeMusic", "jellyfin", "theme-music"),
        ("Backdrop", "plex", "Other"),
        ("Backdrop", "jellyfin", "backdrops"),
    ],
)
def test_extras_subfolder_for_type(canon, server, expected):
    assert extras_subfolder_for_type(canon, server) == expected
