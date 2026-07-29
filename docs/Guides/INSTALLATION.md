# MKV-Auto Installation Guide

Complete installation guide for MKV-Auto - automated optical disc ripping and media management system.

## Table of Contents

- [System Requirements](#system-requirements)
- [Quick Start (Docker)](#quick-start-docker)
- [Detailed Installation](#detailed-installation)
- [Initial Setup](#initial-setup)
- [Configuration](CONFIGURATION.md)
- [Troubleshooting](#troubleshooting)
- [Upgrading](UPGRADE.md)

## System Requirements

### Hardware

- **CPU**: x86_64 / AMD64 or arm64 / aarch64 — the image is multi-arch
- **RAM**: Minimum 4GB, recommended 8GB+
- **Storage**: 200GB+ free space for ripped content (1TB+ recommended for multi-drive systems)
- **Optical Drive**: One or more optical disc drives (DVD/Blu-ray/UHD)

### Software

- **Docker**: Version 20.10+ required
- **Docker Compose**: Version 2.0+ (optional, but recommended)
- **Linux Kernel**: 4.x or later with optical drive support
- **Host OS**: Linux (Ubuntu 22.04+, Debian 11+, Fedora 36+, etc.)

> **Windows and macOS are not supported hosts.** Docker Desktop runs containers
> inside a VM with no access to physical optical drives, so `--device=/dev/sr0`
> has nothing to map. On macOS the documented `docker run` fails outright:
>
> ```
> docker: Error response from daemon: error gathering device information
> while adding custom device "/dev/sr0": not a device node
> ```
>
> Pointing `--device` at the real macOS node (`/dev/disk8` or similar) fails too
> — `no such file or directory` — because the Linux VM cannot see it. Dropping
> `--device` and keeping only `-v /dev/sr0:/dev/sr0` is worse: Docker creates an
> empty *directory* at that path, the container starts, and no drive is ever
> detected.
>
> Run a **Linux VM with USB passthrough** instead — VirtualBox or VMware
> Workstation on Windows, UTM or Parallels or VMware Fusion on macOS — or use a
> Linux box or NAS such as Unraid or TrueNAS. See
> [Windows](VM_SETUP_WINDOWS.md) / [macOS](VM_SETUP_MACOS.md) VM setup for a step-by-step walkthrough.
>
> **Hyper-V will not work.** It has no direct USB passthrough; Enhanced Session
> Mode redirects devices over RDP rather than presenting a real optical device
> to the guest, and Discrete Device Assignment is Windows Server only.
>
> WSL2 with `usbipd-win` is not viable either: the stock WSL2 kernel omits the
> CD-ROM module (`sr_mod`), and even after rebuilding it, MakeMKV's raw SCSI
> commands do not survive USB/IP reliably.

### Network

- **Ports**: Port 80 (or 8080) available for web interface
- **Optional**: Network storage (NFS/SMB) for transfers

## Quick start

If you just want it running, see **[QUICKSTART.md](QUICKSTART.md)** — pull,
run, open the UI. The rest of this guide is the full walkthrough: installing
Docker itself, identifying your drives, and host configuration.

## Detailed Installation

### Step 1: Install Docker

#### Ubuntu/Debian

```bash
# Update package index
sudo apt-get update

# Install prerequisites
sudo apt-get install -y \
    ca-certificates \
    curl \
    gnupg \
    lsb-release

# Add Docker's official GPG key
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg

# Set up the repository
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker Engine
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Add your user to docker group (avoid sudo)
sudo usermod -aG docker $USER
newgrp docker
```

#### Other Distributions

See [Docker's official installation guide](https://docs.docker.com/engine/install/).

### Step 2: Identify Your Optical Drive(s)

```bash
# List optical drives
ls -l /dev/sr*

# Example output:
# /dev/sr0  <- Your first drive
# /dev/sr1  <- Your second drive (if present)
```

### Step 3: Download and Configure

#### Option A: Using Docker Compose (Recommended)

```bash
# Create directory
mkdir -p ~/mkv-auto
cd ~/mkv-auto

# Create docker-compose.yml with the image-based config from
# "Using Docker Compose" above (pulls ghcr.io/mkv-auto/mkv-auto-release)
nano docker-compose.yml

# Start
docker compose up -d
```

#### Option B: Using Docker Run

The single-command form is in [QUICKSTART.md](QUICKSTART.md#1-start-the-container).

### Step 4: Verify Installation

```bash
# Check container status
docker ps -f name=mkv-auto

# View logs
docker logs mkv-auto

# Check health
curl http://localhost:8080/api/system/health
```

## Initial Setup

### First Access

1. **Open your browser** and navigate to:
   ```
   http://localhost:8080
   ```

2. **Complete the setup wizard.** The first step checks whether MakeMKV is installed. If it is not, the wizard offers an in-wizard install using the same backend upgrade/install process (Settings → MakeMKV can also install or update later). Then enter your MakeMKV registration key to finish the step.

3. **Verify system status** in Settings → System

4. **Configure MakeMKV** (if not auto-detected):
   - Go to Settings → MakeMKV
   - Verify MakeMKV installation
   - Add registration key (if you have one)

5. **Configure storage** (optional):
   - Go to Settings → Storage
   - Add transfer configurations for network storage

### Adding Multiple Optical Drives

If you have multiple drives, add them all to the container:

**Docker Compose:**
```yaml
devices:
  - /dev/sr0
  - /dev/sr1
  - /dev/sr2
volumes:
  - /dev/sr0:/dev/sr0
  - /dev/sr1:/dev/sr1
  - /dev/sr2:/dev/sr2
```

**Docker Run:**
```bash
docker run -d \
  --name mkv-auto \
  -v /dev/sr0:/dev/sr0 --device=/dev/sr0 \
  -v /dev/sr1:/dev/sr1 --device=/dev/sr1 \
  -v /dev/sr2:/dev/sr2 --device=/dev/sr2 \
  # ... other options ...
  ghcr.io/mkv-auto/mkv-auto-release:latest
```

## Optical Drive Configuration

MKV-Auto automatically configures optical drives on container startup to prevent discs from auto-reingesting after ejection.

### Automatic Configuration (Default)

The container automatically disables CD-ROM autoclose to prevent host services from interfering with disc ejection. Check if it worked:

```bash
# Check container logs
docker logs mkv-auto | grep "optical"

# Should see: "✅ Disabled CD-ROM autoclose"

# Verify setting
docker exec mkv-auto cat /proc/sys/dev/cdrom/autoclose
# Should show: 0
```

### Manual Setup (If Needed)

If the automatic setup failed (disc still auto-reinserts after ejection), run this ONE-TIME setup on your HOST:

```bash
# Option 1: Use the setup script (recommended)
sudo bash scripts/setup-host-optical.sh

# Option 2: Manual configuration
sudo sh -c 'echo 0 > /proc/sys/dev/cdrom/autoclose'
sudo sh -c 'echo "dev.cdrom.autoclose = 0" >> /etc/sysctl.conf'
```

No container restart required after manual setup.

### Unraid Users

**MKV-Auto is not in Community Apps yet.** Install it as you would any other
container, or add the template shipped in the repo — `Unraid/mkv-auto.xml` — as a
container template by hand. It has the ports, data path, device mapping and
settings pre-filled, so you are not typing them in from scratch.

The template also carries the application settings — MakeMKV key, TMDB key, media
server, Discord, auto-rip and eject behaviour. Filling them in means the container
starts ready to use with no setup wizard; leave a field blank to configure it in
the web UI instead. See
[Unattended setup](CONFIGURATION.md#unattended-setup-application-settings).

Optical drive host configuration is not automatic on Unraid — follow
[Host optical setup](../HOST_OPTICAL_SETUP.md#option-3-unraid-specific).

### Testing

Test that disc ejection works:

1. Insert a disc
2. Press the physical eject button on your drive
3. **Expected**: Disc stays out (doesn't auto-reingest)

If disc still reinserts, run the manual setup above.

## Configuration

Environment variables, volume layout and networking are in
**[CONFIGURATION.md](CONFIGURATION.md)**.

### Skipping the setup wizard

The first time you open the web UI it walks you through a setup wizard. You can
skip it entirely by supplying the same values as environment variables — your
MakeMKV and TMDB keys, media server, Discord webhook and so on — so a fresh
container comes up already configured. That is the usual choice for a Compose
stack, or any deployment you want to rebuild reproducibly.

See [Unattended setup](CONFIGURATION.md#unattended-setup-application-settings)
for the full list. Anything you set that way is re-applied on every start and
shown read-only in the web UI, since a change made there would be reverted on the
next restart.

## Troubleshooting

See **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** — drive not detected, disc
auto-reinserting, container won't start, web UI unreachable, permissions,
and database errors.

## Upgrading

See **[UPGRADE.md](UPGRADE.md)** — pulling a new image, backup, and rollback.

## Getting Help

- **Documentation**: [README.md](../../README.md)
- **Upgrade guide**: [UPGRADE.md](UPGRADE.md)
- **Docker Guide**: [DOCKER.md](DOCKER.md)
- **Architecture**: [README.development.md](../../README.development.md)
- **GitHub Issues**: https://github.com/MKV-Auto/mkv-auto-release/issues
- **Logs**: `docker logs mkv-auto`

## Next Steps

After installation:

1. **Insert a disc** - System auto-detects and scans
2. **Start a rip** - Follow the workflow in the web interface
3. **Configure transfers** - Set up network storage destinations
4. **Explore settings** - Customize to your preferences

Enjoy automated disc ripping with MKV-Auto!
