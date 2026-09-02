"""#856 — the scan_completed notification fires at the end of the FULL title
scan, not the identification phase (which finishes minutes earlier and told
users "start copying" while the UI honestly still showed scanning)."""
import asyncio
import inspect

from api.routers import events


def test_identification_phase_no_longer_notifies():
    src = inspect.getsource(events._notify_disc_scan_complete_async)
    assert "emit_notification(" not in src, (
        "_notify_disc_scan_complete_async (identification phase) must not emit "
        "scan_completed — that belongs to the full-scan path (#856)"
    )


def test_full_scan_success_path_notifies():
    src = inspect.getsource(events._load_disc_info_async)
    assert "_emit_scan_completed_notification" in src


def test_notification_payload_shape(monkeypatch):
    sent = {}

    async def fake_emit(body, kind, ntype, **kw):
        sent.update({"body": body, "kind": kind, "type": ntype, **kw})

    import core.notifications as notif
    monkeypatch.setattr(notif, "emit_notification", fake_emit)
    monkeypatch.setattr("core.job_state._public_app_base_url", lambda: None)

    enriched = {"movie_name": "Star Wars: The Clone Wars", "disc_number": 5}
    asyncio.run(events._emit_scan_completed_notification(enriched, "disc-1", "/dev/sr0", "HASH1"))

    assert sent["type"] == "scan_completed"
    assert sent["job_id"] == "disc-1"
    assert sent["link_path"] == "/activity"
    assert sent["id_key"] == "HASH1"
    assert "Star Wars: The Clone Wars Disc #5" in sent["body"]
