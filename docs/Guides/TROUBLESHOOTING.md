# MKV-Auto troubleshooting

Short fixes for common problems. If these do not help, see the linked guides or open an issue on the project repository.

## Drive not registering; no spin-up when you plug in the drive or insert a disc

If the app does not show your optical drive and you do **not** hear the drive spin up when you connect it or insert a disc:

1. **Restart the Docker container** (for example, if the container is named `mkv-auto`):

   ```bash
   docker restart mkv-auto
   ```

2. If the drive still does not appear in the app but **the host** still sees it (see checks below), **unplug the drive, plug it back in**, then **restart the container** again.

Confirm the basics:

- On the host, the drive is visible (e.g. `ls -l /dev/sr*` or `lsblk`).
- The container was started with the correct device mapping (`--device`, bind mount) and **privileged** mode, as in the [Installation guide](INSTALLATION.md#optical-drive-not-detected).

For “Start Copy” runs but ripping does not start or logs look empty, see [Troubleshooting: rip / makemkvcon](TROUBLESHOOTING_RIP.md).
