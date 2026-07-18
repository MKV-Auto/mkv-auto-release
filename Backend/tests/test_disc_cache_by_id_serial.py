"""Tests for the ``by_id_serial`` co-key added in #540.

The cache must carry the stable hardware identity alongside ``mount_point``
and detect hardware swaps — i.e. when a mount_point is silently reassigned
by the kernel to a different physical drive (the catastrophic failure mode
reproduced in the 2026-06 diagnostic).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _patch_and_clear(monkeypatch):
    """Mirror the helper from ``test_disc_cache.py``: no-op persist, no PID
    process check, empty cache before and after."""

    import core.disc_cache as disc_cache

    monkeypatch.setattr("core.disc_cache._persist_unlocked", lambda: None)
    monkeypatch.setattr(
        "core.utils._find_makemkvcon_process_for_disc", lambda k: (None, None)
    )
    disc_cache.clear()
    yield
    disc_cache.clear()


class TestByIdSerialAlias:
    """``by_id_serial`` is stored as a secondary alias so lookups by serial
    succeed even when the caller only has the identity tuple."""

    def test_set_payload_stores_by_id_serial_alias(self):
        from core import disc_cache

        payload = {
            "disc_num": "1",
            "mount_point": "/dev/sr1",
            "by_id_serial": "1958040110900395",
            "disc_hash": "ABC123",
        }
        disc_cache.set_payload("/dev/sr1", payload)

        # Lookup by every key returns the same payload.
        assert disc_cache.get("/dev/sr1") == payload
        assert disc_cache.get("1958040110900395") == payload
        assert disc_cache.get("ABC123") == payload
        assert disc_cache.get_by_by_id_serial("1958040110900395") == payload

    def test_get_by_by_id_serial_fallback_scan(self):
        from core import disc_cache

        payload = {
            "disc_num": "2",
            "mount_point": "/dev/sr2",
            "by_id_serial": "AAAABBBB000E",
        }
        disc_cache.set_payload("/dev/sr2", payload)
        # Even if direct key lookup fails (e.g. dropped from the cache by
        # accident), the fallback scan over payloads should find it.
        with disc_cache._cache_lock:
            disc_cache._cache.pop("AAAABBBB000E", None)
        assert disc_cache.get_by_by_id_serial("AAAABBBB000E") == payload

    def test_get_by_by_id_serial_empty_returns_none(self):
        from core import disc_cache

        assert disc_cache.get_by_by_id_serial("") is None
        assert disc_cache.get_by_by_id_serial("nonexistent") is None


class TestHardwareSwapDetection:
    """When a mount_point is reused for a different physical drive, ALL
    cache entries keyed by the old by_id_serial must be purged."""

    def test_swap_purges_old_by_id_aliases(self):
        from core import disc_cache

        original = {
            "disc_num": "2",
            "mount_point": "/dev/sr1",
            "by_id_serial": "PIONEER-1958040110900395",
            "disc_hash": "PIONEERHASH",
            "disc_id": "pioneer-uuid",
        }
        disc_cache.set_payload("/dev/sr1", original)
        assert disc_cache.get("PIONEER-1958040110900395") == original
        assert disc_cache.get("PIONEERHASH") == original

        # Kernel renumbering: same mount_point, NEW physical drive (ASUS).
        replacement = {
            "disc_num": "1",
            "mount_point": "/dev/sr1",
            "by_id_serial": "ASUS-AAAABBBB000E",
            "disc_hash": "ASUSHASH",
            "disc_id": "asus-uuid",
        }
        disc_cache.set_payload("/dev/sr1", replacement)

        # The mount_point now resolves to the new drive's payload.
        assert disc_cache.get("/dev/sr1") == replacement
        # New aliases work.
        assert disc_cache.get("ASUS-AAAABBBB000E") == replacement
        assert disc_cache.get("ASUSHASH") == replacement
        # Old Pioneer aliases are PURGED so they don't accidentally resolve
        # against the new physical drive's mount_point.
        assert disc_cache.get("PIONEER-1958040110900395") is None
        assert disc_cache.get("PIONEERHASH") is None
        assert disc_cache.get("pioneer-uuid") is None

    def test_swap_does_not_purge_unrelated_drives(self):
        """A swap on /dev/sr1 must not affect cache entries for /dev/sr2."""

        from core import disc_cache

        sr1_old = {
            "disc_num": "0",
            "mount_point": "/dev/sr1",
            "by_id_serial": "SERIAL-OLD",
        }
        sr2 = {
            "disc_num": "2",
            "mount_point": "/dev/sr2",
            "by_id_serial": "SERIAL-SR2",
            "disc_hash": "SR2HASH",
        }
        disc_cache.set_payload("/dev/sr1", sr1_old)
        disc_cache.set_payload("/dev/sr2", sr2)

        sr1_new = {
            "disc_num": "1",
            "mount_point": "/dev/sr1",
            "by_id_serial": "SERIAL-NEW",
        }
        disc_cache.set_payload("/dev/sr1", sr1_new)

        # /dev/sr2's entries are untouched.
        assert disc_cache.get("/dev/sr2") == sr2
        assert disc_cache.get("SERIAL-SR2") == sr2
        assert disc_cache.get("SR2HASH") == sr2

    def test_same_drive_replug_keeps_serial_no_extra_purge(self):
        """If by_id_serial is unchanged (same physical drive, same disc),
        the swap path is not triggered — normal update happens."""

        from core import disc_cache

        first = {
            "disc_num": "1",
            "mount_point": "/dev/sr1",
            "by_id_serial": "STABLE-SERIAL",
            "disc_hash": "H1",
        }
        disc_cache.set_payload("/dev/sr1", first)

        second = {
            "disc_num": "1",
            "mount_point": "/dev/sr1",
            "by_id_serial": "STABLE-SERIAL",
            "disc_hash": "H2",  # disc swapped in same drive
        }
        disc_cache.set_payload("/dev/sr1", second)

        # The serial alias still points to the latest payload (which
        # reflects the new disc_hash).
        assert disc_cache.get("STABLE-SERIAL") == second
        # The old disc_hash alias is gone, the new one is present.
        assert disc_cache.get("H1") is None
        assert disc_cache.get("H2") == second

    def test_missing_by_id_serial_does_not_trigger_swap_path(self):
        """Payloads without ``by_id_serial`` (legacy callers) must keep
        working — the swap detection is a no-op for them."""

        from core import disc_cache

        payload = {
            "disc_num": "1",
            "mount_point": "/dev/sr1",
            "disc_hash": "LEGACY",
        }
        disc_cache.set_payload("/dev/sr1", payload)

        # No exception, lookups still work.
        assert disc_cache.get("/dev/sr1") == payload
        assert disc_cache.get("LEGACY") == payload
