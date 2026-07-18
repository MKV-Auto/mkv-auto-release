"""Tests for the async readiness gate (#490).

The gate used to call a synchronous ``SELECT 1`` from an ``async def``
middleware with a 2-second cache TTL. Under a fresh page-load burst of
~6 concurrent XHRs, every request that arrived while the first was
still in flight fired its own SELECT, AND blocked the event loop during
the round-trip. The async refactor:

  * Offloads SELECT 1 to the default executor via ``run_in_executor``
  * Coalesces concurrent cold-cache callers behind ``asyncio.Lock``
  * Extends TTL to 30 s once warm; cold TTL stays at 2 s

These tests assert each of those properties in isolation.
"""
import asyncio
import time

import pytest

from api import main as api_main


@pytest.fixture(autouse=True)
def _reset_readiness_state(monkeypatch):
    """Each test starts with a cold cache and a fresh lock."""
    monkeypatch.setattr(
        api_main, "_readiness_state",
        {"checked_at": 0.0, "ready": False, "error": None},
    )
    monkeypatch.setattr(api_main, "_readiness_lock", None)
    yield


# ──────────────────────────────────────────────────────────────────────
# Coalescing — the headline regression target.
# ──────────────────────────────────────────────────────────────────────

def test_concurrent_cold_callers_share_a_single_ping(monkeypatch):
    """N concurrent cold-cache async callers must result in exactly ONE
    SELECT 1 round-trip, not N."""
    call_count = {"n": 0}

    def slow_ping():
        call_count["n"] += 1
        time.sleep(0.05)  # Long enough to keep all N callers queued.

    monkeypatch.setattr(api_main, "_ping_db_blocking", slow_ping)

    async def driver():
        results = await asyncio.gather(*(
            api_main._check_db_ready_async() for _ in range(8)
        ))
        return results

    results = asyncio.run(driver())
    assert all(r == (True, None) for r in results)
    assert call_count["n"] == 1, (
        f"Expected 1 SELECT 1 across 8 concurrent cold callers, got {call_count['n']}"
    )


# ──────────────────────────────────────────────────────────────────────
# Warm cache TTL is 30 s.
# ──────────────────────────────────────────────────────────────────────

def test_warm_cache_skips_ping_within_30s(monkeypatch):
    """Once the cache is warm, callers within 30 s must NOT fire SELECT 1."""
    call_count = {"n": 0}

    def ping():
        call_count["n"] += 1

    monkeypatch.setattr(api_main, "_ping_db_blocking", ping)

    async def driver():
        # First call warms the cache.
        await api_main._check_db_ready_async()
        # 9 more calls within the TTL window — should be cache hits.
        for _ in range(9):
            await api_main._check_db_ready_async()

    asyncio.run(driver())
    assert call_count["n"] == 1, (
        f"Expected 1 SELECT 1 across 10 warm calls, got {call_count['n']}"
    )


def test_warm_ttl_expiry_re_pings(monkeypatch):
    """After the warm TTL expires, the next caller must ping again."""
    call_count = {"n": 0}

    def ping():
        call_count["n"] += 1

    monkeypatch.setattr(api_main, "_ping_db_blocking", ping)

    async def driver():
        await api_main._check_db_ready_async()  # warms cache
        # Fast-forward checked_at so the next call sees an expired TTL.
        api_main._readiness_state["checked_at"] -= (
            api_main._READINESS_CACHE_TTL_WARM_SECONDS + 1.0
        )
        await api_main._check_db_ready_async()

    asyncio.run(driver())
    assert call_count["n"] == 2


# ──────────────────────────────────────────────────────────────────────
# Cold TTL is 2 s (kept short so recovery is snappy).
# ──────────────────────────────────────────────────────────────────────

def test_failure_is_not_cached_so_recovery_is_immediate(monkeypatch):
    """Matches the original sync behavior: cache only kicks in on success.
    A failed ping must NOT be cached — otherwise a transient Postgres hiccup
    would 503 every subsequent request for the full TTL window."""
    call_count = {"n": 0}
    fail_first_two = {"left": 2}

    def flaky():
        call_count["n"] += 1
        if fail_first_two["left"] > 0:
            fail_first_two["left"] -= 1
            raise RuntimeError("postgres warming up")

    monkeypatch.setattr(api_main, "_ping_db_blocking", flaky)

    async def driver():
        # 1st call fails.
        ok1, _ = await api_main._check_db_ready_async()
        # 2nd call fails — must re-ping (failure is not cached).
        ok2, _ = await api_main._check_db_ready_async()
        # 3rd call succeeds — recovery is immediate, no TTL gating.
        ok3, _ = await api_main._check_db_ready_async()
        # 4th call hits the warm cache, no extra ping.
        ok4, _ = await api_main._check_db_ready_async()
        return ok1, ok2, ok3, ok4

    ok1, ok2, ok3, ok4 = asyncio.run(driver())
    assert (ok1, ok2, ok3, ok4) == (False, False, True, True)
    # 3 pings: two failures plus the successful one. The 4th call is a cache hit.
    assert call_count["n"] == 3, (
        f"Expected 3 pings (2 fail + 1 success + 1 cache hit), got {call_count['n']}"
    )


# ──────────────────────────────────────────────────────────────────────
# Sync façade still works (tests, scripts).
# ──────────────────────────────────────────────────────────────────────

def test_sync_check_db_ready_succeeds_when_ping_ok(monkeypatch):
    monkeypatch.setattr(api_main, "_ping_db_blocking", lambda: None)
    ok, err = api_main._check_db_ready()
    assert ok is True
    assert err is None


def test_sync_check_db_ready_returns_error_when_ping_fails(monkeypatch):
    def boom():
        raise RuntimeError("postgres down")

    monkeypatch.setattr(api_main, "_ping_db_blocking", boom)
    ok, err = api_main._check_db_ready()
    assert ok is False
    assert "postgres down" in (err or "")


# ──────────────────────────────────────────────────────────────────────
# Failure path: cached failure surfaces the error and ages out fast.
# ──────────────────────────────────────────────────────────────────────

def test_async_check_propagates_error_string(monkeypatch):
    def boom():
        raise RuntimeError("the database system is not yet accepting connections")

    monkeypatch.setattr(api_main, "_ping_db_blocking", boom)

    async def driver():
        return await api_main._check_db_ready_async()

    ok, err = asyncio.run(driver())
    assert ok is False
    assert "not yet accepting" in (err or "")
