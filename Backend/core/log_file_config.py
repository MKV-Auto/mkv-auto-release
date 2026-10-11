"""
Shared application log file rotation limits (MKVAUTO_ROOT/logs, Docker: /data/mkvauto/logs).

Used by ``SharedRotatingFileHandler`` and by manual append paths (e.g. makemkvcon.log).
"""

from __future__ import annotations

import contextlib
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterator

LOG_ROTATE_MAX_BYTES = 10 * 1024 * 1024
LOG_ROTATE_BACKUP_COUNT = 3


@contextlib.contextmanager
def _rotation_lock(path: Path) -> Iterator[None]:
    """
    Hold an exclusive ``fcntl.flock`` on ``<logname>.rotate.lock`` beside ``path``.

    The lock file is opened fresh on every call: flock locks belong to the open file
    description, so an fd inherited across fork (Celery prefork) would let parent and
    child share the lock instead of excluding each other. Without fcntl, or if the
    lock file cannot be opened, this degrades to no locking (best effort).
    """
    try:
        import fcntl  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        yield
        return

    lock_path = path.parent / f"{path.name}.rotate.lock"
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        yield
        return
    try:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


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

    with _rotation_lock(path):
        _rollover()


class SharedRotatingFileHandler(RotatingFileHandler):
    """
    ``RotatingFileHandler`` that stays correct when several processes append to one file.

    The stock handler assumes it is the file's only writer. In the container,
    ``api.log`` is held by uvicorn *and* by every Celery process that imported
    ``api.main``; when one of them rotated, the rest kept appending to their old
    descriptor, i.e. to ``api.log.1``, until restart (#884). This handler adds:

    * **Follow rotations** (as ``WatchedFileHandler`` does): before each emit, if the
      path no longer names the file we hold open (inode/device changed, or missing),
      reopen it. Whoever rotated, every writer's next line lands in the new base file.
    * **Size from the file, not our offset**: the rollover check uses ``fstat`` size,
      which counts every writer's bytes, not ``tell()`` of this process's descriptor.
    * **One rotation per threshold**: the rename chain runs under a cross-process
      ``flock`` and only if the path still names the file we decided to rotate. A
      writer that loses the race follows the new file instead of rotating it again
      (which would push live lines down to ``.2`` and drop a backup).

    Limits, backup naming (``.1`` … ``.N``) and formatting are those of
    ``RotatingFileHandler``.
    """

    _dev: int = -1
    _ino: int = -1

    def _open(self):  # type: ignore[override]
        stream = super()._open()
        try:
            st = os.fstat(stream.fileno())
            self._dev, self._ino = st.st_dev, st.st_ino
        except (OSError, ValueError):
            self._dev, self._ino = -1, -1
        return stream

    def _path_is_our_stream(self) -> bool:
        """True while ``baseFilename`` still names the file we hold open.

        Missing path -> False (rotated away). Any other stat failure -> True: we
        cannot tell, so keep writing where we are, like the stock handler would.
        """
        try:
            st = os.stat(self.baseFilename)
            return (st.st_dev, st.st_ino) == (self._dev, self._ino)
        except FileNotFoundError:
            return False
        except Exception:
            return True

    def _reopen(self) -> None:
        if self.stream is not None:
            with contextlib.suppress(Exception):
                self.stream.flush()
            with contextlib.suppress(Exception):
                self.stream.close()
            self.stream = None  # if _open() fails, never write to a closed stream
        self.stream = self._open()

    def reopen_if_rotated(self) -> None:
        """Reopen the base file if another writer rotated (renamed/removed) it."""
        if self.stream is not None and not self._path_is_our_stream():
            self._reopen()

    def shouldRollover(self, record: logging.LogRecord) -> bool:  # noqa: N802 (stdlib name)
        if self.stream is None:  # delay=True
            self.stream = self._open()
        if self.maxBytes <= 0:
            return False
        try:
            size = os.fstat(self.stream.fileno()).st_size
        except (OSError, ValueError):
            return False
        if not size:  # never roll an empty file
            return False
        msg = "%s\n" % self.format(record)
        if size + len(msg) < self.maxBytes:
            return False
        # Never roll anything other than a regular file (e.g. /dev/null).
        return not (os.path.exists(self.baseFilename) and not os.path.isfile(self.baseFilename))

    def doRollover(self) -> None:  # noqa: N802 (stdlib name)
        with _rotation_lock(Path(self.baseFilename)):
            if self._path_is_our_stream():
                super().doRollover()  # close, rename chain, reopen (records new inode)
            else:
                # Another process rotated after our pre-emit check: follow, don't re-rotate.
                self._reopen()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.reopen_if_rotated()
        except Exception:
            self.handleError(record)
            return
        super().emit(record)
