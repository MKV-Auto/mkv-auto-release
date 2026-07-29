# MKV-Auto troubleshooting

Fixes for common problems, organised by symptom. If nothing here helps, open an
issue on the [project repository](https://github.com/MKV-Auto/mkv-auto-release/issues).

Ripping specifically — Start Copy runs but nothing happens, or the logs look
empty — is covered separately in
[Troubleshooting: rip / makemkvcon](TROUBLESHOOTING_RIP.md).

## Start here: logs

Most problems are visible in the logs, and every answer below assumes you can
read them.

```bash
docker logs mkv-auto              # everything
docker logs -f mkv-auto           # follow live
docker logs --tail 100 mkv-auto   # recent only

# per-service, inside the container
docker exec mkv-auto cat /var/log/supervisor/uvicorn.log
docker exec mkv-auto cat /var/log/supervisor/postgresql.log
docker exec mkv-auto supervisorctl status
```

For more detail, restart with `MKVAUTO_DEBUG_LEVEL=DEBUG` (see
[CONFIGURATION.md](CONFIGURATION.md)).

## The drive is not detected

The most common problem, and usually one of three things.

**1. The drive never spins up** when you connect it or insert a disc:

```bash
docker restart mkv-auto
```

If it still does not appear but the host can see it, unplug the drive, plug it
back in, and restart the container again.

**2. The host cannot see it either** — that is a host or hardware problem, not
an MKV-Auto one:

```bash
ls -l /dev/sr*      # on the HOST
lsblk
```

**3. The container was not given the device.** It needs both the device mapping
and privileged mode:

```bash
docker exec mkv-auto ls -l /dev/sr*        # visible inside?
docker inspect mkv-auto | grep Privileged  # must be true
```

If `/dev/sr*` exists on the host but not in the container, the container was
started without `--device`. See [QUICKSTART.md](QUICKSTART.md) for the correct
invocation.

> **Running in a VM?** Your drive is often **not** `/dev/sr0` — VM software adds
> its own virtual CD device. Check `/dev/sr1`. See the
> [VM setup guides](VM_SETUP.md).

## The disc auto-reinserts after ejecting

The host is re-closing the tray. MKV-Auto disables CD-ROM autoclose at startup;
if that did not take, set it on the **host**:

```bash
sudo sh -c 'echo 0 > /proc/sys/dev/cdrom/autoclose'
sudo sh -c 'echo "dev.cdrom.autoclose = 0" >> /etc/sysctl.conf'
```

Verify:

```bash
docker exec mkv-auto cat /proc/sys/dev/cdrom/autoclose   # should be 0
docker logs mkv-auto | grep optical                      # "Disabled CD-ROM autoclose"
```

No container restart is needed after setting it manually. On Unraid, make it
survive a reboot with the boot-config method in
[Host optical setup](../HOST_OPTICAL_SETUP.md#option-3-unraid-specific).

## The container will not start

```bash
docker logs mkv-auto          # why it exited
docker ps -a | grep mkv-auto  # exit code
df -h                         # out of disk?
docker info                   # is Docker itself healthy?
```

## I cannot reach the web interface

```bash
docker ps -f name=mkv-auto                      # running?
docker port mkv-auto                            # what is it mapped to?
docker exec mkv-auto curl http://localhost:80   # does it serve internally?
```

If it serves internally but not from your browser, the port mapping is the
problem. **Port already in use** — map a different one:

```yaml
ports:
  - "8081:80"    # then browse to http://localhost:8081
```

## Permission errors

```bash
docker exec mkv-auto ls -la /data

docker exec mkv-auto chown -R postgres:postgres /data/postgres
docker exec mkv-auto chown -R redis:redis /data/redis
```

## Database or cache connection errors

Embedded (the default):

```bash
docker exec mkv-auto pg_isready
docker exec mkv-auto redis-cli ping
docker exec mkv-auto cat /var/log/supervisor/postgresql_err.log
```

External Postgres/Redis:

```bash
docker exec mkv-auto pg_isready -h <db_host>
docker exec mkv-auto redis-cli -h <redis_host> ping
```

External hosts are configured via `DATABASE_URL` / `REDIS_URL` — see
[CONFIGURATION.md](CONFIGURATION.md). Setting either disables the corresponding
embedded service.

## Restarting one service

You rarely need to restart the whole container:

```bash
docker exec mkv-auto supervisorctl status
docker exec mkv-auto supervisorctl restart uvicorn
```

## Still stuck

Open an issue with:

- what you did and what happened
- `docker logs mkv-auto` output around the failure
- `docker exec mkv-auto supervisorctl status`
- your host OS, and whether you are running in a VM
