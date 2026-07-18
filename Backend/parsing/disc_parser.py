"""
Centralized helpers to turn raw drive-manager payloads into interpreted disc metadata.
Keep parsing/normalization here so the backend is the single source of truth.
"""
import logging
from typing import Any, Dict

from core.discdb_enrichment import merge_discdb_enrichment_into_titles
from core.settings import get_discdb_miss_workflow_with_prefill
from core.utils import parse_info_log, infer_resolution_from_log
from core.utils import (
    build_release_slug,
    slugify,
    normalize_disc_format,
    default_disc_name,
    slugify_disc_name,
)

log = logging.getLogger("parsing.disc_parser")


def _format_slug(fmt: str | None) -> str | None:
    fmt = normalize_disc_format(fmt)
    if not fmt:
        return None
    if fmt == "UHD":
        return "4k"
    if fmt == "Blu-Ray":
        return "blu-ray"
    if fmt == "DVD":
        return "dvd"
    return slugify(fmt)


def _title_case(name: str | None) -> str | None:
    if not name:
        return name
    return " ".join(w.capitalize() if w else "" for w in str(name).split())


def _parse_info_log(raw_info_log: Any) -> dict:
    """
    Best-effort parsing of a makemkv info log. Returns titles/scan_tracks/info_title hints.
    """
    if not raw_info_log:
        return {}
    if isinstance(raw_info_log, list):
        raw_info_log = "\n".join(raw_info_log)
    if not isinstance(raw_info_log, str):
        return {}
    try:
        parsed = parse_info_log(raw_info_log)
    except Exception as exc:
        log.warning("parse_info_log failed: %s", exc)
        return {}
    res, fmt = infer_resolution_from_log(raw_info_log)
    parsed.setdefault("resolution", res)
    parsed.setdefault("disc_format", fmt)
    parsed.setdefault("raw_info_log", raw_info_log)
    return parsed


def hydrate_disc_payload(disc_num: str, mount_point: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize/interpret a raw payload from drive-manager:
      - Parse raw_info_log/info_log for scan_tracks/titles/info_title/resolution/format hints.
      - Normalize label flags and suggest slugs.
    """
    payload = payload or {}
    # If we already hydrated this payload (cached), skip re-parsing raw_info_log.
    if payload.get("_hydrated"):
        hydrated: Dict[str, Any] = dict(payload)
        hydrated["disc_num"] = str(disc_num)
        hydrated["mount_point"] = mount_point
        return hydrated

    hydrated: Dict[str, Any] = dict(payload or {})
    hydrated["disc_num"] = str(disc_num)
    hydrated["mount_point"] = mount_point

    scan_tracks: list[dict] = []
    info_label = (
        hydrated.get("info_title")
        or hydrated.get("info_label")
        or hydrated.get("show_title")
        or hydrated.get("release_name")
        or hydrated.get("disc_label")
    )
    # keep format inference separate from resolution so we don't accidentally treat "2160p" as format
    inferred_format = hydrated.get("disc_format") or hydrated.get("format")
    raw_info_log = hydrated.get("raw_info_log") or hydrated.get("info_log") or hydrated.get("makemkv_info_log") or hydrated.get("info_log_raw")

    parsed_log = _parse_info_log(raw_info_log)
    if parsed_log:
        hydrated["raw_info_log"] = parsed_log.get("raw_info_log") or raw_info_log
        titles_map = parsed_log.get("titles_map") or {}
        if titles_map:
            if not hydrated.get("titles"):
                # Create hydrated titles from titles_map
                hydrated_titles = {}
                for k, v in titles_map.items():
                    title_data = dict(v) if isinstance(v, dict) else {}
                    # Use the file from titles_map as source_file if not already set
                    if "file" in title_data and "source_file" not in title_data:
                        title_data["source_file"] = title_data["file"]
                    # Also set track_id and title_id if not present
                    if "source_file" in title_data and "track_id" not in title_data:
                        title_data["track_id"] = title_data["source_file"]
                    if "source_file" in title_data and "title_id" not in title_data:
                        title_data["title_id"] = title_data["source_file"]
                    hydrated_titles[str(k)] = title_data
                hydrated["titles"] = hydrated_titles
            else:
                # Ensure source_file is set from titles_map for existing titles
                for k, v in titles_map.items():
                    title_key = str(k)
                    if title_key in hydrated["titles"]:
                        title_data = hydrated["titles"][title_key]
                        title_map_data = dict(v) if isinstance(v, dict) else {}
                        # Set source_file from titles_map if missing in existing title
                        if "file" in title_map_data and not title_data.get("source_file"):
                            title_data["source_file"] = title_map_data["file"]
                            # Also set track_id and title_id if not present
                            if "source_file" in title_data and "track_id" not in title_data:
                                title_data["track_id"] = title_data["source_file"]
                            if "source_file" in title_data and "title_id" not in title_data:
                                title_data["title_id"] = title_data["source_file"]
        scan_tracks = parsed_log.get("scan_tracks") or scan_tracks
        if (
            hydrated.get("discdb_hit")
            and isinstance(hydrated.get("tracks"), dict)
            and scan_tracks
        ):
            scan_tracks = merge_discdb_enrichment_into_titles(
                list(scan_tracks),
                hydrated["tracks"],
                content_hash=hydrated.get("disc_hash"),
                strip_discdb_ignore_type=get_discdb_miss_workflow_with_prefill(),
            )
        # Merge scan_tracks metadata into hydrated titles if titles exist
        # scan_tracks is a list of dicts, merge with titles_map to enrich titles with metadata
        if hydrated.get("titles") and scan_tracks:
            # Convert scan_tracks list to dict keyed by index for easier merging
            scan_tracks_by_index = {t.get("index"): t for t in scan_tracks if t.get("index") is not None}
            for title_key, title_data in hydrated["titles"].items():
                try:
                    title_index = int(title_key)
                    scan_data = scan_tracks_by_index.get(title_index)
                    if scan_data:
                        # Merge scan_tracks metadata into title_data, preserving source_file from titles_map
                        # source_file from titles_map (from parse_log) should take precedence
                        existing_source_file = title_data.get("source_file")
                        for k, v in scan_data.items():
                            # Preserve source_file from titles_map, don't overwrite with scan_data
                            if k == "source_file":
                                if not existing_source_file and v:
                                    title_data[k] = v
                            else:
                                # Merge other fields from scan_tracks
                                title_data[k] = v
                except ValueError:
                    pass
        info_label = parsed_log.get("info_title") or info_label
        inferred_format = inferred_format or parsed_log.get("disc_format")
        if parsed_log.get("resolution"):
            hydrated.setdefault("resolution", parsed_log.get("resolution"))
        hydrated.setdefault("cinfo_lines", parsed_log.get("cinfo_lines"))

    if scan_tracks:
        hydrated["scan_tracks"] = scan_tracks
    else:
        cached_scan = hydrated.get("scan_tracks") or []
        if cached_scan:
            hydrated["scan_tracks"] = cached_scan

    if info_label:
        nice_name = _title_case(info_label)
        hydrated["info_title"] = hydrated.get("info_title") or nice_name or info_label
        hydrated.setdefault("release_name", hydrated.get("release_name") or hydrated.get("info_title") or nice_name or info_label)
        # Only set movie_name from info_title if we have DiscDB data (tmdb_id indicates DiscDB hit)
        # Do not set movie_name from regular disc info_title to prevent accidental movie creation
        if hydrated.get("tmdb_id"):
            # Only set movie_name if we have TMDB data (DiscDB hit)
            hydrated.setdefault("movie_name", hydrated.get("movie_name") or hydrated.get("show_title") or hydrated.get("info_title") or nice_name or info_label)
        # For regular disc scans without DiscDB, don't set movie_name from info_title
        # It will only be set if it's already in the payload (from user input or previous DiscDB data)
    else:
        log.info("No CINFO info_title found for disc=%s", disc_num)

    # Only fall back to resolution if we still don't have a format.
    inferred_format = hydrated.get("disc_format") or hydrated.get("format") or inferred_format or hydrated.get("resolution")
    if inferred_format:
        inferred_format = normalize_disc_format(inferred_format)
        hydrated["disc_format"] = inferred_format
        hydrated.setdefault("format", inferred_format)

    composed_disc = default_disc_name(inferred_format, hydrated.get("info_title"))
    if composed_disc:
        hydrated.setdefault("disc_name", composed_disc)
        hydrated.setdefault("disc_slug", slugify_disc_name(composed_disc))
    elif (hydrated.get("group_type") or hydrated.get("title_type") or "movie") == "movie":
        if inferred_format == "UHD":
            hydrated.setdefault("disc_name", "UHD")
            hydrated.setdefault("disc_slug", slugify_disc_name("UHD"))
        elif inferred_format == "Blu-Ray":
            hydrated.setdefault("disc_name", "Blu-Ray")
            hydrated.setdefault("disc_slug", slugify_disc_name("Blu-Ray"))

    # normalize label flags for DiscDB misses
    if "label_required" not in hydrated:
        hydrated["label_required"] = False
    if hydrated.get("label_required") and "label_ready" not in hydrated:
        hydrated["label_ready"] = False
    if "label_ready" not in hydrated:
        hydrated["label_ready"] = True
    # Surface discdb miss if we have no hash/title yet but a placeholder is being used
    if "discdb_miss" not in hydrated and not hydrated.get("disc_hash"):
        hydrated["discdb_miss"] = True

    hydrated.setdefault("group_type", hydrated.get("title_type") or "movie")
    if not hydrated.get("disc_group"):
        base_title = hydrated.get("movie_name") or hydrated.get("show_title") or hydrated.get("disc_label") or hydrated.get("release_name")  # Backward compat
        hydrated["disc_group"] = build_release_slug(base_title, None) if base_title else None

    year = hydrated.get("release_year") or hydrated.get("production_year")
    fmt_slug = _format_slug(inferred_format)
    if year and fmt_slug:
        hydrated.setdefault("suggested_release_slug", f"{year}-{fmt_slug}")
    elif year:
        hydrated.setdefault(
            "suggested_release_slug",
            build_release_slug(hydrated.get("movie_name") or hydrated.get("show_title") or hydrated.get("release_name"), year),  # Backward compat
        )
    disc_slug_candidate = hydrated.get("disc_slug") or hydrated.get("disc_name")
    hydrated.setdefault(
        "suggested_disc_slug", slugify_disc_name(disc_slug_candidate) if disc_slug_candidate else ""
    )

    hydrated["_hydrated"] = True
    log.info(
        "Hydrate raw payload disc=%s info_title=%s format=%s year=%s scan_tracks=%s",
        disc_num,
        hydrated.get("info_title"),
        hydrated.get("disc_format"),
        year,
        len(hydrated.get("scan_tracks") or []),
    )
    return hydrated
