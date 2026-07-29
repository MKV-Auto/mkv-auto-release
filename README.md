# MKV Auto

[![Image pulls](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FMKV-Auto%2Fmkv-auto-release%2Fbadges%2Fghcr-pulls.json&style=flat-square&logo=docker&logoColor=white)](https://github.com/MKV-Auto/mkv-auto-release/pkgs/container/mkv-auto-release)
[![Release](https://img.shields.io/github/v/release/MKV-Auto/mkv-auto-release?style=flat-square&color=06b6d4)](https://github.com/MKV-Auto/mkv-auto-release/releases/latest)
[![License](https://img.shields.io/github/license/MKV-Auto/mkv-auto-release?style=flat-square&color=ec4899)](LICENSE)

Self-hosted automated disc ripping and media management. Insert a Blu-ray, UHD, or DVD; get organized files on your Plex or Jellyfin share — with disc identification via [TheDiscDB](https://thediscdb.com), title/episode metadata via [TMDB](https://www.themoviedb.org/), and transfers over local or SMB (for NFS or other network storage, mount it into the container and use local).

## How it works

1. **Insert a disc** — The app detects the drive and scans the disc via MakeMKV.
2. **Identify** — The disc is looked up in TheDiscDB. If found, the label step is a review, not data entry, and you can go straight to rip.
3. **Label (if needed)** — For unknown discs, a short workflow lets you pick the movie or series and season. For TV seasons, TMDB fills in per-episode titles and metadata so you're picking from a list, not typing S02E07 by hand. (Optional — bring your own free TMDB v3 API key. Without it, the URL-paste lookup still works.)
4. **Rip** — MakeMKV extracts the selected titles. A dedicated Exploratory Rip flow handles heavily-obfuscated discs (Lions Gate titles, Avatar UHD, etc.) that hide the real movie behind decoy playlists.
5. **Post-process** — Files are renamed and organized for Plex or Jellyfin (your choice in settings).
6. **Transfer** — Send output to a local folder or over SMB or NFS.

All processing runs on your machine. No cloud dependency.

## How to use it

### Run with Docker

**Requirements:** Docker 20.10+ on a **Linux host**, x86_64 or arm64, and an optical drive. Privileged mode is required for drive access. Docker Desktop on Windows and macOS cannot pass an optical drive through — see [Requirements](#requirements).

```bash
docker pull ghcr.io/mkv-auto/mkv-auto-release:latest

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

Open **http://localhost:8080** (or your host IP). On first run, the setup wizard will guide you through MakeMKV, transfer destinations, and optional Discord notifications.

### Persistent data

Mount `/data` so the app can store:

- Database and cache (PostgreSQL, Redis)
- Job data and ripped output
- Logs and settings

Example: `-v mkv-data:/data` (as above).

### Docker Compose

Create a `docker-compose.yml` with the published image and start it:

```yaml
services:
  mkv-auto:
    image: ghcr.io/mkv-auto/mkv-auto-release:latest
    container_name: mkv-auto
    ports:
      - "8080:80"
    volumes:
      - mkv-data:/data
    devices:
      - /dev/sr0:/dev/sr0   # change to /dev/sr1, etc. if needed
    privileged: true
    restart: unless-stopped

volumes:
  mkv-data:
```

```bash
docker compose up -d
```

Note: the [Docker/docker-compose.yml](Docker/docker-compose.yml) checked into this repo is a **build-from-source** file for development — it does not pull the published image. See [docs/Guides/DOCKER.md](docs/Guides/DOCKER.md) for building from source.

## Documentation

| Guide | Description |
|-------|-------------|
| [Installation](docs/Guides/INSTALLATION.md) | Detailed install steps, host setup, optical drive configuration |
| [Docker](docs/Guides/DOCKER.md) | Image details, Compose, and deployment |
| [Quick start](docs/Guides/QUICKSTART.md) | Minimal steps to get running |
| [Troubleshooting](docs/Guides/TROUBLESHOOTING.md) | Common fixes (drives, containers) before deeper guides |
| [Windows / macOS setup](docs/Guides/VM_SETUP.md) | Why a Linux VM is needed, and how to set one up |
| [Development & architecture](README.development.md) | Architecture overview, single-container and privileged-helper rationale, snapshot model, design principles |
| [Changelog](CHANGELOG.md) | Version history and changes |

## Requirements

- **Docker** 20.10+
- **Architecture** x86_64/AMD64 or **arm64/aarch64** — the image is multi-arch,
  so Docker pulls the right one automatically (Raspberry Pi 5, ARM NAS, Apple
  Silicon VMs)
- **Linux host** — see below
- **Privileged** container (raw SCSI access to the drive)
- **Optical drive** DVD, Blu-ray, or UHD
- **RAM** 4 GB minimum, 8 GB recommended
- **Storage** 200 GB+ for ripped content, 1 TB+ for multiple drive systems

### Why a Linux host

The container reads your drive directly (`--device=/dev/sr0`), and MakeMKV
issues raw SCSI commands to it for disc structure and decryption. That only
works when the machine running Docker is the machine holding the drive.

**Docker Desktop on Windows and macOS runs containers inside a VM that cannot
see physical optical drives.** On macOS the `docker run` above fails outright
with `error gathering device information while adding custom device
"/dev/sr0": not a device node`, and pointing `--device` at the real macOS
device fails too because the Linux VM cannot see it. This is not a
configuration problem, and there is no flag that fixes it.

On Windows or Mac hardware, run Linux as the host for the drive instead:

- A **Linux VM with USB passthrough** — see the step-by-step guides for
  [Windows](docs/Guides/VM_SETUP_WINDOWS.md) and
  [macOS](docs/Guides/VM_SETUP_MACOS.md). **Not Hyper-V**: it has no
  direct USB passthrough, and its alternatives (RDP redirection, Discrete Device
  Assignment) either do not present a real optical device to the guest or are
  Windows Server only.
- **Unraid**, **TrueNAS**, or any Linux box or NAS
- **Dual-boot** into Linux

Attaching the drive to WSL2 with `usbipd-win` is not a supported path: the
stock WSL2 kernel omits the CD-ROM module (`sr_mod`), and even after rebuilding
it, MakeMKV's SCSI passthrough is unreliable over USB/IP.

## Releases

Images are published to GitHub Container Registry:

- **Latest:** `ghcr.io/mkv-auto/mkv-auto-release:latest`
- **Versioned:** `ghcr.io/mkv-auto/mkv-auto-release:1.0.0` (see [Releases](https://github.com/MKV-Auto/mkv-auto-release/releases))

## Links

- **TheDiscDB** — [thediscdb.com](https://thediscdb.com) (disc identification database)
- **TMDB** — [themoviedb.org](https://www.themoviedb.org/) (title & episode metadata; product uses the TMDB API but is not endorsed or certified by TMDB)
- **MakeMKV** — [makemkv.com](https://www.makemkv.com/) (ripping engine)
- **Issues** — [GitHub Issues](https://github.com/MKV-Auto/mkv-auto-release/issues)

## License

Copyright © 2026 Brandon Phillips. MKV-Auto is free software, licensed under the **GNU Affero General Public License v3.0** ([AGPL-3.0-only](LICENSE)): you may use, modify, and redistribute it under the terms of that license, and derivatives — including hosted/networked versions — must remain open under the same terms. It is distributed WITHOUT ANY WARRANTY; see the license for details.

The **MKV-Auto name and logo are not licensed** — please don't use them for modified versions or in ways that imply endorsement without permission.

---

*Self-hosted. Free and open source. For the disc preservation community.*
