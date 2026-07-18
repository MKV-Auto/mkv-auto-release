"""Tests for ``core.disc_scan_dispatch`` (#562 PR 5).

Covers the cache-precondition gate that stops ``rip_disc`` from being
enqueued before disc info is cached. The gate decides between three
sources of truth (request payload, mount_point cache, disc_num cache);
this suite walks them in priority order.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core.disc_scan_dispatch import (
    MINIMUM_CACHED_FIELDS,
    disc_info_cache_satisfies,
    enqueue_discinfo_scan,
)


class TestDiscInfoCacheSatisfies:
    def test_payload_with_disc_hash_wins(self, monkeypatch):
        """Request payload counts when it carries ``disc_hash`` —
        no cache lookup needed."""
        monkeypatch.setattr(
            "core.disc_scan_dispatch.cache_get",
            lambda *a, **k: pytest.fail("cache_get must not be called"),
        )

        assert disc_info_cache_satisfies(
            "/dev/sr0", "1", {"disc_hash": "abc123"}
        )

    def test_payload_missing_disc_hash_falls_through_to_cache(self, monkeypatch):
        calls: list[str] = []

        def fake_cache_get(key):
            calls.append(key)
            return {"disc_hash": "abc123"} if key == "/dev/sr0" else None

        monkeypatch.setattr("core.disc_scan_dispatch.cache_get", fake_cache_get)

        assert disc_info_cache_satisfies("/dev/sr0", "1", {"other": "field"})
        assert calls == ["/dev/sr0"]

    def test_mount_point_cache_hit(self, monkeypatch):
        monkeypatch.setattr(
            "core.disc_scan_dispatch.cache_get",
            lambda k: {"disc_hash": "abc"} if k == "/dev/sr1" else None,
        )

        assert disc_info_cache_satisfies("/dev/sr1", None, None)

    def test_disc_num_cache_fallback(self, monkeypatch):
        monkeypatch.setattr(
            "core.disc_scan_dispatch.cache_get",
            lambda k: {"disc_hash": "abc"} if k == "1" else None,
        )

        assert disc_info_cache_satisfies("/dev/sr1", "1", None)

    def test_no_source_satisfies(self, monkeypatch):
        monkeypatch.setattr("core.disc_scan_dispatch.cache_get", lambda k: None)

        assert not disc_info_cache_satisfies("/dev/sr1", "1", None)
        assert not disc_info_cache_satisfies("/dev/sr1", "1", {})
        assert not disc_info_cache_satisfies("/dev/sr1", "1", {"disc_hash": ""})

    def test_payload_with_empty_disc_hash_falls_through(self, monkeypatch):
        """An empty ``disc_hash`` is not a valid cache hit — it's likely
        a partial payload from a failed scan."""
        monkeypatch.setattr("core.disc_scan_dispatch.cache_get", lambda k: None)

        assert not disc_info_cache_satisfies(
            "/dev/sr0", "1", {"disc_hash": ""}
        )

    def test_minimum_fields_contract_is_disc_hash(self):
        """Guard against accidentally expanding the contract — the rip
        task only needs disc_hash to proceed; ``titles``/``info_log``
        are recoverable via the per-disc scan at start-of-rip."""
        assert MINIMUM_CACHED_FIELDS == ("disc_hash",)


class TestEnqueueDiscinfoScan:
    def test_dispatches_to_celery_queue(self, monkeypatch):
        captured = {}

        class _FakeResult:
            id = "task-xyz"

        class _FakeTask:
            @staticmethod
            def apply_async(*, args, queue):
                captured["args"] = args
                captured["queue"] = queue
                return _FakeResult()

        # Replace the lazy import target at the module path the helper uses.
        import sys
        import types

        fake_workers = types.ModuleType("workers.tasks")
        fake_workers.discinfo_scan = _FakeTask
        monkeypatch.setitem(sys.modules, "workers.tasks", fake_workers)

        task_id = enqueue_discinfo_scan("1", "/dev/sr1")

        assert task_id == "task-xyz"
        assert captured["args"] == ["1", "/dev/sr1"]
        assert captured["queue"] == "celery"

    def test_returns_none_on_dispatch_failure(self, monkeypatch):
        import sys
        import types

        class _FakeTask:
            @staticmethod
            def apply_async(*, args, queue):
                raise RuntimeError("broker unavailable")

        fake_workers = types.ModuleType("workers.tasks")
        fake_workers.discinfo_scan = _FakeTask
        monkeypatch.setitem(sys.modules, "workers.tasks", fake_workers)

        # Must not raise — gate logic still wants to return 409 even when
        # dispatch fails, so callers don't have to handle two error paths.
        assert enqueue_discinfo_scan("1", "/dev/sr1") is None
