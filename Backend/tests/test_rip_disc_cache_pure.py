"""Regression tests for #562 PR 5: the rip task is cache-pure.

After PR 5, ``rip_disc`` must NOT call ``ensure_makemkv_index_for_mount``
(which would chain into ``disc:9999``) and must NOT fall back to
``disc.load_db_info(allow_reentrant=True)`` on cache miss — that path was
the MSG:5010 root cause when a sibling drive's rip was in flight.

Source-level guards. Behavioral tests of the dispatch gate live in
``test_disc_scan_dispatch.py``; the API-level 409 contract lives in
``test_rip_start_disc_scan_in_progress.py``.
"""

from __future__ import annotations

import inspect


def _source(func) -> str:
    return inspect.getsource(func)


def test_rip_disc_does_not_call_ensure_makemkv_index_for_mount():
    """#562 PR 5 dropped the re-resolve at the head of the task body;
    ``_makemkv_source_spec`` already targets ``dev:{mount_point}``."""
    from workers.tasks import rip_disc

    src = _source(rip_disc)
    assert "ensure_makemkv_index_for_mount" not in src, (
        "rip_disc must not call ensure_makemkv_index_for_mount after "
        "#562 PR 5 — it chained into disc:9999 and contended with "
        "any concurrent mkv dev: on a sibling drive."
    )


def test_rip_disc_does_not_call_load_db_info_allow_reentrant():
    """The ``disc.load_db_info(allow_reentrant=True)`` fallback opened
    the disc inline on cache miss and is the MSG:5010 root cause. The
    API-level gate at ``start_rip`` returns 409 on miss; the rip task
    must fail loudly if it ever reaches a cache miss anyway (cache wiped
    between gate and dispatch)."""
    from workers.tasks import rip_disc

    src = _source(rip_disc)
    assert "load_db_info(allow_reentrant=True)" not in src, (
        "rip_disc must not call disc.load_db_info(allow_reentrant=True) "
        "after #562 PR 5 — see test docstring for context."
    )


def test_discinfo_scan_task_exists_on_celery_queue():
    """The new task introduced by PR 5 — dispatched on cache miss from
    the API-level gate. Must live on the same ``celery`` queue as
    ``load_disc_info`` so it never contends with the ``rip`` queue."""
    from workers.tasks import discinfo_scan

    assert callable(discinfo_scan), "discinfo_scan task must exist"
    # Celery wraps the task; the name is on .name when bound to celery_app.
    name = getattr(discinfo_scan, "name", None)
    assert name == "discinfo_scan", f"unexpected name: {name!r}"
