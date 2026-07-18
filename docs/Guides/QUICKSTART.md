# MKV-Auto Quick Start Guide

## Get Running in 5 Minutes

### Step 1: Start the Container

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

(Prefer Compose? Use the image-based `docker-compose.yml` from [INSTALLATION.md](INSTALLATION.md#using-docker-compose) — not the build-from-source file in `Docker/`.)

**That's it!** The container automatically configures everything needed.

### Step 2: Verify Setup

Check that optical drives are configured:

```bash
docker logs mkv-auto | grep "optical"
```

**You should see**:
```
✅ Disabled CD-ROM autoclose (prevents auto-reinsertion)
✅ Optical drives configured correctly (autoclose=0)
```

**If you see warnings**, see [Manual Setup](#manual-setup-optional) below.

### Step 3: Access the Web Interface

Open your browser: **http://localhost:8080**

### Step 4: Test Disc Ejection

1. Insert a disc
2. Wait for it to scan (shows in UI)
3. Press the physical eject button on your drive
4. **Disc should stay out!** (no auto-reinsertion)

**If disc reinserts**, see [Troubleshooting](#troubleshooting) below.

## Manual Setup (Optional)

Only needed if automatic configuration failed.

### Quick Fix

Run this ONE TIME on your HOST:

```bash
sudo bash scripts/setup-host-optical.sh
```

### Manual Commands

```bash
# Disable autoclose immediately
sudo sh -c 'echo 0 > /proc/sys/dev/cdrom/autoclose'

# Make permanent (survives reboot)
sudo sh -c 'echo "dev.cdrom.autoclose = 0" >> /etc/sysctl.conf'

# Verify
cat /proc/sys/dev/cdrom/autoclose
# Should show: 0
```

No container restart needed!

## Troubleshooting

### Disc Auto-Reinserts After Ejection

**Quick Test**:
```bash
# Check the setting
docker exec mkv-auto cat /proc/sys/dev/cdrom/autoclose
```

**If it shows 1** (bad):
```bash
# Run manual setup
sudo bash scripts/setup-host-optical.sh
```

**If it shows 0** (good) but disc still reinserts:
```bash
# Check if cooldown is working
docker exec mkv-auto tail -f /data/mkvauto/logs/api.log | grep -iE "cooldown|spurious"

# Press eject button
# Should see: "Ignoring spurious insert"
```

**If still having issues**:
- See [HOST_OPTICAL_SETUP.md](../HOST_OPTICAL_SETUP.md) for detailed diagnostics
- Run `scripts/compare-udev-events.sh` to compare host vs container events

### Container Won't Start

```bash
# Check logs
docker logs mkv-auto

# Check status
docker ps -a | grep mkv-auto
```

### Drive Not Detected

```bash
# List drives
docker exec mkv-auto ls -la /dev/sr*

# Check supervisor status
docker exec mkv-auto supervisorctl status
```

### Port Already in Use

```bash
# Change port in docker-compose.yml
ports:
  - "8081:80"  # Use port 8081 instead
```

## Multiple Drives

Have multiple optical drives? Add them all:

```bash
docker run -d \
  --name mkv-auto \
  -p 8080:80 \
  -v /dev/sr0:/dev/sr0 --device=/dev/sr0 \
  -v /dev/sr1:/dev/sr1 --device=/dev/sr1 \
  -v /dev/sr2:/dev/sr2 --device=/dev/sr2 \
  --privileged \
  ghcr.io/mkv-auto/mkv-auto-release:latest
```

Or in docker-compose.yml:

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

All drives are auto-detected!

## Unraid Installation

1. Search for "MKV-Auto" in Community Apps
2. Click Install
3. Configure optical drive mappings (e.g., /dev/sr0)
4. Click Apply

Optical drives are automatically configured via the template.

## What Gets Auto-Configured

MKV-Auto automatically sets up:

1. **Optical drive behavior** - Disables autoclose to prevent reinsertion
2. **Database** - Embedded PostgreSQL
3. **Cache** - Embedded Redis
4. **Udev rules** - Automatic disc detection
5. **Web interface** - Served on port 80 (mapped to 8080)

No manual configuration needed for basic operation!

## Next Steps

Once running:

1. **Configure output directory** - Settings → Library
2. **Set up transfers** (optional) - Settings → Transfer
3. **Configure Discord** (optional) - Settings → Discord
4. **Insert a disc** - Auto-detected and scanned!

## Getting Help

- **Installation issues**: See [INSTALLATION.md](INSTALLATION.md)
- **Optical drive issues**: See [HOST_OPTICAL_SETUP.md](../HOST_OPTICAL_SETUP.md)
- **Docker issues**: See [DOCKER.md](DOCKER.md)
- **General questions**: Open an issue on GitHub

## Full Documentation

- [INSTALLATION.md](INSTALLATION.md) - Complete installation guide
- [DOCKER.md](DOCKER.md) - Docker deployment details
- [HOST_OPTICAL_SETUP.md](../HOST_OPTICAL_SETUP.md) - Optical drive configuration
- [README.development.md](../../README.development.md) - Architecture overview and design rationale

---

**Ready to rip!** 🎬
