# Upgrade guidance for container updates

This guide describes how to upgrade the MKV-Auto container to a newer version while preserving your data and configuration.

## Before you upgrade

1. **Back up your data.** See [Backup before upgrade](#backup-before-upgrade) below.
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

3. **Start a new container** using the same flags you originally used — the
   full invocation is in [QUICKSTART.md](QUICKSTART.md). Your data lives in the
   volume, so reusing the same `-v` mounts preserves everything.

   If you use `docker compose`, run `docker compose pull` then
   `docker compose up -d` and your existing compose file is reused as-is.

4. **Verify** the app and API (e.g. health endpoint, frontend load).

## Backup before upgrade

- **Data volume:** Back up the Docker volume that holds MKV-Auto data (e.g. `mkv-data`) before upgrading. Example:
  ```bash
  docker run --rm -v mkv-data:/data -v $(pwd):/backup ubuntu \
    tar czf /backup/mkv-data-backup-$(date +%Y%m%d).tar.gz /data
  ```
- **Database:** With the default embedded PostgreSQL, the database lives in the
  same data volume — backing up the volume is sufficient. If you want a SQL dump
  as well, or you run an external PostgreSQL:
  ```bash
  docker exec mkv-auto pg_dump -U postgres discs > backup.sql          # embedded
  docker exec mkv-auto-postgres pg_dump -U postgres discs > backup.sql  # external
  ```
- **Settings:** Application settings are stored in the database or in the data volume; no separate settings export is required if the volume is backed up.

## Rollback

If you need to revert to a previous version, stop and remove the current container, then start a new container with the previous image tag and the **same** volumes. Your data will be as it was when you last ran that version.

## Related documentation

- [Docker guide](DOCKER.md) – Image layout, building, deployment options.
- [CONFIGURATION.md](CONFIGURATION.md) – Environment variables, volumes, networking.
- Versioning – Releases follow `MAJOR.MINOR.PATCH` and are published only as tagged releases; `:latest` always points at the newest release.
- [INSTALLATION.md](INSTALLATION.md) – Initial setup and host configuration.
- Release repo (mkv-auto-release) – Image tags and run instructions for released builds.
