"""The DV7 library is the user's transfer destination (#902).

Settings -> Dolby Vision used to ask for "library folders", which duplicated
the destination the user had already configured for transfers. The library
scan and conversions now follow the **active transfer destination**:

- ``local``: its ``transfer_dir``, which must be a mapped folder.
- ``smb`` / ``nfs``: transfers write over the network and never mount, but the
  scan has to list folders and read files, so the share is mounted at
  ``LIBRARY_MOUNT`` through the root helper (the same mount the SMB free-space
  check uses), with the config's stored credentials. The mount is reused
  while it stays up and remounted when the destination changes. Files on it
  are converted on local scratch and pushed back (#896).
- ``rsync``: can't be mounted; the library scan isn't available.

``describe`` never mounts (Settings calls it on every page load);
``ensure_root`` mounts when needed and is what the scan and conversions use.
"""
from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

log = logging.getLogger(__name__)

LIBRARY_MOUNT = os.environ.get("MKVAUTO_LIBRARY_MOUNT", "/mnt/mkvauto-library")
MOUNTINFO = "/proc/self/mountinfo"

NO_DESTINATION = (
    "No transfer destination is set up. Add one in Settings → Destinations; the "
    "Dolby Vision library scan uses it."
)
RSYNC_UNSUPPORTED = (
    "Your transfer destination uses rsync, which the library scan can't read "
    "from. Map your library folder into the container and use a local "
    "transfer destination to scan it. Converting new rips still works."
)


@dataclass
class LibraryDestination:
    available: bool
    mode: Optional[str] = None
    name: Optional[str] = None
    display: Optional[str] = None
    root: Optional[str] = None
    problem: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


def _join(base: str, sub: Optional[str]) -> str:
    sub = (sub or "").strip().strip("/")
    return f"{base.rstrip('/')}/{sub}" if sub else base


def _smb_source(cd: dict) -> tuple[str, str, int, str]:
    host = str(cd.get("host") or "").strip()
    share = str(cd.get("share") or "").strip().strip("/")
    port = int(cd.get("port") or 445)
    return host, share, port, f"//{host}/{share}"


def _nfs_source(cd: dict) -> tuple[str, str, str]:
    server = str(cd.get("server") or "").strip()
    export = "/" + str(cd.get("export_path") or "").strip().strip("/")
    return server, export, f"{server}:{export}"


def describe(db: Any) -> LibraryDestination:
    """What the library is, without mounting anything."""
    from core.transfer.service import get_active_config

    config = get_active_config(db)
    if config is None:
        return LibraryDestination(False, problem=NO_DESTINATION)
    mode = (config.mode or "").lower()
    cd = config.config_data if isinstance(config.config_data, dict) else {}
    name = config.name or mode.upper()

    if mode == "local":
        from core.library_catalog import library_path_problem

        path = str(config.transfer_dir or "").strip()
        problem = library_path_problem(path) if path else "The local transfer destination has no folder set."
        return LibraryDestination(problem is None, mode, name, path or None, path or None, problem)
    if mode == "smb":
        host, share, _port, _src = _smb_source(cd)
        if not host or not share:
            return LibraryDestination(False, mode, name, problem="The SMB destination is missing its host or share.")
        return LibraryDestination(True, mode, name, _join(f"smb://{host}/{share}", cd.get("path")))
    if mode == "nfs":
        server, export, _src = _nfs_source(cd)
        if not server:
            return LibraryDestination(False, mode, name, problem="The NFS destination is missing its server.")
        return LibraryDestination(True, mode, name, _join(f"nfs://{server}{export}", cd.get("path")))
    if mode == "rsync":
        return LibraryDestination(False, mode, name, problem=RSYNC_UNSUPPORTED)
    return LibraryDestination(False, mode, name, problem=f"Unknown transfer mode {config.mode!r}.")


def _mounted_source(mount_point: str) -> Optional[str]:
    """The source mounted at ``mount_point`` (e.g. ``//host/share``), or None."""
    try:
        with open(MOUNTINFO, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    target = os.path.normpath(mount_point)
    for line in lines:
        left, sep, right = line.partition(" - ")
        fields = left.split()
        if not sep or len(fields) < 5:
            continue
        mp = fields[4].replace("\\040", " ")
        if os.path.normpath(mp) != target:
            continue
        rest = right.split()
        if len(rest) >= 2:
            return rest[1].replace("\\040", " ")
    return None


def _same_source(mounted: str, wanted: str) -> bool:
    norm = lambda s: s.rstrip("/").lower()
    return norm(mounted) == norm(wanted)


@contextlib.contextmanager
def _mount_lock() -> Iterator[None]:
    """One mount decision at a time across the scan and convert workers."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX dev hosts
        yield
        return
    from core.utils import get_mkvauto_tmp

    lock_dir = Path(get_mkvauto_tmp())
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_dir / "library_mount.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _mount(db: Any, config: Any, mode: str, cd: dict) -> Optional[str]:
    """Mount the destination at LIBRARY_MOUNT; return an error or None."""
    from core.transfer.utils.credentials import get_decrypted_credentials
    from core.utils import _root_helper_mount_nfs, _root_helper_mount_smb, _root_helper_unmount

    wanted = _smb_source(cd)[3] if mode == "smb" else _nfs_source(cd)[2]
    mounted = _mounted_source(LIBRARY_MOUNT)
    if mounted and _same_source(mounted, wanted):
        return None
    if mounted:
        log.info("library mount: %s is mounted at %s; remounting %s", mounted, LIBRARY_MOUNT, wanted)
        _root_helper_unmount(LIBRARY_MOUNT)
        if _mounted_source(LIBRARY_MOUNT):
            return f"{LIBRARY_MOUNT} still holds {mounted} and couldn't be unmounted."

    os.makedirs(LIBRARY_MOUNT, exist_ok=True)
    creds = get_decrypted_credentials(db, config.id) or {}
    if mode == "smb":
        host, share, port, _src = _smb_source(cd)
        _mp, err = _root_helper_mount_smb(
            host, share, LIBRARY_MOUNT, port,
            creds.get("smb_username", "") or "",
            creds.get("smb_password", "") or "",
            creds.get("smb_domain", "") or "",
        )
    else:
        server, export, _src = _nfs_source(cd)
        _mp, err = _root_helper_mount_nfs(server, export, LIBRARY_MOUNT, creds.get("nfs_options", "") or "")
    if err:
        return f"Couldn't mount {wanted}: {err}"
    log.info("library mount: mounted %s at %s", wanted, LIBRARY_MOUNT)
    return None


def ensure_root(db: Any) -> LibraryDestination:
    """The library as a folder in the container, mounting a share if needed.

    ``available`` is False with ``problem`` set when it can't be reached.
    """
    from core.transfer.service import get_active_config

    dest = describe(db)
    if not dest.available or dest.mode == "local":
        return dest
    config = get_active_config(db)
    cd = config.config_data if isinstance(config.config_data, dict) else {}
    with _mount_lock():
        err = _mount(db, config, dest.mode, cd)
    if err:
        dest.available, dest.problem = False, err
        return dest
    root = _join(LIBRARY_MOUNT, cd.get("path"))
    if not os.path.isdir(root):
        dest.available = False
        dest.problem = f"{dest.display} is mounted, but that folder doesn't exist on the share."
        return dest
    dest.root = root
    return dest
