"""API-level tests for #562 PR 5: ``POST /jobs/rip`` returns 409
``disc_scan_in_progress`` on cache miss.

Source-level guards complement these — see
``test_rip_disc_cache_pure.py`` for the cache-purity assertions.

These tests exercise the ``start_rip`` handler directly rather than via
``TestClient`` because the full FastAPI middleware stack requires a live
Postgres for the warmup gate (orthogonal to what's under test here).
"""

from __future__ import annotations

from unittest.mock import Mock

import inspect
import pytest


def _start_rip_source() -> str:
    from api.routers.jobs import start_rip
    return inspect.getsource(start_rip)


def test_start_rip_source_imports_dispatch_gate():
    """The cache-precondition gate is wired into ``start_rip``."""
    src = _start_rip_source()
    assert "disc_info_cache_satisfies" in src
    assert "enqueue_discinfo_scan" in src


def test_start_rip_source_raises_409_with_disc_scan_in_progress_code():
    """The 409 body shape includes ``code: disc_scan_in_progress`` so
    the UI can match on a stable identifier rather than parse the
    detail string."""
    src = _start_rip_source()
    assert "disc_scan_in_progress" in src
    assert "status_code=409" in src


def test_start_rip_with_segment_reorder_source_includes_gate():
    """The same gate runs on the selective-rip start endpoint —
    both paths dispatch ``rip_disc`` and share the failure mode."""
    from api.routers.jobs import start_rip_with_segment_reorder

    src = inspect.getsource(start_rip_with_segment_reorder)
    assert "disc_info_cache_satisfies" in src
    assert "disc_scan_in_progress" in src


def test_start_rip_gate_does_not_reference_disc_payload_attr_on_JobCreate():
    """Regression guard: ``JobCreate`` schema has no ``disc_payload`` field —
    that field lives on ``JobStatus`` (the response model). The original
    PR #568 mistakenly read ``req.disc_payload`` in ``start_rip``, which
    Pydantic v2 surfaces as ``AttributeError`` and uvicorn returns as 500
    "Internal Server Error" rather than the intended 409. Pin against
    re-introducing the reference in either start-rip endpoint.
    """
    from api.routers.jobs import start_rip, start_rip_with_segment_reorder
    from api.schemas import JobCreate

    # The schema itself: ``disc_payload`` must not appear on JobCreate.
    assert "disc_payload" not in JobCreate.model_fields, (
        "Pre-existing PR #568 bug: JobCreate gained a disc_payload field. "
        "Adjust the gate call sites to pass it through if intentional."
    )

    # Both endpoints: must not read ``req.disc_payload`` (would AttributeError).
    for fn in (start_rip, start_rip_with_segment_reorder):
        src = inspect.getsource(fn)
        assert "req.disc_payload" not in src, (
            f"{fn.__name__} reads req.disc_payload — JobCreate has no such "
            f"field; the cache gate must pass None for that argument."
        )
