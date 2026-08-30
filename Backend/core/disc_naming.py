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
        disc_number = getattr(disc, "disc_number", None)
        if disc_number:
            parts.append(f"Disc {disc_number}")
    else:
        parts.append(movie_name)
    if fmt:
        parts.append(fmt)
    return " - ".join(parts)


def is_auto_disc_name(disc: Any) -> bool:
    """True when the stored name is one automation wrote (safe to replace).

    Recognized shapes: blank, a bare format ("DVD", "Blu-Ray", …), the
    scan-time default ("{info_title} - {format}" and its degenerate forms),
    or a previous output of :func:`labeled_disc_name` for the disc's
    current identity. Anything else is treated as user-authored.
    """
    name = _clean(getattr(disc, "disc_name", None))
    if not name:
        return True
    fmt_raw = getattr(disc, "format", None)
    fmt = normalize_disc_format(fmt_raw) if fmt_raw else None
    candidates = {c for c in (
        fmt,
        _clean(fmt_raw),
        default_disc_name(fmt_raw, getattr(disc, "info_title", None)),
        labeled_disc_name(disc),
    ) if c}
    lowered = {c.lower() for c in candidates}
    # Bare formats regardless of stored spelling ("BluRay", "Blu-Ray", "dvd").
    lowered.update({"dvd", "blu-ray", "bluray", "blu-ray 3d", "uhd", "4k uhd", "4k"})
    return name.lower() in lowered


def refresh_auto_disc_identity(disc: Any) -> bool:
    """Regenerate disc_name (+ slug, when it tracked the name) from labeled
    identity. Mutates the ORM object; caller owns the transaction. Returns
    True when something changed. Never touches a user-authored name."""
    try:
        if not is_auto_disc_name(disc):
            return False
        new_name = labeled_disc_name(disc)
        if not new_name or new_name == _clean(getattr(disc, "disc_name", None)):
            return False
        old_name = _clean(getattr(disc, "disc_name", None))
        old_slug = _clean(getattr(disc, "disc_slug", None))
        disc.disc_name = new_name
        # The slug follows the name only when it was following it before
        # (blank, or the slug of the auto name we are replacing).
        if not old_slug or (old_name and old_slug == slugify_disc_name(old_name)):
            disc.disc_slug = slugify_disc_name(new_name)
        logger.info("Auto disc name refreshed: disc=%s %r -> %r",
                    getattr(disc, "id", None), old_name, new_name)
        return True
    except Exception as exc:
        logger.warning("refresh_auto_disc_identity failed for disc %s: %s",
                       getattr(disc, "id", None), exc)
        return False
