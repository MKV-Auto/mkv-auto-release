"""OS-level drive registry: authoritative drive list + media presence.

Replaces ``info disc:9999`` as the source of truth for "which drives exist and
which have media loaded?". Aggregates four OS primitives behind a single
TTL-cached facade:

  - ``/sys/block/sr*``        — drive enumeration
  - ``/dev/disk/by-id/``      — stable hardware identity (via drive_identity)
  - ``sg_turs`` (+ BLKGETSIZE64 fallback) — media-presence flag
  - ``udevadm info -q property -n /dev/srN`` — volume label + media kind

The MakeMKV engine is never invoked here. Callers that need per-disc detail
still scope to ``info dev:{mount_point}`` separately; the registry just
answers which mount_points exist and which carry media — without touching
any drive that may be mid-rip.

See GitHub issue #562 for the migration plan that retires the ``disc:9999``
hot path. This module is PR 1 of that cluster — pure addition, zero callers.
"""

from __future__ import annotations

import glob
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Literal, Optional

from core.drive_identity import DriveIdentity, resolve_drive_identity

logger = logging.getLogger(__name__)

MediaKind = Literal["BD", "DVD", "CD", "unknown"]

UDEVADM_BIN = "udevadm"
UDEVADM_TIMEOUT_SECONDS = 2.0
DEFAULT_TTL_SECONDS = 2.0


@dataclass(frozen=True)
class DriveSnapshot:
    """Point-in-time snapshot of one optical drive.

    ``loaded`` reflects sg_turs / BLKGETSIZE64 — independent of MakeMKV's
    view. ``volume_label`` and ``media_kind`` come from udev properties;
    both are advisory (used for UI/diagnostics, never for control flow).
    ``identity`` is the same DriveIdentity the gatekeeper and swap detector
    use. ``observed_at`` is a monotonic timestamp suitable for staleness
    comparisons but not for wall-clock display.
    """

    mount_point: str
    loaded: bool
    volume_label: Optional[str]
    media_kind: Optional[MediaKind]
    identity: DriveIdentity
    udev_state: dict[str, str]
    observed_at: float


_lock = threading.Lock()
_cached_snapshots: Optional[list[DriveSnapshot]] = None
_cached_ts: float = 0.0


def snapshot_drives(
    *, force: bool = False, ttl_seconds: float = DEFAULT_TTL_SECONDS
) -> list[DriveSnapshot]:
    """Return a snapshot of every optical drive currently visible to the host.

    Coalesces concurrent callers under ``_lock`` — within a single TTL window
    the underlying OS calls run at most once. ``force=True`` bypasses the
    cache (used by startup warmup and udev event handlers).
    """
    global _cached_snapshots, _cached_ts
    with _lock:
        now = time.monotonic()
        if (
            not force
            and _cached_snapshots is not None
            and (now - _cached_ts) < ttl_seconds
        ):
            return list(_cached_snapshots)

        snapshots = _build_snapshots()
        _cached_snapshots = snapshots
        _cached_ts = now
        return list(snapshots)


def get_snapshot_for_mount(
    mount_point: str, *, force: bool = False
) -> Optional[DriveSnapshot]:
    """Return the snapshot for one ``/dev/srN`` (or ``None`` if absent)."""
    for snap in snapshot_drives(force=force):
        if snap.mount_point == mount_point:
            return snap
    return None


def loaded_drives() -> list[DriveSnapshot]:
    """Convenience filter: only drives with media currently inserted."""
    return [snap for snap in snapshot_drives() if snap.loaded]


def invalidate() -> None:
    """Drop the cache. Called by udev event handlers after add/change/remove."""
    global _cached_snapshots, _cached_ts
    with _lock:
        _cached_snapshots = None
        _cached_ts = 0.0


def _enumerate_devices() -> list[str]:
    """Return sorted ``/dev/sr*`` paths. Indirected for test monkeypatching."""
    return sorted(glob.glob("/dev/sr*"))


def _media_present(dev: str) -> bool:
    """Return True if media is loaded in ``dev``. Indirected for testing.

    Lazy import of ``core.utils`` avoids a circular import at module load —
    utils.py already imports drive_identity heavily and we don't want to
    fight the boot-order graph.
    """
    from core.utils import _drive_has_media

    return _drive_has_media(dev)


def _resolve_identity(mount_point: str) -> DriveIdentity:
    """Indirected for testing."""
    return resolve_drive_identity(mount_point)


def _run_udevadm(dev: str) -> dict[str, str]:
    """Parse ``udevadm info -q property -n <dev>`` into a ``{KEY: value}`` dict.

    Returns an empty dict on any failure — udev metadata is advisory, not
    load-bearing. The registry must still produce a snapshot even if udev is
    unavailable (test environments, minimal containers).
    """
    try:
        proc = subprocess.run(
            [UDEVADM_BIN, "info", "-q", "property", "-n", dev],
            capture_output=True,
            text=True,
            timeout=UDEVADM_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("drive_registry: udevadm failed for %s: %s", dev, exc)
        return {}

    if proc.returncode != 0:
        return {}

    result: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key:
            result[key] = value
    return result


def _media_kind_from_udev(udev: dict[str, str]) -> Optional[MediaKind]:
    if udev.get("ID_CDROM_MEDIA_BD") == "1":
        return "BD"
    if udev.get("ID_CDROM_MEDIA_DVD") == "1":
        return "DVD"
    if udev.get("ID_CDROM_MEDIA_CD") == "1":
        return "CD"
    if udev.get("ID_CDROM_MEDIA") == "1":
        return "unknown"
    return None


def _volume_label_from_udev(udev: dict[str, str]) -> Optional[str]:
    label = udev.get("ID_FS_LABEL") or udev.get("ID_FS_LABEL_ENC")
    return label or None


def _build_snapshots() -> list[DriveSnapshot]:
    devices = _enumerate_devices()
    now = time.monotonic()
    snapshots: list[DriveSnapshot] = []
    for dev in devices:
        loaded = _media_present(dev)
        try:
            identity = _resolve_identity(dev)
        except Exception as exc:
            logger.warning(
                "drive_registry: identity resolution failed for %s: %s", dev, exc
            )
            identity = DriveIdentity(
                by_id_serial=f"unknown:{os.path.basename(dev)}",
                vendor="",
                model="",
                bus="unknown",
                by_id_name="",
                hardware_name=None,
                identity_source="unknown",
            )
        udev = _run_udevadm(dev)
        snapshots.append(
            DriveSnapshot(
                mount_point=dev,
                loaded=loaded,
                volume_label=_volume_label_from_udev(udev),
                media_kind=_media_kind_from_udev(udev),
                identity=identity,
                udev_state=udev,
                observed_at=now,
            )
        )
    return snapshots
