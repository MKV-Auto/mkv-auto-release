"""Tests for ``core.utils.build_drive_api_dict``.

Verifies that the dict shape exposed via the ``/drives/drives`` endpoint
carries the stable-identity fields introduced by #540 alongside the legacy
``mount_point`` / ``disc_num`` / ``drive_hardware_name`` triple.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core import utils


from core.drive_identity import DriveIdentity


class TestBuildDriveApiDictShape:
    """All callers of ``build_drive_api_dict`` consume this dict — guard the
    shape, especially the new #540 fields.

    Identity resolution itself is exercised in ``test_drive_identity.py``;
    here we stub it out so we're only asserting the dict-shape contract.
    """

    def test_dict_carries_identity_fields(self, monkeypatch):
        stub = DriveIdentity(
            by_id_serial="1958040110900395",
            vendor="PIONEER",
            model="BD-RW BDR-XD06U",
            bus="usb",
            by_id_name="usb-PIONEER_BD-RW_BDR-XD06U_1958040110900395-0:0",
            hardware_name="PIONEER BD-RW",
            identity_source="by-id",
        )
        monkeypatch.setattr(utils, "resolve_drive_identity", lambda mp, **kw: stub)
        monkeypatch.setattr(utils, "get_drive_hardware_map", lambda: {"/dev/sr1": "PIONEER BD-RW"})

        d = utils.build_drive_api_dict("2", "/dev/sr1")

        # Legacy fields preserved.
        assert d["disc_num"] == "2"
        assert d["mount_point"] == "/dev/sr1"
        assert d["makemkv_disc_index"] == "2"
        assert d["drive_hardware_name"] == "PIONEER BD-RW"
        assert d["friendly_label"] == "Drive 3"

        # New stable-identity fields (#540).
        assert d["by_id_serial"] == "1958040110900395"
        assert d["identity_source"] == "by-id"
        assert d["multi_drive_safe"] is True
        assert d["vendor"] == "PIONEER"
        assert d["model"] == "BD-RW BDR-XD06U"
        assert d["bus"] == "usb"

    def test_unresolved_drive_marked_unsafe(self, monkeypatch):
        stub = DriveIdentity(
            by_id_serial="unknown:sr9",
            vendor="",
            model="",
            bus="unknown",
            by_id_name="",
            hardware_name=None,
            identity_source="unknown",
        )
        monkeypatch.setattr(utils, "resolve_drive_identity", lambda mp, **kw: stub)
        monkeypatch.setattr(utils, "get_drive_hardware_map", lambda: {})

        d = utils.build_drive_api_dict("3", "/dev/sr9")

        assert d["identity_source"] == "unknown"
        assert d["multi_drive_safe"] is False
        assert d["by_id_serial"] == "unknown:sr9"


class TestHardwareNameFallback:
    """After #562 PR 2 the registry feeds ``/drives`` before any per-drive
    MakeMKV scan runs, so the hardware label map is empty for a brief window.
    ``build_drive_api_dict`` must fall back to the by-id identity's
    vendor+model so the UI shows a real label instead of just the mount path.
    """

    def test_falls_back_to_identity_vendor_model_when_makemkv_map_empty(
        self, monkeypatch
    ):
        stub = DriveIdentity(
            by_id_serial="1958040110900395",
            vendor="PIONEER",
            model="BD-RW BDR-XD06U",
            bus="usb",
            by_id_name="usb-PIONEER_BD-RW_BDR-XD06U_1958040110900395-0:0",
            hardware_name=None,
            identity_source="by-id",
        )
        monkeypatch.setattr(utils, "resolve_drive_identity", lambda mp, **kw: stub)
        monkeypatch.setattr(utils, "get_drive_hardware_map", lambda: {})

        d = utils.build_drive_api_dict("0", "/dev/sr1")

        assert d["drive_hardware_name"] == "PIONEER BD-RW BDR-XD06U"
        assert "PIONEER BD-RW BDR-XD06U" in d["name"]

    def test_makemkv_label_wins_when_both_present(self, monkeypatch):
        """When MakeMKV has already populated the hardware map, prefer it —
        it carries the MakeMKV-specific label the rest of the system expects."""
        stub = DriveIdentity(
            by_id_serial="S",
            vendor="PIONEER",
            model="BD-RW BDR-XD06U",
            bus="usb",
            by_id_name="",
            hardware_name=None,
            identity_source="by-id",
        )
        monkeypatch.setattr(utils, "resolve_drive_identity", lambda mp, **kw: stub)
        monkeypatch.setattr(
            utils, "get_drive_hardware_map", lambda: {"/dev/sr1": "USBDVD"}
        )

        d = utils.build_drive_api_dict("0", "/dev/sr1")

        assert d["drive_hardware_name"] == "USBDVD"

    def test_blank_when_neither_source_has_anything(self, monkeypatch):
        stub = DriveIdentity(
            by_id_serial="unknown:sr9",
            vendor="",
            model="",
            bus="unknown",
            by_id_name="",
            hardware_name=None,
            identity_source="unknown",
        )
        monkeypatch.setattr(utils, "resolve_drive_identity", lambda mp, **kw: stub)
        monkeypatch.setattr(utils, "get_drive_hardware_map", lambda: {})

        d = utils.build_drive_api_dict("0", "/dev/sr9")

        assert d["drive_hardware_name"] == ""
