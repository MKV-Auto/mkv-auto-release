"""Module-level asyncio primitives must survive being used by a second event loop.

Background: since Python 3.10, ``asyncio.Lock`` / ``asyncio.Event`` bind to an
event loop on first *contended* use and stay bound. A singleton contended under
one loop then raises ``RuntimeError: ... is bound to a different event loop``
under the next — which in production never happens (one loop per process) but
in the test suite happens constantly, because every ``TestClient`` and every
``asyncio.run`` builds a fresh loop.

That was the CI flake in ``test_pool_concurrent.py``: an earlier test fired
concurrent requests through the readiness-gate middleware and bound
``api.main._readiness_lock``; the later 60-request batch then blew up on
acquire. It reproduced only in CI because the tests that bind the lock first
need a live Postgres/Redis and skip on a bare dev machine.

Three layers here:
  1. Unit tests for ``LoopLocalLock`` / ``LoopLocalEvent`` semantics.
  2. Per-singleton tests that contend each real module-level primitive under two
     loops in a row — these fail with the RuntimeError if a singleton is ever
     reverted to a bare ``asyncio`` primitive.
  3. A structural guard that fails when *new* module- or class-level bare
     primitives appear anywhere in ``api/`` or ``core/``.
"""
from __future__ import annotations

import ast
import asyncio
import gc
import threading
from pathlib import Path

import pytest

from core.loop_local import LoopLocalEvent, LoopLocalLock

BACKEND_ROOT = Path(__file__).resolve().parent.parent


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

async def _contend(lock, workers: int = 3) -> None:
    """Force the *contended* acquire path — the one that binds a lock to a loop.

    An uncontended ``acquire()`` returns on a fast path that never touches
    ``_get_loop()``, so a lock only becomes loop-bound once a second task has to
    wait for it. The first task here holds the lock across an ``await``, which
    guarantees the others queue.
    """
    async def worker() -> None:
        async with lock:
            await asyncio.sleep(0)

    await asyncio.gather(*(worker() for _ in range(workers)))


async def _wait_and_set(event) -> None:
    """Force ``Event.wait()`` to allocate a future, which binds it to the loop."""
    event.clear()
    waiter = asyncio.ensure_future(event.wait())
    await asyncio.sleep(0)  # let the waiter reach _get_loop()
    event.set()
    await waiter


# ──────────────────────────────────────────────────────────────────────
# 1. LoopLocalLock / LoopLocalEvent semantics
# ──────────────────────────────────────────────────────────────────────

def test_lock_survives_a_second_event_loop():
    """The whole point: contend under loop A, then contend under loop B."""
    lock = LoopLocalLock()
    asyncio.run(_contend(lock))
    asyncio.run(_contend(lock))  # would raise RuntimeError with a bare asyncio.Lock


def test_bare_asyncio_lock_still_breaks():
    """Pin the upstream behaviour this wrapper exists to work around.

    If a future Python makes bare locks loop-agnostic, this test fails and the
    wrapper can be reconsidered — the failure is the signal, not a defect.
    """
    bare = asyncio.Lock()
    asyncio.run(_contend(bare))
    with pytest.raises(RuntimeError, match="bound to a different event loop"):
        asyncio.run(_contend(bare))


def test_lock_is_one_object_per_loop():
    """Within a single loop every caller must get the *same* lock, or it would
    not exclude anything."""
    lock = LoopLocalLock()

    async def driver():
        return lock._current(), lock._current()

    first, second = asyncio.run(driver())
    assert first is second


def test_lock_actually_serializes_within_one_loop():
    """Mutual exclusion is preserved — the wrapper is not a no-op."""
    lock = LoopLocalLock()
    concurrent = {"now": 0, "peak": 0}

    async def worker():
        async with lock:
            concurrent["now"] += 1
            concurrent["peak"] = max(concurrent["peak"], concurrent["now"])
            await asyncio.sleep(0.01)
            concurrent["now"] -= 1

    async def driver():
        await asyncio.gather(*(worker() for _ in range(5)))

    asyncio.run(driver())
    assert concurrent["peak"] == 1, "lock let more than one task into the critical section"


def test_lock_reports_locked_state():
    lock = LoopLocalLock()

    async def driver():
        outside = lock.locked()
        async with lock:
            inside = lock.locked()
        return outside, inside

    assert asyncio.run(driver()) == (False, True)


def test_lock_releases_on_exception():
    """An exception inside the block must not leave the lock held."""
    lock = LoopLocalLock()

    async def driver():
        with pytest.raises(ValueError):
            async with lock:
                raise ValueError("boom")
        return lock.locked()

    assert asyncio.run(driver()) is False


def test_event_survives_a_second_event_loop():
    event = LoopLocalEvent()
    asyncio.run(_wait_and_set(event))
    asyncio.run(_wait_and_set(event))  # would raise RuntimeError with a bare asyncio.Event


def test_event_set_clear_is_set_round_trip():
    event = LoopLocalEvent()

    async def driver():
        event.clear()
        cleared = event.is_set()
        event.set()
        was_set = event.is_set()
        # An already-set event returns immediately from wait().
        await asyncio.wait_for(event.wait(), timeout=1)
        return cleared, was_set

    assert asyncio.run(driver()) == (False, True)


def test_closed_loops_do_not_accumulate_primitives():
    """A long test session must not leak one lock per loop it ever created.

    Weak keying cannot do this job: the primitive stores ``self._loop``, so the
    map's value strongly references its own key. Closed loops are pruned on
    access instead.
    """
    lock = LoopLocalLock()
    for _ in range(5):
        asyncio.run(_contend(lock))
    gc.collect()
    assert len(lock._per_loop) <= 1, (
        f"expected closed loops to be pruned, {len(lock._per_loop)} entries remain"
    )


def test_two_live_loops_keep_separate_primitives():
    """Pruning must only drop *closed* loops — a second live loop keeps its own."""
    lock = LoopLocalLock()
    seen = {}

    def run_in_thread(tag):
        async def driver():
            seen[tag] = lock._current()
            await barrier_wait(tag)
        asyncio.run(driver())

    # Two loops alive at once, each parked inside _current() until both arrive.
    # The barrier is bounded so a failure surfaces as an assertion rather than
    # two threads blocked forever at interpreter exit.
    ready = threading.Barrier(2)

    async def barrier_wait(tag):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: ready.wait(timeout=10))

    threads = [threading.Thread(target=run_in_thread, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert set(seen) == {"a", "b"}
    assert seen["a"] is not seen["b"], "two live loops must not share one asyncio.Lock"


# ──────────────────────────────────────────────────────────────────────
# 2. The real singletons, contended under two loops in a row
# ──────────────────────────────────────────────────────────────────────

def _real_locks():
    """(id, lock) for every module-level asyncio lock the backend keeps."""
    from api import main as api_main, scan_guard
    from api.routers import events
    from core import disc_cache
    from core.websocket_manager import get_websocket_manager

    return [
        ("api.main._readiness_lock", api_main._readiness_lock),
        ("api.routers.events._drive_scan_lock", events._drive_scan_lock),
        ("api.routers.events._ejection_lock", events._ejection_lock),
        ("api.routers.events._broadcast_lock", events._broadcast_lock),
        ("api.routers.events._disc_load_lock", events._disc_load_lock),
        ("core.disc_cache._LOCK", disc_cache._LOCK),
        ("api.scan_guard._lock", scan_guard._lock),
        ("core.websocket_manager singleton _lock", get_websocket_manager()._lock),
    ]


@pytest.mark.parametrize("name", [n for n, _ in _real_locks()])
def test_real_lock_singleton_is_reusable_across_loops(name):
    """Each shared lock must work under a fresh loop after being contended.

    This is the direct regression test for the ``test_pool_concurrent`` flake:
    swap any of these back to a bare ``asyncio.Lock()`` and the second
    ``asyncio.run`` raises "bound to a different event loop".
    """
    lock = dict(_real_locks())[name]
    asyncio.run(_contend(lock))
    asyncio.run(_contend(lock))


def test_drive_scan_event_singleton_is_reusable_across_loops():
    from api.routers import events

    asyncio.run(_wait_and_set(events._drive_scan_event))
    asyncio.run(_wait_and_set(events._drive_scan_event))


# ──────────────────────────────────────────────────────────────────────
# 3. Structural guard against new bare primitives
# ──────────────────────────────────────────────────────────────────────

_BARE_PRIMITIVES = {"Lock", "Event", "Semaphore", "BoundedSemaphore", "Condition"}
_SCANNED_DIRS = ("api", "core", "workers")


def _bare_primitive_name(node: ast.AST) -> str | None:
    """``asyncio.Lock()`` → "Lock"; anything else → None."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "asyncio"
        and func.attr in _BARE_PRIMITIVES
    ):
        return func.attr
    return None


def _find_shared_bare_primitives(path: Path) -> list[str]:
    """Bare asyncio primitives bound at module or class scope in ``path``.

    Scope note: this catches the unambiguous singleton cases. It does *not*
    catch ``self._lock = asyncio.Lock()`` inside ``__init__``, which is only
    hazardous when the class is itself a singleton — flagging every instance
    attribute would fire on legitimately per-request objects. The rule for
    those lives in ``core/loop_local.py``, and the singletons that exist today
    are covered by the per-singleton tests above.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[str] = []

    def scan_body(body, scope: str) -> None:
        for stmt in body:
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
                primitive = _bare_primitive_name(stmt.value)
                if primitive:
                    hits.append(f"{path.relative_to(BACKEND_ROOT)}:{stmt.lineno} "
                                f"({scope}) asyncio.{primitive}()")
            elif isinstance(stmt, ast.ClassDef):
                scan_body(stmt.body, f"class {stmt.name}")

    scan_body(tree.body, "module")
    return hits


def test_no_new_bare_asyncio_primitives_in_shared_state():
    """Module- and class-level ``asyncio.Lock()``/``Event()`` are a CI flake
    waiting to happen. Use ``core.loop_local`` instead."""
    offenders: list[str] = []
    for directory in _SCANNED_DIRS:
        root = BACKEND_ROOT / directory
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            offenders.extend(_find_shared_bare_primitives(path))

    assert not offenders, (
        "Bare asyncio primitives held in shared state bind to the first event "
        "loop that contends on them and break every later loop. Use "
        "core.loop_local.LoopLocalLock / LoopLocalEvent instead:\n  "
        + "\n  ".join(offenders)
    )
