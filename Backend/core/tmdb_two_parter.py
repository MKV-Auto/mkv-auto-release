"""Detect TMDB episodes that are one story numbered as two.

TMDB frequently numbers a two-part story as separate episodes carrying a
marker in the name — E20 "Zero Hour (1)", E21 "Zero Hour (2)". Left alone
that marker ends up in the filename, and the relationship between the two
episodes is recorded nowhere.

Detection is deliberately **conservative and adjacent-only**: a marker is
only believed when the same stripped base title appears on *consecutive*
episode numbers in the same season. An episode whose real name merely ends
in a parenthetical ("Nightfall (Part of the Whole)", "Legacy (2)" as an
actual title) must not be rewritten, and a lone "Foo (1)" with no "Foo (2)"
beside it is left exactly as TMDB gave it.

What the caller does with the result depends on the DISC, not on TMDB:

    two files on disc  -> keep them as separate episodes, s03e20 / s03e21
    one file on disc   -> range naming, s03e20-e21

Part suffixes are *not* used for this case — Plex/Jellyfin stacking
requires the same episode number, so `s03e20 - part1` + `s03e21 - part2`
would not stack. `part` is for the other direction: one episode the disc
splits across files (#796).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# " (1)", " Part 1", " - Part One", " Pt. 2" — anchored to the END of the
# name and requiring the separator, so "Episode 2" or "Catch-22" can't match.
_PART_MARKER = re.compile(
    r"""^(?P<base>.+?)                     # the base title, non-greedy
        (?:
            \s*\((?P<paren>\d{1,2})\)      #  (1)
          | \s*[-–—:]?\s*
            (?:part|pt\.?)\s*
            (?P<word>\d{1,2}|one|two|three|four)
        )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

_WORD_TO_INT = {"one": 1, "two": 2, "three": 3, "four": 4}


@dataclass(frozen=True)
class TwoParterPart:
    """One member of a detected multi-episode story."""

    episode_number: int
    part: int
    base_name: str


def _split_marker(name: str) -> tuple[str, int] | None:
    """('Zero Hour', 1) for 'Zero Hour (1)', else None."""
    if not name:
        return None
    m = _PART_MARKER.match(name.strip())
    if not m:
        return None
    raw = m.group("paren") or m.group("word") or ""
    raw = raw.strip().lower()
    part = _WORD_TO_INT.get(raw)
    if part is None:
        try:
            part = int(raw)
        except (TypeError, ValueError):
            return None
    base = (m.group("base") or "").strip(" -–—:")
    if not base or part <= 0:
        return None
    return base, part


def detect_two_parters(episodes) -> dict[int, TwoParterPart]:
    """Map episode_number -> TwoParterPart for every detected multi-parter.

    ``episodes`` is any iterable of objects or mappings exposing
    ``episode_number`` and ``name``. Episodes with no marker, or whose
    marker has no adjacent sibling sharing the base name, are absent from
    the result — callers should treat "absent" as "ordinary episode".
    """
    parsed: dict[int, tuple[str, int]] = {}
    for ep in episodes or ():
        if isinstance(ep, dict):
            number, name = ep.get("episode_number"), ep.get("name")
        else:
            number, name = getattr(ep, "episode_number", None), getattr(ep, "name", None)
        if number is None:
            continue
        split = _split_marker(str(name or ""))
        if split:
            parsed[int(number)] = split

    out: dict[int, TwoParterPart] = {}
    for number, (base, part) in parsed.items():
        # Adjacency is what separates a real two-parter from a title that
        # happens to end in a number. Look both ways so part 2 is detected
        # from part 1 and vice versa.
        neighbours = (
            parsed.get(number - 1),
            parsed.get(number + 1),
        )
        if any(n is not None and n[0].casefold() == base.casefold() for n in neighbours):
            out[number] = TwoParterPart(episode_number=number, part=part, base_name=base)
    return out


def resolve_layout(
    episodes,
    episode_number: int,
    disc_file_count: int,
) -> dict:
    """What a title claiming ``episode_number`` should be labelled.

    Returns a dict of the fields to write (all provenance ``auto``):

        {}                                     not a two-parter — leave alone
        {"title": base}                        two files on disc: separate
                                               episodes, marker stripped
        {"title": base, "episode_end": N}      one file covering both

    ``disc_file_count`` is how many physical titles on the disc claim this
    story. Anything other than 1 means the disc already separates them, so
    they stay separate episodes and only the marker is stripped.
    """
    detected = detect_two_parters(episodes)
    here = detected.get(int(episode_number))
    if here is None:
        return {}
    if disc_file_count == 1:
        siblings = sorted(
            n for n, p in detected.items()
            if p.base_name.casefold() == here.base_name.casefold()
        )
        if len(siblings) > 1 and siblings[-1] > int(episode_number):
            return {"title": here.base_name, "episode_end": siblings[-1]}
    return {"title": here.base_name}
