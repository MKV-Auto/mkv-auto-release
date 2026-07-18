"""Source-level guards for the #578 USB-bus-saturation gate.

Pins the gate's presence in both rip-start endpoints + the override flag
on both request schemas. Behavior is exercised in
``test_usb_bus_saturation_policy.py`` — these tests catch refactors that
silently remove the integration point.
"""

from __future__ import annotations

import inspect


def _src(fn) -> str:
    return inspect.getsource(fn)


def test_start_rip_invokes_bus_saturation_gate():
    from api.routers.jobs import start_rip
    src = _src(start_rip)
    assert "evaluate_bus_saturation" in src
    assert "usb_bus_saturation_risk" in src or "to_409_payload" in src


def test_start_rip_passes_force_override_from_request():
    """The gate must honour the user's explicit override flag — otherwise
    the contention warning becomes inescapable for setups that can't add
    a USB 3.0 port."""
    from api.routers.jobs import start_rip
    src = _src(start_rip)
    assert "force_concurrent_on_saturated_bus" in src
    assert "force_override=" in src


def test_start_rip_with_segment_reorder_also_invokes_gate():
    """The selective-rip path shares the same physical USB constraint."""
    from api.routers.jobs import start_rip_with_segment_reorder
    src = _src(start_rip_with_segment_reorder)
    assert "evaluate_bus_saturation" in src
    assert "force_concurrent_on_saturated_bus" in src


def test_JobCreate_schema_has_override_field():
    from api.schemas import JobCreate
    fields = JobCreate.model_fields
    assert "force_concurrent_on_saturated_bus" in fields
    # Default must be False so absent => gate runs.
    assert fields["force_concurrent_on_saturated_bus"].default is False


def test_segment_reorder_schema_has_override_field():
    """The selective-rip request schema mirrors the JobCreate flag so the
    UI can apply the same retry pattern to both endpoints."""
    from api.routers.jobs import SegmentReorderStartReq
    fields = SegmentReorderStartReq.model_fields
    assert "force_concurrent_on_saturated_bus" in fields
    assert fields["force_concurrent_on_saturated_bus"].default is False


def test_gate_failure_does_not_block_rip_start():
    """Defense against accidentally tightening the fail-open behaviour —
    the gate is best-effort, so any unexpected exception in
    ``evaluate_bus_saturation`` must not block the rip."""
    from api.routers.jobs import start_rip
    src = _src(start_rip)
    # The except-clause that catches non-HTTPException errors logs and
    # falls through. Pin on the distinctive log message substring.
    assert "USB saturation gate failed (fail-open)" in src
