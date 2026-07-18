"""Tests for ``Disc._makemkv_source_spec`` with stable-identity rebinding.

When ``Disc.by_id_serial`` is set (the post-#540 hookup), the spec method
must re-resolve the mount_point at call time so a kernel-renumbered
``/dev/srN`` cannot route the command to the wrong physical drive.
"""

from __future__ import annotations

import pytest

from core.disc import Disc


class TestNoByIdSerial:
    """Backwards compat: legacy callers without ``by_id_serial`` keep working."""

    def test_uses_cached_mount_point(self):
        disc = Disc("1", "/dev/sr1")
        assert disc._makemkv_source_spec() == "dev:/dev/sr1"

    def test_falls_back_to_disc_num_when_no_mount_point(self):
        disc = Disc("2", "")
        assert disc._makemkv_source_spec() == "disc:2"

    def test_raises_when_nothing_resolvable(self):
        disc = Disc("not-a-digit", "")
        with pytest.raises(ValueError):
            disc._makemkv_source_spec()


class TestWithByIdSerial:
    """When ``by_id_serial`` is set, prefer a fresh resolution."""

    def test_uses_fresh_mount_point_when_identity_matches(self, monkeypatch):
        disc = Disc("1", "/dev/sr1")
        disc.by_id_serial = "PIONEER-SERIAL"

        monkeypatch.setattr(
            "core.drive_identity.resolve_current_mount_point_for_serial",
            lambda serial, **kw: "/dev/sr1",
        )

        assert disc._makemkv_source_spec() == "dev:/dev/sr1"

    def test_uses_freshly_resolved_mount_point_after_renumbering(self, monkeypatch, caplog):
        """The cached mount_point became stale (kernel reassigned sr1 → sr2).
        The spec method must follow the by_id_serial to the NEW mount_point."""

        disc = Disc("1", "/dev/sr1")
        disc.by_id_serial = "PIONEER-SERIAL"

        monkeypatch.setattr(
            "core.drive_identity.resolve_current_mount_point_for_serial",
            lambda serial, **kw: "/dev/sr2",  # kernel moved it
        )

        with caplog.at_level("WARNING"):
            spec = disc._makemkv_source_spec()

        assert spec == "dev:/dev/sr2"
        assert any("swap detected" in rec.message for rec in caplog.records)

    def test_raises_when_drive_disconnected_entirely(self, monkeypatch):
        """Drive has been unplugged; refuse to issue makemkvcon against the
        stale mount_point — that's how you write to the wrong device."""

        disc = Disc("1", "/dev/sr1")
        disc.by_id_serial = "PIONEER-SERIAL"

        monkeypatch.setattr(
            "core.drive_identity.resolve_current_mount_point_for_serial",
            lambda serial, **kw: None,
        )

        with pytest.raises(ValueError, match="no longer attached"):
            disc._makemkv_source_spec()
