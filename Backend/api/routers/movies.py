"""Movie router for managing movies."""
import logging
import shutil
from pathlib import Path
from typing import List, Optional
import requests

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from api import database, crud
from api import models as db_models
from api.schemas import (
    MovieSummary,
    MovieRecord,
    MovieCreate,
    MovieUpdate,
    MovieLookupRequest,
    TmdbSearchRequest,
    TmdbSearchResponse,
    TmdbSearchCandidate,
    TmdbEpisodeSummary,
    TmdbSeasonEpisodesResponse,
)
from core.tmdb_scraper import fetch_tmdb_metadata_for_id, parse_tmdb_url
from core import settings as app_settings
from core import tmdb_client


def _invalidate_options() -> None:
    """Invalidate cached workflow options after movie mutation."""
    try:
        from api.routers.discs import invalidate_options_cache
        invalidate_options_cache()
    except Exception:
        pass  # Best-effort; don't fail the request if cache invalidation fails
from core.utils import get_mkvauto_data

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/movies", tags=["movies"])


def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _movie_summary(movie) -> MovieSummary:
    return MovieSummary(
        id=movie.id,
        name=movie.name,
        production_year=movie.production_year,
        tmdb_id=movie.tmdb_id,
        tmdb_type=movie.tmdb_type,
        cover_url=movie.cover_url,
        cover_path=movie.cover_path,
    )


def _movie_record(movie) -> MovieRecord:
    return MovieRecord(
        id=movie.id,
        name=movie.name,
        production_year=movie.production_year,
        tmdb_id=movie.tmdb_id,
        tmdb_type=movie.tmdb_type,
        cover_url=movie.cover_url,
        cover_path=movie.cover_path,
        created_at=movie.created_at,
        updated_at=movie.updated_at,
    )


@router.get("", response_model=List[MovieSummary])
def list_movies(db: Session = Depends(get_db)):
    """List all movies."""
    movies = db.query(db_models.Movie).order_by(db_models.Movie.name).all()
    return [_movie_summary(m) for m in movies]


@router.get("/search", response_model=List[MovieSummary])
def search_movies(
    q: str = Query("", description="Search term (min 3 chars)"),
    limit: int = Query(20, le=50, description="Max results"),
    db: Session = Depends(get_db),
):
    """Search movies by name. For combobox search-as-you-type (≥3 chars)."""
    if len(q) < 3:
        return []
    query = (
        db.query(db_models.Movie)
        .filter(db_models.Movie.name.ilike(f"%{q}%"))
        .order_by(db_models.Movie.name)
        .limit(limit)
    )
    return [_movie_summary(m) for m in query.all()]


@router.get("/{movie_id}", response_model=MovieRecord)
def get_movie(movie_id: str, db: Session = Depends(get_db)):
    """Get movie by ID."""
    movie = db.query(db_models.Movie).filter(db_models.Movie.id == movie_id).first()
    if not movie:
        raise HTTPException(404, detail="Movie not found")
    return _movie_record(movie)


@router.post("", response_model=MovieRecord)
def create_movie(movie_data: MovieCreate, db: Session = Depends(get_db)):
    """Create a new movie."""
    # Check for duplicate tmdb_id
    if movie_data.tmdb_id:
        existing = (
            db.query(db_models.Movie)
            .filter(db_models.Movie.tmdb_id == movie_data.tmdb_id)
            .first()
        )
        if existing:
            raise HTTPException(400, detail=f"Movie with TMDB ID {movie_data.tmdb_id} already exists")
    
    movie = db_models.Movie(
        name=movie_data.name,
        production_year=movie_data.production_year,
        tmdb_id=movie_data.tmdb_id,
        tmdb_type=movie_data.tmdb_type,
        cover_url=movie_data.cover_url,
    )
    db.add(movie)
    db.commit()
    db.refresh(movie)
    _invalidate_options()
    return _movie_record(movie)


@router.patch("/{movie_id}", response_model=MovieRecord)
def update_movie(movie_id: str, movie_data: MovieUpdate, db: Session = Depends(get_db)):
    """Update a movie."""
    movie = db.query(db_models.Movie).filter(db_models.Movie.id == movie_id).first()
    if not movie:
        raise HTTPException(404, detail="Movie not found")
    
    # Check for duplicate tmdb_id if updating
    if movie_data.tmdb_id and movie_data.tmdb_id != movie.tmdb_id:
        existing = (
            db.query(db_models.Movie)
            .filter(db_models.Movie.tmdb_id == movie_data.tmdb_id)
            .filter(db_models.Movie.id != movie_id)
            .first()
        )
        if existing:
            raise HTTPException(400, detail=f"Movie with TMDB ID {movie_data.tmdb_id} already exists")
    
    if movie_data.name is not None:
        movie.name = movie_data.name
    if movie_data.production_year is not None:
        movie.production_year = movie_data.production_year
    if movie_data.tmdb_id is not None:
        movie.tmdb_id = movie_data.tmdb_id
    if movie_data.tmdb_type is not None:
        movie.tmdb_type = movie_data.tmdb_type
    if movie_data.cover_url is not None:
        movie.cover_url = movie_data.cover_url
    if movie_data.cover_path is not None:
        movie.cover_path = movie_data.cover_path
    
    db.commit()
    db.refresh(movie)
    _invalidate_options()
    return _movie_record(movie)


@router.post("/tmdb-search", response_model=TmdbSearchResponse)
def tmdb_search(request: TmdbSearchRequest):
    """Fuzzy search TMDB by title text (#387, part of epic #386).

    Distinct from /movies/search (DB autocomplete) and /movies/lookup (URL paste).
    Used by the film-step suggestion UX (#389) to let the user pick a different
    candidate when the auto-suggestion (#388) is wrong.

    Errors:
      503 {"code": "tmdb_unavailable"}      key missing or devmode disabled
      503 {"code": "tmdb_network_error"}    network/API failure
    """
    if app_settings.get_tmdb_disabled() or not app_settings.get_tmdb_api_key():
        raise HTTPException(
            status_code=503,
            detail={"code": "tmdb_unavailable", "reason": "TMDB key not configured or disabled"},
        )
    query = (request.query or "").strip()
    if not query:
        return TmdbSearchResponse(candidates=[], normalized_query="", hints={})

    normalized, hints = tmdb_client.normalize_title(query)
    if not normalized:
        return TmdbSearchResponse(candidates=[], normalized_query="", hints=hints)

    try:
        candidates = tmdb_client.search_title(
            normalized,
            year_hint=request.year_hint,
            media_type=request.media_type,
            limit=max(1, min(int(request.limit or 3), 10)),
        )
    except tmdb_client.TmdbConfigError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "tmdb_unavailable", "reason": str(exc)},
        ) from exc
    except tmdb_client.TmdbNetworkError as exc:
        logger.warning("TMDB search failed for query=%r: %s", normalized, exc)
        raise HTTPException(
            status_code=503,
            detail={"code": "tmdb_network_error", "reason": str(exc)},
        ) from exc

    return TmdbSearchResponse(
        candidates=[
            TmdbSearchCandidate(
                tmdb_id=c.tmdb_id,
                tmdb_type=c.tmdb_type,
                title=c.title,
                year=c.year,
                cover_url=c.cover_url,
                score=c.score,
            )
            for c in candidates
        ],
        normalized_query=normalized,
        hints=hints,
    )


@router.get(
    "/{tmdb_id}/seasons/{season_number}/episodes",
    response_model=TmdbSeasonEpisodesResponse,
)
def tmdb_tv_season_episodes(tmdb_id: str, season_number: int):
    """TMDB TV episode catalog for one season (#368, part of epic #367).

    Used by the title-label step (#371) to populate an episode dropdown so
    the user picks season/episode/name instead of typing them. The film
    step (#370) prefetches this when the user selects a Series-typed
    candidate so the data is ready when they reach titles.

    Caching: in-process LRU per (tmdb_id, season_number) for the worker
    lifetime (see ``core.tmdb_client._get_tv_season_episodes_cached``).
    TMDB episode metadata changes rarely, so a process-scoped cache is
    enough; there's no DB persistence and no TTL.

    Errors:
      503 {"code": "tmdb_unavailable"}      key missing or devmode disabled
      503 {"code": "tmdb_network_error"}    network/API failure
      404 {"code": "tmdb_not_found"}        unknown tv id or season number
    """
    if app_settings.get_tmdb_disabled() or not app_settings.get_tmdb_api_key():
        raise HTTPException(
            status_code=503,
            detail={"code": "tmdb_unavailable", "reason": "TMDB key not configured or disabled"},
        )
    tid = (tmdb_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="tmdb_id is required")
    if season_number < 0:
        raise HTTPException(status_code=400, detail="season_number must be non-negative")

    try:
        episodes = tmdb_client.get_tv_season_episodes(tid, season_number)
    except tmdb_client.TmdbConfigError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "tmdb_unavailable", "reason": str(exc)},
        ) from exc
    except tmdb_client.TmdbNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "tmdb_not_found", "reason": str(exc)},
        ) from exc
    except tmdb_client.TmdbNetworkError as exc:
        logger.warning(
            "TMDB episode lookup failed for tv=%s season=%s: %s",
            tid, season_number, exc,
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "tmdb_network_error", "reason": str(exc)},
        ) from exc

    # Fold in /tv/{id} for number_of_seasons + series_name (#371 needs both
    # to render the disc-card primary-season selector). Errors from this
    # call share the same _http_get contract — a 404 here is exotic
    # (season list found but show details missing), so degrade gracefully.
    number_of_seasons = 1
    series_name: Optional[str] = None
    try:
        details = tmdb_client.get_tv_details(tid)
        if details is not None:
            number_of_seasons = details.number_of_seasons
            series_name = details.name or None
    except tmdb_client.TmdbNotFoundError:
        pass  # season payload succeeded; show-details miss is non-fatal
    except tmdb_client.TmdbNetworkError as exc:
        logger.warning(
            "TMDB tv details lookup failed for tv=%s (season payload still returned): %s",
            tid, exc,
        )

    return TmdbSeasonEpisodesResponse(
        tmdb_id=tid,
        season_number=season_number,
        episodes=[
            TmdbEpisodeSummary(
                season_number=ep.season_number,
                episode_number=ep.episode_number,
                name=ep.name,
                overview=ep.overview,
                air_date=ep.air_date,
                runtime=ep.runtime,
                still_url=ep.still_url,
            )
            for ep in episodes
        ],
        number_of_seasons=number_of_seasons,
        series_name=series_name,
    )


@router.post("/lookup", response_model=MovieCreate)
def lookup_movie(request: MovieLookupRequest, db: Session = Depends(get_db)):
    """
    Lookup movie information from TMDB URL.
    Scrapes the TMDB page to extract movie name, production year, and cover image.
    """
    # Validate TMDB URL format
    if not request.tmdb_url or not isinstance(request.tmdb_url, str):
        raise HTTPException(400, detail="TMDB URL is required")
    
    url = request.tmdb_url.strip()
    if not (url.startswith("https://www.themoviedb.org/") or url.startswith("https://themoviedb.org/")):
        raise HTTPException(400, detail="TMDB URL must be an HTTPS address from themoviedb.org")
    
    try:
        # Parse URL to get type and ID
        parsed = parse_tmdb_url(request.tmdb_url)
        tmdb_type = parsed["type"]
        tmdb_id = parsed["id"]
        
        # Check if movie already exists
        existing = (
            db.query(db_models.Movie)
            .filter(db_models.Movie.tmdb_id == tmdb_id)
            .first()
        )
        if existing:
            return MovieCreate(
                name=existing.name,
                production_year=existing.production_year,
                tmdb_id=existing.tmdb_id,
                tmdb_type=existing.tmdb_type,
                cover_url=existing.cover_url,
            )

        scraped_data = fetch_tmdb_metadata_for_id(tmdb_id, tmdb_type)
        if not scraped_data:
            raise HTTPException(
                status_code=500,
                detail="Failed to lookup movie from TMDB (scrape failed or empty title)",
            )

        return MovieCreate(
            name=scraped_data["name"],
            production_year=scraped_data["production_year"],
            tmdb_id=scraped_data["tmdb_id"],
            tmdb_type=scraped_data["tmdb_type"],
            cover_url=scraped_data["cover_url"],
        )
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Failed to lookup movie from TMDB: {e}", exc_info=True)
        raise HTTPException(500, detail=f"Failed to lookup movie: {e}") from e


@router.post("/{movie_id}/download-cover")
def download_cover(movie_id: str, job_id: Optional[str] = None, db: Session = Depends(get_db)):
    """
    Download cover image for a movie.
    If job_id is provided, saves to job folder. Otherwise saves to data directory.
    """
    movie = db.query(db_models.Movie).filter(db_models.Movie.id == movie_id).first()
    if not movie:
        raise HTTPException(404, detail="Movie not found")
    
    if not movie.cover_url:
        raise HTTPException(400, detail="Movie has no cover URL")
    
    try:
        # Determine save location
        if job_id:
            from core.job_paths import JobPaths
            from core.utils import resolve_jobs_root
            jobs_root = resolve_jobs_root(None)
            job_paths = JobPaths(jobs_root, job_id)
            job_paths.ensure_layout()
            save_dir = job_paths.root
            filename = "movie_cover.jpg"
        else:
            save_dir = get_mkvauto_data() / "movies"
            save_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{movie_id}_cover.jpg"
        
        # Download image
        response = requests.get(movie.cover_url, timeout=10)
        response.raise_for_status()
        
        target_path = save_dir / filename
        with open(target_path, "wb") as fh:
            fh.write(response.content)
        
        # Update movie record
        movie.cover_path = str(target_path)
        db.commit()
        db.refresh(movie)
        
        return {
            "cover_path": str(target_path),
            "cover_url": movie.cover_url,
        }
    except requests.RequestException as e:
        logger.error(f"Failed to download cover for movie {movie_id}: {e}")
        raise HTTPException(500, detail=f"Failed to download cover: {e}") from e
    except Exception as e:
        logger.error(f"Error downloading cover for movie {movie_id}: {e}", exc_info=True)
        raise HTTPException(500, detail=f"Error downloading cover: {e}") from e

