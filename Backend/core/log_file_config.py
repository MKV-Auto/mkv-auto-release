"""
Shared application log file rotation limits (MKVAUTO_ROOT/logs, Docker: /data/mkvauto/logs).

Used by RotatingFileHandler and by manual append paths (e.g. makemkvcon.log).
"""

from __future__ import annotations

import os
from pathlib import Path

LOG_ROTATE_MAX_BYTES = 10 * 1024 * 1024
LOG_ROTATE_BACKUP_COUNT = 3


def rotate_file_if_needed(
    path: Path | str,
    *,
    max_bytes: int = LOG_ROTATE_MAX_BYTES,
    backup_count: int = LOG_ROTATE_BACKUP_COUNT,
) -> None:
    """
    If ``path`` exists and size >= max_bytes, rotate like logging.RotatingFileHandler:
    path -> path.1, path.1 -> path.2, …; drop path.backup_count.

    Uses fcntl flock on ``<logname>.rotate.lock`` in the same directory when available (Unix). Without fcntl,
    performs a best-effort rotate (same inter-process caveats as unchecked append).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not path.is_file() or path.stat().st_size < max_bytes:
            return
    except OSError:
        return

    lock_path = path.parent / f"{path.name}.rotate.lock"

    def _rollover() -> None:
        try:
            if not path.is_file() or path.stat().st_size < max_bytes:
                return
        except OSError:
            return
        base = str(path)
        if backup_count > 0:
            for i in range(backup_count - 1, 0, -1):
                sfn = f"{base}.{i}"
                dfn = f"{base}.{i + 1}"
                if os.path.exists(sfn):
                    if os.path.exists(dfn):
                        os.remove(dfn)
                    os.replace(sfn, dfn)
            dfn = f"{base}.1"
            if os.path.exists(dfn):
                os.remove(dfn)
            os.replace(base, dfn)
        else:
            path.unlink(missing_ok=True)

    try:
        import fcntl  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        _rollover()
        return

    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            _rollover()
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
    except OSError:
        _rollover()
