"""
Durable disc-scoped preview storage paths (#355).

Previews are stored at {DATA_ROOT}/disc_previews/{disc_id}/{title_id}/
so they survive job cleanup and can be shared across re-rips.
"""
from __future__ import annotations

import os
from pathlib import Path

from core.utils import get_mkvauto_root


def get_disc_preview_root() -> Path:
    """Base directory for all disc-scoped previews."""
    root = get_mkvauto_root()
    return root / "disc_previews"


def get_disc_preview_dir(disc_id: str, title_id: str) -> Path:
    """Directory for a specific disc+title preview (contains preview.m3u8, segments, thumbnail)."""
    return get_disc_preview_root() / disc_id / title_id


def get_disc_preview_manifest(disc_id: str, title_id: str) -> Path:
    """Path to the HLS manifest for a disc+title preview."""
    return get_disc_preview_dir(disc_id, title_id) / "preview.m3u8"


def ensure_disc_preview_dir(disc_id: str, title_id: str) -> Path:
    """Create and return the preview directory for a disc+title."""
    d = get_disc_preview_dir(disc_id, title_id)
    d.mkdir(parents=True, exist_ok=True)
    return d
