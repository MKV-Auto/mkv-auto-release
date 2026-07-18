"""Tests for ``core.drive_policy.evaluate_drive_for_rip``.

The policy is the fail-closed multi-drive eligibility gate. Each
identity_source combined with the sole-drive-vs-many context produces a
deterministic outcome; all four sources are exercised against both contexts.
"""

from __future__ import annotations

import pytest

from core.drive_identity import DriveIdentity
from core.drive_policy import (
    CODE_UNIDENTIFIABLE,
    CODE_UNSAFE_WITH_OTHERS,
    Decision,
    evaluate_drive_for_rip,
)


def _id(source: str, serial: str = "S") -> DriveIdentity:
    return DriveIdentity(
        by_id_serial=serial,
        vendor="V",
        model="M",
        bus="b",
        by_id_name="",
        hardware_name=None,
        identity_source=source,  # type: ignore[arg-type]
    )


class TestSoleDrive:
    """When the drive is the only one attached, only ``unknown`` is blocked."""

    @pytest.mark.parametrize("source", ["by-id", "by-path", "sysfs"])
    def test_resolved_sources_allowed_alone(self, source):
        target = _id(source, "A")
        decision = evaluate_drive_for_rip(target, all_drives=[target])
        assert decision.allowed is True
        assert decision.code is None

    def test_unknown_blocked_even_alone(self):
        target = _id("unknown", "A")
        decision = evaluate_drive_for_rip(target, all_drives=[target])
        assert decision.allowed is False
        assert decision.code == CODE_UNIDENTIFIABLE


class TestMultipleDrivesAttached:
    """When other drives are also attached, only by-id is allowed."""

    def test_by_id_allowed_with_others(self):
        target = _id("by-id", "A")
        other = _id("by-id", "B")
        decision = evaluate_drive_for_rip(target, all_drives=[target, other])
        assert decision.allowed is True

    @pytest.mark.parametrize("source", ["by-path", "sysfs"])
    def test_degraded_blocked_with_others(self, source):
        target = _id(source, "A")
        other = _id("by-id", "B")
        decision = evaluate_drive_for_rip(target, all_drives=[target, other])
        assert decision.allowed is False
        assert decision.code == CODE_UNSAFE_WITH_OTHERS

    def test_degraded_message_names_fallback_source(self):
        target = _id("sysfs", "A")
        other = _id("by-id", "B")
        decision = evaluate_drive_for_rip(target, all_drives=[target, other])
        assert "sysfs" in (decision.message or "")

    def test_unknown_blocked_with_others_too(self):
        target = _id("unknown", "A")
        other = _id("by-id", "B")
        decision = evaluate_drive_for_rip(target, all_drives=[target, other])
        assert decision.allowed is False
        assert decision.code == CODE_UNIDENTIFIABLE


class TestEdgeCases:
    def test_target_present_in_all_drives_list_doesnt_count_as_other(self):
        """The policy must use serial equality, not Python ``is``, so a target
        passed in the all_drives list (which is the normal call shape) is
        recognised as itself."""

        target = _id("by-path", "A")
        decision = evaluate_drive_for_rip(target, all_drives=[target])
        assert decision.allowed is True

    def test_decision_is_immutable(self):
        """Defence against accidental mutation of policy outcomes."""

        with pytest.raises(Exception):
            Decision(allowed=True).allowed = False  # type: ignore[misc]
