"""Is a local path really where the user thinks it is? (#885)

Two silent failures made transfers and space figures lie:

* **A path that doesn't exist in the container.** The classic case is a host
  path (``/mnt/user/media``) entered where the container-side mount path
  (``/media``) belongs. The app used to create it — inside the container's
  own filesystem — and show free space for ``/`` as if it were the drive.
* **A path that exists but isn't on any mount.** Files written there land in
  the container's writable layer: they look fine until the container is
  updated or recreated, and then they are gone. Transfers there also let the
  job clean up its rip afterwards, so nothing is left anywhere.

The checks use ``/proc/self/mountinfo`` (nearest mount point containing the
path). Off Linux, or when it can't be read, nothing is flagged.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

MOUNTINFO = "/proc/self/mountinfo"
DOCKERENV = "/.dockerenv"
_OVERLAY_FSTYPES = {"overlay", "overlayfs", "fuse-overlayfs", "aufs"}
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


def _unescape(field: str) -> str:
    return _OCTAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), field)


def _mounts() -> list[tuple[str, str]]:
    """[(mount_point, fstype)] from mountinfo; [] if unavailable."""
    try:
        with open(MOUNTINFO, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    out: list[tuple[str, str]] = []
    for line in lines:
        left, sep, right = line.partition(" - ")
        fields = left.split()
        if not sep or len(fields) < 5:
            continue
        fstype = right.split()[0] if right.split() else ""
        out.append((_unescape(fields[4]), fstype))
    return out


def _nearest_mount(path: Path, mounts: list[tuple[str, str]]) -> Optional[tuple[str, str]]:
    best: Optional[tuple[str, str]] = None
    s = str(path)
    for mp, fstype in mounts:
        if s == mp or s.startswith(mp.rstrip("/") + "/"):
            # Later entries over-mount earlier ones at the same point.
            if best is None or len(mp) >= len(best[0]):
                best = (mp, fstype)
    return best


def in_container() -> bool:
    if os.path.exists(DOCKERENV):
        return True
    root = _nearest_mount(Path("/"), _mounts())
    return bool(root and root[1] in _OVERLAY_FSTYPES)


@dataclass
class PathCheck:
    path: Path
    exists: bool
    mount_point: Optional[str] = None
    on_container_layer: bool = False


def check_local_path(path: str | Path) -> PathCheck:
    """Resolve ``path`` and say whether it exists and what it is mounted on."""
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except OSError:
        pass
    if not p.exists():
        return PathCheck(path=p, exists=False)
    mounts = _mounts()
    nearest = _nearest_mount(p, mounts)
    if nearest is None:
        return PathCheck(path=p, exists=True)
    container = os.path.exists(DOCKERENV) or (
        (_nearest_mount(Path("/"), mounts) or ("", ""))[1] in _OVERLAY_FSTYPES
    )
    return PathCheck(
        path=p,
        exists=True,
        mount_point=nearest[0],
        on_container_layer=container and nearest[0] == "/",
    )


def missing_message(path: Path) -> str:
    if in_container():
        return (
            f"{path} doesn't exist inside the container. If it's a drive or "
            "share on the host, map it in the container's volumes and enter the "
            "container-side path here (for example /media, not /mnt/user/media)."
        )
    return f"{path} doesn't exist. Create it, or mount the drive, first."


def container_layer_message(path: Path) -> str:
    return (
        f"{path} isn't on a mounted drive — it's inside the container itself, so "
        "anything written there is lost when the container is updated or "
        f"recreated. Map a host folder or drive to {path} in the container's volumes."
    )


def destination_problem(path: str | Path | None) -> Optional[str]:
    """Why a local transfer destination must not be written to, or None.

    Never creates anything: the destination root must already exist on a
    mounted drive. Folders *inside* it are still created as needed.
    """
    if not path or not str(path).strip():
        return "No transfer folder is set. Choose one in Settings > Transfers."
    check = check_local_path(path)
    if not check.exists:
        return missing_message(check.path)
    if check.on_container_layer:
        return container_layer_message(check.path)
    return None


def usage_problem(path: str | Path) -> tuple[Optional[str], Optional[str]]:
    """(error, warning) for showing a space figure for ``path``.

    error: the path can't be measured (doesn't exist).
    warning: it can be, but the figure is the container's own disk.
    """
    check = check_local_path(path)
    if not check.exists:
        return missing_message(check.path), None
    if check.on_container_layer:
        return None, container_layer_message(check.path)
    return None, None
