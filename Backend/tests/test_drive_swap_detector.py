"""Tests for ``core.drive_swap_detector.detect_drive_swaps``.

Same-mount_point identity changes are reported; disappearances and first
appearances are not. Maps an empty serial on either side as "can't tell"
and is silent rather than risk a false positive.
"""

from __future__ import annotations

import pytest

from core.drive_identity import DriveIdentity
from core.drive_swap_detector import DriveSwap, detect_drive_swaps


def _id(serial: str, mount_point: str = "/dev/sr1") -> DriveIdentity:
    return DriveIdentity(
        by_id_serial=serial,
        vendor="V",
        model="M",
        bus="b",
        by_id_name="",
        hardware_name=None,
        identity_source="by-id",
    )


class TestNoSwap:
    def test_empty_maps(self):
        assert detect_drive_swaps({}, {}) == []

    def test_identical_maps(self):
        prev = {"/dev/sr1": _id("A"), "/dev/sr2": _id("B")}
        cur = {"/dev/sr1": _id("A"), "/dev/sr2": _id("B")}
        assert detect_drive_swaps(prev, cur) == []

    def test_drive_disappeared_is_not_a_swap(self):
        prev = {"/dev/sr1": _id("A")}
        cur: dict = {}
        assert detect_drive_swaps(prev, cur) == []

    def test_drive_first_appearance_is_not_a_swap(self):
        prev: dict = {}
        cur = {"/dev/sr1": _id("A")}
        assert detect_drive_swaps(prev, cur) == []


class TestSwapDetected:
    def test_simple_swap(self):
        prev = {"/dev/sr1": _id("PIONEER-SERIAL")}
        cur = {"/dev/sr1": _id("ASUS-SERIAL")}

        swaps = detect_drive_swaps(prev, cur)

        assert swaps == [
            DriveSwap(
                mount_point="/dev/sr1",
                previous_serial="PIONEER-SERIAL",
                current_serial="ASUS-SERIAL",
            )
        ]

    def test_swap_on_one_mount_point_does_not_affect_others(self):
        prev = {
            "/dev/sr1": _id("PIONEER", "/dev/sr1"),
            "/dev/sr2": _id("ASUS", "/dev/sr2"),
        }
        cur = {
            "/dev/sr1": _id("ASUS", "/dev/sr1"),  # ASUS moved to sr1 (diagnostic scenario)
            "/dev/sr2": _id("ASUS", "/dev/sr2"),
        }

        swaps = detect_drive_swaps(prev, cur)
        assert len(swaps) == 1
        assert swaps[0].mount_point == "/dev/sr1"
        assert swaps[0].previous_serial == "PIONEER"
        assert swaps[0].current_serial == "ASUS"

    def test_multiple_swaps(self):
        prev = {
            "/dev/sr1": _id("A", "/dev/sr1"),
            "/dev/sr2": _id("B", "/dev/sr2"),
        }
        cur = {
            "/dev/sr1": _id("X", "/dev/sr1"),
            "/dev/sr2": _id("Y", "/dev/sr2"),
        }

        swaps = detect_drive_swaps(prev, cur)
        assert {s.mount_point for s in swaps} == {"/dev/sr1", "/dev/sr2"}


class TestDefensiveEdgeCases:
    def test_empty_previous_serial_silent(self):
        prev = {"/dev/sr1": _id("")}
        cur = {"/dev/sr1": _id("A")}
        # Can't say whether this is a swap or a first identity resolution.
        assert detect_drive_swaps(prev, cur) == []

    def test_empty_current_serial_silent(self):
        prev = {"/dev/sr1": _id("A")}
        cur = {"/dev/sr1": _id("")}
        assert detect_drive_swaps(prev, cur) == []

    def test_whitespace_only_serial_treated_as_empty(self):
        prev = {"/dev/sr1": _id("   ")}
        cur = {"/dev/sr1": _id("A")}
        assert detect_drive_swaps(prev, cur) == []
