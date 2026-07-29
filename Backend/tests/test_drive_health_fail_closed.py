"""#723 / #724 — an unresponsive drive must fail closed, not serve stale identity.

Covered here:

1. ``core.drive_health`` classification + registry semantics.
2. ``handle_disc_insert`` aborts on a drive-level hash failure: no info scan,
   no scan-complete notification, cache purged, drive marked unhealthy, and
   the slot is NOT left "stable".
3. A disc-level hash failure (no BDMV/VIDEO_TS) still proceeds — MakeMKV can
   read such discs via direct disc access.
4. ``POST /jobs/rip`` refuses with 409 ``drive_unresponsive`` for an unhealthy
   drive.
5. The MakeMKV DRV volume label is used as the ``info_title`` fallback when a
   scan produces no CINFO title.
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from core import drive_health
from core.utils import MakeMKVError

MOUNT_TIMEOUT_MESSAGE = (
    "Drive is not responding (mount timed out after 30s). Try power cycling the drive."
)


@pytest.fixture(autouse=True)
def _clean_drive_health():
    drive_health.reset_drive_health_for_tests()
    yield
    drive_health.reset_drive_health_for_tests()


# --------------------------------------------------------------------------
# 1. Classification + registry
# --------------------------------------------------------------------------

class TestDriveFaultClassification:
    def test_mount_timeout_is_a_drive_fault(self):
        assert drive_health.is_drive_fault(MakeMKVError(MOUNT_TIMEOUT_MESSAGE))

    def test_message_match_without_makemkv_error_type(self):
        assert drive_health.is_drive_fault(
            RuntimeError("Drive is not responding (mount timed out after 30s).")
        )

    def test_missing_disc_structure_is_not_a_drive_fault(self):
        """A disc with no BDMV/VIDEO_TS mounts fine — MakeMKV may still read it."""
        assert not drive_health.is_drive_fault(
            FileNotFoundError("No Blu-ray or DVD structure under '/tmp/mnt'")
        )

    def test_none_is_not_a_fault(self):
        assert not drive_health.is_drive_fault(None)


class TestDriveHealthRegistry:
    def test_mark_get_clear_round_trip(self):
        assert drive_health.get_drive_health("/dev/sr0") is None
        state = drive_health.mark_drive_unresponsive("/dev/sr0", MOUNT_TIMEOUT_MESSAGE)
        assert state is not None
        assert state.code == drive_health.CODE_DRIVE_UNRESPONSIVE
        assert state.message == MOUNT_TIMEOUT_MESSAGE
        assert drive_health.get_drive_health("/dev/sr0") == state
        assert drive_health.clear_drive_health("/dev/sr0") is True
        assert drive_health.get_drive_health("/dev/sr0") is None
        assert drive_health.clear_drive_health("/dev/sr0") is False

    def test_health_is_scoped_per_mount_point(self):
        drive_health.mark_drive_unresponsive("/dev/sr0", MOUNT_TIMEOUT_MESSAGE)
        assert drive_health.get_drive_health("/dev/sr1") is None
        assert [h.mount_point for h in drive_health.snapshot()] == ["/dev/sr0"]

    def test_remark_preserves_first_detection_time(self):
        first = drive_health.mark_drive_unresponsive("/dev/sr0", MOUNT_TIMEOUT_MESSAGE)
        second = drive_health.mark_drive_unresponsive("/dev/sr0", "still dead")
        assert second.detected_at == first.detected_at
        assert second.message == "still dead"


# --------------------------------------------------------------------------
# 2/3. handle_disc_insert fail-closed behaviour
# --------------------------------------------------------------------------

def _run_insert(monkeypatch, *, hash_exc, run_makemkv_spy):
    """Drive ``handle_disc_insert`` with a stubbed hash + info scan."""
    from core import _drive_operations as drv_ops
    from core.disc_slot_state import reset_disc_slot_state_for_tests

    reset_disc_slot_state_for_tests()

    def _hash(*_a, **_kw):
        raise hash_exc

    monkeypatch.setattr(drv_ops, "hash_media_disc", _hash)
    monkeypatch.setattr(drv_ops, "run_makemkv", run_makemkv_spy)

    with patch("core.disc_manager.on_disc_inserted") as inserted, \
         patch("core.disc_manager.on_disc_scan_complete") as scan_complete, \
         patch.object(drv_ops, "clear_keys_by_mount_point") as clear_cache, \
         patch("api.routers.events._notify_drive_unresponsive") as notify:
        result = drv_ops.handle_disc_insert("0", "/dev/sr0")
    return result, {
        "inserted": inserted,
        "scan_complete": scan_complete,
        "clear_cache": clear_cache,
        "notify": notify,
    }


class TestHandleDiscInsertFailsClosed:
    def test_drive_fault_aborts_before_info_scan(self, monkeypatch):
        calls = []

        def _spy(args, *a, **kw):
            calls.append(args)
            return ("", None)

        result, mocks = _run_insert(
            monkeypatch,
            hash_exc=MakeMKVError(MOUNT_TIMEOUT_MESSAGE),
            run_makemkv_spy=_spy,
        )

        # No six-minute makemkvcon run against a drive that will not answer.
        assert calls == []
        assert result["status"] == "error"
        assert result["drive_error"] == MOUNT_TIMEOUT_MESSAGE
        assert result["drive_error_code"] == drive_health.CODE_DRIVE_UNRESPONSIVE
        # The Disc Manager is never told the scan completed, so nothing
        # downstream can serve the previous disc's identity for this slot.
        mocks["scan_complete"].assert_not_called()
        # The cache for this slot is purged (once on entry, once on fault).
        assert mocks["clear_cache"].call_count >= 2
        mocks["notify"].assert_called_once()
        assert mocks["notify"].call_args.args[0] == "/dev/sr0"
        assert mocks["notify"].call_args.args[2] == MOUNT_TIMEOUT_MESSAGE
        assert mocks["notify"].call_args.kwargs["notify"] is True

    def test_repeat_fault_updates_the_card_but_does_not_re_alert(self, monkeypatch):
        """A wedged drive keeps emitting udev events — alert once, not per retry."""
        for _ in range(2):
            _, mocks = _run_insert(
                monkeypatch,
                hash_exc=MakeMKVError(MOUNT_TIMEOUT_MESSAGE),
                run_makemkv_spy=lambda *a, **kw: ("", None),
            )
        # Second pass: still emits the WebSocket card update, no new alert.
        mocks["notify"].assert_called_once()
        assert mocks["notify"].call_args.kwargs["notify"] is False

    def test_drive_fault_records_health(self, monkeypatch):
        _run_insert(
            monkeypatch,
            hash_exc=MakeMKVError(MOUNT_TIMEOUT_MESSAGE),
            run_makemkv_spy=lambda *a, **kw: ("", None),
        )
        state = drive_health.get_drive_health("/dev/sr0")
        assert state is not None
        assert state.message == MOUNT_TIMEOUT_MESSAGE

    def test_drive_fault_does_not_mark_slot_stable(self, monkeypatch):
        from core.disc_slot_state import get_slot_state, should_treat_change_as_weak_insert

        _run_insert(
            monkeypatch,
            hash_exc=MakeMKVError(MOUNT_TIMEOUT_MESSAGE),
            run_makemkv_spy=lambda *a, **kw: ("", None),
        )
        assert get_slot_state("/dev/sr0") == "unknown"
        # A later udev change must be treated as a real insert so the drive
        # gets rescanned once the user power-cycles it.
        assert should_treat_change_as_weak_insert("/dev/sr0") is False

    def test_disc_level_hash_failure_still_scans(self, monkeypatch):
        """No BDMV/VIDEO_TS is a disc problem, not a drive problem."""
        calls = []

        def _spy(args, *a, **kw):
            calls.append(args)
            return ('DRV:0,256,999,0,"BD-ROM","STAR_WARS_REBELS_S3_D1","/dev/sr0"\n', None)

        result, mocks = _run_insert(
            monkeypatch,
            hash_exc=FileNotFoundError("No Blu-ray or DVD structure under '/tmp/x'"),
            run_makemkv_spy=_spy,
        )

        assert len(calls) == 1
        assert "info dev:/dev/sr0" in calls[0]
        assert result["status"] == "ok"
        mocks["scan_complete"].assert_called_once()
        assert drive_health.get_drive_health("/dev/sr0") is None

    def test_successful_hash_clears_a_previous_fault(self, monkeypatch):
        from core import _drive_operations as drv_ops
        from core.disc_slot_state import reset_disc_slot_state_for_tests

        drive_health.mark_drive_unresponsive("/dev/sr0", MOUNT_TIMEOUT_MESSAGE)
        reset_disc_slot_state_for_tests()

        monkeypatch.setattr(drv_ops, "hash_media_disc", lambda *a, **kw: "HASH1")
        monkeypatch.setattr(
            drv_ops,
            "run_makemkv",
            lambda *a, **kw: ('DRV:0,256,999,0,"BD-ROM","THOR","/dev/sr0"\n', None),
        )
        with patch("core.disc_manager.on_disc_inserted"), \
             patch("core.disc_manager.on_disc_scan_complete"), \
             patch.object(drv_ops, "clear_keys_by_mount_point"):
            result = drv_ops.handle_disc_insert("0", "/dev/sr0")

        assert result["status"] == "ok"
        assert drive_health.get_drive_health("/dev/sr0") is None


# --------------------------------------------------------------------------
# 4. Rip gate
# --------------------------------------------------------------------------

@pytest.fixture
def rip_client(test_db, monkeypatch):
    """TestClient with the pre-existing rip-start preconditions bypassed.

    The route blocks on a MakeMKV install check before any drive gate; stub it
    so these tests exercise the drive-health gate itself.
    """
    from api import database
    from api.main import app

    monkeypatch.setattr(
        "core.makemkv_updater.validate_makemkv_installation",
        lambda: {"can_rip": True, "missing_components": [], "error_message": None},
    )
    monkeypatch.setattr(
        "core.disc_scan_dispatch.disc_info_cache_satisfies",
        lambda *a, **k: True,
    )

    def override_get_db():
        session = test_db()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[database.get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestRipRefusesUnhealthyDrive:
    def test_start_rip_returns_409_drive_unresponsive(self, rip_client):
        drive_health.mark_drive_unresponsive("/dev/sr0", MOUNT_TIMEOUT_MESSAGE)
        with patch("core.notifications.emit_notification_sync") as notify:
            resp = rip_client.post(
                "/jobs/rip",
                json={"disc_num": "0", "mount_point": "/dev/sr0", "mode": "copy"},
            )
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == drive_health.CODE_DRIVE_UNRESPONSIVE
        assert detail["mount_point"] == "/dev/sr0"
        assert "power cycling" in detail["error"]
        notify.assert_called_once()
        assert notify.call_args.kwargs["level"] == "error_drive_unresponsive"

    def test_healthy_drive_is_not_blocked_by_the_gate(self, rip_client):
        """Sanity check: the gate is inert when no fault is recorded.

        A healthy drive falls through to the pre-existing gates, so anything
        other than the drive-health 409 means this gate did not fire.
        """
        resp = rip_client.post(
            "/jobs/rip",
            json={"disc_num": "0", "mount_point": "/dev/sr0", "mode": "copy"},
        )
        body = resp.json()
        detail = body.get("detail") if isinstance(body, dict) else None
        code = detail.get("code") if isinstance(detail, dict) else None
        assert code != drive_health.CODE_DRIVE_UNRESPONSIVE

    def test_gate_is_scoped_to_the_target_drive(self, rip_client):
        """A fault on sr1 must not block a rip on sr0."""
        drive_health.mark_drive_unresponsive("/dev/sr1", MOUNT_TIMEOUT_MESSAGE)
        resp = rip_client.post(
            "/jobs/rip",
            json={"disc_num": "0", "mount_point": "/dev/sr0", "mode": "copy"},
        )
        body = resp.json()
        detail = body.get("detail") if isinstance(body, dict) else None
        code = detail.get("code") if isinstance(detail, dict) else None
        assert code != drive_health.CODE_DRIVE_UNRESPONSIVE


# --------------------------------------------------------------------------
# 5. Volume-label fallback
# --------------------------------------------------------------------------

class TestVolumeLabelFallback:
    def test_empty_scan_uses_drv_volume_label_as_info_title(self):
        from parsing.disc_parser import hydrate_disc_payload

        hydrated = hydrate_disc_payload(
            "0",
            "/dev/sr0",
            {
                "disc_hash": "HASH1",
                "makemkv_disc_name": "STAR_WARS_REBELS_S3_D1",
                "raw_info_log": 'DRV:0,256,999,0,"BD-ROM","STAR_WARS_REBELS_S3_D1","/dev/sr0"\n',
            },
        )
        assert hydrated["info_title"] == "Star Wars Rebels S3 D1"

    def test_volume_label_never_overrides_a_real_title(self):
        from parsing.disc_parser import hydrate_disc_payload

        hydrated = hydrate_disc_payload(
            "0",
            "/dev/sr0",
            {
                "disc_hash": "HASH1",
                "info_title": "Thor",
                "makemkv_disc_name": "STAR_WARS_REBELS_S3_D1",
            },
        )
        assert hydrated["info_title"] == "Thor"

    def test_volume_label_alone_does_not_invent_a_release_name(self):
        """Scoped fallback: naming a Release still needs a CINFO title / DiscDB."""
        from parsing.disc_parser import hydrate_disc_payload

        hydrated = hydrate_disc_payload(
            "0",
            "/dev/sr0",
            {"disc_hash": "HASH1", "makemkv_disc_name": "STAR_WARS_REBELS_S3_D1"},
        )
        assert hydrated["info_title"] == "Star Wars Rebels S3 D1"
        assert not hydrated.get("release_name")


# --------------------------------------------------------------------------
# 6. The fault survives a page refresh, and routes to the errors channel
# --------------------------------------------------------------------------

class TestUnhealthyDriveSurvivesReload:
    def test_initial_state_includes_a_failed_card_for_the_faulted_drive(self, test_db):
        """The disc cache is purged on a fault, so without this injection the
        error would vanish from the UI on every page refresh."""
        from api.routers import websockets

        drive_health.mark_drive_unresponsive("/dev/sr0", MOUNT_TIMEOUT_MESSAGE)
        with patch.object(websockets, "get_cached_discs", return_value=[]), \
             patch("api.database.SessionLocal", return_value=test_db()):
            state = websockets._build_initial_coordinator_state_sync()

        cards = [d for d in state["discs"] if d.get("mount_point") == "/dev/sr0"]
        assert len(cards) == 1
        assert cards[0]["scan_state"] == "failed"
        assert cards[0]["scan_error"] == MOUNT_TIMEOUT_MESSAGE
        # No identity is invented for a drive we could not read.
        assert cards[0]["disc_hash"] is None
        assert cards[0]["movie_name"] is None

    def test_no_card_is_injected_for_a_healthy_drive(self, test_db):
        from api.routers import websockets

        with patch.object(websockets, "get_cached_discs", return_value=[]), \
             patch("api.database.SessionLocal", return_value=test_db()):
            state = websockets._build_initial_coordinator_state_sync()
        assert [d for d in state["discs"] if d.get("mount_point") == "/dev/sr0"] == []


class TestNotificationRouting:
    def test_level_routes_to_the_errors_bucket_on_both_channels(self):
        """error_drive_unresponsive must reach the in-app bell and Discord."""
        from core.notification_preferences import (
            ERROR_LEVELS,
            default_notification_preferences,
            level_bucket,
            resolve_delivery_channels,
        )

        assert "error_drive_unresponsive" in ERROR_LEVELS
        bucket, _ = level_bucket("error_drive_unresponsive")
        assert bucket == "error"

        configured = {
            "enabled": True,
            "webhook_url": "https://discord.example/hook",
            "notification_preferences": default_notification_preferences(),
        }
        assert resolve_delivery_channels("error_drive_unresponsive", configured) == (True, True)

        # Discord is opt-in on the webhook being configured; the in-app bell
        # must still fire without it.
        unconfigured = {"notification_preferences": default_notification_preferences()}
        assert resolve_delivery_channels("error_drive_unresponsive", unconfigured) == (True, False)


# --------------------------------------------------------------------------
# 7. scan_state='failed' must not be announced as disc_ready (#723)
# --------------------------------------------------------------------------

class TestFailedScanIsNotAnnouncedReady:
    def test_enrich_stamps_persisted_scan_state(self, test_db):
        """The persist layer's verdict rides back on the payload."""
        from api.routers import events

        session = test_db()
        try:
            payload = {
                "disc_hash": "hash-empty-ws",
                "content_hash": "hash-empty-ws",
                "scan_tracks": [],
                "tracks": {},
            }
            with patch("api.database.SessionLocal", return_value=session), \
                 patch("api.crud._hydrate_payload", side_effect=lambda *a, **kw: dict(payload)):
                enriched = events._enrich_payload_with_disc_record(
                    dict(payload), "0", "/dev/sr0"
                )
            assert enriched["scan_state"] == "failed"
            assert "Empty scan output" in (enriched["scan_error"] or "")
        finally:
            session.close()

    @pytest.mark.asyncio
    async def test_failed_scan_emits_disc_scan_failed_not_disc_ready(self, monkeypatch):
        """The WebSocket must not say 'ready' about a scan the DB calls failed."""
        from api.routers import events

        emitted: list[tuple[str, dict]] = []

        async def _fake_emit(event_type, payload):
            emitted.append((event_type, payload))

        async def _fake_disc_updated(*_a, **_kw):
            emitted.append(("disc_updated", {}))

        monkeypatch.setattr(
            "api.routers.websockets._emit_to_coordinator", _fake_emit
        )
        monkeypatch.setattr(
            "api.routers.websockets._emit_disc_updated_from_info", _fake_disc_updated
        )
        monkeypatch.setattr(
            events,
            "_hydrate_and_enrich",
            lambda payload, disc_num, mount_point: {
                **payload,
                "disc_id": "disc-1",
                "scan_state": "failed",
                "scan_error": "Empty scan output — no format and no tracks enumerated.",
            },
        )

        auto_rip_calls = []
        monkeypatch.setattr(
            "core.auto_rip.maybe_auto_start_rip",
            lambda *a, **kw: auto_rip_calls.append(a),
        )

        # No disc_hash → the DB persist branch is skipped entirely.
        await events._notify_disc_scan_complete_async(
            {"disc_num": "0", "mount_point": "/dev/sr0"}
        )
        # create_task() work needs one loop turn to run.
        import asyncio as _asyncio
        await _asyncio.sleep(0)

        event_types = [e for e, _ in emitted]
        assert "disc_scan_failed" in event_types
        assert "disc_ready" not in event_types
        assert auto_rip_calls == []
