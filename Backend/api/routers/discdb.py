import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload

from api import database, models as db_models
from core import discdb_import
from core.utils import DISKDBURL

router = APIRouter(prefix="/discdb", tags=["discdb"])
log = logging.getLogger("api.routers.discdb")

IMAGE_BASE = "https://thediscdb.com/images/"


def _abs_image(path: str | None) -> str | None:
    if not path:
        return None
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return IMAGE_BASE + path.lstrip("/")


def _run_query(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    try:
        resp = requests.post(
            DISKDBURL,
            json={"query": query, "variables": variables},
            timeout=15,
        )
        if not resp.ok:
            raise HTTPException(502, detail=f"DiscDB request failed ({resp.status_code}): {resp.text} | query={query}")
        data = resp.json()
        if "errors" in data:
            raise HTTPException(502, detail=f"DiscDB error: {data['errors']} | query={query}")
        return data.get("data") or {}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, detail=f"DiscDB request failed: {exc}")


class DiscHashRequest(BaseModel):
    mount: str = Field(..., description="Disc mount point or drive letter (e.g., /mnt/disc or E)")


class FileHashInfoResponse(BaseModel):
    index: int
    name: str
    creation_time: str
    size: int


class DiscHashResponse(BaseModel):
    hash: Optional[str] = None
    files: List[FileHashInfoResponse] = Field(default_factory=list)


@router.post("/hash", response_model=DiscHashResponse)
def compute_disc_hash(payload: DiscHashRequest):
    """
    Compute a content hash for a mounted disc (Blu-ray/DVD) by scanning
    the BDMV/STREAM or VIDEO_TS directory and hashing file sizes in order.
    """
    info = discdb_import.hash_media_disc_cached(payload.mount)
    if info is None:
        raise HTTPException(404, detail="No disc files found (expected BDMV/STREAM or VIDEO_TS)")
    return info.to_dict()


@router.get("/search")
def search_discdb(q: str = Query(..., min_length=2)):
    """
    Search TheDiscDB by title substring.
    """
    query = """
    query SearchMedia($term: String!) {
      mediaItems(where: { title: { contains: $term } }, order: { title: ASC }) {
        nodes {
          id
          title
          type
          slug
          imageUrl
          releases { year imageUrl }
        }
      }
    }
    """
    data = _run_query(query, {"term": q})
    nodes: List[Dict[str, Any]] = data.get("mediaItems", {}).get("nodes") or []
    results = []
    for n in nodes:
        rel0 = (n.get("releases") or [{}])[0]
        results.append(
            {
                "id": n.get("id"),
                "title": n.get("title"),
                "type": n.get("type"),
                "slug": n.get("slug"),
                "image": _abs_image(n.get("imageUrl") or rel0.get("imageUrl")),
                "year": rel0.get("year"),
            }
        )
    return {"results": results}


@router.get("/detail")
def discdb_detail(slug: str = Query(..., min_length=2)):
    """
    Fetch detailed info for a media item by slug.
    """
    query = """
    query Detail($slug: String!) {
      mediaItems(where: { slug: { eq: $slug } }) {
        nodes {
          id
          title
          slug
          type
          imageUrl
          releases {
            year
            imageUrl
            discs {
              name
              format
              contentHash
            }
          }
        }
      }
    }
    """
    data = _run_query(query, {"slug": slug})
    nodes = data.get("mediaItems", {}).get("nodes") or []
    node = nodes[0] if nodes else None
    if not node:
        raise HTTPException(404, detail="Title not found")
    # Flatten a minimal payload
    rel = node.get("releases") or []
    discs = []
    for r in rel:
        for d in (r.get("discs") or []):
            discs.append(
                {
                    "name": d.get("name"),
                    "format": d.get("format"),
                    "contentHash": d.get("contentHash"),
                    "year": r.get("year"),
                }
            )
    return {
        "id": node.get("id"),
        "title": node.get("title"),
        "slug": node.get("slug"),
        "type": node.get("type"),
        "image": _abs_image(node.get("imageUrl") or (rel[0].get("imageUrl") if rel else None)),
        "synopsis": None,
        "releases": [
            {**r, "imageUrl": _abs_image(r.get("imageUrl"))}
            for r in rel
        ],
        "discs": discs,
    }


# ── Library matching (#590) ──────────────────────────────────────────────


def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _normalize_title(s: str) -> str:
    """Normalize a title for cross-source matching: lowercase, strip
    leading 'the '/'a ', collapse whitespace. TheDiscDB tends to keep
    the leading article ("The Goonies"); user-edited library entries
    may not. Drop both forms to a common bucket. """
    out = (s or "").strip().lower()
    while True:
        if out.startswith("the "):
            out = out[4:]
        elif out.startswith("a "):
            out = out[2:]
        elif out.startswith("an "):
            out = out[3:]
        else:
            break
    return " ".join(out.split())


class LibraryMatchRequest(BaseModel):
    """Body for `POST /discdb/library-matches` — list of result titles
    the caller wants checked against the user's library (#590)."""
    titles: List[str] = Field(default_factory=list)


class LibraryMatchResponse(BaseModel):
    """Returns the subset of the requested titles whose normalized form
    matches a movie name in the library. The original titles are echoed
    so the caller can match against its own card list without
    re-normalizing."""
    matched_titles: List[str] = Field(default_factory=list)


@router.post("/library-matches", response_model=LibraryMatchResponse)
def discdb_library_matches(req: LibraryMatchRequest, db: Session = Depends(get_db)):
    """Given a list of search-result titles, return which of them
    correspond to a movie already in the user's library.

    Matches are normalised (case + leading-article + whitespace) so
    "The Goonies" from TheDiscDB matches a "Goonies" entry in the
    library and vice versa. v1 only looks at `movies.name`; the
    backend-side contentHash-level matching (per-disc "ripped" chip)
    is a follow-up scoped for a v2 cut.
    """
    if not req.titles:
        return LibraryMatchResponse(matched_titles=[])

    requested_norm_to_original: Dict[str, str] = {}
    for t in req.titles:
        n = _normalize_title(t)
        if n and n not in requested_norm_to_original:
            requested_norm_to_original[n] = t

    if not requested_norm_to_original:
        return LibraryMatchResponse(matched_titles=[])

    library_names = db.query(db_models.Movie.name).all()
    library_norm = {_normalize_title(name) for (name,) in library_names if name}

    matched = [
        original
        for norm, original in requested_norm_to_original.items()
        if norm in library_norm
    ]
    return LibraryMatchResponse(matched_titles=matched)


# ── Contribution management (#334) ──────────────────────────────────────────


class ContributionStatusUpdate(BaseModel):
    """Request body for updating contribution status."""
    status: Optional[str] = None  # not_submitted, draft, exported, submitted, accepted, rejected
    notes: Optional[str] = None


@router.get("/contributions")
def list_contributions(
    status: Optional[str] = Query(default=None, description="Filter by contribution status"),
    db: Session = Depends(get_db),
):
    """
    List discs eligible for DiscDB contribution (labeled misses with completed jobs).
    Returns disc metadata + contribution status for the Contributions tab (#334).
    """
    q = (
        db.query(db_models.Disc)
        .options(
            joinedload(db_models.Disc.release).joinedload(db_models.Release.movie),
        )
        .join(db_models.Job, db_models.Job.disc_id == db_models.Disc.id)
        .filter(
            db_models.Job.job_status.in_(["completed", "running"]),
            db_models.Job.rip_state == "completed",
        )
    )
    if status:
        q = q.filter(db_models.Disc.discdb_contribution_status == status)
    discs = q.distinct().order_by(db_models.Disc.updated_at.desc()).limit(100).all()

    results = []
    for disc in discs:
        rel = disc.release
        movie = rel.movie if rel else None
        results.append({
            "disc_id": str(disc.id),
            "content_hash": disc.content_hash,
            "disc_format": disc.format,
            "disc_name": disc.disc_name,
            "movie_name": movie.name if movie else disc.info_title,
            "release_name": rel.name if rel else None,
            "title_count": len(disc.titles) if hasattr(disc, "titles") else 0,
            "contribution_status": disc.discdb_contribution_status or "not_submitted",
            "contribution_notes": disc.discdb_contribution_notes,
            "exported_at": disc.discdb_exported_at.isoformat() if disc.discdb_exported_at else None,
            "submitted_at": disc.discdb_submitted_at.isoformat() if disc.discdb_submitted_at else None,
        })
    return results


@router.patch("/contributions/{disc_id}")
def update_contribution_status(
    disc_id: str,
    body: ContributionStatusUpdate,
    db: Session = Depends(get_db),
):
    """Update contribution status and/or notes for a disc (#334)."""
    disc = db.query(db_models.Disc).filter(db_models.Disc.id == disc_id).first()
    if not disc:
        raise HTTPException(404, detail="Disc not found")
    valid_statuses = {"not_submitted", "draft", "exported", "submitted", "accepted", "rejected"}
    if body.status is not None:
        if body.status not in valid_statuses:
            raise HTTPException(400, detail=f"Invalid status: {body.status}. Must be one of: {', '.join(sorted(valid_statuses))}")
        disc.discdb_contribution_status = body.status
        if body.status == "submitted" and not disc.discdb_submitted_at:
            disc.discdb_submitted_at = datetime.now(timezone.utc)
        elif body.status == "exported" and not disc.discdb_exported_at:
            disc.discdb_exported_at = datetime.now(timezone.utc)
    if body.notes is not None:
        disc.discdb_contribution_notes = body.notes
    db.commit()
    return {"disc_id": disc_id, "status": disc.discdb_contribution_status}


@router.get("/contributions/{disc_id}/bundle")
def get_contribution_bundle(
    disc_id: str,
    db: Session = Depends(get_db),
):
    """
    Generate a DiscDB-format export bundle for a disc (#334).
    Wraps existing discdb_finalize.py logic.
    """
    disc = db.query(db_models.Disc).filter(db_models.Disc.id == disc_id).first()
    if not disc:
        raise HTTPException(404, detail="Disc not found")
    # Find a completed job for this disc
    job = (
        db.query(db_models.Job)
        .filter(
            db_models.Job.disc_id == disc_id,
            db_models.Job.rip_state == "completed",
        )
        .order_by(db_models.Job.created_at.desc())
        .first()
    )
    if not job:
        raise HTTPException(400, detail="No completed rip job found for this disc")
    try:
        from core.discdb_finalize import generate_discdb_bundle
        bundle = generate_discdb_bundle(str(job.id), db)
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("Failed to generate contribution bundle for disc %s: %s", disc_id, exc)
        raise HTTPException(500, detail=str(exc))
    # Track the export so the contributions list can distinguish "never
    # exported" from "exported but not yet submitted upstream" (#86).
    if (disc.discdb_contribution_status or "not_submitted") in ("not_submitted", "draft"):
        disc.discdb_contribution_status = "exported"
    disc.discdb_exported_at = datetime.now(timezone.utc)
    db.commit()
    return bundle
