"""The validated MakeMKV x FFmpeg version manifest.

What this is
------------
The installer resolves its build inputs at run time, which made every
install hostage to upstream's release schedule: FFmpeg 9.0 shipped on
2026-08-03, removed AVCodec fields MakeMKV's libffabi still uses, and
broke every fresh install that day with no change on our side.

The fix is to stop guessing. CI builds each MakeMKV version against
candidate FFmpeg versions, and only a pair that *compiled and produced a
working binary* is published. This module is the client half: it reads
that published manifest and answers three questions.

  1. Which MakeMKV version may we offer? (only validated ones — an
     unvalidated version does not exist as far as the updater is
     concerned, so gating is structural rather than a policy check)
  2. Which FFmpeg must we build it against?
  3. What should the downloaded bytes hash to?

Because a hash is only published after a successful build + smoke test,
verifying against it proves more than "not corrupted": it proves these
exact bytes produced a working makemkvcon. That is also what finally
makes the archive.org fallback safe — a capture is otherwise trusted on
faith.

Canonical bytes
---------------
Hashes describe the artifact **as the vendor serves it**. archive.org
serves the tarballs double-gzipped, so the fallback path normalizes
(``_unwrap_double_gzip_if_needed``) before the hash is meaningful. Both
download paths must reach the same canonical form before comparing, or
half the fallback downloads fail verification for what looks like
corruption but is only packaging.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

import requests

log = logging.getLogger("core.makemkv_manifest")

SCHEMA_VERSION = 1

# Public, unauthenticated, CDN-backed. raw.githubusercontent honours
# If-None-Match with a 304, so the daily poll is a header exchange rather
# than a download — and it needs no git clone. (The Releases API would
# rate-limit unauthenticated callers at 60/hour per IP, which collides for
# users behind a shared NAT.)
DEFAULT_MANIFEST_URL = (
    "https://raw.githubusercontent.com/MKV-Auto/mkv-auto-release/"
    "data/makemkv-versions.json"
)

MANIFEST_FETCH_TIMEOUT = 15


def manifest_url() -> str:
    return os.getenv("MKVAUTO_MAKEMKV_MANIFEST_URL", DEFAULT_MANIFEST_URL).strip()


# ── Reading the manifest ────────────────────────────────────────────────────

def validated_versions(manifest: Optional[dict]) -> list[str]:
    """MakeMKV versions with a proven-good FFmpeg pairing, newest first."""
    entries = ((manifest or {}).get("validated") or {})
    return sorted(entries.keys(), key=_version_key, reverse=True)


def latest_validated(manifest: Optional[dict]) -> Optional[str]:
    """The newest MakeMKV version we may offer as an update."""
    explicit = (manifest or {}).get("latest_validated")
    if isinstance(explicit, str) and explicit in ((manifest or {}).get("validated") or {}):
        return explicit
    versions = validated_versions(manifest)
    return versions[0] if versions else None


def ffmpeg_for(manifest: Optional[dict], makemkv_version: str) -> Optional[str]:
    """The FFmpeg version CI proved this MakeMKV version builds against."""
    entry = ((manifest or {}).get("validated") or {}).get(makemkv_version)
    if not isinstance(entry, dict):
        return None
    value = entry.get("ffmpeg_version")
    return value if isinstance(value, str) and value else None


def expected_sha256(
    manifest: Optional[dict], makemkv_version: str, filename: str
) -> Optional[str]:
    """Published hash for one artifact of a validated pair, if known.

    Absence means "we have not certified this file", which callers must
    treat as *unverifiable*, never as *verified*.
    """
    entry = ((manifest or {}).get("validated") or {}).get(makemkv_version)
    if not isinstance(entry, dict):
        return None
    digest = (entry.get("sha256") or {}).get(filename)
    return digest if isinstance(digest, str) and digest else None


def incompatibility_note(
    manifest: Optional[dict], makemkv_version: str
) -> Optional[str]:
    """Why a version is not on offer, for the UI to show.

    A user whose MakeMKV silently never updates cannot tell our gate apart
    from a broken updater. Publishing the negative results lets the app say
    which version exists and why it is being held back.
    """
    for row in ((manifest or {}).get("known_incompatible") or []):
        if not isinstance(row, dict):
            continue
        if row.get("makemkv_version") != makemkv_version:
            continue
        ffmpeg = row.get("ffmpeg_version") or "every FFmpeg version tested"
        reason = row.get("reason") or "build_failed"
        return (
            f"MakeMKV {makemkv_version} is available but does not build against "
            f"{ffmpeg} ({reason}). Holding it back until a working combination "
            f"is validated."
        )
    return None


def is_stale(manifest: Optional[dict], max_age_days: int = 14) -> bool:
    """True when the manifest is old enough to be suspicious.

    Gating updates on CI means a stalled pipeline silently freezes users on
    an old MakeMKV — and MakeMKV betas expire. A stale manifest is itself a
    signal worth surfacing rather than trusting quietly.
    """
    generated = (manifest or {}).get("generated_at")
    if not isinstance(generated, str):
        return True
    try:
        from datetime import datetime, timezone
        ts = datetime.fromisoformat(generated.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - ts).total_seconds() / 86400
        return age_days > max_age_days
    except (ValueError, TypeError):
        return True


def _version_key(version: str) -> tuple:
    try:
        return tuple(int(p) for p in str(version).split("."))
    except (TypeError, ValueError):
        return (0,)


# ── Fetching + caching ──────────────────────────────────────────────────────

def _cache_paths(cache_dir: Path) -> tuple[Path, Path]:
    return cache_dir / "makemkv-versions.json", cache_dir / "makemkv-versions.etag"


def load_cached(cache_dir: Path) -> Optional[dict]:
    """Last manifest we successfully fetched, or the copy baked into the image."""
    body_path, _ = _cache_paths(cache_dir)
    for candidate in (body_path, Path(__file__).resolve().parent.parent / "data" / "makemkv-versions.json"):
        try:
            if candidate.exists():
                return json.loads(candidate.read_text())
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable manifest %s: %s", candidate, exc)
    return None


def fetch_manifest(
    cache_dir: Path,
    *,
    url: Optional[str] = None,
    timeout: int = MANIFEST_FETCH_TIMEOUT,
) -> tuple[Optional[dict], str]:
    """Fetch the manifest, using a conditional GET.

    Returns ``(manifest, status)`` where status is one of ``fresh`` (200 and
    stored), ``unchanged`` (304, cached copy reused), ``cached`` (network
    failed, cached/baked copy reused) or ``unavailable`` (nothing to use).

    Never raises on network trouble: being unable to reach GitHub must not
    break an install that is otherwise fine. It only means we learn about no
    new versions, which is the safe direction for a gate.
    """
    target = url or manifest_url()
    body_path, etag_path = _cache_paths(cache_dir)
    headers = {"Accept": "application/json"}
    try:
        if etag_path.exists():
            etag = etag_path.read_text().strip()
            if etag:
                headers["If-None-Match"] = etag
    except OSError:
        pass

    try:
        resp = requests.get(target, headers=headers, timeout=timeout)
    except Exception as exc:
        log.warning("Manifest fetch failed (%s); using cached copy", exc)
        cached = load_cached(cache_dir)
        return cached, ("cached" if cached else "unavailable")

    if resp.status_code == 304:
        cached = load_cached(cache_dir)
        return cached, ("unchanged" if cached else "unavailable")

    if resp.status_code != 200:
        log.warning("Manifest fetch returned HTTP %s; using cached copy", resp.status_code)
        cached = load_cached(cache_dir)
        return cached, ("cached" if cached else "unavailable")

    try:
        manifest = resp.json()
    except ValueError as exc:
        log.warning("Manifest was not JSON (%s); using cached copy", exc)
        cached = load_cached(cache_dir)
        return cached, ("cached" if cached else "unavailable")

    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA_VERSION:
        # A newer schema than we understand is not an error on the server's
        # part — it means this build is old. Keep using what we know rather
        # than misreading fields.
        log.warning(
            "Manifest schema %s is not the expected %s; using cached copy",
            (manifest or {}).get("schema") if isinstance(manifest, dict) else "?",
            SCHEMA_VERSION,
        )
        cached = load_cached(cache_dir)
        return cached, ("cached" if cached else "unavailable")

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        body_path.write_text(json.dumps(manifest, indent=2) + "\n")
        etag = resp.headers.get("ETag")
        if etag:
            etag_path.write_text(etag)
    except OSError as exc:
        log.warning("Could not cache manifest: %s", exc)

    return manifest, "fresh"


def poll_jitter_seconds(max_seconds: int = 3600) -> int:
    """Stable per-install offset for the daily poll.

    Every container waking at the same minute to hit one URL is a
    self-inflicted thundering herd. Derived from the host identity so an
    install keeps its slot instead of re-rolling on each restart.
    """
    import hashlib
    seed = f"{os.getenv('HOSTNAME', '')}{os.getenv('MKVAUTO_ROOT', '')}".encode()
    if not seed.strip():
        seed = str(time.time()).encode()
    return int(hashlib.sha256(seed).hexdigest()[:8], 16) % max(1, max_seconds)
