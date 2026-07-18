import pytest

from core.title_type_normalize import (
    normalize_title_type_for_api,
    normalize_title_type_for_storage,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        ("", None),
        ("  ", None),
        ("DeletedScene", "DeletedScene"),
        ("deletedscene", "DeletedScene"),
        ("Deletedscene", "DeletedScene"),
        ("deleted", "DeletedScene"),
        ("movie", "MainMovie"),
        ("ignore", "ignore"),
        ("episode", "Episode"),
        ("BehindTheScenes", "BehindTheScenes"),
        ("behind the scenes", "BehindTheScenes"),
        ("featurette", "Featurette"),
        ("Trailer", "Trailer"),
        ("trailer", "Trailer"),
        ("Other", "Other"),
        ("Sample", "Sample"),
        ("Clip", "Clip"),
        ("ThemeMusic", "ThemeMusic"),
        ("theme-music", "ThemeMusic"),
        ("Backdrop", "Backdrop"),
        ("unknown_custom_type", "Extra"),
    ],
)
def test_normalize_title_type_for_api(raw, expected):
    assert normalize_title_type_for_api(raw) == expected


def test_storage_alias_matches_api_normalizer():
    assert normalize_title_type_for_storage is normalize_title_type_for_api
