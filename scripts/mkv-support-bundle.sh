#!/usr/bin/env bash
# MKV-Auto support triage + log bundle.
#
#   ./mkv-support-bundle.sh                 # print a diagnosis
#   ./mkv-support-bundle.sh --bundle        # also write a .tar.gz to send us
#   ./mkv-support-bundle.sh --bundle ~/out  # ...somewhere specific
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

CONTAINER=mkv-auto
BUNDLE=no
OUTDIR="${PWD}"

while [ $# -gt 0 ]; do
  case "$1" in
    --bundle) BUNDLE=yes; case "${2:-}" in -*|"") ;; *) OUTDIR="$2"; shift ;; esac ;;
    -c|--container) CONTAINER="${2:-mkv-auto}"; shift ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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

# ── helpers that hide the host/container split ───────────────────────────────
# cin: run a command INSIDE the container from wherever we are. The `sh -c` is
# what makes globs expand in the right place — see the header note.
cin() {
  if [ "$MODE" = container ]; then sh -c "$1" 2>/dev/null
  else docker exec "$CONTAINER" sh -c "$1" 2>/dev/null
  fi
}
# Host facts readable from inside a container: /proc/modules is the host's
# module list, and /sys/class/scsi_generic is the host's SCSI generic devices —
# both visible even from an unprivileged container with no /dev/sg* of its own.
host_modules()   { cin 'cat /proc/modules 2>/dev/null'; }
host_scsi_generic() { cin 'ls /sys/class/scsi_generic 2>/dev/null'; }

LOGDIR=/data/mkvauto/logs

# ── collect ──────────────────────────────────────────────────────────────────
SG_LOADED=no
host_modules | grep -qE '^sg ' && SG_LOADED=yes
HOST_SG=$(host_scsi_generic | tr '\n' ' ')
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
  # Docker Desktop for Linux runs the engine inside its own VM. That VM has its
  # own kernel and its own /dev, so it never has the host's optical drives —
  # --device and privileged are honoured faithfully against a machine that has
  # nothing to give. Confirmed as a real cause; a user's container showed
  # /dev/sr0 as an empty *directory*, which is what Docker creates when a
  # bind-mount source does not exist on the engine side.
  IS_DESKTOP=no
  DOCKER_CTX=$(docker context show 2>/dev/null)
  case "$DOCKER_CTX" in desktop-*|*desktop*) IS_DESKTOP=yes ;; esac
  case "${DOCKER_HOST:-}" in *docker/desktop*) IS_DESKTOP=yes ;; esac
  docker info 2>/dev/null | grep -qi 'Operating System: Docker Desktop' && IS_DESKTOP=yes
fi

MK=$(cin 'command -v makemkvcon >/dev/null 2>&1 && timeout 120 makemkvcon -r --cache=1 info disc:9999 2>&1')

# A real drive is any DRV line with a non-empty drive_hardware_name. Keying on
# the name rather than the flags field is what makes this correct: flags is 0
# for an empty tray, 2 for a disc loaded, 256 for an unused slot — so matching
# flags=2 counts *discs*, not drives, and silently hides every drive that
# happens to be empty. Unused slots are `,"","",""` and never match.
drv_lines() { printf '%s' "$MK" | grep -E '^DRV:[0-9]+,[0-9]+,[0-9]+,[0-9]+,"[^"]'; }
DRVCOUNT=$(drv_lines | grep -c . || true)

if [ -z "$MK" ]; then                              MKSTATE=notinstalled
elif printf '%s' "$MK" | grep -q 'MSG:5042'; then  MKSTATE=nodrives
elif [ "${DRVCOUNT:-0}" -eq 0 ]; then              MKSTATE=nodrives
else                                               MKSTATE=ok
fi

# privileged is only a *request*; Docker populates the container's /dev (a
# tmpfs) by enumerating host devices at start. A working privileged container
# receives the host's whole device set — ~180 nodes including its disks. One
# where that enumeration is blocked reports privileged=true and gets ~17.
PRIV_EFFECTIVE=yes
[ "$PRIV" = true ] && [ -z "$HOSTDISK" ] && PRIV_EFFECTIVE=no

# ── report ───────────────────────────────────────────────────────────────────
report() {
  say "=== MKV-Auto support triage ==="
  say "  collected from : $MODE"
  say ""
  say "HOST (as seen from here)"
  say "  sg kernel module      : $([ $SG_LOADED = yes ] && echo loaded || echo 'NOT LOADED')"
  say "  SCSI generic devices  : ${HOST_SG:-none}"
  say ""
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
  case "$MKSTATE" in
    notinstalled) say "  makemkvcon            : not installed yet (finish MakeMKV setup in the UI)" ;;
    nodrives)     say "  makemkvcon            : MSG:5042 — finds NO usable optical drives" ;;
    ok)           say "  makemkvcon            : $DRVCOUNT drive(s) found"
                  # flags: 0 = empty tray, 2 = disc loaded. Both are detected
                  # drives; say which so "no disc" is not read as "no drive".
                  drv_lines | while IFS= read -r l; do
                    f=$(printf '%s' "$l" | cut -d, -f2)
                    case "$f" in 2) d="disc loaded" ;; 0) d="empty tray" ;; *) d="flags=$f" ;; esac
                    say "      [$d] $(printf '%s' "$l" | cut -d, -f5-)"
                  done ;;
  esac
  say ""
  say "VERDICT"
  if [ "$MKSTATE" = ok ]; then
    say "  Not a drive-detection problem — MakeMKV can see the drives."
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

STAMP=$(date -u +%Y%m%d-%H%M%S)
WORK=$(mktemp -d 2>/dev/null || mktemp -d -t mkvsupport)
DEST="$WORK/mkv-auto-support-$STAMP"
mkdir -p "$DEST/logs"
trap 'rm -rf "$WORK"' EXIT

report > "$DEST/verdict.txt" 2>&1

{
  say "mode=$MODE container=$CONTAINER"
  say "sg_loaded=$SG_LOADED host_scsi_generic=${HOST_SG:-none}"
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
for f in $(cin "ls -1t $LOGDIR 2>/dev/null | head -12"); do
  cin "tail -c 2000000 $LOGDIR/$f" > "$DEST/logs/$f" 2>/dev/null
done

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

mkdir -p "$OUTDIR" 2>/dev/null
OUT="$OUTDIR/mkv-auto-support-$STAMP.tar.gz"
if tar -czf "$OUT" -C "$WORK" "mkv-auto-support-$STAMP" 2>/dev/null; then
  say ""
  say "Bundle written: $OUT  ($(du -h "$OUT" 2>/dev/null | cut -f1))"
  say "Secrets are redacted; see README.txt inside. Attach it to your issue."
else
  say ""
  say "Could not write $OUT — pass a writable directory: $0 --bundle /tmp"
  exit 1
fi
