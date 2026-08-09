"""Plex vs Jellyfin subfolder names for disc title `type` (extras / theme media).

Plex does not treat a top-level ``Extras`` folder like Jellyfin's ``extras``/``samples``/etc.;
types that only have distinct Jellyfin folders are routed to Plex's ``Other`` folder instead.
See project docs for Plex local extras and Jellyfin extras layout.
"""

from __future__ import annotations

# Canonical type -> (plex_folder_name, jellyfin_folder_name)
_EXTRAS_SUBFOLDERS: dict[str, tuple[str, str]] = {
    "BehindTheScenes": ("Behind The Scenes", "behind the scenes"),
    "DeletedScene": ("Deleted Scenes", "deleted scenes"),
    "Featurette": ("Featurettes", "featurettes"),
    "Interview": ("Interviews", "interviews"),
    "Scene": ("Scenes", "scenes"),
    "Short": ("Shorts", "shorts"),
    "Trailer": ("Trailers", "trailers"),
    "Other": ("Other", "other"),
    "Extra": ("Other", "extras"),
    "Sample": ("Other", "samples"),
    "Clip": ("Other", "clips"),
    "ThemeMusic": ("Other", "theme-music"),
    "Backdrop": ("Other", "backdrops"),
}


# Plex's episode-level extras use a filename suffix, not a folder:
#   <episode filename>-<Descriptive Name>-<suffix>.ext
# The suffix vocabulary is Plex's, and smaller than the folder set — types
# without a dedicated suffix fall back to "other". Jellyfin has no
# episode-level extras at all; its callers never reach this map.
_PLEX_EPISODE_EXTRA_SUFFIXES: dict[str, str] = {
    "BehindTheScenes": "behindthescenes",
    "DeletedScene": "deleted",
    "Featurette": "featurette",
    "Interview": "interview",
    "Scene": "scene",
    "Short": "short",
    "Trailer": "trailer",
}


def plex_episode_extra_suffix_for_type(canonical_type: str | None) -> str | None:
    """Plex filename suffix for an episode-level extra, or None for non-extras.

    Returns None exactly when :func:`extras_subfolder_for_type` would — main
    content and ignored rows are not extras and get no suffix.
    """
    if extras_subfolder_for_type(canonical_type, "plex") is None:
        return None
    return _PLEX_EPISODE_EXTRA_SUFFIXES.get(str(canonical_type).strip(), "other")


def extras_subfolder_for_type(canonical_type: str | None, media_server: str) -> str | None:
    """Return the extras subfolder for this title type, or None for main content rows.

    canonical_type should already be normalized (e.g. via normalize_title_type_for_api).
    """
    if not canonical_type or not str(canonical_type).strip():
        return None
    canon = str(canonical_type).strip()
    if canon in ("MainMovie", "Episode", "ignore"):
        return None
    row = _EXTRAS_SUBFOLDERS.get(canon)
    if not row:
        return None
    ms = (media_server or "plex").strip().lower()
    return row[1] if ms == "jellyfin" else row[0]
