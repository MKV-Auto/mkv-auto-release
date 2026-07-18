"""Unit tests for tmdb_client.normalize_title (#387).

Pure-Python, no network. The matrix below was derived from real disc
info_title values observed in the local DB sample (59 discs / 103 jobs),
plus the engram-spec edge cases called out in the master plan.
"""
import pytest

from core.tmdb_client import normalize_title


@pytest.mark.parametrize(
    "raw,expected_query,expected_hints",
    [
        # Clean MakeMKV CINFO-parsed titles (the dominant case in real data).
        ("Midway", "midway", {}),
        ("Joker", "joker", {}),
        ("The Goonies", "the goonies", {}),
        ("1917", "1917", {}),
        ("V For Vendetta", "v for vendetta", {}),

        # Colons / punctuation collapse to whitespace, apostrophes preserved.
        ("Joker: Folie À Deux", "joker folie à deux", {}),
        ("Dune: Part Two", "dune part two", {}),
        ("Harry Potter And The Sorcerer's Stone", "harry potter and the sorcerer's stone", {}),

        # Series with season + disc suffixes — hints extracted, title cleaned.
        ("Wednesday Season 1 Disc 2", "wednesday", {"season": 1, "disc_num": 2}),
        ("Rick And Morty - Season 4", "rick and morty", {"season": 4}),
        ("Sas Rogue Heroes S2 D2", "sas rogue heroes", {"season": 2, "disc_num": 2}),
        ("Sas: Rogue Heroes Disc 2", "sas rogue heroes", {"disc_num": 2}),
        ("Boondocks_s1_d2", "boondocks", {"season": 1, "disc_num": 2}),
        # Spelled-out season / disc numerals — observed on Fallout S2 disc art.
        # Without this row in the matrix, query becomes "fallout season two"
        # and TMDB returns nothing.
        ("Fallout Season Two Disc 1", "fallout", {"season": 2, "disc_num": 1}),
        ("The Bear Season Three", "the bear", {"season": 3}),
        ("Loki Season One Disc Two", "loki", {"season": 1, "disc_num": 2}),

        # Trailing edition tokens stripped to edition hint.
        ("Harry Potter And The Half-blood Prince Uce", "harry potter and the half blood prince", {"edition": "uce"}),
        ("Harry Potter And The Order Of The Phoenix Uce", "harry potter and the order of the phoenix", {"edition": "uce"}),
        ("Star Wars: The Phantom Menace Bonus Disc", "star wars the phantom menace", {"edition": "bonus disc"}),

        # Underscores → space.
        ("Full_metal_jacket", "full metal jacket", {}),

        # Empty / None handling.
        ("", "", {}),
    ],
)
def test_normalize_title_matrix(raw, expected_query, expected_hints):
    query, hints = normalize_title(raw)
    assert query == expected_query, (
        f"normalize_title({raw!r}) returned query {query!r}, expected {expected_query!r}"
    )
    assert hints == expected_hints, (
        f"normalize_title({raw!r}) returned hints {hints!r}, expected {expected_hints!r}"
    )


def test_normalize_title_handles_none():
    """None-ish input doesn't crash."""
    q, h = normalize_title(None)  # type: ignore[arg-type]
    assert q == ""
    assert h == {}


def test_normalize_title_preserves_ampersand():
    """Ampersands in titles like 'Dungeons & Dragons' must survive normalization
    so TMDB search can match on them."""
    q, _ = normalize_title("Dungeons & Dragons - Honor Among Thieves")
    # The ampersand is preserved; the dash collapses to whitespace.
    assert "&" in q
    assert "dungeons" in q
    assert "dragons" in q
    assert "honor among thieves" in q


def test_normalize_title_does_not_eat_inside_words():
    """The edition stripper must only match trailing tokens, not eat substrings
    inside the title."""
    q, h = normalize_title("BD on the Run")  # 'BD' here is part of the title
    # Trailing 'Run' is not an edition token; 'BD' is at the start so trailing
    # match shouldn't fire. Edition hint must be absent.
    assert "edition" not in h
