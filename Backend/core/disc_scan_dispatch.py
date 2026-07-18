"""Cache-precondition gate for rip-start (#562 PR 5).

Before enqueueing a rip, the request path verifies the disc-info cache
holds the minimum fields the rip task needs. On a miss, enqueue a scoped
``discinfo_scan`` Celery task and surface a 409 to the caller so the UI
can retry shortly. This stops the rip task from opening the disc inline
on cache miss — that path was how MSG:5010 surfaced on a sibling drive's
in-flight rip.
"""

from __future__ import annotations

from typing import Optional

from core.disc_cache import get as cache_get
from core.logging_utils import get_logger

logger = get_logger(__name__)


MINIMUM_CACHED_FIELDS: tuple[str, ...] = ("disc_hash",)


def disc_info_cache_satisfies(
    mount_point: Optional[str],
    disc_num: Optional[str],
    disc_payload: Optional[dict],
) -> bool:
    """Return True if the rip task can proceed without a scan.

    Order of preference:
      1. ``disc_payload`` from the request (frontend already had the data)
      2. ``disc_cache`` keyed by ``mount_point`` (primary key)
      3. ``disc_cache`` keyed by ``disc_num`` (alias, kept for compat)

    A payload counts only when it carries every field in
    :data:`MINIMUM_CACHED_FIELDS` — currently just ``disc_hash``. The rip
    task pulls richer fields (titles / tracks / info_log) but those are
    derivable; missing them is recoverable. Missing ``disc_hash`` means
    we never scanned the disc at all, which is the failure mode the gate
    is here to catch.
    """

    if disc_payload and all(disc_payload.get(k) for k in MINIMUM_CACHED_FIELDS):
        return True
    if mount_point:
        cached = cache_get(mount_point)
        if cached and all(cached.get(k) for k in MINIMUM_CACHED_FIELDS):
            return True
    if disc_num:
        cached = cache_get(str(disc_num))
        if cached and all(cached.get(k) for k in MINIMUM_CACHED_FIELDS):
            return True
    return False


def enqueue_discinfo_scan(disc_num: str, mount_point: str) -> Optional[str]:
    """Dispatch the ``discinfo_scan`` Celery task; return the task id.

    Returns ``None`` if dispatch failed (broker unreachable, import error).
    Callers should still return 409 in that case — a missing scan is no
    less missing if we couldn't enqueue one — but they may want to log
    the failure for operator follow-up.
    """

    try:
        # Import lazily so this module stays usable from request paths
        # whose imports we do not want to drag the Celery client into.
        from workers.tasks import discinfo_scan

        result = discinfo_scan.apply_async(
            args=[str(disc_num), mount_point], queue="celery"
        )
        return result.id
    except Exception as exc:
        logger.warning(
            "discinfo_scan dispatch failed disc_num=%s mount=%s: %s",
            disc_num,
            mount_point,
            exc,
        )
        return None
