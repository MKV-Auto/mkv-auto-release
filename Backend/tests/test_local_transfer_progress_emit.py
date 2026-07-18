"""#499 — auto-dispatched local transfer emits WS progress on each callback.

Before the fix, ``_progress_cb`` inside
:func:`workers.tasks._maybe_auto_dispatch_local_transfer` wrote
``transfer_progress`` to the DB row but did not call
``emit_job_progress_debounced``. The frontend's transfer bar therefore
stayed frozen mid-transfer and only jumped to the terminal state on a
page refresh.

This test captures the ``transfer_progress_callback`` the dispatcher hands
to ``_execute_local_transfer_use_final_map``, invokes it with a mid-transfer
value, and asserts the debounced WS emit fires with ``transfer_progress``
equal to that value.
"""

import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api import models
from workers import tasks


@pytest.fixture
def job_pending_transfer(test_db, tmp_path):
    """A job that satisfies the dispatcher's preconditions: rip done,
    post_paths present so the use_final_map branch is taken."""
    session = test_db()
    try:
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=f"h-{uuid.uuid4().hex[:8]}",
            disc_number=1,
        )
        session.add(disc)
        session.flush()
        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            job_status="pending",
            rip_state="completed",
            transfer_state="pending",
            transfer_progress=0,
            phase="transfer",
            post_paths={"title_001.mkv": "Movie/title_001.mkv"},
            stage_profile="hit",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        yield str(job.id)
    finally:
        session.close()


def test_local_transfer_progress_cb_emits_websocket(
    test_db, tmp_path, job_pending_transfer, monkeypatch
):
    """Invoking the dispatcher's progress callback fires
    ``emit_job_progress_debounced`` with the current transfer_progress."""
    job_id = job_pending_transfer
    src_root = tmp_path / "src"
    src_root.mkdir()

    captured: dict = {}

    def _capture_callback(*args, **kwargs):
        # Capture the cb the dispatcher built; do nothing else.
        captured["cb"] = kwargs.get("transfer_progress_callback")

    fake_config = MagicMock()
    fake_config.id = "cfg-1"
    fake_config.mode = "local"

    emit_spy = MagicMock()

    with patch(
        "core.transfer.service.get_active_config", return_value=fake_config
    ), patch(
        "workers.tasks._resolve_transfer_src_root", return_value=src_root
    ), patch(
        "api.routers.jobs._try_src_equals_dest_shortcut", return_value=False
    ), patch(
        "api.routers.jobs._build_job_metadata", return_value={}
    ), patch(
        "api.routers.jobs._execute_local_transfer_use_final_map",
        side_effect=_capture_callback,
    ), patch(
        "core.progress_emitter.emit_job_progress_debounced", emit_spy
    ):
        tasks._maybe_auto_dispatch_local_transfer(job_id, MagicMock())

        assert "cb" in captured, "dispatcher did not reach the final-map helper"
        cb = captured["cb"]
        assert callable(cb)

        cb(50)

        assert emit_spy.call_count >= 1, "progress cb did not emit WS update"
        args, _ = emit_spy.call_args
        assert args[0] == job_id
        assert args[1].get("transfer_progress") == 50
        # #604 / #605: progress payload must carry stage states so the
        # frontend's CTA gate and transfer-stage UI advance through the
        # auto-dispatched local transfer without depending on a separate
        # context_changed refetch. Mirrors the assertion in
        # test_rip_progress_in_process.py.
        assert "rip_state" in args[1]
        assert "post_state" in args[1]
        assert "transfer_state" in args[1]
        assert args[1]["rip_state"] == "completed"
        # transfer_state was 'pending' at fixture time; the _progress_cb
        # immediately applies transfer_state='running' before emit, so the
        # post-write getattr in tasks.py should pick up 'running'.
        assert args[1]["transfer_state"] == "running"
