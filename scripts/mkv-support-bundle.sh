#!/usr/bin/env bash
# MKV-Auto support triage + log bundle.
#
#   ./mkv-support-bundle.sh                 # print a diagnosis
#   ./mkv-support-bundle.sh --bundle        # also write a .tar.gz to send us
#   ./mkv-support-bundle.sh --bundle ~/out  # ...in a specific directory
#
# --bundle writes to the current directory on the host, or to /data (the
# mounted volume) when run inside the container, since that is the one place
# you can retrieve it from.
#   ./mkv-support-bundle.sh -c my-container # non-default container name
#
# Runs either on the Docker HOST or INSIDE the container, and works out which
# on its own. On the host it can see more (the Docker engine's posture), so
# prefer that when you have the choice.
#
# Read-only. Nothing is changed, no service is restarted.
#
# Why a script rather than a list of commands to paste: `docker exec <c> ls
# /dev/sg*` silently lies. With no shell inside the container the glob is
# expanded by your HOST shell, so the output describes the host while looking
# like it describes the container. That mistake sent a real investigation down
# the wrong path for an hour.

set -u

usage() {
  cat <<'__HELP__'
mkv-support-bundle.sh — diagnose MKV-Auto optical drive problems, and collect
a support bundle when the diagnosis is not enough.

USAGE
  mkv-support-bundle.sh [--bundle [DIR]] [-c NAME]

OPTIONS
  --bundle [DIR]     Also write a redacted .tar.gz you can send to support.
                     DIR is optional; see WHERE THE BUNDLE GOES below.
  --no-makemkv       Skip the `makemkvcon info disc:9999` probe. That probe
                     takes MakeMKV's drive lock, so skip it while a rip is
                     running or the rip will stall waiting for the drive.
                     Everything else is still collected.

                     This script cannot detect a rip on its own — only the
                     backend can see job state, which is why the web UI
                     disables its button during one. From a shell it is on
                     you to know, so the script warns before it scans.
  -c, --container    Container name (default: mkv-auto).
  -h, --help         This message.

WHERE IT RUNS
  Either on the Docker HOST or INSIDE the container; it works out which on
  its own. Prefer the host when you have the choice — only there can it see
  the Docker engine's own configuration, which is one of the possible causes.
  It is read-only: nothing is changed and no service is restarted.

WHERE THE BUNDLE GOES
  On the host        the current directory, or DIR if you give one.
  In the container   /data, the mounted volume — the one directory you can
                     reach from the host afterwards. The script prints the
                     exact `docker cp` command to pull it out, and warns you
                     if you pick a directory that is NOT on /data, because
                     that file cannot be retrieved and is lost when the
                     container is replaced.
  The final path is printed in full. If the directory is not writable the
  script says so immediately rather than after collecting everything.

WHAT IT CHECKS
  Whether the kernel provides SCSI generic (/dev/sg*) at all — MakeMKV finds
  optical drives through those, not through /dev/sr* — and, separately,
  whether the container actually received them. Those are different failures
  with opposite fixes. It also checks whether privileged mode really took
  effect, and identifies engines that cannot pass devices through at all
  (Docker Desktop, snap-packaged Docker, rootless Docker).

  It prints a VERDICT naming the cause and what to do about it, or says
  plainly that it cannot tell. If the container is not running it says so
  instead of guessing: most of these questions can only be answered from
  inside it.

WHAT IS IN THE BUNDLE
  verdict.txt        the diagnosis, as printed
  environment.txt    host and container drive/engine summary
  scsi-topology.txt  /proc/scsi/scsi
  device-nodes.txt   /dev/sr* and /dev/sg* with permissions
  dev-listing.txt    every node in the container's /dev
  host-modules.txt   sg / sr_mod / cdrom / usb_storage load state
  makemkvcon-*.txt   raw robot output of `info disc:9999`
  logs/              the 12 newest logs, capped at 2 MB each
  docker-*.{txt,json} engine posture and container config (host runs only)

  Logs are read from inside the container when it is running, and from the
  /data volume on the host when it is not — so a container that will not
  start still produces a usable bundle.

PRIVACY
  Passwords, API keys, tokens and URL credentials are replaced with
  ***REDACTED*** before the archive is created. It is a plain .tar.gz — open
  it and check before sending if you want to be sure.

MORE
  docs/HOST_OPTICAL_SETUP.md — the full walkthrough this script automates.
__HELP__
}

CONTAINER=mkv-auto
BUNDLE=no
OUTDIR=
SKIP_MAKEMKV=no

while [ $# -gt 0 ]; do
  case "$1" in
    --bundle) BUNDLE=yes; case "${2:-}" in -*|"") ;; *) OUTDIR="$2"; shift ;; esac ;;
    -c|--container) CONTAINER="${2:-mkv-auto}"; shift ;;
    --no-makemkv) SKIP_MAKEMKV=yes ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '%s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# ── where are we? ─────────────────────────────────────────────────────────────
# /.dockerenv is created by Docker; /data/mkvauto is our own layout. Either is
# enough, and neither exists on a host.
if [ -f /.dockerenv ] || [ -d /data/mkvauto ]; then
  MODE=container
elif have docker && docker inspect "$CONTAINER" >/dev/null 2>&1; then
  MODE=host
elif have docker; then
  say "Docker is installed but no container named '$CONTAINER' exists."
  say "Pass the right name with -c, or run this inside the container."
  exit 1
else
  say "Not inside the container, and no docker command found."
  say "Run this on the Docker host, or inside the container."
  exit 1
fi

# ── two kinds of fact, gathered two different ways ───────────────────────────
#
# HOST facts describe the machine and its kernel. /proc/modules, /sys/block,
# /sys/class/scsi_generic and /proc/scsi/scsi all reflect the HOST even when
# read from inside a container — a container shares the host kernel. So these
# are always read LOCALLY, from whichever side we are on. Routing them through
# `docker exec` (as an earlier version did) added a pointless dependency on the
# container being up, and made a stopped container look exactly like a host
# with no sg support.
#
# CONTAINER facts are the "what did this container actually receive" questions.
# Those genuinely have to run inside it, and are simply unavailable when it is
# not running — in which case we say so rather than guess.

hostf()  { cat "$1" 2>/dev/null; }
hostls() { ls "$1" 2>/dev/null; }

host_modules()      { hostf /proc/modules; }
host_scsi_generic() { hostls /sys/class/scsi_generic; }
host_optical()      { hostls /sys/block | grep '^sr' 2>/dev/null; }
host_scsi_topology(){ hostf /proc/scsi/scsi; }

# Is the container up? docker inspect succeeds on a stopped container, so this
# has to be asked separately or every container fact silently comes back empty.
CONTAINER_RUNNING=na
if [ "$MODE" = container ]; then
  CONTAINER_RUNNING=yes
else
  [ "$(docker inspect "$CONTAINER" --format '{{.State.Running}}' 2>/dev/null)" = true ] \
    && CONTAINER_RUNNING=yes || CONTAINER_RUNNING=no
fi

# cin: run a command INSIDE the container. The `sh -c` matters — without it
# there is no shell in the container, so your HOST shell expands the glob and
# the answer describes the host while looking like it describes the container.
cin() {
  [ "$CONTAINER_RUNNING" = yes ] || return 1
  if [ "$MODE" = container ]; then sh -c "$1" 2>/dev/null
  else docker exec "$CONTAINER" sh -c "$1" 2>/dev/null
  fi
}

LOGDIR=/data/mkvauto/logs

# Where the logs live on the host, so a stopped container can still be
# diagnosed. Resolves the mount backing /data whether it is a named volume or a
# bind mount.
host_logdir() {
  [ "$MODE" = host ] || return 1
  src=$(docker inspect "$CONTAINER" \
        --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' 2>/dev/null)
  [ -n "$src" ] && [ -d "$src/mkvauto/logs" ] && printf '%s' "$src/mkvauto/logs"
}

# ── collect ──────────────────────────────────────────────────────────────────
# "Is the sg module loaded" is the wrong question on a kernel that builds SCSI
# generic in (CONFIG_CHR_DEV_SG=y): lsmod will never list it, `modprobe sg` is a
# silent no-op, and it is working fine. A reporter chased that for a while
# because this line said NOT LOADED. What matters is whether the kernel exposes
# SCSI generic devices at all, which /sys/class/scsi_generic answers however
# support was compiled.
SG_MODULE=no
host_modules | grep -qE '^sg ' && SG_MODULE=yes
SG_BUILTIN=unknown
for cfg in "/boot/config-$(uname -r 2>/dev/null)" /proc/config.gz; do
  case "$cfg" in
    *.gz) v=$(zcat "$cfg" 2>/dev/null | grep -h '^CONFIG_CHR_DEV_SG=' 2>/dev/null) ;;
    *)    v=$(grep -h '^CONFIG_CHR_DEV_SG=' "$cfg" 2>/dev/null) ;;
  esac
  case "$v" in *=y) SG_BUILTIN=yes; break ;; *=m) SG_BUILTIN=no; break ;; esac
done
HOST_SG=$(host_scsi_generic | tr '\n' ' ')
HOST_SR=$(host_optical | tr '\n' ' ')
if [ "$SG_MODULE" = yes ] || [ -n "$HOST_SG" ]; then SG_LOADED=yes; else SG_LOADED=no; fi

# Container-side: what did it actually receive?
CSR=$(cin 'ls -1 /dev/sr* 2>/dev/null' | tr '\n' ' ')
CSG=$(cin 'ls -1 /dev/sg* 2>/dev/null' | tr '\n' ' ')
DEVCOUNT=$(cin 'ls /dev 2>/dev/null | wc -l' | tr -d ' ')
HOSTDISK=$(cin 'ls -d /dev/sd? /dev/nvme?n? 2>/dev/null | head -1')

PRIV=unknown; DOCKER_BIN=; DOCKER_ROOT=; IS_SNAP=unknown; IS_ROOTLESS=unknown
IS_DESKTOP=unknown; DOCKER_CTX=
if [ "$MODE" = host ]; then
  PRIV=$(docker inspect "$CONTAINER" --format '{{.HostConfig.Privileged}}' 2>/dev/null)
  DOCKER_BIN=$(command -v docker)
  DOCKER_ROOT=$(docker info 2>/dev/null | sed -n 's/^ *Docker Root Dir: *//p')
  IS_SNAP=no; case "$DOCKER_BIN" in /snap/*) IS_SNAP=yes ;; esac
  if have snap && snap list docker >/dev/null 2>&1; then IS_SNAP=yes; fi
  IS_ROOTLESS=no
  docker info 2>/dev/null | grep -qi rootless && IS_ROOTLESS=yes
  case "$DOCKER_ROOT" in /home/*|/root/.local/*) IS_ROOTLESS=yes ;; esac
  # Docker Desktop runs the engine in its own VM. That VM has its own kernel and
  # its own /dev, so it never has the host's optical drives — --device and
  # privileged are honoured faithfully against a machine with nothing to give.
  IS_DESKTOP=no
  DOCKER_CTX=$(docker context show 2>/dev/null)
  case "$DOCKER_CTX" in desktop-*|*desktop*) IS_DESKTOP=yes ;; esac
  case "${DOCKER_HOST:-}" in *docker/desktop*) IS_DESKTOP=yes ;; esac
  docker info 2>/dev/null | grep -qi 'Operating System: Docker Desktop' && IS_DESKTOP=yes
fi

# This probe takes MakeMKV's drive lock. Running it mid-rip makes the rip block
# on the drive, so callers that might collide (the web UI while a job is
# active) pass --no-makemkv and lose only this one signal.
if [ "$SKIP_MAKEMKV" = yes ]; then
  MK=''
else
  # This script has no way to know whether a rip is running — only the backend
  # can see job state, which is why the web UI disables its button during one.
  # From a shell all we can do is say so before taking the lock.
  say "NOTE: about to scan drives with MakeMKV. If a rip is in progress this"
  say "      can stall it — Ctrl-C now and re-run with --no-makemkv if so."
  say ""

  MK=$(cin 'command -v makemkvcon >/dev/null 2>&1 && timeout 120 makemkvcon -r --cache=1 info disc:9999 2>&1')
fi

# A real drive is any DRV line with a non-empty drive_hardware_name. Keying on
# the name rather than the flags field is what makes this correct: flags is
# tray state (0 closed+empty, 1 open, 2 disc loaded, 3 loading, 256 unused
# slot), so matching flags=2 counts *discs* and hides every empty drive.
drv_lines() { printf '%s' "$MK" | grep -E '^DRV:[0-9]+,[0-9]+,[0-9]+,[0-9]+,"[^"]'; }
DRVCOUNT=$(drv_lines | grep -c . || true)

if [ "$SKIP_MAKEMKV" = yes ]; then                MKSTATE=skipped
elif [ "$CONTAINER_RUNNING" != yes ]; then       MKSTATE=notrunning
elif [ -z "$MK" ]; then                          MKSTATE=notinstalled
elif printf '%s' "$MK" | grep -q 'MSG:5042'; then MKSTATE=nodrives
elif [ "${DRVCOUNT:-0}" -eq 0 ]; then            MKSTATE=nodrives
else                                             MKSTATE=ok
fi

# privileged is only a *request*; Docker fills the container's /dev (a tmpfs) by
# enumerating host devices at start. A working privileged container receives the
# host's whole device set — ~180 nodes including its disks. One where that
# enumeration is blocked reports privileged=true and gets ~17.
PRIV_EFFECTIVE=yes
[ "$PRIV" = true ] && [ "$CONTAINER_RUNNING" = yes ] && [ -z "$HOSTDISK" ] && PRIV_EFFECTIVE=no

# ── report ───────────────────────────────────────────────────────────────────
report() {
  say "=== MKV-Auto support triage ==="
  say "  collected from : $MODE"
  say ""
  say "HOST (as seen from here)"
  if [ "$SG_MODULE" = yes ]; then
    say "  SCSI generic (sg)     : available (module loaded)"
  elif [ "$SG_LOADED" = yes ]; then
    say "  SCSI generic (sg)     : available (built into the kernel — lsmod will"
    say "                          never show it and modprobe is a no-op)"
  elif [ "$SG_BUILTIN" = yes ]; then
    say "  SCSI generic (sg)     : built in, but the kernel lists no sg devices"
  else
    say "  SCSI generic (sg)     : NOT AVAILABLE (module not loaded)"
  fi
  say "  SCSI generic devices  : ${HOST_SG:-none}"
  say "  optical drives        : ${HOST_SR:-none}   (from /sys/block)"
  say ""
  if [ "$CONTAINER_RUNNING" = no ]; then
    say "CONTAINER ($CONTAINER) — NOT RUNNING"
    say "  Container-side facts are unavailable: everything below has to run"
    say "  inside it. Start it and re-run for a full picture."
    say "  privileged (requested): $PRIV"
    say "  docker binary         : ${DOCKER_BIN:-?}"
    say "  docker root dir       : ${DOCKER_ROOT:-?}"
    say "  snap-packaged         : $IS_SNAP"
    say "  rootless              : $IS_ROOTLESS"
    say "  docker context        : ${DOCKER_CTX:-?}"
    say "  Docker Desktop        : $IS_DESKTOP"
  else
  say "CONTAINER ($CONTAINER)"
  say "  /dev/sr* nodes        : ${CSR:-none}"
  say "  /dev/sg* nodes        : ${CSG:-none}"
  say "  /dev node count       : ${DEVCOUNT:-?}   (working privileged container: ~180+)"
  say "  host disks visible    : ${HOSTDISK:-NONE}   (present = privileged really took effect)"
  if [ "$MODE" = host ]; then
    say "  privileged (requested): $PRIV"
    say "  docker binary         : ${DOCKER_BIN:-?}"
    say "  docker root dir       : ${DOCKER_ROOT:-?}"
    say "  snap-packaged         : $IS_SNAP"
    say "  rootless              : $IS_ROOTLESS"
    say "  docker context        : ${DOCKER_CTX:-?}"
    say "  Docker Desktop        : $IS_DESKTOP"
  else
    say "  (run on the host for Docker engine details — snap/rootless detection)"
  fi
  fi
  case "$MKSTATE" in
    notrunning)   : ;;
    skipped)      say "  makemkvcon            : skipped (--no-makemkv; a rip may be in progress)" ;;
    notinstalled) say "  makemkvcon            : not installed yet (finish MakeMKV setup in the UI)" ;;
    nodrives)     say "  makemkvcon            : MSG:5042 — finds NO usable optical drives" ;;
    ok)           say "  makemkvcon            : $DRVCOUNT drive(s) found"
                  # flags: 0 = empty tray, 2 = disc loaded. Both are detected
                  # drives; say which so "no disc" is not read as "no drive".
                  drv_lines | while IFS= read -r l; do
                    f=$(printf '%s' "$l" | cut -d, -f2)
                    case "$f" in
                      2) d="disc loaded" ;;
                      0) d="empty tray" ;;
                      1) d="TRAY OPEN — close it and insert a disc" ;;
                      3) d="loading" ;;
                      *) d="flags=$f" ;;
                    esac
                    say "      [$d] $(printf '%s' "$l" | cut -d, -f5-)"
                  done ;;
  esac
  say ""
  say "VERDICT"
  if [ "$MKSTATE" = skipped ]; then
    say "  No drive verdict — the MakeMKV probe was skipped so an in-progress"
    say "  rip would not stall on the drive lock. Host and container device"
    say "  facts above are complete; re-run without --no-makemkv when idle."
  elif [ "$MKSTATE" = notrunning ]; then
    say "  >> CONTAINER IS NOT RUNNING — no drive verdict possible."
    say "     Whether the container can see your drives can only be answered"
    say "     from inside it. Host-side facts above were still collected."
    say ""
    if [ "$IS_DESKTOP" = yes ]; then
      say "     Note: this engine is Docker Desktop, which runs in its own VM and"
      say "     cannot reach your optical drives at all. Containers created under"
      say "     it also do not exist for a native engine, which is why"
      say "     'docker start' may be failing. See #802."
    else
      say "       docker start $CONTAINER   # then re-run this script"
      say ""
      say "     If that fails, the logs were still collected from the host — run"
      say "     with --bundle and send it."
    fi
  elif [ "$MKSTATE" = ok ]; then
    SRCOUNT=$(printf '%s' "$CSR" | wc -w | tr -d ' ')
    if [ "${SRCOUNT:-0}" -gt "${DRVCOUNT:-0}" ]; then
      say "  >> ONLY $DRVCOUNT of $SRCOUNT OPTICAL DRIVES ARE VISIBLE TO MakeMKV."
      say "     The container has $SRCOUNT /dev/sr* nodes but MakeMKV enumerated"
      say "     $DRVCOUNT. A drive it cannot see almost always has no SCSI generic"
      say "     node of its own — check the sg column on the HOST:"
      say ""
      say "       lsscsi -g"
      say ""
      say "     A '-' there means that drive has no /dev/sgN, and MakeMKV cannot"
      say "     use it. USB enclosures that fall back to usb-storage are the"
      say "     usual culprit; try a different port or enclosure."
    else
      say "  Not a drive-detection problem — MakeMKV can see all $DRVCOUNT drive(s)."
    fi
  elif [ "$MKSTATE" = notinstalled ]; then
    say "  Inconclusive — install MakeMKV via the web UI, then re-run."
  elif [ "$IS_DESKTOP" = yes ]; then
    say "  >> DOCKER DESKTOP CANNOT RIP DISCS."
    say "     It runs the engine inside its own VM, which has its own kernel and"
    say "     its own /dev — your optical drives are not in it. --device and"
    say "     privileged are honoured faithfully against a machine that has"
    say "     nothing to give, so no configuration change will help."
    say ""
    say "     Switch to the native engine:"
    say "       sudo apt install docker.io          # or Docker CE"
    say ""
    say "     Uninstalling Desktop leaves two things behind in ~/.docker/config.json:"
    say "     a credential helper that no longer exists (docker-credential-desktop)"
    say "     and a context still pointing at Desktop's socket. Clearing both:"
    say ""
    say "       mv ~/.docker/config.json ~/.docker/config.json.bak"
    say ""
    say "     Then RE-CREATE the container. Ones made under Desktop lived inside"
    say "     its VM and do not exist for the native engine — 'docker start' will"
    say "     not find them."
  elif [ "$SG_LOADED" = no ] && [ -z "$HOST_SG" ] && [ "$SG_BUILTIN" = yes ]; then
    say "  >> SCSI GENERIC IS BUILT IN, BUT NO sg DEVICES EXIST."
    say "     Nothing to modprobe — support is compiled into the kernel. The"
    say "     kernel is not registering your optical drives as SCSI devices at"
    say "     all, which is a lower-level problem than this script can diagnose."
    say "     Send a bundle: $0 --bundle"
  elif [ "$SG_LOADED" = no ] && [ -z "$HOST_SG" ]; then
    say "  >> HOST IS MISSING THE sg KERNEL MODULE."
    say "     MakeMKV enumerates optical drives through SCSI generic. A container"
    say "     cannot create these nodes — it shares the host kernel. On the HOST:"
    say ""
    say "       sudo modprobe sg"
    say "       echo sg | sudo tee /etc/modules-load.d/sg.conf   # survive reboots"
    say "       docker restart $CONTAINER                        # REQUIRED"
  elif [ -n "$HOST_SG" ] && [ -z "$CSG" ] && [ "$PRIV" = false ]; then
    say "  >> CONTAINER IS NOT PRIVILEGED."
    say "     It only receives the devices named in \`devices:\`, and the sg nodes"
    say "     were not among them. --device is fixed when a container is CREATED,"
    say "     so restarting will not add them — re-create it:"
    say ""
    say "       docker compose up -d --force-recreate     # with privileged: true"
  elif [ -n "$HOST_SG" ] && [ -z "$CSG" ] && [ "$PRIV_EFFECTIVE" = no ]; then
    say "  >> PRIVILEGED IS SET BUT NOT TAKING EFFECT."
    say "     The container reports privileged=true yet received none of the"
    say "     host's disks, so Docker is not enumerating host devices at all."
    say "     Restarting will not help; the engine itself cannot create these."
    say ""
    if [ "$IS_SNAP" = yes ]; then
      say "     Cause: Docker is SNAP-PACKAGED ($DOCKER_BIN). Snap confinement"
      say "     blocks creating arbitrary device nodes."
      say "       sudo snap remove docker"
      say "       # install Docker CE: https://docs.docker.com/engine/install/"
      say "       # then re-create the container"
    elif [ "$IS_ROOTLESS" = yes ]; then
      say "     Cause: Docker is ROOTLESS (root dir $DOCKER_ROOT). Rootless Docker"
      say "     cannot pass through host device nodes at all. Use the system"
      say "     daemon and re-create the container."
    else
      say "     Neither snap nor rootless detected — send us a bundle:"
      say "       $0 --bundle"
    fi
  elif [ -n "$HOST_SG" ] && [ -z "$CSG" ]; then
    say "  >> sg NODES NEVER REACHED THE CONTAINER."
    say "     The module is loaded on the host. A container's /dev is populated"
    say "     when it STARTS, so nodes created afterwards do not appear:"
    say ""
    say "       docker restart $CONTAINER"
  else
    say "  >> NOT a device-node problem — the nodes are present on both sides."
    say "     Send us a bundle so we can look at the logs:"
    say "       $0 --bundle"
  fi
}

report

# ── bundle ───────────────────────────────────────────────────────────────────
[ "$BUNDLE" = yes ] || { say ""; say "Tip: re-run with --bundle to produce a file you can send to support."; exit 0; }

# Where the file lands decides whether the user ever sees it. On the host, the
# working directory is fine. Inside the container it is not: $PWD there is a
# path that vanishes with the container and cannot be opened from outside, so
# default to /data, which is the mounted volume and the one directory a user
# can reach from the host.
if [ -z "$OUTDIR" ]; then
  if [ "$MODE" = container ] && [ -d /data ]; then OUTDIR=/data; else OUTDIR="$PWD"; fi
fi
mkdir -p "$OUTDIR" 2>/dev/null
if [ ! -w "$OUTDIR" ]; then
  say ""
  say "Cannot write to $OUTDIR — pass a writable directory:"
  say "    $0 --bundle /tmp"
  exit 1
fi
# Absolute, so the path printed at the end is one the user can paste.
OUTDIR=$(cd "$OUTDIR" 2>/dev/null && pwd) || OUTDIR="$OUTDIR"

STAMP=$(date -u +%Y%m%d-%H%M%S)
WORK=$(mktemp -d 2>/dev/null || mktemp -d -t mkvsupport)
DEST="$WORK/mkv-auto-support-$STAMP"
mkdir -p "$DEST/logs"
trap 'rm -rf "$WORK"' EXIT

report > "$DEST/verdict.txt" 2>&1

{
  say "mode=$MODE container=$CONTAINER container_running=$CONTAINER_RUNNING"
  say "sg_available=$SG_LOADED sg_module=$SG_MODULE sg_builtin=$SG_BUILTIN"
  say "host_scsi_generic=${HOST_SG:-none} host_optical=${HOST_SR:-none}"
  say "container_sr=${CSR:-none} container_sg=${CSG:-none}"
  say "dev_node_count=${DEVCOUNT:-?} host_disk_visible=${HOSTDISK:-none}"
  say "privileged=$PRIV privileged_effective=$PRIV_EFFECTIVE"
  say "docker_bin=${DOCKER_BIN:-?} docker_root=${DOCKER_ROOT:-?} snap=$IS_SNAP rootless=$IS_ROOTLESS"
  say "kernel=$(uname -r 2>/dev/null) arch=$(uname -m 2>/dev/null)"
} > "$DEST/environment.txt"

cin 'cat /proc/scsi/scsi 2>/dev/null'                 > "$DEST/scsi-topology.txt"
cin 'ls -l /dev/sr* /dev/sg* 2>/dev/null'             > "$DEST/device-nodes.txt"
cin 'ls /dev 2>/dev/null'                             > "$DEST/dev-listing.txt"
host_modules | grep -E '^(sg|sr_mod|cdrom|usb_storage|uas) ' > "$DEST/host-modules.txt" 2>/dev/null
printf '%s\n' "$MK"                                   > "$DEST/makemkvcon-enumeration.txt"
[ "$MODE" = host ] && {
  docker info                     > "$DEST/docker-info.txt" 2>&1
  docker inspect "$CONTAINER"     > "$DEST/docker-inspect.json" 2>&1
  have lsscsi && lsscsi -g        > "$DEST/lsscsi.txt" 2>&1
}

# Logs. Newest first, capped so a long-running install does not produce a
# bundle nobody can email.
# Streamed through `docker exec` when the container is up, so the script never
# needs to know where /data lives. When it is down — which is exactly when
# someone is debugging a container that will not start — fall back to the
# volume's host path, so the bundle still carries logs.
if [ "$CONTAINER_RUNNING" = yes ]; then
  for f in $(cin "ls -1t $LOGDIR 2>/dev/null | head -12"); do
    cin "tail -c 2000000 $LOGDIR/$f" > "$DEST/logs/$f" 2>/dev/null
  done
  LOGSRC="container ($LOGDIR)"
elif HL=$(host_logdir); then
  for f in $(ls -1t "$HL" 2>/dev/null | head -12); do
    tail -c 2000000 "$HL/$f" > "$DEST/logs/$f" 2>/dev/null
  done
  LOGSRC="host volume ($HL) — container was not running"
else
  LOGSRC="unavailable — container not running and its /data mount could not be resolved"
  say "$LOGSRC" > "$DEST/logs/README-no-logs.txt"
fi
say "log_source=$LOGSRC" >> "$DEST/environment.txt"

# Redaction. Logs carry API keys, transfer credentials and destination hosts.
# Better to over-scrub a support bundle than to leak a user's secrets — say so
# in the manifest so they know what they are sending.
find "$DEST" -type f -exec sed -i.bak -E \
  -e 's/((api[_-]?key|apikey|token|password|passwd|secret|auth)"?[[:space:]]*[:=][[:space:]]*"?)[^"[:space:],}]+/\1***REDACTED***/Ig' \
  -e 's#(://[^:/@[:space:]]+):[^@[:space:]]+@#\1:***REDACTED***@#g' \
  {} \; 2>/dev/null
find "$DEST" -name '*.bak' -delete 2>/dev/null

cat > "$DEST/README.txt" <<EOF
MKV-Auto support bundle — $STAMP (collected from: $MODE)

  verdict.txt                 diagnosis, same as the script prints
  environment.txt             host/container drive + engine summary
  scsi-topology.txt           /proc/scsi/scsi
  device-nodes.txt            /dev/sr* and /dev/sg* with permissions
  dev-listing.txt             every node in the container's /dev
  host-modules.txt            sg / sr_mod / cdrom / usb_storage load state
  makemkvcon-enumeration.txt  raw robot output of 'info disc:9999'
  logs/                       up to 12 newest logs, 2MB each
  docker-info.txt             engine posture (host collection only)
  docker-inspect.json         container config (host collection only)

Passwords, API keys and tokens have been replaced with ***REDACTED***.
Skim it before sending if you want to be sure — it is a plain tar.gz.
EOF

OUT="$OUTDIR/mkv-auto-support-$STAMP.tar.gz"
if ! tar -czf "$OUT" -C "$WORK" "mkv-auto-support-$STAMP" 2>/dev/null; then
  say ""
  say "Could not write $OUT — pass a writable directory: $0 --bundle /tmp"
  exit 1
fi

say ""
say "─────────────────────────────────────────────────────────────"
say " BUNDLE WRITTEN"
say "   $OUT   ($(du -h "$OUT" 2>/dev/null | cut -f1))"
say "─────────────────────────────────────────────────────────────"
if [ "$MODE" = container ]; then
  # That path is inside the container. Say how to reach it from outside, or the
  # user has a file they cannot attach to anything.
  case "$OUTDIR" in
    /data*) say " That is inside the container, on the /data volume. From the HOST:"
            say "   docker cp $CONTAINER:$OUT ." ;;
    *)      say " WARNING: $OUTDIR is inside the container and is NOT the /data"
            say " volume, so it is not reachable from the host and is lost when"
            say " the container is replaced. Either copy it out now:"
            say "   docker cp $CONTAINER:$OUT ."
            say " or re-run with --bundle /data" ;;
  esac
else
  say " That is a path on this host — attach it to your issue."
fi
say ""
say " Secrets are redacted; see README.txt inside. Skim it before sending"
say " if you want to be sure — it is a plain tar.gz."
