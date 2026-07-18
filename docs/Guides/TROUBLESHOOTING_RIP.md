# Troubleshooting: Start Copy Runs But No makemkvcon / Empty Celery Logs

When you click **Start Copy**, the UI shows "Copying" but the backend never runs `makemkvcon` and Celery logs look empty. This guide helps you find the cause.

## Flow (what should happen)

1. **Frontend** → `POST /api/jobs/rip` with `mount_point`, `disc_num`, optional `disc_hash`.
2. **API** → `DriveGatekeeper.start_rip()` → creates job, dispatches Celery task `rip_disc` with `task_id=rip_disc:{job_id}` to queue **rip**.
3. **Celery** → Worker consuming queue `rip` receives `rip_disc` → acquires file lock → sets job running → runs MakeMKV (Disc class / makemkvcon).
4. **UI** → Shows progress via WebSocket and job status.

If the UI says "Copying" but nothing runs, the job was created and marked running by the API, but either the **task never ran** or it **exited early** before starting makemkvcon.

---

## 1. Log commands (Docker)

From the host:

```bash
# List available logs
docker exec mkv-auto ls /data/mkvauto/logs/

# View last 100 lines of Celery rip worker (where rip_disc runs)
docker exec mkv-auto tail -100 /data/mkvauto/logs/celery_rip.log

# View API/uvicorn (POST /rip and gatekeeper)
docker exec mkv-auto tail -100 /var/log/supervisor/uvicorn.log
docker exec mkv-auto tail -100 /var/log/supervisor/uvicorn_err.log

# Follow in real time (then click Start Copy in another tab)
docker exec mkv-auto tail -f /data/mkvauto/logs/celery_rip.log
# Or all Celery workers (rip, postprocess, transfer, preview, maintenance)
docker exec mkv-auto tail -f /data/mkvauto/logs/celery.log
```

---

## 2. What to look for

### A. Task never received (Celery logs empty or no "rip_disc")

- **Check API log** for a line like:
  - `DISPATCHING rip_disc task ... job=... task_id=rip_disc:...`
  - `Dispatched Celery task rip_disc ...`
- If you see that but **no** `rip_disc TASK STARTED` or `rip_disc task started` in Celery logs, the worker is not consuming the **rip** queue or cannot reach Redis.

**Possible causes:**

- Redis not reachable from the container (wrong `REDIS_URL` or Redis down).
- Worker not started or crashed: `docker exec mkv-auto supervisorctl status` — `celery-rip`, `celery-postprocess`, `celery-transfer`, `celery-preview`, and `celery` should be `RUNNING`.
- Task routed to wrong queue (`rip_disc` is routed to queue `rip`; only `celery-rip` consumes `rip`).

### B. Task received but returns before makemkvcon

In **celery_rip.log** (under `/data/mkvauto/logs/`), look for:

- `rip_disc TASK STARTED` / `rip_disc task started` → task did run.
- Then one of these **early exits**:
  - `DUPLICATE TASK DETECTED` / `Skipping duplicate rip_disc` → task thought another job/task was already running.
  - `Skipping rip_disc task for job ... job is already failed/completed` → job state was already terminal.
  - `Skipping rip task: rip already completed` → `rip_state` was already `completed`.
  - `Lock held` / `Timeout` → file lock (disc-ripper.lock or .scan) held by another process or stale; task may retry or fail job.

If you see **Worker accepted job; preparing output paths** and then no further progress, the failure is usually **after** lock and **before** or during Disc/makemkvcon (e.g. path creation, drive access, or MakeMKV not installed).

### C. Drive / mount_point not available in container

- The container must see the optical drive (e.g. `--device=/dev/sr0`). If the frontend sends a **host** device path that doesn’t exist in the container (e.g. different host path), the rip will fail when the task tries to use it.
- In API log, note the `mount_point` in the `POST /jobs/rip` / `gatekeeper.start_rip` lines. Inside the container, that path must exist and be the correct block device.

### D. MakeMKV not installed or not in PATH

- In container: `docker exec mkv-auto which makemkvcon` (should print a path).
- If missing, install MakeMKV via the Setup Assistant (Settings → MakeMKV).

---

## 3. Quick checklist

| Check | Command / Where |
|-------|------------------|
| Container running | `docker ps \| grep mkv-auto` |
| Celery workers up | `docker exec mkv-auto supervisorctl status` → all five `celery*` programs RUNNING |
| Rip task dispatched | `docker exec mkv-auto tail -200 /var/log/supervisor/uvicorn.log` → "Dispatched Celery task rip_disc" |
| Rip task received | `docker exec mkv-auto tail -200 /data/mkvauto/logs/celery_rip.log` → "rip_disc TASK STARTED" or "rip_disc task started" |
| Redis reachable | `docker exec mkv-auto redis-cli ping` → PONG |
| MakeMKV in container | `docker exec mkv-auto which makemkvcon` |
| Lock path writable | `docker exec mkv-auto ls -la /data/mkvauto/tmp/` (lock file lives under MKVAUTO_TMP_DIR) |

---

## 4. Common fixes

- **Workers not running:** `docker exec mkv-auto supervisorctl restart celery celery-rip celery-postprocess celery-transfer celery-preview`
- **Redis/DB wrong after reset:** Ensure migrations ran (`docker exec mkv-auto /app/venv/bin/alembic -c /app/backend/alembic.ini upgrade head` from `/app/backend`, or simply restart the container); then restart backend services: `docker exec mkv-auto supervisorctl restart uvicorn celery celery-rip celery-postprocess celery-transfer celery-preview`
- **Stale lock:** Lock is under `MKVAUTO_TMP_DIR` (e.g. `/data/mkvauto/tmp/disc-ripper.lock`). If you're sure no other rip is running, you can remove the lock file and retry (optional):  
  `docker exec mkv-auto rm -f /data/mkvauto/tmp/disc-ripper.lock /data/mkvauto/tmp/disc-ripper.lock.scan`
- **Duplicate / wrong job state:** If the UI shows "Copying" but the task keeps skipping (duplicate or already completed), create a **new** job (e.g. new disc or reset that job state) and try Start Copy again.

---

## 5. Collecting a full snapshot for debugging

Run these and save output (then click Start Copy once and repeat the tail commands):

```bash
docker exec mkv-auto supervisorctl status
docker exec mkv-auto tail -300 /var/log/supervisor/uvicorn.log
docker exec mkv-auto tail -300 /var/log/supervisor/uvicorn_err.log
docker exec mkv-auto tail -300 /data/mkvauto/logs/celery_rip.log
docker exec mkv-auto tail -100 /data/mkvauto/logs/celery_postprocess.log
docker exec mkv-auto tail -100 /data/mkvauto/logs/celery_preview.log
docker exec mkv-auto tail -100 /data/mkvauto/logs/celery.log
```

Then search the uvicorn log for `POST /jobs/rip` and `rip_disc`, and the celery logs for `rip_disc` and any of the skip/duplicate/lock messages above.

---

## 6. Post-process fails: No raw directory / 0 MKV files

If post-process fails with **"No raw directory found to resume"** or **"0 MKV files"** (or `ripped_files_keys_count=0`), the root cause is often that the **rip was marked complete despite producing no output files**. The system should treat "no output files" as a **rip** failure, not a post-process failure.

**What to check:**

- **Rip worker logs** (`celery_rip.log`): Look for `Warning: Failed to calculate source hashes` or a `gather_final_outputs` failure inside `rip_disc` for the job. That indicates the rip path completed MakeMKV but found no MKV files in `raw/` before (incorrectly) setting `rip_state=completed` and (on hit) `post_state=ready` and enqueueing `resume_postprocess`.
- **Post-process logs**: Post-process status is in Celery/worker logs; search for `resume_postprocess` and the job id to see the exact error (e.g. "No raw directory", "0 MKV files").

If you hit this state, eject and re-insert the disc to start a fresh scan, or file an issue with the log lines above.

---

## 7. Callback logging (rip-complete / postprocess-complete)

Rip and postprocess completion are reported by the worker to the API via **localhost-only** callbacks (`POST /jobs/{id}/rip-complete`, `POST /jobs/{id}/postprocess-complete`). If the job never moves to the next stage after the worker finishes, the process may have died **during** the HTTP callback.

**What to look for:**

- In **worker logs** (e.g. `celery_rip.log`): Messages like "reporting rip-complete" or "rip-complete callback starting" (or equivalent in your code). If you see "MakeMKV finished" / "gather_final_outputs completed" but **no** "rip-complete callback" (or "postprocess-complete callback"), the worker may have crashed before or during the POST.
- **After** the callback: If the worker logs "rip-complete callback starting" or "POST ... rip-complete" but the **job in the API/DB** still shows `rip_state=running` (or `post_state=running`), the API may have rejected the request (e.g. 4xx/5xx) or the request never reached the API (network, API down). Check API logs for `POST /jobs/.../rip-complete` or `postprocess-complete` and the response status.

**If the job didn’t update:** The worker reports outcome only via the callback. There is no automatic recovery from disk: if the worker dies before the callback succeeds, the job stays in `rip_state=running` (or `post_state=running`). Use **Start Copy** again (or retry postprocess) to create a new task; the API will mark orphaned rip jobs as failed on startup (`_fail_orphaned_rip_jobs_on_startup`).
