# syntax=docker/dockerfile:1.4
FROM debian:12

ARG VERSION=dev
ARG BUILD_DATE
ARG VCS_REF

# Labels for metadata
LABEL org.opencontainers.image.title="MKV-Auto" \
      org.opencontainers.image.description="Automated optical disc ripping and media management system" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="https://github.com/MKV-Auto/mkv-auto-release" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    TZ=UTC \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    # #699: runtime-readable app version (OCI labels aren't visible from inside).
    # "dev" (the ARG default) disables the update check entirely.
    MKVAUTO_VERSION=${VERSION}

WORKDIR /app

# Enable non-free repositories for libfdk-aac-dev
RUN echo "deb http://deb.debian.org/debian bookworm main contrib non-free non-free-firmware" > /etc/apt/sources.list && \
    echo "deb http://deb.debian.org/debian bookworm-updates main contrib non-free non-free-firmware" >> /etc/apt/sources.list && \
    echo "deb http://deb.debian.org/debian-security bookworm-security main contrib non-free non-free-firmware" >> /etc/apt/sources.list

# Install system dependencies including PostgreSQL and Redis
# Based on manage.sh ensure_system_dependencies()
RUN apt-get update && apt-get install -y \
    # Python 3.11 (matching backend requirements)
    python3.11 python3.11-venv python3-pip python3-venv python3.11-dev \
    # Web server (frontend is pre-built on host and copied in)
    nginx \
    curl ca-certificates \
    # Database and cache
    postgresql-15 postgresql-contrib-15 postgresql-client-15 \
    redis-server redis-tools \
    # MakeMKV build dependencies (will download and install via UI updater)
    # Build tools
    build-essential pkg-config cmake yasm nasm \
    # Libraries
    libc6-dev libssl-dev libexpat1-dev libavcodec-dev libgl1-mesa-dev qtbase5-dev zlib1g-dev \
    libfdk-aac-dev libx264-dev liblzma-dev \
    # Media tools
    ffmpeg \
    mkvtoolnix \
    # Java Runtime Environment - required for Blu-ray disc support (see manage.sh)
    default-jre \
    # Network file systems (from manage.sh)
    # samba-client: For anonymous SMB access (smbclient)
    samba-client \
    # cifs-utils: For SMB/CIFS mount support (mount.cifs)
    cifs-utils \
    # nfs-common: For NFS mount support (mount.nfs)
    nfs-common \
    # Process management
    supervisor \
    # Tini - lightweight init system for proper signal handling and zombie reaping
    tini \
    # Udev for automatic disc detection
    udev \
    # sg3-utils provides sg_turs, the media-presence probe core.utils
    # ._drive_has_media() prefers. Without it that check silently degrades to
    # BLKGETSIZE64, which reports a phantom ~1GiB on an empty USB Blu-ray
    # drive — so the app believes a disc is loaded whenever the tray is
    # empty (#766). Not optional despite the code's `if SG_TURS_BIN` guard.
    sg3-utils \
    # Utilities (wget needed for MakeMKV download in entrypoint)
    wget git rsync \
    && rm -rf /var/lib/apt/lists/*

# Configure PostgreSQL
# Create data directory and set permissions
RUN mkdir -p /data/postgres /data/redis /data/mkvauto/data /data/mkvauto/logs /data/mkvauto/tmp && \
    chown -R postgres:postgres /data/postgres && \
    chown -R redis:redis /data/redis && \
    chmod 700 /data/postgres

# Configure Redis to use /data/redis and work with supervisor
RUN sed -i 's|^dir .*|dir /data/redis|' /etc/redis/redis.conf && \
    sed -i 's|^bind .*|bind 127.0.0.1|' /etc/redis/redis.conf && \
    sed -i 's|^daemonize .*|daemonize no|' /etc/redis/redis.conf && \
    sed -i 's|^supervised .*|supervised systemd|' /etc/redis/redis.conf

# Configure PostgreSQL for embedded mode
RUN sed -i "s|#listen_addresses = 'localhost'|listen_addresses = '*'|" /etc/postgresql/15/main/postgresql.conf && \
    sed -i "s|data_directory = '/var/lib/postgresql/15/main'|data_directory = '/data/postgres'|" /etc/postgresql/15/main/postgresql.conf && \
    echo "host all all 0.0.0.0/0 md5" >> /etc/postgresql/15/main/pg_hba.conf

# Copy application files (frontend is pre-built on host; only dist is in context)
COPY Backend/ /app/backend/
COPY Frontend/ /app/frontend/

# Install backend dependencies
WORKDIR /app/backend
RUN python3.11 -m venv /app/venv && \
    /app/venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel && \
    /app/venv/bin/pip install --no-cache-dir -r requirements.txt

# Copy Docker configuration files (paths relative to build context; context is repo root when using compose or scripts/build-docker)
WORKDIR /app
COPY Docker/nginx.conf /etc/nginx/sites-available/mkvauto
COPY Docker/supervisord.conf /etc/supervisor/conf.d/mkvauto.conf
COPY Docker/entrypoint.sh /entrypoint.sh
COPY Docker/healthcheck.sh /healthcheck.sh
# #709: guarded migration (pre-migration backup + halt-on-failure) and restore
COPY Docker/db-migrate.sh /app/db-migrate.sh
COPY Docker/db-restore.sh /app/db-restore.sh

# Generate udev rules from template with Docker-specific paths
COPY Deploy/udev/99-mkva-disc.rules.template /tmp/udev-template.rules
RUN sed -e 's|__MKVAUTO_ROOT__|/data/mkvauto|g' \
        -e 's|__MKVAUTO_TMP_DIR__|/data/mkvauto/tmp|g' \
        -e 's|__PYTHON_BIN__|/app/venv/bin/python3|g' \
        -e 's|__UDEV_NOTIFY_SCRIPT__|/app/backend/drive_manager/udev_notify.py|g' \
        /tmp/udev-template.rules > /etc/udev/rules.d/99-mkva-disc.rules \
    && rm /tmp/udev-template.rules

# Configure NGINX
RUN rm -f /etc/nginx/sites-enabled/default && \
    ln -s /etc/nginx/sites-available/mkvauto /etc/nginx/sites-enabled/mkvauto && \
    chmod +x /entrypoint.sh /healthcheck.sh /app/db-migrate.sh /app/db-restore.sh

# Create supervisor log directory
RUN mkdir -p /var/log/supervisor

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD /healthcheck.sh

# Volumes for persistent data
VOLUME ["/data"]

# Expose HTTP port
EXPOSE 80

# Run entrypoint with tini for proper signal handling and zombie reaping
# Tini ensures defunct processes are reaped and signals are forwarded correctly
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["supervisord"]
