"""True-concurrency HTTP for tests, without the threaded-TestClient deadlock (#748).

The old pattern — OS threads each calling the *sync* ``TestClient``, then
``t.join()`` with no timeout — hangs: every thread funnels through the
client's single blocking portal into one event loop, sync endpoints fan out
to anyio's bounded worker pool, and under contention a portal waiter is
never woken. The untimed join then freezes the whole suite (it blocked the
v1.2.0 release train twice in a row).

Here the concurrency lives where the app actually runs: one event loop,
httpx's ASGI transport, ``asyncio.gather`` — and the batch is bounded by a
timeout, so a real regression fails in seconds with a message instead of
hanging.
"""
from __future__ import annotations

import asyncio
from typing import Any, Iterable, Optional, Tuple

import httpx

# Requests are (method, url, kwargs-or-None), e.g.
#   ("GET", "/discs/1/info?mount_point=/mnt/sr1", None)
#   ("POST", "/jobs/rip", {"json": {...}})
Request = Tuple[str, str, Optional[dict]]


async def gather_requests(app: Any, requests: Iterable[Request],
                          timeout: float = 60.0) -> "list[httpx.Response]":
    """Issue every request concurrently against ``app``; responses in order.

    A transport-level failure raises, and the whole batch is cut off at
    ``timeout`` — there is deliberately no way for this to block forever.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        tasks = [ac.request(method, url, **(kwargs or {}))
                 for method, url, kwargs in requests]
        return await asyncio.wait_for(asyncio.gather(*tasks), timeout)
