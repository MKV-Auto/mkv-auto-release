# Backend (FastAPI + Celery)

FastAPI API and Celery workers coordinate ripping/copying, post-processing, transfers, and disc/release labeling. State lives in Postgres; Redis is the Celery broker/backend. Release/disc/job IDs are the primary keys; disc hashes uniquely identify discs.

## Install
```bash
cd Backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### System deps (MakeMKV/FFmpeg build support)
```bash
sudo apt-get update -qq && sudo apt-get install -y \
  build-essential yasm pkg-config cmake autoconf automake \
  libfdk-aac-dev libx264-dev libass-dev libfreetype6-dev \
  libgnutls28-dev libmp3lame-dev libsdl2-dev libtool libva-dev \
  libvdpau-dev libvorbis-dev libxcb1-dev libxcb-shm0-dev libxcb-xfixes0-dev \
  zlib1g-dev libssl-dev
```

## Configuration
- `DATABASE_URL`: Postgres DSN for SQLAlchemy. If unset, `api/database.py` defaults to `postgresql+psycopg2://postgres:ripper_pass@localhost:5432/discs`.
  - **Docker dev DB** ([`Docker/docker-compose.dev.yml`](../Docker/docker-compose.dev.yml)): Postgres is exposed on the host as **`127.0.0.1:5432`**, user `postgres`, password **`changeme`**, database **`discs`**:
    - `export DATABASE_URL='postgresql+psycopg2://postgres:changeme@127.0.0.1:5432/discs'`
  - If the **`mkv` dev stack** publishes Postgres on a **LAN address**, use that host instead of `127.0.0.1` with the same user/password/db as your compose file (often still `postgres` / `changeme` / `discs`).
  - **Optional venv hook**: copy [`venv_database_url.env.example`](venv_database_url.env.example) to **`.venv/database_url.env`** inside `Backend/`; `source .venv/bin/activate` will load it when `DATABASE_URL` is not already set.
- `REDIS_URL` (defaults in workers): Redis broker/backend, e.g. `redis://localhost:6379/0`
- Storage roots:
  - `MKVAUTO_ROOT`: base for config/logs (default `~/MakeMKV-Auto`)
  - `MKVAUTO_DATA`: job/artifact root (jobs under `${MKVAUTO_DATA}/jobs`, default `${MKVAUTO_ROOT}/data`)
- `MKVAUTO_TMP_DIR`: temp/work dir for MakeMKV runs (default `${MKVAUTO_ROOT}/tmp`); makemkv temp/data lives here
- Drive lock files live under `${MKVAUTO_TMP_DIR}`; stale locks are auto-cleared if no makemkvcon is running.
- Other knobs: `STALE_JOB_TIMEOUT_SECONDS`, `RIP_OUTPUT_STALL_SECONDS` (max seconds without a makemkvcon stdout line before the worker kills the rip; default `300`, `0` disables), `MIN_OUTPUT_FREE_BYTES`, `MKVAUTO_DISABLE_AUTOSCAN`

## Migrations
```bash
cd Backend
source .venv/bin/activate
alembic upgrade head
```

## Run (manual)
```bash
# API
uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload

# Workers (separate queues: rip never blocks on previews/ffmpeg)
celery -A workers.tasks worker -Q rip --loglevel=info    # rip_disc, recover_running_rip
celery -A workers.tasks worker -Q celery --loglevel=info # previews, postprocess, load_disc_info, etc.
```

## Core APIs (current refactor)
- Jobs: `/jobs/rip`, `/jobs/{id}/status`, `/jobs/current`, `/jobs/{id}/transfer`, `/jobs/{id}/resume`
- Releases: `/releases`, `/releases/{slug}`, `/releases/{slug}/progress`, `/releases/{slug}/export`
- Discs: `/releases/disc/{disc_id}/label`, `/releases/disc/by-hash`, `/releases/{slug}/discs`
- SSE: `/events/job/{id}`, `/events/drive`
- Dev mode validation: enable with `ENABLE_DEVMODE=1`; DiscDB data repo via `THEDISCDB_REPO` (default https://github.com/TheDiscDb/data.git), branch `THEDISCDB_BRANCH` (default main), cache at `${MKVAUTO_ROOT}/thediscdb`. Finalize writes metadata to `${MKVAUTO_DATA_DIR||MKVAUTO_DATA||MAKEMKV_DATA_DIR||MKVAUTO_ROOT}/export`. Validation report HTML available at `/jobs/{job_id}/dev-report`.

## Data model
- `releases`: slug/uuid, type (movie/series/boxset), name/title, tmdb/upc/asin/covers, finalize_state
- `discs`: uuid, content_hash (unique), release FK, disc_number, disc_slug/name, format, label_payload/draft, finalize_result, artifacts
- `jobs`: uuid, disc FK, mode (copy/rip/archive), stage states (rip/postprocess/transfer), progress/per-title progress, tmp/output/result paths, final_paths, transfer status, logs

## Testing
```bash
cd Backend
source .venv/bin/activate
pytest
```

## Notes
- `disc_payload` is kept only as a transitional cache; new code should use release/disc relations.
- Legacy “group” endpoints are being removed in favor of release/disc APIs.
