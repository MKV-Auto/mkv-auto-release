"""#882: run_makemkv must never let a slow line callback block MakeMKV.

2026-10-06 prod (RE: Apocalypse UHD, 140 titles): the rip worker spent
~1.6–8.6 s handling each MakeMKV progress line on the same thread that read
MakeMKV's stdout. The 64 KB pipe filled, makemkvcon sat blocked in
``pipe_write`` for minutes at a time, and ~2,600 lines were still queued when
it exited — the job saw "Copy complete" five hours later.

These tests run a real child process standing in for makemkvcon.
"""
from __future__ import annotations

import os
import re
import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest

import core.utils as u
from core.disc import persist_raw_progress_log

PRGV = re.compile(r"^PRGV:(\d+),(\d+),(\d+)$")


def _fake_makemkvcon(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake_makemkvcon"
    script.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


FLOOD = """
    import sys
    w = sys.stdout.write
    w('MSG:1005,0,1,"MakeMKV v1.18.4 linux(x64-release) started"\\n')
    for t in range(3):
        w(f'PRGC:5057,{t},"Analyzing seamless segments"\\n')
        w(f'PRGC:5017,{t},"Saving to MKV file"\\n')
        for i in range(0, 65536, 8):          # 8,192 progress lines per title
            w(f'PRGV:{i},{t * 21845 + i // 3},65536\\n')
    w('MSG:5036,260,1,"Copy complete. 3 titles saved."\\n')
    sys.stdout.flush()
"""


@pytest.fixture
def no_watchdog(monkeypatch):
    monkeypatch.setenv("RIP_OUTPUT_STALL_SECONDS", "0")
    monkeypatch.setenv("MKVAUTO_RIP_PROGRESS_INTERVAL_SECONDS", "1.0")


def test_slow_callback_does_not_throttle_makemkv(tmp_path, monkeypatch, no_watchdog):
    """~24,600 progress lines and a 20 ms callback. Delivering every line
    would take ~8 minutes; coalescing must finish in seconds."""
    monkeypatch.setattr(u, "MAKEMKVCON_PATH", str(_fake_makemkvcon(tmp_path, FLOOD)))
    delivered: list[str] = []

    def slow_cb(line: str) -> None:
        delivered.append(line)
        time.sleep(0.02)

    started = time.monotonic()
    out, _pid = u.run_makemkv("mkv dev:/dev/sr0 all /tmp/out", line_cb=slow_cb,
                              log_path=tmp_path / "makemkv_progress.log")
    elapsed = time.monotonic() - started

    assert elapsed < 20, f"took {elapsed:.1f}s — callbacks are throttling the reader"
    assert len(delivered) < 200, f"{len(delivered)} callbacks — progress was not coalesced"

    # Nothing is lost from the raw output or the log.
    produced = out.splitlines()
    assert len(produced) == 1 + 3 * (2 + 8192) + 1
    log_lines = (tmp_path / "makemkv_progress.log").read_text().splitlines()
    assert len(log_lines) == len(produced)
    assert all(re.match(r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] ", l) for l in log_lines)

    # Every non-progress line is delivered, in order.
    non_prgv_src = [l for l in produced if not l.startswith("PRGV")]
    assert [l for l in delivered if not l.startswith("PRGV")] == non_prgv_src

    # Before each non-progress line, the callback saw the newest progress value
    # that preceded it in the source (title-boundary logic depends on this).
    last_prgv_before: dict[int, str | None] = {}
    latest = None
    for l in produced:
        if l.startswith("PRGV"):
            latest = l
        else:
            last_prgv_before[len(last_prgv_before)] = latest
    seen = None
    idx = 0
    for l in delivered:
        if l.startswith("PRGV"):
            seen = l
        else:
            assert seen == last_prgv_before[idx], f"stale progress before {l!r}"
            idx += 1


def test_makemkv_keeps_writing_while_the_callback_is_blocked(tmp_path, monkeypatch, no_watchdog):
    """The child writes ~1 MB (far past a 64 KB pipe) and then drops a marker
    file. If the reader shared a thread with the callback, the child would
    block on the full pipe and the marker would never appear while the first
    callback is still running."""
    marker = tmp_path / "child_finished_writing"
    body = f"""
        import sys, pathlib
        w = sys.stdout.write
        w('PRGC:5017,0,"Saving to MKV file"\\n')
        for i in range(40000):
            w(f'PRGV:{{i % 65536}},{{i}},65536\\n')
        sys.stdout.flush()
        pathlib.Path({str(marker)!r}).write_text("done")
        w('MSG:5036,260,1,"Copy complete. 1 titles saved."\\n')
    """
    monkeypatch.setattr(u, "MAKEMKVCON_PATH", str(_fake_makemkvcon(tmp_path, body)))
    marker_seen_inside_first_callback: list[bool] = []

    def blocking_cb(line: str) -> None:
        if marker_seen_inside_first_callback:
            return
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.05)
        marker_seen_inside_first_callback.append(marker.exists())

    u.run_makemkv("mkv dev:/dev/sr0 all /tmp/out", line_cb=blocking_cb,
                  log_path=tmp_path / "p.log")
    assert marker_seen_inside_first_callback == [True]


def test_final_progress_value_is_delivered(tmp_path, monkeypatch, no_watchdog):
    """A trailing burst of progress with no line after it still reaches the
    callback once, with the newest value."""
    body = """
        import sys
        for i in range(5000):
            sys.stdout.write(f'PRGV:{i},{i},65536\\n')
        sys.stdout.flush()
    """
    monkeypatch.setattr(u, "MAKEMKVCON_PATH", str(_fake_makemkvcon(tmp_path, body)))
    delivered: list[str] = []
    u.run_makemkv("mkv dev:/dev/sr0 all /tmp/out", line_cb=delivered.append,
                  log_path=tmp_path / "p.log")
    assert delivered, "no progress delivered at all"
    assert delivered[-1] == "PRGV:4999,4999,65536"


def test_persist_raw_progress_log_keeps_the_timestamped_copy(tmp_path):
    live = tmp_path / "makemkv_progress.log"
    live.write_text("[2026-10-06 21:07:34] MSG:1005,0,1,\"started\"\n")
    persist_raw_progress_log(str(tmp_path), "MSG:1005,0,1,\"started\"\n")

    assert live.read_text() == "MSG:1005,0,1,\"started\"\n"   # raw, as readers expect
    kept = (tmp_path / "makemkv_progress.timestamped.log").read_text()
    assert kept.startswith("[2026-10-06 21:07:34] MSG:1005")

    # A second run in the same folder appends rather than replacing history.
    live.write_text("[2026-10-07 09:00:00] MSG:5036,260,1,\"done\"\n")
    persist_raw_progress_log(str(tmp_path), "MSG:5036,260,1,\"done\"\n")
    kept = (tmp_path / "makemkv_progress.timestamped.log").read_text().splitlines()
    assert kept == [
        "[2026-10-06 21:07:34] MSG:1005,0,1,\"started\"",
        "[2026-10-07 09:00:00] MSG:5036,260,1,\"done\"",
    ]
