"""Regression tests for #576 — rip-start re-resolves mount_point from the
live disc_cache and clears stale ``Disc.scan_state='failed'`` flags.

USB optical drives renumber across hot-plug. The frontend's card carries
the mount_point that was current when the card was first rendered — by
the time the user clicks Rip, the disc may live at a different
``/dev/srN``. The disc_cache always carries the freshest mount_point
because it's repopulated on every successful insert scan; ``start_rip``
must trust that source over the request payload.

A second failure mode this PR closes: a Disc row whose ``scan_state``
was set to ``'failed'`` in a prior session blocks the gatekeeper even
when the current cache is healthy. Clear the stale flag when the cache
shows a fresh ``disc_hash`` matching the DB row's ``content_hash``.

Pure source-level guards. Behavioral coverage of the disc_cache helpers
themselves lives in their own suites.
"""

from __future__ import annotations

import inspect


def _source(fn) -> str:
    return inspect.getsource(fn)


def test_start_rip_re_resolves_mount_point_from_disc_hash_cache():
    """The handler must consult ``disc_cache.get(disc_hash)`` (or equivalent)
    and override ``req.mount_point`` when the cache shows a different
    mount_point — that's the drive renumbering case."""
    from api.routers.jobs import start_rip

    src = _source(start_rip)
    # The override re-assigns to ``req.mount_point``; if a future refactor
    # drops the re-resolve, the assignment goes too.
    assert "req.mount_point = cached_mp" in src, (
        "start_rip must overwrite req.mount_point with the cache's "
        "current mount when they differ. See #576: USB drives renumber, "
        "and the frontend's card carries the stale value."
    )
    # The log line uses a stable substring the test pins on so we can
    # detect accidental removal of the diagnostic too.
    assert "drive renumbering" in src


def test_start_rip_clears_stale_failed_scan_state_when_cache_is_healthy():
    """The handler must clear ``Disc.scan_state='failed'`` (and
    ``last_scan_error``) when the live cache has a payload whose
    ``disc_hash`` matches the DB row. Without this, the gatekeeper
    refuses every rip on a disc that previously failed — even after the
    disc has been re-inserted and re-scanned successfully."""
    from api.routers.jobs import start_rip

    src = _source(start_rip)
    # The clear sets scan_state to None and commits.
    assert "scan_state = None" in src
    assert "last_scan_error = None" in src
    # And the guard ties the clear to the cache's matching hash, not a
    # blanket clear (which would mask real failures).
    assert "cache_has_fresh_hash" in src or "matching disc_hash" in src


def test_clear_branch_does_not_run_when_disc_record_is_none():
    """If the request doesn't include a ``disc_id`` (or the disc row
    can't be loaded), the clear branch must short-circuit — there's no
    DB row to mutate. Pin on the ``disc_record is not None`` guard so
    a future refactor doesn't NPE here."""
    from api.routers.jobs import start_rip

    src = _source(start_rip)
    assert "disc_record is not None" in src
