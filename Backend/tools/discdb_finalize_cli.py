#!/usr/bin/env python3
"""
Dev helper to rerun DiscDB finalize outside the API.

Example:
    python tools/discdb_finalize_cli.py --disc-id <disc uuid> [--base-dir /path/to/job] [--persist] [--force]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.logging_utils import get_logger

from api import database, crud  # type: ignore

logger = get_logger("tools.discdb_finalize_cli")
from api import models as db_models  # type: ignore
from core.discdb_finalize import finalize_from_label  # type: ignore


def _build_label_payload(disc: Any) -> Dict[str, Any]:
    """Rehydrate the label payload from persisted disc + release records."""
    release = getattr(disc, "release", None)
    # Build titles + streams from persisted disc state to match API finalize path.
    stream_map = {}
    for tr in getattr(disc, "tracks", []) or []:
        tid = getattr(tr, "title_id", None)
        if not tid:
            continue
        streams = stream_map.setdefault(str(tid), [])
        streams.append(
            {
                "index": getattr(tr, "stream_index", None),
                "type": getattr(tr, "stream_type", None),
                "audio_type": getattr(tr, "audio_type", None),
                "language_code": getattr(tr, "language_code", None),
                "language": getattr(tr, "language", None),
                "codec_short": getattr(tr, "codec_short", None),
                "codec_hint": getattr(tr, "codec_hint", None),
                "name": getattr(tr, "name", None),
                "resolution": getattr(tr, "resolution", None),
                "aspect_ratio": getattr(tr, "aspect_ratio", None),
            }
        )

    titles = []
    for t in getattr(disc, "titles", []) or []:
        track_id = getattr(t, "source_file", None) or getattr(t, "index", None)
        titles.append(
            {
                "track_id": track_id,
                "title_id": getattr(t, "id", None),
                "title": getattr(t, "title", None),
                "description": getattr(t, "description", None),
                "season": getattr(t, "season", None),
                "episode": getattr(t, "episode", None),
                "type": getattr(t, "type", None),
                "note": getattr(t, "description", None) or getattr(t, "comment", None),
                "comment": getattr(t, "comment", None),
                "duration": getattr(t, "duration", None) or getattr(t, "duration_seconds", None),
                "size": getattr(t, "size", None),
                "display_size": getattr(t, "display_size", None),
                "segment_map": getattr(t, "segment_map", None),
                "chapters": getattr(t, "chapters", None),
                "streams": stream_map.get(str(getattr(t, "id", None)), None),
                "content": getattr(t, "content", True),
                "index": getattr(t, "index", None),
                "source_file": getattr(t, "source_file", None),
            }
        )

    payload: Dict[str, Any] = {
        "disc_slug": getattr(disc, "disc_slug", None),
        "disc_name": getattr(disc, "disc_name", None),
        "disc_number": getattr(disc, "disc_number", None),
        "disc_format": getattr(disc, "format", None),
        "titles": titles,
    }
    if release:
        payload.update(
            {
                "release_slug": release.slug,
                "release_name": release.name,
                "release_year": getattr(release, "release_year", None),
                "original_year": getattr(release, "original_year", None),
                "production_year": getattr(release, "production_year", None),
                "tmdb_id": release.tmdb_id,
                "upc": release.upc,
                "asin": release.asin,
                "cover_front_url": release.cover_front_url,
                "cover_back_url": release.cover_back_url,
                "group_type": release.type,
            }
        )
    return payload


def _select_base_dir(disc: Any, job: Optional[Any], override: Optional[Path]) -> Path:
    if override:
        return override
    if job:
        for field in ("tmp_dir", "result_location"):
            val = getattr(job, field, None)
            if val:
                p = Path(val)
                if p.exists():
                    return p
    artifacts = getattr(disc, "artifacts", {}) or {}
    loc = artifacts.get("result_location")
    if loc:
        p = Path(loc)
        if p.exists():
            return p
    raise FileNotFoundError("No temp/result folder found for this disc; pass --base-dir explicitly.")


def finalize_disc(sess, disc_id: str, job_id: Optional[str], base_dir: Optional[Path], persist: bool, force: bool) -> int:
    disc = (
        sess.query(db_models.Disc)  # type: ignore[attr-defined]
        .options()
        .filter(db_models.Disc.id == disc_id)  # type: ignore[attr-defined]
        .first()
    )
    if not disc:
        raise SystemExit(f"Disc not found: {disc_id}")
    if getattr(disc, "finalized", False) and not force:
        raise SystemExit("Disc already finalized; use --force to rerun.")

    job = None
    if job_id:
        job = crud.get_job(sess, job_id)
        if not job:
            raise SystemExit(f"Job not found: {job_id}")
    else:
        jobs = list(getattr(disc, "jobs", []) or [])
        if jobs:
            job = sorted(jobs, key=lambda j: getattr(j, "created_at", datetime.min))[-1]

    base = _select_base_dir(disc, job, base_dir)
    label_payload = _build_label_payload(disc)
    rel = getattr(disc, "release", None)
    result = finalize_from_label(
        base,
        label_payload,
        disc_hash=getattr(disc, "content_hash", None),
        release_type=getattr(rel, "type", None),
        release_name=getattr(rel, "name", None),
        release_slug_override=getattr(rel, "id", None),
        write_release_artifacts=False,
        write_film_metadata=False,
    )

    logger.info("Disc finalize completed.")
    for key, val in result.items():
        logger.info("%s: %s", key, val)

    if persist:
        disc.finalized = True
        disc.finalize_result = result
        disc.finalized_at = datetime.utcnow()
        sess.commit()
        logger.info("Persisted finalize_result to database.")
    return 0


def finalize_release(sess, release_id: str, force: bool) -> int:
    rel = (
        sess.query(db_models.Release)  # type: ignore[attr-defined]
        .options()
        .filter(db_models.Release.id == release_id)  # type: ignore[attr-defined]
        .first()
    )
    if not rel:
        raise SystemExit(f"Release not found: {release_id}")
    if getattr(rel, "finalized", False) and not force:
        raise SystemExit("Release already finalized; use --force to rerun.")
    # Call API finalize logic (imported function)
    from api.routers import releases as releases_router  # lazy import to reuse code

    result = releases_router.finalize_release(release_id, db=sess)
    logger.info("Release finalize completed.")
    logger.info("%s", result)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-run DiscDB finalize for a disc.")
    sub = parser.add_subparsers(dest="command", required=True)

    disc_cmd = sub.add_parser("disc", help="Finalize a single disc (discNN outputs only)")
    disc_cmd.add_argument("--disc-id", required=True, help="Disc UUID")
    disc_cmd.add_argument("--job-id", help="Optional job UUID to source tmp/result folder")
    disc_cmd.add_argument("--base-dir", type=Path, help="Override base directory containing makemkv_info.log")
    disc_cmd.add_argument("--persist", action="store_true", help="Persist finalize_result + finalized flags back to DB")
    disc_cmd.add_argument("--force", action="store_true", help="Run even if disc is already finalized")

    rel_cmd = sub.add_parser("release", help="Finalize a release (move temp folder, write release.json, covers, film metadata)")
    rel_cmd.add_argument("--release-id", required=True, help="Release UUID")
    rel_cmd.add_argument("--force", action="store_true", help="Run even if release is already finalized")

    args = parser.parse_args()

    session = database.SessionLocal()
    try:
        if args.command == "disc":
            return finalize_disc(session, args.disc_id, args.job_id, args.base_dir, args.persist, args.force)
        if args.command == "release":
            return finalize_release(session, args.release_id, args.force)
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
