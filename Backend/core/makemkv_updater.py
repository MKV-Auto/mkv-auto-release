"""
Utilities for querying and updating the local MakeMKV installation.
The update flow mirrors the manual instructions in README.md but wraps
them in Python so the API or CLI can orchestrate downloads and builds.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import select
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from collections import Counter
import re
import urllib.parse
import requests
from core import makemkv_state
from pathlib import Path as _Path

import threading

from core.utils import get_makemkvcon_path, get_mkvauto_tmp, hash_file

FFMPEG_URL_TEMPLATE = "https://ffmpeg.org/releases/ffmpeg-{version}.tar.xz"

# ─── FFmpeg compatibility ceiling ───────────────────────────────────────────
# MakeMKV's libffabi is compiled against whatever FFmpeg we build here, and it
# still uses AVCodec fields that FFmpeg deprecated in 7.1 (in favour of
# avcodec_get_supported_config()) and REMOVED in 9.0: `ch_layouts`,
# `sample_fmts`, `supported_samplerates`. Building MakeMKV against 9.x fails
# with "error: 'AVCodec' has no member named 'ch_layouts'" and friends.
#
# This resolver used to take the newest tarball on ffmpeg.org unconditionally,
# which made every install a hostage to upstream's release schedule: FFmpeg 9.0
# was published 2026-08-03 and every fresh install broke that day, with no
# change on our side. The ceiling is EXCLUSIVE and deliberately coarse — we
# still track the newest release below it, so 8.x point releases (including
# security fixes) roll in automatically; only the major bump is gated.
#
# Raise this when MakeMKV's ffabi moves onto avcodec_get_supported_config().
# Verify first by building against the new major in a scratch container —
# `mkv test makemkv` covers it — because the failure mode is a compile error
# an hour into a user's first-run setup.
FFMPEG_MAX_VERSION_EXCLUSIVE = (9, 0)

# ─── Version cache (#343) ───────────────────────────────────────────────────
# Cache installed version keyed by resolved path + st_size + st_mtime_ns.
# Invalidated when the binary changes (stat differs) or explicitly via
# invalidate_version_cache() after a MakeMKV install/update.
_version_cache_lock = threading.Lock()
_version_cache: dict = {
    "path": None,
    "st_size": None,
    "st_mtime_ns": None,
    "version": None,
}


def invalidate_version_cache() -> None:
    """Force the next get_installed_version() call to rescan the binary."""
    with _version_cache_lock:
        _version_cache["path"] = None
        _version_cache["st_size"] = None
        _version_cache["st_mtime_ns"] = None
        _version_cache["version"] = None
MAKEMKV_BIN_URL = "https://www.makemkv.com/download/makemkv-bin-{version}.tar.gz"
MAKEMKV_OSS_URL = "https://www.makemkv.com/download/makemkv-oss-{version}.tar.gz"

# Timeouts for HTTP operations (seconds)
MAKEMKV_VERSION_PAGE_TIMEOUT = 10
MAKEMKV_DOWNLOAD_TIMEOUT = 30
# Max seconds with no data received before treating connection as stalled (open but not downloading)
MAKEMKV_DOWNLOAD_PROGRESS_TIMEOUT = 15
WAYBACK_REQUEST_TIMEOUT = 15
# CDX is an index SCAN, not a lookup, and is routinely far slower than the
# availability API this timeout was sized for. Measured from the ripper on
# 2026-08-05: 32.5s, 8.2s, >60s for the same query. At 15s the primary
# resolver timed out on most attempts and fell through to the availability
# API — which is the endpoint that rate-limits — so a first-run install
# failed with "archive.org could not be queried" while CDX itself was
# perfectly healthy. Found by running the actual setup wizard; the unit
# tests mock requests, so the timeout never showed up there.
WAYBACK_CDX_TIMEOUT = 90
# Archive.org is a slow mirror serving large files, and with makemkv.com down
# it is the ONLY source — so a transient stall here fails a user's install
# outright. Measured on a GitHub runner (2026-08-05): the 6.6MB oss tarball
# timed out at 120s while the same fetch succeeded in ~60s from another host.
WAYBACK_DOWNLOAD_TIMEOUT = 300
# A capture that times out deserves another go before we write it off: a
# popular tarball often has exactly ONE usable capture, so "move to the next
# capture" is frequently not an option at all.
WAYBACK_DOWNLOAD_ATTEMPTS = 3
WAYBACK_DOWNLOAD_RETRY_BACKOFF_S = (5, 20)

# Wayback Machine fallback when makemkv.com is unreachable
WAYBACK_AVAILABLE_API = "https://archive.org/wayback/available"
# CDX is the Wayback *index*: one row per capture, filterable and sortable,
# where the availability API above returns a single "closest" snapshot and
# nothing else. Both matter here — see _wayback_snapshot_urls_for.
WAYBACK_CDX_API = "https://web.archive.org/cdx/search/cdx"
# How many archived captures to try before giving up. Captures of one URL
# are few (2 for the 1.18.4 tarballs), so this is a generous ceiling.
WAYBACK_MAX_SNAPSHOTS = 5
# A capture smaller than this is a crawl artifact, not a tarball — the
# 1.18.4 captures from 2026-07-11 are 1,027-byte error pages recorded while
# makemkv.com was already failing. Reject them from the index rather than
# downloading and failing verification.
WAYBACK_MIN_TARBALL_BYTES = 1_000_000
# archive.org rate-limits its index aggressively and answers 429 with an HTML
# body. That is a "come back shortly", not an answer, so retry a few times
# with backoff before giving up. Bounded: an install already takes minutes,
# but it must not hang here.
WAYBACK_RATE_LIMIT_RETRIES = 3
WAYBACK_RATE_LIMIT_BACKOFF_S = (5, 15, 30)
WAYBACK_FORUM_FALLBACK_URL = (
    "https://web.archive.org/web/20260220053429/"
    "https://forum.makemkv.com/forum/viewtopic.php?t=224&f=3"
)
FORUM_URL_FOR_WAYBACK = "https://forum.makemkv.com/forum/viewtopic.php?f=3&t=224"

# Browser-like User-Agent so Cloudflare (error 1010) allows the download
DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/octet-stream,*/*",
}
# Request raw body from archive.org to avoid double-gzip (server sends content-encoding: gzip on an already-gzipped .tar.gz)
WAYBACK_DOWNLOAD_HEADERS = {**DOWNLOAD_HEADERS, "Accept-Encoding": "identity"}

log = logging.getLogger("core.makemkv_updater")


class BuildEnvironmentError(RuntimeError):
    """The BUILD HOST is not equipped — missing compiler, headers, libraries.

    Deliberately not a MakeMKVUpdateError subclass so callers cannot lump it
    in with "this version pair does not work". It is a statement about the
    machine, not about the software: the version-matrix job publishes
    compatibility claims, and a misconfigured runner recording
    "1.18.4 is incompatible with every FFmpeg" would tell users something
    false about their software (observed 2026-08-05, when a runner missing
    libx264-dev published exactly that).
    """


class MakeMKVUpdateError(RuntimeError):
    """Raised when the MakeMKV update process fails."""


def _run(cmd: List[str], *, cwd: Optional[Path] = None, env: Optional[dict] = None, logs: List[str], log_cb=None) -> None:
    """
    Wrapper around subprocess.Popen that streams stdout/stderr in real-time
    and sends heartbeat messages during long silent periods.
    """
    pretty = " ".join(cmd)
    log.info("Running: %s", pretty)
    if log_cb:
        log_cb(f"Running: {pretty}")
    
    # Use Popen for real-time output streaming
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1  # Line buffered
    )
    
    last_output_time = time.time()
    heartbeat_interval = 30  # Send heartbeat every 30 seconds of silence
    stderr_lines = []
    
    # Poll for output with timeout to allow heartbeat messages
    while True:
        # Check if process has finished
        if process.poll() is not None:
            # Read any remaining output
            remaining_stdout = process.stdout.read() if process.stdout else ""
            remaining_stderr = process.stderr.read() if process.stderr else ""
            if remaining_stdout:
                for line in remaining_stdout.strip().split('\n'):
                    if line:
                        logs.append(line)
                        if log_cb:
                            log_cb(line)
            if remaining_stderr:
                for line in remaining_stderr.strip().split('\n'):
                    if line:
                        logs.append(line)
                        stderr_lines.append(line)
                        if log_cb:
                            log_cb(line)
            break
        
        # Read available output with timeout
        ready, _, _ = select.select([process.stdout, process.stderr], [], [], 1.0)
        
        if ready:
            last_output_time = time.time()
            for stream in ready:
                line = stream.readline()
                if line:
                    line = line.strip()
                    logs.append(line)
                    if stream == process.stderr:
                        stderr_lines.append(line)
                    if log_cb:
                        log_cb(line)
        else:
            # No output available, check if we need a heartbeat
            current_time = time.time()
            if current_time - last_output_time >= heartbeat_interval:
                if log_cb:
                    log_cb(f"Still building... (running for {int(current_time - last_output_time + heartbeat_interval)}s)")
                last_output_time = current_time
    
    returncode = process.returncode
    if returncode != 0:
        stderr_text = '\n'.join(stderr_lines) if stderr_lines else "No error output"
        log.error(
            "Command failed: %s (exit %s)\nstderr: %s",
            pretty,
            returncode,
            stderr_text,
        )
        raise MakeMKVUpdateError(f"Command failed ({returncode}): {pretty}\n{stderr_text}")


# Pattern for MakeMKV bin/oss tarball URLs (used for Wayback fallback)
_MAKEMKV_TARBALL_PATTERN = re.compile(
    r"^https://www\.makemkv\.com/download/makemkv-(?:bin|oss)-\d+\.\d+\.\d+\.tar\.gz$",
    re.IGNORECASE,
)


def _verify_tarball_gz(archive: Path, logs: List[str], log_cb=None) -> bool:
    """Return True if the file is a valid .tar.gz we can extract; False if truncated/corrupt."""
    try:
        with tarfile.open(archive, "r:gz") as tar:
            tar.getmembers()
        return True
    except tarfile.ReadError:
        return False
    except (EOFError, OSError) as exc:
        err_str = str(exc).lower()
        if "end-of-stream" in err_str or "compressed file ended" in err_str or "unexpected end of data" in err_str:
            return False
        raise


def _unwrap_double_gzip_if_needed(archive: Path, logs: List[str], log_cb=None) -> None:
    """
    If the file is double-gzipped (e.g. archive.org re-compresses .tar.gz),
    decompress one layer in place so we have a normal .tar.gz for verification and extraction.
    """
    data = archive.read_bytes()
    if len(data) < 2 or data[:2] != b"\x1f\x8b":
        return
    try:
        first = gzip.decompress(data)
        if len(first) < 2 or first[:2] != b"\x1f\x8b":
            return
        archive.write_bytes(first)
        msg = "Unwrapped double-gzip from archive.org"
        logs.append(msg)
        if log_cb:
            log_cb(msg)
        log.info("%s: %s", msg, archive)
    except (OSError, gzip.BadGzipFile):
        return


def _download(
    url: str,
    dest: Path,
    logs: List[str],
    log_cb=None,
    *,
    timeout: int = MAKEMKV_DOWNLOAD_TIMEOUT,
    headers: Optional[dict] = None,
) -> None:
    if headers is None:
        headers = DOWNLOAD_HEADERS
    def _remaining_str(deadline_ts: float) -> str:
        remaining = max(0, int(deadline_ts - time.monotonic()))
        return f"{remaining // 60}:{remaining % 60:02d}"

    deadline = time.monotonic() + timeout
    # Socket read timeout: if no data received for this many seconds, treat as stalled (not just slow)
    read_timeout = min(MAKEMKV_DOWNLOAD_PROGRESS_TIMEOUT, timeout)
    log.info("Downloading %s -> %s", url, dest)
    if log_cb:
        log_cb(f"Downloading {url} ({_remaining_str(deadline)} remaining)")
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=read_timeout) as response, open(
            dest, "wb"
        ) as fh:
            # Get content length if available for progress reporting
            content_length = response.getheader("Content-Length")
            total_size = int(content_length) if content_length else None

            # Enforce total elapsed time so slow/trickle responses don't hang indefinitely
            downloaded = 0
            chunk_size = 8192  # 8KB chunks
            last_progress_report = 0
            progress_interval = 5 * 1024 * 1024  # Report every 5MB

            while True:
                if time.monotonic() > deadline:
                    msg = f"Download timed out after {timeout} seconds"
                    logs.append(msg)
                    if log_cb:
                        log_cb(msg)
                    log.error("%s URL: %s", msg, url)
                    try:
                        dest.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise MakeMKVUpdateError(f"{msg} for URL {url}")
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                fh.write(chunk)
                downloaded += len(chunk)

                # Report progress every 5MB
                if downloaded - last_progress_report >= progress_interval:
                    if total_size:
                        percent = (downloaded / total_size) * 100
                        msg = f"Downloaded {downloaded // (1024*1024)}MB / {total_size // (1024*1024)}MB ({percent:.0f}%)"
                    else:
                        msg = f"Downloaded {downloaded // (1024*1024)}MB"
                    msg += f" — {_remaining_str(deadline)} remaining"
                    if log_cb:
                        log_cb(msg)
                    last_progress_report = downloaded

            # Report completion if we haven't already reported at this size
            if (
                total_size
                and downloaded >= total_size
                and downloaded - last_progress_report > 0
            ):
                percent = 100.0
                msg = f"Downloaded {downloaded // (1024*1024)}MB / {total_size // (1024*1024)}MB ({percent:.0f}%) — {_remaining_str(deadline)} remaining"
                if log_cb:
                    log_cb(msg)

            # Validate we got a real file so callers (e.g. _download_with_fallback) can retry or use fallback
            if downloaded == 0:
                msg = "Download produced no data (empty response)"
                logs.append(msg)
                if log_cb:
                    log_cb(msg)
                log.error("%s URL: %s", msg, url)
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
                raise MakeMKVUpdateError(f"{msg} for URL {url}")
            if total_size is not None and downloaded != total_size:
                msg = f"Download incomplete: got {downloaded} bytes, expected {total_size}"
                logs.append(msg)
                if log_cb:
                    log_cb(msg)
                log.error("%s URL: %s", msg, url)
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
                raise MakeMKVUpdateError(f"{msg} for URL {url}")
    except urllib.error.HTTPError as exc:
        body_snippet = ""
        try:
            body_bytes = exc.read(2048)
            body_snippet = (
                body_bytes.decode("utf-8", errors="replace").strip() or "(empty)"
            )
        except Exception:
            body_snippet = "(could not read response body)"
        msg_line = f"Download failed: HTTP {exc.code} {exc.reason}"
        url_line = f"URL: {url}"
        resp_line = f"Response: {body_snippet}"
        logs.append(msg_line)
        logs.append(url_line)
        logs.append(resp_line)
        if log_cb:
            log_cb(msg_line)
            log_cb(url_line)
            log_cb(resp_line)
        log.error("%s %s %s", msg_line, url_line, resp_line)
        raise MakeMKVUpdateError(
            f"Download failed: HTTP {exc.code} {exc.reason} for URL {url}"
        ) from exc
    except TimeoutError as exc:
        msg_line = f"No data received within {read_timeout} seconds (connection stalled)"
        logs.append(msg_line)
        logs.append(f"URL: {url}")
        if log_cb:
            log_cb(msg_line)
            log_cb(f"URL: {url}")
        log.error("%s URL: %s", msg_line, url)
        raise MakeMKVUpdateError(
            f"Download failed: {msg_line} for URL {url}"
        ) from exc
    except urllib.error.URLError as exc:
        msg_line = f"Download failed: {exc.reason or exc}"
        logs.append(msg_line)
        logs.append(f"URL: {url}")
        if log_cb:
            log_cb(msg_line)
            log_cb(f"URL: {url}")
        log.error("%s URL: %s", msg_line, url)
        raise SourceUnavailableError(
            f"Download failed (timeout or network): {exc.reason or exc} for URL {url}"
        ) from exc
    log.debug("Downloaded %s", url)
    logs.append(f"Downloaded {url}")
    if log_cb:
        log_cb(f"Downloaded {url}")


class SourceUnavailableError(MakeMKVUpdateError):
    """Sources could not be FETCHED — network, timeout, mirror refusing.

    Says nothing about whether the software builds. On 2026-08-06 a
    transient ffmpeg.org timeout was recorded as `1.18.4 + 8.1.2 ->
    build_failed`; that verdict is memoized, so the pair would never have
    been retried and the matrix would have pinned users to an older FFmpeg
    on the strength of one slow download.

    Deliberately a MakeMKVUpdateError subclass: the archive-fallback retry
    loop catches that type, and breaking the hierarchy would stop the
    fallback from retrying at all. Callers that need to tell the two apart
    order this handler first.
    """


class WaybackLookupError(Exception):
    """The archive could not be *asked* (rate limit, network, bad response).

    Distinct from "asked, and there are no captures". Conflating the two is
    how a routine HTTP 429 from archive.org came to be reported to users as
    "no Wayback snapshot for URL …" while the file was, in fact, archived.
    """


def _cdx_rows(original_url: str) -> list[list]:
    """Query the Wayback CDX index for every capture of ``original_url``.

    Returns CDX rows (timestamp/status/digest/length per capture), newest
    first. Raises WaybackLookupError when the index cannot be reached or
    answers with something that isn't the JSON we asked for — notably an
    HTML 429 body, which archive.org serves freely under load.

    ``collapse=digest`` drops consecutive captures with identical content,
    so we try genuinely different bytes rather than the same file five
    times.
    """
    params = {
        "url": original_url,
        "output": "json",
        "filter": "statuscode:200",
        "collapse": "digest",
        "limit": str(-WAYBACK_MAX_SNAPSHOTS),  # negative = the LAST n (newest)
    }
    resp = None
    for attempt in range(WAYBACK_RATE_LIMIT_RETRIES + 1):
        try:
            resp = requests.get(
                WAYBACK_CDX_API,
                params=params,
                timeout=WAYBACK_CDX_TIMEOUT,
                headers=DOWNLOAD_HEADERS,
            )
        except Exception as exc:
            raise WaybackLookupError(f"CDX request failed: {exc}") from exc
        if resp.status_code != 429:
            break
        if attempt >= WAYBACK_RATE_LIMIT_RETRIES:
            break
        # Honour Retry-After when archive.org sends it; otherwise back off.
        delay = WAYBACK_RATE_LIMIT_BACKOFF_S[min(attempt, len(WAYBACK_RATE_LIMIT_BACKOFF_S) - 1)]
        try:
            delay = max(delay, int(resp.headers.get("Retry-After", 0)))
        except (TypeError, ValueError):
            pass
        log.info("CDX rate-limited; retrying in %ss (attempt %d)", delay, attempt + 1)
        time.sleep(delay)
    if resp is None:
        raise WaybackLookupError("CDX request produced no response")
    if resp.status_code == 429:
        raise WaybackLookupError("CDX rate-limited (HTTP 429) after retries")
    if resp.status_code >= 400:
        raise WaybackLookupError(f"CDX returned HTTP {resp.status_code}")
    body = (resp.text or "").strip()
    if not body:
        return []  # asked successfully; genuinely no captures
    try:
        rows = json.loads(body)
    except ValueError as exc:
        # An HTML body here means an error page, not an empty index.
        raise WaybackLookupError(f"CDX returned non-JSON body: {body[:120]!r}") from exc
    if not isinstance(rows, list) or len(rows) < 2:
        return []
    return list(reversed(rows[1:]))  # drop header row; newest first


def _wayback_snapshot_urls_for(
    original_url: str, *, min_bytes: int = 0
) -> list[str]:
    """Candidate archive.org download URLs for ``original_url``, best first.

    Every capture is a candidate, not just the "closest" one: the index
    routinely holds duds alongside good copies (the 1.18.4 tarballs have a
    good June capture and a 1 KB July error page each), so the caller walks
    the list until a download verifies.

    ``min_bytes`` filters obvious non-tarballs out of the index using the
    CDX record length, which avoids spending a download on them.

    Falls back to the availability API when CDX cannot answer, so a CDX
    outage degrades to the previous single-snapshot behaviour rather than
    to nothing.
    """
    urls: list[str] = []
    cdx_answered = False
    try:
        rows = _cdx_rows(original_url)
        cdx_answered = True
        for row in rows:
            # CDX row: [urlkey, timestamp, original, mimetype, statuscode, digest, length]
            if len(row) < 7:
                continue
            timestamp = row[1]
            try:
                length = int(row[6])
            except (TypeError, ValueError):
                length = 0
            if min_bytes and length and length < min_bytes:
                log.info(
                    "Skipping archive.org capture %s of %s: %d bytes is too small to be the tarball",
                    timestamp, original_url, length,
                )
                continue
            # id_ suffix returns raw content without Wayback rewriting
            urls.append(f"https://web.archive.org/web/{timestamp}id_/{original_url}")
    except WaybackLookupError as exc:
        log.warning("CDX lookup failed for %s (%s); trying availability API", original_url, exc)

    if urls:
        return urls
    if cdx_answered:
        # CDX is the authoritative index and it answered: there is nothing
        # usable here. Asking the availability API now would only surface a
        # capture CDX already told us to reject.
        return []

    # Secondary: the availability API. One snapshot, no filtering, but a
    # different endpoint — useful when CDX specifically is unhappy.
    try:
        api_url = f"{WAYBACK_AVAILABLE_API}?url={urllib.parse.quote(original_url, safe='')}"
        resp = requests.get(api_url, timeout=WAYBACK_REQUEST_TIMEOUT, headers=DOWNLOAD_HEADERS)
        if resp.status_code == 429:
            raise WaybackLookupError("availability API rate-limited (HTTP 429)")
        resp.raise_for_status()
        snap = (resp.json().get("archived_snapshots") or {}).get("closest")
        if snap and snap.get("available") and snap.get("timestamp"):
            return [f"https://web.archive.org/web/{snap['timestamp']}id_/{original_url}"]
    except Exception as exc:
        raise WaybackLookupError(f"availability API failed: {exc}") from exc
    return []


def _wayback_url_for(original_url: str) -> Optional[str]:
    """Back-compat single-snapshot resolver. Prefer _wayback_snapshot_urls_for."""
    try:
        urls = _wayback_snapshot_urls_for(original_url)
    except WaybackLookupError:
        return None
    return urls[0] if urls else None


def _download_with_fallback(
    url: str, dest: Path, logs: List[str], log_cb=None
) -> None:
    """
    Download from primary URL; on failure, try Wayback Machine for MakeMKV bin/oss tarballs.
    For MakeMKV tarballs, verifies the download is a valid archive; if corrupt/truncated, tries fallback.
    """
    try:
        _download(url, dest, logs, log_cb=log_cb)
    except (MakeMKVUpdateError, TimeoutError):
        # Emit immediately so user sees we're trying fallback (before _wayback_url_for which can take up to WAYBACK_REQUEST_TIMEOUT)
        _primary_fail_msg = "Primary download failed, trying archive.org fallback..."
        logs.append(_primary_fail_msg)
        if log_cb:
            log_cb(_primary_fail_msg)
        pass
    except Exception:
        raise
    else:
        # Download succeeded — for MakeMKV tarballs verify before accepting
        if _MAKEMKV_TARBALL_PATTERN.match(url):
            if not _verify_tarball_gz(dest, logs, log_cb):
                msg = "Downloaded file failed verification (incomplete or corrupted), trying archive.org fallback"
                logs.append(msg)
                if log_cb:
                    log_cb(msg)
                log.warning("%s %s", msg, url)
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
                # Fall through to try Wayback
            else:
                return
        else:
            return

    if not _MAKEMKV_TARBALL_PATTERN.match(url):
        raise MakeMKVUpdateError(
            f"Primary and Wayback download failed for URL {url}"
        )

    try:
        snapshot_urls = _wayback_snapshot_urls_for(
            url, min_bytes=WAYBACK_MIN_TARBALL_BYTES
        )
    except WaybackLookupError as exc:
        # We could not ASK the archive. Say that, rather than asserting the
        # file isn't archived — the two need different actions from the user
        # (retry later vs. fetch the tarball by hand).
        msg = (
            f"Primary download failed and archive.org could not be queried for {url} "
            f"({exc}). This is usually a temporary rate limit — try the install again "
            f"in a few minutes."
        )
        logs.append(msg)
        if log_cb:
            log_cb(msg)
        log.warning("%s", msg)
        raise MakeMKVUpdateError(msg) from exc

    if not snapshot_urls:
        raise MakeMKVUpdateError(
            f"Primary download failed and no Wayback snapshot for URL {url}"
        )

    # Walk captures newest-first: the index holds duds next to good copies,
    # so "we found a snapshot" is not the same as "we found the file".
    last_error: Optional[str] = None
    for idx, wayback_url in enumerate(snapshot_urls, start=1):
        fallback_msg = (
            f"Using Wayback Machine fallback for {url} "
            f"(capture {idx} of {len(snapshot_urls)})"
        )
        logs.append(fallback_msg)
        if log_cb:
            log_cb(fallback_msg)
        log.info("%s -> %s", fallback_msg, wayback_url)

        # Retry this capture before giving up on it. With one usable capture
        # (the common case) the alternative is failing the whole install on a
        # single slow read.
        downloaded = False
        for attempt in range(WAYBACK_DOWNLOAD_ATTEMPTS):
            try:
                _download(
                    wayback_url, dest, logs, log_cb=log_cb,
                    headers=WAYBACK_DOWNLOAD_HEADERS, timeout=WAYBACK_DOWNLOAD_TIMEOUT,
                )
                downloaded = True
                break
            except (MakeMKVUpdateError, TimeoutError) as exc:
                last_error = str(exc)
                log.warning(
                    "Archive capture %s failed to download (attempt %d/%d): %s",
                    wayback_url, attempt + 1, WAYBACK_DOWNLOAD_ATTEMPTS, exc,
                )
                try:
                    dest.unlink(missing_ok=True)  # drop the partial before retrying
                except OSError:
                    pass
                if attempt + 1 < WAYBACK_DOWNLOAD_ATTEMPTS:
                    delay = WAYBACK_DOWNLOAD_RETRY_BACKOFF_S[
                        min(attempt, len(WAYBACK_DOWNLOAD_RETRY_BACKOFF_S) - 1)
                    ]
                    retry_msg = f"Retrying that capture in {delay}s…"
                    logs.append(retry_msg)
                    if log_cb:
                        log_cb(retry_msg)
                    time.sleep(delay)
        if not downloaded:
            continue

        # Archive.org sometimes returns double-gzipped content; unwrap one layer
        # so we have a normal .tar.gz.
        _unwrap_double_gzip_if_needed(dest, logs, log_cb)

        if _verify_tarball_gz(dest, logs, log_cb):
            return

        last_error = "failed archive verification"
        msg = f"Archive capture {idx} failed verification; trying an older capture"
        logs.append(msg)
        if log_cb:
            log_cb(msg)
        log.warning("Wayback capture failed verification: %s", wayback_url)
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass

    msg = (
        f"Primary download failed and all {len(snapshot_urls)} archive.org capture(s) "
        f"of {url} were unusable"
        + (f" (last error: {last_error})" if last_error else "")
        + ". Try the install again later, or download the tarball manually from makemkv.com."
    )
    logs.append(msg)
    if log_cb:
        log_cb(msg)
    log.warning("%s", msg)
    raise MakeMKVUpdateError(msg)

def _has_lzma() -> bool:
    try:
        import lzma  # noqa: F401
        return True
    except Exception:
        return False


def _check_build_deps(logs: List[str], log_cb=None) -> None:
    """
    Verify minimal toolchain for ffmpeg/MakeMKV builds and emit actionable hints.
    We do not auto-install packages; we just fail fast with guidance.
    """
    missing_tools = []
    for tool in ("yasm", "pkg-config", "cmake", "nasm"):
        if shutil.which(tool) is None:
            missing_tools.append(tool)
    missing_libs = []
    lzma_missing = False
    for lib in ("fdk-aac", "x264"):
        rc = subprocess.run(
            ["pkg-config", "--exists", lib],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        if rc != 0:
            missing_libs.append(lib)
    # lzma is optional (we can fallback to system tar), so warn instead of fail
    rc_lzma = subprocess.run(
        ["pkg-config", "--exists", "lzma"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode
    if rc_lzma != 0:
        lzma_missing = True

    if missing_tools or missing_libs:
        msg = (
            "Missing build dependencies: "
            + (f"tools={','.join(missing_tools)} " if missing_tools else "")
            + (f"libs={','.join(missing_libs)}" if missing_libs else "")
            + ". Install via: sudo apt-get install -y "
              "build-essential yasm nasm pkg-config cmake "
              "libfdk-aac-dev libx264-dev liblzma-dev"
        )
        logs.append(msg)
        if log_cb:
            log_cb(msg)
        raise BuildEnvironmentError(msg)

    if lzma_missing:
        warn = "liblzma-dev missing; will use system tar to extract .xz archives (install liblzma-dev to avoid this warning)"
        logs.append(warn)
        if log_cb:
            log_cb(warn)


def _extract(archive: Path, dest: Path, logs: List[str], log_cb=None) -> Path:
    try:
        size = archive.stat().st_size
    except OSError:
        size = -1
    log.info("Extract start: archive=%s size=%s dest=%s", archive, size, dest)
    start_msg = f"Extracting {archive.name} ({size // (1024*1024)}MB)" if size >= 0 else f"Extracting {archive.name}"
    logs.append(start_msg)
    if log_cb:
        log_cb(start_msg)
    if archive.suffixes and archive.suffixes[-1] == ".xz" and not _has_lzma():
        # Python lacks lzma: fallback to system tar
        log.info("Extract: using system tar for .xz")
        dest.mkdir(parents=True, exist_ok=True)
        # Use _run() for extraction to get heartbeat messages during long extractions
        _run(["tar", "-xf", str(archive), "-C", str(dest)], cwd=None, logs=logs, log_cb=log_cb)
        log.info("Extract: system tar finished, listing contents")
        # List contents with timeout to avoid hanging
        try:
            result = subprocess.run(
                ["tar", "-tf", str(archive)],
                capture_output=True,
                text=True,
                timeout=30,
                check=True
            )
            first = result.stdout.splitlines()[0]
            top = first.split("/")[0]
        except subprocess.TimeoutExpired:
            raise MakeMKVUpdateError(f"Timeout while listing contents of {archive.name}")
        except subprocess.CalledProcessError as e:
            raise MakeMKVUpdateError(f"Failed to list contents of {archive.name}: {e.stderr}")
        logs.append("Used system tar to extract .xz (install liblzma-dev to enable Python lzma)")
        if log_cb:
            log_cb("Used system tar to extract .xz (install liblzma-dev to enable Python lzma)")
        return dest / top

    mode = "r:xz" if archive.suffixes and archive.suffixes[-1] == ".xz" else "r:gz"
    log.info("Extract: opening with tarfile mode=%s", mode)
    if log_cb:
        log_cb(f"Opening {archive.name} (mode={mode})")
    try:
        with tarfile.open(archive, mode) as tar:
            members = tar.getmembers()
            member_count = len(members)
            top = members[0].name.split("/")[0] if members else ""
            log.info("Extract: archive has %s members, extracting to %s", member_count, dest)
            if log_cb:
                log_cb(f"Extracting {member_count} files...")
            tar.extractall(dest)
            log.info("Extract: extractall done")
    except tarfile.ReadError as exc:
        msg = (
            f"Archive {archive.name} is truncated or corrupted (incomplete download?). "
            "Please try the install again; if it persists, check your network connection."
        )
        logs.append(msg)
        if log_cb:
            log_cb(msg)
        raise MakeMKVUpdateError(msg) from exc
    except (EOFError, OSError) as exc:
        err_str = str(exc).lower()
        if "end-of-stream" in err_str or "compressed file ended" in err_str or "unexpected end of data" in err_str:
            msg = (
                f"Archive {archive.name} appears incomplete or corrupted. "
                "The download may have been interrupted. Please try the install again."
            )
            logs.append(msg)
            if log_cb:
                log_cb(msg)
            raise MakeMKVUpdateError(msg) from exc
        raise
    log.info("Extract done: %s -> %s", archive.name, dest / top)
    if log_cb:
        log_cb(f"Extracted {archive.name}")
    return dest / top


def _accept_eula(target_dir: Path, logs: List[str], log_cb=None) -> None:
    """Auto-accept MakeMKV EULA by creating the expected marker file."""
    tmp = target_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    marker = tmp / "eula_accepted"
    marker.write_text("accepted\n")
    # some Makefiles check for a "yes" file in source root too
    yes_marker = target_dir / "yes"
    yes_marker.write_text("yes\n")
    msg = f"Auto-accepted EULA at {marker}"
    logs.append(msg)
    if log_cb:
        log_cb(msg)


def _version_key(version: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in version.split("."))


def _select_most_frequent_version(versions: list[str]) -> Optional[str]:
    if not versions:
        return None
    counts = Counter(versions)
    return max(counts, key=lambda version: (counts[version], _version_key(version)))


def get_makemkvcon_metadata() -> dict:
    """
    Return metadata about the resolved makemkvcon binary.
    Includes resolved path, sha256 hash, and mtime when available.
    """
    binary_path = get_makemkvcon_path()
    exists = os.path.isfile(binary_path)
    resolved_path = str(Path(binary_path).expanduser().resolve()) if exists else binary_path
    sha256 = hash_file(binary_path) if exists else None
    mtime = os.path.getmtime(binary_path) if exists else None
    return {
        "binary_path": binary_path,
        "resolved_path": resolved_path,
        "binary_sha256": sha256,
        "binary_mtime": mtime,
    }


def validate_makemkv_installation() -> dict:
    """
    Validate that MakeMKV is properly installed and functional.
    
    Checks:
    - makemkvcon binary exists at expected location
    - Binary is executable
    - Symlink exists (if in Docker/Linux)
    - Binary can be executed
    
    Returns:
        dict with:
            - is_valid: bool - Overall installation validity
            - can_rip: bool - Whether ripping is possible
            - missing_components: list[str] - What's missing/broken
            - error_message: str | None - Human-readable error
            - installed_version: str | None - Detected version
            - binary_path: str - Expected binary location
    """
    import stat
    
    missing_components = []
    error_message = None
    
    # Get expected binary path
    binary_path = get_makemkvcon_path()
    
    # Check 1: Binary exists
    if not os.path.isfile(binary_path):
        missing_components.append("makemkvcon_binary")
        error_message = f"MakeMKV CLI binary not found at {binary_path}"
        
        # Check if it's in the data directory (Docker case)
        if os.path.exists('/.dockerenv'):
            data_path = "/data/mkvauto/makemkv/bin/makemkvcon"
            if not os.path.isfile(data_path):
                missing_components.append("source_binary")
                error_message = f"MakeMKV binary missing from both {binary_path} and {data_path}. The BIN package may not have been installed."
            else:
                # Source exists but symlink is broken
                missing_components.append("symlink")
                error_message = f"MakeMKV binary exists at {data_path} but symlink at {binary_path} is missing or broken"
        
        return {
            "is_valid": False,
            "can_rip": False,
            "missing_components": missing_components,
            "error_message": error_message,
            "installed_version": None,
            "binary_path": binary_path,
        }
    
    # Check 2: Binary is executable
    try:
        file_stat = os.stat(binary_path)
        is_executable = bool(file_stat.st_mode & stat.S_IXUSR)
        if not is_executable:
            missing_components.append("executable_permission")
            error_message = f"MakeMKV binary at {binary_path} is not executable"
            return {
                "is_valid": False,
                "can_rip": False,
                "missing_components": missing_components,
                "error_message": error_message,
                "installed_version": None,
                "binary_path": binary_path,
            }
    except OSError as e:
        missing_components.append("file_access")
        error_message = f"Cannot access MakeMKV binary at {binary_path}: {e}"
        return {
            "is_valid": False,
            "can_rip": False,
            "missing_components": missing_components,
            "error_message": error_message,
            "installed_version": None,
            "binary_path": binary_path,
        }
    
    # Check 3: Validate symlink target (Docker case)
    if os.path.exists('/.dockerenv') and os.path.islink(binary_path):
        try:
            link_target = os.readlink(binary_path)
            if not os.path.isfile(link_target):
                missing_components.append("symlink_target")
                error_message = f"Symlink at {binary_path} points to non-existent file {link_target}"
                return {
                    "is_valid": False,
                    "can_rip": False,
                    "missing_components": missing_components,
                    "error_message": error_message,
                    "installed_version": None,
                    "binary_path": binary_path,
                }
        except OSError:
            pass  # Not a symlink or can't read - that's okay
    
    # Check 4: Try to get version (validates binary can execute)
    installed_version = get_installed_version()
    if installed_version is None:
        # Binary exists but can't get version - might be corrupted or wrong binary
        log.warning("MakeMKV binary exists but cannot determine version - binary may be corrupted")
        # Don't mark as invalid - version detection can fail for other reasons
    
    # All checks passed
    return {
        "is_valid": True,
        "can_rip": True,
        "missing_components": [],
        "error_message": None,
        "installed_version": installed_version,
        "binary_path": binary_path,
    }


def get_installed_version(force_refresh: bool = False) -> Optional[str]:
    """
    Returns the version reported by the makemkvcon binary by scanning strings
    output and selecting the most frequent version match.

    Results are cached by resolved path + st_size + st_mtime_ns so repeated
    calls (health checks, pollers) avoid running ``strings -a`` every time.
    Pass ``force_refresh=True`` to bypass the cache.
    """
    binary_path = get_makemkvcon_path()
    if not os.path.isfile(binary_path):
        return None

    # Resolve symlinks for stable identity
    try:
        resolved = os.path.realpath(binary_path)
        st = os.stat(resolved)
        cur_size = st.st_size
        cur_mtime_ns = st.st_mtime_ns
    except OSError:
        resolved = binary_path
        cur_size = None
        cur_mtime_ns = None

    # Check cache
    if not force_refresh:
        with _version_cache_lock:
            if (
                _version_cache["path"] == resolved
                and _version_cache["st_size"] == cur_size
                and _version_cache["st_mtime_ns"] == cur_mtime_ns
                and _version_cache["version"] is not None
            ):
                return _version_cache["version"]

    strings_bin = shutil.which("strings")
    if not strings_bin:
        log.warning("Cannot detect MakeMKV version: 'strings' command not found")
        return None

    try:
        result = subprocess.run(
            [strings_bin, "-a", binary_path],
            capture_output=True,
            text=True,
            errors="ignore",
            check=False,
        )
    except FileNotFoundError:
        return None

    output = (result.stdout or "") + "\n" + (result.stderr or "")
    version_regex = re.compile(r"v(\d+\.\d+\.\d+)")
    versions = [m.group(1) for m in version_regex.finditer(output)]
    version = _select_most_frequent_version(versions)

    # Store in cache
    with _version_cache_lock:
        _version_cache["path"] = resolved
        _version_cache["st_size"] = cur_size
        _version_cache["st_mtime_ns"] = cur_mtime_ns
        _version_cache["version"] = version

    return version


def _read_settings_key() -> Optional[str]:
    settings_path = Path.home() / ".MakeMKV" / "settings.conf"
    if not settings_path.exists():
        return None
    try:
        text = settings_path.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            if "app_Key" in line:
                m = re.search(r'app_Key\s*=\s*"(.*)"', line)
                if m:
                    return m.group(1).strip()
    except Exception:
        return None
    return None


_reg_status_cache_lock = threading.Lock()
_reg_status_cache: dict = {"expired": None, "message": None, "key": None, "ts": 0.0}
_REG_STATUS_CACHE_TTL = 300  # 5 minutes


def get_registration_status(force_refresh: bool = False) -> tuple[bool, Optional[str], Optional[str]]:
    """
    Return (expired, message, current_key). If makemkvcon output contains the expired string, expired=True.
    Cached for 5 minutes to avoid running makemkvcon on every health check.
    """
    import time as _time
    now = _time.monotonic()
    if not force_refresh:
        with _reg_status_cache_lock:
            if _reg_status_cache["ts"] > 0 and (now - _reg_status_cache["ts"]) < _REG_STATUS_CACHE_TTL:
                return _reg_status_cache["expired"], _reg_status_cache["message"], _reg_status_cache["key"]
    try:
        binary_path = get_makemkvcon_path()
        # "-r info disc:9999" exits right after the startup MSG stream (no
        # disc/drive needed) — the same invocation the key probe uses. A bare
        # makemkvcon run just prints usage text, which can never contain the
        # registration/expiry messages this function greps for, so the old
        # probe reported "not expired" unconditionally (prod, 2026-09-03).
        result = subprocess.run(
            [binary_path, "-r", "info", "disc:9999"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except FileNotFoundError:
        return False, "MakeMKV not found", _read_settings_key()
    except subprocess.TimeoutExpired:
        return False, "MakeMKV timed out", _read_settings_key()

    output = (result.stdout or "") + "\n" + (result.stderr or "")
    expired_strings = [
        "Evaluation period has expired",
        "shareware functionality unavailable",
    ]
    expired = any(s.lower() in output.lower() for s in expired_strings)
    key = _read_settings_key()
    with _reg_status_cache_lock:
        _reg_status_cache["expired"] = expired
        _reg_status_cache["message"] = output.strip() or None
        _reg_status_cache["key"] = key
        _reg_status_cache["ts"] = now
    return expired, output.strip() or None, key


# MakeMKV key shape: "M-" (purchased/permanent) or "T-" (beta/temporary) followed by
# a long base64ish payload. Charset per observed real keys; the strict match also
# guarantees the value is safe to embed in a settings.conf quoted string.
_MAKEMKV_KEY_RE = re.compile(r"^[MT]-[A-Za-z0-9_@%+=/-]{40,90}$")


def _probe_key_in_sandbox(binary_path: str, key: Optional[str], *, timeout: int = 90) -> tuple[str, str]:
    """
    Evaluate a candidate key in an ISOLATED $HOME so the user's real MakeMKV state
    is never touched (#688: an invalid stored key is *worse* than no key — it
    escalates a working trial into the MSG:5021 "too old" state, so write-then-
    revert against the real settings.conf is unsafe).

    Runs ``makemkvcon -r info disc:9999`` (exits right after the startup messages;
    needs no disc/drive) with ``app_Key`` planted in a temp HOME and classifies by
    makemkvcon's own verdict:

    - ``MSG:5020`` ("stored activation key is invalid") → ``invalid`` — emitted
      even while a trial is active, which makes it a definitive oracle.
    - ``MSG:5021`` without 5020 → ``binary_expired`` — a valid key normally
      rescues an expired beta, so 5021 persisting with the key applied means the
      binary needs an update before the key can be confirmed.
    - neither → ``valid``.

    Returns (verdict, combined_output).
    """
    with tempfile.TemporaryDirectory(prefix="mkv-key-probe-") as home:
        conf_dir = Path(home) / ".MakeMKV"
        conf_dir.mkdir(parents=True, exist_ok=True)
        if key:
            (conf_dir / "settings.conf").write_text(f'app_Key = "{key}"\n', encoding="utf-8")
        env = os.environ.copy()
        env["HOME"] = home
        result = subprocess.run(
            [binary_path, "-r", "info", "disc:9999"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=env,
        )
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        if "error while loading shared libraries" in output or "cannot open shared object file" in output:
            raise MakeMKVUpdateError(
                "MakeMKV is not properly installed. Please install MakeMKV before registering a key."
            )
        if "MSG:5020" in output:
            return "invalid", output
        if "MSG:5021" in output:
            return "binary_expired", output
        return "valid", output


def _write_app_key_preserving(key: str) -> Optional[str]:
    """
    Merge ``app_Key`` into the real ``~/.MakeMKV/settings.conf``, preserving every
    other setting. Returns the file's prior text (None if it did not exist) so a
    failed post-commit verification can restore it exactly.
    """
    settings_path = Path.home() / ".MakeMKV" / "settings.conf"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    prior_text: Optional[str] = None
    lines: list[str] = []
    if settings_path.exists():
        prior_text = settings_path.read_text(encoding="utf-8", errors="ignore")
        lines = [l for l in prior_text.splitlines() if not re.match(r"\s*app_Key\s*=", l)]
    lines.append(f'app_Key = "{key}"')
    settings_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return prior_text


def reapply_stored_registration_key() -> bool:
    """
    Re-write ``app_Key`` into ``~/.MakeMKV/settings.conf`` from the app's
    stored settings (the durable copy in /data).

    ``settings.conf`` lives in the container layer, so every image upgrade
    destroys it — and nothing re-created it unless the key was ALSO provided
    via the environment. A key registered through the UI therefore silently
    evaporated on the next upgrade, and makemkvcon ran unregistered until the
    evaluation window lapsed and Blu-ray/UHD rips started failing (prod,
    2026-09-03). Called at boot; the env-provided key takes precedence and is
    written by the caller before this runs. Returns True when the file was
    (re)written.
    """
    from core import settings as app_settings

    stored = (app_settings.load_settings().get("makemkv_registration_key") or "").strip()
    if not stored or not _MAKEMKV_KEY_RE.match(stored):
        return False
    if _read_settings_key() == stored:
        return False
    _write_app_key_preserving(stored)
    return True


def _restore_settings_conf(prior_text: Optional[str]) -> None:
    """Restore settings.conf to its pre-commit content (delete if it didn't exist)."""
    settings_path = Path.home() / ".MakeMKV" / "settings.conf"
    try:
        if prior_text is None:
            settings_path.unlink(missing_ok=True)
        else:
            settings_path.write_text(prior_text, encoding="utf-8")
    except OSError:
        pass


def set_registration_key(key: str) -> tuple[bool, str]:
    """
    Validate and register a MakeMKV key (purchased ``M-…`` or beta ``T-…``).
    Returns (success, message).

    #688: ``makemkvcon reg`` rejects valid beta keys that MakeMKV's runtime
    accepts from settings.conf (the GUI and community tooling write the key
    directly), so ``reg`` is not used at all. Instead the candidate is evaluated
    in a sandboxed $HOME via makemkvcon's own startup verdict (MSG:5020 oracle —
    see :func:`_probe_key_in_sandbox`), committed to the real settings.conf only
    on a clean probe, and verified post-commit (restoring the prior file if the
    committed state somehow regresses).
    """
    if not key or not key.strip():
        raise MakeMKVUpdateError("Registration key is empty")

    key = key.strip()
    if not _MAKEMKV_KEY_RE.match(key):
        raise MakeMKVUpdateError(
            "That doesn't look like a MakeMKV key. Keys start with \"M-\" (purchased) "
            "or \"T-\" (beta) followed by a long string of characters — paste the full key."
        )

    binary_path = get_makemkvcon_path()
    key_type = "purchased" if key.startswith("M-") else "beta"

    try:
        verdict, _output = _probe_key_in_sandbox(binary_path, key)
    except FileNotFoundError:
        raise MakeMKVUpdateError("MakeMKV (makemkvcon) not found")
    except subprocess.TimeoutExpired:
        raise MakeMKVUpdateError("Key validation timed out")

    if verdict == "invalid":
        raise MakeMKVUpdateError(
            "MakeMKV rejected this key as invalid. Double-check the full key was pasted"
            + (
                " — beta keys rotate roughly monthly, so make sure it's the current one from the MakeMKV forum."
                if key_type == "beta"
                else "."
            )
        )
    if verdict == "binary_expired":
        raise MakeMKVUpdateError(
            "This MakeMKV version is too old, and the key could not be confirmed on it. "
            "Update MakeMKV (Settings → MakeMKV → Update), then enter the key again."
        )

    # Clean probe: commit to the real settings.conf, then verify the committed state.
    prior_text = _write_app_key_preserving(key)
    try:
        post_verdict, _post_output = _probe_key_in_sandbox(binary_path, _read_settings_key())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        post_verdict = "valid"  # sandbox already proved the key; don't fail the commit on a flaky re-probe
    if post_verdict == "invalid":
        _restore_settings_conf(prior_text)
        raise MakeMKVUpdateError(
            "The key verified in isolation but not after saving — settings were restored. "
            "Please retry; if this persists, check ~/.MakeMKV/settings.conf for conflicting entries."
        )

    # Invalidate registration status cache so next health check picks up the new key
    with _reg_status_cache_lock:
        _reg_status_cache["ts"] = 0.0
    return True, f"Registration key verified and saved ({key_type} key)"


def _normalize_version(version: Optional[str]) -> str:
    """
    Ensure the requested version looks like a semantic MakeMKV release (e.g. 1.18.2).
    Return the cleaned numeric portion or raise if it cannot be parsed.
    """
    if not version:
        raise MakeMKVUpdateError("Version is required")

    m = re.search(r"(\d+\.\d+\.\d+)", version)
    if not m:
        raise MakeMKVUpdateError(f"Unrecognized MakeMKV version '{version}'. Expected format like 1.18.2")
    return m.group(1)


def _extract_versions_from_html(html: str) -> list[str]:
    return re.findall(r"makemkv-(?:bin|oss)-(\d+\.\d+\.\d+)\.tar\.gz", html, flags=re.IGNORECASE)


def _fetch_versions_from_wayback() -> list[str]:
    """
    Try to get MakeMKV version strings from the Wayback Machine (forum page).
    First tries the Available API for the closest snapshot; on failure uses a fixed snapshot URL.
    """
    versions: list[str] = []
    # Try Available API for closest snapshot of the forum page
    try:
        api_url = f"{WAYBACK_AVAILABLE_API}?url={urllib.parse.quote(FORUM_URL_FOR_WAYBACK, safe='')}"
        resp = requests.get(api_url, timeout=WAYBACK_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        snap = (data.get("archived_snapshots") or {}).get("closest")
        if snap and snap.get("available") and snap.get("url"):
            archive_url = snap["url"]
            page_resp = requests.get(archive_url, timeout=WAYBACK_REQUEST_TIMEOUT)
            page_resp.raise_for_status()
            versions.extend(_extract_versions_from_html(page_resp.text))
    except Exception as exc:
        log.warning("Wayback Available API or snapshot fetch failed: %s", exc)

    if not versions:
        # Fallback to fixed known-good snapshot
        try:
            resp = requests.get(WAYBACK_FORUM_FALLBACK_URL, timeout=WAYBACK_REQUEST_TIMEOUT)
            resp.raise_for_status()
            versions.extend(_extract_versions_from_html(resp.text))
        except Exception as exc:
            log.warning("Wayback fixed snapshot fetch failed: %s", exc)

    return versions


def _verify_against_manifest(
    makemkv_version: str, files: "list[Path]", logs: List[str], log_cb=None
) -> None:
    """Check downloaded artifacts against their build-validated hashes.

    A published hash exists only because those exact bytes compiled and
    produced a working binary, so a match proves more than "not corrupted".
    It is also what makes the archive.org fallback safe: an archived capture
    is otherwise trusted on faith, and this is the step that stops a
    substituted or subtly different tarball from being built and installed.

    A MISMATCH is fatal — that is the whole point. An artifact we cannot
    find a hash for is merely *unverified*: brand-new versions and offline
    installs legitimately land here, and refusing them would gate users on
    our CI for no security gain (we would have nothing to compare against
    either way).
    """
    try:
        from core import makemkv_manifest as mf

        manifest = mf.load_cached(Path(get_mkvauto_tmp()) / "manifest-cache")
    except Exception as exc:
        log.warning("Could not load manifest for verification: %s", exc)
        return

    for path in files:
        expected = mf.expected_sha256(manifest, makemkv_version, path.name)
        if not expected:
            msg = f"No published hash for {path.name}; installing unverified"
            logs.append(msg)
            log.info("%s", msg)
            continue
        actual = hash_file(str(path))
        if actual == expected:
            msg = f"Verified {path.name} against the validated build hash"
            logs.append(msg)
            if log_cb:
                log_cb(msg)
            continue
        msg = (
            f"{path.name} does not match the validated build hash "
            f"(expected {expected[:12]}…, got {actual[:12]}…). Refusing to build "
            f"an artifact we have not tested — this can mean a corrupted download "
            f"or a substituted file."
        )
        logs.append(msg)
        if log_cb:
            log_cb(msg)
        log.error("%s", msg)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise MakeMKVUpdateError(msg)


def resolve_ffmpeg_for_build(makemkv_version: str) -> Optional[str]:
    """Which FFmpeg to build this MakeMKV version against.

    Precedence, strongest evidence first:

    1. The manifest pairing — a combination CI actually compiled and
       smoke-tested. This is real knowledge, per MakeMKV version.
    2. The newest release under FFMPEG_MAX_VERSION_EXCLUSIVE — a single
       global guess, correct today but blind to which MakeMKV version it
       is building. It stays as the offline/no-manifest default.

    The ceiling deliberately survives the manifest's arrival: an install
    with no network path to GitHub still needs a sane answer, and "newest
    below the last major we know broke" is a better guess than "newest".
    """
    try:
        from core import makemkv_manifest as mf

        manifest = mf.load_cached(Path(get_mkvauto_tmp()) / "manifest-cache")
        pinned = mf.ffmpeg_for(manifest, makemkv_version)
        if pinned:
            log.info(
                "Building MakeMKV %s against validated FFmpeg %s (from manifest)",
                makemkv_version, pinned,
            )
            return pinned
        log.info(
            "No validated FFmpeg pairing for MakeMKV %s; falling back to newest below the %s ceiling",
            makemkv_version,
            ".".join(str(p) for p in FFMPEG_MAX_VERSION_EXCLUSIVE),
        )
    except Exception as exc:
        log.warning("Manifest lookup failed (%s); using the version ceiling", exc)
    return fetch_latest_ffmpeg()


def ffmpeg_version_key(version: str, *, width: int = 3) -> tuple:
    """Sortable tuple for an ffmpeg version string ('8.1.2' → (8, 1, 2)).

    Zero-padded to a fixed width so comparisons between differently-shaped
    versions are well-defined: a bare '9' must not compare below '9.0'
    (plain tuple comparison makes the shorter one smaller, which would let
    an incompatible major slip past the ceiling).
    """
    parts = tuple(int(x) for x in version.split("."))
    return (parts + (0,) * width)[:width]


def is_ffmpeg_version_supported(version: str) -> bool:
    """True when this FFmpeg version can build MakeMKV's libffabi.

    Gate is `< FFMPEG_MAX_VERSION_EXCLUSIVE`. Unparseable versions are
    rejected rather than assumed good — a version we cannot reason about
    must not silently become the one we build against.
    """
    try:
        key = ffmpeg_version_key(version)
    except (ValueError, AttributeError):
        return False
    ceiling = (FFMPEG_MAX_VERSION_EXCLUSIVE + (0, 0, 0))[:3]
    return key < ceiling


def supported_ffmpeg_versions(limit: int = 0) -> list[str]:
    """Every published FFmpeg release MakeMKV can build against, newest first.

    The relationship tester walks this list downward to find the boundary
    for a given MakeMKV version, which is how a real compatibility matrix
    gets built instead of a single global guess.
    """
    versions = _fetch_ffmpeg_release_versions()
    supported = [v for v in versions if is_ffmpeg_version_supported(v)]
    if versions and supported and versions[0] != supported[0]:
        log.info(
            "Newest published ffmpeg %s is at/above the compatibility ceiling %s; "
            "newest usable is %s",
            versions[0],
            ".".join(str(p) for p in FFMPEG_MAX_VERSION_EXCLUSIVE),
            supported[0],
        )
    elif versions and not supported:
        log.error(
            "No ffmpeg release below the MakeMKV compatibility ceiling %s "
            "(newest published: %s). MakeMKV cannot be built until the "
            "ceiling is raised — see FFMPEG_MAX_VERSION_EXCLUSIVE.",
            ".".join(str(p) for p in FFMPEG_MAX_VERSION_EXCLUSIVE),
            versions[0],
        )
    return supported[:limit] if limit else supported


def _fetch_ffmpeg_release_versions() -> list[str]:
    """All version strings on the FFmpeg releases page, newest first."""
    url = "https://ffmpeg.org/releases/"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
    except requests.HTTPError as exc:
        snippet = (exc.response.text or "")[:1024] if exc.response is not None else ""
        log.warning(
            "Failed to fetch ffmpeg releases page: url=%s status=%s response=%s",
            url,
            exc.response.status_code if exc.response else None,
            snippet,
        )
        return []
    except Exception as exc:
        log.warning("Failed to fetch ffmpeg releases page: url=%s error=%s", url, exc)
        return []

    matches = re.findall(r"ffmpeg-(\d+\.\d+(?:\.\d+)?)\.tar\.xz", resp.text)
    if not matches:
        log.warning("No ffmpeg tarballs found on releases page")
        return []
    return sorted(set(matches), key=ffmpeg_version_key, reverse=True)


def fetch_latest_ffmpeg() -> Optional[str]:
    """The newest FFmpeg release MakeMKV can build against, or None.

    None means "could not resolve" — callers must not silently skip the
    ffmpeg build and hand MakeMKV whatever the system has, which is a
    subtler failure than saying so.
    """
    supported = supported_ffmpeg_versions()
    return supported[0] if supported else None

def resolve_offerable_version() -> tuple[Optional[str], Optional[str], str]:
    """The newest MakeMKV version we may offer, and why.

    Returns ``(version, note, source)``. ``source`` is ``manifest`` when the
    answer came from the CI-validated manifest, ``upstream`` when we fell
    back to scraping makemkv.com, or ``unavailable``.

    Update detection prefers the manifest for two reasons. It gates
    structurally — a version CI has not built successfully simply is not in
    the manifest, so there is no policy check to get wrong. And it stops
    detection from depending on makemkv.com being reachable, which today it
    is not (Cloudflare 525) — the scrape currently limps along on a Wayback
    copy of a forum thread.

    ``note`` carries the user-facing explanation when a newer version
    exists but is being held back; a silently-never-updating installer is
    indistinguishable from a broken one.
    """
    from core import makemkv_manifest as mf

    manifest, status = mf.fetch_manifest(Path(get_mkvauto_tmp()) / "manifest-cache")
    validated = mf.latest_validated(manifest)
    if validated:
        note = None
        if mf.is_stale(manifest):
            note = (
                "The validated-version list has not been refreshed recently, so a "
                "newer MakeMKV release may not be listed yet."
            )
        # Surface a held-back version when upstream is ahead of us AND we
        # know why. Cheap: no network, the reason is already published.
        for row in (manifest or {}).get("known_incompatible") or []:
            candidate = row.get("makemkv_version") if isinstance(row, dict) else None
            if candidate and _version_gt(candidate, validated):
                note = mf.incompatibility_note(manifest, candidate) or note
                break
        return validated, note, "manifest"

    log.warning(
        "No validated MakeMKV manifest available (%s); falling back to upstream scrape",
        status,
    )
    try:
        return fetch_latest_version(), None, "upstream"
    except Exception as exc:
        log.warning("Upstream version lookup failed too: %s", exc)
        return None, None, "unavailable"


def _version_gt(a: str, b: str) -> bool:
    def key(v):
        try:
            return tuple(int(p) for p in str(v).split("."))
        except (TypeError, ValueError):
            return (0,)
    return key(a) > key(b)


def fetch_latest_version() -> str:
    """
    Scrape the MakeMKV download page for the latest tarball version.
    Tries primary URLs first; on failure falls back to Wayback Machine (forum page).
    """
    urls = [
        "https://www.makemkv.com/download/",
        # forum sticky that lists current beta
        "https://forum.makemkv.com/forum/viewtopic.php?f=3&t=224",
    ]
    versions: list[str] = []
    last_error: Optional[Exception] = None

    for url in urls:
        try:
            resp = requests.get(url, timeout=MAKEMKV_VERSION_PAGE_TIMEOUT)
            resp.raise_for_status()
            versions.extend(_extract_versions_from_html(resp.text))
        except (requests.HTTPError, requests.RequestException) as exc:
            last_error = exc
            if hasattr(exc, "response") and exc.response is not None:
                snippet = (getattr(exc.response, "text", "") or "")[:1024]
                log.warning(
                    "Failed to fetch MakeMKV version from %s: status=%s response=%s",
                    url,
                    getattr(exc.response, "status_code", None),
                    snippet,
                )
            else:
                log.warning("Failed to fetch MakeMKV version from %s: %s", url, exc)
            continue
        except Exception as exc:
            last_error = exc
            log.warning("Failed to fetch MakeMKV version from %s: %s", url, exc)
            continue

    if not versions:
        # Fallback: try Wayback Machine (forum page)
        versions = _fetch_versions_from_wayback()

    if not versions:
        raise MakeMKVUpdateError(
            "Could not determine latest MakeMKV version from primary download pages or Wayback Machine"
            + (f": {last_error}" if last_error else "")
        )

    # pick highest semver found
    def _version_key(v: str):
        return tuple(int(x) for x in v.split("."))
    latest = sorted(set(versions), key=_version_key, reverse=True)[0]
    return latest


def _create_system_symlinks(install_prefix: str, use_sudo: bool, logs: list, log_cb=None) -> None:
    """
    Create symlinks from standard system paths to MakeMKV installation.
    Used in Docker/root environments to make MakeMKV accessible via standard paths.
    """
    prefix = Path(install_prefix)
    
    symlinks = [
        (prefix / "bin" / "makemkvcon", Path("/usr/bin/makemkvcon")),
        (prefix / "lib" / "libmakemkv.so.1", Path("/usr/lib/libmakemkv.so.1")),
        (prefix / "lib" / "libdriveio.so.0", Path("/usr/lib/libdriveio.so.0")),
    ]
    
    for source, target in symlinks:
        if not source.exists():
            msg = f"Skipping symlink (source not found): {source}"
            logs.append(msg)
            if log_cb:
                log_cb(msg)
            continue
        
        try:
            # Remove existing symlink/file
            if target.exists() or target.is_symlink():
                cmd = ["rm", "-f", str(target)]
                if use_sudo:
                    cmd = ["sudo"] + cmd
                subprocess.run(cmd, check=True, capture_output=True)
            
            # Create symlink
            cmd = ["ln", "-s", str(source), str(target)]
            if use_sudo:
                cmd = ["sudo"] + cmd
            subprocess.run(cmd, check=True, capture_output=True)
            
            msg = f"Created symlink: {target} -> {source}"
            logs.append(msg)
            if log_cb:
                log_cb(msg)
        except subprocess.CalledProcessError as exc:
            msg = f"Warning: Failed to create symlink {target}: {exc}"
            logs.append(msg)
            if log_cb:
                log_cb(msg)
    
    # Update library cache
    try:
        cmd = ["ldconfig"]
        if use_sudo:
            cmd = ["sudo"] + cmd
        subprocess.run(cmd, check=True, capture_output=True)
        
        msg = "Updated library cache (ldconfig)"
        logs.append(msg)
        if log_cb:
            log_cb(msg)
    except subprocess.CalledProcessError as exc:
        msg = f"Warning: Failed to run ldconfig: {exc}"
        logs.append(msg)
        if log_cb:
            log_cb(msg)


# ─── Pre-download layout (#625) ────────────────────────────────────────────
PREDOWNLOAD_SUBDIR = "makemkv-download"
PREDOWNLOAD_MANIFEST_NAME = "manifest.json"
PREDOWNLOAD_EULA_NAME = "EULA.txt"

# Case-insensitive candidate filenames inside the source tarballs that may
# carry the EULA text. The extractor picks the first whose contents contain
# the phrase "End User License Agreement".
_EULA_MARKER = "end user license agreement"
_EULA_NAME_RE = re.compile(r"(?:^|/)(?:license|eula|legal|readme)(?:\.[a-z0-9]+)?$", re.IGNORECASE)


@dataclass
class DownloadResult:
    """Result of download_makemkv_sources() — where the tars landed and metadata."""
    version: str
    work_dir: Path
    bin_tar: Path
    oss_tar: Path
    eula_path: Optional[Path]
    already_present: bool
    logs: List[str]


def predownload_dir(version: str) -> Path:
    """
    Persistent per-version directory under ``${MKVAUTO_TMP_DIR}/makemkv-download/{version}``
    where the source tarballs and the extracted EULA text are cached. Lives in the
    named data volume so it survives container restarts.
    """
    base = get_mkvauto_tmp() / PREDOWNLOAD_SUBDIR / _normalize_version(version)
    base.mkdir(parents=True, exist_ok=True)
    return base


def read_predownload_manifest(version: Optional[str] = None) -> Optional[dict]:
    """Read the manifest for the given version (or latest present) from disk. None if absent."""
    if version:
        candidate = predownload_dir(version) / PREDOWNLOAD_MANIFEST_NAME
        if candidate.exists():
            try:
                return json.loads(candidate.read_text())
            except (OSError, json.JSONDecodeError):
                return None
        return None
    root = get_mkvauto_tmp() / PREDOWNLOAD_SUBDIR
    if not root.exists():
        return None
    manifests = sorted(root.glob(f"*/{PREDOWNLOAD_MANIFEST_NAME}"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    for m in manifests:
        try:
            return json.loads(m.read_text())
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _extract_eula_text(bin_tar: Path, oss_tar: Path, out_path: Path,
                       logs: List[str], log_cb=None) -> bool:
    """
    Walk the source tarballs for the file that contains the MakeMKV EULA text
    and copy its contents to ``out_path``. Returns True on success. Best-effort —
    if no candidate contains the marker phrase, returns False without raising.
    """
    for tar_path in (bin_tar, oss_tar):
        if not tar_path.exists():
            continue
        try:
            with tarfile.open(tar_path, "r:gz") as tar:
                for m in tar.getmembers():
                    if not m.isfile():
                        continue
                    if not _EULA_NAME_RE.search(m.name):
                        continue
                    try:
                        fh = tar.extractfile(m)
                        if fh is None:
                            continue
                        data = fh.read()
                    except (OSError, tarfile.TarError):
                        continue
                    text = data.decode("utf-8", errors="replace")
                    if _EULA_MARKER not in text.lower():
                        continue
                    out_path.write_text(text, encoding="utf-8")
                    msg = f"Extracted EULA text from {tar_path.name}:{m.name}"
                    logs.append(msg)
                    if log_cb:
                        log_cb(msg)
                    return True
        except (tarfile.ReadError, OSError):
            continue
    msg = "Could not locate EULA text in MakeMKV source tarballs"
    logs.append(msg)
    if log_cb:
        log_cb(msg)
    return False


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def download_makemkv_sources(
    version: Optional[str] = None,
    *,
    log_cb=None,
) -> DownloadResult:
    """
    Idempotent MakeMKV source download.

    Fetches ``makemkv-bin`` and ``makemkv-oss`` tarballs to a persistent, versioned
    directory under ``${MKVAUTO_TMP_DIR}/makemkv-download/{version}``. If both
    tars are already present and verify cleanly, returns without hitting the
    network. Extracts the EULA text to ``EULA.txt`` and writes a ``manifest.json``.

    Called from container startup (#625) to warm the Setup Assistant EULA link,
    and from :func:`install_makemkv_from_sources` as a fallback when the tars are
    missing at install time.
    """
    resolved = version if version else fetch_latest_version()
    clean_version = _normalize_version(resolved)
    work = predownload_dir(clean_version)
    bin_tar = work / f"makemkv-bin-{clean_version}.tar.gz"
    oss_tar = work / f"makemkv-oss-{clean_version}.tar.gz"
    manifest_path = work / PREDOWNLOAD_MANIFEST_NAME
    eula_path = work / PREDOWNLOAD_EULA_NAME
    logs: List[str] = []

    if (
        bin_tar.exists()
        and oss_tar.exists()
        and _verify_tarball_gz(bin_tar, logs, log_cb=log_cb)
        and _verify_tarball_gz(oss_tar, logs, log_cb=log_cb)
    ):
        msg = f"MakeMKV sources already present at {work}; skipping download"
        logs.append(msg)
        if log_cb:
            log_cb(msg)
        return DownloadResult(
            version=clean_version, work_dir=work, bin_tar=bin_tar, oss_tar=oss_tar,
            eula_path=(eula_path if eula_path.exists() else None),
            already_present=True, logs=logs,
        )

    # Fetch each artifact independently, keeping any that already verifies.
    #
    # This used to delete BOTH tarballs whenever either was missing, then
    # refetch both. A partial success therefore threw away good bytes: on a
    # fresh install with makemkv.com down (2026-08-05), the bin tarball
    # downloaded from archive.org over ~2 minutes, then the oss fetch was
    # rate-limited — and the next attempt deleted the 18MB it had just
    # earned. That is exactly backwards when the network is the scarce
    # resource, which is precisely when this fallback is load-bearing.
    #
    # Only a file that is absent or fails verification is refetched, so an
    # install interrupted by a throttled archive resumes instead of
    # restarting.
    for tar, url_tpl in (
        (bin_tar, MAKEMKV_BIN_URL),
        (oss_tar, MAKEMKV_OSS_URL),
    ):
        if tar.exists() and _verify_tarball_gz(tar, logs, log_cb=log_cb):
            msg = f"Reusing already-downloaded {tar.name}"
            logs.append(msg)
            if log_cb:
                log_cb(msg)
            continue
        if tar.exists():
            try:
                tar.unlink()  # partial or corrupt — this one only
            except OSError:
                pass
        _download_with_fallback(
            url_tpl.format(version=clean_version), tar, logs, log_cb=log_cb
        )
    _verify_against_manifest(clean_version, [bin_tar, oss_tar], logs, log_cb=log_cb)

    have_eula = _extract_eula_text(bin_tar, oss_tar, eula_path, logs, log_cb=log_cb)

    manifest_path.write_text(json.dumps({
        "version": clean_version,
        "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "bin_tar": bin_tar.name,
        "oss_tar": oss_tar.name,
        "sha256_bin": _sha256_file(bin_tar),
        "sha256_oss": _sha256_file(oss_tar),
        "eula_file": PREDOWNLOAD_EULA_NAME if have_eula else None,
    }, indent=2), encoding="utf-8")

    msg = f"MakeMKV sources downloaded to {work}"
    logs.append(msg)
    if log_cb:
        log_cb(msg)

    return DownloadResult(
        version=clean_version, work_dir=work, bin_tar=bin_tar, oss_tar=oss_tar,
        eula_path=(eula_path if have_eula else None),
        already_present=False, logs=logs,
    )


def install_makemkv_from_sources(
    version: Optional[str],
    *,
    build_ffmpeg: bool = True,
    ffmpeg_advanced_features: bool = True,
    ffmpeg_version: Optional[str] = None,
    install_prefix: Optional[str] = None,
    work_dir: Optional[str] = None,
    use_sudo_install: bool = False,
    log_cb=None,
) -> dict:
    """
    Extract, build, and install MakeMKV from pre-fetched source tarballs.

    Calls :func:`download_makemkv_sources` (idempotent) to ensure the tars are
    present — pre-downloaded on container startup in the common case, fetched
    inline on the fallback path when startup pre-download failed or was skipped.
    """
    if install_prefix is None:
        # In Docker, install to /data so it persists across container restarts
        if os.path.exists('/.dockerenv'):
            install_prefix = "/data/mkvauto/makemkv"
        elif os.getuid() == 0:
            # Running as root outside Docker - use system-wide install
            install_prefix = "/usr/local"
        else:
            # Regular user - use local install
            install_prefix = str(_Path.home() / ".local" / "makemkv")

    # When no explicit version is requested (the Setup Assistant / updater send
    # none), install the version that was pre-downloaded at container startup
    # (#625) — the sources the Setup Assistant showed the EULA for — rather than
    # re-scraping the download pages here. A fresh scrape at install time can
    # resolve a *different, older* version than the pre-download (fetch_latest_version
    # falls back to a stale Wayback snapshot when the live pages fail), so the
    # installed binary would mismatch the pre-download and could be an expired
    # beta that then rejects a valid registration key. Fall back to a live scrape
    # (inside download_makemkv_sources) only when no pre-download is ready.
    if not version:
        try:
            from core import makemkv_predownload_state
            snap = makemkv_predownload_state.snapshot()
            if snap.get("state") == "ready" and snap.get("version"):
                version = snap["version"]
                log.info("MakeMKV install using pre-downloaded version %s", version)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "Could not read MakeMKV pre-download state; resolving version via scrape: %s",
                exc,
            )

    dl = download_makemkv_sources(version, log_cb=log_cb)
    clean_version = dl.version
    logs: List[str] = list(dl.logs)
    bin_tar = dl.bin_tar
    oss_tar = dl.oss_tar

    # An explicit pin (the version matrix testing one candidate) wins over
    # resolution; otherwise use the validated pairing, then the ceiling.
    if not build_ffmpeg:
        ffmpeg_version = None
    elif ffmpeg_version:
        log.info("Building against explicitly pinned ffmpeg %s", ffmpeg_version)
    else:
        ffmpeg_version = resolve_ffmpeg_for_build(clean_version)
    tmp_root = Path(tempfile.mkdtemp(prefix="makemkv-build-", dir=work_dir))
    ffmpeg_prefix = Path(tmp_root / "ffmpeg") if build_ffmpeg else None

    log.info(
        "MakeMKV install starting: version=%s install_prefix=%s build_ffmpeg=%s ffmpeg_advanced_features=%s work_dir=%s tmp_root=%s",
        clean_version,
        install_prefix,
        build_ffmpeg,
        ffmpeg_advanced_features,
        work_dir,
        tmp_root,
    )

    try:
        ffmpeg_tar = None
        if build_ffmpeg and ffmpeg_version:
            _check_build_deps(logs, log_cb=log_cb)
            ffmpeg_tar = tmp_root / f"ffmpeg-{ffmpeg_version}.tar.xz"
            _download(FFMPEG_URL_TEMPLATE.format(version=ffmpeg_version), ffmpeg_tar, logs, log_cb=log_cb)

        # extract (source tars are pre-fetched; only build artifacts are transient)
        oss_dir = _extract(oss_tar, tmp_root, logs, log_cb=log_cb)
        bin_dir = _extract(bin_tar, tmp_root, logs, log_cb=log_cb)
        ffmpeg_dir = _extract(ffmpeg_tar, tmp_root, logs, log_cb=log_cb) if ffmpeg_tar else None

        # accept EULA for OSS and BIN builds to avoid interactive prompt
        _accept_eula(oss_dir, logs, log_cb=log_cb)
        _accept_eula(bin_dir, logs, log_cb=log_cb)

        env = os.environ.copy()
        ldconfig_path = shutil.which("ldconfig")
        if not ldconfig_path:
            true_path = shutil.which("true") or "/bin/true" or "/usr/bin/true"
            ldconfig_path = true_path
            note = f"ldconfig not found; using {ldconfig_path} to skip linker cache update"
            logs.append(note)
            if log_cb:
                log_cb(note)
        env["LDCONFIG"] = ldconfig_path
        env["ACCEPT_EULA"] = "yes"
        jobs = str(os.cpu_count() or 2)

        if ffmpeg_dir and ffmpeg_prefix:
            logs.append(f"Building ffmpeg {ffmpeg_version}")
            if log_cb:
                log_cb(f"Building ffmpeg {ffmpeg_version} - this may take 5-10 minutes...")
            ffmpeg_install = ffmpeg_prefix / "install"
            ffmpeg_install.mkdir(parents=True, exist_ok=True)
            has_nasm = shutil.which("nasm") is not None
            base_config = [
                "./configure",
                f"--prefix={ffmpeg_install}",
                "--enable-static",
                "--disable-shared",
                "--enable-pic",
            ]
            asm_flags = []
            if not has_nasm:
                note = "nasm not found; configuring ffmpeg with --disable-x86asm (slower)"
                logs.append(note)
                if log_cb:
                    log_cb(note)
                asm_flags = ["--disable-x86asm"]
            if ffmpeg_advanced_features:
                # Try advanced features (--enable-nonfree, --enable-libfdk-aac)
                fdk_flags = ["--enable-nonfree", "--enable-libfdk-aac"]
                try:
                    _run(
                        base_config + asm_flags + fdk_flags,
                        cwd=ffmpeg_dir,
                        env=env,
                        logs=logs,
                        log_cb=log_cb,
                    )
                except MakeMKVUpdateError as exc:
                    if "libfdk_aac" in str(exc).lower():
                        warn = "libfdk_aac not found; retrying ffmpeg configure without fdk-aac (using built-in AAC)"
                        logs.append(warn)
                        if log_cb:
                            log_cb(warn)
                        _run(
                            base_config + asm_flags,
                            cwd=ffmpeg_dir,
                            env=env,
                            logs=logs,
                            log_cb=log_cb,
                        )
                    elif "nasm" in str(exc).lower() and "--disable-x86asm" not in asm_flags:
                        warn = "nasm missing; retrying ffmpeg configure with --disable-x86asm"
                        logs.append(warn)
                        if log_cb:
                            log_cb(warn)
                        asm_flags = ["--disable-x86asm"]
                        _run(
                            base_config + asm_flags + fdk_flags,
                            cwd=ffmpeg_dir,
                            env=env,
                            logs=logs,
                            log_cb=log_cb,
                        )
                    else:
                        raise
            else:
                # Skip advanced features, use base config only
                _run(
                    base_config + asm_flags,
                    cwd=ffmpeg_dir,
                    env=env,
                    logs=logs,
                    log_cb=log_cb,
                )
            _run(["make", f"-j{jobs}"], cwd=ffmpeg_dir, env=env, logs=logs, log_cb=log_cb)
            install_cmd = ["make", "install"]
            if use_sudo_install:
                install_cmd = ["sudo"] + install_cmd
            _run(install_cmd, cwd=ffmpeg_dir, env=env, logs=logs, log_cb=log_cb)

            env["PKG_CONFIG_PATH"] = str(ffmpeg_install / "lib/pkgconfig")

        # build OSS portion
        configure_cmd = ["./configure"]
        if install_prefix:
            configure_cmd.append(f"--prefix={install_prefix}")
        _run(configure_cmd, cwd=oss_dir, env=env, logs=logs, log_cb=log_cb)
        _accept_eula(oss_dir, logs, log_cb=log_cb)  # reinforce before make
        _run(["make", f"-j{jobs}"], cwd=oss_dir, env=env, logs=logs, log_cb=log_cb)
        install_cmd = ["make", "install"]
        if use_sudo_install:
            install_cmd = ["sudo"] + install_cmd
        _run(install_cmd, cwd=oss_dir, env=env, logs=logs, log_cb=log_cb)

        # build BIN portion (does not depend on env)
        bin_env = env.copy()
        _accept_eula(bin_dir, logs, log_cb=log_cb)  # reinforce before make
        _run(["make", f"-j{jobs}"], cwd=bin_dir, env=bin_env, logs=logs, log_cb=log_cb)
        
        # Install BIN portion with PREFIX override
        # The makemkv-bin Makefile uses PREFIX variable (default /usr) which we override here
        bin_install_cmd = ["make", "install"]
        if install_prefix and install_prefix != "/usr":
            bin_install_cmd.append(f"PREFIX={install_prefix}")
            msg = f"Installing BIN with PREFIX={install_prefix}"
            logs.append(msg)
            if log_cb:
                log_cb(msg)
        if use_sudo_install:
            bin_install_cmd = ["sudo"] + bin_install_cmd
        _run(bin_install_cmd, cwd=bin_dir, env=bin_env, logs=logs, log_cb=log_cb)

        # Create symlinks to standard system paths in Docker/root environments
        if os.path.exists('/.dockerenv') or (os.getuid() == 0 and install_prefix != "/usr/local"):
            msg = f"Creating symlinks to standard system paths for prefix: {install_prefix}"
            logs.append(msg)
            if log_cb:
                log_cb(msg)
            _create_system_symlinks(install_prefix, use_sudo_install, logs, log_cb)

        invalidate_version_cache()  # Force rescan after install
        current = get_installed_version(force_refresh=True)
        if not current:
            meta = get_makemkvcon_metadata()
            raise MakeMKVUpdateError(
                "Unable to determine installed MakeMKV version after update. "
                f"Binary path={meta.get('binary_path')}, resolved={meta.get('resolved_path')}"
            )
        if current != clean_version:
            meta = get_makemkvcon_metadata()
            raise MakeMKVUpdateError(
                "MakeMKV update completed but detected version does not match expected. "
                f"expected={clean_version} detected={current} "
                f"binary_path={meta.get('binary_path')} resolved={meta.get('resolved_path')}"
            )
        return {
            "version": current,
            "logs": logs,
            "ffmpeg_built": bool(ffmpeg_dir),
        }
    except Exception:
        for line in logs:
            log.error("update log: %s", line)
        raise
    finally:
        # transient extract dir; source tars persist in dl.work_dir
        shutil.rmtree(tmp_root, ignore_errors=True)


def update_makemkv(
    version: Optional[str],
    *,
    build_ffmpeg: bool = True,
    ffmpeg_advanced_features: bool = True,
    ffmpeg_version: Optional[str] = None,
    install_prefix: Optional[str] = None,
    work_dir: Optional[str] = None,
    use_sudo_install: bool = False,
    log_cb=None,
) -> dict:
    """
    Download and compile the requested MakeMKV release.

    Thin wrapper around :func:`install_makemkv_from_sources` (which itself
    calls :func:`download_makemkv_sources` — idempotent, skips fetching when
    the versioned tarballs are already cached under ``MKVAUTO_TMP_DIR``).
    Signature preserved so existing callers (``start_update_job``, tests,
    ``mkv test makemkv``) keep working.
    """
    return install_makemkv_from_sources(
        version,
        build_ffmpeg=build_ffmpeg,
        ffmpeg_advanced_features=ffmpeg_advanced_features,
        install_prefix=install_prefix,
        work_dir=work_dir,
        use_sudo_install=use_sudo_install,
        log_cb=log_cb,
    )
