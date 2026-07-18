# Upgrade guidance for container updates

This guide describes how to upgrade the MKV-Auto container to a newer version while preserving your data and configuration.

## Before you upgrade

1. **Back up your data.** See [Backup Before Upgrade](#backup-before-upgrade) below and the [Installation guide](INSTALLATION.md) section "Backup Before Upgrade" for volume and database backup commands.
2. **Check release notes.** New versions are published as [GitHub Releases](https://github.com/MKV-Auto/mkv-auto-release/releases). The README and release notes describe image tags and any migration steps.

## How to upgrade

1. **Pull the new image** (or use your orchestration tool’s update command):
   ```bash
   docker pull ghcr.io/mkv-auto/mkv-auto-release:<tag>
   ```
   Use the tag for the version you want (e.g. `latest`, or a specific version like `1.2.0`).

2. **Stop and remove the current container** (data lives in volumes, not in the container):
   ```bash
   docker stop mkv-auto
   docker rm mkv-auto
   ```

3. **Start a new container** with the same volume mounts and configuration as before, using the new image:
   ```bash
   docker run -d \
     --name mkv-auto \
     -p 8080:80 \
     -v mkv-data:/data \
     -v /dev/sr0:/dev/sr0 \
     --device=/dev/sr0 \
     --privileged \
     ghcr.io/mkv-auto/mkv-auto-release:<tag>
   ```
   If you use `docker compose`, run `docker compose pull` then `docker compose up -d` so the updated image is used with your existing compose file.

4. **Verify** the app and API (e.g. health endpoint, frontend load).

## Backup before upgrade

- **Data volume:** Back up the Docker volume that holds MKV-Auto data (e.g. `mkv-data`) before upgrading. Example:
  ```bash
  docker run --rm -v mkv-data:/data -v $(pwd):/backup ubuntu \
    tar czf /backup/mkv-data-backup-$(date +%Y%m%d).tar.gz /data
  ```
- **Database:** If you use an external PostgreSQL instance, back it up with your usual method (e.g. `pg_dump`). If PostgreSQL runs inside the container, it uses the same data volume; backing up the volume is sufficient.
- **Settings:** Application settings are stored in the database or in the data volume; no separate settings export is required if the volume is backed up.

See [INSTALLATION.md](INSTALLATION.md) for more backup and rollback details.

## Rollback

If you need to revert to a previous version, stop and remove the current container, then start a new container with the previous image tag and the **same** volumes. Your data will be as it was when you last ran that version. See "Rollback to Previous Version" in [INSTALLATION.md](INSTALLATION.md).

## Related documentation

- [Docker guide](DOCKER.md) – Image layout, building, deployment options, and configuration.
- Versioning – Releases follow `MAJOR.MINOR.PATCH` and are published only as tagged releases; `:latest` always points at the newest release.
- [INSTALLATION.md](INSTALLATION.md) – Initial setup, backup, and rollback commands.
- Release repo (mkv-auto-release) – Image tags and run instructions for released builds.
