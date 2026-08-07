"""Data access helpers."""
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
import logging
from sqlalchemy import and_, or_, exists
from . import models
from core.disc import Disc
from core.utils import (
    run_makemkv,  # kept for test monkeypatching
    parse_log,    # kept for test monkeypatching
    slugify,
    normalize_disc_format,
    default_disc_name,
    slugify_disc_name,
    coerce_duration_seconds,
    is_dev_mode,
    get_disc_size_bytes_for_mount_point,
)
from parsing.disc_parser import hydrate_disc_payload
from core.settings import get_discdb_miss_workflow_with_prefill
from core.title_type_normalize import normalize_title_type_for_storage as _canonical_disc_title_type
from core.tmdb_scraper import fetch_tmdb_metadata_for_id, normalize_tmdb_id_str
from core.release_link_validation import (
    normalize_gtin_from_discdb,
    release_link_ready as release_link_ready_for_disc,
    release_missing_required_field_keys,
)
import os, uuid, time
import re
import datetime

logger = logging.getLogger("api.crud")

SKIP_AUTOSCAN = os.getenv("MKVAUTO_DISABLE_AUTOSCAN", "").lower() in ("1", "true", "yes")


def _sanitize_unicode_for_db(obj):
    """
    Recursively sanitize non-ASCII characters in string values for DB storage.

    Some Postgres connections (psycopg2 with ascii client_encoding) cannot INSERT
    non-ASCII characters like ``→`` (U+2192) that appear in MakeMKV scan output
    or DiscDB metadata.  Replace known problematic characters with ASCII equivalents.
    """
    if isinstance(obj, str):
        # Replace common Unicode characters with ASCII equivalents
        replacements = {
            "\u2192": "->",   # → right arrow
            "\u2190": "<-",   # ← left arrow
            "\u2194": "<->",  # ↔ left-right arrow
            "\u2013": "-",    # – en dash
            "\u2014": "--",   # — em dash
            "\u2018": "'",    # ' left single quote
            "\u2019": "'",    # ' right single quote
            "\u201c": '"',    # " left double quote
            "\u201d": '"',    # " right double quote
            "\u2026": "...",  # … ellipsis
            "\u00a0": " ",    # non-breaking space
        }
        for char, replacement in replacements.items():
            obj = obj.replace(char, replacement)
        # For any remaining non-ASCII, try to keep it (Postgres UTF-8 should handle it)
        # but if the connection is ASCII, this will be caught at commit time
        return obj
    elif isinstance(obj, dict):
        return {k: _sanitize_unicode_for_db(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_sanitize_unicode_for_db(item) for item in obj]
    return obj

# Valid GTIN lengths: GTIN-8 (EAN-8), GTIN-12 (UPC-A), GTIN-13 (EAN-13), GTIN-14 (EAN-14)
VALID_GTIN_LENGTHS = (8, 12, 13, 14)


def _valid_gtin(upc: str | None) -> bool:
    """Return True if upc is a valid GTIN (8, 12, 13, or 14 digits), excluding all-zero sentinels."""
    if not upc:
        return False
    s = str(upc).strip()
    if not s.isdigit() or len(s) not in VALID_GTIN_LENGTHS:
        return False
    try:
        if int(s) == 0:
            return False
    except ValueError:
        return False
    return True


def _normalize_optional_int(value):
    """
    Coerce values for nullable Integer columns (e.g. DiscTitle.season / episode).
    Empty strings from scan JSON must become None — PostgreSQL rejects '' for integer.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s == "":
            return None
        try:
            return int(s)
        except (ValueError, TypeError):
            return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


FORMAT_ORDER = {"UHD": 3, "4K": 3, "BLU-RAY": 2, "BLURAY": 2, "BD": 2, "DVD": 1}


def _normalize_format(fmt: str | None) -> str | None:
    return normalize_disc_format(fmt)


def set_title_type(
    title,  # models.DiscTitle (untyped to avoid forward-ref import order issues)
    value: str | None,
    source: str,
) -> None:
    """Single chokepoint for writing `disc_titles.type` (and its source split).

    `disc_titles` carries three columns for type provenance:
      - `type`      — denormalized "effective" cache (legacy reads)
      - `auto_type` — set by automated detection (DiscDB import, Path A
                      sibling-ignore, m2ts subsumption, scan-time
                      defaults, MakeMKV decoy flag)
      - `user_type` — set by direct user input (PATCH edits, exploratory-
                      rip canonical selection, "previous order had decoys"
                      action)

    Resolution rule: `type = user_type if user_type is not None else auto_type`.

    Args:
      title:  a models.DiscTitle row (must already be in the session).
      value:  the type value to set (`'MainMovie'`, `'Extra'`, `'ignore'`,
              `None` to clear, etc.). Strings are stored verbatim — case
              normalization is the caller's job.
      source: either `'user'` or `'auto'`. Setting `'user'` clears nothing
              in `auto_type`, so the chip system can show "User selected
              X · DiscDB had Y" when they differ.

    Why a helper instead of property setters: SQLAlchemy property setters
    bypass the dirty-tracking sweep cleanly enough, but lots of write
    sites in this codebase pass dicts of fields through generic
    `_apply_title_patch_fields`-style spread helpers — those don't fire
    setters, so the cache silently drifts. A function call at every
    write site is harder to misuse.
    """
    if source not in ("user", "auto"):
        raise ValueError(f"set_title_type: source must be 'user' or 'auto', got {source!r}")
    if source == "user":
        title.user_type = value
    else:
        title.auto_type = value
    # Recompute the denormalized cache. `user_type` always wins when set;
    # falls through to `auto_type` otherwise.
    title.type = title.user_type if title.user_type is not None else title.auto_type


# Label fields carrying the user_/auto_ provenance split (plus `type`,
# which has its own helper above and predates the generalization).
PROVENANCED_TITLE_FIELDS = (
    "title", "edition", "description", "season", "episode",
    # Multi-part layout (#796) — same provenance rules, so TMDB two-parter
    # detection writes auto_* and a hand-correction always wins.
    "part", "part_of", "episode_end",
)


def set_title_field(
    title,  # models.DiscTitle (untyped to avoid forward-ref import order issues)
    field: str,
    value,
    source: str,
) -> None:
    """Single chokepoint for writing any user-editable disc_titles label
    field, generalizing `set_title_type` to title/edition/description/
    season/episode (title-state redesign, area 1).

    Each field is stored three ways: `user_<field>` (a human said this),
    `auto_<field>` (automation concluded this — DiscDB import, scan,
    detector, group propagation), and the legacy resolved column that
    every reader consumes. Resolution: `resolved = user ?? auto`.

    Because automated writers touch only `auto_<field>`, an automated
    pass can NEVER overwrite a human's value — the collision class that
    produced the labeling data-loss bugs (#775/#778) is gone by
    construction, not by client-side defense.

    A user write of ``None`` RETRACTS the user value: the resolved
    column falls back to the automatic one. This matches `user_type`
    semantics (un-ignore clears user_type and the auto opinion shows
    through) — "the user removed their correction" restores automation's
    answer rather than pinning an empty value forever.

    `field='type'` delegates to `set_title_type` so callers can route
    every label field through one function.
    """
    if field == "type":
        set_title_type(title, value, source)
        return
    if field not in PROVENANCED_TITLE_FIELDS:
        raise ValueError(
            f"set_title_field: field must be one of {PROVENANCED_TITLE_FIELDS + ('type',)}, got {field!r}"
        )
    if source not in ("user", "auto"):
        raise ValueError(f"set_title_field: source must be 'user' or 'auto', got {source!r}")
    setattr(title, f"{source}_{field}", value)
    user_val = getattr(title, f"user_{field}", None)
    setattr(title, field, user_val if user_val is not None else getattr(title, f"auto_{field}", None))


def title_provenance_payload(title) -> dict:
    """The user_/auto_ source-split for every provenanced label field, as
    serializer-ready keys. One definition so the five title serializers
    (workflow-context GET, disc titles list, patch echo, …) can't drift."""
    payload: dict = {
        "auto_type": getattr(title, "auto_type", None),
        "user_type": getattr(title, "user_type", None),
    }
    for f in PROVENANCED_TITLE_FIELDS:
        payload[f"auto_{f}"] = getattr(title, f"auto_{f}", None)
        payload[f"user_{f}"] = getattr(title, f"user_{f}", None)
    return payload


def _format_rank(fmt: str | None) -> int:
    if not fmt:
        return 0
    return FORMAT_ORDER.get(fmt.upper(), 0)


def _disc_name_sluggify(name: str | None) -> str:
    """Backward-compatible alias for core.utils.slugify_disc_name."""
    return slugify_disc_name(name)


def apply_disc_slug_from_label_payload(disc: models.Disc, payload_disc_slug: Any) -> None:
    """
    After disc.disc_name reflects the label payload: set disc_slug from an explicit
    non-blank payload value, or slugify disc.disc_name when the slug was omitted or blank.
    """
    if payload_disc_slug is not None and str(payload_disc_slug).strip() != "":
        disc.disc_slug = str(payload_disc_slug).strip()
        return
    name = (disc.disc_name or "").strip()
    if not name:
        return
    generated = _disc_name_sluggify(name)
    if generated:
        disc.disc_slug = generated


def backfill_disc_slug_if_blank(disc: models.Disc) -> None:
    """If disc_slug is missing or whitespace-only, set it from slugified disc_name."""
    cur = disc.disc_slug
    if cur is not None and str(cur).strip() != "":
        return
    name = (disc.disc_name or "").strip()
    if not name:
        return
    generated = _disc_name_sluggify(name)
    if generated:
        disc.disc_slug = generated


def sync_disc_label_draft_with_release(disc: models.Disc, release: models.Release | None) -> None:
    """
    Sync disc.label_draft with release assignment to prevent stale label_draft.
    
    When a disc is assigned to (or unlinked from) a release, update label_draft to reflect
    the current database state. This prevents the issue where label_draft contains outdated
    release_id/movie_id that conflicts with the actual disc.release_id in the database.
    
    Args:
        disc: The disc to update
        release: The release to sync with (or None to clear release fields from label_draft)
    """
    if not isinstance(disc.label_draft, dict):
        disc.label_draft = {}
    
    if release:
        # Sync label_draft with release assignment
        disc.label_draft["movie_id"] = str(release.movie_id) if release.movie_id else None
        disc.label_draft["release_id"] = str(release.id)
        disc.label_draft["boxset_id"] = str(release.boxset_id) if release.boxset_id else None
        logger.info(
            f"Synced label_draft for disc {disc.id}: "
            f"movie_id={disc.label_draft['movie_id']}, "
            f"release_id={disc.label_draft['release_id']}, "
            f"boxset_id={disc.label_draft.get('boxset_id')}"
        )
    else:
        # Clear only release/boxset fields when disc is unlinked. Preserve movie_id and group_type
        # so the user's film-step selection is not lost (they can continue to boxset step and
        # create or assign a release there).
        disc.label_draft["release_id"] = None
        disc.label_draft["boxset_id"] = None
        logger.info(
            f"Cleared release/boxset from label_draft for disc {disc.id} "
            f"(movie_id preserved: {disc.label_draft.get('movie_id')})"
        )
    
    # Mark the field as changed so SQLAlchemy knows to update it
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(disc, "label_draft")


def _format_slug(fmt: str | None) -> str | None:
    fmt = _normalize_format(fmt)
    if not fmt:
        return None
    if fmt == "UHD":
        return "4k"
    if fmt == "Blu-Ray":
        return "blu-ray"
    if fmt == "DVD":
        return "dvd"
    return slugify(fmt)


def _best_format(existing: str | None, candidate: str | None) -> str | None:
    if _format_rank(candidate) > _format_rank(existing):
        return _normalize_format(candidate)
    return existing


def _title_case(name: str | None) -> str | None:
    if not name:
        return name
    return " ".join(w.capitalize() if w else "" for w in str(name).split())


def _ensure_movie_from_discdb(db: Session, payload: dict) -> str | None:
    """
    Ensure a movie exists from DiscDB data. Only creates movies if TMDB ID is present.
    Looks up existing movie by TMDB ID if available.
    Returns movie_id or None if movie data is insufficient or no TMDB ID.

    For new rows, prefers TMDB page scrape (same as /movies/lookup) for name, year, type, and poster;
    falls back to DiscDB payload fields when scrape fails. Release.cover_front_url may still use
    DiscDB art; movie.cover_url is then backfilled from release only if still unset.
    """
    tmdb_id = normalize_tmdb_id_str(payload.get("tmdb_id"))
    if not tmdb_id:
        return None

    existing = db.query(models.Movie).filter(models.Movie.tmdb_id == tmdb_id).first()
    if existing:
        return existing.id

    movie_name = payload.get("movie_name") or payload.get("show_title")  # Backward compat
    production_year = payload.get("production_year")
    tmdb_type = payload.get("tmdb_type")
    media_hint = payload.get("media_type") or payload.get("title_type")

    scraped = fetch_tmdb_metadata_for_id(
        tmdb_id,
        tmdb_type,
        media_type=media_hint,
        group_type=payload.get("group_type"),
        title_type=payload.get("title_type"),
    )
    cover_url = None
    if scraped:
        movie_name = scraped["name"]
        if scraped.get("production_year") is not None:
            production_year = scraped["production_year"]
        tmdb_type = scraped["tmdb_type"]
        cover_url = scraped.get("cover_url")
    elif not movie_name:
        return None

    new_movie = models.Movie(
        name=movie_name,
        production_year=production_year,
        tmdb_id=tmdb_id,
        tmdb_type=tmdb_type,
        cover_url=cover_url,
    )
    db.add(new_movie)
    db.commit()
    db.refresh(new_movie)
    return new_movie.id


def _coerce_optional_release_year_from_discdb_payload(payload: dict) -> int | None:
    """Parse release year from DiscDB payload; None if unknown (no current-year default)."""
    release_year_raw = payload.get("release_year")
    try:
        y = int(release_year_raw) if release_year_raw is not None else None
        if y is not None and 1000 <= y <= 9999:
            return y
    except (TypeError, ValueError):
        pass
    py = payload.get("production_year")
    if isinstance(py, int) and 1000 <= py <= 9999:
        return py
    return None


def _merge_discdb_into_boxset_candidate(bs: models.Boxset, payload: dict) -> None:
    """Apply DiscDB boxset scan fields onto an existing Boxset row (partial data allowed)."""
    n = payload.get("name") or payload.get("title")
    if n and str(n).strip():
        bs.name = str(n).strip()
    t = payload.get("title")
    if t and str(t).strip():
        bs.title = str(t).strip()
    st = payload.get("sort_title")
    if st and str(st).strip():
        bs.sort_title = str(st).strip()
    if "upc" in payload:
        bs.upc = normalize_gtin_from_discdb(payload.get("upc"))
    if payload.get("asin"):
        bs.asin = payload.get("asin")
    y = payload.get("year")
    if isinstance(y, int) and 1000 <= y <= 9999:
        bs.year = y
    if payload.get("locale"):
        bs.locale = payload.get("locale")
    if payload.get("region_code"):
        bs.region_code = str(payload["region_code"]).strip() or bs.region_code
    cf = payload.get("cover_front_url")
    if isinstance(cf, str) and (cf.startswith("http://") or cf.startswith("https://")):
        bs.cover_front_url = cf
    if payload.get("cover_back_url"):
        bs.cover_back_url = payload.get("cover_back_url")


def upsert_discdb_boxset_candidate(db: Session, payload: dict) -> models.Boxset | None:
    """
    Create or update a Boxset from TheDiscDB Release.boxset data (partial metadata allowed).
    Uses a stable slug (discdb-boxset-{id} when DiscDB id is present) for idempotent upserts.
    """
    slug = (payload.get("slug") or "").strip()
    if not slug:
        return None
    bs = (
        db.query(models.Boxset)
        .filter(models.Boxset.slug == slug)
        .with_for_update()
        .first()
    )
    if bs:
        _merge_discdb_into_boxset_candidate(bs, payload)
        db.commit()
        db.refresh(bs)
        return bs

    n = payload.get("name") or payload.get("title")
    name = str(n).strip() if n and str(n).strip() else None
    upc = normalize_gtin_from_discdb(payload.get("upc"))
    cf = payload.get("cover_front_url")
    cover = cf if isinstance(cf, str) and (cf.startswith("http://") or cf.startswith("https://")) else None
    yr = payload.get("year")
    year = yr if isinstance(yr, int) and 1000 <= yr <= 9999 else None
    bs = models.Boxset(
        slug=slug,
        name=name,
        title=(payload.get("title") or name),
        sort_title=payload.get("sort_title"),
        upc=upc,
        asin=payload.get("asin"),
        year=year,
        locale=payload.get("locale"),
        region_code=(str(payload["region_code"]).strip() if payload.get("region_code") is not None else None),
        cover_front_url=cover,
        cover_back_url=payload.get("cover_back_url"),
        modified=False,
    )
    db.add(bs)
    try:
        db.commit()
        db.refresh(bs)
    except IntegrityError:
        db.rollback()
        logger.warning(
            "Race creating DiscDB boxset candidate for slug=%s; retrying lookup",
            slug,
        )
        bs = (
            db.query(models.Boxset)
            .filter(models.Boxset.slug == slug)
            .with_for_update()
            .first()
        )
        if bs:
            _merge_discdb_into_boxset_candidate(bs, payload)
            db.commit()
            db.refresh(bs)
        else:
            return None
    return bs


def _merge_discdb_into_release_candidate(db: Session, rel: models.Release, payload: dict) -> None:
    """Apply DiscDB scan fields onto an existing Release row (partial data allowed)."""
    rel.type = (payload.get("group_type") or payload.get("title_type") or rel.type or "movie").lower()
    rn = payload.get("movie_name") or payload.get("release_name")
    if rn and str(rn).strip():
        rel.name = str(rn).strip()
    ry = _coerce_optional_release_year_from_discdb_payload(payload)
    if ry is not None:
        rel.release_year = ry
    if "upc" in payload:
        rel.upc = normalize_gtin_from_discdb(payload.get("upc"))
    else:
        rel.upc = normalize_gtin_from_discdb(rel.upc)
    if payload.get("asin"):
        rel.asin = payload.get("asin")
    rim = payload.get("release_image")
    if isinstance(rim, str) and (rim.startswith("http://") or rim.startswith("https://")):
        rel.cover_front_url = rim
    if payload.get("cover_back_url"):
        rel.cover_back_url = payload.get("cover_back_url")
    rr = payload.get("release_resolution") or payload.get("resolution")
    if rr:
        rel.resolution = rr


def upsert_discdb_release_candidate(
    db: Session, payload: dict, movie_id: str, boxset_id: str | None = None
) -> models.Release | None:
    """
    Create or update a Release from DiscDB without requiring full standalone GTIN/cover rules.
    When boxset_id is set, targets the (movie_id, boxset_id) release row; otherwise standalone (boxset_id NULL).
    Disc linking is deferred until release_link_ready_for_disc is true (see finalize_discdb_scan_record).
    """
    disc_hash = payload.get("disc_hash")
    if disc_hash:
        existing_disc = db.query(models.Disc).filter(models.Disc.content_hash == disc_hash).first()
        if existing_disc and existing_disc.release_id:
            rel = db.query(models.Release).filter(models.Release.id == existing_disc.release_id).first()
            if rel:
                _merge_discdb_into_release_candidate(db, rel, payload)
                db.commit()
                db.refresh(rel)
                _backfill_movie_cover_from_release(db, rel)
                return rel

    q = (
        db.query(models.Release)
        .filter(models.Release.movie_id == movie_id)
        .with_for_update()
    )
    if boxset_id:
        q = q.filter(models.Release.boxset_id == boxset_id)
    else:
        q = q.filter(models.Release.boxset_id.is_(None))
    rel = q.first()
    if rel:
        _merge_discdb_into_release_candidate(db, rel, payload)
        db.commit()
        db.refresh(rel)
        _backfill_movie_cover_from_release(db, rel)
        return rel

    rn = payload.get("movie_name") or payload.get("release_name")
    name = str(rn).strip() if rn and str(rn).strip() else None
    ry = _coerce_optional_release_year_from_discdb_payload(payload)
    upc = normalize_gtin_from_discdb(payload.get("upc"))
    rim = payload.get("release_image")
    cover = rim if isinstance(rim, str) and (rim.startswith("http://") or rim.startswith("https://")) else None
    rel = models.Release(
        slug="pending",
        type=(payload.get("group_type") or payload.get("title_type") or "movie").lower(),
        name=name,
        movie_id=movie_id,
        upc=upc,
        asin=payload.get("asin"),
        cover_front_url=cover,
        cover_back_url=payload.get("cover_back_url"),
        release_year=ry,
        resolution=payload.get("release_resolution") or payload.get("resolution"),
        boxset_id=boxset_id,
        modified=False,
    )
    db.add(rel)
    try:
        db.commit()
        db.refresh(rel)
    except IntegrityError:
        db.rollback()
        logger.warning(
            "Race creating DiscDB release candidate for movie_id=%s boxset_id=%s; retrying lookup",
            movie_id,
            boxset_id,
        )
        q2 = (
            db.query(models.Release)
            .filter(models.Release.movie_id == movie_id)
            .with_for_update()
        )
        if boxset_id:
            q2 = q2.filter(models.Release.boxset_id == boxset_id)
        else:
            q2 = q2.filter(models.Release.boxset_id.is_(None))
        rel = q2.first()
        if rel:
            _merge_discdb_into_release_candidate(db, rel, payload)
            db.commit()
            db.refresh(rel)
        else:
            return None
    _backfill_movie_cover_from_release(db, rel)
    return rel


def ensure_release_from_discdb(db: Session, payload: dict) -> models.Release | None:
    """
    For DiscDB hits: ensure Movie exists, then upsert a Release candidate (partial metadata allowed).
    When payload includes discdb_boxset (from TheDiscDB), upserts Boxset first and sets release.boxset_id.
    Callers must use release_link_ready_for_disc + get_or_create_disc to decide disc.release_id.
    """
    if not payload.get("discdb_hit") or not payload.get("tmdb_id"):
        return None
    movie_id = _ensure_movie_from_discdb(db, payload)
    if not movie_id:
        return None
    boxset_id: str | None = None
    bsp = payload.get("discdb_boxset")
    if isinstance(bsp, dict) and bsp:
        box = upsert_discdb_boxset_candidate(db, bsp)
        if box:
            boxset_id = box.id
    return upsert_discdb_release_candidate(db, payload, movie_id, boxset_id=boxset_id)


def release_link_ready(db: Session, release: models.Release | None) -> bool:
    """True when release (and parent boxset, if any) satisfies the same rules as get_or_create_release."""
    return release_link_ready_for_disc(db, release)


def sync_disc_pending_release_metadata(
    db: Session, disc: models.Disc, release: models.Release, link_ready: bool
) -> None:
    """Store pending_release_id / missing fields on disc.disc_info when disc is not linkable yet."""
    from sqlalchemy.orm.attributes import flag_modified

    base = dict(disc.disc_info) if isinstance(disc.disc_info, dict) else {}
    if link_ready:
        base.pop("pending_release_id", None)
        base.pop("release_missing_required_fields", None)
        base.pop("release_link_ready", None)
    else:
        base["pending_release_id"] = str(release.id)
        base["release_missing_required_fields"] = release_missing_required_field_keys(db, release)
        base["release_link_ready"] = False
    disc.disc_info = base
    flag_modified(disc, "disc_info")


def sync_label_draft_movie_for_pending_release(db: Session, disc: models.Disc, release: models.Release) -> None:
    """When disc has no release_id yet, still expose movie_id for film/boxset workflow."""
    if disc.release_id:
        return
    from sqlalchemy.orm.attributes import flag_modified

    if not isinstance(disc.label_draft, dict):
        disc.label_draft = {}
    disc.label_draft["movie_id"] = str(release.movie_id)
    if release.type:
        disc.label_draft["group_type"] = release.type
    if getattr(release, "boxset_id", None):
        disc.label_draft["boxset_id"] = str(release.boxset_id)
    flag_modified(disc, "label_draft")


def finalize_discdb_scan_record(db: Session, disc: models.Disc, release: models.Release | None) -> None:
    """After get_or_create_disc: merge disc_info pending keys and label_draft movie hint."""
    if not release:
        return
    lr = release_link_ready_for_disc(db, release)
    if not lr and disc.release_id == release.id:
        disc.release_id = None
        sync_disc_label_draft_with_release(disc, None)
    sync_disc_pending_release_metadata(db, disc, release, lr)
    if not disc.release_id:
        sync_label_draft_movie_for_pending_release(db, disc, release)
    db.commit()
    db.refresh(disc)


def persist_disc_scan_with_discdb(db: Session, content_hash: str, disc_info: dict) -> models.Disc:
    """
    Create/update Disc row from scan payload; link disc->release only when metadata is link-ready.
    """
    # Sanitize non-ASCII that could crash psycopg2 with ascii client_encoding
    disc_info = _sanitize_unicode_for_db(disc_info)
    existing_disc = db.query(models.Disc).filter(models.Disc.content_hash == content_hash).first()
    release = None
    if existing_disc and existing_disc.release_id:
        release = db.query(models.Release).filter(models.Release.id == existing_disc.release_id).first()
    if release is None and disc_info.get("discdb_hit") and disc_info.get("tmdb_id"):
        release = ensure_release_from_discdb(db, disc_info)
    lr = release_link_ready_for_disc(db, release) if release else False
    disc_record = get_or_create_disc(db, content_hash, release if lr else None, disc_info)
    if release:
        finalize_discdb_scan_record(db, disc_record, release)
    db.refresh(disc_record)
    if disc_record.release_id:
        rel_norm = db.query(models.Release).filter(models.Release.id == disc_record.release_id).first()
        if rel_norm:
            normalize_disc_numbers_for_release(db, rel_norm)
            db.commit()
            db.refresh(disc_record)
    # TMDB label_draft seed (#388). Only seeds when there is no existing
    # draft AND no DiscDB-driven release was linked above — never overwrites
    # user edits or richer DiscDB metadata. The seed itself is transient
    # (lives only on the in-memory disc_info dict); it does NOT persist on
    # disc.disc_info because _extract_disc_scan_info filters it out.
    _seed_label_draft_from_tmdb(db, disc_record, disc_info)
    return disc_record


def _seed_label_draft_from_tmdb(db: Session, disc: models.Disc, disc_info: dict) -> None:
    """Copy ``disc_info['label_draft_seed']`` into ``disc.label_draft`` when safe.

    Rules:
      * Only fires when ``disc.label_draft`` is currently None/empty AND
        ``disc.release_id`` is None — i.e. the disc has no other identity yet.
      * Never overwrites a user-edited draft, by design (a re-scan of an
        already-labeled disc must not regress the user's choice).
      * The seed JSON shape mirrors ``label_draft_seed`` set by
        ``disc_manager.on_disc_scan_complete``: ``{tmdb_id, tmdb_type,
        title, year, cover_url, group_type, source: "tmdb_auto"}``.
    """
    seed = disc_info.get("label_draft_seed") if isinstance(disc_info, dict) else None
    if not isinstance(seed, dict) or not seed:
        return
    if disc.release_id is not None:
        return
    existing = disc.label_draft if isinstance(disc.label_draft, dict) else {}
    if existing:
        return
    disc.label_draft = {
        "tmdb_id": seed.get("tmdb_id"),
        "tmdb_type": seed.get("tmdb_type"),
        "title": seed.get("title"),
        "year": seed.get("year"),
        "cover_url": seed.get("cover_url"),
        "group_type": seed.get("group_type"),
        "source": "tmdb_auto",
    }
    db.commit()
    db.refresh(disc)


def merge_pending_release_into_disc_info_dict(disc: models.Disc, target: dict) -> None:
    """Copy pending release enrichment keys from disc.disc_info into an API/cache dict."""
    info = disc.disc_info if isinstance(disc.disc_info, dict) else {}
    if info.get("pending_release_id"):
        target["pending_release_id"] = info["pending_release_id"]
        target["release_missing_required_fields"] = info.get("release_missing_required_fields", [])
        target["release_link_ready"] = False
    elif disc.release_id:
        target["release_link_ready"] = True
        target.setdefault("release_missing_required_fields", [])


def _backfill_movie_cover_from_release(db: Session, release: models.Release) -> None:
    """When the linked movie has no cover_url, set it from release or boxset cover (e.g. from DiscDB)."""
    if not release.movie_id:
        return
    movie = release.movie
    if not movie or movie.cover_url:
        return
    cover = release.cover_front_url
    if not cover and release.boxset_id:
        boxset = db.query(models.Boxset).filter(models.Boxset.id == release.boxset_id).first()
        if boxset and boxset.cover_front_url:
            cover = boxset.cover_front_url
    if not cover or not (str(cover).strip().startswith("http://") or str(cover).strip().startswith("https://")):
        return
    movie.cover_url = cover.strip()
    db.commit()


def _merge_boxset_into_release_payload(payload: dict, boxset: models.Boxset) -> dict:
    """Copy boxset edition fields into payload when missing (mirrors update_boxset_metadata → releases)."""
    out = dict(payload)
    disp_name = (boxset.name or boxset.title or "").strip()
    rn = out.get("release_name")
    if rn is None or (isinstance(rn, str) and not str(rn).strip()):
        if disp_name:
            out["release_name"] = disp_name
    if out.get("release_year") is None and boxset.year is not None:
        out["release_year"] = boxset.year
    if not out.get("upc") and boxset.upc:
        out["upc"] = boxset.upc
    if not out.get("asin") and boxset.asin:
        out["asin"] = boxset.asin
    cf = out.get("cover_front_url") or out.get("movie_cover_url")
    if not cf and boxset.cover_front_url:
        out["cover_front_url"] = boxset.cover_front_url
    if not out.get("cover_back_url") and boxset.cover_back_url:
        out["cover_back_url"] = boxset.cover_back_url
    return out


def get_or_create_release(db: Session, payload: dict, disc_hash: str | None = None) -> models.Release | None:
    """Create or fetch a release based on movie_id.
    
    If disc_hash is provided and a disc with that hash already exists and has a release_id,
    return that existing release instead of creating a new one.
    """
    payload = dict(payload or {})
    boxset_id_early = payload.get("boxset_id")
    boxset: models.Boxset | None = None
    if boxset_id_early:
        boxset = db.query(models.Boxset).filter(models.Boxset.id == boxset_id_early).first()
        if boxset:
            payload = _merge_boxset_into_release_payload(payload, boxset)

    # First, check if disc already has a release (prevent duplicate releases)
    # BUT only reuse if movie_id matches - different movies in same boxset need separate releases
    if disc_hash:
        existing_disc = db.query(models.Disc).filter(models.Disc.content_hash == disc_hash).first()
        if existing_disc and existing_disc.release_id:
            # Disc already has a release - verify movie_id matches before reusing
            rel = db.query(models.Release).filter(models.Release.id == existing_disc.release_id).first()
            if rel:
                # Get movie_id from payload early to verify match
                payload_movie_id = payload.get("movie_id") or payload.get("film_id")
                
                # Only reuse release if movie_id matches (or payload has no movie_id yet)
                # This prevents different movies in same boxset from sharing releases
                if not payload_movie_id or rel.movie_id == payload_movie_id:
                    # Update existing release with any new metadata from payload
                    rel.type = payload.get("group_type") or rel.type or "movie"
                    if not rel.name:
                        rel.name = payload.get("release_name") or None
                    rel.upc = payload.get("upc") or rel.upc
                    rel.asin = payload.get("asin") or rel.asin
                    rel.cover_front_url = payload.get("cover_front_url") or payload.get("movie_cover_url") or rel.cover_front_url
                    rel.cover_back_url = payload.get("cover_back_url") or rel.cover_back_url
                    rel.release_year = payload.get("release_year") or getattr(rel, "release_year", None)
                    # Update boxset_id if provided
                    if "boxset_id" in payload:
                        rel.boxset_id = payload.get("boxset_id") if payload.get("boxset_id") else None
                    # Store resolution from DiscDB if available
                    release_res = payload.get("release_resolution") or payload.get("resolution")
                    if release_res:
                        rel.resolution = release_res
                    db.commit()
                    db.refresh(rel)
                    _backfill_movie_cover_from_release(db, rel)
                    return rel
                # If movie_id doesn't match, continue to create new release below
                logger.info(f"Disc {disc_hash} has release {rel.id} for movie {rel.movie_id}, but payload has movie {payload_movie_id} - creating new release")
    
    # Get movie_id early - required for release lookup
    movie_id = payload.get("movie_id") or payload.get("film_id")
    
    # If no movie_id but we have DiscDB movie data with TMDB ID, try to ensure movie exists
    # Only create movies when TMDB ID is present (from DiscDB external IDs or TMDB URL lookup)
    # Do not create movies from regular disc info_title
    if not movie_id and payload.get("tmdb_id"):
        movie_id = _ensure_movie_from_discdb(db, payload)
    
    if not movie_id:
        # Return None if we can't create a release without movie_id
        return None
    
    # Verify movie exists
    movie = db.query(models.Movie).filter(models.Movie.id == movie_id).first()
    if not movie:
        # Movie doesn't exist, can't create release
        return None
    
    # Look up existing release by movie_id (and optionally boxset_id)
    boxset_id = payload.get("boxset_id")
    
    # Validate required fields and formats before creating release
    if boxset_id:
        # If boxset_id is provided, validate boxset has required fields with correct formats
        if not boxset:
            logger.warning(f"Boxset {boxset_id} not found, cannot create release")
            return None
        
        # Validate boxset has required fields
        if not boxset.name or not boxset.name.strip():
            logger.warning(f"Boxset {boxset_id} missing required field: name")
            return None
        if not boxset.year or not (1000 <= boxset.year <= 9999):
            logger.warning(f"Boxset {boxset_id} missing or invalid year (must be 4-digit: 1000-9999)")
            return None
        if not _valid_gtin(boxset.upc):
            logger.warning(f"Boxset {boxset_id} missing or invalid UPC/GTIN (must be 8, 12, 13, or 14 digits)")
            return None
        if not boxset.cover_front_url or not (boxset.cover_front_url.startswith("http://") or boxset.cover_front_url.startswith("https://")):
            logger.warning(f"Boxset {boxset_id} missing or invalid cover_front_url (must be http:// or https:// URL)")
            return None
    else:
        # If boxset_id is NOT provided, require release_name, release_year (4-digit), upc (GTIN 8/12/13/14), cover_front_url (http/https)
        release_name = payload.get("release_name")
        release_year_raw = payload.get("release_year")
        # Coerce year from int or string (e.g. from form input)
        release_year = None
        if release_year_raw is not None:
            try:
                release_year = int(release_year_raw) if not isinstance(release_year_raw, int) else release_year_raw
            except (TypeError, ValueError):
                pass
        upc = payload.get("upc")
        cover_front_url = payload.get("cover_front_url")

        if not release_name or not (isinstance(release_name, str) and release_name.strip()):
            logger.warning("Release creation failed: release_name is required when not linked to boxset")
            return None
        if release_year is None or not (1000 <= release_year <= 9999):
            logger.warning("Release creation failed: release_year must be a 4-digit number (1000-9999) when not linked to boxset")
            return None
        if not _valid_gtin(upc):
            logger.warning("Release creation failed: UPC/GTIN must be 8, 12, 13, or 14 digits when not linked to boxset")
            return None
        cover_str = (str(cover_front_url).strip() if cover_front_url is not None else "")
        if not cover_str or not (cover_str.startswith("http://") or cover_str.startswith("https://")):
            logger.warning("Release creation failed: cover_front_url must be a valid http:// or https:// URL when not linked to boxset")
            return None
    
    rel = None
    
    if boxset_id:
        # If boxset_id is provided, look for release with matching movie_id AND boxset_id
        # Use with_for_update() to prevent race conditions
        rel = (
            db.query(models.Release)
            .filter(
                models.Release.movie_id == movie_id,
                models.Release.boxset_id == boxset_id
            )
            .with_for_update()
            .first()
        )
    else:
        # Standalone: multiple releases per movie are valid (different editions). Never use
        # .filter(movie_id, standalone).first() — order is undefined and can return a stale
        # row whose UPC/year we then only partially overwrite (payload.get("x") or rel.x).
        # Match by exact UPC for this movie so we update the intended edition or insert new.
        upc_key = str(payload.get("upc") or "").strip()
        if upc_key and _valid_gtin(upc_key):
            rel = (
                db.query(models.Release)
                .filter(
                    models.Release.movie_id == movie_id,
                    models.Release.boxset_id.is_(None),
                    models.Release.upc == upc_key,
                )
                .with_for_update()
                .first()
            )
        else:
            rel = None
    
    if rel:
        # Update existing release with new metadata
        rel.type = payload.get("group_type") or rel.type or "movie"
        if not rel.name:
            rel.name = payload.get("release_name") or None
        rel.upc = payload.get("upc") or rel.upc
        rel.asin = payload.get("asin") or rel.asin
        rel.cover_front_url = payload.get("cover_front_url") or payload.get("movie_cover_url") or rel.cover_front_url
        rel.cover_back_url = payload.get("cover_back_url") or rel.cover_back_url
        rel.release_year = payload.get("release_year") or getattr(rel, "release_year", None)
        # Update boxset_id if provided
        if "boxset_id" in payload:
            rel.boxset_id = payload.get("boxset_id") if payload.get("boxset_id") else None
        
        # Update slug from "pending" if release_name + release_year become available
        # Only update if not linked to boxset (boxset.slug takes precedence)
        if rel.slug == "pending" and not rel.boxset_id:
            release_name = rel.name or payload.get("release_name")
            release_year = rel.release_year or payload.get("release_year")
            if release_name and release_year:
                # Generate slug from release_name and release_year
                name_slug = slugify(release_name).replace("-", "_")
                new_slug = f"{name_slug}-{release_year}"
                # Try to add format if available
                best_fmt: str | None = None
                if rel.discs:
                    for d in rel.discs:
                        best_fmt = _best_format(best_fmt, getattr(d, "format", None))
                best_fmt = _best_format(best_fmt, payload.get("disc_format") or payload.get("format"))
                if not best_fmt:
                    best_fmt = payload.get("resolution")
                fmt_slug = _format_slug(best_fmt)
                if fmt_slug:
                    new_slug = f"{name_slug}-{release_year}-{fmt_slug}"
                rel.slug = new_slug
        
        # Store resolution from DiscDB if available
        release_res = payload.get("release_resolution") or payload.get("resolution")
        if release_res:
            rel.resolution = release_res
        db.commit()
        db.refresh(rel)
        _backfill_movie_cover_from_release(db, rel)
        return rel
    
    # Create new release
    # Check if release will be part of boxset
    boxset = None
    if boxset_id:
        boxset = db.query(models.Boxset).filter(models.Boxset.id == boxset_id).first()
    
    def compute_slug(existing: models.Release | None) -> str | None:
        # If release is part of boxset, don't generate slug - will use boxset.slug
        if boxset:
            return None
        
        # Require both release_name and release_year for slug generation
        release_name = payload.get("release_name")
        year = payload.get("release_year") or (existing.release_year if existing else None)
        
        if not release_name or not year:
            return None
        
        # Get format from discs or payload
        best_fmt: str | None = None
        if existing and existing.discs:
            for d in existing.discs:
                best_fmt = _best_format(best_fmt, getattr(d, "format", None))
        best_fmt = _best_format(best_fmt, payload.get("disc_format") or payload.get("format"))
        if not best_fmt:
            best_fmt = payload.get("resolution")
        fmt_slug = _format_slug(best_fmt)
        
        # Try: release_name-year-format
        if fmt_slug:
            name_slug = slugify(release_name).replace("-", "_")
            return f"{name_slug}-{year}-{fmt_slug}"
        
        # Fallback: release_name-year
        name_slug = slugify(release_name).replace("-", "_")
        return f"{name_slug}-{year}"
    
    desired_slug = compute_slug(None)
    # Never use UUID - use placeholder that will be replaced
    # Slug will be set when boxset is linked or when user provides release_name + release_year
    new_slug = desired_slug or "pending"
    
    # If boxset is provided, use boxset.slug instead of generated slug
    final_slug = new_slug
    if boxset and boxset.slug:
        final_slug = boxset.slug
    
    rel = models.Release(
        slug=final_slug,
        # #711 backstop: never persist a nameless boxset-member release (it
        # silently stalls the label workflow). Fall back to the boxset name.
        type=payload.get("group_type") or payload.get("title_type") or "movie",
        name=(payload.get("release_name") or (boxset.name or boxset.title if boxset else None) or None),
        movie_id=movie_id,
        upc=payload.get("upc"),
        asin=payload.get("asin"),
        cover_front_url=payload.get("cover_front_url") or payload.get("movie_cover_url"),
        cover_back_url=payload.get("cover_back_url"),
        release_year=payload.get("release_year"),
        resolution=payload.get("release_resolution") or payload.get("resolution"),
        boxset_id=boxset_id,  # Set boxset_id when creating new release
    )
    db.add(rel)
    
    try:
        db.commit()
        db.refresh(rel)
        _backfill_movie_cover_from_release(db, rel)
        return rel
    except IntegrityError as e:
        # Race condition detected: another transaction created the same release
        # Roll back and retry lookup to fetch the conflicting release
        db.rollback()
        logger.warning(
            f"Race condition detected creating release for movie_id={movie_id}, "
            f"boxset_id={boxset_id}. Retrying lookup to fetch existing release.",
            exc_info=e
        )
        
        # Retry lookup with locking to get the release created by the other
        # transaction. The unique-constraint shape determines what we can
        # safely match on:
        #
        # - With a boxset: there can be many releases per movie when scoped
        #   inside a boxset, but only one per (movie, boxset) pair. Match
        #   on both.
        # - Standalone (no boxset): the partial unique index
        #   `uq_releases_movie_standalone` (movie_id WHERE boxset_id IS NULL)
        #   guarantees AT MOST ONE row, so matching purely on
        #   (movie_id, boxset_id IS NULL) is both sufficient AND necessary.
        #   The previous version filtered by `upc == payload.upc` to avoid
        #   stomping on a divergent row, but that filter could miss the
        #   conflicting row entirely (e.g. existing release has empty UPC,
        #   payload has a real UPC) — leading to a 500 on the apply-release
        #   endpoint instead of the intended merge.
        if boxset_id:
            rel = (
                db.query(models.Release)
                .filter(
                    models.Release.movie_id == movie_id,
                    models.Release.boxset_id == boxset_id
                )
                .with_for_update()
                .first()
            )
        else:
            rel = (
                db.query(models.Release)
                .filter(
                    models.Release.movie_id == movie_id,
                    models.Release.boxset_id.is_(None),
                )
                .with_for_update()
                .first()
            )

        if rel:
            # Found the conflicting release, update it with payload metadata
            rel.type = payload.get("group_type") or rel.type or "movie"
            if not rel.name:
                rel.name = payload.get("release_name") or None
            rel.upc = payload.get("upc") or rel.upc
            rel.asin = payload.get("asin") or rel.asin
            rel.cover_front_url = payload.get("cover_front_url") or payload.get("movie_cover_url") or rel.cover_front_url
            rel.cover_back_url = payload.get("cover_back_url") or rel.cover_back_url
            rel.release_year = payload.get("release_year") or getattr(rel, "release_year", None)
            release_res = payload.get("release_resolution") or payload.get("resolution")
            if release_res:
                rel.resolution = release_res
            db.commit()
            db.refresh(rel)
            _backfill_movie_cover_from_release(db, rel)
            return rel
        else:
            # Shouldn't happen, but log error and raise
            logger.error(
                f"Failed to fetch release after IntegrityError for movie_id={movie_id}, "
                f"boxset_id={boxset_id}"
            )
            raise


def _next_disc_number(db: Session, rel: models.Release, exclude_disc_id: str | None = None) -> int | None:
    """
    Calculate the next disc number for a release.
    If the release is part of a boxset, counts discs across all releases in the boxset.
    Otherwise, counts discs only in the current release.

    Counts ALL discs in the release/boxset (including those with DiscDB-hit jobs) so that
    each physical disc gets a unique sequential number (1, 2, 3, ...). Excludes only the
    current disc when exclude_disc_id is provided.
    """
    try:
        if rel.boxset_id:
            releases_in_boxset = db.query(models.Release).filter(
                models.Release.boxset_id == rel.boxset_id
            ).all()
            release_ids = [r.id for r in releases_in_boxset]
            query = db.query(models.Disc).filter(models.Disc.release_id.in_(release_ids))
            if exclude_disc_id:
                query = query.filter(models.Disc.id != exclude_disc_id)
            count = query.count()
            return count + 1
        else:
            query = db.query(models.Disc).filter(models.Disc.release_id == rel.id)
            if exclude_disc_id:
                query = query.filter(models.Disc.id != exclude_disc_id)
            count = query.count()
            return count + 1
    except Exception:
        if rel.boxset_id:
            releases_in_boxset = db.query(models.Release).filter(
                models.Release.boxset_id == rel.boxset_id
            ).all()
            all_discs = []
            for r in releases_in_boxset:
                all_discs.extend(r.discs or [])
            if exclude_disc_id:
                all_discs = [d for d in all_discs if d.id != exclude_disc_id]
            return len(all_discs) + 1 if all_discs else 1
        else:
            discs = rel.discs or []
            if exclude_disc_id:
                discs = [d for d in discs if d.id != exclude_disc_id]
            return len(discs) + 1 if discs else 1


def _next_disc_numbers_all(db: Session, rel: models.Release, exclude_disc_id: str | None = None) -> int | None:
    """
    Calculate the next disc number for a release, counting ALL discs (including unfinished ones).
    This is used after normalization to assign a number to the current disc.
    
    If the release is part of a boxset, counts discs across all releases in the boxset.
    Otherwise, counts discs only in the current release.
    """
    try:
        if rel.boxset_id:
            # Count all discs across all releases in the boxset
            releases_in_boxset = db.query(models.Release).filter(
                models.Release.boxset_id == rel.boxset_id
            ).all()
            release_ids = [r.id for r in releases_in_boxset]
            query = db.query(models.Disc).filter(models.Disc.release_id.in_(release_ids))
            # Exclude the current disc from the count if provided
            if exclude_disc_id:
                query = query.filter(models.Disc.id != exclude_disc_id)
            count = query.count()
            return count + 1
        else:
            # Count discs only in this release
            query = db.query(models.Disc).filter(models.Disc.release_id == rel.id)
            # Exclude the current disc from the count if provided
            if exclude_disc_id:
                query = query.filter(models.Disc.id != exclude_disc_id)
            count = query.count()
            return count + 1
    except Exception:
        # Fallback: use relationship if available
        if rel.boxset_id:
            releases_in_boxset = db.query(models.Release).filter(
                models.Release.boxset_id == rel.boxset_id
            ).all()
            all_discs = []
            for r in releases_in_boxset:
                all_discs.extend(r.discs or [])
            if exclude_disc_id:
                all_discs = [d for d in all_discs if d.id != exclude_disc_id]
            return len(all_discs) + 1 if all_discs else 1
        else:
            discs = rel.discs or []
            if exclude_disc_id:
                discs = [d for d in discs if d.id != exclude_disc_id]
            return len(discs) + 1 if discs else None


def normalize_disc_numbers_for_release(db: Session, release: models.Release, exclude_disc_id: str | None = None) -> dict[str, int]:
    """
    Normalize disc numbers for all discs in a release (or boxset).
    Ensures all discs have unique, sequential disc numbers based on created_at.
    Includes ALL discs (including those with DiscDB-hit jobs) so each physical disc
    gets a unique number (1, 2, 3, ...).

    Args:
        db: Database session
        release: Release to normalize disc numbers for
        exclude_disc_id: Optional disc ID to exclude from normalization (will be assigned number after)

    Returns:
        Dict mapping disc_id -> disc_number for all normalized discs
    """
    if release.boxset_id:
        releases_in_boxset = db.query(models.Release).filter(
            models.Release.boxset_id == release.boxset_id
        ).all()
        release_ids = [r.id for r in releases_in_boxset]
        all_discs = (
            db.query(models.Disc)
            .filter(models.Disc.release_id.in_(release_ids))
            .order_by(models.Disc.created_at.asc(), models.Disc.id.asc())
            .all()
        )
    else:
        all_discs = (
            db.query(models.Disc)
            .filter(models.Disc.release_id == release.id)
            .order_by(models.Disc.created_at.asc(), models.Disc.id.asc())
            .all()
        )
    
    # Exclude the current disc if provided
    if exclude_disc_id:
        all_discs = [d for d in all_discs if d.id != exclude_disc_id]
    
    # Assign sequential disc numbers based on created_at order
    disc_number_map = {}
    for idx, disc in enumerate(all_discs, start=1):
        disc.disc_number = idx
        disc_number_map[disc.id] = idx
    
    # Don't commit here - let the caller handle the commit
    db.flush()
    return disc_number_map


def get_disc_id_by_hash(db: Session, content_hash: str) -> str | None:
    """
    Get disc_id from content_hash by querying the database.
    
    Args:
        db: Database session
        content_hash: Disc content hash
        
    Returns:
        disc_id as string if found, None otherwise
    """
    disc = db.query(models.Disc).filter(models.Disc.content_hash == content_hash).first()
    if disc:
        return str(disc.id)
    return None


def _discdb_boxset_payload_from_model(box: models.Boxset | None) -> dict | None:
    """Rebuild discdb_boxset dict for DB-backed DiscDB cache (persist_disc_scan / ensure_release_from_discdb)."""
    if not box:
        return None
    return {
        "slug": box.slug,
        "name": box.name,
        "title": box.title or box.name,
        "sort_title": box.sort_title,
        "upc": box.upc,
        "asin": box.asin,
        "year": box.year,
        "locale": box.locale,
        "region_code": box.region_code,
        "cover_front_url": box.cover_front_url,
        "cover_back_url": box.cover_back_url,
    }


def get_discdb_data_from_db(db: Session, content_hash: str) -> dict | None:
    """
    Check if a Disc with this content_hash already exists in the DB with DiscDB data.

    Used as a DB-backed cache for DiscDB lookups: if we've already queried TheDiscDB
    for this disc and got a hit (or the user manually labeled it), we can skip the
    API call entirely and return the cached data.

    Returns:
        A dict matching the shape of query_discdb() return value if the disc exists
        in the DB with a linked release (hit). Returns None if the disc is not found
        or has no release (known miss — re-query the API in case data was added).
    """
    from sqlalchemy.orm import joinedload

    disc = (
        db.query(models.Disc)
        .options(
            joinedload(models.Disc.release).joinedload(models.Release.movie),
            joinedload(models.Disc.release).joinedload(models.Release.boxset),
        )
        .filter(models.Disc.content_hash == content_hash)
        .first()
    )

    if not disc:
        # Never seen this disc before — caller should query the API
        return None

    if not disc.release_id or not disc.release:
        info = disc.disc_info if isinstance(disc.disc_info, dict) else {}
        pid = info.get("pending_release_id")
        if pid:
            release = (
                db.query(models.Release)
                .options(
                    joinedload(models.Release.movie),
                    joinedload(models.Release.boxset),
                )
                .filter(models.Release.id == str(pid))
                .first()
            )
            if release:
                movie = release.movie
                missing = release_missing_required_field_keys(db, release)
                result = {
                    "discdb_hit": True,
                    "label_required": True,
                    "label_ready": False,
                    "movie_name": movie.name if movie else None,
                    "release_image": release.cover_front_url,
                    "disc_slug": disc.disc_slug,
                    "resolution": release.resolution,
                    "disc_format": disc.format,
                    "title_type": release.type or "movie",
                    "disc_group": release.slug,
                    "group_type": release.type or "movie",
                    "release_year": release.release_year,
                    "tmdb_id": movie.tmdb_id if movie else None,
                    "tmdb_type": movie.tmdb_type if movie else None,
                    "production_year": movie.production_year if movie else None,
                    "pending_release_id": str(pid),
                    "release_missing_required_fields": missing,
                    "release_link_ready": False,
                }
                bsp = _discdb_boxset_payload_from_model(release.boxset)
                if bsp:
                    result["discdb_boxset"] = bsp
                if disc.disc_number is not None:
                    result["disc_number"] = disc.disc_number
                if disc.discdb_disc_num is not None:
                    result["discdb_disc_num"] = disc.discdb_disc_num
                return result
        # Disc exists but has no release — it was a miss last time.
        return None

    # Disc has a release — reconstruct DiscDB-like data from DB records
    release = disc.release
    movie = release.movie

    result = {
        "discdb_hit": True,
        "label_required": False,
        "label_ready": True,
        "movie_name": movie.name if movie else None,
        "release_image": release.cover_front_url,
        "disc_slug": disc.disc_slug,
        "resolution": release.resolution,
        "disc_format": disc.format,
        "title_type": release.type or "movie",
        "disc_group": release.slug,
        "group_type": release.type or "movie",
        "release_year": release.release_year,
        "tmdb_id": movie.tmdb_id if movie else None,
        "tmdb_type": movie.tmdb_type if movie else None,
        "production_year": movie.production_year if movie else None,
        "release_link_ready": True,
        "release_missing_required_fields": [],
    }

    if disc.disc_number is not None:
        result["disc_number"] = disc.disc_number
    if disc.discdb_disc_num is not None:
        result["discdb_disc_num"] = disc.discdb_disc_num

    bsp = _discdb_boxset_payload_from_model(release.boxset)
    if bsp:
        result["discdb_boxset"] = bsp

    return result


def get_or_create_disc(db: Session, content_hash: str, release: models.Release | None, payload: dict) -> models.Disc:
    """Create or update a disc record anchored on content_hash."""
    disc = db.query(models.Disc).filter(models.Disc.content_hash == content_hash).first()

    if disc:
        if release:
            # Only update release_id if disc doesn't already have one (prevent moving disc to different release)
            if disc.release_id is None:
                disc.release_id = release.id
                sync_disc_label_draft_with_release(disc, release)
                if disc.disc_number is None:
                    disc.disc_number = _next_disc_number(db, release, exclude_disc_id=disc.id)
            elif disc.release_id == release.id:
                # Disc already has this release - just update disc_number if needed
                # Sync label_draft to ensure it's up-to-date with the release
                sync_disc_label_draft_with_release(disc, release)
                if disc.disc_number is None:
                    disc.disc_number = _next_disc_number(db, release, exclude_disc_id=disc.id)
            # If disc.release_id exists and is different from release.id, keep the existing release_id
        # Always update format if provided in payload (even if disc already has a format)
        if payload.get("disc_format") or payload.get("format"):
            disc.format = payload.get("disc_format") or payload.get("format")
        
        payload_info = payload.get("info_title") or _title_case(payload.get("info_label"))
        effective_info_title = payload_info or disc.info_title
        disc_format_eff = disc.format or payload.get("disc_format") or payload.get("format")

        # Auto-populate disc_name from info title + format when not set (user can still edit it)
        disc_name = payload.get("disc_name")
        if not disc_name and not disc.disc_name:
            auto_name = default_disc_name(disc_format_eff, effective_info_title)
            if auto_name:
                disc_name = auto_name
                disc.disc_name = disc_name
        elif disc_name:
            # User provided disc_name, use it
            disc.disc_name = disc_name
        
        apply_disc_slug_from_label_payload(disc, payload.get("disc_slug"))
        # Always update info_title if provided in payload (even if disc already has one)
        if payload.get("info_title") or payload.get("info_label"):
            disc.info_title = payload.get("info_title") or _title_case(payload.get("info_label")) or disc.info_title
        if payload.get("disc_size_bytes"):
            disc.disc_size_bytes = payload.get("disc_size_bytes")
        # TheDiscDB index is reference-only; never persist payload disc_number from scan (legacy key ignored).
        if payload.get("discdb_disc_num") is not None:
            try:
                disc.discdb_disc_num = int(payload["discdb_disc_num"])
            except (TypeError, ValueError):
                pass
        # Add-only, matching upstream's rule for the field. This is what backfills
        # a library ripped before we computed it: re-inserting any disc fills the
        # gap, while a disc that already has an ID is never overwritten by a
        # later scan that failed to read one.
        if not disc.global_disc_id and payload.get("global_disc_id"):
            disc.global_disc_id = str(payload["global_disc_id"]).upper()
        db.commit()
        db.refresh(disc)
        return disc

    disc_number = _next_disc_number(db, release) if release else None
    discdb_disc_num = None
    if payload.get("discdb_disc_num") is not None:
        try:
            discdb_disc_num = int(payload["discdb_disc_num"])
        except (TypeError, ValueError):
            discdb_disc_num = None

    disc_format = payload.get("disc_format") or payload.get("format")
    info_title_val = payload.get("info_title") or _title_case(payload.get("info_label"))
    disc_name = payload.get("disc_name")
    if not disc_name:
        disc_name = default_disc_name(disc_format, info_title_val)
    
    raw_slug = payload.get("disc_slug")
    if raw_slug is not None and str(raw_slug).strip() != "":
        disc_slug: str | None = str(raw_slug).strip()
    else:
        sug = payload.get("suggested_disc_slug")
        if sug is not None and str(sug).strip() != "":
            disc_slug = str(sug).strip()
        else:
            n = (disc_name or "").strip() if disc_name else ""
            gen = _disc_name_sluggify(n) if n else ""
            disc_slug = gen if gen else None

    # label_draft is for user draft only; do not fill from scan/DiscDB payloads
    global_disc_id = payload.get("global_disc_id")
    disc = models.Disc(
        content_hash=content_hash,
        global_disc_id=str(global_disc_id).upper() if global_disc_id else None,
        release_id=release.id if release else None,
        disc_slug=disc_slug,
        disc_name=disc_name,
        format=disc_format,
        info_title=info_title_val,
        disc_number=disc_number,
        discdb_disc_num=discdb_disc_num,
        label_payload=payload.get("label_payload"),
        label_draft=None,
        disc_size_bytes=payload.get("disc_size_bytes"),
    )
    db.add(disc)
    db.commit()
    db.refresh(disc)
    
    # Sync label_draft with release assignment
    if release:
        sync_disc_label_draft_with_release(disc, release)
        db.commit()
        db.refresh(disc)
    
    return disc


def _extract_disc_scan_info(payload: dict) -> dict:
    """
    Extract disc scan info fields from a payload dict.
    
    Returns a dict containing only disc scan info fields that should be stored
    in disc.disc_info (not job.disc_payload).
    """
    disc_scan_fields = [
        'raw_info_log',
        'info_log',
        'makemkv_info_log',
        'titles_map',
        'scan_tracks',
        'titles',
        'cinfo_lines',
        'resolution',
        # TMDB auto-suggestion from disc_manager.query_tmdb (#388). Persisted
        # on disc.disc_info so the film-step UI (#389) can render it without
        # a re-query. label_draft_seed is consumed by persist_disc_scan_with_discdb
        # to seed disc.label_draft on first insert; it is NOT stored on disc_info.
        'tmdb_suggestion',
        # #753: TheDiscDB's own location for a matched disc, so an update
        # export can overwrite their entry instead of duplicating it.
        'discdb_upstream',
    ]
    
    disc_info = {}
    for field in disc_scan_fields:
        if field in payload:
            disc_info[field] = payload[field]
    
    return disc_info


def _store_disc_scan_info(db: Session, disc: models.Disc, disc_info: dict) -> None:
    """
    Store disc scan info in disc.disc_info, merging with existing data.
    
    Args:
        db: Database session
        disc: Disc model instance
        disc_info: Dict containing disc scan info fields to store
    """
    if not disc_info:
        return
    
    # Get current disc_info or initialize as empty dict
    current_disc_info = disc.disc_info or {}
    
    # Merge disc_info_data into current_disc_info (new data takes precedence)
    merged_disc_info = {**current_disc_info, **disc_info}
    
    # Store in disc.disc_info
    disc.disc_info = merged_disc_info
    db.commit()
    db.refresh(disc)


def _get_disc_scan_info(disc: models.Disc) -> dict:
    """
    Retrieve disc scan info from disc.disc_info.
    
    Args:
        disc: Disc model instance
        
    Returns:
        Dict containing disc scan info, or empty dict if not available
    """
    return disc.disc_info or {}


def _hydrate_payload(
    disc_num: str,
    mount_point: str,
    payload: dict,
    force_scan: bool = False,
    allow_scan: bool = True,
    disc_info: dict | None = None,
) -> dict:
    """
    Backward-compat shim to hydrate payloads via the centralized parser.
    
    Args:
        disc_num: Disc number
        mount_point: Mount point
        payload: Payload dict to hydrate
        force_scan: Force scan flag
        allow_scan: Allow scan flag
        disc_info: Optional disc_info dict to merge into payload (disc_info takes precedence)
    """
    # Merge disc_info into payload if provided (disc_info takes precedence)
    if disc_info:
        payload = {**payload, **disc_info}
    return hydrate_disc_payload(disc_num, mount_point, payload)


def _apply_discdb_metadata_to_titles(disc, db_mapping: dict):
    """
    Update existing disc_titles with DiscDB metadata where source_file matches.
    Used when scan_tracks were applied first (hit path): overlay type/season/episode/title/description
    from the DiscDB hit. Does not modify MakeMKV-derived fields such as comment.
    Never sets ``index`` from DiscDB; MakeMKV scan is the only source for title index.
    """
    if not db_mapping or not isinstance(db_mapping, dict):
        return

    prefill_miss = get_discdb_miss_workflow_with_prefill()

    for title in getattr(disc, "titles", None) or []:
        src = getattr(title, "source_file", None)
        if not src or src not in db_mapping:
            continue
        track_data = db_mapping.get(src)
        if not isinstance(track_data, dict):
            continue
        if hasattr(title, "type"):
            type_val = track_data.get("type")
            if type_val is None or (isinstance(type_val, str) and not type_val.strip()):
                type_val = "ignore"
            resolved = _canonical_disc_title_type(type_val)
            if not (prefill_miss and resolved == "ignore"):
                set_title_type(title, resolved, source="auto")
        # DiscDB overlay is automation: it writes the auto columns only, so
        # a user's hand-typed label on a re-looked-up disc survives the
        # overlay by construction (set_title_field resolves user ?? auto).
        set_title_field(title, "title", track_data.get("episode_name") or track_data.get("title"), source="auto")
        set_title_field(title, "season", _normalize_optional_int(track_data.get("season")), source="auto")
        set_title_field(title, "episode", _normalize_optional_int(track_data.get("episode")), source="auto")
        desc = track_data.get("description")
        if desc is not None:
            set_title_field(title, "description", desc, source="auto")

    # Any title that still has no type (not in db_mapping or blank in payload) should be ignore
    if not prefill_miss:
        for title in getattr(disc, "titles", None) or []:
            if hasattr(title, "type") and (title.type is None or (isinstance(title.type, str) and not title.type.strip())):
                set_title_type(title, "ignore", source="auto")

    _sync_duplicate_groups_for_disc_safe(disc)


def _apply_discdb_tracks(disc, db_mapping: dict):
    """
    Persist DiscDB track mapping into disc_titles table.
    db_mapping is a dict: {sourceFile: {type, season, episode, episode_name, title, ...}}
    """
    if not db_mapping or not isinstance(db_mapping, dict) or not hasattr(disc, "titles"):
        return
    # Avoid clobbering user edits if titles already exist (e.g. from scan_tracks).
    existing_titles = getattr(disc, "titles", None) or []
    if existing_titles:
        return
    try:
        from sqlalchemy import inspect
        sess = inspect(disc).session
        if sess:
            sess.query(models.DiscTitle).filter(models.DiscTitle.disc_id == disc.id).delete()
            sess.flush()
    except Exception:
        pass
    disc.titles = []

    prefill_miss = get_discdb_miss_workflow_with_prefill()

    for idx, (source_file, track_data) in enumerate(db_mapping.items()):
        if not isinstance(track_data, dict):
            continue
        type_val = track_data.get("type")
        if type_val is None or (isinstance(type_val, str) and not type_val.strip()):
            type_val = "ignore"
        type_val = _canonical_disc_title_type(type_val)
        if prefill_miss and type_val == "ignore":
            type_val = None
        imported_title = track_data.get("episode_name") or track_data.get("title")
        imported_season = _normalize_optional_int(track_data.get("season"))
        imported_episode = _normalize_optional_int(track_data.get("episode"))
        title_row = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            index=None,
            order_index=idx,
            source_file=str(source_file),
            # DiscDB import → auto provenance for every label field.
            # Mirror the helper's resolution rule at construction time
            # (no user_* yet, so each resolved cache = its auto column).
            title=imported_title,
            auto_title=imported_title,
            type=type_val,
            auto_type=type_val,
            season=imported_season,
            auto_season=imported_season,
            episode=imported_episode,
            auto_episode=imported_episode,
        )
        disc.titles.append(title_row)

    _sync_duplicate_groups_for_disc_safe(disc)


def _append_disc_title_from_scan_track(disc, t: dict, order_index: int) -> models.DiscTitle | None:
    """Create one DiscTitle (+ TitleStream rows) from a scan_tracks dict; append onto disc."""
    # Sanitize non-ASCII that could crash psycopg2 with ascii client_encoding
    t = _sanitize_unicode_for_db(t)
    source_key = t.get("source_file") or t.get("track_id")
    if not source_key:
        logger.warning(
            "_apply_scan_tracks: Missing source_file for track at order_index %s: %s",
            order_index,
            t.get("title", "unknown"),
        )
        return None
    source_key = str(source_key)
    type_val = t.get("type")
    if isinstance(type_val, str) and not type_val.strip():
        type_val = None
    type_val = _canonical_disc_title_type(type_val)
    streams = t.get("streams") or []
    title_row = models.DiscTitle(
        id=str(uuid.uuid4()),
        disc_id=disc.id,
        index=t.get("index"),
        order_index=order_index,
        comment=t.get("comment"),
        source_file=source_key,
        segment_map=t.get("segment_map"),
        duration=coerce_duration_seconds(t.get("duration")) if isinstance(t.get("duration"), str) else t.get("duration"),
        duration_raw=t.get("duration_raw") or t.get("duration"),
        size=t.get("size"),
        display_size=t.get("display_size"),
        title=None,
        # Scan-time MakeMKV type → auto provenance. user_type stays NULL
        # until the user edits via PATCH or the exploratory-rip flow.
        type=type_val,
        auto_type=type_val,
        season=_normalize_optional_int(t.get("season")),
        episode=_normalize_optional_int(t.get("episode")),
        chapters=t.get("chapters") or t.get("chapters_info"),
        streams=streams,
        language_code=t.get("language_code"),
        language=t.get("language"),
        obfuscation_flag=bool(t.get("obfuscation_flag", False)),
        # Seed the tier-aware reason from MakeMKV's per-title hint. Dedupe
        # computation later overrides this to 'segment_set_sibling' for any
        # row that turns out to be a non-representative member of a sorted
        # segment-set group, and Path A overrides it to 'path_a_decoy' /
        # NULL for skipped siblings / the matched canonical respectively.
        obfuscation_reason=(
            'makemkv_msg3307' if bool(t.get("obfuscation_flag", False)) else None
        ),
        playitem_durations_s=t.get("playitem_durations_s"),
    )
    disc.titles.append(title_row)
    if not streams:
        disc.title_streams.append(
            models.TitleStream(
                id=str(uuid.uuid4()),
                disc_id=disc.id,
                title_id=title_row.id,
                stream_index=0,
                stream_type=None,
                title=t.get("title") or t.get("comment"),
                note=t.get("comment"),
                duration=coerce_duration_seconds(t.get("duration")) if isinstance(t.get("duration"), str) else t.get("duration"),
                size=t.get("size"),
                streams=None,
                order_index=order_index,
            )
        )
        return title_row

    for s_idx, stream in enumerate(streams):
        if not isinstance(stream, dict):
            continue
        ch_val = stream.get("channels")
        if isinstance(ch_val, str):
            try:
                ch_val = int(ch_val)
            except Exception:
                ch_val = None
        disc.title_streams.append(
            models.TitleStream(
                id=str(uuid.uuid4()),
                disc_id=disc.id,
                title_id=title_row.id,
                stream_index=s_idx,
                stream_type=stream.get("type"),
                audio_type=stream.get("audio_type"),
                language_code=stream.get("language_code"),
                language=stream.get("language"),
                codec_short=stream.get("codec_short"),
                codec_hint=stream.get("codec_hint"),
                name=stream.get("name"),
                bitrate=stream.get("bitrate"),
                channels=ch_val if isinstance(ch_val, (int, type(None))) else None,
                sample_rate=stream.get("sample_rate"),
                bit_depth=stream.get("bit_depth"),
                resolution=stream.get("resolution"),
                aspect_ratio=stream.get("aspect_ratio"),
                frame_rate=stream.get("frame_rate"),
                reference_frames=stream.get("reference_frames"),
                description=stream.get("description"),
                info=stream.get("info"),
                duration_seconds=(
                    coerce_duration_seconds(stream.get("duration_seconds"))
                    if isinstance(stream.get("duration_seconds"), str)
                    else stream.get("duration_seconds")
                ),
                flag=stream.get("flag"),
                default=stream.get("default"),
                layout=stream.get("layout"),
                title=t.get("title") or t.get("comment"),
                note=stream.get("note") or t.get("comment"),
                duration=coerce_duration_seconds(t.get("duration")) if isinstance(t.get("duration"), str) else t.get("duration"),
                size=t.get("size"),
                streams=stream,
                order_index=s_idx,
            )
        )
    return title_row


def _remove_title_streams_for_title(disc, title_id: str) -> None:
    tid = str(title_id)
    try:
        from sqlalchemy import inspect

        sess = inspect(disc).session
        if sess:
            sess.query(models.TitleStream).filter(models.TitleStream.title_id == tid).delete(
                synchronize_session=False
            )
    except Exception:
        pass
    if getattr(disc, "title_streams", None):
        disc.title_streams[:] = [tr for tr in disc.title_streams if str(getattr(tr, "title_id", "")) != tid]


def _add_title_streams_for_title_from_scan(disc, title_row: models.DiscTitle, t: dict, type_val) -> None:
    """Append TitleStream rows for an existing title from scan dict (same rules as _append_disc_title_from_scan_track)."""
    streams = t.get("streams") or []
    oi = title_row.order_index if title_row.order_index is not None else 0
    if not streams:
        disc.title_streams.append(
            models.TitleStream(
                id=str(uuid.uuid4()),
                disc_id=disc.id,
                title_id=title_row.id,
                stream_index=0,
                stream_type=None,
                title=t.get("title") or t.get("comment"),
                note=t.get("comment"),
                duration=coerce_duration_seconds(t.get("duration"))
                if isinstance(t.get("duration"), str)
                else t.get("duration"),
                size=t.get("size"),
                streams=None,
                order_index=oi,
            )
        )
        return
    for s_idx, stream in enumerate(streams):
        if not isinstance(stream, dict):
            continue
        ch_val = stream.get("channels")
        if isinstance(ch_val, str):
            try:
                ch_val = int(ch_val)
            except Exception:
                ch_val = None
        disc.title_streams.append(
            models.TitleStream(
                id=str(uuid.uuid4()),
                disc_id=disc.id,
                title_id=title_row.id,
                stream_index=s_idx,
                stream_type=stream.get("type"),
                audio_type=stream.get("audio_type"),
                language_code=stream.get("language_code"),
                language=stream.get("language"),
                codec_short=stream.get("codec_short"),
                codec_hint=stream.get("codec_hint"),
                name=stream.get("name"),
                bitrate=stream.get("bitrate"),
                channels=ch_val if isinstance(ch_val, (int, type(None))) else None,
                sample_rate=stream.get("sample_rate"),
                bit_depth=stream.get("bit_depth"),
                resolution=stream.get("resolution"),
                aspect_ratio=stream.get("aspect_ratio"),
                frame_rate=stream.get("frame_rate"),
                reference_frames=stream.get("reference_frames"),
                description=stream.get("description"),
                info=stream.get("info"),
                duration_seconds=(
                    coerce_duration_seconds(stream.get("duration_seconds"))
                    if isinstance(stream.get("duration_seconds"), str)
                    else stream.get("duration_seconds")
                ),
                flag=stream.get("flag"),
                default=stream.get("default"),
                layout=stream.get("layout"),
                title=t.get("title") or t.get("comment"),
                note=stream.get("note") or t.get("comment"),
                duration=coerce_duration_seconds(t.get("duration"))
                if isinstance(t.get("duration"), str)
                else t.get("duration"),
                size=t.get("size"),
                streams=stream,
                order_index=s_idx,
            )
        )


def _reconcile_disc_title_from_scan_track(disc, title_row: models.DiscTitle, t: dict) -> None:
    """
    Update an existing DiscTitle + title_streams from latest scan for the same source_file.
    Always overwrites MakeMKV index and structural fields from the scan (rescan may renumber).
    """
    source_key = t.get("source_file") or t.get("track_id")
    if not source_key:
        return
    type_val = t.get("type")
    if isinstance(type_val, str) and not type_val.strip():
        type_val = None
    title_row.index = t.get("index")
    title_row.comment = t.get("comment")
    title_row.source_file = str(source_key)
    title_row.segment_map = t.get("segment_map")
    title_row.duration = (
        coerce_duration_seconds(t.get("duration")) if isinstance(t.get("duration"), str) else t.get("duration")
    )
    title_row.duration_raw = t.get("duration_raw") or t.get("duration")
    title_row.size = t.get("size")
    title_row.display_size = t.get("display_size")
    # Scan-derived season/episode are automation's opinion; a user's
    # values on a reconciled row survive through resolution.
    set_title_field(title_row, "season", _normalize_optional_int(t.get("season")), source="auto")
    set_title_field(title_row, "episode", _normalize_optional_int(t.get("episode")), source="auto")
    title_row.chapters = t.get("chapters") or t.get("chapters_info")
    title_row.streams = t.get("streams") or []
    title_row.language_code = t.get("language_code")
    title_row.language = t.get("language")
    if type_val is not None:
        # Scan-time MakeMKV type → auto provenance. Preserves any existing
        # user_type override (the helper resolves user_type ?? auto_type).
        set_title_type(title_row, type_val, source="auto")
    _remove_title_streams_for_title(disc, title_row.id)
    _add_title_streams_for_title_from_scan(disc, title_row, t, type_val)


def _sync_duplicate_groups_for_disc_safe(disc) -> None:
    """Enforce duplicate primary/ignore invariant after title rows change. Logs on failure."""
    try:
        from sqlalchemy import inspect
        from core.duplicate_group_sync import sync_duplicate_group_labels_for_disc

        sess = inspect(disc).session
        disc_id = getattr(disc, "id", None)
        if sess is not None and disc_id is not None:
            sync_duplicate_group_labels_for_disc(sess, str(disc_id))
    except Exception:
        logger.exception(
            "_sync_duplicate_groups_for_disc_safe failed disc_id=%s",
            getattr(disc, "id", None),
        )


def _apply_path_b_marks_for_disc_safe(disc) -> None:
    """Persist Path B obfuscation/subsumption marks after title rows change.

    Runs at scan time so GET workflow-context stays a pure read (it used to
    perform these writes mid-GET — see apply_path_b_marks_for_disc). The
    rows must be flushed first so the apply pass sees them. Logs on
    failure; a failed mark pass must never fail the scan itself."""
    try:
        from sqlalchemy import inspect
        from core.path_b_dedupe import apply_path_b_marks_for_disc

        sess = inspect(disc).session
        disc_id = getattr(disc, "id", None)
        if sess is not None and disc_id is not None:
            sess.flush()
            cleared, set_sibling, marked, set_sub = apply_path_b_marks_for_disc(sess, str(disc_id))
            if cleared or set_sibling or marked or set_sub:
                logger.info(
                    "Path B (scan-time): disc %s reason cleared=%d set=%d; subsumption ignore=%d set=%d",
                    disc_id, cleared, set_sibling, marked, set_sub,
                )
    except Exception:
        logger.exception(
            "_apply_path_b_marks_for_disc_safe failed disc_id=%s",
            getattr(disc, "id", None),
        )


def _apply_scan_tracks(disc, scan_tracks: list[dict]):
    """
    Persist parsed scan_tracks into disc_titles and title_streams so the UI can
    render tracks even when DiscDB is missing.
    """
    if not scan_tracks or not hasattr(disc, "titles"):
        return
    existing_titles = getattr(disc, "titles", None) or []

    def _is_numeric_string(s):
        if not isinstance(s, str):
            return False
        try:
            int(s)
            return True
        except ValueError:
            return False

    has_incorrect_source_files = False
    if existing_titles:
        for title in existing_titles:
            if hasattr(title, "source_file") and title.source_file:
                if _is_numeric_string(str(title.source_file)):
                    has_incorrect_source_files = True
                    break

    # Existing rows with real filenames: reconcile by source_file, then append new titles only.
    if existing_titles and not has_incorrect_source_files:
        scan_by_sf: dict[str, dict] = {}
        for t in scan_tracks:
            sk = t.get("source_file") or t.get("track_id")
            if not sk:
                continue
            scan_by_sf[str(sk)] = t

        by_sf = {
            str(x.source_file): x
            for x in existing_titles
            if getattr(x, "source_file", None)
        }
        for sf, t in scan_by_sf.items():
            row = by_sf.get(sf)
            if row:
                _reconcile_disc_title_from_scan_track(disc, row, t)

        existing_sf = set(by_sf.keys())
        max_order = max(
            (x.order_index for x in existing_titles if x.order_index is not None),
            default=-1,
        )
        added = 0
        for t in scan_tracks:
            sk = t.get("source_file") or t.get("track_id")
            if not sk:
                continue
            sk = str(sk)
            if sk in existing_sf:
                continue
            oi = max_order + 1 + added
            row = _append_disc_title_from_scan_track(disc, t, oi)
            if row:
                existing_sf.add(sk)
                added += 1
        _sync_duplicate_groups_for_disc_safe(disc)
        _apply_path_b_marks_for_disc_safe(disc)
        return

    if has_incorrect_source_files:
        try:
            from sqlalchemy import inspect
            sess = inspect(disc).session
            if sess:
                sess.query(models.DiscTitle).filter(models.DiscTitle.disc_id == disc.id).delete()
                sess.query(models.TitleStream).filter(models.TitleStream.disc_id == disc.id).delete()
                sess.flush()
        except Exception:
            pass
        disc.titles = []
        disc.title_streams = []
    try:
        from sqlalchemy import inspect
        sess = inspect(disc).session
        if sess:
            sess.query(models.DiscTitle).filter(models.DiscTitle.disc_id == disc.id).delete()
            sess.query(models.TitleStream).filter(models.TitleStream.disc_id == disc.id).delete()
            sess.flush()
    except Exception:
        pass
    disc.titles = []
    disc.title_streams = []
    scan_by_sf_replace: dict[str, dict] = {}
    first_pos: dict[str, int] = {}
    for i, t in enumerate(scan_tracks):
        sk = t.get("source_file") or t.get("track_id")
        if not sk:
            continue
        sk = str(sk)
        scan_by_sf_replace[sk] = t
        first_pos.setdefault(sk, i)
    ordered_sf = sorted(scan_by_sf_replace.keys(), key=lambda sf: first_pos[sf])
    for idx, sf in enumerate(ordered_sf):
        _append_disc_title_from_scan_track(disc, scan_by_sf_replace[sf], idx)
    _sync_duplicate_groups_for_disc_safe(disc)
    _apply_path_b_marks_for_disc_safe(disc)


def create_job(
    db: Session,
    disc_num: str,
    mount_point: str,
    mode: str = "copy",
    output_dir: str | None = None,
    payload: dict | None = None,
    celery_task_id: str | None = None,
) -> models.Job:
    # load payload if not provided (from Disc Manager)
    if payload is None:
        try:
            from core.disc_manager import get_disc_info
            payload = get_disc_info(str(disc_num), mount_point)
        except Exception as exc:
            raise ValueError("disc payload missing; refresh disc info before starting a job") from exc
    payload = _hydrate_payload(disc_num, mount_point, payload, force_scan=False, allow_scan=False)
    # Sanitize non-ASCII characters that crash psycopg2 with ascii client_encoding
    # (e.g. → U+2192 in MakeMKV title descriptions or DiscDB metadata)
    payload = _sanitize_unicode_for_db(payload)
    disc_hash = payload.get("disc_hash")
    if not disc_hash:
        raise ValueError("disc_hash missing from payload; cannot create job")

    # Check if disc already exists and has a release (from previous labeling)
    # During rip start, we don't create releases - they're assigned during labeling phase
    existing_disc = db.query(models.Disc).filter(models.Disc.content_hash == disc_hash).first()
    release = None
    if existing_disc and existing_disc.release_id:
        # Disc already has a release from previous labeling - reuse it
        release = db.query(models.Release).filter(models.Release.id == existing_disc.release_id).first()
    
    # Create or update disc (release may be None if not yet labeled)
    disc = get_or_create_disc(db, disc_hash, release, payload)
    payload.setdefault("disc_hash", disc_hash)
    if release:
        payload.setdefault("disc_group", release.slug)
        payload.setdefault("group_type", release.type)
        # Set movie_name from release.movie.name if available (hierarchy: movie.name -> release.name -> title.name)
        if release.movie:
            payload.setdefault("movie_name", release.movie.name or "")
        else:
            payload.setdefault("movie_name", "")
    
    # Extract scan_tracks before removing disc scan info (needed for _apply_scan_tracks)
    scan_tracks = payload.get("scan_tracks") or []
    
    # Extract and store disc scan info in disc.disc_info
    disc_scan_info = _extract_disc_scan_info(payload)
    if disc_scan_info:
        _store_disc_scan_info(db, disc, disc_scan_info)
        # Remove disc scan info fields from payload (they're now in disc.disc_info)
        for field in disc_scan_info.keys():
            payload.pop(field, None)
    if scan_tracks:
        _apply_scan_tracks(disc, scan_tracks)
    db_mapping = payload.get("tracks") or payload.get("db_mapping") or {}
    if db_mapping:
        _apply_discdb_metadata_to_titles(disc, db_mapping)
    if scan_tracks or db_mapping:
        db.commit()
        db.refresh(disc)
    label_required = bool(payload.get("label_required"))
    discdb_result = payload.get("discdb_result")
    if not discdb_result:
        payload_discdb_hit = payload.get("discdb_hit")
        if payload_discdb_hit is True:
            discdb_result = "hit"
        elif payload_discdb_hit is False:
            discdb_result = "miss"
        else:
            discdb_result = "miss" if label_required else "hit"
    stage_profile = "miss" if label_required else "hit"
    label_state = "pending" if stage_profile == "miss" else "skipped"
    finalize_state = "pending" if stage_profile == "miss" else "skipped"
    finalize_release_state = "pending" if stage_profile == "miss" else "skipped"

    # Persist the by-id stable hardware identity at job-creation time (#540).
    # The drive_swap_handler from #551 fails jobs whose drive_by_id_serial
    # matches the previous identity at a swapped mount_point; without this
    # write the column stays NULL and the swap detector can never match.
    # Resolution failures fall through to NULL — the policy gate already
    # refused the rip if the identity is unusable, so reaching this point
    # implies at least a degraded but usable resolution.
    drive_by_id_serial: str | None = None
    try:
        from core.drive_identity import resolve_drive_identity

        identity = resolve_drive_identity(mount_point)
        if identity.identity_source != "unknown":
            drive_by_id_serial = identity.by_id_serial
    except Exception:
        drive_by_id_serial = None

    job = models.Job(
        disc_id=disc.id,
        disc_num=disc_num,
        mount_point=mount_point,
        drive_by_id_serial=drive_by_id_serial,
        mode=mode,
        disc_payload=payload,
        stage_profile=stage_profile,
        discdb_result=discdb_result,
        scan_state="completed",  # info scan + hash validation already succeeded
        rip_state="pending",
        label_state=label_state,
        finalize_state=finalize_state,
        # #365 step 5 — post_state column dropped; derived via
        # Job.derived_post_state. When rip_state="pending" the derivation
        # returns None, which is the canonical "not yet" value.
        transfer_state="pending",
        finalize_release_state=finalize_release_state,
        dev_mode=is_dev_mode(),
        celery_task_id=celery_task_id,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def ensure_disc_record_from_scan(
    db: Session, disc_num: str, mount_point: str, payload: dict | None = None
) -> models.Disc | None:
    """
    During initial drive scans (before a copy job), ensure a Release + Disc record exists.
    Uses the provided payload (from drive-manager cache) and persists release/disc and any
    scan_tracks as DiscTitle rows without touching the drive.
    """
    if payload is None:
        payload = {}
    
    # Check if disc already exists and has disc_info - merge it into payload first
    disc_hash = payload.get("disc_hash") or payload.get("content_hash")
    if disc_hash:
        existing_disc = db.query(models.Disc).filter(models.Disc.content_hash == disc_hash).first()
        if existing_disc and existing_disc.disc_info:
            # Merge existing disc_info into payload (payload takes precedence)
            payload = {**existing_disc.disc_info, **payload}
    
    hydrated = _hydrate_payload(disc_num, mount_point, dict(payload), force_scan=False, allow_scan=False)
    disc_hash = hydrated.get("disc_hash") or payload.get("disc_hash")
    if not disc_hash:
        return None
    # Ensure disc_size_bytes is set when we have mount_point (fallback if scan payload lacked it)
    if not hydrated.get("disc_size_bytes") and mount_point:
        size = get_disc_size_bytes_for_mount_point(mount_point)
        if size is not None:
            hydrated["disc_size_bytes"] = size
    disc = persist_disc_scan_with_discdb(db, disc_hash, hydrated)
    # #720: a successful scan must clear the previous failure. Otherwise a disc
    # that failed once and then scanned fine keeps a stale last_scan_error
    # forever, which now drives the user-facing Start Copy message (and would
    # report a decrypt/read failure on a disc that actually scanned).
    if getattr(disc, "last_scan_error", None):
        titles_now = (
            db.query(models.DiscTitle).filter(models.DiscTitle.disc_id == disc.id).count()
        )
        if titles_now or disc.disc_info:
            disc.last_scan_error = None
            db.commit()
            logger.info("Cleared stale last_scan_error after successful scan: disc=%s", disc.id)
    rel = disc.release
    logger.info(
        "Persisted scan: disc=%s release=%s slug=%s format=%s info_title=%s",
        disc.id,
        rel.id if rel else None,
        rel.slug if rel else None,
        disc.format,
        rel.name if rel else None,
    )
    # Persist scan_tracks first so disc_titles has all titles from the disc (hit and miss).
    scan_tracks = hydrated.get("scan_tracks") or []
    if scan_tracks:
        _apply_scan_tracks(disc, scan_tracks)

    # Then apply DiscDB: overlay metadata on existing titles (hit) or create titles from mapping only (no scan).
    db_mapping = hydrated.get("tracks") or hydrated.get("db_mapping") or {}
    if db_mapping:
        existing_titles = getattr(disc, "titles", None) or []
        if existing_titles:
            _apply_discdb_metadata_to_titles(disc, db_mapping)
        else:
            _apply_discdb_tracks(disc, db_mapping)

    # Store disc scan info in disc.disc_info
    disc_scan_info = _extract_disc_scan_info(hydrated)
    if disc_scan_info:
        _store_disc_scan_info(db, disc, disc_scan_info)

    if db_mapping or scan_tracks:
        db.commit()
        db.refresh(disc)

    # #638: reject empty scan output. When a udev-triggered scan races the previous
    # disc's cleanup, we can land here with format=None and no titles — the disc row
    # otherwise gets scan_state='completed' downstream and a subsequent rip wastes 20+
    # minutes before rip_verification fails on "no MKV outputs found under raw/".
    # Mark the disc as failed so the user has to eject+reinsert; a fresh udev event
    # will drive a clean scan without the race pressure.
    has_titles = bool(getattr(disc, "titles", None) or scan_tracks or db_mapping)
    if not disc.format and not has_titles:
        logger.warning(
            "#638: empty scan output for disc=%s (format=None, 0 titles). "
            "Marking scan_state='failed' — disc must be re-scanned.",
            disc.id,
        )
        disc.scan_state = "failed"
        disc.last_scan_error = (
            "Empty scan output — no format and no tracks enumerated. "
            "Eject and reinsert the disc to retry."
        )
        db.commit()
        db.refresh(disc)
    return disc


def get_job(db: Session, job_id):
    try:
        return db.get(models.Job, job_id)
    except AttributeError:
        return db.query(models.Job).get(job_id)


def update_job(db, job, **kwargs):
    for k, v in kwargs.items():
        setattr(job, k, v)
    db.commit()
    db.refresh(job)
    return job


def append_log(db, job, line):
    logs = job.logs or []
    logs.append(line)
    job.logs = logs[-500:]
    # Keep updated_at in sync so API time-based heuristics see worker log activity.
    job.updated_at = datetime.datetime.now(datetime.timezone.utc)
    db.commit()
    db.refresh(job)
    return job


def get_most_recent_running_job(db: Session):
    """
    Return the most recent job that is still doing work: either the rip/post-process
    is running/pending, or the transfer stage is running/pending.
    """
    return (
        db.query(models.Job)
        .filter(
            or_(
                models.Job.job_status.in_(["pending", "running"]),
                models.Job.transfer_state.in_(["pending", "ready", "running"]),
            )
        )
        .order_by(models.Job.created_at.desc())
        .first()
    )


def get_active_job_for_disc(db: Session, disc_num: str, disc_hash: str | None = None):
    """
    Find the most recent active job for a disc (by disc_num and optional hash).
    """
    q = db.query(models.Job).join(models.Disc, models.Job.disc_id == models.Disc.id)
    q = q.filter(
        or_(
            models.Job.job_status.in_(["pending", "running"]),
            models.Job.transfer_state.in_(["pending", "ready", "running"]),
        )
    )
    exprs = [models.Job.disc_num == disc_num]
    if disc_hash:
        exprs.append(models.Disc.content_hash == disc_hash)
    q = q.filter(or_(*exprs))
    return q.order_by(models.Job.created_at.desc()).first()


def get_active_job_for_hash(db: Session, disc_hash: str):
    # Exclude failed jobs so they never block starting a new rip
    not_failed = ~or_(
        models.Job.job_status == "failed",
        models.Job.rip_state == "failed",
    )
    q = (
        db.query(models.Job)
        .join(models.Disc, models.Job.disc_id == models.Disc.id)
        .filter(
            models.Disc.content_hash == disc_hash,
            or_(
                models.Job.job_status.in_(["pending", "running"]),
                models.Job.rip_state.in_(["pending", "running"]),
                models.Job.transfer_state.in_(["pending", "ready", "running"]),
            ),
            not_failed,
        )
        .order_by(models.Job.created_at.desc())
    )
    return q.first()


def get_job_by_task_id(db: Session, task_id: str) -> models.Job | None:
    """Find a job by its Celery task_id."""
    return db.query(models.Job).filter(models.Job.celery_task_id == task_id).first()


# Boxset CRUD operations

def get_or_create_boxset(db: Session, payload: dict) -> models.Boxset | None:
    """Create or find boxset by slug."""
    slug = payload.get("slug")
    if not slug:
        # Generate slug from name
        name = payload.get("name") or payload.get("title")
        if not name:
            return None
        slug = slugify(name)
    
    boxset = db.query(models.Boxset).filter(models.Boxset.slug == slug).first()
    if boxset:
        return boxset
    
    # Create new boxset
    boxset = models.Boxset(
        id=str(uuid.uuid4()),
        slug=slug,
        name=payload.get("name"),
        title=payload.get("title") or payload.get("name"),
        sort_title=payload.get("sort_title"),
        upc=payload.get("upc"),
        asin=payload.get("asin"),
        year=payload.get("year"),
        locale=payload.get("locale"),
        region_code=payload.get("region_code"),
        cover_front_url=payload.get("cover_front_url"),
        cover_back_url=payload.get("cover_back_url"),
        release_date=payload.get("release_date"),
    )
    db.add(boxset)
    db.flush()
    return boxset


def get_boxset_by_slug(db: Session, slug: str) -> models.Boxset | None:
    """Retrieve boxset by slug."""
    return db.query(models.Boxset).filter(models.Boxset.slug == slug).first()


def get_boxset_by_id(db: Session, boxset_id: str) -> models.Boxset | None:
    """Retrieve boxset by id."""
    return db.query(models.Boxset).filter(models.Boxset.id == boxset_id).first()


def list_boxsets(db: Session, finalized: bool | None = None) -> list[models.Boxset]:
    """List all boxsets, optionally filtered by finalized status."""
    q = db.query(models.Boxset)
    if finalized is not None:
        q = q.filter(models.Boxset.finalized == finalized)
    return q.order_by(models.Boxset.created_at.desc()).all()


def add_release_to_boxset(db: Session, boxset: models.Boxset, release: models.Release) -> models.Release:
    """Link release to boxset by setting boxset_id, updating release metadata from boxset."""
    # If release is already linked to a different boxset, unlink it first (one-to-many relationship)
    if release.boxset_id and release.boxset_id != boxset.id:
        release.boxset_id = None
    
    # Link to this boxset
    release.boxset_id = boxset.id
    
    # Always copy ALL boxset fields to release when linking/relinking
    # Boxset is authoritative source - always overwrite release fields
    if boxset.slug:
        release.slug = boxset.slug
    if boxset.name:
        release.name = boxset.name
    if boxset.year:
        release.release_year = boxset.year
    if boxset.cover_front_url:
        release.cover_front_url = boxset.cover_front_url
    if boxset.cover_back_url:
        release.cover_back_url = boxset.cover_back_url
    if boxset.upc:
        release.upc = boxset.upc
    if boxset.asin:
        release.asin = boxset.asin
    
    db.flush()
    return release


def cleanup_orphaned_release(db: Session, release: models.Release) -> bool:
    """
    Check if a release has any associated discs, and delete it if it has none.
    Returns True if release was deleted, False otherwise.
    
    SAFETY: Multiple checks are performed to prevent accidental deletion of releases
    that still have discs. This function will NOT delete a release if it has ANY discs.
    """
    if not release:
        return False
    
    # Refresh release to ensure we have the latest state
    db.refresh(release)
    
    # First check: count discs via query
    disc_count = db.query(models.Disc).filter(models.Disc.release_id == release.id).count()
    
    logger.info(
        f"cleanup_orphaned_release: Evaluating release {release.id} "
        f"(slug: {release.slug}, name: {release.name}) - disc_count={disc_count}"
    )
    
    if disc_count > 0:
        logger.info(
            f"cleanup_orphaned_release: Release {release.id} has {disc_count} disc(s), NOT deleting"
        )
        return False
    
    # Second check: verify via relationship (should match query count)
    relationship_disc_count = len(release.discs) if release.discs else 0
    
    if relationship_disc_count != disc_count:
        logger.warning(
            f"cleanup_orphaned_release: Mismatch between query count ({disc_count}) "
            f"and relationship count ({relationship_disc_count}) for release {release.id}. "
            f"NOT deleting as a safety measure."
        )
        return False
    
    # Triple check: query again to ensure count is still zero (race condition check)
    disc_count_final = db.query(models.Disc).filter(models.Disc.release_id == release.id).count()
    
    if disc_count_final != disc_count:
        logger.warning(
            f"cleanup_orphaned_release: Disc count changed from {disc_count} to {disc_count_final} "
            f"for release {release.id}. Potential race condition detected. NOT deleting."
        )
        return False
    
    if disc_count_final == 0:
        logger.warning(
            f"cleanup_orphaned_release: DELETING orphaned release {release.id} "
            f"(slug: {release.slug}, name: {release.name}, movie_id: {release.movie_id}, "
            f"boxset_id: {release.boxset_id}) - verified zero discs after multiple checks"
        )
        db.delete(release)
        db.flush()
        logger.info(f"cleanup_orphaned_release: Successfully deleted release {release.id}")
        return True
    
    return False


def cleanup_orphaned_boxset(db: Session, boxset: models.Boxset) -> bool:
    """
    Check if a boxset has any associated releases, and delete it if it has none.
    Returns True if boxset was deleted, False otherwise.
    """
    if not boxset:
        return False

    db.refresh(boxset)
    release_count = db.query(models.Release).filter(models.Release.boxset_id == boxset.id).count()

    logger.info(
        f"cleanup_orphaned_boxset: Evaluating boxset {boxset.id} "
        f"(name: {getattr(boxset, 'name', None)}) - release_count={release_count}"
    )

    if release_count > 0:
        logger.info(
            f"cleanup_orphaned_boxset: Boxset {boxset.id} has {release_count} release(s), NOT deleting"
        )
        return False

    rel_count_relationship = len(boxset.releases) if boxset.releases else 0
    if rel_count_relationship != release_count:
        logger.warning(
            f"cleanup_orphaned_boxset: Mismatch between query count ({release_count}) "
            f"and relationship count ({rel_count_relationship}) for boxset {boxset.id}. NOT deleting."
        )
        return False

    release_count_final = db.query(models.Release).filter(models.Release.boxset_id == boxset.id).count()
    if release_count_final != release_count:
        logger.warning(
            f"cleanup_orphaned_boxset: Release count changed for boxset {boxset.id}. NOT deleting."
        )
        return False

    logger.warning(
        f"cleanup_orphaned_boxset: DELETING orphaned boxset {boxset.id} (name: {getattr(boxset, 'name', None)})"
    )
    db.delete(boxset)
    db.flush()
    logger.info(f"cleanup_orphaned_boxset: Successfully deleted boxset {boxset.id}")
    return True


def update_boxset_metadata(db: Session, boxset: models.Boxset, payload: dict) -> models.Boxset:
    """Update boxset metadata and propagate to all linked releases."""
    # Update boxset fields
    if "name" in payload:
        boxset.name = payload["name"]
    if "title" in payload:
        boxset.title = payload["title"]
    if "sort_title" in payload:
        boxset.sort_title = payload["sort_title"]
    if "year" in payload:
        boxset.year = payload["year"]
    if "upc" in payload:
        boxset.upc = payload["upc"]
    if "asin" in payload:
        boxset.asin = payload["asin"]
    if "locale" in payload:
        boxset.locale = payload["locale"]
    if "region_code" in payload:
        boxset.region_code = payload["region_code"]
    if "cover_front_url" in payload:
        boxset.cover_front_url = payload["cover_front_url"]
    if "cover_back_url" in payload:
        boxset.cover_back_url = payload["cover_back_url"]
    if "release_date" in payload:
        boxset.release_date = payload["release_date"]
    
    # Propagate boxset metadata to all linked releases (always overwrite)
    # Boxset is authoritative source - always overwrite release fields
    releases = db.query(models.Release).filter(models.Release.boxset_id == boxset.id).all()
    for release in releases:
        if boxset.slug:
            release.slug = boxset.slug
        if boxset.name:
            release.name = boxset.name
        if boxset.year:
            release.release_year = boxset.year
        if boxset.cover_front_url:
            release.cover_front_url = boxset.cover_front_url
        if boxset.cover_back_url:
            release.cover_back_url = boxset.cover_back_url
        if boxset.upc:
            release.upc = boxset.upc
        if boxset.asin:
            release.asin = boxset.asin
    
    db.flush()
    return boxset