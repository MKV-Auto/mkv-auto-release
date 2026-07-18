"""
Process-wide state for the MakeMKV source pre-download that runs on container
startup (#625). The Setup Assistant's ⚠️ License Terms bullet links to the
extracted EULA text — the frontend polls ``GET /system/makemkv/health`` and
reads ``download`` from this module to know whether the link is ready.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Literal, Optional

DownloadState = Literal["missing", "downloading", "ready", "failed"]


@dataclass
class PredownloadState:
    state: DownloadState = "missing"
    version: Optional[str] = None
    downloaded_at: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


_lock = threading.Lock()
_state: PredownloadState = PredownloadState()


def snapshot() -> dict:
    """Return a copy of the current state as a plain dict — safe for API responses."""
    with _lock:
        return _state.as_dict()


def initialize_from_disk() -> None:
    """
    Populate initial state from any existing manifest.json on disk. Called once at
    process startup so a container restart with cached tars begins in ``ready``
    without waiting for the background download hook to fire.
    """
    from core.makemkv_updater import read_predownload_manifest

    manifest = read_predownload_manifest()
    if not manifest:
        return
    with _lock:
        _state.state = "ready"
        _state.version = manifest.get("version")
        _state.downloaded_at = manifest.get("downloaded_at")
        _state.error = None


def mark_downloading(version: Optional[str]) -> None:
    with _lock:
        _state.state = "downloading"
        _state.version = version
        _state.error = None
        _state.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def mark_ready(version: str, downloaded_at: Optional[str] = None) -> None:
    with _lock:
        _state.state = "ready"
        _state.version = version
        _state.downloaded_at = downloaded_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _state.error = None


def mark_failed(error: str) -> None:
    with _lock:
        _state.state = "failed"
        _state.error = error


def reset_for_test() -> None:
    """Test-only helper — restore singleton to its initial state."""
    global _state
    with _lock:
        _state = PredownloadState()


def run_predownload_if_needed(
    *,
    validation_fn=None,
    download_fn=None,
    logger=None,
) -> None:
    """
    Synchronous helper wrapping the startup pre-download policy:

    - Skip when MakeMKV is already installed (nothing to warm — the Setup
      Assistant only shows the EULA link in the not-installed phase).
    - Skip when the singleton already reports ``ready`` (initialize_from_disk
      picked up a cached manifest).
    - Otherwise mark the state ``downloading``, invoke ``download_fn``, and
      transition to ``ready`` / ``failed`` based on the result.

    Injectable ``validation_fn`` / ``download_fn`` keep this unit-testable
    without importing the whole updater module.
    """
    if validation_fn is None:
        from core.makemkv_updater import validate_makemkv_installation as validation_fn  # type: ignore[assignment]
    if download_fn is None:
        from core.makemkv_updater import download_makemkv_sources as download_fn  # type: ignore[assignment]

    validation = validation_fn()
    if validation.get("is_valid"):
        if logger:
            logger.info("MakeMKV already installed — skipping source pre-download")
        return

    current = snapshot()
    if current.get("state") == "ready":
        if logger:
            logger.info(
                "MakeMKV sources already cached (%s) — no pre-download needed",
                current.get("version"),
            )
        return

    mark_downloading(current.get("version"))
    try:
        result = download_fn()
    except Exception as exc:
        mark_failed(str(exc))
        if logger:
            logger.warning(
                "MakeMKV pre-download failed; Setup Assistant will fall back to inline download: %s",
                exc,
            )
        return

    mark_ready(getattr(result, "version", None) or (result.get("version") if isinstance(result, dict) else None))
    if logger:
        already = getattr(result, "already_present", False)
        logger.info(
            "MakeMKV pre-download %s (version=%s)",
            "hit cache" if already else "complete",
            getattr(result, "version", None),
        )
