"""Several writers on one rotating log file (#884).

uvicorn and the Celery workers all append to ``api.log`` (and ``uvicorn.log``,
``celery_worker.log``). With the stock ``RotatingFileHandler``, when one process
rotated, the others kept writing to their old descriptor -- ``api.log.1`` -- until
restart. ``SharedRotatingFileHandler`` must make every writer follow the rotation.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import textwrap
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from core.log_file_config import SharedRotatingFileHandler

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("api.test", logging.INFO, __file__, 0, msg, None, None)


def _handler(cls, path: Path, max_bytes: int = 1_000_000, backups: int = 3):
    h = cls(path, maxBytes=max_bytes, backupCount=backups)
    h.setFormatter(logging.Formatter("%(message)s"))
    return h


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def test_writer_b_follows_rotation_forced_by_writer_a(tmp_path: Path) -> None:
    log = tmp_path / "api.log"
    a = _handler(SharedRotatingFileHandler, log)
    b = _handler(SharedRotatingFileHandler, log)
    try:
        a.emit(_record("a-before"))
        b.emit(_record("b-before"))

        a.doRollover()  # writer A rotates: api.log -> api.log.1

        b.emit(_record("b-after"))
        backup = tmp_path / "api.log.1"
        assert "b-before" in _read(backup)
        assert "b-after" in _read(log)
        assert "b-after" not in _read(backup)

        size_after_rotation = backup.stat().st_size
        for i in range(5):
            b.emit(_record(f"b-more-{i}"))
            a.emit(_record(f"a-more-{i}"))
        assert backup.stat().st_size == size_after_rotation, ".1 kept growing after the rotation"
        assert "b-more-4" in _read(log) and "a-more-4" in _read(log)
    finally:
        a.close()
        b.close()


def test_stock_handler_reproduces_the_bug(tmp_path: Path) -> None:
    """Guard that the scenario above is the real failure: stock B writes into .1."""
    log = tmp_path / "api.log"
    a = _handler(RotatingFileHandler, log)
    b = _handler(RotatingFileHandler, log)
    try:
        a.emit(_record("a-before"))
        b.emit(_record("b-before"))
        a.doRollover()
        b.emit(_record("b-after"))
        assert "b-after" in _read(tmp_path / "api.log.1")
        assert "b-after" not in _read(log)
    finally:
        a.close()
        b.close()


def test_writer_follows_when_file_was_removed(tmp_path: Path) -> None:
    log = tmp_path / "api.log"
    h = _handler(SharedRotatingFileHandler, log)
    try:
        h.emit(_record("first"))
        log.unlink()
        h.emit(_record("second"))
        assert _read(log) == "second\n"
    finally:
        h.close()


def test_size_rotation_by_interleaved_writers_rotates_once_per_threshold(tmp_path: Path) -> None:
    """Size-triggered rollovers from either writer: no cascade, no near-empty backups."""
    log = tmp_path / "api.log"
    max_bytes = 2_000
    a = _handler(SharedRotatingFileHandler, log, max_bytes=max_bytes, backups=3)
    b = _handler(SharedRotatingFileHandler, log, max_bytes=max_bytes, backups=3)
    line_len = len("x-0000-" + "y" * 40 + "\n")
    try:
        for i in range(150):  # ~8 KB: several rotations, triggered by both writers
            (a if i % 2 else b).emit(_record(f"x-{i:04d}-" + "y" * 40))
        last = "x-0149-"
        assert last in _read(log)
        assert log.stat().st_size < max_bytes
        for n in (1, 2, 3):
            size = (tmp_path / f"api.log.{n}").stat().st_size
            # Each backup is a full file: a second writer re-rotating a fresh
            # file would leave a backup holding only a line or two.
            assert max_bytes - line_len <= size < max_bytes, (n, size)
        assert not (tmp_path / "api.log.4").exists()
        # Every surviving line is in order and unduplicated across files.
        lines = "".join(_read(tmp_path / f"api.log.{n}") for n in (3, 2, 1)) + _read(log)
        ids = [int(l.split("-")[1]) for l in lines.splitlines()]
        assert ids == sorted(set(ids)) and ids[-1] == 149
    finally:
        a.close()
        b.close()


_CHILD = textwrap.dedent(
    """
    import logging, sys
    from core.log_file_config import SharedRotatingFileHandler
    h = SharedRotatingFileHandler(sys.argv[1], maxBytes=1_000_000, backupCount=3)
    h.setFormatter(logging.Formatter("%(message)s"))
    for line in sys.stdin:
        msg = line.strip()
        h.emit(logging.LogRecord("api.child", logging.INFO, "", 0, msg, None, None))
        print("ok", flush=True)
    h.close()
    """
)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX rename semantics")
def test_separate_process_follows_rotation(tmp_path: Path) -> None:
    """Same as the first test with writer B in a real second process (like a Celery worker)."""
    log = tmp_path / "api.log"
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(log)],
        cwd=BACKEND_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )

    def child_emit(msg: str) -> None:
        assert child.stdin is not None and child.stdout is not None
        child.stdin.write(msg + "\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "ok"

    a = _handler(SharedRotatingFileHandler, log)
    try:
        child_emit("child-before")
        a.emit(_record("api-before"))
        a.doRollover()
        child_emit("child-after")
        backup = tmp_path / "api.log.1"
        size_after_rotation = backup.stat().st_size
        child_emit("child-later")
        a.emit(_record("api-after"))

        assert "child-before" in _read(backup) and "api-before" in _read(backup)
        current = _read(log)
        assert "child-after" in current and "child-later" in current and "api-after" in current
        assert backup.stat().st_size == size_after_rotation
    finally:
        a.close()
        if child.stdin:
            child.stdin.close()
        child.wait(timeout=10)
