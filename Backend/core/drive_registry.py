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
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, replace
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

# ---- probe deadline + not-responding cooldown (#862) ---------------------
# A wedged drive leaves probe children in unkillable D-state; even
# subprocess timeouts hang, because TimeoutExpired's cleanup kill()+wait()s
# a process the kernel will not release. Probes therefore run on a small
# worker pool with a hard result deadline — on timeout the caller ABANDONS
# the stuck call (the thread stays blocked until the kernel releases it;
# bounded by the cooldown below, which stops re-probing that device).
# During the 2026-09-06 outage the missing deadline let one dead drive
# pin every uvicorn worker within minutes.
_PROBE_TIMEOUT_SECONDS = float(os.environ.get("DRIVE_PROBE_TIMEOUT_SECONDS", "3.0"))
_PROBE_COOLDOWN_SECONDS = float(os.environ.get("DRIVE_PROBE_COOLDOWN_SECONDS", "30.0"))
_LOCK_WAIT_SECONDS = 2.0
_probe_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="drive-probe")
_unresponsive_until: dict[str, float] = {}
_last_known: dict[str, DriveSnapshot] = {}


def snapshot_drives(
    *, force: bool = False, ttl_seconds: float = DEFAULT_TTL_SECONDS
) -> list[DriveSnapshot]:
    """Return a snapshot of every optical drive currently visible to the host.

    Coalesces concurrent callers under ``_lock`` — within a single TTL window
    the underlying OS calls run at most once. ``force=True`` bypasses the
    cache (used by startup warmup and udev event handlers).
    """
    global _cached_snapshots, _cached_ts
    # Bounded lock wait (#862): if another caller is stuck mid-probe, serve
    # the last snapshot (stale beats a pinned thread — during the 2026-09-06
    # outage every waiter here held a uvicorn worker until the pool died).
    if not _lock.acquire(timeout=_LOCK_WAIT_SECONDS):
        logger.warning(
            "drive_registry: snapshot lock busy for %.1fs — serving stale snapshot",
            _LOCK_WAIT_SECONDS,
        )
        return list(_cached_snapshots) if _cached_snapshots is not None else []
    try:
        now = time.monotonic()
        if force:
            # A forced refresh (startup warmup, udev event) means something
            # believes the hardware is talking again — retry cooled-down
            # devices immediately.
            _unresponsive_until.clear()
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
    finally:
        _lock.release()


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


def _enumerate_scsi_generic() -> list[str]:
    """Return sorted ``/dev/sg*`` paths. Indirected for test monkeypatching."""
    return sorted(glob.glob("/dev/sg*"))


def _enumerate_host_scsi_generic() -> list[str]:
    """SCSI generic devices the *kernel* knows about, from sysfs.

    This is the host's view, not ours. ``/sys`` is the host's sysfs even in a
    container that was given no ``/dev/sg*`` nodes and no privileges — verified
    on an unprivileged container that saw ``sg0 sg1 sg2`` here while its own
    ``/dev/sg*`` was empty.

    That gap is the whole diagnosis. Nothing here means the host has no SCSI
    generic support at all (the ``sg`` module is not loaded). Entries here with
    no matching ``/dev/sg*`` means the host is fine and the container simply
    was not given the nodes.
    """
    try:
        return sorted(os.listdir("/sys/class/scsi_generic"))
    except OSError:
        return []


def scsi_generic_missing() -> bool:
    """True when optical drives exist but no SCSI generic node does.

    MakeMKV enumerates optical drives through SCSI generic, not through
    ``/dev/sr*``, so with no ``/dev/sg*`` it reports zero usable drives.
    Conservative by design: False when there are no optical drives at all
    (nothing to diagnose) and False when any ``sg`` node exists.
    """
    if not _enumerate_devices():
        return False
    return not _enumerate_scsi_generic()


def diagnose_no_drives_environment() -> tuple[str, str]:
    """Why might MakeMKV see no drives here? Returns ``(reason, detail)``.

    Ordered cheapest-and-most-decisive first. ``reason`` is a stable slug for
    callers and tests; ``detail`` is a human sentence naming what was observed.

    The reason this enumerates causes rather than asserting one: the first
    report of #802 looked exactly like a missing ``sg`` kernel module, and it
    was not — the reporter's host had ``sg`` loaded and all three ``/dev/sg*``
    nodes were visible inside the container. A probe that bets on one cause
    gives confidently wrong advice. This one reports what it can actually see
    and says ``unknown`` when that is the honest answer.
    """
    try:
        sr_nodes = _enumerate_devices()
        sg_nodes = _enumerate_scsi_generic()
        host_sg = _enumerate_host_scsi_generic()
    except Exception as exc:  # noqa: BLE001 - diagnosis must never raise
        logger.debug("drive environment probe failed: %s", exc)
        return "unknown", "The drive environment could not be inspected."

    # Order matters. "Nothing at all" first, then the host/container split
    # before "no sg nodes" — those two look identical from /dev alone, and two
    # separate reporters were told to run `modprobe sg` when they had already
    # done so and the real problem was that the nodes never reached their
    # container.
    if not sr_nodes and not sg_nodes and not host_sg:
        return (
            "no_devices",
            "No optical drives (/dev/sr*) and no SCSI generic nodes (/dev/sg*) "
            "are visible here, and the host reports no SCSI generic devices "
            "either. If a drive is attached to the host, it is not reaching "
            "the container.",
        )
    if not sg_nodes and host_sg:
        return (
            "sg_not_passed_through",
            f"The host has SCSI generic devices ({', '.join(host_sg)}) but none "
            "of them reached this container (no /dev/sg*). The kernel module is "
            "loaded; the container was not given the nodes.",
        )
    if not sg_nodes and not host_sg:
        return (
            "no_sg_nodes",
            f"Optical drives are visible ({', '.join(sr_nodes) or 'none'}) but the "
            "host has no SCSI generic support at all — nothing in "
            "/sys/class/scsi_generic and no /dev/sg*. MakeMKV enumerates drives "
            "through SCSI generic.",
        )
    if not sr_nodes:
        return (
            "no_sr_nodes",
            f"SCSI generic nodes exist ({', '.join(sg_nodes)}) but no optical "
            "drive node (/dev/sr*) is visible here.",
        )

    unreadable = [node for node in sg_nodes if not os.access(node, os.R_OK | os.W_OK)]
    if unreadable:
        return (
            "sg_not_accessible",
            f"SCSI generic nodes exist but are not readable/writable by this "
            f"process: {', '.join(unreadable)}.",
        )

    return (
        "unknown",
        f"Optical drives ({', '.join(sr_nodes)}) and SCSI generic nodes "
        f"({', '.join(sg_nodes)}) are both visible and accessible, so the cause "
        "is not a missing device node or a permission problem.",
    )


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


def _probe_device(dev: str, now: float) -> DriveSnapshot:
    """The actual hardware probe for one device. May block on a dead drive —
    only ever called through the deadline pool in ``_snapshot_device``."""
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
    return DriveSnapshot(
        mount_point=dev,
        loaded=loaded,
        volume_label=_volume_label_from_udev(udev),
        media_kind=_media_kind_from_udev(udev),
        identity=identity,
        udev_state=udev,
        observed_at=now,
    )


def _not_responding_snapshot(dev: str, now: float) -> DriveSnapshot:
    """Fallback row for a drive that is not answering: keep the last known
    identity (drive cards stay tellable-apart) but never claim media —
    a non-responding drive must not offer Start-copy affordances (#724)."""
    prev = _last_known.get(dev)
    if prev is not None:
        return replace(prev, loaded=False, volume_label=None, observed_at=now)
    return DriveSnapshot(
        mount_point=dev,
        loaded=False,
        volume_label=None,
        media_kind=None,
        identity=DriveIdentity(
            by_id_serial=f"unknown:{os.path.basename(dev)}",
            vendor="",
            model="",
            bus="unknown",
            by_id_name="",
            hardware_name=None,
            identity_source="unknown",
        ),
        udev_state={},
        observed_at=now,
    )


def _snapshot_device(dev: str, now: float) -> DriveSnapshot:
    """Probe one device under the hard deadline (#862). A timeout abandons
    the stuck call, starts the per-device cooldown (no hardware touched
    until it expires), and serves the not-responding fallback."""
    if now < _unresponsive_until.get(dev, 0.0):
        return _not_responding_snapshot(dev, now)
    future = _probe_pool.submit(_probe_device, dev, now)
    try:
        snap = future.result(timeout=_PROBE_TIMEOUT_SECONDS)
    except FutureTimeout:
        future.cancel()  # frees the slot when the probe never started
        _unresponsive_until[dev] = now + _PROBE_COOLDOWN_SECONDS
        logger.warning(
            "drive_registry: probe of %s exceeded %.1fs — drive treated as "
            "not responding for %.0fs (power-cycle the drive if this persists)",
            dev, _PROBE_TIMEOUT_SECONDS, _PROBE_COOLDOWN_SECONDS,
        )
        return _not_responding_snapshot(dev, now)
    except Exception as exc:
        logger.warning("drive_registry: probe of %s failed: %s", dev, exc)
        return _not_responding_snapshot(dev, now)
    _unresponsive_until.pop(dev, None)
    _last_known[dev] = snap
    return snap


def _build_snapshots() -> list[DriveSnapshot]:
    devices = _enumerate_devices()
    now = time.monotonic()
    return [_snapshot_device(dev, now) for dev in devices]
