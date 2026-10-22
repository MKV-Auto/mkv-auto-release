"""Auto disc name + slug from labeled identity (#845).

The scan-time default (`utils.default_disc_name`) only has MakeMKV's
``info_title`` and the format, so junk disc labels produce names like
"DVD" or "Blu-Ray". Once the disc is *labeled* we know better, and prod
shows the conventions the maintainer has been typing by hand:

    movies:  "Thor: Ragnarok - Blu-Ray"            {movie} - {format}
    series:  "Star Wars Rebels: Season 2 - Disc 2 - DVD"
             {show}: Season {N} - Disc {M} - {format}

This module renders those from the linked movie/release, the per-disc
season (``disc.label_draft.primary_season``), the disc number, and the
format — and refreshes a disc's stored name **only when the stored name is
recognizably auto-generated** (blank, a bare format, or the scan-time
default). A name the user typed is never touched. The series form also
matches TheDiscDB's disc-name convention ("Season N Disc M"), so exports
read naturally there.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from core.utils import default_disc_name, normalize_disc_format, slugify_disc_name

logger = logging.getLogger(__name__)


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _primary_season(disc: Any) -> Optional[int]:
    draft = getattr(disc, "label_draft", None)
    if not isinstance(draft, dict):
        return None
    raw = draft.get("primary_season")
    if isinstance(raw, bool):
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def season_scoped_disc_ordinal(disc: Any) -> Optional[int]:
    """Within-season disc ordinal for multi-season releases.

    A "Season 1-5" box numbers its discs 1..22 release-wide (that stays in
    ``disc_number`` — the boxset position), but the NAME should say
    "Season 4 - Disc 1", not "Season 4 - Disc 12". Rank this disc's
    ``disc_number`` among the release's discs that carry the same season.
    Returns None when the season, siblings, or numbers aren't available —
    callers fall back to ``disc_number``. Public: the card carousel surfaces
    it as ``disc_season_ordinal`` beside the boxset position (#846).
    """
    season = _primary_season(disc)
    if season is None:
        return None
    release = getattr(disc, "release", None)
    siblings = getattr(release, "discs", None) if release else None
    if not siblings:
        return None
    my_number = getattr(disc, "disc_number", None)
    if my_number is None:
        return None
    same_season_numbers = sorted(
        n for n in (
            getattr(d, "disc_number", None)
            for d in siblings
            if _primary_season(d) == season
        ) if n is not None
    )
    if my_number not in same_season_numbers:
        return None
    return same_season_numbers.index(my_number) + 1


def labeled_disc_name(disc: Any) -> Optional[str]:
    """The convention-formatted name, or None when identity is missing."""
    release = getattr(disc, "release", None)
    movie = getattr(release, "movie", None) if release else None
    movie_name = _clean(getattr(movie, "name", None))
    if not movie_name:
        return None
    fmt = normalize_disc_format(getattr(disc, "format", None)) if getattr(disc, "format", None) else None
    tmdb_type = (_clean(getattr(movie, "tmdb_type", None)) or "").lower()

    parts: list[str] = []
    if tmdb_type == "tv":
        season = _primary_season(disc)
        parts.append(f"{movie_name}: Season {season}" if season else movie_name)
        disc_number = (
            (season_scoped_disc_ordinal(disc) if season else None)
            or getattr(disc, "disc_number", None)
        )
        if disc_number:
            parts.append(f"Disc {disc_number}")
    else:
        parts.append(movie_name)
    if fmt:
        parts.append(fmt)
    return " - ".join(parts)


# Spellings normalize_disc_format can produce — used to recognize renders
# made under a format the disc no longer carries.
_KNOWN_FORMATS = ("DVD", "Blu-Ray", "Blu-Ray 3D", "UHD", "4K UHD")


def _convention_variants(disc: Any) -> set[str]:
    """Every name a PAST convention render could have produced for this disc.

    Identity evolves during labeling — the season arrives after the movie
    link, the disc gets renumbered when it joins a multi-disc release, the
    numbering switched from release-wide to within-season. A name generated
    at any earlier point must still be recognized as machine-written, or the
    refresh treats it as the user's and never updates it again (seen live:
    'Star Wars: The Clone Wars - Disc 5 - DVD' froze once Season 4 was set,
    because only the current render was checked).
    """
    release = getattr(disc, "release", None)
    movie = getattr(release, "movie", None) if release else None
    movie_name = _clean(getattr(movie, "name", None))
    if not movie_name:
        return set()
    # ALL formats, not just the current one: a render made before a format
    # correction ("… - DVD" while the disc is now Blu-Ray) must still be
    # recognized, or changing the format freezes the name (seen on the
    # 1.6.13-rc.1 rig).
    fmts = set(_KNOWN_FORMATS)
    if getattr(disc, "format", None):
        fmts.add(normalize_disc_format(disc.format))
    season = _primary_season(disc)
    numbers = {n for n in (
        getattr(disc, "disc_number", None),
        season_scoped_disc_ordinal(disc),
    ) if n}
    heads = {movie_name}
    if season is not None:
        heads.add(f"{movie_name}: Season {season}")
    variants: set[str] = set()
    for head in heads:
        stems = {head}
        for n in numbers:
            stems.add(f"{head} - Disc {n}")
        for stem in stems:
            variants.add(stem)
            for f in fmts:
                variants.add(f"{stem} - {f}")
    return variants


def is_auto_disc_name(disc: Any) -> bool:
    """True when the stored name is one automation wrote (safe to replace)."""
    return machine_generated_name(disc, getattr(disc, "disc_name", None))


def machine_generated_name(disc: Any, candidate: Any) -> bool:
    """True when ``candidate`` is a name automation could have produced for
    this disc — safe to replace, and NEVER to be recorded as a user edit.

    Recognized shapes: blank, a bare format ("DVD", "Blu-Ray", …), the
    scan-time default ("{info_title} - {format}" and its degenerate forms),
    any past or present convention render for this disc's identity
    (see :func:`_convention_variants`), or a TheDiscDB-style composite
    ("{movie} - {release name} - …" — machine data, never typed by a user).
    Anything else is treated as user-authored.

    Used only to decide whether a STORED name is automation's to replace
    (refresh_auto_disc_identity). Incoming payloads are never shape-filtered:
    the client dirty-tracks disc_name/disc_slug and omits them unless the
    user actually edited them, so anything that arrives is a user edit —
    even a machine-looking value like "Blu-Ray".
    """
    name = _clean(candidate)
    if not name:
        return True
    if _is_our_name(disc, name):
        return True
    return _is_discdb_composite(disc, name)


def _is_our_name(disc: Any, name: str) -> bool:
    """A name MKV-Auto writes itself: bare format, scan-time default, or a
    past/present convention render for this disc."""
    fmt_raw = getattr(disc, "format", None)
    fmt = normalize_disc_format(fmt_raw) if fmt_raw else None
    candidates = {c for c in (
        fmt,
        _clean(fmt_raw),
        default_disc_name(fmt_raw, getattr(disc, "info_title", None)),
        labeled_disc_name(disc),
    ) if c}
    candidates |= _convention_variants(disc)
    lowered = {c.lower() for c in candidates}
    # Bare formats regardless of stored spelling ("BluRay", "Blu-Ray", "dvd").
    lowered.update({"dvd", "blu-ray", "bluray", "blu-ray 3d", "uhd", "4k uhd", "4k"})
    return name.lower() in lowered


def _is_discdb_composite(disc: Any, name: str) -> bool:
    """TheDiscDB-style composite: "{movie} - {release name} - …". Machine
    origin by construction (seen live overwriting the convention name and
    then masquerading as user-typed)."""
    release = getattr(disc, "release", None)
    movie = getattr(release, "movie", None) if release else None
    movie_name = _clean(getattr(movie, "name", None))
    release_name = _clean(getattr(release, "name", None)) if release else None
    if movie_name and release_name:
        prefixes = {f"{movie_name} - {release_name}".lower()}
        # Real imports often store the release name ALREADY movie-prefixed
        # ("Star Wars: The Clone Wars - Season 1-5 Collector's Edition");
        # the composite disc name is then just "{release.name} - …". Only
        # honored when the release name carries the movie prefix itself, so
        # a short user-typed name can't collide (rc.4 rig: the double-
        # prefixed guess above never matched prod rows).
        if release_name.lower().startswith(movie_name.lower() + " - "):
            prefixes.add(release_name.lower())
        for prefix in prefixes:
            if name.lower() == prefix or name.lower().startswith(prefix + " - "):
                return True
    return False


def effective_auto_naming(disc: Any) -> bool:
    """Whether the "Automatic naming" toggle is on for this disc (#915).

    An explicit choice (``disc.auto_naming``) wins. With none, it is on when
    the stored name is machine-made (blank, a bare format, the scan-time
    default, a convention render, or a TheDiscDB-style composite, which once
    froze auto-naming by passing for user-typed) and the slug follows it
    (blank or the name's slug). A typed name or a hand-typed slug makes it
    off, so the first save of an inferred value can never overwrite either.
    """
    explicit = getattr(disc, "auto_naming", None)
    if explicit is not None:
        return bool(explicit)
    name = getattr(disc, "disc_name", None)
    if not machine_generated_name(disc, name):
        return False
    clean_name = _clean(name)
    if clean_name and _is_discdb_composite(disc, clean_name):
        return True   # TheDiscDB wrote the slug too, in its own spelling
    slug = _clean(getattr(disc, "disc_slug", None))
    return not slug or slug == slugify_disc_name(clean_name or "")


def apply_naming_from_label(disc: Any, payload: dict, default_name: Optional[str]) -> None:
    """Disc name and slug from a label save (#915). Mutates the ORM object.

    ``payload["auto_naming"]``, when present, records the user's toggle. With
    automatic naming on, the payload's name and slug are ignored: the fields
    are read-only in the UI and :func:`refresh_auto_disc_identity` renders the
    convention name. With it off, the user's values are kept as typed; an
    empty name keeps the stored one (a disc always has a name) and an empty
    slug is generated from the name.
    """
    from api.crud import apply_disc_slug_from_label_payload

    toggle = payload.get("auto_naming")
    if toggle is None and (_clean(payload.get("disc_name")) or _clean(payload.get("disc_slug"))):
        # A name or slug sent without the toggle is one the user typed:
        # pre-#915 clients and API callers send these fields only for user
        # edits (#845 dirty tracking). Keep it, and stop renaming the disc.
        toggle = False
    if toggle is not None:
        disc.auto_naming = bool(toggle)
    if not _clean(getattr(disc, "disc_name", None)) and default_name:
        disc.disc_name = default_name
    if effective_auto_naming(disc):
        if not _clean(getattr(disc, "disc_slug", None)) and _clean(disc.disc_name):
            disc.disc_slug = slugify_disc_name(disc.disc_name)
        return
    typed = _clean(payload.get("disc_name"))
    if typed:
        disc.disc_name = typed
    apply_disc_slug_from_label_payload(disc, payload.get("disc_slug"))


def refresh_auto_disc_identity(disc: Any) -> bool:
    """Regenerate disc_name and disc_slug from labeled identity while
    automatic naming is on (#915). Mutates the ORM object; caller owns the
    transaction. Returns True when something changed. Never touches a disc
    whose toggle is off."""
    try:
        if not effective_auto_naming(disc):
            return False
        new_name = labeled_disc_name(disc)
        if not new_name:
            return False
        old_name = _clean(getattr(disc, "disc_name", None))
        old_slug = _clean(getattr(disc, "disc_slug", None))
        # With the toggle explicitly on, the slug is read-only and always
        # follows the name. On a disc that never chose (legacy rows), it
        # follows only if it already did: a hand-typed slug survives.
        follows = (
            getattr(disc, "auto_naming", None) is True
            or not old_slug
            or (old_name is not None and old_slug == slugify_disc_name(old_name))
        )
        new_slug = slugify_disc_name(new_name) if follows else old_slug
        if new_name == old_name and new_slug == old_slug:
            return False
        disc.disc_name = new_name
        disc.disc_slug = new_slug
        logger.info("Auto disc name refreshed: disc=%s %r -> %r",
                    getattr(disc, "id", None), old_name, new_name)
        return True
    except Exception as exc:
        logger.warning("refresh_auto_disc_identity failed for disc %s: %s",
                       getattr(disc, "id", None), exc)
        return False
