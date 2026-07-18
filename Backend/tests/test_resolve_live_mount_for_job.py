"""Tests for ``api.routers.websockets._resolve_live_mount_for_job`` (#576).

The coordinator's unfinished-job entries used to send
``Job.mount_point`` verbatim — but that column is set at job-create time
and never updated, so when a USB optical drive renumbers across hot-plug
the failed-job card on the frontend displays the wrong device path.

The helper now resolves the live mount via ``drive_by_id_serial`` (the
stable identity persisted in #549/#556). Tests stub the resolver to keep
this unit pure.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from api.routers import websockets


def _mk_job(*, mount_point: str | None = None, serial: str | None = None):
    job = MagicMock()
    job.mount_point = mount_point
    job.drive_by_id_serial = serial
    return job


class TestResolveLiveMount:
    def test_live_serial_resolution_wins_over_persisted(self, monkeypatch):
        monkeypatch.setattr(
            "core.drive_identity.resolve_current_mount_point_for_serial",
            lambda serial: "/dev/sr0" if serial == "PIONEER-001" else None,
        )
        job = _mk_job(mount_point="/dev/sr2", serial="PIONEER-001")
        assert websockets._resolve_live_mount_for_job(job) == "/dev/sr0"

    def test_falls_back_to_persisted_when_drive_not_attached(self, monkeypatch):
        # The drive identified by this serial isn't currently attached.
        monkeypatch.setattr(
            "core.drive_identity.resolve_current_mount_point_for_serial",
            lambda serial: None,
        )
        job = _mk_job(mount_point="/dev/sr2", serial="PIONEER-001")
        assert websockets._resolve_live_mount_for_job(job) == "/dev/sr2"

    def test_no_serial_falls_back_to_persisted(self, monkeypatch):
        """ATAPI/SATA optical drives don't have a by-id serial."""
        resolver = MagicMock()
        monkeypatch.setattr(
            "core.drive_identity.resolve_current_mount_point_for_serial",
            resolver,
        )
        job = _mk_job(mount_point="/dev/sr0", serial=None)
        assert websockets._resolve_live_mount_for_job(job) == "/dev/sr0"
        # Resolver must not be called when there's no serial to look up.
        resolver.assert_not_called()

    def test_empty_string_serial_treated_as_no_serial(self, monkeypatch):
        resolver = MagicMock()
        monkeypatch.setattr(
            "core.drive_identity.resolve_current_mount_point_for_serial",
            resolver,
        )
        job = _mk_job(mount_point="/dev/sr1", serial="")
        assert websockets._resolve_live_mount_for_job(job) == "/dev/sr1"
        resolver.assert_not_called()

    def test_whitespace_serial_treated_as_no_serial(self, monkeypatch):
        resolver = MagicMock()
        monkeypatch.setattr(
            "core.drive_identity.resolve_current_mount_point_for_serial",
            resolver,
        )
        job = _mk_job(mount_point="/dev/sr1", serial="   ")
        assert websockets._resolve_live_mount_for_job(job) == "/dev/sr1"
        resolver.assert_not_called()

    def test_resolver_exception_fails_open_to_persisted(self, monkeypatch):
        """If the identity layer errors (sysfs unreadable, etc.) the
        carousel still gets a value rather than null — the persisted
        mount is the best fallback."""

        def boom(_serial):
            raise RuntimeError("sysfs unreadable")

        monkeypatch.setattr(
            "core.drive_identity.resolve_current_mount_point_for_serial",
            boom,
        )
        job = _mk_job(mount_point="/dev/sr2", serial="PIONEER-001")
        assert websockets._resolve_live_mount_for_job(job) == "/dev/sr2"

    def test_no_serial_no_persisted_returns_none(self, monkeypatch):
        resolver = MagicMock()
        monkeypatch.setattr(
            "core.drive_identity.resolve_current_mount_point_for_serial",
            resolver,
        )
        job = _mk_job(mount_point=None, serial=None)
        assert websockets._resolve_live_mount_for_job(job) is None
        resolver.assert_not_called()

    def test_resolved_overrides_even_when_persisted_matches(self, monkeypatch):
        """If the persisted value coincidentally still matches the live
        mount, we still prefer the resolved path — no need to special-case
        the equal-string scenario."""
        monkeypatch.setattr(
            "core.drive_identity.resolve_current_mount_point_for_serial",
            lambda serial: "/dev/sr1",
        )
        job = _mk_job(mount_point="/dev/sr1", serial="PIONEER-001")
        assert websockets._resolve_live_mount_for_job(job) == "/dev/sr1"


class TestCoordinatorStateUsesResolver:
    """Source-level guard: the helper is wired into the coordinator state
    builder. Catches a refactor that accidentally reverts to
    ``job.mount_point`` verbatim."""

    def test_initial_state_builder_calls_resolve_live_mount(self):
        import inspect
        src = inspect.getsource(websockets._build_initial_coordinator_state_sync)
        assert "_resolve_live_mount_for_job" in src, (
            "The coordinator state builder must resolve the live mount "
            "via the #576 helper. Don't pass job.mount_point verbatim."
        )
