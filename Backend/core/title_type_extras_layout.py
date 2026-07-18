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
