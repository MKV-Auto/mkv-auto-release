"""TMDB v3 API client for search-by-title (#387) and episode catalog (#368).

Distinct from `core/tmdb_scraper.py`, which scrapes the public HTML pages
when the user pastes a TMDB URL. This module talks to the JSON API and
requires an API key (set via `core.settings.set_tmdb_api_key`).

Public surface:
    normalize_title(raw)  -> (normalized_query, hints)
    search_title(query, *, year_hint, media_type, limit) -> list[TmdbCandidate]
    get_tv_season_episodes(tmdb_id, season_number) -> list[TmdbEpisode]

Errors:
    TmdbConfigError      key missing
    TmdbNotFoundError    TMDB returned 404 (e.g. unknown tv id or season)
    TmdbNetworkError     network/API failure (HTTP error, timeout)
    TmdbError            base class for the above
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, asdict
from functools import lru_cache
from typing import Any, Dict, List, Literal, Optional, Tuple

import requests

from core import settings

logger = logging.getLogger(__name__)

_TMDB_BASE = "https://api.themoviedb.org/3"
_TMDB_IMG_BASE = "https://image.tmdb.org/t/p/w500"
_HTTP_TIMEOUT_SECONDS = 10


class TmdbError(Exception):
    """Base class for TMDB client errors."""


class TmdbConfigError(TmdbError):
    """Raised when the TMDB API key is missing or empty."""


class TmdbNotFoundError(TmdbError):
    """Raised when TMDB returns 404 — e.g. unknown tv id or season number.
    Separate from generic network errors so route handlers can return 404
    instead of 503 in that specific case."""


class TmdbNetworkError(TmdbError):
    """Raised when the TMDB API returns an error or the network call fails."""


@dataclass(frozen=True)
class TmdbCandidate:
    tmdb_id: str
    tmdb_type: Literal["movie", "tv"]
    title: str
    year: Optional[int]
    cover_url: Optional[str]
    score: float  # 0.0–1.0; combination of title-overlap + popularity + year proximity

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --- Normalization ---------------------------------------------------------

# Spelled-out English numerals 1–20. Sufficient for seasons (any series
# beyond season 20 is vanishingly rare in disc-rip data) and overlaps with
# disc-number cases like "Disc Three".
_WORD_TO_INT: Dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}
_WORD_NUMERAL_RE = "(?:" + "|".join(_WORD_TO_INT.keys()) + ")"


# Compiled in priority order. Each pattern, if it fires, strips the matched
# substring and emits a hint via the named group ``value`` (digits) or ``word``
# (English numeral). Order matters: long forms before short forms (Season 3
# before S3) so a token like "Season 3" isn't half-stripped as "S3 ".
_NORMALIZE_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    # "Season 3" → digits
    ("season", re.compile(r"\bseason\s+(?P<value>\d+)\b", re.IGNORECASE)),
    # "Season Two" → spelled-out numeral (real-world: "Fallout Season Two Disc 1")
    ("season", re.compile(r"\bseason\s+(?P<word>" + _WORD_NUMERAL_RE + r")\b", re.IGNORECASE)),
    # "S3"
    ("season", re.compile(r"(?<![a-z])s(?P<value>\d+)\b", re.IGNORECASE)),
    # "Disc 2"
    ("disc_num", re.compile(r"\bdisc\s+(?P<value>\d+)\b", re.IGNORECASE)),
    # "Disc Two"
    ("disc_num", re.compile(r"\bdisc\s+(?P<word>" + _WORD_NUMERAL_RE + r")\b", re.IGNORECASE)),
    # "D2"
    ("disc_num", re.compile(r"(?<![a-z])d(?P<value>\d+)\b", re.IGNORECASE)),
]


def _hint_value_from_match(m: "re.Match[str]") -> Optional[int]:
    """Extract an int hint from a regex match: digits via group ``value`` or
    English numeral via group ``word``. Returns None if neither parses."""
    groups = m.groupdict()
    if groups.get("value"):
        try:
            return int(groups["value"])
        except (ValueError, TypeError):
            return None
    word = (groups.get("word") or "").lower()
    return _WORD_TO_INT.get(word)

# Trailing-edition tokens stripped from the query (case-insensitive).
# Stored as edition hint so downstream callers can preserve the user's
# preference (UCE, Bonus Disc, etc.) without polluting the TMDB query.
_EDITION_TOKENS = (
    "ultimate collectors edition",
    "ultimate collector's edition",
    "ultimate edition",
    "bonus disc",
    "bonus",
    "uce",
    "ucr",
    "uhd",
    "bluray",
    "blu-ray",
    "bd",
    "4k",
)

# Punctuation we collapse to whitespace at the start of normalization.
# Apostrophes and ampersands are preserved — TMDB indexes them and removing
# them hurts match quality (e.g. "Dungeons & Dragons", "Sorcerer's Stone").
_PUNCT_TO_SPACE_RE = re.compile(r"[_\-:;,/]+")
_WS_RE = re.compile(r"\s+")


def normalize_title(raw: str) -> Tuple[str, Dict[str, Any]]:
    """Normalize a noisy disc info_title for TMDB search.

    Returns (query, hints). The query is suitable for passing to TMDB
    /search/multi (or /search/movie + /search/tv). Hints capture metadata
    that was stripped from the query so the caller can use it downstream
    (season number, disc number, edition label).

    Empirical observation (see master plan): MakeMKV CINFO-parsed
    info_titles are already cleanly title-cased. The hard "engram-spec"
    case of merged words like STRANGENEWWORLDS_SEASON3 has not been
    observed in actual disc data and is deferred.
    """
    if not raw:
        return "", {}

    text = str(raw)
    hints: Dict[str, Any] = {}

    # Pass 1: punctuation → whitespace (preserves apostrophes/ampersands)
    text = _PUNCT_TO_SPACE_RE.sub(" ", text)

    # Pass 2: extract season/disc hints (strip from query so TMDB doesn't see them)
    for hint_name, pattern in _NORMALIZE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        # Only set the hint if not already captured — long form (Season N) wins.
        if hint_name not in hints:
            v = _hint_value_from_match(m)
            if v is not None:
                hints[hint_name] = v
        text = pattern.sub(" ", text)

    # Pass 3: strip trailing edition tokens (case-insensitive)
    lower = text.lower()
    for token in _EDITION_TOKENS:
        # Match as standalone trailing token, optionally surrounded by whitespace.
        # Match anywhere, but only consume if it sits at the end of the string
        # (post-whitespace-trim) so we don't eat words from inside the title.
        edition_re = re.compile(r"\b" + re.escape(token) + r"\b\s*$", re.IGNORECASE)
        m2 = edition_re.search(lower)
        if not m2:
            continue
        hints.setdefault("edition", token)
        text = text[: m2.start()]
        lower = text.lower()

    # Pass 4: lowercase and collapse whitespace
    text = _WS_RE.sub(" ", text).strip().lower()

    return text, hints


# --- Search ----------------------------------------------------------------

def _api_key() -> str:
    key = settings.get_tmdb_api_key()
    if not key:
        raise TmdbConfigError("TMDB API key is not configured")
    return key


def _http_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """GET request against TMDB v3. Raises TmdbNetworkError on failure."""
    full_params = {"api_key": _api_key(), "language": "en-US", **params}
    url = f"{_TMDB_BASE}{path}"
    try:
        resp = requests.get(url, params=full_params, timeout=_HTTP_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise TmdbNetworkError(f"TMDB request failed: {exc}") from exc
    if resp.status_code == 401:
        raise TmdbConfigError("TMDB rejected the API key (401)")
    if resp.status_code == 404:
        # Distinct error so route handlers can map to HTTP 404 directly
        # instead of the generic 503 for upstream failures.
        raise TmdbNotFoundError(f"TMDB returned 404 for {path}")
    if not resp.ok:
        raise TmdbNetworkError(f"TMDB returned HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise TmdbNetworkError(f"TMDB returned non-JSON body: {exc}") from exc


def _year_from_release_date(date_str: Optional[str]) -> Optional[int]:
    if not date_str or not isinstance(date_str, str):
        return None
    m = re.match(r"^(\d{4})", date_str)
    return int(m.group(1)) if m else None


def _cover_url(poster_path: Optional[str]) -> Optional[str]:
    if not poster_path or not isinstance(poster_path, str):
        return None
    return f"{_TMDB_IMG_BASE}{poster_path}"


def _title_score(query: str, title: str) -> float:
    """Case-insensitive token-set overlap between query and result title.

    Range 0.0 (no overlap) to 1.0 (all query tokens present in title).
    """
    if not query or not title:
        return 0.0
    q_tokens = {t for t in re.findall(r"[a-z0-9']+", query.lower()) if t}
    t_tokens = {t for t in re.findall(r"[a-z0-9']+", title.lower()) if t}
    if not q_tokens:
        return 0.0
    overlap = len(q_tokens & t_tokens)
    return overlap / len(q_tokens)


def _rank_candidate(
    query: str,
    title: str,
    popularity: float,
    year: Optional[int],
    year_hint: Optional[int],
) -> float:
    """Combine title-overlap, popularity, and year proximity into one score.

    Weights chosen to make title-overlap dominant. Year hint adds a small
    boost when within ±1 year of the candidate's year; popularity breaks
    ties between candidates with the same title overlap (e.g. Midway 1976
    vs 2019). Returns 0.0–1.0.
    """
    overlap = _title_score(query, title)
    # Popularity boost: cap at 50 (TMDB's high end) and scale to 0.0–0.15.
    pop_boost = min(max(popularity or 0.0, 0.0), 50.0) / 50.0 * 0.15
    # Year boost: 0.10 for exact match, 0.05 for ±1 year, 0 otherwise.
    year_boost = 0.0
    if year_hint and year:
        diff = abs(year_hint - year)
        if diff == 0:
            year_boost = 0.10
        elif diff == 1:
            year_boost = 0.05
    score = overlap * 0.75 + pop_boost + year_boost
    return min(max(score, 0.0), 1.0)


def _parse_results(
    raw_results: List[Dict[str, Any]],
    *,
    query: str,
    year_hint: Optional[int],
    forced_type: Optional[Literal["movie", "tv"]],
) -> List[TmdbCandidate]:
    out: List[TmdbCandidate] = []
    for r in raw_results:
        if not isinstance(r, dict):
            continue
        media_type = forced_type or r.get("media_type")
        if media_type not in ("movie", "tv"):
            continue
        # /search/multi includes "person" results; skip silently.
        title = r.get("title") if media_type == "movie" else r.get("name")
        if not title:
            continue
        tmdb_id = r.get("id")
        if tmdb_id is None:
            continue
        date_field = "release_date" if media_type == "movie" else "first_air_date"
        year = _year_from_release_date(r.get(date_field))
        popularity = float(r.get("popularity") or 0.0)
        score = _rank_candidate(query, str(title), popularity, year, year_hint)
        out.append(
            TmdbCandidate(
                tmdb_id=str(tmdb_id),
                tmdb_type=media_type,  # type: ignore[arg-type]
                title=str(title),
                year=year,
                cover_url=_cover_url(r.get("poster_path")),
                score=round(score, 4),
            )
        )
    return out


@lru_cache(maxsize=128)
def _search_title_cached(
    query: str,
    year_hint: Optional[int],
    media_type: Optional[str],
    limit: int,
) -> Tuple[TmdbCandidate, ...]:
    """Cached layer over the real network call. Tuple return for hashability."""
    if not query:
        return ()
    if media_type == "movie":
        path = "/search/movie"
        params: Dict[str, Any] = {"query": query, "include_adult": "false"}
        if year_hint:
            params["year"] = year_hint
        data = _http_get(path, params)
        results = _parse_results(
            data.get("results") or [],
            query=query,
            year_hint=year_hint,
            forced_type="movie",
        )
    elif media_type == "tv":
        path = "/search/tv"
        params = {"query": query, "include_adult": "false"}
        if year_hint:
            params["first_air_date_year"] = year_hint
        data = _http_get(path, params)
        results = _parse_results(
            data.get("results") or [],
            query=query,
            year_hint=year_hint,
            forced_type="tv",
        )
    else:
        path = "/search/multi"
        params = {"query": query, "include_adult": "false"}
        data = _http_get(path, params)
        results = _parse_results(
            data.get("results") or [],
            query=query,
            year_hint=year_hint,
            forced_type=None,
        )

    results.sort(key=lambda c: c.score, reverse=True)
    return tuple(results[: max(1, int(limit))])


def search_title(
    query: str,
    *,
    year_hint: Optional[int] = None,
    media_type: Optional[Literal["movie", "tv"]] = None,
    limit: int = 3,
) -> List[TmdbCandidate]:
    """Search TMDB for the given (already-normalized) title.

    The query should typically come from ``normalize_title(disc.info_title)[0]``.
    Caller is responsible for normalization — this function does not re-run it,
    so the cache key matches what's persisted on the disc.

    Raises:
        TmdbConfigError: API key not configured.
        TmdbNetworkError: API call failed or returned a non-OK status.
    """
    results = _search_title_cached(query, year_hint, media_type, int(limit))
    return list(results)


def clear_cache() -> None:
    """Reset the in-process search and episode-catalog caches. Used by tests."""
    _search_title_cached.cache_clear()
    _get_tv_season_episodes_cached.cache_clear()
    _get_tv_details_cached.cache_clear()


# --- TV episode catalog (#368) ----------------------------------------------

@dataclass(frozen=True)
class TmdbEpisode:
    """A single episode from TMDB ``/3/tv/{tv_id}/season/{season_number}``.

    Field set matches what the labeling UI needs to fill in season /
    episode / title fields plus a thumbnail. Empty/missing fields from
    TMDB become ``None`` (or empty string for required name).
    """
    season_number: int
    episode_number: int
    name: str
    overview: Optional[str]
    air_date: Optional[str]   # ISO date string e.g. "2024-04-10"
    runtime: Optional[int]    # minutes; nullable on TMDB even for aired episodes
    still_url: Optional[str]  # full URL (w500 base) when poster_path is set

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _still_url(still_path: Optional[str]) -> Optional[str]:
    if not still_path or not isinstance(still_path, str):
        return None
    return f"{_TMDB_IMG_BASE}{still_path}"


def _parse_episode(raw: Dict[str, Any]) -> Optional[TmdbEpisode]:
    if not isinstance(raw, dict):
        return None
    try:
        season_number = int(raw.get("season_number"))
        episode_number = int(raw.get("episode_number"))
    except (TypeError, ValueError):
        return None
    name = raw.get("name")
    if not isinstance(name, str):
        return None
    overview = raw.get("overview") if isinstance(raw.get("overview"), str) else None
    air_date = raw.get("air_date") if isinstance(raw.get("air_date"), str) else None
    runtime = raw.get("runtime")
    if runtime is not None and not isinstance(runtime, int):
        try:
            runtime = int(runtime)
        except (TypeError, ValueError):
            runtime = None
    return TmdbEpisode(
        season_number=season_number,
        episode_number=episode_number,
        name=name,
        overview=overview,
        air_date=air_date,
        runtime=runtime,
        still_url=_still_url(raw.get("still_path")),
    )


@lru_cache(maxsize=128)
def _get_tv_season_episodes_cached(
    tmdb_id: str,
    season_number: int,
) -> Tuple[TmdbEpisode, ...]:
    """Cached layer over the real network call. Tuple return for hashability.

    Cache key is (tmdb_id, season_number). TMDB episode data doesn't change
    often within a process lifetime; LRU avoids hammering the API when the
    user navigates the titles step.
    """
    path = f"/tv/{tmdb_id}/season/{season_number}"
    data = _http_get(path, {})
    raw_episodes = data.get("episodes")
    if not isinstance(raw_episodes, list):
        return ()
    parsed = [ep for ep in (_parse_episode(r) for r in raw_episodes) if ep is not None]
    return tuple(parsed)


def get_tv_season_episodes(
    tmdb_id: str | int,
    season_number: int,
) -> List[TmdbEpisode]:
    """Fetch the episode list for a TV show's season from TMDB.

    Returns an empty list when TMDB responds with an empty ``episodes``
    array (e.g. a future season with no scheduled episodes yet).

    Raises:
        TmdbConfigError: API key not configured.
        TmdbNotFoundError: TMDB returned 404 (unknown tv id or season).
        TmdbNetworkError: API call failed or returned a non-OK status.
    """
    tid = str(tmdb_id).strip()
    if not tid:
        return []
    return list(_get_tv_season_episodes_cached(tid, int(season_number)))


# --- TV show details (#368 fold-in) -----------------------------------------

@dataclass(frozen=True)
class TmdbTvDetails:
    """Top-level metadata for a TV show — number_of_seasons drives the
    disc-card primary-season selector on the frontend (#371)."""
    tmdb_id: str
    name: str
    number_of_seasons: int
    status: Optional[str]


@lru_cache(maxsize=128)
def _get_tv_details_cached(tmdb_id: str) -> TmdbTvDetails:
    path = f"/tv/{tmdb_id}"
    data = _http_get(path, {})
    name = data.get("name") if isinstance(data.get("name"), str) else ""
    raw_n = data.get("number_of_seasons")
    try:
        number_of_seasons = int(raw_n) if raw_n is not None else 1
    except (TypeError, ValueError):
        number_of_seasons = 1
    if number_of_seasons < 1:
        number_of_seasons = 1
    status = data.get("status") if isinstance(data.get("status"), str) else None
    return TmdbTvDetails(
        tmdb_id=tmdb_id,
        name=name,
        number_of_seasons=number_of_seasons,
        status=status,
    )


def get_tv_details(tmdb_id: str | int) -> Optional[TmdbTvDetails]:
    """Fetch top-level TV-show metadata from TMDB ``/3/tv/{tv_id}``.

    Returns ``None`` for a blank id (defensive — caller may pass through
    user input). Raises the same exceptions as ``get_tv_season_episodes``.
    """
    tid = str(tmdb_id).strip()
    if not tid:
        return None
    return _get_tv_details_cached(tid)
