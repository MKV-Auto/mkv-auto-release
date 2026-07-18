"""
Simple async guard to prevent concurrent scans for the same disc.
Keeps track of in-progress scans keyed by a disc identifier (disc_num + mount).
"""
import asyncio
import os
import time
from typing import Dict, Tuple

SCAN_TIMEOUT = int(os.getenv("SCAN_GUARD_TIMEOUT", "180"))

_inflight: Dict[str, Tuple[asyncio.Future, float]] = {}
_lock = asyncio.Lock()


def _key(disc_num: str, mount: str) -> str:
    return f"{disc_num}:{mount}"


async def try_start(disc_num: str, mount: str) -> Tuple[bool, asyncio.Future]:
    """
    Attempt to mark a scan as started. Returns (started, future).
    If a scan is already in progress, started=False and future refers to that scan.
    """
    async with _lock:
        key = _key(disc_num, mount)
        entry = _inflight.get(key)
        if entry:
            fut, started_ts = entry
            if fut and not fut.done():
                # if stuck beyond timeout, clear it
                if time.time() - started_ts > SCAN_TIMEOUT:
                    _inflight.pop(key, None)
                else:
                    return False, fut
        fut = asyncio.get_running_loop().create_future()
        _inflight[key] = (fut, time.time())
        return True, fut


async def complete(disc_num: str, mount: str, result=None) -> None:
    async with _lock:
        key = _key(disc_num, mount)
        entry = _inflight.pop(key, None)
        fut = entry[0] if entry else None
        if fut and not fut.done():
            fut.set_result(result)


async def fail(disc_num: str, mount: str, exc: Exception) -> None:
    async with _lock:
        key = _key(disc_num, mount)
        entry = _inflight.pop(key, None)
        fut = entry[0] if entry else None
        if fut and not fut.done():
            fut.set_exception(exc)


async def is_in_progress(disc_num: str, mount: str) -> bool:
    async with _lock:
        key = _key(disc_num, mount)
        entry = _inflight.get(key)
        if not entry:
            return False
        fut, started_ts = entry
        if fut and not fut.done():
            if time.time() - started_ts > SCAN_TIMEOUT:
                _inflight.pop(key, None)
                return False
            return True
        return False
