"""#839 / #841 — backend-derived card state, its event, and deep links."""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.card_state import build_job_card_state_payload, derive_card_state


def _job(**kw):
    base = dict(
        id="894534c4-0000-0000-0000-000000000000", disc_id="disc-1",
        job_status="running", rip_state=None, label_state=None,
        derived_post_state=None, transfer_state=None, transfer_phase=None,
        rip_phase=None, stage_profile="miss",
        rip_progress=None, post_progress=None, transfer_progress=None,
        failure_kind=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_failure_kind_gates_the_retry_pill():
    """#853: 'Retry' only when retry can succeed. precondition names the
    real remedy; config points at settings; transient/unknown keep retry."""
    d = derive_card_state(_job(job_status="failed", derived_post_state="failed",
                               failure_kind="precondition"))
    assert (d["card_state"], d["pill"]) == ("failed_post", "Re-rip needed")

    d = derive_card_state(_job(job_status="failed", transfer_state="failed",
                               failure_kind="config"))
    assert (d["card_state"], d["pill"]) == ("failed_transfer", "Fix settings")

    d = derive_card_state(_job(job_status="failed", derived_post_state="failed",
                               failure_kind="transient"))
    assert (d["card_state"], d["pill"]) == ("failed_post", "Retry processing")

    # Unknown/legacy NULL keeps today's behavior exactly.
    d = derive_card_state(_job(job_status="failed", derived_post_state="failed"))
    assert (d["card_state"], d["pill"]) == ("failed_post", "Retry processing")


CASES = [
    (dict(rip_state="pending"), ("queued", "working", "Waiting for drive")),
    (dict(rip_state="running", rip_phase="copy", rip_progress=42), ("copying", "working", "Copying")),
    (dict(rip_state="running", rip_phase="verification"), ("copying", "working", "Checking copy")),
    (dict(rip_state="completed"), ("awaiting_label", "your_turn", "Label titles")),
    (dict(rip_state="completed", stage_profile="hit"), ("ready_to_process", "your_turn", "Start processing")),
    (dict(rip_state="completed", label_state="completed"), ("ready_to_process", "your_turn", "Start processing")),
    (dict(rip_state="completed", label_state="completed", derived_post_state="running", post_progress=60),
     ("postprocessing", "working", "Post-processing")),
    (dict(rip_state="completed", label_state="completed", transfer_state="ready"),
     ("ready_to_transfer", "your_turn", "Start transfer")),
    (dict(transfer_state="running", transfer_phase="transferring", transfer_progress=63),
     ("transferring", "working", "Transferring")),
    (dict(transfer_state="running", transfer_phase="verifying"), ("verifying", "working", "Verifying")),
    (dict(job_status="validating", transfer_state="running"), ("verifying", "working", "Verifying")),
    (dict(transfer_state="completed", job_status="validating"), ("ready_to_finish", "your_turn", "Finish")),
    (dict(transfer_state="completed"), ("ready_to_finish", "your_turn", "Finish")),
    (dict(job_status="completed", transfer_state="completed"), ("completed", "done", "In library")),
    (dict(job_status="failed", rip_state="failed"), ("failed_copy", "fix", "Retry copy")),
    (dict(job_status="failed", derived_post_state="failed"), ("failed_post", "fix", "Retry processing")),
    (dict(job_status="failed", transfer_state="failed", transfer_progress=71),
     ("failed_transfer", "fix", "Retry transfer")),
    (dict(job_status="failed"), ("failed", "fix", "See error")),
]


@pytest.mark.parametrize("fields,expected", CASES, ids=[e[0] + "-" + str(i) for i, (_, e) in enumerate(CASES)])
def test_derivation_matrix(fields, expected):
    d = derive_card_state(_job(**fields))
    assert (d["card_state"], d["family"], d["pill"]) == expected
    assert d["path"].endswith("?jobId=894534c4-0000-0000-0000-000000000000")


def test_progress_snapshot_rides_along():
    assert derive_card_state(_job(rip_state="running", rip_progress=42))["progress"] == 42
    assert derive_card_state(_job(transfer_state="completed"))["progress"] == 100
    assert derive_card_state(_job(rip_state="pending"))["progress"] is None


def test_needs_destination_only_when_lookup_says_no():
    j = _job(rip_state="completed", label_state="completed", transfer_state="ready")
    assert derive_card_state(j, transfer_destination=False)["card_state"] == "needs_destination"
    assert derive_card_state(j, transfer_destination=True)["card_state"] == "ready_to_transfer"
    # Lookup failure (None) must not nag the user.
    assert derive_card_state(j, transfer_destination=None)["card_state"] == "ready_to_transfer"


def test_payload_carries_ids():
    p = build_job_card_state_payload(_job(rip_state="running"))
    assert p["job_id"].startswith("894534c4") and p["disc_id"] == "disc-1"
    assert p["card_state"] == "copying"


def test_apply_job_state_emits_on_transition_only(test_db):
    from api import models
    from core.job_state import apply_job_state

    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"cs-{uuid.uuid4().hex[:8]}")
        session.add(disc)
        session.flush()
        job = models.Job(id=str(uuid.uuid4()), disc_id=disc.id, disc_num="0", mount_point="/dev/sr0", job_status="pending")
        session.add(job)
        session.commit()
        with patch("core.card_state.schedule_job_card_state_event") as sched:
            apply_job_state(session, job, updates={"job_status": "running", "rip_state": "running"},
                            reason="test start")
            assert sched.call_count == 1
        with patch("core.card_state.schedule_job_card_state_event") as sched:
            # No state-field change → no event.
            apply_job_state(session, job, updates={"rip_progress": 50}, reason="progress only")
            assert sched.call_count == 0
    finally:
        session.close()


def test_job_status_payload_carries_card_contract(test_db):
    from api import models
    from api.routers.jobs import _build_job_status

    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"cs2-{uuid.uuid4().hex[:8]}")
        session.add(disc)
        session.flush()
        job = models.Job(id=str(uuid.uuid4()), disc_id=disc.id, disc_num="0", mount_point="/dev/sr0", job_status="running", rip_state="running", rip_progress=42)
        session.add(job)
        session.commit()
        status = _build_job_status(job)
        assert status.card_state == "copying"
        assert status.card_family == "working"
        assert status.card_pill == "Copying"
    finally:
        session.close()


# ---- #841: base URL + Discord deep links --------------------------------

def test_normalize_base_url():
    from core.settings import normalize_base_url
    assert normalize_base_url("http://192.0.2.10:8080/") == "http://192.0.2.10:8080"
    assert normalize_base_url("https://mkv.example.com") == "https://mkv.example.com"
    assert normalize_base_url("https://host/mkv/") == "https://host/mkv"
    assert normalize_base_url("") is None and normalize_base_url(None) is None
    for bad in ("mkv.example.com", "ftp://x", "http://", "http:// spaced.com"):
        with pytest.raises(ValueError):
            normalize_base_url(bad)


def test_discord_send_appends_link_only_with_base_and_job(monkeypatch, tmp_path):
    import core.notifications as notif

    sent = []
    import core.utils as cu
    monkeypatch.setattr(cu, "notify_discord", lambda url, msg, **kw: sent.append(msg))
    import core.discord_config as dcfg
    monkeypatch.setattr(dcfg, "get_webhook_url", lambda: "https://discord.invalid/hook")

    import core.settings as st
    monkeypatch.setattr(st, "get_base_url", lambda: "http://192.0.2.10:8080")
    notif._send_to_discord("Transferred and verified: Rebels.", "success", link_path="/activity?jobId=job-1")
    assert sent[-1].endswith("Open: http://192.0.2.10:8080/activity?jobId=job-1")

    notif._send_to_discord("No link attached.", "info")
    assert "Open:" not in sent[-1]

    monkeypatch.setattr(st, "get_base_url", lambda: None)
    notif._send_to_discord("Base unset.", "info", link_path="/activity?jobId=job-1")
    assert "Open:" not in sent[-1]


def test_link_path_defaults_from_job_id_but_explicit_wins(monkeypatch):
    """scan_completed regression (#841): its `job_id` is really a DISC id
    (dedupe identity), and the first shipped link sent users to a job route
    that 404s — "Failed to load workflow" on every scan notification. The
    default derivation only applies when the caller did not say otherwise."""
    import asyncio
    import core.notifications as notif

    seen = {}
    monkeypatch.setattr(notif, "_send_to_discord",
                        lambda message, kind, link_path=None: seen.setdefault("link", link_path))
    async def fake_emit(payload): seen["payload"] = payload
    import api.routers.websockets as ws
    monkeypatch.setattr(ws, "_emit_unified", fake_emit)
    import core.discord_config as dcfg
    monkeypatch.setattr(dcfg, "load_discord_config",
                        lambda: {"enabled": True, "webhook_url": "https://d.invalid/h",
                                 "notification_preferences": {"action_required": {"in_app": True, "discord": True},
                                                              "errors": {"in_app": True, "discord": True}}})
    async def run():
        # Real job → derived link.
        await notif.emit_notification("m", "success", "transfer_completed", job_id="job-9",
                                      dedupe_ttl=0)
        assert seen.pop("link") == "/activity?jobId=job-9"
        assert seen.pop("payload")["link_path"] == "/activity?jobId=job-9"
        # Disc id masquerading as job_id, caller says /activity → explicit wins.
        await notif.emit_notification("m2", "info", "scan_completed", job_id="disc-1",
                                      link_path="/activity", dedupe_ttl=0)
        assert seen.pop("link") == "/activity"
        assert seen.pop("payload")["link_path"] == "/activity"
    asyncio.run(run())


def test_initial_state_unfinished_card_carries_contract(test_db):
    """The coordinator initial state is the card strip's first paint — it
    must carry the card contract (regression: the unfinished branch called
    _build_disc_metadata without db, silently skipping derivation)."""
    from api import models
    from api.routers.websockets import _build_disc_metadata

    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"cs3-{uuid.uuid4().hex[:8]}")
        session.add(disc)
        session.flush()
        job = models.Job(id=str(uuid.uuid4()), disc_id=disc.id, disc_num="0", mount_point="/dev/sr0",
                         job_status="running", rip_state="completed", stage_profile="miss")
        session.add(job)
        session.commit()
        meta = _build_disc_metadata(disc, disc_state="unfinished", job_id=str(job.id),
                                    created_at=job.created_at, job_status=job.job_status, db=session)
        assert meta.card_state == "awaiting_label"
        assert meta.card_family == "your_turn"
        assert meta.card_pill == "Label titles"
    finally:
        session.close()


def test_worker_process_falls_back_to_redis_bridge(monkeypatch):
    """In a celery worker there is no uvicorn loop; the event must ride the
    same Redis pub/sub bridge the progress emitter uses (caught live on the
    dev rig: worker-side postprocess failures never reached the card)."""
    import core.card_state as cs

    published = []

    class FakeRedis:
        def publish(self, channel, message):
            published.append((channel, message))

    import redis as redis_mod
    monkeypatch.setattr(redis_mod.Redis, "from_url", classmethod(lambda cls, url, **kw: FakeRedis()))
    # Simulate worker: no running loop and no app instance.
    import api.main as api_main
    monkeypatch.setattr(api_main, "_app_instance", None)

    cs.schedule_job_card_state_event(_job(rip_state="running", rip_progress=42))
    assert len(published) == 1
    channel, message = published[0]
    assert channel == cs.COORDINATOR_EVENTS_CHANNEL
    import json as _json
    body = _json.loads(message)
    assert body["type"] == "job_card_state" and body["card_state"] == "copying"
