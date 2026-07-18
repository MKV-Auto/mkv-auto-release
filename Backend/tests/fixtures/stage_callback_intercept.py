"""
Shared helper: build a fake ``requests`` module that intercepts worker callback
POSTs (rip-progress, rip-complete, rip-verification-complete, postprocess-complete)
and applies job state via StageState / apply_job_state directly — bypassing the
HTTP round-trip that otherwise self-deadlocks under eager Celery.

Used in two places:

- ``Backend/tests/conftest_backend.py`` for ``@pytest.mark.integration`` tests
  that drive ``rip_disc.run(...)`` directly.

- ``Backend/scripts/e2e_bootstrap.py`` for the full-stack E2E backend
  (``run_e2e_backend.py``) so the rip → rip_verification → postprocess →
  transfer callback chain does not deadlock against ``CELERY_TASK_ALWAYS_EAGER``
  + single-process uvicorn (issue #378).

Keep the two call sites identical: callbacks are the only HTTP boundary inside
the test backend, and an HTTP detour through the live API costs ~60s per hop
when the calling thread holds the only worker.
"""
from __future__ import annotations

from typing import Any


def make_stage_callback_fake_requests():
    """Return a fake ``requests`` module whose ``.post()`` intercepts the
    well-known worker callback URLs and applies state through StageState
    directly. All other URLs fall through to the real ``requests.post``.
    """
    from workers.tasks import database as tasks_db
    from api import crud
    from core.job_state import StageState, _infer_profile, apply_job_state
    from workers import tasks as workers_tasks

    def _apply_rip_copy_callback(
        job_id: str,
        success: bool,
        error_reason: str | None = None,
        source_hashes: dict | None = None,
    ) -> None:
        from workers.rip_verification_impl import run_rip_verification_for_job

        db = tasks_db.SessionLocal()
        try:
            job = crud.get_job(db, job_id)
            if not job:
                return
            if not success:
                StageState.rip_failed(
                    db, job,
                    error_reason=error_reason or "Rip failed",
                    reason="stage_callback_intercept",
                )
                db.commit()
                return
            if getattr(job, "rip_state", None) != "running":
                StageState.rip_started(db, job, reason="stage_callback_intercept (rip_started before copy ack)")
            StageState.rip_copy_complete(db, job, reason="stage_callback_intercept rip-complete")
            db.commit()
        finally:
            db.close()

        try:
            run_rip_verification_for_job(workers_tasks.rip_verification, str(job_id))
        except Exception:
            pass

    def _apply_rip_verification_complete_callback(job_id: str, json_body: dict) -> None:
        db = tasks_db.SessionLocal()
        try:
            job = crud.get_job(db, job_id)
            if not job:
                return
            if json_body.get("success"):
                branch = _infer_profile(job) or "miss"
                ripped = json_body.get("ripped_files") or {}
                StageState.rip_complete(
                    db,
                    job,
                    branch=branch,
                    ripped_files=ripped,
                    source_hashes=json_body.get("source_hashes"),
                    reason="stage_callback_intercept rip_verification_complete",
                )
                db.commit()
                if branch == "hit":
                    StageState.postprocess_started(db, job, reason="stage_callback_intercept (hit)")
                    db.commit()
                    try:
                        workers_tasks.start_transfer.run(job_id=str(job_id))
                    except Exception:
                        pass
                elif branch == "miss" and json_body.get("preview_detect_keys"):
                    try:
                        workers_tasks.preview_and_detect.apply(
                            args=[str(job_id), json_body["preview_detect_keys"]],
                            kwargs={"rel_path_overrides": json_body.get("preview_detect_overrides")},
                        )
                    except Exception:
                        pass
            else:
                StageState.rip_failed(
                    db,
                    job,
                    error_reason=json_body.get("error_reason") or "Rip verification failed",
                    reason="stage_callback_intercept rip_verification_complete failure",
                    error_type=json_body.get("error_type"),
                )
                db.commit()
        finally:
            db.close()

    def _apply_postprocess_callback(
        job_id: str,
        success: bool,
        post_paths: dict | None = None,
        post_progress: int = 100,
        disc_payload_updates: dict | None = None,
        error_reason: str | None = None,
    ) -> None:
        db = tasks_db.SessionLocal()
        try:
            job = crud.get_job(db, job_id)
            if not job:
                return
            if success:
                StageState.postprocess_complete(
                    db, job,
                    post_paths=post_paths or {},
                    post_progress=post_progress,
                    disc_payload_updates=disc_payload_updates,
                    reason="stage_callback_intercept",
                )
            else:
                StageState.postprocess_failed(
                    db, job,
                    error_reason=error_reason or "Postprocess failed",
                    reason="stage_callback_intercept",
                )
            db.commit()
        finally:
            db.close()

    def _apply_rip_progress_callback(job_id: str, body: dict) -> None:
        allowed = {
            "rip_phase", "rip_progress", "titles_completed", "total_titles",
            "current_title_id", "current_title_number", "current_title_progress", "per_title_progress",
        }
        updates = {k: v for k, v in body.items() if k in allowed}
        if not updates:
            return
        db = tasks_db.SessionLocal()
        try:
            job = crud.get_job(db, job_id)
            if job:
                apply_job_state(db, job, updates=updates, reason="stage_callback_intercept rip_progress")
        finally:
            db.close()

    def _resp(status: int = 200, body: str = "") -> Any:
        return type("Resp", (), {"status_code": status, "text": body})()

    def _fake_post(url, *args, **kwargs):
        json_body = kwargs.get("json") or {}
        url_str = str(url)
        if "/rip-progress" in url_str:
            job_id = url_str.rstrip("/").split("/jobs/")[-1].split("/")[0]
            _apply_rip_progress_callback(job_id, json_body)
            return _resp()
        if "/rip-complete" in url_str:
            job_id = url_str.rstrip("/").split("/jobs/")[-1].split("/")[0]
            _apply_rip_copy_callback(
                job_id,
                success=json_body.get("success", False),
                error_reason=json_body.get("error_reason"),
                source_hashes=json_body.get("source_hashes"),
            )
            return _resp()
        if "/rip-verification-complete" in url_str:
            job_id = url_str.rstrip("/").split("/jobs/")[-1].split("/")[0]
            _apply_rip_verification_complete_callback(job_id, json_body)
            return _resp()
        if "/postprocess-complete" in url_str:
            job_id = url_str.rstrip("/").split("/jobs/")[-1].split("/")[0]
            _apply_postprocess_callback(
                job_id,
                success=json_body.get("success", False),
                post_paths=json_body.get("post_paths"),
                post_progress=json_body.get("post_progress", 100),
                disc_payload_updates=json_body.get("disc_payload_updates"),
                error_reason=json_body.get("error_reason"),
            )
            return _resp()
        return __import__("requests").post(url, *args, **kwargs)

    real_requests = __import__("requests")
    fake = type(real_requests)("requests")
    fake.post = _fake_post
    fake.RequestException = real_requests.RequestException
    return fake
