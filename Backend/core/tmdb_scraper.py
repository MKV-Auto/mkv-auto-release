"""TMDB page scraping utilities."""
import re
import logging
import html
from typing import Dict, Optional, List
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def parse_tmdb_url(url: str) -> Dict[str, Optional[str]]:
    """
    Parse TMDB URL to extract type and ID.
    
    Examples:
        https://www.themoviedb.org/tv/66732-stranger-things -> {"type": "tv", "id": "66732"}
        https://www.themoviedb.org/movie/414906 -> {"type": "movie", "id": "414906"}
    
    Args:
        url: TMDB URL
        
    Returns:
        Dict with "type" ("tv" or "movie") and "id" (TMDB ID as string)
    """
    if not url:
        raise ValueError("URL cannot be empty")
    
    # Match pattern: /tv/ or /movie/ followed by digits
    pattern = r'/(tv|movie)/(\d+)'
    match = re.search(pattern, url)
    
    if not match:
        raise ValueError(f"Invalid TMDB URL format: {url}")
    
    tmdb_type = match.group(1)
    tmdb_id = match.group(2)
    
    return {"type": tmdb_type, "id": tmdb_id}


def normalize_tmdb_id_str(tmdb_id: str | int | None) -> str | None:
    """Coerce TMDB id to a non-empty string for DB and URLs."""
    if tmdb_id is None:
        return None
    s = str(tmdb_id).strip()
    return s if s else None


def normalize_tmdb_type_for_scrape(
    tmdb_type: str | None = None,
    *,
    media_type: str | None = None,
    group_type: str | None = None,
    title_type: str | None = None,
) -> str:
    """
    Map DiscDB / payload type hints to 'movie' or 'tv' for scrape_tmdb_page URLs.
    Aligns with parse_discdb_data fallback: movie when DiscDB media is Movie, else tv.
    """
    raw = (str(tmdb_type).strip().lower() if tmdb_type is not None else "") or ""
    if raw in ("movie", "movies"):
        return "movie"
    if raw in ("tv", "series"):
        return "tv"
    gt = (str(group_type).strip().lower() if group_type else "") or ""
    if gt in ("series", "tv"):
        return "tv"
    tt = (str(title_type).strip().lower() if title_type else "") or ""
    if tt in ("series", "tv"):
        return "tv"
    mt = str(media_type).strip() if media_type else ""
    if mt.lower() == "movie" or mt == "Movie":
        return "movie"
    if mt:
        return "tv"
    return "movie"


def fetch_tmdb_metadata_for_id(
    tmdb_id: str | int | None,
    tmdb_type: str | None = None,
    *,
    media_type: str | None = None,
    group_type: str | None = None,
    title_type: str | None = None,
) -> Dict[str, str | int | None] | None:
    """
    Scrape TMDB by id for canonical title, year, and poster. Returns None on failure
    (network, parse, or empty title) so callers can fall back to other sources.
    """
    tid = normalize_tmdb_id_str(tmdb_id)
    if not tid:
        return None
    inferred_media = media_type or title_type
    ttype = normalize_tmdb_type_for_scrape(
        tmdb_type,
        media_type=inferred_media,
        group_type=group_type,
        title_type=title_type,
    )
    try:
        scraped = scrape_tmdb_page(ttype, tid)
    except Exception as exc:
        logger.warning("TMDB scrape failed for %s/%s: %s", ttype, tid, exc)
        return None
    name = scraped.get("name")
    if not name or not str(name).strip():
        logger.warning("TMDB scrape returned empty title for %s/%s", ttype, tid)
        return None
    return {
        "name": str(name).strip(),
        "production_year": scraped.get("production_year"),
        "cover_url": scraped.get("cover_url"),
        "tmdb_type": ttype,
        "tmdb_id": tid,
    }


def _parse_runtime_to_minutes(runtime_str: str) -> Optional[int]:
    """
    Parse runtime string like "2h 13m" or "133m" to minutes.
    
    Args:
        runtime_str: Runtime string (e.g., "2h 13m", "133m", "2h")
        
    Returns:
        Total minutes as integer, or None if parsing fails
    """
    if not runtime_str:
        return None
    
    runtime_str = runtime_str.strip().lower()
    total_minutes = 0
    
    # Match hours: "2h" or "2 h"
    hour_match = re.search(r'(\d+)\s*h', runtime_str)
    if hour_match:
        total_minutes += int(hour_match.group(1)) * 60
    
    # Match minutes: "13m" or "13 m"
    minute_match = re.search(r'(\d+)\s*m', runtime_str)
    if minute_match:
        total_minutes += int(minute_match.group(1))
    
    return total_minutes if total_minutes > 0 else None


def scrape_tmdb_page(tmdb_type: str, tmdb_id: str) -> Dict[str, Optional[str | int | List[str]]]:
    """
    Scrape TMDB page to extract film information.
    
    Args:
        tmdb_type: "tv" or "movie"
        tmdb_id: TMDB ID as string
        
    Returns:
        Dict with:
            - name: Film name
            - production_year: Production year as integer (or None)
            - cover_url: Poster image URL (largest from srcset)
            - genres: List of genre names
            - runtime: Runtime string (e.g., "2h 13m")
            - runtime_minutes: Runtime in minutes (int)
            - tagline: Tagline text
            - plot: Plot/overview text
            - content_rating: Content rating (e.g., "PG-13")
            - imdb_id: IMDB ID if available
    """
    url = f"https://www.themoviedb.org/{tmdb_type}/{tmdb_id}"
    
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, "html.parser")
        
        # Extract title from <title> tag
        # Format: "Movie Name (Year) — The Movie Database (TMDB)" or "Movie Name (Year) | TMDB"
        title_tag = soup.find("title")
        if not title_tag:
            raise ValueError("Could not find title tag on TMDB page")
        
        # Get raw title text - BeautifulSoup's get_text() decodes most entities, but we'll also use html.unescape for safety
        title_text = title_tag.get_text().strip()
        # Explicitly decode HTML entities like &#39; to '
        title_text = html.unescape(title_text)
        
        # Remove " | TMDB" suffix if present
        title_text = title_text.replace(" | TMDB", "").strip()
        
        # Drop everything after "—" (em dash) if present
        # Format: "Now You See Me: Now You Don't (2025) — The Movie Database (TMDB)"
        if "—" in title_text:
            title_text = title_text.split("—")[0].strip()
        
        # Parse name and year from title
        # TV series title format: "Show Name (TV Series 2022- )" or "Show Name (TV Series 2022)"
        # Movie title format: "Movie Name (2022)"
        name = title_text
        production_year = None

        if tmdb_type == "tv":
            # Match "(TV Series YYYY- )" or "(TV Series YYYY)" at end; use show name only and first_air year
            tv_match = re.search(r"^(.+?)\s*\(TV Series\s+(\d{4})(?:\s*-\s*)?\)\s*$", title_text)
            if tv_match:
                name = tv_match.group(1).strip()
                production_year = int(tv_match.group(2))

        if production_year is None:
            # Match pattern: find (####) in the string (movies or fallback)
            year_match = re.search(r"^(.+?)\s*\((\d{4})\)", title_text)
            if year_match:
                name = year_match.group(1).strip()
                production_year = int(year_match.group(2))
            else:
                name = title_text.strip()
        
        # Extract cover image from element with class "poster w-full"
        cover_url = None
        poster_elem = soup.find(class_=re.compile(r'poster.*w-full|w-full.*poster'))
        if not poster_elem:
            # Try alternative selectors
            poster_elem = soup.find("img", class_=re.compile(r'poster'))
        
        if poster_elem:
            # Try srcset first (contains multiple sizes)
            srcset = poster_elem.get("srcset") or poster_elem.get("data-srcset")
            if srcset:
                # Parse srcset: "url1 1x, url2 2x, url3 3x" or "url1 300w, url2 600w"
                # Extract all URLs and sizes
                urls = []
                for item in srcset.split(","):
                    item = item.strip()
                    parts = item.rsplit(None, 1)  # Split on last whitespace
                    if len(parts) == 2:
                        url_part, size_part = parts
                        # Extract width if present (e.g., "300w" -> 300)
                        width_match = re.search(r'(\d+)w', size_part)
                        if width_match:
                            width = int(width_match.group(1))
                            urls.append((url_part.strip(), width))
                        # Handle x descriptors (e.g., "2x")
                        elif size_part.endswith("x"):
                            multiplier = float(size_part[:-1])
                            urls.append((url_part.strip(), int(1000 * multiplier)))  # Approximate
                
                if urls:
                    # Sort by width (descending) and take largest
                    urls.sort(key=lambda x: x[1], reverse=True)
                    cover_url = urls[0][0]
            
            # Fallback to src attribute
            if not cover_url:
                cover_url = poster_elem.get("src") or poster_elem.get("data-src")
            
            # Ensure full URL
            if cover_url and not cover_url.startswith("http"):
                if cover_url.startswith("//"):
                    cover_url = "https:" + cover_url
                elif cover_url.startswith("/"):
                    cover_url = "https://www.themoviedb.org" + cover_url
        
        if not cover_url:
            logger.warning(f"Could not find cover image on TMDB page: {url}")
        
        # Extract genres
        genres: List[str] = []
        genres_elem = soup.find("span", class_="genres")
        if genres_elem:
            genre_links = genres_elem.find_all("a")
            for link in genre_links:
                genre_text = link.get_text().strip()
                if genre_text:
                    genres.append(genre_text)
        
        # Extract runtime
        runtime_str = None
        runtime_minutes = None
        runtime_elem = soup.find("span", class_="runtime")
        if runtime_elem:
            runtime_str = runtime_elem.get_text().strip()
            runtime_minutes = _parse_runtime_to_minutes(runtime_str)
        
        # Extract tagline
        tagline = None
        tagline_elem = soup.find("h3", class_="tagline")
        if tagline_elem:
            tagline = tagline_elem.get_text().strip()
        
        # Extract plot/overview
        plot = None
        overview_elem = soup.find("div", class_="overview")
        if overview_elem:
            plot_para = overview_elem.find("p")
            if plot_para:
                plot = plot_para.get_text().strip()
            else:
                plot = overview_elem.get_text().strip()
        
        # Extract content rating/certification
        content_rating = None
        cert_elem = soup.find("span", class_="certification")
        if cert_elem:
            content_rating = cert_elem.get_text().strip()
        
        # Extract IMDB ID - look for links or meta tags
        imdb_id = None
        # Try to find IMDB link
        imdb_link = soup.find("a", href=re.compile(r'imdb\.com/title/(tt\d+)'))
        if imdb_link:
            imdb_match = re.search(r'imdb\.com/title/(tt\d+)', imdb_link.get("href", ""))
            if imdb_match:
                imdb_id = imdb_match.group(1)
        # Try meta tags
        if not imdb_id:
            meta_imdb = soup.find("meta", property=re.compile(r'imdb', re.I))
            if meta_imdb:
                content = meta_imdb.get("content", "")
                imdb_match = re.search(r'tt\d+', content)
                if imdb_match:
                    imdb_id = imdb_match.group(0)
        
        return {
            "name": name,
            "production_year": production_year,
            "cover_url": cover_url,
            "genres": genres,
            "runtime": runtime_str,
            "runtime_minutes": runtime_minutes,
            "tagline": tagline,
            "plot": plot,
            "content_rating": content_rating,
            "imdb_id": imdb_id,
        }
        
    except requests.RequestException as e:
        logger.error(f"Failed to fetch TMDB page {url}: {e}")
        raise ValueError(f"Failed to fetch TMDB page: {e}") from e
    except Exception as e:
        logger.error(f"Error scraping TMDB page {url}: {e}")
        raise ValueError(f"Error scraping TMDB page: {e}") from e


def scrape_tmdb_cast_page(tmdb_type: str, tmdb_id: str) -> Dict[str, List[str]]:
    """
    Scrape TMDB cast page to extract directors, writers, and stars.
    
    Args:
        tmdb_type: "tv" or "movie"
        tmdb_id: TMDB ID as string
        
    Returns:
        Dict with:
            - directors: List of director names
            - writers: List of writer names
            - stars: List of top 3 cast member names
    """
    url = f"https://www.themoviedb.org/{tmdb_type}/{tmdb_id}/cast"
    
    directors: List[str] = []
    writers: List[str] = []
    stars: List[str] = []
    
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, "html.parser")
        
        # Find all crew_wrapper divs
        crew_wrappers = soup.find_all("div", class_="crew_wrapper")
        
        # Find "Directing" section - it's an h4 inside a crew_wrapper div
        for wrapper in crew_wrappers:
            h4 = wrapper.find("h4")
            if h4 and re.search(r"Directing", h4.get_text(), re.I):
                # Find all person links in this crew section
                person_links = wrapper.find_all("a", href=re.compile(r'/person/\d+'))
                for link in person_links:
                    name = link.get_text().strip()
                    # Only get Director role (not Assistant Directors)
                    role_elem = link.find_parent("li")
                    if role_elem:
                        role_text = role_elem.get_text()
                        if "Director" in role_text and "Assistant" not in role_text:
                            if name and name not in directors:
                                directors.append(name)
                break
        
        # Find "Writing" section - it's an h4 inside a crew_wrapper div
        for wrapper in crew_wrappers:
            h4 = wrapper.find("h4")
            if h4 and re.search(r"Writing", h4.get_text(), re.I):
                # Find all person links in this crew section
                person_links = wrapper.find_all("a", href=re.compile(r'/person/\d+'))
                for link in person_links:
                    name = link.get_text().strip()
                    # Get writers (Screenplay, Novel, etc.)
                    role_elem = link.find_parent("li")
                    if role_elem:
                        role_text = role_elem.get_text()
                        if any(keyword in role_text for keyword in ["Screenplay", "Novel", "Writer", "Writing"]):
                            if name and name not in writers:
                                writers.append(name)
                break
        
        # Find "Cast" section - it's an h3 with "Cast" text followed by an ol with class "people credits" (no "crew")
        cast_section = soup.find("h3", text=re.compile(r"Cast", re.I))
        if not cast_section:
            # Try finding h3 that contains "Cast" in its text
            for h3 in soup.find_all("h3"):
                if h3.get_text() and re.search(r"Cast", h3.get_text(), re.I):
                    cast_section = h3
                    break
        
        if cast_section:
            # Find the next ol with class containing "people" and "credits" but not "crew"
            # The ol might have class="people credits " (with trailing space)
            cast_list = None
            # Try next sibling first
            for sibling in cast_section.next_siblings:
                if hasattr(sibling, 'name') and sibling.name == 'ol':
                    classes = sibling.get('class', [])
                    # Normalize classes (handle trailing spaces)
                    classes_normalized = [c.strip() for c in classes] if classes else []
                    if 'people' in classes_normalized and 'credits' in classes_normalized and 'crew' not in classes_normalized:
                        cast_list = sibling
                        break
            
            # If not found, try finding it in the parent section
            if not cast_list:
                parent_section = cast_section.find_parent("section")
                if parent_section:
                    for ol in parent_section.find_all("ol"):
                        classes = ol.get('class', [])
                        # Normalize classes (handle trailing spaces)
                        classes_normalized = [c.strip() for c in classes] if classes else []
                        # Check if it has "people" and "credits" but NOT "crew"
                        if 'people' in classes_normalized and 'credits' in classes_normalized and 'crew' not in classes_normalized:
                            cast_list = ol
                            break
            
            if cast_list:
                # Find all person links in the cast section
                person_links = cast_list.find_all("a", href=re.compile(r'/person/\d+'))
                # Get top 3, but skip if they're in directors/writers
                count = 0
                for link in person_links:
                    if count >= 3:
                        break
                    name = link.get_text().strip()
                    if name and name not in stars and name not in directors and name not in writers:
                        stars.append(name)
                        count += 1
        
        return {
            "directors": directors,
            "writers": writers,
            "stars": stars,
        }
        
    except requests.RequestException as e:
        logger.warning(f"Failed to fetch TMDB cast page {url}: {e}")
        return {"directors": [], "writers": [], "stars": []}
    except Exception as e:
        logger.warning(f"Error scraping TMDB cast page {url}: {e}")
        return {"directors": [], "writers": [], "stars": []}

