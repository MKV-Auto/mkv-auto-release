"""Machine-wide pool for background ffmpeg/ffprobe work (#929).

Preview encodes, thumbnails and junk detection all decode video, and each
one can use a full core. They used to be limited separately, or not at all:
on 2026-10-19 the preview limiter held 6 HLS encodes while junk detection ran
5 full-length ``freezedetect`` decodes beside them, on a 6-core host. Load
reached 40 and page loads timed out.

Two rules for every caller:

* ``media_slot()`` caps how many run at once across all processes and
  threads. Slots are lock files held with ``flock``, so claiming one is
  atomic and the kernel releases it when the holder dies; no PID files to
  go stale and no count-then-claim race.
* ``low_priority(cmd)`` runs the work at the lowest CPU priority and idle
  I/O class, so the API, Postgres and a rip in progress always come first.

``run_media`` combines the two.
"""
from __future__ import annotations

import fcntl
import logging
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Sequence

log = logging.getLogger(__name__)

SLOT_WAIT_SECONDS = 1800.0
_POLL_SECONDS = 0.5


def slot_dir() -> Path:
    from core.utils import get_mkvauto_tmp

    d = get_mkvauto_tmp() / "media_slots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pool_size() -> int:
    """The configured limit (Settings > Previews, max parallel), clamped to the CPU count."""
    try:
        from core.settings import get_preview_dict

        return max(1, int(get_preview_dict().get("max_parallel") or 1))
    except Exception:
        return max(1, (os.cpu_count() or 2) - 1)


@contextmanager
def media_slot(max_parallel: Optional[int] = None, *, wait_seconds: float = SLOT_WAIT_SECONDS) -> Iterator[None]:
    """Hold one machine-wide media slot for the duration of the block.

    Raises ``TimeoutError`` if no slot frees up within ``wait_seconds``.
    """
    cap = max(1, int(max_parallel or pool_size()))
    directory = slot_dir()
    deadline = time.monotonic() + wait_seconds
    held: Optional[int] = None
    while held is None:
        for i in range(cap):
            fd = os.open(directory / f"slot-{i}.lock", os.O_RDWR | os.O_CREAT, 0o666)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            held = fd
            break
        if held is None:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"no media slot free after {wait_seconds:.0f}s (limit {cap})")
            time.sleep(_POLL_SECONDS)
    try:
        yield
    finally:
        try:
            fcntl.flock(held, fcntl.LOCK_UN)
        finally:
            os.close(held)


def low_priority(cmd: Sequence[str]) -> list[str]:
    """Prefix ``cmd`` so it runs at nice 19 in the idle I/O class.

    ``ionice -t`` carries on if the class can't be set, so a host without
    I/O scheduling support still runs the command.
    """
    prefix: list[str] = []
    ionice = shutil.which("ionice")
    if ionice:
        prefix += [ionice, "-c3", "-t"]
    nice = shutil.which("nice")
    if nice:
        prefix += [nice, "-n", "19"]
    return prefix + list(cmd)


def run_media(
    cmd: Sequence[str],
    *,
    max_parallel: Optional[int] = None,
    wait_seconds: float = SLOT_WAIT_SECONDS,
    **kwargs,
) -> subprocess.CompletedProcess:
    """``subprocess.run`` for background media work: one pool slot, low priority."""
    with media_slot(max_parallel, wait_seconds=wait_seconds):
        return subprocess.run(low_priority(cmd), **kwargs)
