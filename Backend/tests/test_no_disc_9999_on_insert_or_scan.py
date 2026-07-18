"""Regression tests for #562 PR 4: the pre-flight ``disc:9999`` enumeration
is removed from both the disc-insert handler and the scan-refresh path.

That enumeration ran against ALL drives, so any in-flight ``mkv dev:`` on a
sibling drive got hit with the same lock and surfaced as ``MSG:5010 "Failed
to open disc"`` on the new drive's prep. The per-disc ``info dev:`` scan
that follows already refreshes the path→DRV index for the device under
test via ``upsert_makemkv_drive_cache_for_mount``, so the pre-flight was
load-bearing only by accident.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core import _drive_operations as drv_ops


def _strict_no_disc_9999(info_dev_response: str = ""):
    """Patch ``run_makemkv`` so any ``disc:9999`` invocation explodes."""

    def side_effect(cmd, **_kw):
        cmd_str = cmd if isinstance(cmd, str) else str(cmd)
        if "disc:9999" in cmd_str:
            raise AssertionError(
                "disc:9999 enumeration must not run after #562 PR 4 — "
                f"command was: {cmd_str!r}"
            )
        return (info_dev_response, None)

    return side_effect


class TestLoadDiscinfoRefreshNoDisc9999:
    """``_load_discinfo(refresh=True)`` must not call ``disc:9999`` —
    the per-disc ``info dev:`` scan refreshes the index map on its own."""

    def test_refresh_path_runs_only_info_dev(self, monkeypatch):
        sample_info_dev = (
            'DRV:0,256,999,0,"BD-ROM","TEST_DISC","/dev/sr0"\n'
            'MSG:3104,0,0,"Hash: abc123"\n'
        )
        side_effect = _strict_no_disc_9999(sample_info_dev)

        monkeypatch.setattr("core.utils.run_makemkv", side_effect)
        monkeypatch.setattr("core._drive_operations.run_makemkv", side_effect)
        monkeypatch.setattr(
            "core._drive_operations.hash_media_disc", lambda *a, **k: "abc123"
        )
        monkeypatch.setattr(
            "core._drive_operations.get_disc_size_bytes_for_mount_point",
            lambda mp: None,
        )

        result = drv_ops._load_discinfo(
            "0", "/dev/sr0", refresh=True, source="test"
        )

        assert result["disc_hash"] == "abc123"
        assert result["mount_point"] == "/dev/sr0"


class TestHandleDiscInsertNoDisc9999:
    """``handle_disc_insert`` must not call ``disc:9999`` — same risk: would
    race a sibling drive's in-flight rip."""

    def test_insert_runs_only_info_dev(self, monkeypatch):
        side_effect = _strict_no_disc_9999(
            'DRV:0,256,999,0,"BD-ROM","NEW_DISC","/dev/sr1"\n'
        )

        monkeypatch.setattr("core.utils.run_makemkv", side_effect)
        monkeypatch.setattr("core._drive_operations.run_makemkv", side_effect)
        monkeypatch.setattr(
            "core._drive_operations.hash_media_disc", lambda *a, **k: "newhash"
        )

        # Insert-scan single-flight guard: free the slot for the test.
        from core.disc_slot_state import reset_disc_slot_state_for_tests

        reset_disc_slot_state_for_tests()

        # disc_manager.on_disc_inserted is reachable but exercises the full
        # cache-set pipeline — stub it to keep this test focused on the
        # MakeMKV-invocation contract.
        with patch("core.disc_manager.on_disc_inserted"):
            with patch(
                "core._drive_operations.clear_keys_by_mount_point",
                return_value=None,
            ):
                result = drv_ops.handle_disc_insert("0", "/dev/sr1")

        assert result["status"] == "ok"
