# MKV-Auto Configuration

Environment variables, volume layout, and networking for the container.

New here? Start with the [Quick start](QUICKSTART.md), or the
[Installation guide](INSTALLATION.md) for the full walkthrough. This document is
reference — it assumes you already have the container running.

## Configuration

### Environment Variables

#### Database Configuration

```bash
# External PostgreSQL (disables embedded)
DATABASE_URL=postgresql://user:password@host:5432/discs

# External Redis (disables embedded)
REDIS_URL=redis://host:6379/0
```

**Note:** If these are set to external hosts, embedded databases are automatically disabled.

#### Application Configuration

```bash
# Data directories (inside container)
MKVAUTO_ROOT=/data/mkvauto
MKVAUTO_DATA=/data/mkvauto/data
MKVAUTO_TMP_DIR=/data/mkvauto/tmp

# Logging
MKVAUTO_DEBUG_LEVEL=INFO  # DEBUG, INFO, WARNING, ERROR

# Timezone
TZ=America/New_York
```

#### MakeMKV Configuration

```bash
# Root helper socket path
MAKEMKV_ROOT_HELPER_SOCK=/tmp/makemkv_auto.sock
```

#### Disk space

```bash
# Rips and Dolby Vision conversions reserve the space they'll write before
# they start, so two of them can't both count on the same free space. This
# much is always kept free on top of every reservation (default 10 GiB), so
# the database never runs out of room.
MKVAUTO_SPACE_RESERVE_FLOOR_BYTES=10737418240
# Where Dolby Vision conversions of files on a network share do their work.
# Defaults to the data volume's tmp folder.
MKVAUTO_DV7_SCRATCH_DIR=/data/mkvauto/tmp/dv7-scratch
# Where an SMB or NFS transfer destination is mounted inside the container
# for the Dolby Vision library scan.
MKVAUTO_LIBRARY_MOUNT=/mnt/mkvauto-library
```

#### Optical drives

```bash
# How long the kernel waits on a single drive command before aborting it and
# resetting the drive. Linux defaults to 30 s, which a drive working through a
# slow area of a disc can exceed — and a reset mid-read can leave the drive
# unresponsive until it is power-cycled. MKV-Auto raises each drive to this
# value at startup and whenever a drive is (re)connected. It never lowers a
# value that is already higher. Requires the container to run privileged
# (the default compose file does); otherwise it logs a warning and changes
# nothing. 0 turns it off.
MKVAUTO_OPTICAL_COMMAND_TIMEOUT_SECONDS=180
```

### Unattended setup (application settings)

The variables above configure the *runtime*. These configure the *application* —
the same settings the setup wizard asks for. Set them and a fresh container comes
up already configured, with no wizard to click through: useful for Compose stacks
and anything rebuilt from a config file.

| Variable | Setting | Notes |
| --- | --- | --- |
| `MKVAUTO_MAKEMKV_KEY` | `makemkv_registration_key` | also written to MakeMKV's own `settings.conf` at startup |
| `MKVAUTO_TMDB_API_KEY` | `tmdb_api_key` | |
| `MKVAUTO_MEDIA_SERVER` | `media_server` | `plex` or `jellyfin` |
| `MKVAUTO_DISCORD_WEBHOOK_URL` | `discord.webhook_url` | |
| `MKVAUTO_DISCORD_ENABLED` | `discord.enabled` | |
| `MKVAUTO_AUTO_RIP` | `auto_rip_enabled` | start copying as soon as a disc finishes scanning |
| `MKVAUTO_EJECT_ON_FINISH` | `eject_on_finish` | |
| `MKVAUTO_EJECT_ON_RESTART` | `eject_on_restart` | |
| `MKVAUTO_PATH_TEMPLATE_MOVIE` | `path_template_movie` | |
| `MKVAUTO_PATH_TEMPLATE_SERIES` | `path_template_series` | |
| `MKVAUTO_PREVIEW_DURATION_SECONDS` | `preview_duration_seconds` | |
| `MKVAUTO_PREVIEW_MAX_PARALLEL` | `preview_max_parallel` | |

Booleans accept `1/0`, `true/false`, `yes/no`, `on/off`.

**The environment wins, on every boot — not just the first.** Editing your Compose
file and restarting changes the setting; it is not a one-time seed that stops
mattering once the container has state. The trade-off is the obvious one: a
setting you pin here **cannot be changed in the web UI**, because the next restart
would revert it. Those fields are shown disabled with a note saying where the
value comes from, and the setup wizard treats the steps they answer as already
complete.

To take a setting back under UI control, remove the variable and restart. The last
value applied stays in `settings.json` and becomes editable again.

An unset or empty variable means "not configured" and leaves the setting alone, so
`MKVAUTO_TMDB_API_KEY=${TMDB_KEY}` with `TMDB_KEY` undefined is harmless rather
than destructive. A value that cannot be parsed (`MKVAUTO_AUTO_RIP=ture`) is
logged and skipped rather than coerced — a typo must not silently turn a feature
off.

Transfer destinations are not settable this way: they live in the database with
encrypted credentials and are configured in the UI.

```yaml
services:
  mkv-auto:
    image: ghcr.io/mkv-auto/mkv-auto-release:latest
    environment:
      MKVAUTO_MAKEMKV_KEY: ${MAKEMKV_KEY}
      MKVAUTO_TMDB_API_KEY: ${TMDB_KEY}
      MKVAUTO_MEDIA_SERVER: jellyfin
      MKVAUTO_AUTO_RIP: "true"
      MKVAUTO_EJECT_ON_FINISH: "true"
```

Secrets in a Compose file are readable by anyone who can read the file and appear
in `docker inspect`. Keep them in a `.env` file that is not committed, or use your
orchestrator's secret mechanism.

### Complete Example

```bash
docker run -d \
  --name mkv-auto \
  -p 8080:80 \
  -v mkv-data:/data \
  -v /dev/sr0:/dev/sr0 \
  --device=/dev/sr0 \
  --privileged \
  -e TZ=America/Los_Angeles \
  -e MKVAUTO_DEBUG_LEVEL=DEBUG \
  --restart unless-stopped \
  ghcr.io/mkv-auto/mkv-auto-release:latest
```

## Volume Management

### Data Volume Structure

The `/data` volume contains all persistent data:

```
/data/
├── postgres/          # PostgreSQL database files (if embedded)
├── redis/             # Redis persistence files (if embedded)
└── mkvauto/
    ├── data/          # Job artifacts and ripped files
    ├── logs/          # Application logs
    └── tmp/           # Temporary files
```

### Backup Strategies

#### Full Volume Backup

```bash
# Stop container
docker stop mkv-auto

# Backup entire volume
docker run --rm \
  -v mkv-data:/data \
  -v $(pwd):/backup \
  ubuntu tar czf /backup/mkv-data-$(date +%Y%m%d).tar.gz /data

# Restart container
docker start mkv-auto
```

#### Database-Only Backup

```bash
# PostgreSQL (embedded)
docker exec mkv-auto pg_dump -U postgres discs > backup.sql

# PostgreSQL (external)
docker exec mkv-auto-postgres pg_dump -U postgres discs > backup.sql
```

#### Restore from Backup

```bash
# Create new volume
docker volume create mkv-data-new

# Restore
docker run --rm \
  -v mkv-data-new:/data \
  -v $(pwd):/backup \
  ubuntu tar xzf /backup/mkv-data-20260201.tar.gz -C /

# Use new volume
docker run -v mkv-data-new:/data ... mkv-auto
```

### Volume Migration

```bash
# Copy data between volumes
docker run --rm \
  -v mkv-data-old:/source \
  -v mkv-data-new:/dest \
  ubuntu bash -c "cp -a /source/* /dest/"
```

## Dolby Vision conversion (Plex compatibility)

UHD Blu-rays use **Dolby Vision profile 7**, which most Plex and Jellyfin clients, browsers and streaming devices can't play. MKV-Auto can convert those files to **profile 8.1**, which plays everywhere. The conversion is lossless: the video is copied bit for bit, the HDR metadata is kept, and only the enhancement layer, which streaming devices don't use, is dropped.

It's **off by default**. Turn it on in **Settings > Dolby Vision**:

- **Convert new rips** converts UHD rips during postprocessing, before they're transferred, so nothing is copied to your server twice. Only titles you've labeled are converted; ignored and unlabeled titles are left alone.
- **Scan library** looks through your **transfer destination** (Settings > Destinations) for existing files, but only in the folders MKV-Auto writes to: **Movies** and **Series** (and the first folder of a custom path template, if it's a fixed name). Anything else on the share is never read. Every video's format is recorded, and each Dolby Vision 7 file is queued for conversion, one at a time. There's nothing extra to set up: the library is wherever your rips are sent. Files under 1 MB, and `.ts` files that aren't video (TypeScript), are skipped. A file the scan can't read at all is listed as **Unreadable**: that usually means it's damaged or an incomplete download, so check whether it plays. On a large network share the first scan takes a while; Settings shows how many files it has checked so far, and later scans only re-read files that changed.

Before you turn it on:

- **How the library is reached depends on the destination type:**
  - **Local folder:** used as-is. It must be a folder mapped into the container, which a local destination already needs.
  - **SMB or NFS share:** MKV-Auto mounts the share inside the container while it scans and converts, using the destination's saved login. Nothing is mounted for ordinary transfers. If the mount fails, Settings shows the reason under **Library** and in the last scan's result.
  - **rsync:** the library can't be scanned over rsync. Map the library folder into the container and use a local destination instead. **Convert new rips** still works, because it converts before the transfer.
- **The original file is replaced.** Each converted file is checked before anything is touched: Dolby Vision 8.1 present, the same duration, the same audio and subtitle tracks. Only then does it replace the original. If any check fails, the original stays exactly as it was. There's no second copy kept afterwards, so back up your library first if you want an undo.
- **Stopping the container mid-conversion is safe.** The original is never deleted before the converted file is fully in place: it's set aside under a hidden name, the new file is moved in and checked, and only then is the original removed. Each step is recorded in `/data`, and when the container starts again any interrupted conversion is either finished (if the new file was already in place) or rolled back with the original restored, and then queued again.
- **Free space:** a conversion needs about **twice the file's size** in working space. For a file on a network share (NFS or SMB), MKV-Auto works in local scratch space when there's room and copies the finished file back, which roughly halves the network traffic. Otherwise it works next to the file. A file that can't get the space yet waits in the queue with "Waiting for disk space" and is retried automatically.
- **Time:** one file converts at a time. A UHD movie takes roughly as long as reading and writing the file once.

## Networking

### Port Mapping

```bash
# Default (port 8080)
-p 8080:80

# Custom port
-p 3000:80

# Bind to specific interface
-p 192.168.1.100:8080:80

# All interfaces
-p 0.0.0.0:8080:80
```

### HTTPS Setup

Use a reverse proxy like Traefik or NGINX:

#### Traefik Example

```yaml
services:
  mkv-auto:
    image: ghcr.io/mkv-auto/mkv-auto-release:latest
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.mkv-auto.rule=Host(`mkvauto.example.com`)"
      - "traefik.http.routers.mkv-auto.entrypoints=websecure"
      - "traefik.http.routers.mkv-auto.tls.certresolver=letsencrypt"
    volumes:
      - mkv-data:/data
      - /dev/sr0:/dev/sr0
    devices:
      - /dev/sr0
    privileged: true
```

#### NGINX Reverse Proxy

```nginx
server {
    listen 443 ssl http2;
    server_name mkvauto.example.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # WebSocket/SSE support
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_buffering off;
    }
}
```
