# MKV-Auto Docker Guide

How the MKV-Auto image is put together and the ways to run it: deployment
options, disc detection, security, and production setups. Volumes and
networking live in [CONFIGURATION.md](CONFIGURATION.md).

## Table of Contents

- [Image Architecture](#image-architecture)
- [Building the image](#building-the-image)
- [Automatic Disc Detection](#automatic-disc-detection)
- [Deployment Options](#deployment-options)
- [Configuration, volumes and networking](CONFIGURATION.md)
- [Security](#security)
- [Advanced Configurations](#advanced-configurations)
- [Production Deployment](#production-deployment)

## Image Architecture

### Container Structure

The MKV-Auto Docker image is a **single-container deployment** containing all necessary services:

```
┌─────────────────────────────────────┐
│     MKV-Auto Container (Port 80)    │
├─────────────────────────────────────┤
│  NGINX (Reverse Proxy)              │
│    ├── Frontend (/)                 │
│    └── API (/api/*)                 │
├─────────────────────────────────────┤
│  FastAPI Backend (Port 8000)        │
│  Celery Worker                      │
│  Root Helper                        │
├─────────────────────────────────────┤
│  PostgreSQL 15 (embedded/external)  │
│  Redis 8 (embedded/external)        │
├─────────────────────────────────────┤
│  Supervisor (Process Manager)       │
└─────────────────────────────────────┘
```

### Process Management

The container uses a **two-layer process management architecture** for reliable operation:

**Layer 1: Tini (PID 1)**
- Lightweight init system (~10KB)
- Reaps zombie/defunct processes automatically
- Forwards signals (SIGTERM, SIGINT) to child processes
- Ensures clean container shutdown
- Critical for handling MakeMKV's Java subprocess (blues.jar) zombies

**Layer 2: Supervisor (PID 2)**
- Manages application services:
  - PostgreSQL (if embedded)
  - Redis (if embedded)
  - NGINX
  - FastAPI/Uvicorn
  - Celery workers: `celery-rip`, `celery-postprocess`, `celery-transfer`, `celery-preview`, and `celery` (default queue / maintenance)
  - Root Helper
- Automatic restart on failure
- Process group management (stopasgroup=true, killasgroup=true)

**Rebuilding the image:** Changes to `Docker/supervisord.conf` or the Dockerfile are only applied when the image is rebuilt. Restarting the container (`docker restart mkv-auto`) reuses the same image. To pick up config changes: rebuild the image per [Building the image](#building-the-image), then stop and start the container so the new image is used.

**Zombie Process Handling:**

MakeMKV spawns Java processes (blues.jar) for Blu-ray DRM decryption. These grandchild processes can become zombies if not properly reaped:

```
tini (PID 1)
 └─ supervisord (PID 2)
     └─ celery worker
         └─ makemkvcon
             └─ java (blues.jar) → becomes defunct without proper reaping
```

The container uses multiple mechanisms to prevent zombie accumulation:

1. **Tini automatic reaping**: Primary mechanism, handles all zombies at PID 1 level
2. **Periodic cleanup task**: Celery task runs every 5 minutes as defense-in-depth
3. **Application-level reaping**: Python code monitors and reaps zombies during recovery scenarios

**Monitoring zombie processes:**

```bash
# Check for defunct/zombie processes
docker exec mkvauto ps aux | grep defunct

# Count zombie processes
docker exec mkvauto ps aux | grep defunct | wc -l

# View zombie cleanup logs
docker exec mkvauto tail -f /data/mkvauto/logs/celery.log | grep zombie
```

**Expected behavior:**
- Java processes appear during rip operations
- Processes are reaped immediately after completion
- No zombie accumulation over time

## Building the image

Most users should **pull the published image** (`ghcr.io/mkv-auto/mkv-auto-release`) instead of building. If you do build, the image must include a built frontend (`Frontend/dist/`):

- **From the repo root:** Build the frontend first (`cd Frontend && npm ci && npm run build -- --configuration=production`), then build the image with `docker build -f Docker/Dockerfile .` (or `docker compose -f Docker/docker-compose.yml build`). The image must include `Frontend/dist/`; `.dockerignore` does not exclude it, so the built output is included in the context when present. Skipping the frontend build produces an image whose web UI serves nothing.

- **Official images:** Built by GitHub Actions in the [mkv-auto-release](https://github.com/MKV-Auto/mkv-auto-release) repo on tag `v*.*.*`: the workflow builds the frontend, then runs `docker build` with context `.`, and pushes to GHCR (`ghcr.io/mkv-auto/mkv-auto-release`).

**Testing the image:** Run `./scripts/test-docker.sh` from the repo root. It starts a test container and volume, verifies the health endpoint and frontend, then tears down the container and `mkv-test-data` volume on exit. Use `./scripts/test-docker.sh --keep` to leave the container running (e.g. for the MakeMKV updater E2E test); clean up manually when done.

### First-boot MakeMKV pre-download (#625)

On first startup, the backend downloads the MakeMKV source tarballs (`makemkv-bin-*.tar.gz` + `makemkv-oss-*.tar.gz`) in the background so the Setup Assistant can link to the extracted End User License Agreement text *before* the user clicks Install. The download is:

- **Non-blocking** — runs as an asyncio task in the FastAPI lifespan after HTTP starts serving.
- **Skipped** when MakeMKV is already installed (the EULA link only renders during the not-installed phase).
- **Idempotent** — cached under `${MKVAUTO_TMP_DIR}/makemkv-download/{version}/`; container restarts with the cache present don't re-download.
- **Non-fatal** — if the pre-download fails, the Install button still works and falls back to inline download; a subdued hint appears in the Setup Assistant.

To force a fresh pre-download, delete the cache dir and restart the container:

```bash
docker exec mkv-auto rm -rf /data/mkvauto/tmp/makemkv-download
docker restart mkv-auto
```

### Dependencies & Licensing

**Base Image:** Debian 12 (Bookworm)

**Included Software:**
- PostgreSQL 15 (PostgreSQL License - permissive)
- Redis 8 (BSD License)
- NGINX 1.22+ (BSD-like License)
- Python 3.11, Node.js 20 (open source)
- FFmpeg - Debian package (LGPL 2.1+, permissive)
  - Pre-installed for basic operations
  - Users can optionally rebuild during MakeMKV installation with GPL/non-free codecs

**Non-Free Dependencies:**
- `libfdk-aac-dev` - AAC encoding library from Debian non-free (patent-encumbered)
- Required for MakeMKV compilation, included as build dependency only

**MakeMKV (User-Installed):**
- **Not included in the image** - must be installed via the UI
- Proprietary shareware (free during beta, license required after)
- Users accept MakeMKV's EULA when clicking "Install" in the setup wizard
- EULA is included in the MakeMKV source download
- Optional FFmpeg rebuild: When "Include ffmpeg" is selected, FFmpeg is recompiled with `--enable-gpl --enable-nonfree --enable-libfdk-aac`, making the compiled version GPL 2.0+ with patent-encumbered codecs (AAC, H.264)

Processes start in priority order and restart automatically on failure.

### Image Size

- **Compressed**: ~800MB
- **Uncompressed**: ~2.5GB

## Automatic Disc Detection

The container includes automatic disc insertion/ejection detection using udev. When you insert or eject a disc, the Drive Manager automatically rescans without manual intervention.

### How It Works

1. **udev daemon** runs inside the container (managed by supervisor)
2. When you insert/eject a disc, **udev detects the event** via `DISK_MEDIA_CHANGE`
3. udev triggers the **notification script** (`udev_notify.py`) via Unix Domain Socket
4. **Drive Manager** invalidates cache and rescans
5. **Frontend** automatically updates to show/clear the disc

### Requirements

- Container must run in **privileged mode** (already configured in docker-compose.yml)
- Optical drive must be passed to container via **devices** and **volumes**

Both requirements are already met in the provided docker-compose.yml configuration.

### Multiple Drives

The udev rules support multiple drives automatically. Pass every optical device you use into the container (see below).

**Important:** `/dev/sr0`, `/dev/sr1`, … are **kernel enumeration order**, not guaranteed physical “bay 1 / bay 2”. **MakeMKV’s `DRV:0`, `DRV:1`, … also need not match the `srN` number** (e.g. `DRV:0` can be `/dev/sr2`). The API runs **`makemkvcon info disc:9999` at startup** and uses **throttled `disc:9999`** for drive-list refresh, then **insert-style scans** per loaded disc (hash + **`info dev:{path}`**, then parse **`DRV:`** to update the path→MakeMKV index cache)—**in parallel across drives** by default—so the coordinator carousel matches trays after renumbering. Set **`MKVAUTO_STARTUP_DISC_RESCAN_SERIAL=1`** to run those rescans **one after another** if you hit instability. Rips use **`mkv disc:{index}`** when the job’s disc index is known. Set **`MKVAUTO_SKIP_STARTUP_DISC_RESCAN=1`** to skip the per-disc scans (e.g. headless CI) while still mapping drives. Drives with **no volume label** in MakeMKV’s `DRV:` output are omitted from the drive list.

To use multiple drives, add them to `Docker/docker-compose.yml`:

```yaml
services:
  mkv-auto:
    devices:
      - /dev/sr0
      - /dev/sr1
      - /dev/sr2
    volumes:
      - /dev/sr0:/dev/sr0
      - /dev/sr1:/dev/sr1
      - /dev/sr2:/dev/sr2
```

### Disc cache file (optional)

The backend keeps disc metadata **in memory** while running. Writing **`drive_cache.json`** to disk is **opt-in**: set **`MKVAUTO_PERSIST_DISC_CACHE=1`** if you explicitly want persistence across process restarts. **`MKVAUTO_DISABLE_DISC_CACHE=1`** forces persistence off. After drive topology changes, a stale cache file can mis-associate discs.

**Startup rescan:** By default the API repopulates in-memory disc cache from drives at boot (see multiple-drives section above), with **parallel** per-drive work unless **`MKVAUTO_STARTUP_DISC_RESCAN_SERIAL=1`**. Use **`MKVAUTO_SKIP_STARTUP_DISC_RESCAN=1`** if you must avoid hashing or long `makemkvcon` runs on startup (containers without discs, automated tests).

### Benefits

- **Zero host configuration** - works out of the box with no udev rules on the host
- **Multiple drives** - automatically detects all `/dev/sr*` devices
- **Reliable** - uses proven Unix Domain Socket communication
- **Fast** - near-instant detection (< 1 second)
- **Simple** - no manual refresh needed

### Troubleshooting

**Check udev logs:**

```bash
docker exec mkv-auto tail -f /var/log/supervisor/udevd.log
```

**Test udev rule manually:**

```bash
docker exec mkv-auto udevadm test /sys/block/sr0
```

**Check if udevd is running:**

```bash
docker exec mkv-auto supervisorctl status udevd
```

**Manually trigger udev event:**

```bash
docker exec mkv-auto udevadm trigger --subsystem-match=block --action=change
```

## Deployment Options

### Option 1: Embedded Databases (Recommended for Home Use)

**Simplest deployment** - everything in one container. This is the default and
what [QUICKSTART.md](QUICKSTART.md#1-start-the-container) sets up.

**Pros:**
- Single container to manage
- No external dependencies
- Simple backup (one volume)
- Perfect for home/lab use

**Cons:**
- Cannot scale horizontally
- Larger container

### Option 2: External Databases (Recommended for Production)

**Better scalability** - separate database containers.

**Docker/docker-compose.external.yml:**
```yaml
version: '3.8'
services:
  mkv-auto:
    image: ghcr.io/mkv-auto/mkv-auto-release:latest
    ports:
      - "8080:80"
    volumes:
      - mkv-data:/data
      - /dev/sr0:/dev/sr0
    devices:
      - /dev/sr0
    privileged: true
    environment:
      - DATABASE_URL=postgresql://postgres:password@postgres:5432/discs
      - REDIS_URL=redis://redis:6379/0
    depends_on:
      - postgres
      - redis

  postgres:
    image: postgres:17
    environment:
      POSTGRES_DB: discs
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: password
    volumes:
      - postgres-data:/var/lib/postgresql/data

  redis:
    image: redis:8
    volumes:
      - redis-data:/data

volumes:
  mkv-data:
  postgres-data:
  redis-data:
```

**Pros:**
- Can scale backend horizontally
- Better resource isolation
- Easier to upgrade databases
- Better for cloud deployments

**Cons:**
- Three containers to manage
- More complex setup

## Configuration, volumes and networking

Moved to **[CONFIGURATION.md](CONFIGURATION.md)** — environment variables,
the `/data` volume layout, backup and migration, port mapping and HTTPS.

## Security

### Privileged Mode

**Why Required:**
- Optical drive access requires direct hardware access
- MakeMKV needs low-level disc operations
- Mount operations for remote file systems

**Security Considerations:**
- Only run on trusted networks
- Don't expose port 80 directly to internet
- Use reverse proxy with authentication for remote access

### User Permissions

Services run as appropriate users:
- PostgreSQL: `postgres` user
- Redis: `redis` user
- NGINX: `www-data` user
- Backend/Celery: `root` (for drive operations)

### Network Isolation

```yaml
# Create isolated network
networks:
  mkvauto-net:
    driver: bridge
    internal: true  # No external access

services:
  mkv-auto:
    networks:
      - mkvauto-net
      - default  # External access only for this service
  
  postgres:
    networks:
      - mkvauto-net  # No external access
```

## Advanced Configurations

### Multiple Optical Drives

```yaml
volumes:
  - /dev/sr0:/dev/sr0
  - /dev/sr1:/dev/sr1
  - /dev/sr2:/dev/sr2
devices:
  - /dev/sr0
  - /dev/sr1
  - /dev/sr2
```

### Custom Build

```bash
# Clone repository
git clone https://github.com/MKV-Auto/mkv-auto-release.git
cd mkv-auto-release

# Build the frontend first (required — the image copies Frontend/dist/)
cd Frontend && npm ci && npm run build -- --configuration=production && cd ..

# Build with custom tag
docker build \
  --build-arg VERSION=custom \
  -f Docker/Dockerfile \
  -t mkv-auto:custom \
  .

# Run custom build
docker run ... mkv-auto:custom
```

### Health Check Configuration

```yaml
healthcheck:
  test: ["CMD", "/healthcheck.sh"]
  interval: 30s      # Check every 30 seconds
  timeout: 10s       # Timeout after 10 seconds
  retries: 3         # Mark unhealthy after 3 failures
  start_period: 60s  # Allow 60s for startup
```

### Resource Limits

```yaml
services:
  mkv-auto:
    deploy:
      resources:
        limits:
          cpus: '4.0'
          memory: 4G
        reservations:
          cpus: '2.0'
          memory: 2G
```

## Production Deployment

### High-Availability Setup

**Not recommended** - MKV-Auto is designed for single-instance deployment due to optical drive hardware access. For redundancy:

- Use external databases with replication
- Run multiple instances with different drives
- Implement load balancer for read-only operations

### Monitoring

#### Health Monitoring

```bash
# Check health endpoint
curl http://localhost:8080/api/system/health

# Watch container health
watch 'docker inspect --format="{{.State.Health.Status}}" mkv-auto'
```

#### Log Management

```yaml
services:
  mkv-auto:
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

#### Metrics Collection

Integrate with Prometheus:

```yaml
# Add Prometheus exporter
services:
  mkv-auto-exporter:
    image: prom/process-exporter
    volumes:
      - /proc:/host/proc:ro
    command:
      - '--procfs=/host/proc'
      - '--children=true'
```

### Automated Backups

```bash
#!/bin/bash
# backup.sh - Daily backup script

BACKUP_DIR="/backups/mkv-auto"
DATE=$(date +%Y%m%d)

# Create backup directory
mkdir -p "$BACKUP_DIR"

# Backup database
docker exec mkv-auto pg_dump -U postgres discs | \
  gzip > "$BACKUP_DIR/db-$DATE.sql.gz"

# Backup volume
docker run --rm \
  -v mkv-data:/data \
  -v "$BACKUP_DIR":/backup \
  ubuntu tar czf "/backup/data-$DATE.tar.gz" /data

# Cleanup old backups (keep 30 days)
find "$BACKUP_DIR" -name "*.gz" -mtime +30 -delete

# Run daily via cron
# 0 2 * * * /path/to/backup.sh
```

### Disaster Recovery

1. **Stop container:**
   ```bash
   docker stop mkv-auto
   ```

2. **Restore volume:**
   ```bash
   docker run --rm \
     -v mkv-data:/data \
     -v /backups:/backup \
     ubuntu tar xzf /backup/data-latest.tar.gz -C /
   ```

3. **Restore database (if needed):**
   ```bash
   docker exec mkv-auto psql -U postgres discs < backup.sql
   ```

4. **Restart:**
   ```bash
   docker start mkv-auto
   ```

## Troubleshooting

See **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** — drive not detected, disc
auto-reinserting, container won't start, web UI unreachable, permissions,
and database errors.

## Support

- **Documentation**: [README.md](../../README.md), [INSTALLATION.md](INSTALLATION.md)
- **Architecture**: [README.development.md](../../README.development.md)
- **GitHub Issues**: https://github.com/MKV-Auto/mkv-auto-release/issues

## License

GNU Affero General Public License v3.0 (AGPL-3.0-only) — see [LICENSE](../../LICENSE) for the full text.
