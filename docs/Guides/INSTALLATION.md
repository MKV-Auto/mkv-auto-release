# MKV-Auto Installation Guide

Complete installation guide for MKV-Auto - automated optical disc ripping and media management system.

## Table of Contents

- [System Requirements](#system-requirements)
- [Quick Start (Docker)](#quick-start-docker)
- [Detailed Installation](#detailed-installation)
- [Initial Setup](#initial-setup)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Upgrading](#upgrading)

## System Requirements

### Hardware

- **CPU**: x86_64 / AMD64 architecture (ARM not supported due to MakeMKV)
- **RAM**: Minimum 4GB, recommended 8GB+
- **Storage**: 200GB+ free space for ripped content (1TB+ recommended for multi-drive systems)
- **Optical Drive**: One or more optical disc drives (DVD/Blu-ray/UHD)

### Software

- **Docker**: Version 20.10+ required
- **Docker Compose**: Version 2.0+ (optional, but recommended)
- **Linux Kernel**: 4.x or later with optical drive support
- **Host OS**: Linux (Ubuntu 22.04+, Debian 11+, Fedora 36+, etc.)

### Network

- **Ports**: Port 80 (or 8080) available for web interface
- **Optional**: Network storage (NFS/SMB) for transfers

## Quick Start (Docker)

### Using Docker Run

```bash
# Pull the image
docker pull ghcr.io/mkv-auto/mkv-auto-release:latest

# Run the container
docker run -d \
  --name mkv-auto \
  -p 8080:80 \
  -v mkv-data:/data \
  -v /dev/sr0:/dev/sr0 \
  --device=/dev/sr0 \
  --privileged \
  --restart unless-stopped \
  ghcr.io/mkv-auto/mkv-auto-release:latest

# Access at http://localhost:8080
```

### Using Docker Compose

1. **Create a `docker-compose.yml`** with the published image (the `Docker/docker-compose.yml` in this repo is a *build-from-source* file for developers — don't use it to run the app):

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

2. **Start the container:**

```bash
docker compose up -d
```

3. **Access the web interface:**

```
http://localhost:8080
```

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

If you installed via Unraid Community Apps, optical drives are automatically configured.
No manual setup needed!

### Testing

Test that disc ejection works:

1. Insert a disc
2. Press the physical eject button on your drive
3. **Expected**: Disc stays out (doesn't auto-reingest)

If disc still reinserts, run the manual setup above.

## Configuration

### Environment Variables

Customize the container with environment variables:

```yaml
environment:
  # Database (use external PostgreSQL)
  - DATABASE_URL=postgresql://user:pass@host:5432/discs
  
  # Cache (use external Redis)
  - REDIS_URL=redis://host:6379/0
  
  # Data directories
  - MKVAUTO_ROOT=/data/mkvauto
  - MKVAUTO_DATA=/data/mkvauto/data
  
  # Logging
  - MKVAUTO_DEBUG_LEVEL=INFO  # DEBUG, INFO, WARNING, ERROR
  
  # Timezone
  - TZ=America/New_York
```

### Volume Mapping

Data is stored in the `/data` volume:

```bash
# Use a named volume (recommended)
volumes:
  - mkv-data:/data

# Or bind mount to host directory
volumes:
  - /path/on/host:/data
```

**Data structure:**
- `/data/postgres` - Database files (if embedded)
- `/data/redis` - Cache files (if embedded)
- `/data/mkvauto/data` - Job artifacts and ripped files
- `/data/mkvauto/logs` - Application logs
- `/data/mkvauto/tmp` - Temporary files

### Network Configuration

#### Using Different Port

```bash
# Map container port 80 to host port 3000
docker run -p 3000:80 ... mkv-auto
# Access at http://localhost:3000
```

#### Using HTTPS (Reverse Proxy)

See [DOCKER.md](DOCKER.md#https-setup) for reverse proxy configuration.

## Troubleshooting

### Container Won't Start

```bash
# Check logs
docker logs mkv-auto

# Check disk space
df -h

# Verify Docker is running
docker info
```

### Can't Access Web Interface

```bash
# Check if container is running
docker ps -f name=mkv-auto

# Check port mapping
docker port mkv-auto

# Test from inside container
docker exec mkv-auto curl http://localhost:80
```

### Optical Drive Not Detected

If the drive never spins up when you connect it or insert a disc, try a container restart and USB re-plug first; see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

```bash
# Verify drive exists on host
ls -l /dev/sr*

# Check container has access
docker exec mkv-auto ls -l /dev/sr*

# Verify privileged mode is enabled
docker inspect mkv-auto | grep Privileged
```

### Permission Issues

```bash
# Check volume permissions
docker exec mkv-auto ls -la /data

# Fix permissions if needed
docker exec mkv-auto chown -R postgres:postgres /data/postgres
docker exec mkv-auto chown -R redis:redis /data/redis
```

### Database Connection Errors

```bash
# If using embedded databases, check logs
docker logs mkv-auto | grep -i postgres
docker logs mkv-auto | grep -i redis

# If using external databases, test connectivity
docker exec mkv-auto pg_isready -h <db_host>
docker exec mkv-auto redis-cli -h <redis_host> ping
```

## Upgrading

### Upgrading to New Version

```bash
# Stop and remove old container
docker stop mkv-auto
docker rm mkv-auto

# Pull new image
docker pull ghcr.io/mkv-auto/mkv-auto-release:latest

# Start new container (data persists in volume)
docker-compose up -d
# Or use docker run command
```

### Backup Before Upgrade

For full upgrade steps (pull new image, restart with same volumes, backup, rollback), see [UPGRADE.md](UPGRADE.md).

```bash
# Backup data volume
docker run --rm \
  -v mkv-data:/data \
  -v $(pwd):/backup \
  ubuntu tar czf /backup/mkv-data-backup-$(date +%Y%m%d).tar.gz /data

# Backup database (embedded PostgreSQL — default setup)
docker exec mkv-auto pg_dump -U postgres discs > backup.sql

# Backup database (external PostgreSQL container)
docker exec mkv-auto-postgres pg_dump -U postgres discs > backup.sql
```

### Rollback to Previous Version

```bash
# Stop current container
docker stop mkv-auto
docker rm mkv-auto

# Run previous version
docker run -d \
  --name mkv-auto \
  -p 8080:80 \
  -v mkv-data:/data \
  -v /dev/sr0:/dev/sr0 \
  --device=/dev/sr0 \
  --privileged \
  ghcr.io/mkv-auto/mkv-auto-release:1.0.0  # Specific version
```

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
