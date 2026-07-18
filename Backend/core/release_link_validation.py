"""
Standalone vs boxset release completeness for disc linking (mirrors api.crud.get_or_create_release checks).

Used to defer disc.release_id until metadata matches manual-create rules, without relaxing DB constraints.
"""
from __future__ import annotations

from typing import Any

# Match api.crud.VALID_GTIN_LENGTHS
VALID_GTIN_LENGTHS = (8, 12, 13, 14)


def _valid_gtin(upc: str | None) -> bool:
    if not upc:
        return False
    s = str(upc).strip()
    if not (s.isdigit() and len(s) in VALID_GTIN_LENGTHS):
        return False
    try:
        if int(s) == 0:
            return False
    except ValueError:
        return False
    return True


def normalize_gtin_from_discdb(raw: Any) -> str | None:
    """
    Persist NULL when DiscDB omits UPC or sends a non-real GTIN (all-zero, wrong length).
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if not s.isdigit():
        return None
    if len(s) not in VALID_GTIN_LENGTHS:
        return None
    try:
        if int(s) == 0:
            return None
    except ValueError:
        return None
    return s


def boxset_missing_field_keys(boxset: Any) -> list[str]:
    """Same required fields as api.crud.get_or_create_release when boxset_id is set."""
    missing: list[str] = []
    if not boxset:
        return ["boxset"]
    if not getattr(boxset, "name", None) or not str(boxset.name).strip():
        missing.append("name")
    y = getattr(boxset, "year", None)
    if y is None or not (1000 <= int(y) <= 9999):
        missing.append("year")
    if not _valid_gtin(getattr(boxset, "upc", None)):
        missing.append("upc")
    cover = getattr(boxset, "cover_front_url", None) or ""
    cover_str = str(cover).strip() if cover is not None else ""
    if not cover_str or not (
        cover_str.startswith("http://") or cover_str.startswith("https://")
    ):
        missing.append("cover_front_url")
    return missing


def standalone_release_missing_field_keys(release: Any) -> list[str]:
    """Same required fields as api.crud.get_or_create_release standalone branch (no boxset)."""
    missing: list[str] = []
    name = getattr(release, "name", None)
    if not name or not str(name).strip():
        missing.append("release_name")
    y = getattr(release, "release_year", None)
    if y is None:
        missing.append("release_year")
    else:
        try:
            yi = int(y)
            if not (1000 <= yi <= 9999):
                missing.append("release_year")
        except (TypeError, ValueError):
            missing.append("release_year")
    if not _valid_gtin(getattr(release, "upc", None)):
        missing.append("upc")
    cover = getattr(release, "cover_front_url", None) or ""
    cover_str = str(cover).strip() if cover is not None else ""
    if not cover_str or not (
        cover_str.startswith("http://") or cover_str.startswith("https://")
    ):
        missing.append("cover_front_url")
    return missing


def release_missing_required_field_keys(db: Any, release: Any) -> list[str]:
    """
    Return field keys missing for link-readiness. Uses boxset rules when release.boxset_id is set.
    """
    if not release:
        return ["release"]
    boxset_id = getattr(release, "boxset_id", None)
    if boxset_id:
        from api import models

        boxset = db.query(models.Boxset).filter(models.Boxset.id == boxset_id).first()
        return boxset_missing_field_keys(boxset)
    return standalone_release_missing_field_keys(release)


def release_link_ready(db: Any, release: Any) -> bool:
    return len(release_missing_required_field_keys(db, release)) == 0
