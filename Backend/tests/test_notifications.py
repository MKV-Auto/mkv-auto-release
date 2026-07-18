"""Unit tests for core.notifications and job_state notification wiring."""
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.discord_notification_defaults import DEFAULT_DISCORD_NOTIFICATION_LEVELS
from core.notifications import (
    NOTIFICATION_LEVELS,
    DEFAULT_DISCORD_LEVELS,
    emit_notification_sync,
)
from core.pipeline_notification_labels import job_audience_label


def test_job_audience_label_movie_release_and_disc_num():
    movie = SimpleNamespace(name="The Film")
    rel = SimpleNamespace(name="Release Name", movie=movie)
    disc = SimpleNamespace(release=rel, info_title="VOL1", disc_number=2)
    job = SimpleNamespace(disc=disc, disc_num="0")
    assert job_audience_label(job, disc) == "The Film Disc #2"


def test_job_audience_label_falls_back_to_job_disc_num_when_disc_number_unset():
    movie = SimpleNamespace(name="The Film")
    rel = SimpleNamespace(name="Release Name", movie=movie)
    disc = SimpleNamespace(release=rel, info_title="VOL1", disc_number=None)
    job = SimpleNamespace(disc=disc, disc_num="3")
    assert job_audience_label(job, disc) == "The Film Disc #3"


def test_job_audience_label_falls_back_to_info_title():
    disc = SimpleNamespace(release=None, info_title="MakeMKV title")
    job = SimpleNamespace(disc=disc, disc_num=None)
    assert job_audience_label(job, disc) == "MakeMKV title"


def test_notification_levels_non_empty():
    assert len(NOTIFICATION_LEVELS) >= 1
    assert "rip_complete" in NOTIFICATION_LEVELS
    assert "job_completed" in NOTIFICATION_LEVELS
    assert "error_disk_space" in NOTIFICATION_LEVELS
    assert "scan_completed" in NOTIFICATION_LEVELS


def test_default_discord_levels_subset_of_all():
    for level in DEFAULT_DISCORD_LEVELS:
        assert level in NOTIFICATION_LEVELS


def test_default_discord_levels_includes_awaiting_labeling():
    assert "awaiting_labeling" in DEFAULT_DISCORD_LEVELS


def test_default_discord_levels_includes_postprocess_and_label_milestones():
    assert "postprocess_complete" in DEFAULT_DISCORD_LEVELS
    assert "label_complete" in DEFAULT_DISCORD_LEVELS
    assert "job_completed" in DEFAULT_DISCORD_LEVELS
    assert "scan_completed" in DEFAULT_DISCORD_LEVELS


def test_default_discord_levels_matches_shared_module():
    assert DEFAULT_DISCORD_LEVELS == list(DEFAULT_DISCORD_NOTIFICATION_LEVELS)


def test_get_discord_dict_returns_notification_preferences_when_legacy_missing():
    """GET /system/discord/config: migrate to notification_preferences when old list absent."""
    from core import settings

    with patch.object(
        settings,
        "load_settings",
        return_value={"discord": {"webhook_url": None, "enabled": False}},
    ):
        with patch.object(settings, "save_settings"):
            d = settings.get_discord_dict()
    prefs = d["notification_preferences"]
    assert prefs["informative"]["enabled"] is False
    assert "rip_complete" in prefs["informative"]["categories"]
    assert prefs["action_required"]["in_app"] is True


def test_get_discord_dict_migrates_legacy_notification_levels():
    """Legacy notification_levels list becomes structured notification_preferences."""
    from core import settings

    legacy = ["rip_complete", "awaiting_labeling", "rip_failed"]
    with patch.object(
        settings,
        "load_settings",
        return_value={
            "discord": {
                "webhook_url": None,
                "enabled": False,
                "notification_levels": legacy,
            }
        },
    ):
        with patch.object(settings, "save_settings") as save:
            d = settings.get_discord_dict()
    save.assert_called()
    prefs = d["notification_preferences"]
    assert prefs["informative"]["enabled"] is True
    assert prefs["informative"]["categories"]["rip_complete"]["discord"] is True
    assert prefs["action_required"]["discord"] is True
    assert prefs["errors"]["discord"] is True


def test_legacy_migrate_action_discord_true_when_errors_but_no_action_levels():
    """Errors on legacy Discord list enable the action-required Discord bucket (e.g. scan_completed)."""
    from core import settings

    legacy = ["rip_complete", "rip_failed"]
    with patch.object(
        settings,
        "load_settings",
        return_value={
            "discord": {
                "webhook_url": None,
                "enabled": False,
                "notification_levels": legacy,
            }
        },
    ):
        with patch.object(settings, "save_settings"):
            d = settings.get_discord_dict()
    prefs = d["notification_preferences"]
    assert prefs["action_required"]["discord"] is True


def test_legacy_migrate_informative_only_leaves_action_discord_false():
    """Legacy list with only informative levels does not enable action-required Discord."""
    from core import settings

    legacy = ["rip_complete", "job_completed"]
    with patch.object(
        settings,
        "load_settings",
        return_value={
            "discord": {
                "webhook_url": None,
                "enabled": False,
                "notification_levels": legacy,
            }
        },
    ):
        with patch.object(settings, "save_settings"):
            d = settings.get_discord_dict()
    prefs = d["notification_preferences"]
    assert prefs["action_required"]["discord"] is False


@pytest.mark.asyncio
async def test_emit_informative_master_off_skips_ws_and_discord_and_dedupe():
    from core.notifications import emit_notification
    from core.notification_preferences import default_notification_preferences

    prefs = default_notification_preferences()
    prefs["informative"]["enabled"] = False
    cfg = {"enabled": True, "webhook_url": "https://example.com/hook", "notification_preferences": prefs}
    with patch("core.discord_config.load_discord_config", return_value=cfg):
        with patch("core.notifications._dedupe_should_send", new_callable=AsyncMock) as dedupe:
            with patch("api.routers.websockets._emit_unified", new_callable=AsyncMock) as ws:
                with patch("core.notifications._send_to_discord") as dc:
                    await emit_notification("m", "info", "rip_complete", job_id="j1")
    dedupe.assert_not_called()
    ws.assert_not_called()
    dc.assert_not_called()


@pytest.mark.asyncio
async def test_emit_informative_rip_complete_respects_per_category_discord_off():
    from core.notifications import emit_notification
    from core.notification_preferences import default_notification_preferences

    prefs = default_notification_preferences()
    prefs["informative"]["enabled"] = True
    prefs["informative"]["categories"]["rip_complete"]["in_app"] = True
    prefs["informative"]["categories"]["rip_complete"]["discord"] = False
    cfg = {"enabled": True, "webhook_url": "https://example.com/hook", "notification_preferences": prefs}
    with patch("core.discord_config.load_discord_config", return_value=cfg):
        with patch("core.notifications._dedupe_should_send", AsyncMock(return_value=True)):
            with patch("api.routers.websockets._emit_unified", new_callable=AsyncMock) as ws:
                with patch("core.notifications._send_to_discord") as dc:
                    await emit_notification("m", "info", "rip_complete", job_id="j1")
    ws.assert_called_once()
    dc.assert_not_called()


@pytest.mark.asyncio
async def test_emit_action_required_respects_type_level_discord_off():
    from core.notifications import emit_notification
    from core.notification_preferences import default_notification_preferences

    prefs = default_notification_preferences()
    prefs["informative"]["enabled"] = True
    prefs["action_required"]["in_app"] = True
    prefs["action_required"]["discord"] = False
    cfg = {"enabled": True, "webhook_url": "https://example.com/hook", "notification_preferences": prefs}
    with patch("core.discord_config.load_discord_config", return_value=cfg):
        with patch("core.notifications._dedupe_should_send", AsyncMock(return_value=True)):
            with patch("api.routers.websockets._emit_unified", new_callable=AsyncMock) as ws:
                with patch("core.notifications._send_to_discord") as dc:
                    await emit_notification("m", "success", "awaiting_labeling", job_id="j1")
    ws.assert_called_once()
    dc.assert_not_called()


def test_emit_notification_sync_does_not_raise():
    """emit_notification_sync may not send (no loop) but must not raise."""
    emit_notification_sync("Test message", "info", "error_generic")


def test_job_failure_emits_notification_sync_without_running_loop():
    """Terminal failed state must call emit_notification_sync even with no asyncio loop."""
    from core.job_state import _emit_job_state_websocket_updates

    job = SimpleNamespace(
        id="test-job-123",
        job_status="failed",
        rip_state="failed",
        error_reason="Test failure: makemkvcon not found",
        disc=None,
    )
    normalized = {
        "job_status": "failed",
        "rip_state": "failed",
        "error_reason": "Test failure: makemkvcon not found",
    }

    with patch("core.notifications.emit_notification_sync") as mock_emit:
        with patch("api.main._app_instance", None):
            _emit_job_state_websocket_updates(job, normalized, skip_context_changed=True)

    assert mock_emit.call_count == 1
    pos = mock_emit.call_args[0]
    assert pos[2] == "rip_failed"


def test_terminal_notification_from_background_thread_no_loop():
    """Stale cleanup / executor: no running loop still delivers terminal notification."""
    from core.job_state import _emit_job_state_websocket_updates

    job = SimpleNamespace(
        id="thread-job",
        job_status="failed",
        rip_state="failed",
        error_reason="stale",
        disc=None,
    )
    normalized = {"job_status": "failed", "rip_state": "failed", "error_reason": "stale"}
    results: list[int] = []

    def run():
        with patch("core.notifications.emit_notification_sync") as mock_emit:
            with patch("api.main._app_instance", None):
                _emit_job_state_websocket_updates(job, normalized, skip_context_changed=True)
            results.append(mock_emit.call_count)

    t = threading.Thread(target=run)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()
    assert results == [1]


def test_rip_complete_miss_emits_awaiting_labeling_notification():
    """Rip finished (e.g. after verification) with label phase uses awaiting_labeling (separate Discord dedupe from rip_complete)."""
    from core.job_state import _emit_job_state_websocket_updates

    disc = SimpleNamespace(info_title="Disc vol", release=None, disc_number=2)
    job = SimpleNamespace(
        id="job-label-1",
        job_status="running",
        disc_id="d1",
        disc_num="9",
        rip_state="completed",
        phase="label",
        disc=disc,
    )
    normalized = {"rip_state": "completed"}

    with patch("core.notifications.emit_notification_sync") as mock_emit:
        with patch("api.main._app_instance", None):
            with patch("api.routers.discs.invalidate_workflow_context_cache", lambda **_: None):
                _emit_job_state_websocket_updates(job, normalized, skip_context_changed=True)

    labeling_calls = [
        c for c in mock_emit.call_args_list if len(c[0]) > 2 and c[0][2] == "awaiting_labeling"
    ]
    assert len(labeling_calls) == 1
    args, kwargs = labeling_calls[0]
    assert "Copy of" in args[0]
    assert "Disc #2" in args[0]
    assert "Labeling awaiting" in args[0]
    assert kwargs.get("id_key") == "labeling"


def test_rip_complete_hit_prefers_normalized_phase_over_stale_job_attrs():
    """StageState.rip_complete passes phase/post_state in the same payload as rip_state; stale ORM fields must not skip the toast."""
    from core.job_state import _emit_job_state_websocket_updates

    disc = SimpleNamespace(info_title="Hit disc", release=None)
    job = SimpleNamespace(
        id="job-hit-verify",
        job_status="running",
        rip_state="completed",
        phase="rip",
        disc=disc,
    )
    normalized = {
        "rip_state": "completed",
        "phase": "postprocess",
        "post_state": "ready",
    }

    with patch("core.notifications.emit_notification_sync") as mock_emit:
        with patch("api.main._app_instance", None):
            with patch("api.routers.discs.invalidate_workflow_context_cache", lambda **_: None):
                _emit_job_state_websocket_updates(job, normalized, skip_context_changed=True)

    rip_calls = [c for c in mock_emit.call_args_list if len(c[0]) > 2 and c[0][2] == "rip_complete"]
    assert len(rip_calls) == 1
    assert "Hit disc" in rip_calls[0][0][0]
    assert "rip complete" in rip_calls[0][0][0].lower()
    assert "post-processing" in rip_calls[0][0][0].lower()


def test_rip_complete_milestone_fallback_emits_generic_when_branch_unmatched():
    """If rip_state completes but phase/post_state are unexpected, still emit a generic rip_complete toast."""
    from core.job_state import _emit_job_state_websocket_updates

    disc = SimpleNamespace(info_title="Odd", release=None)
    job = SimpleNamespace(
        id="job-fallback",
        job_status="running",
        rip_state="completed",
        phase="transfer",
        disc=disc,
    )
    normalized = {
        "rip_state": "completed",
        "phase": "transfer",
        "post_state": "completed",
    }

    with patch("core.notifications.emit_notification_sync") as mock_emit:
        with patch("api.main._app_instance", None):
            with patch("api.routers.discs.invalidate_workflow_context_cache", lambda **_: None):
                _emit_job_state_websocket_updates(job, normalized, skip_context_changed=True)

    rip_calls = [c for c in mock_emit.call_args_list if len(c[0]) > 2 and c[0][2] == "rip_complete"]
    assert len(rip_calls) == 1
    assert rip_calls[0][0][0] == "Rip complete: Odd."


def test_terminal_job_complete_emits_job_completed_level():
    """Terminal 'Job complete' toast uses job_completed so dedupe does not collide with rip milestone rip_complete."""
    from core.job_state import _emit_job_state_websocket_updates

    job = SimpleNamespace(
        id="job-term-jc",
        job_status="completed",
        rip_state="completed",
        phase="postprocess",
        transfer_state="ready",
        disc_id=None,
        disc=SimpleNamespace(info_title="DoneTitle"),
        created_at=None,
    )
    normalized = {"job_status": "completed"}

    with patch("core.notifications.emit_notification_sync") as mock_emit:
        with patch("api.main._app_instance", None):
            with patch("api.routers.discs.invalidate_workflow_context_cache", lambda **_: None):
                _emit_job_state_websocket_updates(job, normalized, skip_context_changed=True)

    jc = [c for c in mock_emit.call_args_list if len(c[0]) > 2 and c[0][2] == "job_completed"]
    assert len(jc) == 1
    assert jc[0][0][0] == "Job complete: DoneTitle"
    rip = [c for c in mock_emit.call_args_list if len(c[0]) > 2 and c[0][2] == "rip_complete"]
    assert len(rip) == 0


def test_transfer_completed_while_job_still_running_emits_once():
    """Miss-style path: transfer_state completes while job_status stays running."""
    from core.job_state import _emit_job_state_websocket_updates

    job = SimpleNamespace(
        id="job-transfer-running",
        job_status="running",
        transfer_state="completed",
        phase="complete",
        disc_id=None,
        disc=None,
        created_at=None,
    )
    normalized = {"transfer_state": "completed"}

    with patch("core.notifications.emit_notification_sync") as mock_emit:
        with patch("api.routers.discs.invalidate_workflow_context_cache", lambda **_: None):
            _emit_job_state_websocket_updates(job, normalized, skip_context_changed=True)

    tc = [c for c in mock_emit.call_args_list if len(c[0]) > 2 and c[0][2] == "transfer_completed"]
    assert len(tc) == 1
    assert tc[0][0][0] == "Transfer complete: this disc"


def test_transfer_completed_same_update_as_job_completed_single_emit():
    """Hit path: one apply_job_state sets transfer + job completed — no duplicate transfer_completed."""
    from core.job_state import _emit_job_state_websocket_updates

    job = SimpleNamespace(
        id="job-hit-done",
        job_status="completed",
        transfer_state="completed",
        phase="complete",
        disc_id=None,
        disc=SimpleNamespace(info_title="My Movie"),
        created_at=None,
    )
    normalized = {"transfer_state": "completed", "job_status": "completed"}

    with patch("core.notifications.emit_notification_sync") as mock_emit:
        with patch("api.main._app_instance", None):
            with patch("api.routers.discs.invalidate_workflow_context_cache", lambda **_: None):
                _emit_job_state_websocket_updates(job, normalized, skip_context_changed=True)

    tc = [c for c in mock_emit.call_args_list if len(c[0]) > 2 and c[0][2] == "transfer_completed"]
    assert len(tc) == 1
    assert "Transfer complete: My Movie" in tc[0][0][0]
    rip = [c for c in mock_emit.call_args_list if len(c[0]) > 2 and c[0][2] == "rip_complete"]
    assert len(rip) == 0


def test_job_completed_terminal_fallback_transfer_completed_when_transfer_not_in_normalized():
    """Late job_status=completed update: job already transfer-done; emit transfer_completed once."""
    from core.job_state import _emit_job_state_websocket_updates

    job = SimpleNamespace(
        id="job-late-terminal",
        job_status="completed",
        transfer_state="completed",
        phase="complete",
        disc_id=None,
        disc=SimpleNamespace(info_title="Late"),
        created_at=None,
    )
    normalized = {"job_status": "completed"}

    with patch("core.notifications.emit_notification_sync") as mock_emit:
        with patch("api.main._app_instance", None):
            with patch("api.routers.discs.invalidate_workflow_context_cache", lambda **_: None):
                _emit_job_state_websocket_updates(job, normalized, skip_context_changed=True)

    tc = [c for c in mock_emit.call_args_list if len(c[0]) > 2 and c[0][2] == "transfer_completed"]
    assert len(tc) == 1
    assert "Transfer complete: Late" in tc[0][0][0]
    rip = [c for c in mock_emit.call_args_list if len(c[0]) > 2 and c[0][2] == "rip_complete"]
    assert len(rip) == 0


# transfer_failed notification gating: must fire whenever the job is alive (running OR validating).
# Regression for the V for Vendetta case where transfer failed during job_status=validating
# (post-validate window) and no Discord/toast notification ever went out.

def _make_failed_transfer_job(*, job_status: str, transfer_error: str, info_title: str = "My Movie"):
    return SimpleNamespace(
        id="job-transfer-fail",
        job_status=job_status,
        transfer_state="failed",
        transfer_error=transfer_error,
        rip_state="completed",
        phase="transfer",
        disc_id=None,
        disc=SimpleNamespace(info_title=info_title, release=None),
        created_at=None,
        error_reason=None,
    )


def test_transfer_failed_emits_notification_while_job_status_running():
    """Baseline: when job_status=running, transfer_state=failed must emit transfer_failed."""
    from core.job_state import _emit_job_state_websocket_updates

    job = _make_failed_transfer_job(
        job_status="running", transfer_error="NT_STATUS_ACCESS_DENIED creating Movies/X (2024)"
    )
    normalized = {"transfer_state": "failed"}

    with patch("core.notifications.emit_notification_sync") as mock_emit:
        with patch("api.routers.discs.invalidate_workflow_context_cache", lambda **_: None):
            _emit_job_state_websocket_updates(job, normalized, skip_context_changed=True)

    tf = [c for c in mock_emit.call_args_list if len(c[0]) > 2 and c[0][2] == "transfer_failed"]
    assert len(tf) == 1
    assert "Transfer failed" in tf[0][0][0]
    assert "NT_STATUS_ACCESS_DENIED" in tf[0][0][0]


def test_transfer_failed_emits_notification_while_job_status_validating():
    """Regression: V for Vendetta failure sat at job_status=validating; the prior gate skipped
    notification. This must now emit because the job is still alive."""
    from core.job_state import _emit_job_state_websocket_updates

    job = _make_failed_transfer_job(
        job_status="validating",
        transfer_error="NT_STATUS_OBJECT_PATH_NOT_FOUND opening remote file ...",
    )
    normalized = {"transfer_state": "failed"}

    with patch("core.notifications.emit_notification_sync") as mock_emit:
        with patch("api.routers.discs.invalidate_workflow_context_cache", lambda **_: None):
            _emit_job_state_websocket_updates(job, normalized, skip_context_changed=True)

    tf = [c for c in mock_emit.call_args_list if len(c[0]) > 2 and c[0][2] == "transfer_failed"]
    assert len(tf) == 1, "transfer_failed notification must fire when job_status=validating"
    # Notification kind is "error" and includes a retry action so the user can retry from Discord/toast.
    assert tf[0][0][1] == "error"
    kwargs = tf[0][1]
    assert kwargs.get("action_type") == "retry_transfer"
    assert kwargs.get("action_payload", {}).get("job_id") == job.id


def test_transfer_failed_does_not_emit_when_job_already_failed():
    """If the job is already terminal (job_status=failed), the transfer_failed notification path
    must skip — otherwise we'd double-notify on cleanup transitions."""
    from core.job_state import _emit_job_state_websocket_updates

    job = _make_failed_transfer_job(job_status="failed", transfer_error="anything")
    normalized = {"transfer_state": "failed"}

    with patch("core.notifications.emit_notification_sync") as mock_emit:
        with patch("api.routers.discs.invalidate_workflow_context_cache", lambda **_: None):
            _emit_job_state_websocket_updates(job, normalized, skip_context_changed=True)

    tf = [c for c in mock_emit.call_args_list if len(c[0]) > 2 and c[0][2] == "transfer_failed"]
    assert len(tf) == 0


def test_transfer_failed_does_not_emit_when_failed_and_superseded():
    """Superseded job (job_status=failed + superseded reason) must not emit transfer_failed."""
    from core.job_state import _emit_job_state_websocket_updates

    job = _make_failed_transfer_job(job_status="failed", transfer_error="anything")
    job.error_reason = "superseded by new rip; starting new rip"
    normalized = {"transfer_state": "failed"}

    with patch("core.notifications.emit_notification_sync") as mock_emit:
        with patch("api.routers.discs.invalidate_workflow_context_cache", lambda **_: None):
            _emit_job_state_websocket_updates(job, normalized, skip_context_changed=True)

    tf = [c for c in mock_emit.call_args_list if len(c[0]) > 2 and c[0][2] == "transfer_failed"]
    assert len(tf) == 0
