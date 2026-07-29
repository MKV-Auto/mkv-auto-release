# MKV-Auto Quick Start

Running in about five minutes.

> **You need Docker on a Linux host** (x86_64 or arm64) with an optical drive
> attached. Docker Desktop on Windows and macOS **cannot** pass an optical drive
> into a container — run a small Linux VM instead, see
> [Windows / macOS setup](VM_SETUP.md).

## 1. Start the container

```bash
docker run -d \
  --name mkv-auto \
  -p 8080:80 \
  -v mkv-data:/data \
  -v /dev/sr0:/dev/sr0 \
  --device=/dev/sr0 \
  --privileged \
  --restart unless-stopped \
  ghcr.io/mkv-auto/mkv-auto-release:latest
```

The image is multi-arch — Docker pulls the right build for your machine.

**More than one drive?** Repeat `-v` and `--device` for each (`/dev/sr1`,
`/dev/sr2`, …). All of them are detected automatically.

**Prefer Compose?** Use the image-based `docker-compose.yml` in
[INSTALLATION.md](INSTALLATION.md#using-docker-compose) — *not* the
build-from-source file in `Docker/`.

**On Unraid?** There is no Community Apps entry yet. Use the `docker run` above,
or add [`Unraid/mkv-auto.xml`](https://github.com/MKV-Auto/mkv-auto-release/blob/main/Unraid/mkv-auto.xml)
as a container template by hand — it has the ports, paths, device mapping and
settings already filled in.

## 2. Check the drive was configured

```bash
docker logs mkv-auto | grep optical
```

Expected:

```
✅ Disabled CD-ROM autoclose (prevents auto-reinsertion)
✅ Optical drives configured correctly (autoclose=0)
```

If you see warnings instead, run this **once on the host**:

```bash
sudo sh -c 'echo 0 > /proc/sys/dev/cdrom/autoclose'
sudo sh -c 'echo "dev.cdrom.autoclose = 0" >> /etc/sysctl.conf'
```

No container restart needed.

## 3. Open the web interface

**http://localhost:8080**

The setup wizard installs MakeMKV and walks you through transfer destinations on
first run.

## 4. Try a disc

Insert a disc, wait for the scan to appear in the UI, then press the physical
eject button. **The disc should stay out** — if it gets pulled back in, see
[Troubleshooting](TROUBLESHOOTING.md#the-disc-auto-reinserts-after-ejecting).

## Next

- **Set your output location** — Settings → Library
- **Set up transfers** (optional) — Settings → Transfer
- **Discord notifications** (optional) — Settings → Discord

## Where to go next

| | |
|---|---|
| [Installation](INSTALLATION.md) | Full walkthrough — installing Docker, host setup, multi-drive, Unraid |
| [Configuration](CONFIGURATION.md) | Environment variables, volumes, networking — including how to [skip the setup wizard](CONFIGURATION.md#unattended-setup-application-settings) by supplying your keys as variables |
| [Windows / macOS](VM_SETUP.md) | Running via a Linux VM |
| [Troubleshooting](TROUBLESHOOTING.md) | When something does not work |
| [Upgrading](UPGRADE.md) | Moving to a new version |
| [Docker reference](DOCKER.md) | Image internals, building from source, production |
