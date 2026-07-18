"""Regression test for #560: the rip-start request paths must not call
``_cleanup_stale_jobs`` inline.

The 2026-06-19 live verification of the multi-drive cluster caught a bug
where ``POST /jobs/rip`` ran ``_cleanup_stale_jobs`` before creating the
new job. When a second rip was started within ~15 seconds of the first,
the cleanup observed the first rip's task (still in prep, with no
``rip_pid`` and no acquired lock) and marked it as a "service restart"
orphan — failing a job that was actually progressing fine.

The fix is to remove the inline call from both rip-start endpoints; the
periodic task at ``Backend/api/main.py:_periodic_stale_job_cleanup`` runs
the same logic every N seconds without the request-path race.
"""

from __future__ import annotations

import inspect


def _source(func) -> str:
    return inspect.getsource(func)


def test_post_jobs_rip_does_not_call_cleanup_stale_jobs():
    """``start_rip`` (POST /jobs/rip) must not contain a direct call to
    ``_cleanup_stale_jobs``. The periodic background task owns that work."""

    from api.routers.jobs import start_rip

    src = _source(start_rip)
    assert "_cleanup_stale_jobs(" not in src, (
        "POST /jobs/rip must not call _cleanup_stale_jobs on the request "
        "path. See #560: the inline call races with concurrent rip starts "
        "and false-orphans the sibling task. The periodic task at "
        "Backend/api/main.py:_periodic_stale_job_cleanup runs the same "
        "logic safely off-request."
    )


def test_post_jobs_rip_with_segment_reorder_does_not_call_cleanup_stale_jobs():
    """The selective-rip start endpoint shares the same failure mode and
    must also not call the cleanup inline."""

    from api.routers.jobs import start_rip_with_segment_reorder

    src = _source(start_rip_with_segment_reorder)
    assert "_cleanup_stale_jobs(" not in src, (
        "POST /jobs/rip-with-segment-reorder must not call "
        "_cleanup_stale_jobs on the request path. See #560."
    )


def test_periodic_cleanup_task_still_invokes_cleanup_stale_jobs():
    """Defense against accidental over-correction: the periodic task in
    ``api.main`` must STILL call ``_cleanup_stale_jobs`` — that's where the
    work now lives exclusively."""

    from api import main as api_main

    src = inspect.getsource(api_main)
    assert "_cleanup_stale_jobs(" in src, (
        "Backend/api/main.py must continue to call _cleanup_stale_jobs from "
        "the periodic background task. If this assertion fails, stale "
        "orphan jobs will never be cleaned up after #560's removal of the "
        "inline call sites."
    )
