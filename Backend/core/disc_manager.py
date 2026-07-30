"""
Disc Manager module.
Primary interface for Backend API to interact with disc information.
Handles parsing, DiscDB queries, and formatting. Returns data structures only (no DB access).
"""
import logging
import os
from typing import Optional, Dict, List, Any, Callable

"""
Public interface for disc operations.
This is the ONLY module that should be imported by API routers for disc operations.

All drive operations are accessed through this module, which enforces proper separation
of concerns and prevents direct access to low-level drive operations.
"""
from core._drive_operations import (
    list_drives as _list_drives,
    get_disc_info as _get_disc_info,
    refresh_disc_info as _refresh_disc_info,
    scan_disc_info as _scan_disc_info,
    hash_disc as _hash_disc,
)
from fastapi import HTTPException

# Create DriveManagerError for compatibility
class DriveManagerError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
from core.disc_cache import get as cache_get, set_payload as cache_set
from core.disc_locks import is_operation_active as _is_operation_active, get_active_operations
from core import settings as app_settings
from core.discdb_enrichment import merge_discdb_enrichment_into_titles
from core.utils import (
    retrieve_discdb_data,
    parse_discdb_data,
    parse_log,
    parse_title_metadata,
    infer_resolution_from_log,
    extract_info_title,
    is_dev_mode,
)
from core import tmdb_client
# Lock files removed - using drive manager state tracking instead

logger = logging.getLogger(__name__)

# Score above which a TMDB candidate is confident enough to pre-seed
# `disc.label_draft` on a DiscDB miss (#388). Tuneable — below this we
# still persist the suggestion under `disc.disc_info.tmdb_suggestion`
# for the film step to render, just don't seed the draft so the user
# starts on the empty form and avoids being primed with a bad match.
TMDB_LABEL_DRAFT_SEED_THRESHOLD = 0.75

# Backend API callback for notifications
_backend_notify_callback: Optional[Callable] = None

# Optional DB-backed DiscDB lookup callback; set by API layer at startup.
# When registered, query_discdb() checks the database before hitting the API.
_db_discdb_lookup: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None


def register_backend_callback(callback: Callable) -> None:
    """
    Register a callback function that Disc Manager will call to notify Backend API.
    
    Args:
        callback: Callable function that accepts (event_type: str, data: dict)
                  or separate functions for different events
    """
    global _backend_notify_callback
    _backend_notify_callback = callback
    logger.info("Registered Backend API callback for Disc Manager notifications")


def register_db_discdb_lookup(fn: Callable[[str], Optional[Dict[str, Any]]]) -> None:
    """
    Register a DB-backed DiscDB lookup function.

    Called by the API layer at startup so that query_discdb() can check the database
    before hitting TheDiscDB API.  The function receives a content_hash and returns
    a dict matching query_discdb() return shape (discdb_hit, movie_name, etc.) if
    the disc is already known, or None to fall through to the API.
    """
    global _db_discdb_lookup
    _db_discdb_lookup = fn
    logger.info("Registered DB-backed DiscDB lookup callback")


def is_operation_active(disc_num: str, operation_type: str, mount_point: str | None = None) -> bool:
    """
    Compatibility wrapper for disc lock checks.
    Prefers mount_point when available (stable physical identity).
    """
    key = mount_point or disc_num
    return _is_operation_active(key, operation_type, mount_point=mount_point)


def fetch_disc_info(
    disc_num: str,
    mount_point: str,
    timeout: float = 60.0,
    refresh: bool = False
) -> Dict[str, Any]:
    """
    Compatibility wrapper for drive manager fetch.
    """
    return _get_disc_info(str(disc_num), mount_point, refresh=refresh)


def on_disc_inserted(disc_num: str, mount_point: str) -> None:
    """
    Notify that a disc has been inserted (early notification, before scanning).
    Calls Backend API callback if registered.
    
    Args:
        disc_num: Disc number (may be "9999" if not yet identified)
        mount_point: Mount point (e.g., "/dev/sr1")
    """
    import json, time
    if _backend_notify_callback:
        try:
            # Call the backend notification function for disc insertion
            _backend_notify_callback(disc_num, mount_point)
            logger.info(f"Notified Backend API of disc insertion: disc_num={disc_num} mount_point={mount_point}")
        except Exception as exc:
            logger.error(f"Failed to notify Backend API of disc insertion: {exc}")
    else:
        logger.debug("No Backend API callback registered, skipping disc insertion notification")


def on_disc_scan_complete(raw_data: dict) -> None:
    """
    Notify that disc scanning is complete with raw data from Drive Manager.
    Parses info log, queries DiscDB, enriches data, and notifies Backend API.
    
    Args:
        raw_data: Raw data dict from Drive Manager with keys:
                  - disc_num: str
                  - mount_point: str
                  - disc_hash: str (optional)
                  - info_log: str (optional)
    """
    disc_num = str(raw_data.get("disc_num", ""))
    mount_point = raw_data.get("mount_point", "")
    content_hash = raw_data.get("disc_hash") or raw_data.get("content_hash")
    info_log = raw_data.get("info_log") or raw_data.get("raw_info_log")
    
    logger.debug(f"Processing disc scan completion: disc_num={disc_num} mount_point={mount_point}")
    
    # Parse info log if present
    parsed_info = {}
    if info_log:
        try:
            parsed_info = parse_info_log(info_log)
            logger.debug(f"Parsed info log for disc {disc_num}")
        except Exception as exc:
            logger.warning(f"Failed to parse info log for disc {disc_num}: {exc}")
    
    # Query DiscDB if we have a hash
    discdb_data = {}
    if content_hash:
        try:
            discdb_data = query_discdb(content_hash)
            logger.debug(f"DiscDB query for disc {disc_num}: hit={bool(discdb_data)}")
        except Exception as exc:
            logger.warning(f"DiscDB query failed for disc {disc_num}: {exc}")
    
    # Merge all data into enriched disc_info
    disc_info = {
        "disc_num": disc_num,
        "mount_point": mount_point,
        "disc_hash": content_hash,
        **raw_data,  # Include all raw info from Drive Manager
        **parsed_info,  # Include parsed info log data
    }
    
    # Add DiscDB data if available
    # Check discdb_hit flag explicitly, not just if discdb_data is truthy
    # (query_discdb can return a dict with discdb_hit=False for misses)
    if discdb_data and discdb_data.get("discdb_hit"):
        disc_info.update(discdb_data)
        disc_info["discdb_hit"] = True
        disc_info["discdb_miss"] = False
    else:
        # DiscDB miss: merge any partial data but set hit/miss flags correctly
        if discdb_data:
            disc_info.update(discdb_data)
        disc_info["discdb_hit"] = False
        disc_info["discdb_miss"] = True
    app_settings.apply_discdb_miss_workflow_prefill_to_payload(disc_info)

    # TMDB auto-identification (#388, part of epic #386). Runs in parallel to
    # the DiscDB query above so the film step opens with a pre-filled best
    # guess instead of a blank form. No-op when no key is configured or the
    # devmode toggle disables it.
    tmdb_suggestion = query_tmdb(disc_info, content_hash)
    if tmdb_suggestion:
        disc_info["tmdb_suggestion"] = tmdb_suggestion
        # Seed label_draft on DiscDB miss when TMDB is confident. The persist
        # layer copies label_draft_seed → disc.label_draft only when no draft
        # exists yet, so re-scans never trample a user's edits.
        if (
            disc_info.get("discdb_miss")
            and tmdb_suggestion.get("score", 0.0) >= TMDB_LABEL_DRAFT_SEED_THRESHOLD
        ):
            disc_info["label_draft_seed"] = {
                "tmdb_id": tmdb_suggestion["tmdb_id"],
                "tmdb_type": tmdb_suggestion["tmdb_type"],
                "title": tmdb_suggestion["title"],
                "year": tmdb_suggestion["year"],
                "cover_url": tmdb_suggestion["cover_url"],
                "group_type": "series" if tmdb_suggestion["tmdb_type"] == "tv" else "movie",
                "source": "tmdb_auto",
            }

    # Update cache — keyed by mount_point (stable physical identity)
    try:
        cache_set(mount_point or disc_num, disc_info)
        logger.debug(f"Updated cache for mount_point={mount_point} disc_num={disc_num}")
    except Exception as exc:
        logger.warning(f"Failed to update cache for mount_point={mount_point} disc_num={disc_num}: {exc}")
    
    # Notify Backend API if callback registered
    import json, time
    if _backend_notify_callback:
        try:
            # Call the backend notification function for disc scan completion
            _backend_notify_callback(disc_info)
            logger.info(f"Notified Backend API of disc scan completion: disc_num={disc_num}")
        except Exception as exc:
            logger.error(f"Failed to notify Backend API of disc scan completion: {exc}")
    else:
        logger.debug("No Backend API callback registered, skipping disc scan completion notification")


def parse_info_log(info_log: str | List[str] | None) -> Dict[str, Any]:
    """
    Parse raw makemkv info log into structured data.
    
    Args:
        info_log: Raw info log (string or list of lines)
    
    Returns:
        Dict with parsed data (titles, scan_tracks, resolution, disc_format)
    """
    if not info_log:
        return {}
    
    # Convert list to string if needed
    if isinstance(info_log, list):
        log_text = "\n".join(info_log)
    else:
        log_text = str(info_log)
    
    if not log_text.strip():
        return {}
    
    try:
        titles_map = parse_log(log_text)
        scan_tracks = parse_title_metadata(log_text)
        resolution, disc_format = infer_resolution_from_log(log_text)
        
        return {
            "titles": titles_map,
            "scan_tracks": scan_tracks,
            "resolution": resolution,
            "disc_format": disc_format,
        }
    except Exception as exc:
        logger.warning(f"Error parsing info log: {exc}")
        return {}


def _resolve_info_title_for_tmdb(disc_info: Dict[str, Any]) -> Optional[str]:
    """Pick the best title string from a scan-completion disc_info for TMDB search.

    Order: explicit info_title, info_label / show_title / release_name (set
    by DiscDB), then derive from the raw MakeMKV log CINFO lines.
    """
    for key in ("info_title", "info_label", "show_title", "release_name"):
        v = disc_info.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    raw_log = (
        disc_info.get("raw_info_log")
        or disc_info.get("info_log")
        or disc_info.get("makemkv_info_log")
    )
    if raw_log:
        try:
            chosen, _ = extract_info_title(raw_log)
            if chosen:
                return chosen.strip()
        except Exception:  # pragma: no cover — defensive only
            return None
    return None


def query_tmdb(disc_info: Dict[str, Any], content_hash: Optional[str]) -> Dict[str, Any]:
    """Query TMDB by the disc's info_title and return a suggestion block.

    Returns ``{}`` (no suggestion) when:
      * TMDB is disabled in devmode (``settings.get_tmdb_disabled``)
      * No API key is configured (``settings.get_tmdb_api_key``)
      * The disc has no usable title string
      * TMDB returns no results, errors, or times out

    Errors are caught and logged at WARNING — a TMDB failure must never
    block the disc-scan-completion path, since DiscDB and the user-driven
    label flow remain functional without it.
    """
    if app_settings.get_tmdb_disabled():
        return {}
    if not app_settings.get_tmdb_api_key():
        return {}
    info_title = _resolve_info_title_for_tmdb(disc_info)
    if not info_title:
        return {}
    try:
        normalized, hints = tmdb_client.normalize_title(info_title)
        if not normalized:
            return {}
        candidates = tmdb_client.search_title(normalized, limit=3)
    except tmdb_client.TmdbError as exc:
        logger.warning(
            "TMDB query failed for content_hash=%s info_title=%r: %s",
            content_hash, info_title, exc,
        )
        return {}
    if not candidates:
        logger.debug(
            "TMDB miss for content_hash=%s query=%r hints=%s",
            content_hash, normalized, hints,
        )
        return {}
    top = candidates[0]
    logger.info(
        "TMDB suggestion for content_hash=%s query=%r → %r (%s, %s, score=%.2f) "
        "candidates=%s",
        content_hash, normalized, top.title, top.tmdb_type, top.year, top.score,
        [(c.title, c.year, round(c.score, 2)) for c in candidates],
    )
    return {
        "tmdb_id": top.tmdb_id,
        "tmdb_type": top.tmdb_type,
        "title": top.title,
        "year": top.year,
        "cover_url": top.cover_url,
        "score": top.score,
        "normalized_query": normalized,
        "hints": hints,
        "candidates": [c.to_dict() for c in candidates],
    }


def backfill_tmdb_suggestions_for_unlabeled_discs(db, max_discs: int = 50) -> Dict[str, int]:
    """Iterate over discs that are still unlabeled (no linked release) and missing
    a tmdb_suggestion, run query_tmdb on each, and persist the result.

    Triggered from the settings flow when the user adds (or rotates) a TMDB API
    key — without this, only discs scanned AFTER the key was configured would
    ever get a suggestion. With this, an existing user who plugs in their key
    immediately sees suggestions for everything they haven't labeled yet.

    Args:
        db: SQLAlchemy session
        max_discs: cap on how many discs to process in a single call. Protects
            against runaway TMDB-quota usage if a user has hundreds of unlabeled
            discs sitting around.

    Returns:
        ``{scanned, updated, seeded}`` — how many discs were inspected, how many
        got a tmdb_suggestion written, and how many of those also got
        label_draft seeded (score ≥ TMDB_LABEL_DRAFT_SEED_THRESHOLD).
    """
    # Late import to avoid circular dependency: api.models depends on core.
    from api import models

    if app_settings.get_tmdb_disabled() or not app_settings.get_tmdb_api_key():
        return {"scanned": 0, "updated": 0, "seeded": 0}

    rows = (
        db.query(models.Disc)
        .filter(models.Disc.release_id.is_(None))
        .order_by(models.Disc.created_at.desc())
        .limit(max_discs)
        .all()
    )
    scanned = 0
    updated = 0
    seeded = 0
    for disc in rows:
        scanned += 1
        existing_info = disc.disc_info if isinstance(disc.disc_info, dict) else {}
        if existing_info.get("tmdb_suggestion"):
            continue  # already has one — idempotent skip

        query_input: Dict[str, Any] = {
            **existing_info,
            "info_title": disc.info_title,
        }
        suggestion = query_tmdb(query_input, disc.content_hash)
        if not suggestion:
            continue

        new_info = {**existing_info, "tmdb_suggestion": suggestion}
        disc.disc_info = new_info
        updated += 1

        # Seed label_draft only when the disc has none yet. Mirrors the rule
        # in _seed_label_draft_from_tmdb (crud.py): user edits are never
        # overwritten by an auto-fill, even on a key-save backfill.
        score = float(suggestion.get("score") or 0.0)
        existing_draft = disc.label_draft if isinstance(disc.label_draft, dict) else {}
        if not existing_draft and score >= TMDB_LABEL_DRAFT_SEED_THRESHOLD:
            disc.label_draft = {
                "tmdb_id": suggestion.get("tmdb_id"),
                "tmdb_type": suggestion.get("tmdb_type"),
                "title": suggestion.get("title"),
                "year": suggestion.get("year"),
                "cover_url": suggestion.get("cover_url"),
                "group_type": "series" if suggestion.get("tmdb_type") == "tv" else "movie",
                "source": "tmdb_auto",
            }
            seeded += 1

    if updated:
        db.commit()
    return {"scanned": scanned, "updated": updated, "seeded": seeded}


def extract_upstream_coords(raw_db_query: dict, content_hash: str) -> dict | None:
    """Pull TheDiscDB's own location for the matched disc out of the hit query.

    Returns ``{film_title, film_year, release_slug, disc_index}`` — enough to
    reconstruct ``data/movie/{Film (Year)}/{release_slug}/disc{NN}`` — or None
    when the shape is unexpected. ``global_disc_id`` rides along when upstream
    has one, so an update from a record that predates AACS-ID capture keeps
    their value instead of deleting it. Best-effort by design: a miss here only
    means an update exports the old way (as a new release directory).
    """
    try:
        nodes = (raw_db_query or {}).get("mediaItems", {}).get("nodes") or []
        for node in nodes:
            for release in node.get("releases") or []:
                for disc in release.get("discs") or []:
                    if (disc.get("contentHash") or "").upper() == (content_hash or "").upper():
                        idx = disc.get("index")
                        if idx is None or not release.get("slug") or not node.get("title"):
                            return None
                        coords = {
                            "film_title": node["title"],
                            "film_year": node.get("year"),
                            "release_slug": release["slug"],
                            "disc_index": int(idx),
                        }
                        if disc.get("globalDiscId"):
                            coords["global_disc_id"] = disc["globalDiscId"]
                        return coords
    except Exception as exc:  # noqa: BLE001 - shape drift must not break scans
        logger.warning("Could not extract upstream coords: %s", exc)
    return None


def query_discdb(content_hash: str) -> Dict[str, Any]:
    """
    Query DiscDB API for disc metadata.

    Checks the database first via a registered callback (see register_db_discdb_lookup).
    If the disc already exists in the DB with a linked release, the cached data is
    returned without hitting TheDiscDB API.  Falls through to the API for unknown
    discs or known misses (where data may have been added to TheDiscDB since last check).
    
    Args:
        content_hash: Disc content hash
    
    Returns:
        Dict with DiscDB data (movie_name, release_image, tracks, etc.)
    """
    # Check DB first — skip the API call if we already have data for this disc
    if _db_discdb_lookup is not None:
        try:
            db_result = _db_discdb_lookup(content_hash)
            if db_result is not None:
                logger.debug("DiscDB DB cache hit for hash %s", content_hash[:12])
                app_settings.apply_discdb_miss_workflow_prefill_to_payload(db_result)
                return db_result
        except Exception as exc:
            logger.warning("DB DiscDB lookup failed, falling back to API: %s", exc)

    try:
        if is_dev_mode():
            pass
        
        raw_db_query = retrieve_discdb_data(content_hash)
        
        # Parse DiscDB data
        (
            movie_name,
            release_image,
            disc_slug,
            db_mapping,
            resolution,
            disc_format,
            title_type,
            disc_group,
            release_year,
            release_date,
            original_year,
            original_release_date,
            release_discs,
            tmdb_id,
            release_resolution,
            tmdb_type,
            production_year,
            matched_disc_index,
            discdb_boxset,
        ) = parse_discdb_data(raw_db_query, content_hash)

        group_type = title_type or "movie"

        result = {
            "movie_name": movie_name,
            "release_image": release_image,
            "disc_slug": disc_slug,
            "tracks": db_mapping,
            "resolution": resolution,
            "disc_format": disc_format,
            "title_type": title_type,
            "disc_group": disc_group or disc_slug,
            "group_type": group_type,
            "release_year": release_year,
            "release_date": release_date,
            "original_year": original_year,
            "original_release_date": original_release_date,
            "release_discs": release_discs,
            "tmdb_id": tmdb_id,
            "release_resolution": release_resolution,
            "tmdb_type": tmdb_type,
            "production_year": production_year,
            "discdb_hit": True,
            "label_required": False,
            "label_ready": True,
            # Raw GraphQL-shaped response for job raw/disc_db_query.json (labeling audit trail).
            "raw_db_query": raw_db_query,
        }
        if discdb_boxset:
            result["discdb_boxset"] = discdb_boxset
        if matched_disc_index is not None:
            result["discdb_disc_num"] = matched_disc_index
        # #753: upstream coordinates, captured while the lookup has them in
        # hand. An update export must overwrite TheDiscDB's existing files —
        # data/movie/{their film dir}/{their release slug}/disc{their index} —
        # or it lands as a duplicate sibling release instead of a correction.
        coords = extract_upstream_coords(raw_db_query, content_hash)
        if coords:
            result["discdb_upstream"] = coords
        app_settings.apply_discdb_miss_workflow_prefill_to_payload(result)
        return result
    except Exception as exc:
        logger.warning(f"DiscDB lookup failed: {exc}")
        return {
            "discdb_hit": False,
            "label_required": True,
            "label_ready": False,
            "error": str(exc),
        }


def get_disc_info(disc_num: str, mount_point: str, refresh: bool = False) -> Dict[str, Any]:
    """
    Get formatted disc info (cached or fetch from Drive Manager).
    
    Args:
        disc_num: Disc number
        mount_point: Mount point
        refresh: Force refresh (bypass cache)
    
    Returns:
        Dict with disc information (includes drive info: disc_num, mount_point)
    """
    # Check cache first if not refreshing — prefer mount_point (primary key)
    if not refresh:
        cached = cache_get(mount_point) if mount_point else None
        if not cached:
            cached = cache_get(str(disc_num))
        if cached:
            logger.debug(f"Cache hit for mount_point={mount_point} disc_num={disc_num}")
            # Ensure drive info is included
            cached.setdefault("disc_num", disc_num)
            cached.setdefault("mount_point", mount_point)
            return cached
    
    # Avoid scanning while any operation (hash, info, rip) is active when cache is empty.
    active = get_active_operations(str(disc_num))
    if active:
        raise DriveManagerError(
            f"Drive busy: another operation in progress for disc {disc_num} (active: {', '.join(active)})",
            status_code=409,
        )
    
    # Fetch from drive operations (direct call, no HTTP)
    try:
        if refresh:
            raw_info = _refresh_disc_info(str(disc_num), mount_point)
        else:
            raw_info = _get_disc_info(str(disc_num), mount_point, refresh=False)
    except HTTPException as exc:
        # Convert HTTPException to DriveManagerError for compatibility
        raise DriveManagerError(exc.detail, status_code=exc.status_code) from exc
    except Exception as exc:
        # Handle other exceptions
        if hasattr(exc, 'status_code'):
            raise DriveManagerError(str(exc), status_code=exc.status_code) from exc
        raise DriveManagerError(str(exc), status_code=500) from exc
    
    # Parse info log if present
    info_log = raw_info.get("info_log") or raw_info.get("raw_info_log")
    parsed_info = {}
    if info_log:
        parsed_info = parse_info_log(info_log)
    
    # Get content hash
    content_hash = raw_info.get("disc_hash") or raw_info.get("content_hash")
    
    # Query DiscDB if we have a hash
    discdb_data = {}
    if content_hash:
        discdb_data = query_discdb(content_hash)
    
    # Merge all data
    disc_info: Dict[str, Any] = {
        "disc_num": str(disc_num),
        "mount_point": mount_point,
        "disc_hash": content_hash,
        **raw_info,  # Include all raw info from Drive Manager
        **parsed_info,  # Include parsed info log data
    }
    
    # Override with DiscDB data if available (DiscDB takes precedence for metadata)
    if discdb_data.get("discdb_hit"):
        disc_info.update(discdb_data)
        # Enrich scan_tracks with DiscDB metadata (MakeMKV scan is canonical for structure/comment).
        try:
            scan_titles = list(disc_info.get("scan_tracks") or [])
            if scan_titles and isinstance(discdb_data.get("tracks"), dict):
                enriched = merge_discdb_enrichment_into_titles(
                    scan_titles,
                    discdb_data["tracks"],
                    content_hash=content_hash,
                    strip_discdb_ignore_type=app_settings.get_discdb_miss_workflow_with_prefill(),
                )
                disc_info["titles"] = enriched
                disc_info["scan_tracks"] = enriched
        except Exception as exc:  # noqa: S110
            logger.warning("DiscDB enrichment into scan titles failed: %s", exc)

        app_settings.apply_discdb_miss_workflow_prefill_to_payload(disc_info)
    else:
        # DiscDB miss: use parsed info for resolution/format if available
        if not disc_info.get("resolution") and parsed_info.get("resolution"):
            disc_info["resolution"] = parsed_info["resolution"]
        if not disc_info.get("disc_format") and parsed_info.get("disc_format"):
            disc_info["disc_format"] = parsed_info["disc_format"]
        # Set DiscDB miss flags
        disc_info["discdb_hit"] = False
        disc_info["label_required"] = True
        disc_info["label_ready"] = False
    
    # Ensure info_log is included
    if info_log:
        disc_info["info_log"] = info_log
        disc_info["raw_info_log"] = info_log if isinstance(info_log, str) else "\n".join(info_log)
    
    # Cache the result — keyed by mount_point (stable physical identity)
    cache_set(mount_point or str(disc_num), disc_info)
    
    return disc_info


def refresh_disc_info(disc_num: str, mount_point: str) -> Dict[str, Any]:
    """
    Force refresh disc info (only if no active operations).
    
    Args:
        disc_num: Disc number
        mount_point: Mount point
    
    Returns:
        Dict with refreshed disc information
    """
    # Check if any operation is active (one operation per drive)
    active_ops = get_active_operations(str(disc_num))
    if active_ops:
        raise DriveManagerError(
            f"Cannot refresh: operations active for disc {disc_num}: {active_ops}",
            status_code=409
        )
    
    return get_disc_info(disc_num, mount_point, refresh=True)


def get_disc_hash(disc_num: str, mount_point: str) -> str:
    """
    Get disc hash (cached or calculate via Drive Manager).
    
    Args:
        disc_num: Disc number
        mount_point: Mount point
    
    Returns:
        Disc content hash
    """
    # Check cache first — prefer mount_point (primary key)
    cached = cache_get(mount_point) if mount_point else None
    if not cached:
        cached = cache_get(str(disc_num))
    if cached and cached.get("disc_hash"):
        return cached["disc_hash"]
    
    # Fetch from drive operations (this will calculate hash if needed)
    try:
        info = _get_disc_info(str(disc_num), mount_point, refresh=False)
        content_hash = info.get("disc_hash") or info.get("content_hash")
        if content_hash:
            return content_hash
    except (DriveManagerError, HTTPException):
        pass
    
    raise DriveManagerError("Could not get disc hash", status_code=404)


def list_drives() -> List[Dict[str, Any]]:
    """
    List all drives with discs. Each dict includes disc_num, mount_point, and UI fields
    (drive_hardware_name, friendly_label, name, makemkv_disc_index).
    """
    try:
        drives_list = _list_drives()
        result: List[Dict[str, Any]] = []
        for drive_dict in drives_list:
            disc_num = drive_dict.get("disc_num")
            mount_point = drive_dict.get("mount_point")
            if disc_num and mount_point:
                result.append(dict(drive_dict))
        return result
    except Exception as exc:
        logger.error(f"Error listing drives: {exc}")
        return []


def get_cached_discs() -> List[Dict[str, Any]]:
    """
    Get currently inserted discs from cache only (no scan, no makemkvcon calls).
    This function does NOT trigger any disc scans or drive enumeration - it only uses cached data.
    
    Architecture: Drive Manager handles udev events and updates cache via disc_manager callbacks.
    This function reads from that cache without triggering any Drive Manager operations.
    
    When multiple cache entries exist for the same **mount_point** (block device), e.g. after
    MakeMKV renumbered a drive so payloads were stored under a new ``disc_num`` key while the old
    key was never removed, or after an eject was skipped and a new disc was scanned, the entry
    with the latest timestamp **per mount_point** wins. Physical tray identity is the device path,
    not the MakeMKV index.
    
    Returns:
        List of disc info dicts (each includes disc_num, mount_point, disc_hash, and disc_id if available)
    """
    try:
        # Iterate through cache entries to find discs with mount_point (indicating they're inserted)
        # Cache stores entries by disc_num, disc_hash, and disc_id
        # Access cache via internal module (cache is not exported, but we need to iterate it)
        import core.disc_cache as disc_cache_module

        # Group by mount_point (one carousel slot per block device). Multiple cache keys can refer
        # to the same device after DRV renumbering (same mount, different disc_num keys).
        slot_latest: Dict[str, tuple] = {}  # mount_point -> (timestamp, disc_info)

        for _key, timestamp, payload in disc_cache_module.snapshot_entries():
            disc_num = payload.get("disc_num")
            mount_point = payload.get("mount_point")
            if not disc_num or not mount_point:
                continue
            mp = str(mount_point).strip()
            disc_info = {
                "disc_num": str(disc_num),
                "mount_point": mp,
                **payload,
            }
            if mp not in slot_latest or timestamp > slot_latest[mp][0]:
                slot_latest[mp] = (timestamp, disc_info)
        
        return [disc_info for _, disc_info in slot_latest.values()]
    except Exception as exc:
        logger.error(f"Error getting cached discs: {exc}", exc_info=True)
        # Re-raise to allow callers to handle appropriately
        raise


def list_discs() -> List[Dict[str, Any]]:
    """
    List all discs with their associated drives (filters out empty drives).
    
    Returns:
        List of disc info dicts (each includes disc_num and mount_point)
    """
    try:
        # Get drives from drive operations (direct call, no HTTP)
        drives_list = _list_drives()
        
        discs = []
        for drive_dict in drives_list:
            disc_num = drive_dict.get("disc_num")
            mount_point = drive_dict.get("mount_point")
            if not disc_num or not mount_point:
                continue
            # Try to get cached info for this disc — prefer mount_point (primary key)
            cached = cache_get(mount_point) if mount_point else None
            if not cached:
                cached = cache_get(str(disc_num))
            if cached:
                # Include drive info
                disc_info = {
                    "disc_num": str(disc_num),
                    "mount_point": mount_point,
                    **cached,
                }
                discs.append(disc_info)
            else:
                # No cached info, but drive has a disc - include minimal info
                discs.append({
                    "disc_num": str(disc_num),
                    "mount_point": mount_point,
                    "pending": True,
                })
        
        return discs
    except Exception as exc:
        logger.error(f"Error listing discs: {exc}", exc_info=True)
        # Re-raise to allow callers to handle appropriately
        raise

