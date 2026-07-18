"""Canonical disc title `type` strings (match frontend <option> values).

Used for both API responses and DB persistence so reads and writes stay consistent.
"""

from __future__ import annotations

# Lowercase / legacy input -> canonical PascalCase (or "ignore")
_ALIAS_TO_CANONICAL: dict[str, str] = {
    # Main content
    "movie": "MainMovie",
    "mainmovie": "MainMovie",
    "main": "MainMovie",
    "episode": "Episode",
    "ignore": "ignore",
    # Legacy / API variants
    "deletedscene": "DeletedScene",
    "deleted": "DeletedScene",
    "extra": "Extra",
    "trailer": "Trailer",
    # Extras (interchangeable + extended)
    "behindthescenes": "BehindTheScenes",
    "behind the scenes": "BehindTheScenes",
    "behind_the_scenes": "BehindTheScenes",
    "featurette": "Featurette",
    "featurettes": "Featurette",
    "interview": "Interview",
    "interviews": "Interview",
    "scene": "Scene",
    "scenes": "Scene",
    "short": "Short",
    "shorts": "Short",
    "other": "Other",
    "sample": "Sample",
    "samples": "Sample",
    "clip": "Clip",
    "clips": "Clip",
    "thememusic": "ThemeMusic",
    "theme-music": "ThemeMusic",
    "theme_music": "ThemeMusic",
    "backdrop": "Backdrop",
    "backdrops": "Backdrop",
}

# Canonical values that round-trip when lowercased for lookup
for _c in (
    "MainMovie",
    "Episode",
    "DeletedScene",
    "Extra",
    "Trailer",
    "BehindTheScenes",
    "Featurette",
    "Interview",
    "Scene",
    "Short",
    "Other",
    "Sample",
    "Clip",
    "ThemeMusic",
    "Backdrop",
):
    _ALIAS_TO_CANONICAL[_c.lower()] = _c


def normalize_title_type_for_api(value: str | None) -> str | None:
    """Map parser / client / legacy DB values to the canonical type string."""
    if not value:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    key = normalized.lower().strip()
    key_ws = " ".join(key.split())
    if key_ws in _ALIAS_TO_CANONICAL:
        return _ALIAS_TO_CANONICAL[key_ws]
    if key in _ALIAS_TO_CANONICAL:
        return _ALIAS_TO_CANONICAL[key]
    compact = key.replace("-", "").replace("_", "").replace(" ", "")
    if compact in _ALIAS_TO_CANONICAL:
        return _ALIAS_TO_CANONICAL[compact]
    # Unknown: avoid bad folder names from arbitrary strings
    return "Extra"


# Alias: persist the same strings the API exposes
normalize_title_type_for_storage = normalize_title_type_for_api
