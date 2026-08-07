"""Event-loop-affine asyncio primitives for module-level singletons.

Since Python 3.10 an ``asyncio.Lock`` or ``asyncio.Event`` binds itself to an
event loop the first time it needs a future — that is, on the first *contended*
acquire (or first ``Event.wait()``), **not** at construction. The binding is
permanent, and any later use under a different loop raises::

    RuntimeError: <asyncio.locks.Lock object at 0x... [locked]> is bound to a
    different event loop

In the running app that never bites: there is one event loop for the life of
the process. It bites in the test suite, where every ``TestClient`` and every
``asyncio.run`` builds a fresh loop. A singleton contended in one test stays
bound to that test's now-closed loop and poisons every later test that contends
on it — an order-dependent flake that only shows up when the earlier test
actually runs (in CI, where Postgres and Redis are available, dozens of tests
that skip locally do run).

Lazily constructing the singleton on first use does not fix this. It defers the
binding by exactly one loop and then the object is just as sticky.

``LoopLocalLock`` and ``LoopLocalEvent`` keep one real primitive **per running
loop**, in a weakly-keyed map so a closed loop's entry is collected with the
loop. Call sites are unchanged: ``async with _lock:`` and
``await _event.wait()`` work exactly as before.

**What this does and does not guarantee.** These serialize tasks *within* one
event loop — precisely what a bare ``asyncio.Lock`` does. Neither provides
mutual exclusion across loops or across threads; ``asyncio.Lock`` never did
either, it raised instead. Where cross-thread safety is the requirement, use a
``threading.Lock``, as ``core.disc_cache`` does for its cache dict.

Rule of thumb: **an ``asyncio`` synchronization primitive must never be stored
in module-level (or singleton-instance) state as a bare ``asyncio.Lock`` /
``asyncio.Event``.** Use these wrappers instead. Primitives created inside a
coroutine and discarded when it returns are fine as-is.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Callable, Dict, Generic, TypeVar

_T = TypeVar("_T")

__all__ = ["LoopLocalLock", "LoopLocalEvent"]


class _LoopLocal(Generic[_T]):
    """One ``_T`` per running event loop, created on demand.

    Closed loops are pruned on access rather than held weakly. A
    ``WeakKeyDictionary`` keyed on the loop looks like the natural fit but
    cannot work here: ``_LoopBoundMixin`` makes the primitive store
    ``self._loop``, so the map's *value* strongly references its own *key* and
    the entry is never collectable. Pruning closed loops is what actually
    bounds the map — and it covers the real cases, since both ``asyncio.run``
    and ``TestClient`` close their loop on the way out.
    """

    __slots__ = ("_factory", "_per_loop", "_guard", "__weakref__")

    def __init__(self, factory: Callable[[], _T]) -> None:
        self._factory = factory
        self._per_loop: Dict[asyncio.AbstractEventLoop, _T] = {}
        # Two loops running in two threads can mutate the map concurrently.
        # They can never race on the *same* key (a loop runs in one thread at a
        # time), but the map itself still needs guarding. CPython's own
        # asyncio.mixins takes the same precaution.
        self._guard = threading.Lock()

    def _current(self) -> _T:
        """The primitive for the running loop. Raises if no loop is running."""
        loop = asyncio.get_running_loop()
        with self._guard:
            primitive = self._per_loop.get(loop)
            if primitive is None:
                # Only ever more than one entry in a process that builds
                # several loops — i.e. the test suite. The app has exactly one,
                # so this costs a length check on the hot path.
                if self._per_loop:
                    self._prune_closed()
                primitive = self._factory()
                self._per_loop[loop] = primitive
            return primitive

    def _prune_closed(self) -> None:
        """Drop entries for loops that have been closed. Caller holds _guard."""
        for dead in [lp for lp in self._per_loop if lp.is_closed()]:
            del self._per_loop[dead]


class LoopLocalLock(_LoopLocal[asyncio.Lock]):
    """Drop-in ``asyncio.Lock`` that is safe to hold in module-level state.

    ``__aenter__`` and ``__aexit__`` resolve the same underlying lock: a
    coroutine cannot migrate between loops mid-execution, so the lookup is
    stable for the lifetime of the ``async with`` block.
    """

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__(asyncio.Lock)

    async def acquire(self) -> bool:
        return await self._current().acquire()

    def release(self) -> None:
        self._current().release()

    def locked(self) -> bool:
        return self._current().locked()

    async def __aenter__(self) -> None:
        await self.acquire()
        return None

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.release()


class LoopLocalEvent(_LoopLocal[asyncio.Event]):
    """Drop-in ``asyncio.Event`` that is safe to hold in module-level state.

    Per-loop identity means a ``set()`` on one loop is not observable from
    another. That is not a behaviour change: waiting on a bare ``asyncio.Event``
    from a second loop raised instead of blocking, so no code could have relied
    on cross-loop signalling.

    One real difference: every method here needs a running loop, where a bare
    ``Event``'s ``set``/``clear``/``is_set`` did not. Calling one from a sync
    thread raises ``RuntimeError: no running event loop`` rather than mutating
    an Event that no loop is watching — loud instead of silently ineffective,
    but keep it in mind when adding call sites.
    """

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__(asyncio.Event)

    def set(self) -> None:
        self._current().set()

    def clear(self) -> None:
        self._current().clear()

    def is_set(self) -> bool:
        return self._current().is_set()

    async def wait(self) -> bool:
        return await self._current().wait()
