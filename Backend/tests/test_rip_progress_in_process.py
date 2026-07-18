"""
Coverage for the in-process rip-progress callback (#365 cleanup).

Mirrors the transfer-progress conversion (PR #430) — high-frequency
callback (one per rip progress tick × thousands per rip) converted from
HTTP POST to in-process DB write.

The worker has had a client-side throttle since long before the
collapse work; this conversion drops the duplicate server-side
rate-limit that lived in the API endpoint. The worker's throttle is
the stricter of the two so production behaviour is identical.

See ``docs/ADR-001-postprocess-collapse.md``.
"""
import uuid

import pytest

from api import crud, models
from workers.tasks import (
    _post_rip_progress,
    _rip_progress_last_send,
    _rip_progress_last_pct,
    RIP_PROGRESS_THROTTLE_SECONDS,
    RIP_PROGRESS_THROTTLE_PCT,
)


@pytest.fixture(autouse=True)
def _reset_throttle_state():
    """Clear the throttle dicts so cross-test state doesn't pollute."""
    _rip_progress_last_send.clear()
    _rip_progress_last_pct.clear()
    yield
    _rip_progress_last_send.clear()
    _rip_progress_last_pct.clear()


@pytest.fixture
def job_ripping(test_db):
    """A job with rip_state=running — the only state in which the
    progress callback is allowed to write."""
    session = test_db()
    try:
        disc = models.Disc(
            id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:8]}",
            disc_number=1,
        )
        session.add(disc)
        session.flush()
        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            job_status="running",
            rip_state="running",
            rip_progress=0,
            stage_profile="hit",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        yield str(job.id)
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────────
# Basic write paths
# ──────────────────────────────────────────────────────────────────────────


def test_first_progress_call_writes_to_db(test_db, job_ripping):
    """No prior throttle state for this job — first call lands."""
    _post_rip_progress(job_ripping, rip_progress=42)

    session = test_db()
    try:
        job = crud.get_job(session, job_ripping)
        assert job.rip_progress == 42
    finally:
        session.close()


def test_phase_change_writes_even_when_within_throttle_window(
    test_db, job_ripping, monkeypatch
):
    """Phase changes bypass the throttle — needed because the
    rip→verification boundary must land promptly so the worker can
    proceed."""
    import workers.tasks as tasks_mod

    fake_time = [1000.0]
    monkeypatch.setattr(tasks_mod.time, "time", lambda: fake_time[0])

    _post_rip_progress(job_ripping, rip_progress=50, rip_phase="copy")
    # Within throttle window — would normally be dropped — but phase
    # change forces the write.
    fake_time[0] += 0.1
    _post_rip_progress(job_ripping, rip_progress=51, rip_phase="verification")

    session = test_db()
    try:
        job = crud.get_job(session, job_ripping)
        assert job.rip_phase == "verification"
    finally:
        session.close()


def test_clear_rip_phase_bypasses_throttle(test_db, job_ripping, monkeypatch):
    """clear_rip_phase is used at rip completion to wipe the phase
    marker — must always land."""
    import workers.tasks as tasks_mod

    fake_time = [1000.0]
    monkeypatch.setattr(tasks_mod.time, "time", lambda: fake_time[0])

    # Set initial state
    _post_rip_progress(job_ripping, rip_phase="copy", rip_progress=20)
    fake_time[0] += 0.1
    # Within throttle window but with clear_rip_phase=True — should land.
    _post_rip_progress(job_ripping, clear_rip_phase=True, rip_progress=21)

    session = test_db()
    try:
        job = crud.get_job(session, job_ripping)
        assert job.rip_phase is None
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────────
# Throttle
# ──────────────────────────────────────────────────────────────────────────


def test_throttle_drops_small_rapid_progress_updates(
    test_db, job_ripping, monkeypatch
):
    """Progress-only call within 2s window AND <5% delta is dropped.
    Critical: rip progress fires many times per second; without
    throttling the DB gets hammered."""
    import workers.tasks as tasks_mod

    fake_time = [1000.0]
    monkeypatch.setattr(tasks_mod.time, "time", lambda: fake_time[0])

    _post_rip_progress(job_ripping, rip_progress=50)
    fake_time[0] += 0.5
    # Within window, +2% — should be dropped.
    _post_rip_progress(job_ripping, rip_progress=52)

    session = test_db()
    try:
        job = crud.get_job(session, job_ripping)
        assert job.rip_progress == 50  # second call dropped
    finally:
        session.close()


def test_throttle_passes_large_progress_delta(
    test_db, job_ripping, monkeypatch
):
    """Progress jump ≥5% bypasses the time-based throttle so a fast
    rip doesn't appear stuck on the UI."""
    import workers.tasks as tasks_mod

    fake_time = [1000.0]
    monkeypatch.setattr(tasks_mod.time, "time", lambda: fake_time[0])

    _post_rip_progress(job_ripping, rip_progress=50)
    fake_time[0] += 0.5  # within time window
    _post_rip_progress(job_ripping, rip_progress=60)  # +10% — passes

    session = test_db()
    try:
        job = crud.get_job(session, job_ripping)
        assert job.rip_progress == 60
    finally:
        session.close()


def test_throttle_passes_after_time_window(
    test_db, job_ripping, monkeypatch
):
    """After the throttle window elapses, small deltas land again."""
    import workers.tasks as tasks_mod

    fake_time = [1000.0]
    monkeypatch.setattr(tasks_mod.time, "time", lambda: fake_time[0])

    _post_rip_progress(job_ripping, rip_progress=50)
    fake_time[0] += RIP_PROGRESS_THROTTLE_SECONDS + 0.1
    _post_rip_progress(job_ripping, rip_progress=51)  # small delta, but after window

    session = test_db()
    try:
        job = crud.get_job(session, job_ripping)
        assert job.rip_progress == 51
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────────
# State guard
# ──────────────────────────────────────────────────────────────────────────


def test_drop_progress_when_rip_not_running(test_db, job_ripping):
    """Progress against a job whose rip is not running (e.g. already
    completed) is silently dropped — matches the API endpoint's 409
    guard. Prevents stale callbacks from rewinding a completed job."""
    session = test_db()
    try:
        job = crud.get_job(session, job_ripping)
        job.rip_state = "completed"
        job.rip_progress = 100
        session.commit()
    finally:
        session.close()

    _post_rip_progress(job_ripping, rip_progress=5)

    session = test_db()
    try:
        job = crud.get_job(session, job_ripping)
        assert job.rip_state == "completed"
        assert job.rip_progress == 100
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────────────────────────────────


def test_empty_body_is_no_op(test_db, job_ripping):
    """If the caller passes no progress fields at all, nothing should
    be written. Avoids a useless DB roundtrip."""
    _post_rip_progress(job_ripping)  # no kwargs → empty body

    session = test_db()
    try:
        job = crud.get_job(session, job_ripping)
        assert job.rip_progress == 0  # unchanged
    finally:
        session.close()


def test_unknown_job_does_not_raise(test_db):
    """Callback for a deleted job logs and returns cleanly — never
    propagates an exception that would crash the rip worker mid-task."""
    _post_rip_progress(str(uuid.uuid4()), rip_progress=42)
    # If we got here, no exception propagated — pass.


# ──────────────────────────────────────────────────────────────────────────
# Regression guard
# ──────────────────────────────────────────────────────────────────────────


def test_ws_emit_payload_includes_rip_state(test_db, job_ripping, monkeypatch):
    """#604: the rip-progress WS payload must carry rip_state. The in-process
    path applies job state with skip_context_changed=True, so the
    authoritative context_changed WS event never fires; without rip_state
    on the progress emit, the frontend's jobStatus.rip_state stays at
    'running' through verifying → terminal and the spinner strands until
    the user hard-refreshes."""
    captured: list[tuple[str, dict]] = []

    def fake_emit(job_id: str, payload: dict) -> None:
        captured.append((job_id, dict(payload)))

    import core.progress_emitter as pe_mod

    monkeypatch.setattr(pe_mod, "emit_job_progress_debounced", fake_emit)

    _post_rip_progress(job_ripping, rip_progress=50)

    assert captured, "expected one progress emit"
    emitted_job_id, payload = captured[0]
    assert emitted_job_id == job_ripping
    assert "rip_state" in payload, (
        f"progress payload must include rip_state; got keys: {sorted(payload.keys())}"
    )
    # job_ripping fixture sets rip_state='running'.
    assert payload["rip_state"] == "running"


def test_ws_emit_payload_carries_terminal_rip_state_after_clear_phase(
    test_db, job_ripping, monkeypatch
):
    """#604 terminal repro: a job whose rip_state has just flipped to
    'completed' is exactly the state the verifying spinner hits. Confirm
    the payload picks up the post-write value, not a stale read."""
    captured: list[tuple[str, dict]] = []

    def fake_emit(job_id: str, payload: dict) -> None:
        captured.append((job_id, dict(payload)))

    import core.progress_emitter as pe_mod

    monkeypatch.setattr(pe_mod, "emit_job_progress_debounced", fake_emit)

    # Flip rip_state to 'completed' before the emit so we capture the
    # exact path the verifying → terminal transition takes.
    session = test_db()
    try:
        job = crud.get_job(session, job_ripping)
        job.rip_state = "running"  # state guard requires running/pending entry
        session.commit()
    finally:
        session.close()

    # First call lands and emits with rip_state='running'.
    _post_rip_progress(job_ripping, rip_progress=99, rip_phase="verification")
    assert captured and captured[-1][1]["rip_state"] == "running"

    # Mirror what the verification complete path does: terminal state +
    # clear_rip_phase. The state guard at function top requires
    # running/pending, so for this test we keep rip_state='running' at
    # entry; what we care about is the post-apply read.
    captured.clear()
    _post_rip_progress(
        job_ripping,
        rip_progress=100,
        clear_rip_phase=True,
        is_final=True,
    )

    assert captured, "final progress emit should fire"
    payload = captured[-1][1]
    assert "rip_state" in payload
    # rip_phase was cleared this call — the spinner-gate signal.
    assert payload["rip_phase"] is None


def test_ws_emit_payload_includes_post_state_and_transfer_state(
    test_db, job_ripping, monkeypatch
):
    """#605: the rip-progress WS payload must also carry post_state and
    transfer_state. Same skip_context_changed=True path as #604's rip_state
    fix — without these on the progress emit, the frontend's local
    jobStatus.post_state / .transfer_state stay stale through the entire
    transfer-step UX and the CTA button label / spinner gates show the
    wrong state.

    Note: post_state is the derived hybrid_property (#365 dropped the
    column); transfer_state is still a real column. During a rip in
    progress, derived_post_state returns None because rip_state isn't
    'completed' yet — that's the correct value to ship."""
    captured: list[tuple[str, dict]] = []

    def fake_emit(job_id: str, payload: dict) -> None:
        captured.append((job_id, dict(payload)))

    import core.progress_emitter as pe_mod

    monkeypatch.setattr(pe_mod, "emit_job_progress_debounced", fake_emit)

    # Seed transfer_state so the getattr in tasks.py picks it up.
    # post_state is derived; rip_state='running' on the fixture means
    # derived_post_state evaluates to None — which is the correct value.
    session = test_db()
    try:
        job = crud.get_job(session, job_ripping)
        job.transfer_state = "pending"
        session.commit()
    finally:
        session.close()

    _post_rip_progress(job_ripping, rip_progress=50)

    assert captured, "expected one progress emit"
    payload = captured[0][1]
    assert "post_state" in payload, (
        f"progress payload must include post_state; got keys: {sorted(payload.keys())}"
    )
    assert "transfer_state" in payload, (
        f"progress payload must include transfer_state; got keys: {sorted(payload.keys())}"
    )
    # During rip (rip_state='running'), derived_post_state returns None
    # per the decision table in models.py — that's the right shipped value.
    assert payload["post_state"] is None
    assert payload["transfer_state"] == "pending"


def test_no_http_layer_dependency(monkeypatch):
    """Regression guard: the highest-frequency callback must NOT fall
    back to ``requests.post``. Restoring HTTP coupling here would
    re-introduce #378 fragility AND hammer the API with thousands of
    roundtrips per rip."""
    import workers.tasks as tasks_mod

    calls = []

    def fail_on_post(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError(
            "in-process rip-progress must not POST anywhere — "
            f"saw call to requests.post with {args!r}"
        )

    monkeypatch.setattr(tasks_mod.requests, "post", fail_on_post)
    _post_rip_progress(str(uuid.uuid4()), rip_progress=42)
    assert calls == [], (
        "in-process rip-progress should never invoke requests.post; "
        f"saw {len(calls)} unexpected call(s)"
    )
