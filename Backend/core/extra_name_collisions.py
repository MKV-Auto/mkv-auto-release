"""Keep extras from sibling discs of one release from clobbering each other (#831).

A multi-disc release often carries the same extra on every disc — "Rebels
Recon – Play All" on all four Season Two DVDs, "Trailer" on each disc of a
two-disc edition. Each disc is labeled on its own, the postprocess renamer
builds the same filename for each, and the transfer copies every job's
tree into one library folder with ``copytree(dirs_exist_ok=True)``: the
last disc wins and the others are silently gone.

The fix lives at naming time, where it is protocol-agnostic and
deterministic: an extra on disc N whose name is already used by an extra
on a *lower-numbered* sibling disc of the same release gets `` (Disc N)``
appended. Lower discs are never renamed retroactively (the rule only looks
downward), so a library that already holds disc 1's file keeps it, and
re-running disc 2's postprocess yields the same name again. Discs without a
disc number, and releases with a single disc, are untouched.

The hash-aware "never overwrite a different source at transfer time" guard
is the broader #606 problem and stays there.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from core.title_type_extras_layout import extras_subfolder_for_type
from core.title_type_normalize import normalize_title_type_for_api
from core.utils import sanitize_path_component

_DISC_SUFFIX = re.compile(r"\s*\(Disc \d+\)\s*$", re.IGNORECASE)


def extra_name_key(name: Any) -> str | None:
    """Case-insensitive, path-sanitized comparison key for an extra's name."""
    if name is None:
        return None
    text = sanitize_path_component(str(name)) or str(name).strip()
    text = _DISC_SUFFIX.sub("", text).strip().lower()
    return text or None


def is_extra_type(title_type: Any, media_server: str = "plex") -> bool:
    canon = normalize_title_type_for_api(title_type) or ""
    return bool(extras_subfolder_for_type(canon, media_server))


def reserved_extra_names_for_disc(disc: Any, media_server: str = "plex") -> set[str]:
    """Names of extras on lower-numbered sibling discs of ``disc``'s release.

    ``disc`` is an ORM ``Disc`` with ``release`` → ``discs`` → ``titles``
    loaded (lazy loads are fine; this runs once per postprocess). Returns an
    empty set when the disc has no number, no release, or no lower siblings.
    """
    disc_number = getattr(disc, "disc_number", None)
    release = getattr(disc, "release", None)
    if disc_number is None or release is None:
        return set()
    try:
        mine = int(disc_number)
    except (TypeError, ValueError):
        return set()
    reserved: set[str] = set()
    for sibling in getattr(release, "discs", None) or []:
        if getattr(sibling, "id", None) == getattr(disc, "id", None):
            continue
        num = getattr(sibling, "disc_number", None)
        try:
            if num is None or int(num) >= mine:
                continue
        except (TypeError, ValueError):
            continue
        for t in getattr(sibling, "titles", None) or []:
            if not is_extra_type(getattr(t, "type", None), media_server):
                continue
            key = extra_name_key(getattr(t, "title", None))
            if key:
                reserved.add(key)
    return reserved


def disambiguate_extra_name(base_name: str, reserved: Iterable[str] | None, disc_number: Any) -> str:
    """Append `` (Disc N)`` when ``base_name`` collides with a reserved name.

    Idempotent: a name already carrying the suffix compares on its stem and
    is not suffixed twice.
    """
    if not base_name or not reserved or disc_number is None:
        return base_name
    key = extra_name_key(base_name)
    if key is None or key not in set(reserved):
        return base_name
    if _DISC_SUFFIX.search(base_name):
        return base_name
    try:
        n = int(disc_number)
    except (TypeError, ValueError):
        return base_name
    return f"{base_name} (Disc {n})"
