import json
import logging
import shutil
from pathlib import Path
from typing import Dict, Any, List
from datetime import datetime, timezone

import requests
from fastapi import HTTPException
from core.importbuddy_prefill import parse_copy_log
from core.title_type_normalize import normalize_title_type_for_api
from core.utils import get_export_root

logger = logging.getLogger(__name__)

# MakeMKV field codes (aligned with ImportBuddy LogParser)
TINFO_CHAPTERS = 8
TINFO_LENGTH = 9
TINFO_DISPLAY_SIZE = 10
TINFO_SIZE = 11
TINFO_PLAYLIST = 16
TINFO_SEGMENT_MAP = 26
TINFO_COMMENT = 27
TINFO_JAVA_COMMENT = 49
TINFO_SOURCE_TITLE = 24  # DVDs

SINFO_TYPE = 1
SINFO_NAME = 7
SINFO_AUDIO_TYPE = 2
SINFO_LANG_CODE = 3
SINFO_LANG = 4
SINFO_RESOLUTION = 19
SINFO_ASPECT = 20


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError:
            return {}


def _save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def _normalize_slug(raw: str | None, fallback: str) -> str:
    if raw:
        return raw.strip().replace(" ", "-").lower()
    return fallback

def _safe_copy(src: Path, dest: Path) -> bool:
    """
    Best-effort copy that creates parents and ignores errors; returns True when copied.
    In devmode, just touches the destination file instead of copying.
    """
    from core.utils import is_dev_mode
    try:
        if src.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            # In devmode, just create placeholder instead of copying
            if is_dev_mode():
                pass
            else:
                shutil.copy2(src, dest)
            return True
    except Exception:
        pass
    return False


def _download_file(url: str | None, dest: Path, timeout: int = 10) -> bool:
    """
    Download a URL to dest; returns True on success. Ignores errors.
    """
    if not url or not url.lower().startswith(("http://", "https://")):
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        return True
    except Exception:
        return False


def _disc_path(base_dir: Path, disc_number: int | None = None, slug: str | None = None) -> Path:
    if disc_number is not None:
        return base_dir / f"disc{disc_number:02d}.json"
    if slug and slug.lower().startswith("disc"):
        return base_dir / f"{slug}.json"
    existing = sorted(base_dir.glob("disc*.json"))
    if existing:
        return existing[0]
    return base_dir / "disc01.json"


def _to_release_json(label: Dict[str, Any], release_slug: str) -> Dict[str, Any]:
    return {
        "Slug": release_slug,
        "Title": label.get("release_name") or release_slug,
        "Format": label.get("disc_format"),
        "Upc": label.get("upc"),
        "Asin": label.get("asin"),
        "TmdbId": label.get("tmdb_id"),
        "Year": label.get("release_year"),
        "Locale": label.get("locale"),
        "RegionCode": label.get("region_code"),
        "SortTitle": label.get("sort_title") or label.get("release_name"),
        "ImageUrl": label.get("image_url"),
        "ReleaseDate": label.get("release_date"),
        "DateAdded": datetime.now(timezone.utc).isoformat(),
    }

def _film_dir(
    movie_name: str | None = None,
    boxset_name: str | None = None,
    series_name: str | None = None,
    rel_type: str | None = None,
    production_year: int | None = None,
) -> Path:
    """
    Generate film directory path using movie/boxset/series name.
    For movies, includes production year in format: <name> (<year>).
    
    Args:
        movie_name: Movie name (for movies)
        boxset_name: Boxset name (for boxsets)
        series_name: Series name (for series)
        rel_type: Release type ("movie", "boxset", "series")
        production_year: Production year (only used for movies)
    
    Returns:
        Path to film directory: export/<type>/<name>/ or export/<type>/<name> (<year>)/ for movies
    """
    # Determine name based on type
    name = None
    if boxset_name:
        name = boxset_name
    elif movie_name:
        name = movie_name
    elif series_name:
        name = series_name
    
    # Fallback to "Unnamed" if no name provided
    name = (name or "").strip() or "Unnamed"
    safe = name.replace("/", "-").replace("\\", "-")
    
    # For movies, append production year if available
    safe_type = (rel_type or "movie").strip().lower() if rel_type else "movie"
    if not safe_type or safe_type not in ("movie", "boxset", "series", "tv"):
        safe_type = "movie"
    
    # Only append year for movies, not boxsets or series
    if safe_type == "movie" and production_year:
        safe = f"{safe} ({production_year})"
    
    return get_export_root() / safe_type / safe

def _generate_sort_title(title: str) -> str:
    """
    Generate sort title by removing leading articles (The, A, An).
    
    Args:
        title: Original title
        
    Returns:
        Sort title with articles removed
    """
    if not title:
        return ""
    
    title = title.strip()
    # Remove leading articles (case-insensitive)
    articles = ["the ", "a ", "an "]
    for article in articles:
        if title.lower().startswith(article):
            return title[len(article):].strip()
    
    return title


def _write_film_metadata(base_dir: Path, film_dir: Path, label_payload: Dict[str, Any]) -> None:
    film_dir.mkdir(parents=True, exist_ok=True)
    
    # Copy any existing metadata blobs from the job directory.
    copied_tmdb = _safe_copy(base_dir / "tmdb.json", film_dir / "tmdb.json")
    copied_meta = _safe_copy(base_dir / "metadata.json", film_dir / "metadata.json")
    copied_imdb = _safe_copy(base_dir / "imdb.json", film_dir / "imdb.json")

    # Load TMDB data if available
    tmdb_data = None
    if copied_tmdb:
        try:
            import json
            tmdb_data = json.loads((film_dir / "tmdb.json").read_text(encoding="utf-8"))
        except Exception:
            pass
    
    # Try to scrape TMDB data if we have a TMDB ID but no complete data
    if not tmdb_data or not tmdb_data.get("complete"):
        tmdb_id = label_payload.get("tmdb_id")
        tmdb_type = label_payload.get("tmdb_type") or label_payload.get("group_type") or "movie"
        if tmdb_id and tmdb_type in ("movie", "tv"):
            try:
                from core.tmdb_scraper import scrape_tmdb_page, scrape_tmdb_cast_page
                
                # Scrape main page
                main_data = scrape_tmdb_page(tmdb_type, str(tmdb_id))
                
                # Scrape cast page for directors, writers, stars
                cast_data = scrape_tmdb_cast_page(tmdb_type, str(tmdb_id))
                
                # Merge data
                tmdb_data = {
                    "id": tmdb_id,
                    "type": tmdb_type,
                    "complete": True,
                    **main_data,
                    **cast_data,
                }
                
                # Save scraped data
                _save_json(film_dir / "tmdb.json", tmdb_data)
            except Exception as e:
                logger.warning(f"Failed to scrape TMDB data: {e}")
                if not tmdb_data:
                    tmdb_data = {"complete": False}
    
    # Seed minimal TMDB metadata when missing
    if not tmdb_data:
        if label_payload.get("tmdb_id") is not None:
            tmdb_data = {
                "id": label_payload.get("tmdb_id"),
                "title": label_payload.get("release_name") or label_payload.get("disc_group"),
                "slug": label_payload.get("release_slug") or label_payload.get("disc_group"),
                "complete": False,
            }
            _save_json(film_dir / "tmdb.json", tmdb_data)
        else:
            _save_json(film_dir / "tmdb.json", {"complete": False})
    
    # Generate metadata.json in the correct format
    if not copied_meta:
        # Extract data from various sources
        title = (
            tmdb_data.get("name") or
            label_payload.get("movie_name") or
            label_payload.get("release_name") or
            label_payload.get("disc_group") or
            "Unknown"
        )
        
        year = (
            tmdb_data.get("production_year") or
            label_payload.get("production_year") or
            label_payload.get("release_year") or
            label_payload.get("original_year")
        )
        
        full_title = f"{title} ({year})" if year else title
        sort_title = _generate_sort_title(title)
        
        # Determine type
        rel_type = label_payload.get("group_type") or label_payload.get("type") or "movie"
        type_str = "Movie"
        if rel_type == "series" or rel_type == "tv":
            type_str = "Series"
        # Boxset is no longer a release type - it's a relationship
        
        # Generate slug
        slug = (
            label_payload.get("release_slug") or
            label_payload.get("disc_group") or
            title.lower().replace(" ", "-").replace("/", "-").replace("\\", "-")
        )
        
        # Image URL (relative path)
        image_url = f"{type_str}/{slug}/cover.jpg"
        
        # External IDs
        external_ids = {}
        if tmdb_data.get("id"):
            external_ids["Tmdb"] = str(tmdb_data["id"])
        if tmdb_data.get("imdb_id") or label_payload.get("imdb_id"):
            external_ids["Imdb"] = tmdb_data.get("imdb_id") or label_payload.get("imdb_id")
        
        # Build metadata dict
        metadata = {
            "Title": title,
            "FullTitle": full_title,
            "SortTitle": sort_title,
            "Slug": slug,
            "Type": type_str,
            "Year": year,
            "ImageUrl": image_url,
            "ExternalIds": external_ids if external_ids else {},
            "DateAdded": datetime.now(timezone.utc).isoformat(),
        }
        
        # Add optional fields from TMDB data
        if tmdb_data.get("plot"):
            metadata["Plot"] = tmdb_data["plot"]
        elif label_payload.get("plot"):
            metadata["Plot"] = label_payload["plot"]
        
        if tmdb_data.get("tagline"):
            metadata["Tagline"] = tmdb_data["tagline"]
        elif label_payload.get("tagline"):
            metadata["Tagline"] = label_payload["tagline"]
        
        if tmdb_data.get("directors"):
            metadata["Directors"] = ", ".join(tmdb_data["directors"])
        elif label_payload.get("directors"):
            if isinstance(label_payload["directors"], list):
                metadata["Directors"] = ", ".join(label_payload["directors"])
            else:
                metadata["Directors"] = str(label_payload["directors"])
        
        if tmdb_data.get("writers"):
            metadata["Writers"] = ", ".join(tmdb_data["writers"])
        elif label_payload.get("writers"):
            if isinstance(label_payload["writers"], list):
                metadata["Writers"] = ", ".join(label_payload["writers"])
            else:
                metadata["Writers"] = str(label_payload["writers"])
        
        if tmdb_data.get("stars"):
            metadata["Stars"] = ", ".join(tmdb_data["stars"])
        elif label_payload.get("stars"):
            if isinstance(label_payload["stars"], list):
                metadata["Stars"] = ", ".join(label_payload["stars"])
            else:
                metadata["Stars"] = str(label_payload["stars"])
        
        if tmdb_data.get("genres"):
            metadata["Genres"] = ", ".join(tmdb_data["genres"])
        elif label_payload.get("genres"):
            if isinstance(label_payload["genres"], list):
                metadata["Genres"] = ", ".join(label_payload["genres"])
            else:
                metadata["Genres"] = str(label_payload["genres"])
        
        if tmdb_data.get("runtime_minutes"):
            metadata["RuntimeMinutes"] = tmdb_data["runtime_minutes"]
        elif label_payload.get("runtime_minutes"):
            metadata["RuntimeMinutes"] = label_payload["runtime_minutes"]
        
        if tmdb_data.get("runtime"):
            metadata["Runtime"] = tmdb_data["runtime"]
        elif label_payload.get("runtime"):
            metadata["Runtime"] = label_payload["runtime"]
        
        if tmdb_data.get("content_rating"):
            metadata["ContentRating"] = tmdb_data["content_rating"]
        elif label_payload.get("content_rating"):
            metadata["ContentRating"] = label_payload["content_rating"]
        
        # Release date
        if label_payload.get("release_date"):
            metadata["ReleaseDate"] = label_payload["release_date"]
        elif label_payload.get("production_year"):
            # Create a basic release date from year
            metadata["ReleaseDate"] = f"{label_payload['production_year']}-01-01T00:00:00+00:00"
        
        _save_json(film_dir / "metadata.json", metadata)
    
    # Download cover for movies/series
    film_cover = film_dir / "cover.jpg"
    if not film_cover.exists():
        cover_url = None
        
        # Try to get cover URL from various sources
        if tmdb_data and tmdb_data.get("cover_url"):
            cover_url = tmdb_data["cover_url"]
        elif label_payload.get("movie") and label_payload["movie"].get("cover_url"):
            cover_url = label_payload["movie"]["cover_url"]
        elif label_payload.get("cover_url"):
            cover_url = label_payload["cover_url"]
        elif label_payload.get("cover_front_url"):
            cover_url = label_payload["cover_front_url"]
        
        if cover_url:
            try:
                _download_file(cover_url, film_cover)
            except Exception as e:
                logger.warning(f"Failed to download cover image: {e}")
    
    # Handle IMDB data
    if not copied_imdb:
        imdb_payload = {"complete": False}
        if tmdb_data and tmdb_data.get("imdb_id"):
            imdb_payload["id"] = tmdb_data["imdb_id"]
            imdb_payload["title"] = title
            imdb_payload["complete"] = True
        elif label_payload.get("imdb_id"):
            imdb_payload["id"] = label_payload["imdb_id"]
            imdb_payload["title"] = label_payload.get("release_name") or label_payload.get("disc_group")
        _save_json(film_dir / "imdb.json", imdb_payload)


def _format_duration(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        total = int(round(raw))
    else:
        try:
            # already formatted (e.g., "1:56:30")
            if isinstance(raw, str) and ":" in raw and all(part.isdigit() for part in raw.replace(":", " ").split()):
                return raw
            total = int(float(raw))
        except Exception:
            return str(raw)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def _format_size(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    try:
        size_bytes = float(raw)
        if size_bytes >= 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
        if size_bytes >= 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        return f"{size_bytes:.0f} bytes"
    except Exception:
        return str(raw)


def _resolution_from_format(fmt: str | None) -> str | None:
    if not fmt:
        return None
    f = fmt.lower()
    if "uhd" in f or "4k" in f:
        return "2160p"
    if "blu" in f:
        return "1080p"
    if "dvd" in f:
        return "480p"
    return None


def _build_summary_filename(item_title: str | None, base_name: str, disc_format: str | None, year: int | None) -> str:
    # For the main movie include year + resolution; for extras just use the title.
    resolution = _resolution_from_format(disc_format) or "1080p"
    safe_title = (item_title or base_name).replace(":", "-").replace("/", "-").replace("\\", "-").strip()
    if not safe_title:
        safe_title = base_name
    if base_name.lower().startswith("disc"):
        # extras/trailers
        return f"{safe_title}.mkv"
    # main movie naming with year/resolution if available
    suffix_year = f" ({year})" if year else ""
    return f"{safe_title}{suffix_year} [{resolution}].mkv"


def _render_disc_summary(
    disc_json: Dict[str, Any],
    disc_number: int | None,
    label_payload: Dict[str, Any],
) -> str:
    titles = disc_json.get("Titles") or []
    base_name = f"disc{(disc_number or 1):02d}"
    lines: list[str] = []
    disc_format = label_payload.get("disc_format")
    # Filter out ignored titles from summary - only include non-ignored titles
    for t in titles:
        item = t.get("Item") or {}
        type_val = item.get("Type") or (t.get("Comment") and "Extra") or None
        # Skip titles marked as ignore in the summary
        if type_val and str(type_val).lower() == "ignore":
            continue
        title = item.get("Title") or t.get("Comment") or t.get("SourceFile") or base_name
        source = (
            t.get("SourceFile")
            or item.get("SourceFile")
            or t.get("Comment")  # sometimes stored in Comment for legacy payloads
            or ""
        )
        duration = _format_duration(t.get("Duration"))
        chapters = item.get("Chapters") or []
        chapter_count = t.get("ChapterCount")
        if chapter_count is None:
            if isinstance(chapters, dict):
                chapter_count = chapters.get("count")
            elif isinstance(chapters, list):
                chapter_count = len(chapters)
        size = _format_size(t.get("Size") or t.get("DisplaySize"))
        seg_map_raw = t.get("SegmentMap")
        if isinstance(seg_map_raw, list):
            segment_map = ",".join([str(x) for x in seg_map_raw])
        else:
            segment_map = str(seg_map_raw) if seg_map_raw is not None else ""
        seg_count = 0
        if segment_map:
            from core.segment_reorder import parse_segment_map_tokens
            seg_count = len(parse_segment_map_tokens(segment_map))
        type_val = item.get("Type") or (t.get("Comment") and "Extra") or None
        description = t.get("Description")
        resolution_name = _resolution_from_format(disc_format)

        lines.append(f"Name: {title}")
        if source:
            lines.append(f"Source file name: {source}")
        if duration:
            lines.append(f"Duration: {duration}")
        if chapter_count:
            lines.append(f"Chapters count: {chapter_count}")
        if size:
            lines.append(f"Size: {size}")
        if seg_count:
            lines.append(f"Segment count: {seg_count}")
        if segment_map:
            lines.append(f"Segment map: {segment_map}")
        if type_val:
            lines.append(f"Type: {type_val}")
        if description:
            lines.append(f"Description: {description}")

        # Do not list chapter entries; only count is shown above.

        # Year is intentionally omitted from summary now; pass None to avoid suffix.
        file_name = _build_summary_filename(title, base_name, disc_format, None)
        if resolution_name and " [" not in file_name and type_val and type_val.lower() == "mainmovie":
            file_name = _build_summary_filename(title, base_name, disc_format, None)
        lines.append(f"File name: {file_name}")
        lines.append("")  # blank line between items

    return "\n".join(lines).rstrip() + "\n"


def _write_disc_summary(
    release_dir: Path,
    disc_json: Dict[str, Any],
    disc_number: int | None,
    label_payload: Dict[str, Any],
) -> Path:
    base_name = f"disc{(disc_number or 1):02d}"
    summary_path = release_dir / f"{base_name}-summary.txt"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        _render_disc_summary(disc_json, disc_number, label_payload), encoding="utf-8"
    )
    return summary_path


def _to_disc_json(
    label: Dict[str, Any],
    disc_hash: str | None,
    disc_number: int | None,
    disc_slug: str | None,
    parsed_log: Dict[str, Any] | None,
) -> Dict[str, Any]:
    titles: List[Dict[str, Any]] = []

    parsed_titles = parsed_log.get("titles") if parsed_log else []
    # build lookup from parsed titles
    parsed_by_track: Dict[str, Dict[str, Any]] = {}
    parsed_by_index: Dict[int, Dict[str, Any]] = {}
    for t in parsed_titles:
        key = t.get("source_file") or t.get("title_id") or t.get("track_id")
        if key:
            parsed_by_track[key] = t
        if t.get("index") is not None:
            try:
                parsed_by_index[int(t["index"])] = t
            except Exception:
                pass

    # Only use explicit titles; do not treat tracks as titles.
    label_tracks = label.get("titles") or []
    matched_indices: set[int] = set()
    matched_keys: set[str] = set()

    def _normalize_type(raw: str | None) -> str | None:
        return normalize_title_type_for_api(raw)

    def _normalize_tracks(tracks: Any) -> List[Dict[str, Any]]:
        norm: List[Dict[str, Any]] = []
        if isinstance(tracks, dict):
            tracks = tracks.values()
        for t in tracks or []:
            if not isinstance(t, dict):
                continue
            norm.append(
                {
                    "Index": t.get("index"),
                    "Name": t.get("name") or t.get("codec_short") or t.get("codec_hint") or t.get("codec"),
                    "Type": (t.get("type") or "").capitalize() if t.get("type") else None,
                    "AudioType": t.get("audio_type"),
                    "LanguageCode": t.get("language_code"),
                    "Language": t.get("language"),
                    "Resolution": t.get("resolution"),
                    "AspectRatio": t.get("aspect_ratio"),
                }
            )
        return norm

    def _merge_chapters(label_chapters, parsed_chapters, chapter_count):
        """
        Build a chapter list with Index/Title.
        - Prefer explicit chapter dicts from label (Index/Title).
        - Accept label chapter titles as plain strings (auto-indexed).
        - Fallback to parsed chapters; then placeholder names when count known.
        """
        chapters: List[Dict[str, Any]] = []
        if isinstance(label_chapters, list) and label_chapters:
            # Normalize label chapters
            for i, ch in enumerate(label_chapters, start=1):
                if isinstance(ch, dict):
                    idx = ch.get("Index") or ch.get("index") or ch.get("number") or i
                    title = ch.get("Title") or ch.get("title") or ch.get("name")
                else:
                    idx = i
                    title = str(ch)
                chapters.append({"Index": idx, "Title": title or f"Chapter {idx}"})
        elif isinstance(parsed_chapters, list) and parsed_chapters:
            chapters = parsed_chapters
        elif chapter_count:
            # Accept chapter_count as number or dict with count
            if isinstance(chapter_count, dict):
                chapter_count = chapter_count.get("count")
            try:
                cnt = int(chapter_count)
                chapters = [{"Index": i, "Title": f"Chapter {i}"} for i in range(1, cnt + 1)]
            except Exception:
                chapters = []
        return chapters

    # Merge label tracks onto parsed skeleton when possible
    # Include ALL titles in discNN.json, even those marked as ignore
    for idx, t in enumerate(label_tracks):
        if not isinstance(t, dict):
            continue
        t_type_raw = (t.get("type") or "").lower()
        # Do not skip ignored titles - they should be in discNN.json
        track_key = t.get("title_id") or t.get("track_id") or t.get("source_file")
        parsed = parsed_by_track.get(track_key)
        if not parsed and t.get("index") is not None:
            parsed = parsed_by_index.get(int(t["index"]))
        merged = {**parsed} if parsed else {}
        merged.update(t)  # label wins on conflicting keys

        tracks = merged.get("streams") or merged.get("tracks") or (parsed.get("tracks") if parsed else []) or []
        # Determine chapter count (allow dict payload with count)
        chapter_count_val = merged.get("chapter_count")
        if chapter_count_val is None and isinstance(merged.get("chapters"), dict):
            chapter_count_val = merged.get("chapters", {}).get("count")
        if chapter_count_val is None and parsed and isinstance(parsed.get("chapters"), dict):
            chapter_count_val = parsed.get("chapters", {}).get("count")
        if isinstance(chapter_count_val, dict):
            chapter_count_val = chapter_count_val.get("count")
        chapters = _merge_chapters(
            merged.get("chapters"),
            parsed.get("chapters") if parsed else [],
            chapter_count_val or (parsed.get("chapter_count") if parsed else None),
        )
        size_val = merged.get("size") or (parsed.get("size") if parsed else None)
        try:
            size_val = int(size_val)
        except Exception:
            pass
        display_size = merged.get("display_size") or (parsed.get("display_size") if parsed else None) or _format_size(size_val)
        duration = _format_duration(merged.get("duration") or (parsed.get("duration") if parsed else None))
        seg_raw = merged.get("segment_map") or (parsed.get("segment_map") if parsed else None)
        if isinstance(seg_raw, list):
            segment_map = ",".join([str(x) for x in seg_raw])
        else:
            segment_map = seg_raw
        type_val = _normalize_type(merged.get("type"))
        item_title = merged.get("title") or merged.get("comment") or merged.get("output_file") or merged.get("source_file")
        base_name = merged.get("comment") or merged.get("output_file") or f"disc{(disc_number or 1):02d}"
        comment_val = merged.get("output_file") or merged.get("comment") or _build_summary_filename(item_title, base_name, label.get("disc_format"), label.get("release_year") or label.get("original_year"))

        titles.append(
            {
                "Index": merged.get("index", idx),
                "Comment": comment_val,
                "SourceFile": merged.get("source_file") or merged.get("track_id") or merged.get("title_id") or (parsed.get("source_file") if parsed else None),
                "SegmentMap": segment_map,
                "Duration": duration,
                "Size": size_val,
                "DisplaySize": display_size,
                "Description": merged.get("description"),
                "ChapterCount": chapter_count_val,
                "Item": {
                    "Title": item_title,
                    "Type": type_val,
                    "Season": merged.get("season"),
                    "Episode": merged.get("episode"),
                    "Chapters": chapters,
                },
                "Tracks": _normalize_tracks(tracks),
                "Content": merged.get("content", True),
            }
        )
        if parsed and parsed.get("index") is not None:
            matched_indices.add(int(parsed["index"]))
        if track_key:
            matched_keys.add(track_key)

    # Add any parsed titles that were not labeled
    for t in parsed_titles:
        idx = t.get("index")
        key = t.get("source_file") or t.get("title_id") or t.get("track_id")
        if (idx is not None and int(idx) in matched_indices) or (key and key in matched_keys):
            continue
        chapter_count_val = t.get("chapter_count")
        if isinstance(chapter_count_val, dict):
            chapter_count_val = chapter_count_val.get("count")
        chapters = _merge_chapters(
            t.get("chapters"),
            t.get("chapters"),
            chapter_count_val,
        )
        size_val = t.get("size")
        try:
            size_val = int(size_val)
        except Exception:
            pass
        titles.append(
            {
                "Index": idx,
                "SourceFile": t.get("source_file") or key,
                "SegmentMap": t.get("segment_map"),
                "Duration": _format_duration(t.get("duration")),
                "Size": size_val,
                "DisplaySize": t.get("display_size") or _format_size(size_val),
                "Description": t.get("description"),
                "ChapterCount": chapter_count_val,
                "Item": {
                    "Title": t.get("title"),
                    "Type": _normalize_type(t.get("type")),
                    "Season": t.get("season"),
                    "Episode": t.get("episode"),
                    "Chapters": chapters,
                },
                "Tracks": _normalize_tracks(t.get("tracks") or []),
                "Content": True,
            }
        )

    # Ensure deterministic ordering
    titles = sorted(titles, key=lambda t: t.get("Index") if t.get("Index") is not None else 0)
    # Normalize track ordering inside each title
    for t in titles:
        tracks = t.get("Tracks")
        if isinstance(tracks, list):
            t["Tracks"] = sorted(
                tracks,
                key=lambda tr: tr.get("Index") if tr.get("Index") is not None else 0,
            )
    disc_name = label.get("disc_name") or (parsed_log or {}).get("disc_label") or f"Disc {disc_number or 1}"
    disc_json: Dict[str, Any] = {
        "Index": disc_number or 1,
        "Slug": disc_slug or f"disc{disc_number or 1:02d}",
        "Name": disc_name,
        "Format": label.get("disc_format"),
        "ContentHash": disc_hash,
        "Mode": label.get("mode"),
        "Titles": titles,
    }
    return disc_json


def finalize_from_label(
    base_dir: Path,
    label_payload: Dict[str, Any],
    disc_hash: str | None = None,
    final_paths: Dict[str, str] | None = None,
    final_hashes: Dict[str, str] | None = None,
    release_type: str | None = None,
    release_name: str | None = None,
    release_slug_override: str | None = None,
    write_release_artifacts: bool = True,
    write_film_metadata: bool = True,
) -> Dict[str, Any]:
    """
    Write release.json and discNN.json in a layout compatible with ImportBuddy outputs.
    Files are written under ${MKVAUTO_DATA_DIR||MKVAUTO_DATA||MAKEMKV_DATA_DIR||MKVAUTO_ROOT}/export/<Type>/<Release Name>/<release-slug>.
    """
    if not base_dir.exists():
        raise HTTPException(404, detail="Base directory not found for finalize")

    release_slug = _normalize_slug(
        release_slug_override or label_payload.get("release_slug") or label_payload.get("disc_group"),
        "release",
    )
    disc_slug = _normalize_slug(label_payload.get("disc_slug"), None)
    disc_number = label_payload.get("disc_number")
    rel_type = (release_type or label_payload.get("group_type") or label_payload.get("type") or "movie").strip().lower() or "movie"
    
    # Extract movie/boxset/series name from label_payload
    movie_name = label_payload.get("movie_name") or (label_payload.get("movie") and label_payload["movie"].get("name"))
    boxset_name = label_payload.get("boxset_name") or (label_payload.get("boxset") and label_payload["boxset"].get("name"))
    series_name = label_payload.get("series_name") or (label_payload.get("series") and label_payload["series"].get("name"))
    
    # Fallback to release_name if no specific name found
    if not movie_name and not boxset_name and not series_name:
        raw_rel_name = release_name or label_payload.get("release_name") or release_slug
        if rel_type in ("series", "tv"):
            series_name = (raw_rel_name or release_slug).replace("/", "-").replace("\\", "-")
        else:
            movie_name = (raw_rel_name or release_slug).replace("/", "-").replace("\\", "-")
    
    # Extract production year for movies
    production_year = None
    if rel_type == "movie":
        # Try to get production year from label_payload
        production_year = label_payload.get("production_year")
        if not production_year and label_payload.get("movie") and isinstance(label_payload["movie"], dict):
            production_year = label_payload["movie"].get("production_year")
        # Fallback to original_year if production_year not available
        if not production_year:
            production_year = label_payload.get("original_year")

    film_dir = _film_dir(
        movie_name=movie_name,
        boxset_name=boxset_name,
        series_name=series_name,
        rel_type=rel_type,
        production_year=production_year,
    )
    release_dir = film_dir / release_slug
    release_dir.mkdir(parents=True, exist_ok=True)
    if write_film_metadata:
        _write_film_metadata(base_dir, film_dir, label_payload)

    if write_release_artifacts:
        release_json = _to_release_json(label_payload, release_slug)
        _save_json(release_dir / "release.json", release_json)

    disc_json_path = _disc_path(release_dir, disc_number=disc_number, slug=disc_slug)
    # Prefer info log; if missing, fail (progress log is not sufficient for finalize metadata)
    info_log_path = base_dir / "makemkv_info.log"
    if not info_log_path.exists():
        raise HTTPException(404, detail="makemkv_info.log not found; run info scan before finalize")
    parsed_log = parse_copy_log(info_log_path)
    disc_json = _to_disc_json(label_payload, disc_hash, disc_number, disc_slug, parsed_log)
    _save_json(disc_json_path, disc_json)

    # Persist logs alongside the disc for parity with DiscDB exports.
    base_name = f"disc{(disc_number or 1):02d}"
    summary_dest = release_dir / f"{base_name}-summary.txt"
    _write_disc_summary(release_dir, disc_json, disc_number, label_payload)
    progress_log = base_dir / "makemkv_info.log"
    progress_dest = release_dir / f"{base_name}.txt"
    if progress_log.exists():
        _safe_copy(progress_log, progress_dest)
    else:
        # Create a stub log if we only have the info log so downstream scripts still find the file.
        if not progress_dest.exists():
            progress_dest.write_text("No makemkv.log available; finalize generated this placeholder.\n", encoding="utf-8")

    if write_release_artifacts:
        # label_payload.json is no longer copied - discNN.json already contains this data

        # Copy covers if present at root; keep best-effort
        for cover_name in ("front.jpg", "back.jpg"):
            root_cover = base_dir / cover_name
            dest = release_dir / cover_name
            try:
                if root_cover.exists() and not dest.exists():
                    # In devmode, just create placeholder instead of copying
                    from core.utils import is_dev_mode
                    if is_dev_mode():
                        pass
                    else:
                        shutil.copy2(root_cover, dest)
                if not dest.exists() and cover_name == "front.jpg":
                    # Fallback to film cover or download from release cover URL
                    if not _safe_copy(film_dir / "cover.jpg", dest):
                        _download_file(label_payload.get("cover_front_url"), dest)
                if not dest.exists() and cover_name == "back.jpg":
                    _download_file(label_payload.get("cover_back_url"), dest)
            except Exception:
                pass

    return {
        "release_dir": str(release_dir),
        "release_json": str(release_dir / "release.json"),
        "disc_json": str(disc_json_path),
        "metadata_dir": str(release_dir),
    }


def generate_disc_files(
    base_dir: Path,
    target_dir: Path,
    label_payload: Dict[str, Any],
    disc_hash: str | None = None,
) -> Dict[str, Path]:
    """
    Generate disc-specific files (discNN.json, discNN-summary.txt, discNN.txt) directly in target_dir.
    This is used for disc finalization to write files directly to the job's finalize folder.
    """
    if not base_dir.exists():
        raise HTTPException(404, detail="Base directory not found for finalize")
    
    target_dir.mkdir(parents=True, exist_ok=True)
    
    disc_slug = _normalize_slug(label_payload.get("disc_slug"), None)
    disc_number = label_payload.get("disc_number")
    
    # Prefer info log; if missing, fail (progress log is not sufficient for finalize metadata)
    info_log_path = base_dir / "makemkv_info.log"
    if not info_log_path.exists():
        raise HTTPException(404, detail="makemkv_info.log not found; run info scan before finalize")
    
    parsed_log = parse_copy_log(info_log_path)
    disc_json = _to_disc_json(label_payload, disc_hash, disc_number, disc_slug, parsed_log)
    
    # Generate disc files
    base_name = f"disc{(disc_number or 1):02d}"
    disc_json_path = target_dir / f"{base_name}.json"
    _save_json(disc_json_path, disc_json)
    
    summary_path = target_dir / f"{base_name}-summary.txt"
    _write_disc_summary(target_dir, disc_json, disc_number, label_payload)
    
    log_path = target_dir / f"{base_name}.txt"
    if info_log_path.exists():
        _safe_copy(info_log_path, log_path)
    else:
        # Create a stub log if we only have the info log so downstream scripts still find the file.
        if not log_path.exists():
            log_path.write_text("No makemkv.log available; finalize generated this placeholder.\n", encoding="utf-8")
    
    return {
        "disc_json": disc_json_path,
        "summary": summary_path,
        "log": log_path,
    }


def finalize_boxset(
    boxset: Any,
    releases: List[Any],
    db: Any,
) -> Dict[str, Any]:
    """
    Finalize a boxset by generating boxset.json.
    Validates all releases are finalized and all movies have production_year.
    """
    from core.utils import slugify
    from api import models as db_models
    
    # Validate all releases are finalized
    not_finalized = [r.slug for r in releases if not r.finalized]
    if not_finalized:
        raise HTTPException(400, detail=f"Releases not finalized: {', '.join(not_finalized)}")
    
    # Validate all movies have production_year
    missing_prod_year = []
    for release in releases:
        movie = release.movie
        if not movie or not movie.production_year:
            missing_prod_year.append(release.slug)
    if missing_prod_year:
        raise HTTPException(400, detail=f"Movies missing production_year for releases: {', '.join(missing_prod_year)}")
    
    # Build Discs array (ordered by release creation date)
    discs_array = []
    for idx, release in enumerate(releases, start=1):
        movie = release.movie
        if not movie:
            continue
        
        # Generate TitleSlug: movie-name-slugified-production-year
        movie_slug = slugify(movie.name)
        title_slug = f"{movie_slug}-{movie.production_year}" if movie.production_year else None
        
        # Get disc format (from first disc)
        disc_format = None
        if release.discs:
            disc_format = release.discs[0].format or "Blu-ray"
        
        discs_array.append({
            "Index": idx,
            "Name": movie.name,
            "Format": disc_format or "Blu-ray",
            "Slug": movie_slug,
            "TitleSlug": title_slug,
            "ReleaseSlug": release.slug,
        })
    
    # Build boxset.json
    boxset_name = boxset.name or boxset.title or boxset.slug
    boxset_folder_name = f"{boxset_name}{(' (' + str(boxset.year) + ')') if boxset.year else ''}"
    
    boxset_json = {
        "Slug": boxset.slug,
        "Asin": boxset.asin,
        "Upc": boxset.upc,
        "Year": boxset.year,
        "Locale": boxset.locale or "en-us",
        "RegionCode": boxset.region_code or "1",
        "Title": boxset.title or boxset.name or boxset.slug,
        "SortTitle": boxset.sort_title or boxset.title or boxset.name or boxset.slug,
        "Type": "Movie",
        "ImageUrl": boxset.image_url or f"boxset/{boxset.slug}.jpg",
        "ReleaseDate": boxset.release_date.isoformat() if boxset.release_date else None,
        "DateAdded": datetime.now(timezone.utc).isoformat(),
        "Discs": discs_array,
    }
    
    # Write to export directory
    export_root = get_export_root()
    sets_dir = export_root / "sets"
    boxset_dir = sets_dir / boxset_folder_name
    boxset_dir.mkdir(parents=True, exist_ok=True)
    
    boxset_json_path = boxset_dir / "boxset.json"
    _save_json(boxset_json_path, boxset_json)
    
    # Copy cover images
    if boxset.cover_front_url:
        front_dest = boxset_dir / "front.jpg"
        _download_file(boxset.cover_front_url, front_dest)
    
    if boxset.cover_back_url:
        back_dest = boxset_dir / "back.jpg"
        _download_file(boxset.cover_back_url, back_dest)
    
    return {
        "boxset_dir": str(boxset_dir),
        "boxset_json": str(boxset_json_path),
        "metadata_dir": str(boxset_dir),
    }


def build_label_payload_from_disc(disc: Any, release: Any) -> Dict[str, Any]:
    """Build a label payload (the finalize/export input shape) from persisted
    disc + release rows, so titles/tracks reflect the DB rather than whatever
    was last posted from a client."""
    stream_map: Dict[str, list[dict[str, Any]]] = {}
    for tr in getattr(disc, "tracks", []) or []:
        key = getattr(tr, "title_id", None)
        if not key:
            continue
        streams = stream_map.setdefault(str(key), [])
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
    titles: list[dict[str, Any]] = []
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
        "disc_slug": disc.disc_slug,
        "disc_name": disc.disc_name,
        "disc_number": disc.disc_number,
        "disc_format": disc.format,
        "titles": titles,
    }
    movie_name = None
    boxset_name = None
    movie = getattr(release, "movie", None)
    boxset = getattr(release, "boxset", None)
    if movie:
        movie_name = movie.name
    elif boxset and boxset.name:
        boxset_name = boxset.name
    payload.update(
        {
            "release_slug": release.slug,
            "release_name": release.name,
            "release_year": getattr(release, "release_year", None),
            "production_year": getattr(release, "production_year", None),
            "original_year": getattr(release, "original_year", None),
            "tmdb_id": movie.tmdb_id if movie else None,
            "tmdb_type": (movie.tmdb_type if movie else None) or release.type,
            "movie_name": movie_name,
            "boxset_name": boxset_name,
            "upc": release.upc,
            "asin": release.asin,
            "cover_front_url": release.cover_front_url,
            "cover_back_url": release.cover_back_url,
            "group_type": release.type,
        }
    )
    return payload


def generate_discdb_bundle(job_id: str, db: Any) -> Dict[str, Any]:
    """Build an in-memory TheDiscDB-shaped export bundle for a job's disc.

    Returns the same JSON shapes finalize writes to the export tree
    (release.json / discNN.json / discNN-summary.txt) without touching the
    filesystem, so the API can hand the bundle straight to the client for
    manual contribution (#86)."""
    from api import models as db_models
    from core.job_paths import JobPaths

    job = db.query(db_models.Job).filter(db_models.Job.id == job_id).first()
    if not job:
        raise HTTPException(404, detail="Job not found")
    disc = db.query(db_models.Disc).filter(db_models.Disc.id == job.disc_id).first()
    if not disc:
        raise HTTPException(404, detail="Disc not found for job")
    release = getattr(disc, "release", None)
    if release is None:
        raise HTTPException(
            400, detail="Disc is not linked to a release; label the disc before exporting"
        )

    label_payload = build_label_payload_from_disc(disc, release)
    if (release.type or "").strip().lower() in ("series", "tv") and not label_payload.get("movie_name"):
        label_payload.setdefault("series_name", release.name)

    # The MakeMKV info log enriches titles with parsed chapters/streams when the
    # job artifacts still exist; after cleanup we degrade to DB-only data.
    parsed_log = None
    paths = JobPaths.for_id(str(job.id))
    for cand_dir in (paths.raw, paths.metadata):
        cand = cand_dir / "makemkv_info.log"
        if cand.exists():
            try:
                parsed_log = parse_copy_log(cand)
            except Exception as exc:
                logger.warning("bundle: failed to parse %s: %s", cand, exc)
            break

    release_slug = _normalize_slug(release.slug or label_payload.get("release_slug"), "release")
    disc_slug = _normalize_slug(label_payload.get("disc_slug"), None)
    disc_json = _to_disc_json(
        label_payload, disc.content_hash, disc.disc_number, disc_slug, parsed_log
    )
    release_json = _to_release_json(label_payload, release_slug)
    summary_text = _render_disc_summary(disc_json, disc.disc_number, label_payload)

    return {
        "schema": "thediscdb-bundle/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "disc_id": str(disc.id),
        "content_hash": disc.content_hash,
        "disc_number": disc.disc_number,
        "release_slug": release_slug,
        "release": release_json,
        "disc": disc_json,
        "summary": summary_text,
        "info_log_included": parsed_log is not None,
    }
