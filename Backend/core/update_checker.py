"""Release update check (#699).

Compares the running image's version (``MKVAUTO_VERSION``, baked in by the
Dockerfile) against the newest published GitHub Release of the public
``mkv-auto-release`` repo — the release pipeline publishes that Release
atomically with every image push, so it is the authoritative "latest" oracle.

Privacy posture: this module only talks to the network when
:func:`get_update_status` is called, and the API only calls it when a browser
actively requests ``/system/update-status``. The backend never phones home on
its own — no timers, no background polling.

Failure posture: any network / parse problem degrades to "no update info"
(``update_available: False`` with ``latest_version: None``). The check must
never break or slow the UI beyond its own request.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

RELEASES_LATEST_URL = (
    "https://api.github.com/repos/MKV-Auto/mkv-auto-release/releases/latest"
)
REQUEST_TIMEOUT_SECONDS = 5
CACHE_TTL_SECONDS = 6 * 3600

_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

# In-process cache: {"checked_at_monotonic": float, "result": dict}
_cache: Dict[str, Any] = {"checked_at_monotonic": 0.0, "result": None}


def get_current_version() -> str:
    """The running app version as baked into the image ("dev" outside Docker)."""
    return os.environ.get("MKVAUTO_VERSION", "dev").strip() or "dev"


def _parse_semver(value: Optional[str]) -> Optional[Tuple[int, int, int]]:
    m = _SEMVER_RE.match((value or "").strip())
    if not m:
        return None
    return tuple(int(g) for g in m.groups())  # type: ignore[return-value]


def reset_cache() -> None:
    """Test hook: forget any cached check result."""
    _cache["checked_at_monotonic"] = 0.0
    _cache["result"] = None


def get_update_status(force: bool = False) -> Dict[str, Any]:
    """Return the update status, consulting GitHub at most once per TTL.

    Shape::

        {
          "current_version": "1.0.1",
          "latest_version": "1.0.2" | None,
          "update_available": bool,
          "release_url": str | None,
          "release_name": str | None,
          "published_at": str | None,
          "checked_at": ISO-8601 str,
        }
    """
    now = time.monotonic()
    cached = _cache["result"]
    if not force and cached is not None and (now - _cache["checked_at_monotonic"]) < CACHE_TTL_SECONDS:
        return cached

    current = get_current_version()
    result: Dict[str, Any] = {
        "current_version": current,
        "latest_version": None,
        "update_available": False,
        "release_url": None,
        "release_name": None,
        "published_at": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    current_tuple = _parse_semver(current)
    if current_tuple is None:
        # Dev / unstamped build: never phone out, never nag.
        _cache["checked_at_monotonic"] = now
        _cache["result"] = result
        return result

    try:
        resp = requests.get(
            RELEASES_LATEST_URL,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"Accept": "application/vnd.github+json"},
        )
        resp.raise_for_status()
        data = resp.json()
        tag = (data.get("tag_name") or "").strip()
        latest_tuple = _parse_semver(tag)
        if latest_tuple is not None:
            result["latest_version"] = tag.lstrip("v")
            result["release_url"] = data.get("html_url")
            result["release_name"] = data.get("name")
            result["published_at"] = data.get("published_at")
            result["update_available"] = latest_tuple > current_tuple
    except Exception as exc:  # noqa: BLE001 — degrade to "no info", never raise
        logger.debug("update check failed (degrading to no-info): %s", exc)

    _cache["checked_at_monotonic"] = now
    _cache["result"] = result
    return result
