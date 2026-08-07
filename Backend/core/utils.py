import re, os, struct, hashlib, json, requests, stat, tempfile, socket, shutil, logging, time, fcntl, glob, signal
import subprocess
import threading
import datetime
from typing import Optional, Callable
from tqdm import tqdm
from gql import gql, Client
from gql.transport.requests import RequestsHTTPTransport
from pathvalidate import sanitize_filepath
import platform
import string
from pathlib import Path
from filelock import FileLock, Timeout
import unicodedata

# Use the API logger so messages land in backend.log
from core.logging_utils import get_logger
from core.log_file_config import rotate_file_if_needed
from core.drive_identity import resolve_drive_identity
from core.drive_registry import loaded_drives as _registry_loaded_drives

logger = get_logger("core.utils")

MOUNT_TIMEOUT = int(os.getenv("MAKEMKV_MOUNT_TIMEOUT", "30"))  # seconds
DRIVE_SCAN_TIMEOUT = float(os.getenv("DRIVE_SCAN_TIMEOUT", "-1"))  # seconds; <=0 disables timeout

_last_drive_scan: dict = {"ts": 0, "drives": [], "fail_count": 0, "drive_hardware": {}}

# MakeMKV DRV robot lines: index, drive hardware name, volume label (disc name), device path.
_DRV_LINE_RE = re.compile(r'^DRV:(\d+),[^,]*,[^,]*,[^,]*,"([^"]*)","([^"]*)","([^"]+)"')
SG_TURS_BIN = shutil.which("sg_turs")  # optional media-ready probe

# Characters unsafe in path components on Linux and Windows (Jellyfin/OS rules)
_PATH_COMPONENT_UNSAFE = re.compile(r'[\x00-\x1f\\/:*?"<>|]')


def sanitize_path_component(s: str) -> str:
    """
    Sanitize a single path component (folder or filename segment) for use on
    Linux and Windows. Replaces or strips characters that are problematic on
    both platforms; trims leading/trailing spaces and dots.
    Use only for path segments; keep original values in DB/API for display.
    """
    if s is None or not isinstance(s, str):
        return ""
    out = _PATH_COMPONENT_UNSAFE.sub("", s)
    out = out.strip(string.whitespace + ".")
    return out


def get_os_name() -> str:
    name = platform.system().lower()
    if name.startswith("linux"):
        return "linux"
    if name.startswith("darwin"):
        return "macos"
    if name.startswith("win"):
        return "windows"
    return name

def get_makemkvcon_path() -> str:
    # allow override via env
    env_path = os.getenv("MAKEMKVCON_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path

    os_name = get_os_name()
    home_override = os.path.join(os.path.expanduser("~"), ".local", "makemkv", "bin", "makemkvcon")
    if os.path.isfile(home_override):
        return home_override

    if os_name == "windows":
        # 64-bit Windows install
        return r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe"
    elif os_name == "linux":
        # most distros install it here
        return "/usr/bin/makemkvcon"
    elif os_name == "macos":
        # if you used the official macOS dmg
        return "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon"
    else:
        raise RuntimeError(f"Unsupported OS: {os_name}")

def get_mkvauto_root() -> Path:
    """
    Base path for app-level files (lock, config, keys). Defaults to ~/MakeMKV-Auto.
    """
    env_root = os.getenv("MKVAUTO_ROOT") or os.getenv("MAKEMKV_CONFIG_DIR")
    root = Path(env_root or (Path.home() / "MakeMKV-Auto")).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root

def get_mkvauto_tmp() -> Path:
    """Temporary directory under MKVAUTO_ROOT/tmp."""
    tmp_env = os.getenv("MKVAUTO_TMP_DIR")
    base = Path(tmp_env or (get_mkvauto_root() / "tmp")).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    return base

def get_drive_scan_lock_path() -> Path:
    """
    Cross-process drive-scan lock location. Defaults to MKVAUTO tmp so all locks
    live under the same root; overrideable via DRIVE_SCAN_FILELOCK env.
    """
    env_path = os.getenv("DRIVE_SCAN_FILELOCK")
    path = Path(env_path).expanduser() if env_path else get_mkvauto_tmp() / "drive-scan.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

def get_mkvauto_conf() -> Path:
    """Config directory under MKVAUTO_ROOT/config."""
    conf_env = os.getenv("MKVAUTO_CONF_DIR")
    base = Path(conf_env or (get_mkvauto_root() / "config")).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    return base

def get_mkvauto_data() -> Path:
    """
    Base path for job artifacts. Defaults to ${MKVAUTO_ROOT}/jobs.
    Override with:
      - MKVAUTO_JOBS_DIR (absolute jobs path)
      - MKVAUTO_DATA / MAKEMKV_DATA_DIR (data root; jobs/ appended)
    """
    jobs_env = os.getenv("MKVAUTO_JOBS_DIR")
    if jobs_env:
        base = Path(jobs_env).expanduser()
    else:
        env_data = os.getenv("MKVAUTO_DATA") or os.getenv("MAKEMKV_DATA_DIR")
        base_root = Path(env_data).expanduser() if env_data else get_mkvauto_root()
        base = base_root / "jobs"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.error("Failed to prepare data directory %s: %s", base, exc)
        raise
    return base

def get_export_root() -> Path:
    """
    Base path for finalized metadata exports. Defaults to ${MKVAUTO_DATA_DIR||MKVAUTO_DATA||MAKEMKV_DATA_DIR||MKVAUTO_ROOT}/export.
    Separated from the jobs root to keep metadata collocated under the data dir.
    """
    env_data = os.getenv("MKVAUTO_DATA_DIR") or os.getenv("MKVAUTO_DATA") or os.getenv("MAKEMKV_DATA_DIR")
    base_root = Path(env_data).expanduser() if env_data else get_mkvauto_root()
    export_root = base_root / "export"
    export_root.mkdir(parents=True, exist_ok=True)
    return export_root

def is_dev_mode() -> bool:
    """Return True if dev mode is enabled via ENABLE_DEVMODE env var."""
    return os.getenv("ENABLE_DEVMODE", "").strip().lower() in ("1", "true", "yes", "on")

def get_discdb_repo_url() -> str:
    """DiscDB data repo URL used for dev-mode validation."""
    return os.getenv("THEDISCDB_REPO", "https://github.com/TheDiscDb/data.git")

def get_discdb_repo_branch() -> str:
    """DiscDB data repo branch (defaults to main)."""
    return os.getenv("THEDISCDB_BRANCH", "main")

def get_discdb_repo_path() -> Path:
    """Cached checkout path for DiscDB data (defaults to ${MKVAUTO_ROOT}/thediscdb)."""
    override = os.getenv("THEDISCDB_PATH")
    if override:
        path = Path(override).expanduser()
    else:
        path = get_mkvauto_root() / "thediscdb"
    path.mkdir(parents=True, exist_ok=True)
    return path

def get_transfer_root() -> Path:
    """
    Base path for transfer-related state (rsync configs/keys). Defaults to ${MKVAUTO_DATA}/transfer.
    Override with MKVAUTO_TRANS_DIR.
    """
    trans_env = os.getenv("MKVAUTO_TRANS_DIR")
    if trans_env:
        base = Path(trans_env).expanduser()
    else:
        env_data = os.getenv("MKVAUTO_DATA") or os.getenv("MAKEMKV_DATA_DIR")
        base_root = Path(env_data).expanduser() if env_data else get_mkvauto_root()
        base = base_root / "transfer"
    base.mkdir(parents=True, exist_ok=True)
    return base

def get_default_output_path() -> str:
    return str(get_mkvauto_data())

def resolve_jobs_root(out_dir: str | None = None) -> Path:
    """
    Normalize the jobs root. If caller passes the data root (MKVAUTO_DATA/MAKEMKV_DATA_DIR)
    or MKVAUTO_ROOT, we append "jobs" so we don't litter the data root. If a specific jobs
    dir is provided, use it as-is.
    """
    jobs_env = os.getenv("MKVAUTO_JOBS_DIR")
    if out_dir:
        base = Path(out_dir).expanduser()
        # Guard against accidental root ("/") overrides; fall back to configured jobs dir.
        if str(base) == "/":
            base = Path(jobs_env).expanduser() if jobs_env else get_mkvauto_data()
    elif jobs_env:
        base = Path(jobs_env).expanduser()
    else:
        return get_mkvauto_data()

    env_data = os.getenv("MKVAUTO_DATA") or os.getenv("MAKEMKV_DATA_DIR")
    candidates = [get_mkvauto_root()]
    if env_data:
        candidates.append(Path(env_data).expanduser())

    if base.name == "jobs":
        target = base
    elif any(base == c for c in candidates):
        target = base / "jobs"
    else:
        target = base

    target.mkdir(parents=True, exist_ok=True)
    return target
    
def get_lock_path() -> str:
    """
    Location for the cross-process rip lock.
    """
    lock_path = get_mkvauto_tmp() / "disc-ripper.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    return str(lock_path)

# use this everywhere you need the path
MAKEMKVCON_PATH = get_makemkvcon_path()

DISKDBURL = "https://thediscdb.com/graphql/"

PRGV_RE = re.compile(r'^PRGV:(\d+),(\d+),(\d+)')
PAT_TITLE = re.compile(r'^MSG:3307,\d+,\d+,"File (\d{5}\.(?:mpls|m2ts)) was added as title #(\d+)"')


def _extract_output_dir_from_cmdline(cmdline: str) -> Path | None:
    """
    Extract the output directory path from a makemkvcon command line.
    For 'mkv disc:X all /path/to/output ...', returns /path/to/output.
    """
    try:
        parts = cmdline.split()
        # Look for 'mkv' or 'backup' command followed by 'disc:X' and 'all' or a path
        for i, part in enumerate(parts):
            if part in ('mkv', 'backup') and i + 1 < len(parts):
                # Look ahead for disc:X pattern
                j = i + 1
                while j < len(parts):
                    if parts[j].startswith('disc:'):
                        # Found disc:X, now look for the output path
                        # It's usually after 'all' for mkv, or after disc:X for backup
                        j += 1
                        if j < len(parts) and parts[j] == 'all':
                            j += 1
                        if j < len(parts):
                            # This should be the output path
                            output_path = Path(parts[j])
                            if output_path.is_absolute() or output_path.exists():
                                return output_path
                    j += 1
    except Exception as exc:
        logger.debug(f"Failed to extract output dir from cmdline: {exc}")
    return None


def monitor_running_makemkvcon(pid: int, log_path: Path | None, line_cb: Callable[[str], None] | None = None) -> int:
    """
    Monitor a running makemkvcon process by tailing its log file.
    Returns the exit code when the process completes.
    
    Args:
        pid: Process ID of the running makemkvcon process
        log_path: Path to the log file being written by the process
        line_cb: Optional callback function to call for each line of output
        
    Returns:
        Exit code of the process (0 on success, non-zero on failure)
    """
    if not log_path or not log_path.exists():
        logger.warning(f"Log file {log_path} does not exist, cannot monitor process {pid}")
        return -1
    
    # Check if we're the parent of this process (to detect orphaned processes)
    current_pid = os.getpid()
    orphaned_detected = False
    
    stall_seconds = get_rip_output_stall_seconds()
    last_log_output_at = time.monotonic()

    try:
        # Tail the log file (read from end, like tail -f)
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            # Seek to end to only read new output
            f.seek(0, 2)
            last_position = f.tell()
            
            while True:
                # Check if process is still running
                try:
                    os.kill(pid, 0)  # Check if process exists (doesn't kill, just checks)
                except OSError:
                    # Process has exited
                    break
                
                # Check if process is a zombie (defunct) - os.kill succeeds for zombies
                # so we need to explicitly check the process state
                try:
                    status_path = Path(f"/proc/{pid}/status")
                    if status_path.exists():
                        ppid = None
                        state = None
                        with open(status_path, 'r') as status_file:
                            for status_line in status_file:
                                if status_line.startswith("State:"):
                                    state = status_line.split(":", 1)[1].strip()
                                elif status_line.startswith("PPid:"):
                                    ppid = int(status_line.split(":", 1)[1].strip())
                        
                        # Check if process is orphaned (parent is not us and not init)
                        if ppid is not None and ppid != current_pid and ppid != 1:
                            if not orphaned_detected:
                                logger.warning(
                                    f"Process {pid} appears to be orphaned: parent PID is {ppid} "
                                    f"(current process is {current_pid}). This may indicate the original "
                                    f"worker process crashed. Process state: {state}"
                                )
                                orphaned_detected = True
                        
                        if state and ("zombie" in state.lower() or "defunct" in state.lower()):
                            # Process is zombie/defunct, try to reap it and exit
                            if orphaned_detected:
                                logger.error(
                                    f"Process {pid} is zombie/defunct and was orphaned (parent {ppid}). "
                                    f"This indicates the original worker process crashed, leaving the child orphaned."
                                )
                            else:
                                logger.warning(f"Process {pid} is zombie/defunct, attempting to reap")
                            
                            try:
                                _, status = os.waitpid(pid, os.WNOHANG)
                                if os.WIFEXITED(status):
                                    exit_code = os.WEXITSTATUS(status)
                                    logger.info(f"Reaped zombie process {pid}, exit code: {exit_code}")
                                    return exit_code
                                elif os.WIFSIGNALED(status):
                                    signal = os.WTERMSIG(status)
                                    logger.warning(f"Reaped zombie process {pid}, killed by signal: {signal}")
                                    return -signal
                            except (OSError, ChildProcessError):
                                # Already reaped or can't wait, try blocking wait
                                try:
                                    _, status = os.waitpid(pid, 0)
                                    if os.WIFEXITED(status):
                                        exit_code = os.WEXITSTATUS(status)
                                        logger.info(f"Reaped zombie process {pid} (blocking wait), exit code: {exit_code}")
                                        return exit_code
                                    elif os.WIFSIGNALED(status):
                                        signal = os.WTERMSIG(status)
                                        logger.warning(f"Reaped zombie process {pid} (blocking wait), killed by signal: {signal}")
                                        return -signal
                                except (OSError, ChildProcessError) as wait_exc:
                                    # Can't reap, assume failure
                                    logger.error(
                                        f"Could not reap zombie process {pid}: {wait_exc}. "
                                        f"Process may have been orphaned by a crashed worker."
                                    )
                                    return -1
                            # If we got here, couldn't determine exit code
                            return -1
                except (OSError, IOError):
                    # Can't read /proc status, continue monitoring
                    pass
                
                # Read new lines
                f.seek(last_position)
                new_lines = f.readlines()
                last_position = f.tell()

                had_content = False
                for line in new_lines:
                    stripped = line.strip()
                    if stripped and line_cb:
                        # Skip timestamp prefix if present (format: [YYYY-MM-DD HH:MM:SS] line)
                        if stripped.startswith('[') and ']' in stripped:
                            stripped = stripped.split(']', 1)[1].strip()
                        if stripped:  # Only call if there's content after removing timestamp
                            had_content = True
                            line_cb(stripped)
                    elif stripped:
                        had_content = True

                if had_content:
                    last_log_output_at = time.monotonic()
                elif stall_seconds > 0 and (time.monotonic() - last_log_output_at) >= stall_seconds:
                    logger.warning(
                        "makemkvcon PID %s: no new log lines for %ds; sending kill (stall watchdog)",
                        pid,
                        stall_seconds,
                    )
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    break
                
                # Wait a bit before checking again
                time.sleep(0.5)
        
        # Process has exited, try to get exit code
        try:
            # Use waitpid to get exit status
            _, status = os.waitpid(pid, os.WNOHANG)
            if os.WIFEXITED(status):
                return os.WEXITSTATUS(status)
            elif os.WIFSIGNALED(status):
                return -os.WTERMSIG(status)
        except (OSError, ChildProcessError):
            # Process already reaped or we can't wait for it
            pass
        
        # Check /proc/{pid}/status for exit code
        try:
            status_path = Path(f"/proc/{pid}/status")
            if status_path.exists():
                with open(status_path, 'r') as f:
                    for line in f:
                        if line.startswith("State:"):
                            if "zombie" in line.lower():
                                # Process is zombie, try waitpid again
                                try:
                                    _, status = os.waitpid(pid, 0)
                                    if os.WIFEXITED(status):
                                        return os.WEXITSTATUS(status)
                                except (OSError, ChildProcessError):
                                    pass
                            break
        except (OSError, IOError):
            pass
        
        return 0  # Assume success if we can't determine exit code
    except Exception as exc:
        logger.error(f"Error monitoring makemkvcon process {pid}: {exc}")
        return -1


def _is_makemkvcon_running_for_disc(
    first: str,
    makemkv_disc_index: str | None = None,
) -> bool:
    """
    Check if makemkvcon mkv/backup is running for this drive.

    If ``first`` is a device path (``/dev/sr*``), match ``dev:{path}`` and optionally
    ``disc:{makemkv_disc_index}``. Otherwise treat ``first`` as a MakeMKV index string
    and match both ``disc:{first}`` and legacy ``dev:{first}``.
    """
    try:
        import subprocess

        patterns: list[str] = []
        if first.startswith("/dev/"):
            patterns.append(rf"makemkvcon.*(mkv|backup).*dev:{re.escape(first)}")
            di = (makemkv_disc_index or "").strip()
            if di:
                patterns.append(rf"makemkvcon.*(mkv|backup).*disc:{re.escape(di)}")
        else:
            esc = re.escape(first.strip())
            patterns.append(rf"makemkvcon.*(mkv|backup).*disc:{esc}")
            patterns.append(rf"makemkvcon.*(mkv|backup).*dev:{esc}")

        for pattern in patterns:
            result = subprocess.run(
                ["pgrep", "-f", pattern],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                return True
        return False
    except Exception:
        return False


def _find_makemkvcon_process_for_disc(
    disc_num: str,
    mount_point: str | None = None,
) -> tuple[int | None, str | None]:
    """
    Find the PID and cmdline of makemkvcon mkv/backup for this disc.

    ``disc_num`` is the MakeMKV index when not a path. If ``mount_point`` is set,
    also match ``dev:{mount_point}`` and ``disc:{disc_num}``.
    """
    try:
        import subprocess

        search_patterns: list[str] = []
        if disc_num.startswith("/dev/"):
            search_patterns.append(rf"makemkvcon.*(mkv|backup).*dev:{re.escape(disc_num)}")
        else:
            esc = re.escape(disc_num.strip())
            search_patterns.append(rf"makemkvcon.*(mkv|backup).*disc:{esc}")
            search_patterns.append(rf"makemkvcon.*(mkv|backup).*dev:{esc}")
        mp = (mount_point or "").strip()
        if mp.startswith("/dev/"):
            search_patterns.append(rf"makemkvcon.*(mkv|backup).*dev:{re.escape(mp)}")

        # Use pgrep to find matching processes
        for pattern in search_patterns:
            result = subprocess.run(
                ["pgrep", "-f", pattern],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True
            )
            if result.returncode == 0 and result.stdout.strip():
                pids = [int(pid.strip()) for pid in result.stdout.strip().split('\n') if pid.strip()]
                for pid in pids:
                    try:
                        # Read command line from /proc/{pid}/cmdline (Linux)
                        cmdline_path = Path(f"/proc/{pid}/cmdline")
                        if cmdline_path.exists():
                            cmdline_bytes = cmdline_path.read_bytes()
                            # cmdline is null-separated, replace nulls with spaces
                            cmdline = cmdline_bytes.replace(b'\x00', b' ').decode('utf-8', errors='ignore').strip()
                            # Verify it's actually a mkv/backup operation (not info)
                            if 'mkv' in cmdline or 'backup' in cmdline:
                                # Double-check it matches our pattern
                                import re
                                if re.search(pattern.replace('.*', '.*'), cmdline):
                                    return (pid, cmdline)
                    except (OSError, IOError, ValueError):
                        # Process may have exited, try next PID
                        continue
        
        return (None, None)
    except Exception as exc:
        logger.debug("Failed to find makemkvcon process for disc %s: %s", disc_num, exc)
        return (None, None)


def kill_makemkvcon_for_disc(disc_num_or_mount: str, sigterm_timeout_seconds: float = 0.0) -> bool:
    """
    Find and kill the makemkvcon process running for a specific disc (by disc_num or mount_point).
    Uses SIGKILL immediately so the process cannot ignore the signal or delay exit (e.g. if the
    user inserts a new disc while we are waiting, makemkvcon might think the disc is still there
    and take a long time to close). Returns True if a process was found and killed, False otherwise.
    """
    pid, _ = _find_makemkvcon_process_for_disc(disc_num_or_mount)
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGKILL)
        logger.info("Sent SIGKILL to makemkvcon PID %s for disc %s", pid, disc_num_or_mount)
    except ProcessLookupError:
        return True  # already gone
    except OSError as exc:
        logger.warning("Failed to send SIGKILL to makemkvcon PID %s: %s", pid, exc)
        return False
    return True


def _maybe_clear_stale_lock(lock_path: Path, max_age_seconds: int = 600) -> None:
    """
    If a lock file lingers with no makemkvcon running and is older than the
    provided age, remove it to avoid a stale busy error.
    """
    try:
        if not lock_path.exists():
            return
        age = time.time() - lock_path.stat().st_mtime
        if age < max_age_seconds:
            return
        # If any makemkvcon process is running, assume the lock is legitimate.
        proc = subprocess.run(["pgrep", "-f", "makemkvcon"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if proc.returncode == 0:
            return
        logger.warning("Removing stale rip lock at %s (age %.0fs)", lock_path, age)
        lock_path.unlink(missing_ok=True)
    except Exception as exc:
        logger.debug("Failed to clear stale lock %s: %s", lock_path, exc)


class MakeMKVError(Exception):
    """Custom exception when makemkvcon fails."""
    pass


class MakeMKVStallError(MakeMKVError):
    """Raised when makemkvcon produces no stdout lines for longer than RIP_OUTPUT_STALL_SECONDS."""
    pass


class MakeMKVNoDrivesError(MakeMKVError):
    """MakeMKV enumerated zero usable optical drives (#802).

    Distinct from "this disc will not read": the engine never saw a drive to
    begin with, so retrying, cleaning the disc, or trying another disc cannot
    help. Kept as its own type so callers can surface the host-level remedy
    instead of a disc-level one.
    """
    pass


# MSG:5042 "The program can't find any usable optical drives."
# MSG:2024 "Unknown device - '/dev/sr0'" — emitted when an explicitly passed
#          device path matches nothing MakeMKV enumerated.
#
# The optional ``[timestamp] `` prefix matters: run_makemkv keeps the raw
# stdout line in ``full_output`` but writes a timestamped copy to
# makemkvcon.log. Accepting both means the same classifier works on the live
# stream and on a log file pasted into a bug report.
_MSG_PREFIX = r'^(?:\[[^\]]*\]\s*)?'
_NO_USABLE_DRIVES_RE = re.compile(_MSG_PREFIX + r'MSG:5042', re.M)
_UNKNOWN_DEVICE_RE = re.compile(
    _MSG_PREFIX + r'MSG:2024,[^,]*,[^,]*,"Unknown device[^"]*"', re.M
)


def diagnose_makemkv_no_drives(output: str) -> Optional[str]:
    """Actionable message when MakeMKV output shows zero usable drives, else None.

    The signature is ``MSG:5042`` (no usable optical drives) or ``MSG:2024``
    (a device path MakeMKV cannot resolve). Both mean the engine's drive
    enumeration came back empty, which on Linux almost always means there are
    no ``/dev/sg*`` nodes for it to enumerate — see
    ``core.drive_registry.scsi_generic_missing``.

    Pure and string-only so it can be unit-tested against captured logs
    without a drive, an engine, or a container.
    """
    if not output:
        return None
    saw_no_drives = bool(_NO_USABLE_DRIVES_RE.search(output))
    saw_unknown_device = bool(_UNKNOWN_DEVICE_RE.search(output))
    if not (saw_no_drives or saw_unknown_device):
        return None

    # Import here: core.drive_registry imports core.utils lazily for its media
    # probe, so a module-level import would close the cycle.
    try:
        from core.drive_registry import diagnose_no_drives_environment
        reason, detail = diagnose_no_drives_environment()
    except Exception:  # noqa: BLE001 - diagnosis must never be the thing that fails
        reason, detail = "unknown", "The drive environment could not be inspected."

    remedies = {
        "no_sg_nodes": [
            "Load the SCSI generic module ON THE HOST — a container cannot",
            "create these nodes, it shares the host kernel:",
            "",
            "    sudo modprobe sg",
            "    echo sg | sudo tee /etc/modules-load.d/sg.conf   # persist across reboots",
            "",
            "Then restart the container.",
        ],
        "sg_not_passed_through": [
            "The host is fine — do NOT run modprobe. The container was not given",
            "the SCSI generic nodes. Note that --device flags are fixed when a",
            "container is created, so `docker restart` will not add them:",
            "",
            "    docker inspect <container> --format '{{.HostConfig.Privileged}}'",
            "",
            "If that prints false, re-create the container with --privileged, or",
            "pass each drive's sg node explicitly (--device=/dev/sgN alongside",
            "--device=/dev/srN). See docs/HOST_OPTICAL_SETUP.md.",
        ],
        "no_devices": [
            "Pass the drive through to the container. For each drive you need",
            "both its /dev/srN node and its /dev/sgN node — see",
            "docs/HOST_OPTICAL_SETUP.md.",
        ],
        "no_sr_nodes": [
            "Pass the drive's /dev/srN node through to the container as well;",
            "the SCSI generic node alone is not enough. See",
            "docs/HOST_OPTICAL_SETUP.md.",
        ],
        "sg_not_accessible": [
            "Grant the container access to those nodes. The host's device group",
            "usually does not exist inside the container, so add its GID with",
            "group_add (compose) or --group-add (docker run). See",
            "docs/HOST_OPTICAL_SETUP.md.",
        ],
        "unknown": [
            "Collect the following and open an issue with the output — the",
            "device nodes themselves are not the problem:",
            "",
            "    docker exec <container> sg_inq /dev/sgN     # SCSI reachable?",
            "    docker exec <container> makemkvcon -r info disc:9999",
            "    docker inspect <container> --format '{{.HostConfig.Privileged}}'",
        ],
    }

    return "\n".join(
        ["MakeMKV found no usable optical drives.", "", detail, ""]
        + remedies.get(reason, remedies["unknown"])
    )


def get_rip_output_stall_seconds() -> int:
    """
    Max seconds without a line of process output before the rip is aborted (worker-owned stall).
    From env RIP_OUTPUT_STALL_SECONDS, default 300. 0 disables the watchdog.
    """
    raw = os.environ.get("RIP_OUTPUT_STALL_SECONDS", "300")
    try:
        v = int(str(raw).strip())
    except ValueError:
        v = 300
    return max(0, v)


def is_registration_related_makemkv_failure(exc: BaseException) -> bool:
    """
    True when makemkvcon failure likely needs a valid registration key (not a drive read error).
    Used to defer drive warmup retry until after the user registers.
    """
    if isinstance(exc, MakeMKVError):
        msg = str(exc).lower()
        if "253" in msg or "expired" in msg or "out of date" in msg:
            return True
        if "registration" in msg and "key" in msg:
            return True
        if "shareware" in msg or "evaluation period" in msg:
            return True
        if "purchase" in msg and "makemkv" in msg:
            return True
    return False


def is_disc_read_error(message: str) -> bool:
    """
    Return True if the error message indicates a disc/drive read error
    (e.g. MSG:2003, No such device, TIMEOUT ON LOGICAL UNIT, Failed to open disc).
    Used to classify rip failures for critical user notification and suggestions.
    """
    if not message:
        return False
    msg = message.lower()
    return (
        "msg:2003" in msg
        or "read-error" in msg
        or "read‐error" in message  # Unicode hyphen
        or "no such device" in msg
        or "timeout on logical unit" in msg
        or "failed to open disc" in msg
    )


def eject_disc(mount_point: str) -> bool:
    """
    Physically eject the disc at the given device path (e.g. /dev/sr0).
    Returns True on success, False if the command fails or eject is not available.
    Used by the eject_on_finish setting (#20).
    """
    import shutil
    import subprocess
    eject_bin = shutil.which("eject")
    if not eject_bin:
        logger.warning("eject_disc: 'eject' command not found; cannot eject %s", mount_point)
        return False
    try:
        result = subprocess.run(
            [eject_bin, mount_point],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            logger.info("eject_disc: ejected %s", mount_point)
            return True
        logger.warning("eject_disc: eject %s failed (rc=%s): %s", mount_point, result.returncode, result.stderr.strip())
        return False
    except Exception as exc:
        logger.warning("eject_disc: failed to eject %s: %s", mount_point, exc)
        return False


# --- Slug helpers (align with TheDiscDB ImportBuddy) ---
def slugify(value: str | None) -> str:
    """
    Lowercase slug generator: keep letters/digits/dash, whitespace -> dash, '&' -> 'and'.
    Mirrors ImportBuddy's StringExtensions.Slugify.
    """
    if not value:
        return ""
    replacements = {"Æ": "AE", "æ": "ae", "Œ": "OE", "œ": "oe"}
    for src, repl in replacements.items():
        value = value.replace(src, repl)
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    parts = []
    for c in value:
        if c.isalnum() or c == "-":
            parts.append(c.lower())
        elif c.isspace():
            parts.append("-")
        elif c == "&":
            parts.append("and")
    return "".join(parts)


def build_release_slug(name: str | None, year: int | None = None) -> str:
    """
    Build a release slug from name + optional year.
    """
    base = slugify(name) or "release"
    if year and year > 0:
        return f"{base}-{year}"
    return base


def normalize_disc_format(fmt: str | None) -> str | None:
    """
    Normalize disc format strings (UHD, Blu-Ray, DVD) for storage and display.
    Matches api.crud._normalize_format behavior.
    """
    if not fmt:
        return None
    f = str(fmt).strip()
    upper = f.upper()
    if "UHD" in upper or "4K" in upper:
        return "UHD"
    if "BLU" in upper or "BD" in upper:
        return "Blu-Ray"
    if "DVD" in upper:
        return "DVD"
    return f


def default_disc_name(disc_format: str | None, info_title: str | None) -> str | None:
    """
    Default human-readable disc name: "{info_title} - {format}" when both exist.
    """
    fmt = normalize_disc_format(disc_format) if disc_format else None
    title_raw = info_title
    if title_raw is None:
        title = None
    else:
        t = str(title_raw).strip()
        title = t if t else None
    if fmt and title:
        return f"{title} - {fmt}"
    if fmt:
        return fmt
    if title:
        return title
    return None


def _disc_slug_separator_char(c: str) -> bool:
    """Non-whitespace punctuation that becomes a hyphen in disc slugs."""
    if c in '-_/\\.:,|':
        return True
    if c == "_":
        return True
    try:
        return unicodedata.category(c) == "Pd"
    except TypeError:
        return False


def slugify_disc_name(name: str | None) -> str:
    """
    Disc filesystem-style slug: ASCII-fold like slugify; spaces -> underscore;
    other separators -> hyphen; drop remaining specials; collapse runs; trim edges.
    """
    if not name:
        return ""
    replacements = {"Æ": "AE", "æ": "ae", "Œ": "OE", "œ": "oe"}
    s = str(name)
    for src, repl in replacements.items():
        s = s.replace(src, repl)
    # Map dash punctuation (e.g. U+2010) to ASCII hyphen before stripping non-ASCII
    s = "".join("-" if unicodedata.category(c) == "Pd" else c for c in s)
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii")

    parts: list[str] = []
    for c in s:
        if c.isalnum():
            parts.append(c.lower())
        elif c.isspace():
            parts.append("_")
        elif _disc_slug_separator_char(c):
            parts.append("-")
        # else drop

    out: list[str] = []
    last: str | None = None
    for p in parts:
        if p == "_" and last == "_":
            continue
        if p == "-" and last == "-":
            continue
        out.append(p)
        last = p
    result = "".join(out).strip("_-")
    return result


# --- Core Functions ---

def run_makemkv(
    cmd_args: str,
    line_cb=None,
    log_path: Path | None = None,
    pid_callback=None,
) -> tuple[str, int | None]:
    """
    Run makemkvcon with provided arguments, show tqdm progress, return full output and PID.

    ``pid_callback`` is an optional ``Callable[[int], None]`` invoked
    immediately after ``Popen`` returns — i.e. the moment we know the
    makemkvcon PID, before any blocking wait. Workers persist ``Job.rip_pid``
    via this callback so restart-during-rip recovery (#541) can validate
    against ``/proc/{pid}/cmdline``. Callback exceptions are caught and
    logged so they cannot affect rip progression.

    Returns:
        Tuple of (output: str, pid: int | None) where pid is the process ID of makemkvcon.
    """
    PRGV_RE      = re.compile(r'^PRGV:(\d+),(\d+),(\d+)')
    DISC_ERR_RE  = re.compile(r'^MSG:2003')   # SCSI ILLEGAL REQUEST read failure
    parts        = cmd_args.split()
    if "-r" not in parts and "--robot" not in parts:
        parts.insert(0, "-r")
    cmd          = [MAKEMKVCON_PATH] + parts
    if log_path is None:
        log_dir = get_mkvauto_root() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "makemkvcon.log"
    else:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    default_makemkv_log = get_mkvauto_root() / "logs" / "makemkvcon.log"
    try:
        if log_path.resolve() == default_makemkv_log.resolve():
            rotate_file_if_needed(log_path)
    except OSError:
        pass
    log_handle   = open(log_path, "a", buffering=1, encoding="utf-8")

    env = os.environ.copy()
    # Keep MakeMKV temp/lock files under our tmp dir (e.g., makemkv_drive.lock).
    env["TMPDIR"] = str(get_mkvauto_tmp())
    proc         = subprocess.Popen(cmd,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    text=True,
                                    bufsize=1,
                                    env=env)
    makemkv_pid = proc.pid  # Capture PID immediately after process creation
    if pid_callback is not None:
        try:
            pid_callback(makemkv_pid)
        except Exception as cb_exc:
            logger.warning(
                "run_makemkv pid_callback raised (continuing): %s", cb_exc
            )
    stall_seconds = get_rip_output_stall_seconds()
    stop_watchdog = threading.Event()
    stall_fired = threading.Event()
    last_line_lock = threading.Lock()
    last_line_at = [time.monotonic()]

    def _bump_last_stdout_line() -> None:
        with last_line_lock:
            last_line_at[0] = time.monotonic()

    def _stall_watchdog() -> None:
        if stall_seconds <= 0:
            return
        check_interval = min(10.0, max(1.0, stall_seconds / 5))
        while not stop_watchdog.wait(timeout=check_interval):
            with last_line_lock:
                idle = time.monotonic() - last_line_at[0]
            if idle >= stall_seconds:
                logger.warning(
                    "makemkvcon PID %s: no stdout for %.0fs (>= %ds); sending kill (stall watchdog)",
                    makemkv_pid,
                    idle,
                    stall_seconds,
                )
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                stall_fired.set()
                return

    _bump_last_stdout_line()
    if stall_seconds > 0:
        threading.Thread(
            target=_stall_watchdog,
            name="makemkv-stall-watchdog",
            daemon=True,
        ).start()

    full_output        = []
    pbar               = None
    disc_error_detected = False

    version_seen = None

    try:
        line_count = 0
        for line in proc.stdout:
            stripped = line.strip()
            _bump_last_stdout_line()
            full_output.append(line)
            line_count += 1
            try:
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                log_handle.write(f"[{ts}] {stripped}\n")
            except Exception:
                pass

            if line_cb:
                try:
                    line_cb(stripped)
                except Exception as cb_exc:
                    pass

            # progress handling
            m = PRGV_RE.match(stripped)
            if m:
                _, done, total = map(int, m.groups())
                if pbar is None:
                    pbar = tqdm(total=total, unit="units", dynamic_ncols=True)
                pbar.update(done - pbar.n)

            # detect the specific SCSI read error
            if DISC_ERR_RE.match(stripped):
                disc_error_detected = True

            # capture version banner if present so we can surface it on errors
            if "makemkv v" in stripped.lower():
                version_seen = stripped
    finally:
        stop_watchdog.set()
        rc = proc.wait()
        if pbar:
            pbar.close()
        try:
            log_handle.close()
        except Exception:
            pass

    if stall_fired.is_set():
        raise MakeMKVStallError(
            f"No output from MakeMKV for {stall_seconds}s; rip aborted as stalled."
        )

    # #802: zero usable drives beats every other diagnosis — the engine never
    # saw a drive, so disc-level advice would send the user chasing a clean
    # cloth for a kernel-module problem.
    #
    # Scoped to device-targeted commands (``dev:``) and checked regardless of
    # exit code, because makemkvcon reports "no usable optical drives" and
    # still exits 0 for an ``info dev:`` scan — verified on a CachyOS host with
    # the sg module unloaded, where the API happily returned 200 with a payload
    # whose info_log was nothing but MSG:5042. ``disc:9999`` enumeration is
    # deliberately excluded: "no drives" is a legitimate empty list there, not
    # an error. Living here rather than at the call sites so a third
    # ``info dev:`` caller cannot silently miss it — there were already two.
    if "dev:" in cmd_args:
        no_drives = diagnose_makemkv_no_drives(''.join(full_output))
        if no_drives:
            raise MakeMKVNoDrivesError(
                f"{no_drives}\n\nFull output:\n{''.join(full_output)}"
            )

    if rc != 0:
        # tailor the error for the common expiry/out-of-date case (253)
        if rc == 253:
            msg = (
                "MakeMKV is expired or out of date (exit code 253). "
                "Please update MakeMKV or enter a valid registration key.\n"
            )
            if version_seen:
                msg += f"Detected version: {version_seen}\n"
            msg += "\nFull output:\n" + ''.join(full_output)
            raise MakeMKVError(msg)

        msg = f"makemkvcon exited with code {rc}\nFull output:\n{''.join(full_output)}"
        if disc_error_detected:
            msg = (
                "makemkvcon read‐error detected (MSG:2003):\n"
                "It looks like the drive couldn’t read a stream—your disc may be dirty or scratched.\n"
                "Try cleaning the disc and running again.\n\n"
            ) + msg
        raise MakeMKVError(msg)

    return ''.join(full_output), makemkv_pid

def has_enough_space(path: str, required_bytes: int) -> bool:
    """
    Return True if the filesystem containing `path` has at least
    `required_bytes` bytes of free space, else False.

    :param path: Directory to check (must exist).
    :param required_bytes: Size in bytes you need.
    :raises FileNotFoundError: if `path` does not exist.
    :raises NotADirectoryError: if `path` isn’t a directory.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Path does not exist: {path}")
    if not os.path.isdir(path):
        raise NotADirectoryError(f"Not a directory: {path}")

    usage = shutil.disk_usage(path)
    return usage.free >= required_bytes
  
# MakeMKV MSG:3307's first numeric field is a flag bitmask. Bit 0x01000000 marks
# titles MakeMKV's BD-J emulator considers part of a suspected fake-playlist mass
# (playlist obfuscation, e.g. Lions Gate's Midway). Empirically:
# 205 of 213 Midway titles are flagged; the 8 unflagged are the m2ts extras +
# one legitimate mpls. Used as a "this is obfuscation territory" signal in Phase 1.
MSG_3307_OBFUSCATION_BIT = 0x01000000


def parse_log(log: str) -> dict:
    """
    Parse MakeMKV log to map title numbers to source files and obfuscation flag.

    MSG:3307 format: MSG:3307,<flag>,<num>,"File 00050.m2ts was added as title #24",...
    Returns a dict {title_id: {"file": str, "flag": int, "obfuscated": bool}}.
    The "flag" and "obfuscated" keys are absent on the legacy fallback regex (rare).
    """
    titles = {}
    # New format: MSG:3307,<flag>,<num>,"...","...","<source_file>","<title_num>"
    PAT_NEW = re.compile(
        r'^MSG:3307,(\d+),[^,]+(?:,"[^"]*")+,"([^"]+)","(\d+)"\s*$'
    )
    for line in log.splitlines():
        m = PAT_NEW.match(line)
        if m:
            flag_str, fn, tid = m.groups()
            try:
                tid_int = int(tid)
                flag = int(flag_str)
                titles[tid_int] = {
                    "file": fn,
                    "flag": flag,
                    "obfuscated": bool(flag & MSG_3307_OBFUSCATION_BIT),
                }
            except ValueError:
                pass
    # Fallback to old format if new format didn't match. Rare; flag isn't recoverable here.
    if not titles:
        PAT_OLD = re.compile(r'^MSG:3307,\d+,\d+,"File (\d{5}\.(?:mpls|m2ts)) was added as title #(\d+)"')
        for line in log.splitlines():
            m = PAT_OLD.match(line)
            if m:
                fn, tid = m.groups()
                titles[int(tid)] = {"file": fn}
    return titles


def extract_info_title(log: str) -> tuple[str | None, list[str]]:
    """
    Extract the disc title from CINFO lines. Prefer the explicit disc title code CINFO:2,0.
    Returns (info_title, matched_lines).
    """
    lines = log.splitlines()
    matched_lines: list[str] = []
    chosen: str | None = None
    for idx, line in enumerate(lines):
        m = re.search(r'CINFO:\s*2,0,"([^"]+)"', line)
        if not m:
            continue
        chosen = m.group(1).strip()
        matched_lines.append(line.strip())
        _trace("CINFO line[%s] -> %s", idx, chosen)
    if chosen:
        _trace("Chosen info_title=%s from CINFO lines=%s", chosen, matched_lines)
    return chosen, matched_lines


def parse_info_log(log: str) -> dict:
    """
    Parse a full MakeMKV info log into a structured payload:
      - titles_map: MSG3307 title -> source file mapping
      - scan_tracks: parsed TINFO/SINFO per title
      - info_title: disc-level title from CINFO (after last TCOUNT)
      - cinfo_lines: raw CINFO lines encountered
    """
    titles_map = parse_log(log)
    scan_tracks = parse_title_metadata(log)
    info_title, cinfo_lines = extract_info_title(log)
    return {
        "titles_map": titles_map,
        "scan_tracks": scan_tracks,
        "info_title": info_title,
        "cinfo_lines": cinfo_lines,
    }


TRACE_PARSE = os.getenv("MKVAUTO_PARSE_TRACE", "").lower() in ("1", "true", "yes")
parse_logger = logging.getLogger("core.utils.parse")


def _trace(msg: str, *args):
    if TRACE_PARSE:
        parse_logger.debug(msg, *args)


def parse_title_metadata(log: str) -> list[dict]:
    """
    Parse MakeMKV info log for per-title metadata (playlist/file, duration, size, display size, segment map, comment, language, chapters)
    and per-stream details from SINFO blocks.
    Returns a list of dicts keyed by title_id/track_id (playlist filename) when available.
    """
    file_map = parse_log(log)
    titles: dict[int, dict] = {}
    # Some makemkv builds emit three numeric fields, others emit four; accept both.
    tinfo_re = re.compile(r'^TINFO:(\d+),(\d+),(\d+)(?:,\d+)?,\s*"(.*)"$')
    sinfo_re = re.compile(r'^SINFO:(\d+),(\d+),(\d+)(?:,\d+)?,\s*"(.*)"$')
    # MSG:3307 format: MSG:3307,0,2,"File 00050.m2ts was added as title #24","File %1 was added as title #%2","00050.m2ts","24"
    # Extract second-to-last quoted field (source file) and last quoted field (title number)
    # Match all quoted fields and extract the last two
    # Capture flag bit too — bit 0x01000000 is the obfuscation marker (see parse_log).
    msg_playlist = re.compile(r'^MSG:3307,(\d+),[^,]+(?:,"[^"]*")+,"([^"]+)","(\d+)"\s*$')
    for line in log.splitlines():
        m = msg_playlist.match(line)
        if m:
            flag_str, fn, tid = m.groups()
            try:
                tid_i = int(tid)
                flag = int(flag_str)
                meta = titles.setdefault(tid_i, {})
                meta.setdefault("track_id", fn)
                meta.setdefault("title_id", fn)
                meta.setdefault("source_file", fn)
                meta.setdefault("index", tid_i)
                meta.setdefault("flag", flag)
                meta.setdefault("obfuscation_flag", bool(flag & MSG_3307_OBFUSCATION_BIT))
                _trace("MSG3307 title #%s -> source_file=%s flag=0x%x", tid_i, fn, flag)
            except ValueError:
                pass
    # Fallback to old format if new format didn't match
    if not any("source_file" in meta for meta in titles.values()):
        msg_playlist_old = re.compile(r'^MSG:3307,\d+,\d+,"File (\d{5}\.(?:mpls|m2ts)) was added as title #(\d+)"')
        for line in log.splitlines():
            m = msg_playlist_old.match(line)
            if m:
                fn, tid = m.groups()
                tid_i = int(tid)
                meta = titles.setdefault(tid_i, {})
                if "source_file" not in meta:
                    meta.setdefault("track_id", fn)
                    meta.setdefault("title_id", fn)
                    meta.setdefault("source_file", fn)
                    meta.setdefault("index", tid_i)
                    _trace("MSG3307 (old format) title #%s -> source_file=%s", tid_i, fn)
    # TINFO per-title attributes
    for line in log.splitlines():
        m = tinfo_re.match(line)
        if not m:
            continue
        tid_s, code_s, _sub, val = m.groups()
        try:
            tid = int(tid_s)
            code = int(code_s)
        except ValueError:
            continue
        meta = titles.setdefault(tid, {})
        meta.setdefault("index", tid)
        # Use file from file_map if available, otherwise fallback to title-{tid}
        file_from_map = file_map.get(tid, {}).get("file")
        meta.setdefault("track_id", file_from_map or f"title-{tid}")
        meta.setdefault("title_id", meta.get("track_id"))
        # Ensure source_file is set from file_map if not already set
        if file_from_map and "source_file" not in meta:
            meta["source_file"] = file_from_map
        if code == 9:  # duration
            meta["duration_raw"] = val.strip()
            meta["duration"] = coerce_duration_seconds(val.strip())
            _trace("TINFO:%s,9 -> duration=%s", tid, meta["duration_raw"])
        elif code == 11:  # size bytes
            try:
                meta["size"] = int(val.strip())
                _trace("TINFO:%s,11 -> size=%s", tid, meta["size"])
            except Exception:
                pass
        elif code == 10:  # display size
            meta["display_size"] = val.strip()
            _trace("TINFO:%s,10 -> display_size=%s", tid, meta["display_size"])
        elif code == 16:  # playlist filename
            meta["track_id"] = val.strip() or meta.get("track_id")
            meta["title_id"] = meta.get("track_id")
            meta["source_file"] = val.strip() or meta.get("source_file")
            _trace("TINFO:%s,16 -> track_id=%s", tid, meta["track_id"])
        elif code == 26:  # segment map
            meta["segment_map"] = val.strip()
            _trace("TINFO:%s,26 -> segment_map=%s", tid, meta["segment_map"])
        elif code == 27:  # comment
            meta["comment"] = val.strip()
            _trace("TINFO:%s,27 -> comment=%s", tid, meta["comment"])
        elif code == 28:  # language code
            meta["language_code"] = val.strip()
            _trace("TINFO:%s,28 -> language_code=%s", tid, meta["language_code"])
        elif code == 29:  # language
            meta["language"] = val.strip()
            _trace("TINFO:%s,29 -> language=%s", tid, meta["language"])
        elif code == 30:  # chapter summary string
            meta["chapters_info"] = val.strip()
            _trace("TINFO:%s,30 -> chapters_info=%s", tid, meta["chapters_info"])
        elif code == 10:  # display size
            meta["display_size"] = val.strip()
            _trace("TINFO:%s,10 -> display_size=%s", tid, meta["display_size"])
        elif code == 8:  # chapter count
            try:
                meta.setdefault("chapters", {})
                meta["chapters"]["count"] = int(val.strip())
                _trace("TINFO:%s,8 -> chapters.count=%s", tid, meta["chapters"]["count"])
            except Exception:
                pass
        elif code == 25:  # angle count / chapter count fallback
            try:
                meta.setdefault("chapters", {})
                meta["chapters"]["angles"] = int(val.strip())
                _trace("TINFO:%s,25 -> chapters.angles=%s", tid, meta["chapters"]["angles"])
            except Exception:
                pass
        elif code == 2:  # title name
            meta["title"] = val.strip()
            _trace("TINFO:%s,2 -> title=%s", tid, meta["title"])
    # SINFO per-stream attributes
    for line in log.splitlines():
        m = sinfo_re.match(line)
        if not m:
            continue
        tid_s, sid_s, code_s, val = m.groups()
        try:
            tid = int(tid_s)
            sid = int(sid_s)
            code = int(code_s)
        except ValueError:
            continue
        meta = titles.setdefault(tid, {})
        meta.setdefault("streams", [])
        while len(meta["streams"]) <= sid:
            meta["streams"].append({})
        stream = meta["streams"][sid] or {}
        meta["streams"][sid] = stream
        # map codes we care about
        if code == 1:
            stream["type"] = val
            _trace("SINFO:%s,%s,1 -> type=%s", tid, sid, val)
        elif code == 2:
            stream["audio_type"] = val
            _trace("SINFO:%s,%s,2 -> audio_type=%s", tid, sid, val)
        elif code == 3:
            stream["language_code"] = val
            _trace("SINFO:%s,%s,3 -> language_code=%s", tid, sid, val)
        elif code == 4:
            stream["language"] = val
            _trace("SINFO:%s,%s,4 -> language=%s", tid, sid, val)
        elif code == 5:
            stream["codec_short"] = val
            _trace("SINFO:%s,%s,5 -> codec_short=%s", tid, sid, val)
        elif code == 6:
            stream["codec_hint"] = val
            _trace("SINFO:%s,%s,6 -> codec_hint=%s", tid, sid, val)
        elif code == 7:
            stream["name"] = val
            _trace("SINFO:%s,%s,7 -> name=%s", tid, sid, val)
        elif code == 13:
            stream["bitrate"] = val
            _trace("SINFO:%s,%s,13 -> bitrate=%s", tid, sid, val)
        elif code == 14:
            try:
                stream["channels"] = int(val)
            except Exception:
                stream["channels"] = val
            _trace("SINFO:%s,%s,14 -> channels=%s", tid, sid, stream["channels"])
        elif code == 17:
            stream["sample_rate"] = val
            _trace("SINFO:%s,%s,17 -> sample_rate=%s", tid, sid, val)
        elif code == 18:
            stream["bit_depth"] = val
            _trace("SINFO:%s,%s,18 -> bit_depth=%s", tid, sid, val)
        elif code == 19:
            stream["resolution"] = val
            _trace("SINFO:%s,%s,19 -> resolution=%s", tid, sid, val)
        elif code == 20:
            stream["aspect_ratio"] = val
            _trace("SINFO:%s,%s,20 -> aspect_ratio=%s", tid, sid, val)
        elif code == 21:
            stream["frame_rate"] = val
            _trace("SINFO:%s,%s,21 -> frame_rate=%s", tid, sid, val)
        elif code == 22:
            stream["reference_frames"] = val
            _trace("SINFO:%s,%s,22 -> reference_frames=%s", tid, sid, val)
        elif code == 28:
            stream.setdefault("language_code", val)
            _trace("SINFO:%s,%s,28 -> language_code=%s", tid, sid, val)
        elif code == 29:
            stream.setdefault("language", val)
            _trace("SINFO:%s,%s,29 -> language=%s", tid, sid, val)
        elif code == 30:
            stream["description"] = val
            _trace("SINFO:%s,%s,30 -> description=%s", tid, sid, val)
        elif code == 31:
            stream["info"] = val
            _trace("SINFO:%s,%s,31 -> info=%s", tid, sid, val)
        elif code == 33:
            stream["duration_seconds"] = coerce_duration_seconds(val) if ":" in val else val
            _trace("SINFO:%s,%s,33 -> duration_seconds=%s", tid, sid, stream["duration_seconds"])
        elif code == 38:
            stream["flag"] = val
            _trace("SINFO:%s,%s,38 -> flag=%s", tid, sid, val)
        elif code == 39:
            stream["default"] = "default" in val.lower()
            _trace("SINFO:%s,%s,39 -> default=%s", tid, sid, stream["default"])
        elif code == 40:
            stream["layout"] = val
            _trace("SINFO:%s,%s,40 -> layout=%s", tid, sid, val)
        elif code == 42:
            stream["note"] = val
            _trace("SINFO:%s,%s,42 -> note=%s", tid, sid, val)
    # If duration is missing at the title level, fall back to the first stream duration.
    for meta in titles.values():
        if meta.get("duration") is None and meta.get("streams"):
            try:
                dur = meta["streams"][0].get("duration_seconds")
                meta["duration"] = float(dur) if dur is not None else None
                meta.setdefault("duration_raw", str(dur) if dur is not None else None)
            except Exception:
                pass
        # Fold chapter metadata into a single chapters dict
        if meta.get("chapters_info"):
            meta.setdefault("chapters", {})
            meta["chapters"]["summary"] = meta["chapters_info"]
    return list(titles.values())


def coerce_duration_seconds(val: str | None) -> float | None:
    """Convert a HH:MM:SS string to seconds."""
    if not val:
        return None
    parts = val.strip().split(":")
    try:
        parts = [int(p) for p in parts]
    except Exception:
        return None
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h = 0
        m, s = parts
    else:
        return None
    return float(h * 3600 + m * 60 + s)

def infer_resolution_from_log(log: str) -> tuple[str | None, str | None]:
    """
    Extract the highest video resolution from a makemkv info log and map it to a disc format.

    Returns (resolution_str, disc_format) where resolution_str is like "2160p"/"1080p".
    """
    # SINFO lines contain widthxheight at field 19, e.g., SINFO:0,0,19,0,"3840x2160"
    pat = re.compile(r'^SINFO:\d+,\d+,\d+,\d+,"(?P<w>\d{3,4})x(?P<h>\d{3,4})"')
    max_h = 0
    for line in log.splitlines():
        m = pat.match(line)
        if m:
            try:
                h = int(m.group("h"))
                if h > max_h:
                    max_h = h
            except ValueError:
                continue
    if max_h <= 0:
        return None, None
    if max_h >= 2000:
        return "2160p", "UHD"
    if max_h >= 1000:
        return "1080p", "Blu-Ray"
    if max_h >= 700:
        return "720p", "Blu-Ray"
    if max_h >= 500:
        return "576p", "DVD"
    return "480p", "DVD"

def _resolve_mount_point(path: str) -> str | None:
    # if it's already a directory, done
    if os.path.isdir(path):
        return path
    # otherwise try to find a matching device in /proc/mounts
    try:
        with open('/proc/mounts', 'r') as mounts:
            for line in mounts:
                dev, mnt, *_ = line.split()
                if dev == path and os.path.isdir(mnt):
                    return mnt
    except OSError:
        pass
    return None


def get_disc_size_bytes_for_mount_point(mount_point: str) -> int | None:
    """
    Return disc capacity in bytes for the given mount point, or None if unavailable.
    Resolves mount_point to the block device (using only mount_point; no disc_num)
    and reads /sys/class/block/<device>/size (sectors * 512).
    Used by both drive_manager (standalone) and _drive_operations (in-process) scan paths.
    """
    try:
        resolved = Path(mount_point).resolve()
    except Exception:
        return None

    device_name = None
    # Block device path (e.g. /dev/sr1)
    try:
        if os.path.exists(mount_point):
            st = os.stat(mount_point)
            if stat.S_ISBLK(st.st_mode):
                device_name = resolved.name
    except OSError:
        pass

    if not device_name:
        # Directory or path: find device from /proc/mounts (longest matching mount)
        try:
            with open("/proc/mounts", "r") as f:
                lines = f.readlines()
        except OSError:
            return None
        target = str(resolved)
        best_dev = None
        best_len = -1
        for line in lines:
            parts = line.split()
            if len(parts) < 2:
                continue
            dev, mnt = parts[0], parts[1].replace("\\040", " ")
            try:
                mnt_resolved = str(Path(mnt).resolve())
            except Exception:
                continue
            if target == mnt_resolved or (mnt_resolved and target.startswith(mnt_resolved + os.sep)):
                if len(mnt_resolved) > best_len:
                    best_len = len(mnt_resolved)
                    best_dev = dev
        if not best_dev:
            return None
        device_name = Path(best_dev).name
        sys_block = Path("/sys/class/block") / device_name
        if sys_block.exists():
            try:
                real = os.path.realpath(sys_block)
                device_name = Path(real).name
            except OSError:
                pass

    if not device_name:
        logger.debug("disc_size_bytes: no block device for mount_point=%s", mount_point)
        return None
    size_path = Path("/sys/class/block") / device_name / "size"
    try:
        if size_path.exists():
            sectors = int(size_path.read_text().strip())
            if sectors > 0:
                return sectors * 512
    except Exception:
        pass
    logger.debug("disc_size_bytes: could not read size for device=%s mount_point=%s", device_name, mount_point)
    return None


def hash_media_disc(mount_point: str, allow_reentrant: bool = False) -> str:
    """
    Compute a disc hash by MD5'ing the file sizes in a BDMV/STREAM folder or VIDEO_TS.
    mount_point may be a mount directory or a device node (/dev/sr1).
    """
    # Lock files removed - using in-memory state tracking instead
    # State checking is handled by drive manager
    # No need to check locks here since drive manager handles state
    
    # Helper function to perform the actual hashing work (extracted to avoid duplication)
    def _do_hash_work():
        func_logger = get_logger("core.utils", "_do_hash_work")
        func_logger.debug("_do_hash_work called mount_point=%s allow_reentrant=%s", mount_point, allow_reentrant)
        real_mp = _resolve_mount_point(mount_point)
        func_logger.debug("Mount point resolved mount_point=%s real_mp=%s", mount_point, real_mp)
        mounted_here = False
        mounted_via_helper = False
        tmp_mount_dir: str | None = None

        logger.info("Hashing disc for mount_point=%s", mount_point)

        # if not mounted but is a block device, try to mount read-only temporarily
        if not real_mp and os.path.exists(mount_point):
            st = os.stat(mount_point)
            if stat.S_ISBLK(st.st_mode):
                # try root helper first
                logger.info("Attempting helper mount for %s", mount_point)
                helper_mp, helper_err = _root_helper_mount(mount_point)
                if helper_mp:
                    real_mp = helper_mp
                    mounted_here = True
                    mounted_via_helper = True
                    logger.info("Helper mount succeeded at %s", real_mp)
                else:
                    # Helper mount failed - possibly disc not ready yet
                    # Add small delay before local mount attempt to let disc stabilize
                    if helper_err and "exit status 32" in str(helper_err):
                        logger.info("Helper mount failed with exit 32 (disc may not be ready), waiting 2s before retry")
                        import time
                        time.sleep(2)
                    
                    logger.info("Helper mount failed (%s); attempting local mount for %s", helper_err or "unknown reason", mount_point)
                    tmp_dir = tempfile.mkdtemp(prefix="makemkv-mount-")
                    mount_succeeded = False
                    
                    # Try UDF first (most common for Blu-ray/DVD)
                    try:
                        subprocess.run(
                            ["mount", "-o", "ro", "-t", "udf", mount_point, tmp_dir],
                            check=True,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=MOUNT_TIMEOUT,
                        )
                        real_mp = tmp_dir
                        mounted_here = True
                        tmp_mount_dir = tmp_dir
                        mount_succeeded = True
                        logger.info("Mounted %s at %s (udf)", mount_point, real_mp)
                    except subprocess.TimeoutExpired:
                        logger.warning("Mount timed out after %ss for %s (dir=%s)", MOUNT_TIMEOUT, mount_point, tmp_dir)
                        # best-effort cleanup of the mount point we created
                        try:
                            subprocess.run(["umount", "-l", tmp_dir], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except Exception:
                            pass
                        try:
                            Path(tmp_dir).rmdir()
                        except Exception:
                            pass
                        # try to reset the stuck device so subsequent attempts can succeed
                        try:
                            _root_helper_reset_device(mount_point)
                        except Exception:
                            pass
                        raise MakeMKVError(f"Drive is not responding (mount timed out after {MOUNT_TIMEOUT}s). Try power cycling the drive.") from None
                    except subprocess.CalledProcessError as e:
                        logger.warning("Local mount failed with UDF for %s (exit=%s), trying auto-detect", mount_point, e.returncode)
                        # UDF failed, try with auto filesystem detection (DVD might be iso9660)
                        try:
                            subprocess.run(
                                ["mount", "-o", "ro", mount_point, tmp_dir],
                                check=True,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                timeout=MOUNT_TIMEOUT,
                            )
                            real_mp = tmp_dir
                            mounted_here = True
                            tmp_mount_dir = tmp_dir
                            mount_succeeded = True
                            logger.info("Mounted %s at %s (auto-detect)", mount_point, real_mp)
                        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e2:
                            logger.warning("Local mount failed with auto-detect for %s: %s", mount_point, e2)
                            try:
                                Path(tmp_dir).rmdir()
                            except Exception:
                                pass

        if not real_mp:
            logger.warning("No mount point found for %s and auto-mount failed", mount_point)
            detail = f"No mount point found for {mount_point!r} (and auto-mount failed)"
            if locals().get("helper_err"):
                detail = f"No mount point found for {mount_point!r} (helper: {helper_err})"
            func_logger.debug("No mount point found mount_point=%s detail=%s", mount_point, detail)
            raise FileNotFoundError(detail)

        try:
            # look for Blu-ray or DVD layout
            br = os.path.join(real_mp, "BDMV", "STREAM")
            dv = os.path.join(real_mp, "VIDEO_TS")
            if os.path.isdir(br):
                path_dir, ext = br, ".m2ts"
            elif os.path.isdir(dv):
                path_dir, ext = dv, None
            else:
                error_msg = f"No Blu-ray or DVD structure under {real_mp!r}"
                func_logger.debug("No BDMV/STREAM or VIDEO_TS found mount_point=%s real_mp=%s br_exists=%s dv_exists=%s error_msg=%s", 
                                mount_point, real_mp, os.path.isdir(br) if 'br' in locals() else False, os.path.isdir(dv) if 'dv' in locals() else False, error_msg)
                raise FileNotFoundError(error_msg)

            # collect and sort the files
            files = sorted(
                f for f in os.listdir(path_dir)
                if ext is None or f.lower().endswith(ext)
            )

            # build the hash from each file's size (8-byte little endian)
            md5 = hashlib.md5()
            for fname in files:
                full = os.path.join(path_dir, fname)
                size = os.path.getsize(full)
                md5.update(struct.pack("<Q", size))

            result = md5.hexdigest().upper()
            logger.info("Disc hash for %s = %s", mount_point, result)
            func_logger.debug("Hash computed successfully mount_point=%s result=%s file_count=%s", mount_point, result, len(files))
            return result
        finally:
            if mounted_here:
                try:
                    if mounted_via_helper:
                        _root_helper_unmount(real_mp)
                    else:
                        subprocess.run(["umount", real_mp], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        if tmp_mount_dir:
                            Path(tmp_mount_dir).rmdir()
                except subprocess.CalledProcessError:
                    logger.warning("Failed to unmount temporary mount at %s", real_mp)
                except Exception:
                    # keep cleanup best-effort; avoid masking original errors
                    pass
    
    # Lock files removed - using in-memory state tracking instead
    # State checking is handled by drive manager
    # No need to check locks here since drive manager handles state
    func_logger = get_logger("core.utils", "hash_media_disc")
    func_logger.debug("About to call _do_hash_work mount_point=%s allow_reentrant=%s", mount_point, allow_reentrant)
    try:
        result = _do_hash_work()
        func_logger.debug("_do_hash_work returned mount_point=%s result=%s", mount_point, result)
        return result
    except Exception as exc:
        func_logger.debug("Exception in hash_media_disc mount_point=%s error=%s error_type=%s", mount_point, str(exc), type(exc).__name__)
        raise


def _root_helper_request(payload: dict) -> Optional[dict]:
    # Accept multiple well-known socket locations so env mismatches don't break mounts.
    candidates = [
        os.getenv("MAKEMKV_ROOT_HELPER_SOCK", "/run/makemkv_updater.sock"),
        "/run/makemkv_helper.sock",   # legacy name used by some setups
        "/tmp/makemkv_auto.sock",     # default from manage.sh
        "/tmp/makemkv_updater.sock",  # original fallback
    ]
    seen = set()
    errors = []
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                logger.debug("Root helper: trying socket %s", path)
                s.settimeout(2)
                s.connect(path)
                logger.debug("Root helper: connected to %s", path)
                # Allow longer for responses that trigger mount/umount work; mount timeout + buffer.
                s.settimeout(MOUNT_TIMEOUT + 5)
                s.sendall((json.dumps(payload) + "\n").encode())
                data = s.recv(1024 * 1024)
                if not data:
                    errors.append(f"{path}: empty response")
                    continue
                parsed = json.loads(data.decode().strip())
                logger.debug("Root helper connected via %s, response: %s", path, data.decode(errors='ignore').strip()[:500])
                return parsed
        except socket.timeout as exc:
            errors.append(f"{path}: connect/recv timed out ({exc})")
            logger.debug("Root helper timeout on %s: %s", path, exc)
            continue
        except Exception as exc:
            errors.append(f"{path}: {exc}")
            logger.debug("Root helper error on %s: %s", path, exc)
            continue
    if errors:
        logger.debug("Root helper unavailable; tried %s", ", ".join(errors))
    return None


def _root_helper_mount(device: str) -> tuple[Optional[str], Optional[str]]:
    resp = _root_helper_request({"cmd": "mount", "device": device})
    if not resp:
        return None, "root helper unavailable"
    if resp.get("status") == "ok":
        return resp.get("mount_point"), None
    err = resp.get("error") or "root helper mount failed"
    logger.warning("Root helper mount failed for %s: %s", device, err)
    return None, err


def _root_helper_unmount(mount_point: str) -> None:
    resp = _root_helper_request({"cmd": "unmount", "mount_point": mount_point})
    if resp and resp.get("status") != "ok":
        logger.debug("Root helper unmount error for %s: %s", mount_point, resp)

def _root_helper_reset_device(device: str) -> None:
    """Ask the root helper to delete/rescan a stuck block device."""
    resp = _root_helper_request({"cmd": "reset_device", "device": device})
    if resp and resp.get("status") != "ok":
        logger.debug("Root helper reset_device error for %s: %s", device, resp)


def _root_helper_mount_smb(host: str, share: str, mount_point: str, port: int, username: str, password: str, domain: str) -> tuple[Optional[str], Optional[str]]:
    """Ask the root helper to mount an SMB/CIFS share."""
    resp = _root_helper_request({
        "cmd": "mount_smb",
        "host": host,
        "share": share,
        "mount_point": mount_point,
        "port": port,
        "username": username,
        "password": password,
        "domain": domain
    })
    if not resp:
        return None, "root helper unavailable"
    if resp.get("status") == "ok":
        return resp.get("mount_point"), None
    err = resp.get("error") or "root helper SMB mount failed"
    logger.warning("Root helper SMB mount failed for %s/%s: %s", host, share, err)
    return None, err


def _root_helper_mount_nfs(server: str, export_path: str, mount_point: str, options: str = "") -> tuple[Optional[str], Optional[str]]:
    """Ask the root helper to mount an NFS share."""
    resp = _root_helper_request({
        "cmd": "mount_nfs",
        "server": server,
        "export_path": export_path,
        "mount_point": mount_point,
        "options": options
    })
    if not resp:
        return None, "root helper unavailable"
    if resp.get("status") == "ok":
        return resp.get("mount_point"), None
    err = resp.get("error") or "root helper NFS mount failed"
    logger.warning("Root helper NFS mount failed for %s:%s: %s", server, export_path, err)
    return None, err

def retrieve_discdb_data(content_hash: str) -> dict:
    """
    Fetch disc metadata from DiscDB GraphQL by content hash.

    Returns the overarching media item, the release(s) that contain a disc with this
    content hash, and all discs within each release (no filter on discs — full list).
    """
    transport = RequestsHTTPTransport(url=DISKDBURL, headers={"Content-Type": "application/json"}, use_json=True)
    client = Client(transport=transport, fetch_schema_from_transport=False)
    query = gql("""
      query GetDiscByHash($contentHash: String!) {
        mediaItems(
          where: {
            releases: {
              some: {
                discs: { some: { contentHash: { eq: $contentHash } } }
              }
            }
          }
        ) {
          nodes {
            id
            title
            imageUrl
            year
            releaseDate
            externalids {
              tmdb
              imdb
            }
            releases(
              where: {
                discs: { some: { contentHash: { eq: $contentHash } } }
              }
            ) {
              id
              slug
              releaseDate
              year
              imageUrl
              boxset {
                id
                slug
                title
                sortTitle
                imageUrl
                type
                releaseId
                release {
                  id
                  slug
                  title
                  year
                  upc
                  asin
                  imageUrl
                  releaseDate
                  locale
                  regionCode
                }
              }
              discs(order: { index: ASC }) {
                index
                name
                format
                slug
                contentHash
                globalDiscId
                id
                titles {
                  __typename
                  ... on Title { id }
                  disc {
                    contentHash
                    format
                    id
                    index
                    name
                    slug
                  }
                comment
                description
                discItemReferenceId
                displaySize
                duration
                episode
                hasItem
                id
                index
                itemType
                season
                segmentMap
                size
                sourceFile
                item {
                episode
                season
                title
                type
                id
                description
                }

                }
              }
            }
            type
          }
        }
      }
    """)
    try:
        result = client.execute(query, variable_values={"contentHash": content_hash})
    except Exception as e:
        raise
    return result


def _discdb_image_url(image_url: str | None) -> str | None:
    if not image_url or not isinstance(image_url, str):
        return None
    s = image_url.strip()
    if not s:
        return None
    if s.startswith("http://") or s.startswith("https://"):
        return s
    return f"https://thediscdb.com/images/{s}"


def _extract_discdb_boxset_payload(release: dict) -> dict | None:
    """
    Map TheDiscDB Release.boxset + nested packaging release into a dict for crud.upsert_discdb_boxset_candidate.
    Returns None when this release is not part of a boxset in DiscDB.
    """
    from core.release_link_validation import normalize_gtin_from_discdb

    bs = release.get("boxset")
    if not bs or not isinstance(bs, dict):
        return None
    pr = bs.get("release")
    pr = pr if isinstance(pr, dict) else None

    title = (bs.get("title") or "").strip() or None
    discdb_slug = (bs.get("slug") or "").strip() or None
    bid = bs.get("id")
    # Stable DB slug for upsert (boxsets.slug is not unique — prefer DiscDB id when present).
    if bid is not None:
        slug = f"discdb-boxset-{bid}"
    elif discdb_slug:
        slug = f"discdb-bs-{discdb_slug}"
    elif title:
        slug = f"discdb-bs-{slugify(title) or 'unknown'}"
    else:
        slug = None

    cover_bs = _discdb_image_url(bs.get("imageUrl"))
    cover_pr = _discdb_image_url(pr.get("imageUrl")) if pr else None
    cover_front = cover_bs or cover_pr

    year = None
    if pr and pr.get("year") is not None:
        try:
            yi = int(pr["year"])
            if 1000 <= yi <= 9999:
                year = yi
        except (TypeError, ValueError):
            pass

    upc_raw = pr.get("upc") if pr else None
    upc = normalize_gtin_from_discdb(upc_raw)

    return {
        "discdb_boxset_id": bid,
        "slug": slug,
        "title": title,
        "name": title,
        "sort_title": (bs.get("sortTitle") or "").strip() or None,
        "discdb_boxset_type": bs.get("type"),
        "discdb_slug": discdb_slug,
        "cover_front_url": cover_front,
        "upc": upc,
        "asin": (pr.get("asin") if pr else None) or None,
        "year": year,
        "locale": pr.get("locale") if pr else None,
        "region_code": str(pr["regionCode"]) if pr and pr.get("regionCode") is not None else None,
        "packaging_release_slug": (pr.get("slug") if pr else None),
    }


def parse_discdb_data(raw: dict, target_hash: str | None = None):
    """
    Extract movie name and mapping of sourceFile -> {season, episode, format, episode_name}.
    Also return all discs in the release (with metadata and track mappings) so the UI
    can render discs that have not been ripped yet.
    """
    nodes = raw.get("mediaItems", {}).get("nodes") or []
    if not nodes:
        raise Exception("DiscDB: no match found for content hash")
    if len(nodes) > 1:
        raise Exception("DiscDB: multiple matches returned for content hash")

    node = nodes[0]
    releases = node.get("releases") or []
    if not releases:
        raise Exception("DiscDB: release data missing for content hash")
    release = releases[0]
    boxset_payload = _extract_discdb_boxset_payload(release)
    orig_year = node.get("year")
    orig_release_date = node.get("releaseDate")

    discs = release.get("discs") or []
    if not discs:
        raise Exception("DiscDB: disc list missing for release")
    release_slug = release.get("slug")
    release_year = release.get("year")
    release_date = release.get("releaseDate")

    movie_name = (node.get("title") or "").strip()  # MediaItem.title is the movie name
    release_image = None
    if release.get("imageUrl"):
        release_image = f"https://thediscdb.com/images/{release.get('imageUrl')}"
    media_type = node.get("type")  # Renamed from 'type' to avoid shadowing built-in type()
    
    # Extract movie/TMDB data from DiscDB response
    # externalids (lowercase) is an ExternalIds object on MediaItem that contains tmdb entry
    # externalids only exists on MediaItem, not on Release
    external_ids = node.get("externalids")
    
    tmdb_id = None
    tmdb_type = None
    if external_ids:
        if isinstance(external_ids, dict):
            # Check if tmdb is a nested object or direct field
            tmdb_data = external_ids.get("tmdb")
            if isinstance(tmdb_data, dict):
                tmdb_id = tmdb_data.get("id") or tmdb_data.get("tmdbId")
                tmdb_type = tmdb_data.get("type") or tmdb_data.get("tmdbType")
            elif isinstance(tmdb_data, str):
                # Try to parse as JSON first (in case it's a JSON string)
                try:
                    parsed = json.loads(tmdb_data)
                    if isinstance(parsed, dict):
                        tmdb_id = parsed.get("id") or parsed.get("tmdbId")
                        tmdb_type = parsed.get("type") or parsed.get("tmdbType")
                    else:
                        tmdb_id = str(parsed)
                        tmdb_type = "movie" if media_type == "Movie" else "tv"
                except (json.JSONDecodeError, ValueError):
                    # If tmdb is just a string ID, try to infer type from context
                    tmdb_id = tmdb_data
                    tmdb_type = "movie" if media_type == "Movie" else "tv"
        elif isinstance(external_ids, list):
            # If externalids is a list, find tmdb entry
            for item in external_ids:
                if isinstance(item, dict) and item.get("source") == "tmdb":
                    tmdb_id = item.get("id")
                    tmdb_type = item.get("type")
                    break
    
    # Use original_year (from mediaItem) or release_year as production_year for movie lookup
    production_year = orig_year or release_year

    def _build_tracks(title_list: list[dict]) -> dict:
        """Map sourceFile -> metadata only. Omit DiscDB order/index fields — MakeMKV scan owns index."""
        mapping: dict = {}
        for t in title_list or []:
            if t.get("itemType") != "":
                sf = t.get("sourceFile")
                if not sf:
                    continue
                logger.debug("DiscDB title item: %s", t.get("item"))
                item_title = t.get("item", {}).get("title")
                top_title = t.get("title")
                eff = None
                if top_title is not None and str(top_title).strip():
                    eff = str(top_title).strip()
                elif item_title is not None and str(item_title).strip():
                    eff = str(item_title).strip()
                mapping[sf] = {
                    "type": t.get("itemType"),
                    "season": t.get("season"),
                    "episode": t.get("episode"),
                    "format": t.get("format"),
                    "episode_name": eff,
                    "title": eff,
                }
        return mapping

    release_discs: list[dict] = []
    disc_slug = release_slug
    resolution = None
    disc_format = None
    mapping: dict = {}
    matched_disc_index = None  # Disc number (1-based) from DiscDB for the disc that matched target_hash

    # Find highest resolution across ALL discs in the release (for release-level resolution)
    release_resolution = None
    resolution_priority = {"2160p": 3, "1080p": 2, "720p": 1, "576p": 0, "480p": 0}
    highest_priority = -1

    for disc in discs:
        disc_title_map = _build_tracks(disc.get("titles") or [])
        content_hash = disc.get("contentHash")
        disc_fmt = (disc.get("format") or "").strip() or None
        
        # Calculate resolution for this disc
        disc_res = None
        if disc_fmt == "UHD":
            disc_res = "2160p"
        elif disc_fmt == "Blu-Ray":
            disc_res = "1080p"
        elif disc_fmt == "DVD":
            disc_res = "480p"
        
        # Track highest resolution across all discs for release-level resolution
        if disc_res and disc_res in resolution_priority:
            priority = resolution_priority[disc_res]
            if priority > highest_priority:
                highest_priority = priority
                release_resolution = disc_res
        
        disc_entry = {
            "index": disc.get("index"),
            "name": disc.get("name"),
            "format": disc_fmt,
            "slug": disc.get("slug") or release_slug,
            "content_hash": content_hash,
            "id": disc.get("id"),
            "tracks": disc_title_map,
        }
        release_discs.append(disc_entry)

        if target_hash and content_hash == target_hash:
            mapping = disc_title_map
            disc_slug = disc_entry["slug"]
            disc_format = disc_fmt
            resolution = disc_res  # Use the calculated disc_res
            matched_disc_index = disc_entry.get("index")

    # Fallback to first disc if we didn't find a match for the target hash.
    if not mapping and release_discs:
        first_disc = release_discs[0]
        mapping = first_disc.get("tracks", {})
        disc_slug = first_disc.get("slug")
        disc_format = first_disc.get("format")
        # Resolution already calculated above
        if not resolution:
            if disc_format == "UHD":
                resolution = "2160p"
            elif disc_format == "Blu-Ray":
                resolution = "1080p"
            elif disc_format == "DVD":
                resolution = "480p"

    # Use release_resolution (highest across all discs) if available, otherwise fall back to current disc resolution
    final_resolution = release_resolution or resolution
    
    return (
        movie_name,  # Movie name (replaces legacy show_title)
        release_image,  # Release cover image (replaces legacy show_image)
        disc_slug,
        mapping,
        resolution,  # Current disc resolution (for disc-level info)
        disc_format,
        media_type,  # Renamed from 'type' to avoid shadowing built-in type()
        release_slug,
        release_year,
        release_date,
        orig_year,
        orig_release_date,
        release_discs,
        tmdb_id,  # Movie metadata from DiscDB
        final_resolution,  # Release-level resolution (highest across all discs)
        tmdb_type,
        production_year,
        matched_disc_index,  # Disc number from DiscDB for the matched disc (1-based), or None
        boxset_payload,  # For Phase B: TheDiscDB boxset → DB Boxset + Release.boxset_id
    )


def _parse_drv_output_for_loaded_discs(output: str) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """
    Parse MakeMKV robot output from ``info disc:9999`` (or equivalent) into
    (makemkv_index, mount_point) pairs and a hardware-name map.

    Omits drives with no volume label (empty disc name in DRV) and non-/dev/sr* paths.
    """
    drives: list[tuple[str, str]] = []
    hardware: dict[str, str] = {}
    seen_devs: set[str] = set()
    for line in output.splitlines():
        if not line.startswith("DRV:"):
            continue
        m = _DRV_LINE_RE.match(line)
        if not m:
            continue
        idx, hw_name, vol_label, dev_path = m.groups()
        dev_path = dev_path.strip()
        vol_label = (vol_label or "").strip()
        if not vol_label:
            continue
        if not dev_path.startswith("/dev/sr"):
            continue
        if dev_path in seen_devs:
            continue
        if not _drive_has_media(dev_path):
            continue
        seen_devs.add(dev_path)
        drives.append((idx, dev_path))
        hardware[dev_path] = (hw_name or "").strip()
    return drives, hardware


def _run_makemkv_disc_9999_enumeration(*, log_path: Path) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Single ``makemkvcon info disc:9999`` and DRV parse for loaded, labeled discs."""
    minlen = int(os.getenv("MKVAUTO_MIN_TITLE_LENGTH", "0"))
    output, _ = run_makemkv(
        f"info disc:9999 --cache=1 --minlength={minlen}",
        log_path=log_path,
    )
    return _parse_drv_output_for_loaded_discs(output)


def makemkv_index_for_mount(mount_point: str) -> str | None:
    """Resolve MakeMKV drive index for a block device using the last enumeration in ``_last_drive_scan``."""
    mp = (mount_point or "").strip()
    if not mp:
        return None
    for idx, path in _last_drive_scan.get("drives") or []:
        if path == mp:
            return str(idx)
    return None


def refresh_drive_enumeration_disc_9999(*, log_path: Path | None = None) -> list[tuple[str, str]]:
    """
    Run ``info disc:9999`` and refresh ``_last_drive_scan`` (drives + hardware).
    Used when resolving mount_point → index for ``info disc:{N}`` scans.
    """
    path = log_path or (get_mkvauto_tmp() / "makemkv_disc_9999_refresh.log")
    drives, hardware = _run_makemkv_disc_9999_enumeration(log_path=path)
    _last_drive_scan["ts"] = time.time()
    _last_drive_scan["drives"] = drives
    _last_drive_scan["drive_hardware"] = hardware
    _last_drive_scan["fail_count"] = 0
    logger.info("disc:9999 enumeration refresh: %s", drives)
    return drives


def ensure_makemkv_index_for_mount(
    mount_point: str,
    *,
    refresh_enumeration_first: bool = False,
    max_cache_age_seconds: Optional[float] = None,
    log_path: Optional[Path] = None,
) -> Optional[str]:
    """
    Return MakeMKV index for ``mount_point`` using ``_last_drive_scan``.

    If ``refresh_enumeration_first`` is True, run ``info disc:9999`` before lookup so path→index
    matches MakeMKV after hot-plug or DRV renumbering.

    If ``max_cache_age_seconds`` is set and the last enumeration is older, refresh first.

    If the path is still unknown after any forced refresh, run ``disc:9999`` once more (legacy
    fallback) unless we already refreshed in this call.
    """
    mp = (mount_point or "").strip()
    if not mp:
        return None

    need_global_refresh = bool(refresh_enumeration_first)
    if not need_global_refresh and max_cache_age_seconds is not None:
        ts = _last_drive_scan.get("ts") or 0
        try:
            age = time.time() - float(ts)
        except (TypeError, ValueError):
            age = max_cache_age_seconds + 1.0
        if not ts or age > float(max_cache_age_seconds):
            need_global_refresh = True

    if need_global_refresh:
        try:
            refresh_drive_enumeration_disc_9999(log_path=log_path)
        except Exception as exc:
            logger.warning("disc:9999 refresh failed while resolving mount %s: %s", mp, exc)

    idx = makemkv_index_for_mount(mp)
    if idx is not None:
        return idx

    if need_global_refresh:
        return None

    try:
        refresh_drive_enumeration_disc_9999(log_path=log_path)
    except Exception as exc:
        logger.warning("disc:9999 refresh failed while resolving mount %s: %s", mp, exc)
        return None
    return makemkv_index_for_mount(mp)


def parse_drv_fields_for_mount(output: str, mount_point: str) -> tuple[str | None, str | None, str | None]:
    """
    Parse robot ``DRV:`` lines and return ``(makemkv_index, drive_hardware_name, volume_label)``
    for the line whose device path matches ``mount_point``.
    """
    mp = (mount_point or "").strip()
    if not mp:
        return None, None, None
    text = output if isinstance(output, str) else "\n".join(output) if isinstance(output, list) else str(output)
    for line in text.splitlines():
        if not line.startswith("DRV:"):
            continue
        m = _DRV_LINE_RE.match(line)
        if not m:
            continue
        idx, hw_name, vol_label, dev_path = m.groups()
        if dev_path.strip() == mp:
            hw = (hw_name or "").strip()
            vl = (vol_label or "").strip()
            return str(idx), (hw or None), (vl or None)
    return None, None, None


def makemkv_index_from_drv_lines_for_mount(output: str, mount_point: str) -> str | None:
    """Parse robot ``DRV:`` lines and return MakeMKV index for the given device path."""
    idx, _, _ = parse_drv_fields_for_mount(output, mount_point)
    return idx


def upsert_makemkv_drive_cache_for_mount(
    mount_point: str,
    makemkv_index: str,
    drive_hardware_name: str | None = None,
) -> None:
    """
    Update ``_last_drive_scan`` for a single device after ``info dev:{mount}`` (or equivalent).

    Replaces the tuple for ``mount_point`` or appends it so path→MakeMKV index stays current
    when DRV indices change without a global ``disc:9999`` run.
    """
    mp = (mount_point or "").strip()
    idx = str(makemkv_index).strip()
    if not mp or not idx:
        return
    prev = list(_last_drive_scan.get("drives") or [])
    out: list[tuple[str, str]] = []
    replaced = False
    for cur_idx, path in prev:
        if path == mp:
            out.append((idx, mp))
            replaced = True
        else:
            out.append((cur_idx, path))
    if not replaced:
        out.append((idx, mp))
    _last_drive_scan["drives"] = out
    hw_map = dict(_last_drive_scan.get("drive_hardware") or {})
    if drive_hardware_name is not None:
        hw_map[mp] = (drive_hardware_name or "").strip()
    _last_drive_scan["drive_hardware"] = hw_map
    _last_drive_scan["ts"] = time.time()
    logger.debug("upsert_makemkv_drive_cache_for_mount mount=%s idx=%s hw=%s", mp, idx, drive_hardware_name)


def get_drives() -> list:
    """
    List drives with media via the OS-level drive registry (#562).

    Returns ``[(synthesized_ordinal_str, mount_point), ...]`` sorted by
    ``mount_point``. The MakeMKV engine is **not** invoked — the registry
    answers from ``/sys/block``, ``/dev/disk/by-id``, ``sg_turs``, and
    ``udevadm``. The synthesized ordinal is a stable sort position, **not**
    the MakeMKV DRV index; the real DRV index is rediscovered lazily by
    ``upsert_makemkv_drive_cache_for_mount`` during per-drive ``info dev:``
    scans (see ``core._drive_operations._load_discinfo``).

    Side benefit: this endpoint no longer blocks on MakeMKV contention with
    in-flight rips — the #545 "drives endpoint empty during rip" failure
    mode disappears.
    """
    snapshots = sorted(_registry_loaded_drives(), key=lambda s: s.mount_point)
    return [(str(i), snap.mount_point) for i, snap in enumerate(snapshots)]


def get_drive_hardware_map() -> dict[str, str]:
    """Device path -> MakeMKV DRV hardware name (last successful get_drives probe)."""
    return dict(_last_drive_scan.get("drive_hardware") or {})


def build_drive_api_dict(disc_idx: str, mount_point: str) -> dict:
    """
    Single drive entry for API / UI: MakeMKV index, path, hardware label, friendly name,
    plus the stable hardware identity used by the multi-drive gatekeeper (#540).
    """
    hw_from_makemkv = (get_drive_hardware_map().get(mount_point) or "").strip()
    identity = resolve_drive_identity(
        mount_point, hardware_name=hw_from_makemkv or None
    )

    # Cosmetic fallback for #562 PR 2: after the registry takes over /drives,
    # the MakeMKV-side hardware label is only populated lazily by per-drive
    # ``info dev:`` scans. Until that runs we surface the by-id vendor+model
    # so the UI shows something meaningful instead of just the mount point.
    if hw_from_makemkv:
        hw = hw_from_makemkv
    elif identity.vendor or identity.model:
        hw = " ".join(p for p in (identity.vendor, identity.model) if p).strip()
    else:
        hw = ""

    try:
        n = int(str(disc_idx).strip())
        friendly = f"Drive {n + 1}"
    except ValueError:
        friendly = f"Drive {disc_idx}"
    parts = [p for p in (hw, mount_point, friendly) if p]
    name = " — ".join(parts) if len(parts) > 1 else (hw or friendly)

    return {
        "disc_num": str(disc_idx),
        "mount_point": mount_point,
        "makemkv_disc_index": str(disc_idx),
        "drive_hardware_name": hw,
        "friendly_label": friendly,
        "name": name,
        "by_id_serial": identity.by_id_serial,
        "identity_source": identity.identity_source,
        "multi_drive_safe": identity.multi_drive_safe,
        "vendor": identity.vendor,
        "model": identity.model,
        "bus": identity.bus,
    }

# Linux cdrom API (uapi/linux/cdrom.h). CDROM_DRIVE_STATUS is the kernel's own
# answer to "is there usable media in this optical drive?" — no extra package,
# and correct on drives that lie about their size (see _drive_has_media).
CDROM_DRIVE_STATUS = 0x5326
CDS_NO_INFO = 0
CDS_NO_DISC = 1
CDS_TRAY_OPEN = 2
CDS_DRIVE_NOT_READY = 3
CDS_DISC_OK = 4

# Devices already warned about, so an unanswerable probe is reported once per
# device rather than on every registry snapshot.
_media_probe_warned: set[str] = set()


def _drive_has_media(dev: str, timeout: float = 1.5) -> bool:
    """Return True when *dev* has usable media loaded.

    Probes in order of authority:

    1. ``sg_turs`` (TEST UNIT READY) — definitive when sg3-utils is installed.
    2. ``CDROM_DRIVE_STATUS`` — the kernel's cdrom API. Definitive and always
       available on Linux for ``/dev/sr*``.

    **Device size is deliberately not consulted.** The previous fallback read
    ``BLKGETSIZE64`` and treated any nonzero size as media-present; a USB
    Blu-ray drive (Pioneer BD-RW BDR-XD06U) reports a phantom 1073741312 bytes
    with an *empty* tray, so every empty drive looked loaded — and because
    ``sg_turs`` was missing from the container image, that unreliable fallback
    was the only probe that ever ran (#766).

    Only a genuinely unanswerable probe falls back to ``True``, so an
    unrecognised drive stays usable instead of vanishing from the UI. That case
    is logged once per device: the original bug was a probe degrading in
    silence, and an optimistic default must never be indistinguishable from a
    real answer.
    """
    # sg_turs returns status GOOD when media is present/ready.
    if SG_TURS_BIN and os.path.exists(dev):
        try:
            proc = subprocess.run(
                [SG_TURS_BIN, dev],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
            return proc.returncode == 0
        except subprocess.TimeoutExpired:
            return False
        except Exception:
            pass

    try:
        fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
        try:
            status = fcntl.ioctl(fd, CDROM_DRIVE_STATUS)
        finally:
            os.close(fd)
        if status == CDS_DISC_OK:
            return True
        # NO_DISC / TRAY_OPEN are definitive. DRIVE_NOT_READY is a disc still
        # spinning up: no usable media *yet*, and the next poll sees DISC_OK.
        if status in (CDS_NO_DISC, CDS_TRAY_OPEN, CDS_DRIVE_NOT_READY):
            return False
        # CDS_NO_INFO: the drive declined to answer — fall through to unknown.
    except Exception:
        pass

    if dev not in _media_probe_warned:
        _media_probe_warned.add(dev)
        logger.warning(
            "Media presence for %s is unknown: sg_turs is %s and CDROM_DRIVE_STATUS "
            "gave no answer. Treating the drive as loaded so it stays usable — "
            "install sg3-utils if this persists.",
            dev,
            "unavailable" if not SG_TURS_BIN else "inconclusive",
        )
    return True


def _enumerate_media_devices() -> list[str]:
    """
    Find /dev/sr* devices that currently have media present.
    """
    devs = sorted(glob.glob("/dev/sr*"))
    result = []
    for d in devs:
        has_media = _drive_has_media(d)
        if has_media:
            result.append(d)
    return result


def startup_enumerate_drives_via_registry() -> list:
    """
    API startup: enumerate optical drives via the OS-level drive registry (#562).

    Replaces the previous ``makemkvcon info disc:9999`` startup probe — that
    was the global-lock contention root cause that derailed two-drive boots.
    The registry walks ``/sys/block``, ``/dev/disk/by-id``, and ``sg_turs``;
    MakeMKV is **not** invoked. The MakeMKV-side hardware label and DRV index
    are rediscovered lazily by the per-drive ``info dev:`` scans that
    ``startup_enumerate_and_rescan_loaded_discs`` runs immediately after.
    """
    snapshots = sorted(_registry_loaded_drives(), key=lambda s: s.mount_point)
    drives = [(str(i), snap.mount_point) for i, snap in enumerate(snapshots)]
    logger.info("Startup registry enumeration: %s", drives)
    return drives


def startup_warm_drive_cache(*, reraise_if_registration_required: bool = False) -> list:
    """
    Called from API lifespan. After #562 PR 3 this is a thin wrapper around
    :func:`startup_enumerate_drives_via_registry` — no MakeMKV is invoked and
    there is nothing left to retry, so ``reraise_if_registration_required``
    is preserved as a no-op for caller signature compatibility (the per-disc
    ``info dev:`` scans that follow are where registration errors can now
    surface; the API-health layer already gates on ``validate_makemkv_installation``).
    """
    del reraise_if_registration_required  # no longer load-bearing here
    return startup_enumerate_drives_via_registry()


def hash_file(path, hash_type='sha256', buffer_size=1024*1024, progress_cb=None):
    """
    Returns the hash digest of a file with optional progress reporting.
    
    Args:
        path: Path to file to hash
        hash_type: Hash algorithm (default: sha256)
        buffer_size: Size of chunks to read (default: 1MB)
        progress_cb: Optional callback function(bytes_read: int, total_bytes: int, file_path: str)
    
    Returns:
        Hexadecimal hash digest string
    """
    h = hashlib.new(hash_type)
    total_bytes = os.path.getsize(path) if os.path.exists(path) else 0
    bytes_read = 0
    
    with open(path, 'rb') as f:
        chunk_count = 0
        last_progress_log = 0
        while chunk := f.read(buffer_size):
            h.update(chunk)
            bytes_read += len(chunk)
            chunk_count += 1
            if progress_cb:
                # Only call progress_cb every N chunks to avoid excessive DB updates
                # For large files, call every 100MB (100 chunks at 1MB buffer)
                should_update = (chunk_count % 100 == 0) or (bytes_read >= total_bytes)
                if should_update:
                    try:
                        progress_cb(bytes_read, total_bytes, path)
                    except Exception:
                        pass  # Don't fail hashing if callback errors
    return h.hexdigest()

def move_with_progress(src_path: str, dest_path: str,
                       buffer_size: int = 8*1024*1024,
                       hash_verify: bool = True,
                       hash_type: str = 'sha256',
                       progress_cb=None,
                       log_fn=None):
    """
    Move a file, invoking `progress_cb(percentage:int)` as we copy.
    If src & dest are on the same FS the move is instant and
    progress_cb(100) is issued immediately.
    
    In dev mode, source files are prepared (reduced size) before post-processing,
    so this function works the same as normal mode - just with smaller files.
    """
    from core.logging_utils import get_logger
    move_logger = get_logger("core.utils", "move_with_progress")
    
    def _hash_file(path: str) -> str:
        hasher = hashlib.new(hash_type)
        with open(path, 'rb') as fh:
            while chunk := fh.read(buffer_size):
                hasher.update(chunk)
        return hasher.hexdigest()

    # Log source file existence check
    src_exists = os.path.exists(src_path)
    if not src_exists:
        error_msg = f"Source file does not exist: {src_path}"
        move_logger.error(f"move_with_progress: {error_msg}")
        if log_fn:
            log_fn(f"[postprocess] move_file: ERROR - {error_msg}")
        raise FileNotFoundError(error_msg)
    
    try:
        size = os.path.getsize(src_path)
        move_logger.info(f"move_with_progress: Starting move {src_path} ({size} bytes) -> {dest_path}")
        if log_fn:
            log_fn(f"[postprocess] move_file: Moving {src_path} -> {dest_path} ({size} bytes)")
    except Exception as exc:
        move_logger.warning(f"move_with_progress: Error getting source file size: {exc}")
        if log_fn:
            log_fn(f"[postprocess] move_file: Moving {src_path} -> {dest_path} (error getting size: {exc})")
    
    # Ensure destination directory exists
    dest_dir = os.path.dirname(dest_path)
    if dest_dir:
        os.makedirs(dest_dir, exist_ok=True)
        if log_fn:
            log_fn(f"[postprocess] move_file: Created destination directory: {dest_dir}")

    # Fast path: try rename first (instant on same filesystem, even with hash_verify).
    # If rename works, we can verify hash after the move if needed.
    try:
        os.rename(src_path, dest_path)
        # Rename succeeded - skip hash verification for same-filesystem moves
        # Since os.rename() is atomic on the same filesystem, the file integrity is guaranteed.
        # Hash verification would require reading the entire file again (expensive for large files).
        if progress_cb:
            progress_cb(100)
        
        # Verify file exists after move
        if os.path.exists(dest_path):
            try:
                dest_size = os.path.getsize(dest_path)
                move_logger.info(f"move_with_progress: Completed move (same-filesystem rename) {src_path} -> {dest_path} ({dest_size} bytes)")
                if log_fn:
                    log_fn(f"[postprocess] move_file: Completed move (same-filesystem rename) -> {dest_path}")
                    log_fn(f"[postprocess] move_file: Post-move verification: {dest_path} exists ({dest_size} bytes)")
            except Exception:
                move_logger.info(f"move_with_progress: Completed move (same-filesystem rename) {src_path} -> {dest_path}")
                if log_fn:
                    log_fn(f"[postprocess] move_file: Completed move (same-filesystem rename) -> {dest_path}")
                    log_fn(f"[postprocess] move_file: Post-move verification: {dest_path} exists")
        else:
            error_msg = f"Post-move verification failed: {dest_path} does not exist"
            move_logger.error(f"move_with_progress: {error_msg}")
            if log_fn:
                log_fn(f"[postprocess] move_file: ERROR - {error_msg}")
        return
    except OSError as e:
        if e.errno != getattr(os, 'EXDEV', 18):
            raise  # other error – propagate
        # EXDEV means cross-device, need to copy

    # 2) copy + delete (cross-filesystem move)
    move_logger.info(f"move_with_progress: Cross-filesystem move required (copy + delete method) for {src_path} -> {dest_path}")
    if log_fn:
        log_fn(f"[postprocess] move_file: Cross-filesystem move required (copy + delete method)")
    total = os.path.getsize(src_path)
    copied = 0
    hasher = hashlib.new(hash_type) if hash_verify else None

    if hash_verify:
        move_logger.debug(f"move_with_progress: Hash verification enabled (type: {hash_type})")
        if log_fn:
            log_fn(f"[postprocess] move_file: Hash verification enabled (type: {hash_type})")

    with open(src_path, 'rb') as sf, open(dest_path, 'wb') as df:
        while (chunk := sf.read(buffer_size)):
            df.write(chunk)
            copied += len(chunk)
            if hasher:
                hasher.update(chunk)

            # coarse (integer) percentage to avoid chatty DB writes
            if progress_cb:
                progress_cb(int(copied * 100 / total))

        df.flush(); os.fsync(df.fileno())

    expected_digest = hasher.hexdigest() if hasher else None
    if progress_cb:
        progress_cb(100)  # make sure we end on 100

    if hash_verify:
        move_logger.debug(f"move_with_progress: Verifying hash for {dest_path}")
        if log_fn:
            log_fn(f"[postprocess] move_file: Verifying hash for {dest_path}")
        actual_digest = _hash_file(dest_path)
        if actual_digest != expected_digest:
            error_msg = f"Hash mismatch: expected {expected_digest[:16]}..., got {actual_digest[:16]}..."
            move_logger.error(f"move_with_progress: {error_msg}")
            if log_fn:
                log_fn(f"[postprocess] move_file: ERROR - {error_msg}")
            try:
                os.remove(dest_path)
                move_logger.debug(f"move_with_progress: Removed destination file due to hash mismatch")
                if log_fn:
                    log_fn(f"[postprocess] move_file: Removed destination file due to hash mismatch")
            except Exception:
                pass
            # keep the source so the caller can retry safely
            raise ValueError("Hash mismatch after move; source preserved")
        else:
            move_logger.debug(f"move_with_progress: Hash verification passed: {actual_digest[:16]}...")
            if log_fn:
                log_fn(f"[postprocess] move_file: Hash verification passed: {actual_digest[:16]}...")

    os.remove(src_path)
    # Verify file exists after move
    if os.path.exists(dest_path):
        try:
            dest_size = os.path.getsize(dest_path)
            move_logger.info(f"move_with_progress: Completed move (cross-filesystem copy) {src_path} -> {dest_path} ({dest_size} bytes)")
            if log_fn:
                log_fn(f"[postprocess] move_file: Completed move (cross-filesystem copy) -> {dest_path}")
                log_fn(f"[postprocess] move_file: Post-move verification: {dest_path} exists ({dest_size} bytes)")
        except Exception:
            move_logger.info(f"move_with_progress: Completed move (cross-filesystem copy) {src_path} -> {dest_path}")
            if log_fn:
                log_fn(f"[postprocess] move_file: Completed move (cross-filesystem copy) -> {dest_path}")
                log_fn(f"[postprocess] move_file: Post-move verification: {dest_path} exists")
    else:
        error_msg = f"Post-move verification failed: {dest_path} does not exist"
        move_logger.error(f"move_with_progress: {error_msg}")
        if log_fn:
            log_fn(f"[postprocess] move_file: ERROR - {error_msg}")

def notify_discord(webhook_url: str, message: str, username: str = "MakeMKV-Auto", avatar_url: str = None):
    """
    Send a simple message to a Discord channel via webhook.

    :param webhook_url: Your Discord webhook URL (string).
    :param message: The content of your notification.
    :param username: The displayed name for the webhook message.
    :param avatar_url: URL to an image to use as the avatar (optional).
    """
    data = {
        "content": message,
        "username": username,
    }
    if avatar_url:
        data["avatar_url"] = avatar_url

    resp = requests.post(webhook_url, json=data)
    resp.raise_for_status()  # will throw an exception for 4xx/5xx responses


def _parse_size_to_bytes(size_value: any) -> int:
    """
    Parse a size value to bytes.
    Handles various formats: numbers (bytes), strings with units (KB, MB, GB, TB).
    
    :param size_value: Size value (int, float, or string like "5.2 GB")
    :return: Size in bytes, or 0 if parsing fails
    """
    if size_value is None:
        return 0
    
    # If it's already a number, assume bytes
    if isinstance(size_value, (int, float)):
        if not (isinstance(size_value, float) and (size_value != size_value)):  # Check for NaN
            return int(size_value)
        return 0
    
    # If it's a string, try to parse it
    if isinstance(size_value, str):
        trimmed = size_value.strip()
        if not trimmed:
            return 0
        
        # Try to match patterns like "5.2 GB", "1024 MB", "1.5TB", etc.
        match = re.match(r'^([\d.]+)\s*(kb|mb|gb|tb|bytes?)?\s*$', trimmed, re.IGNORECASE)
        if match:
            value = float(match.group(1))
            unit = (match.group(2) or '').lower()
            
            factor = 1
            if unit in ('tb', 'terabyte', 'terabytes'):
                factor = 1024 ** 4
            elif unit in ('gb', 'gigabyte', 'gigabytes'):
                factor = 1024 ** 3
            elif unit in ('mb', 'megabyte', 'megabytes'):
                factor = 1024 ** 2
            elif unit in ('kb', 'kilobyte', 'kilobytes'):
                factor = 1024
            
            return int(value * factor)
        
        # Try to parse as a plain number
        try:
            return int(float(trimmed))
        except (ValueError, TypeError):
            pass
    
    return 0


def calculate_disc_size_from_titles(titles: dict) -> int:
    """
    Calculate total disc size from titles dictionary.
    
    :param titles: Dictionary of titles, where each value may have a 'size' field
    :return: Total size in bytes, or 0 if cannot be calculated
    """
    if not titles:
        return 0
    
    total_bytes = 0
    for title_key, title_info in titles.items():
        if isinstance(title_info, dict):
            # Try various size fields
            size_value = (
                title_info.get("size")
                or title_info.get("meta_size")
                or title_info.get("metaSize")
            )
            if size_value is None and isinstance(title_info.get("meta"), dict):
                size_value = (title_info.get("meta") or {}).get("size")
            if size_value:
                total_bytes += _parse_size_to_bytes(size_value)
        elif isinstance(title_info, (int, float)):
            # If title_info itself is a number, treat as size
            total_bytes += int(title_info)
    
    return total_bytes


def estimate_preview_size_bytes(
    titles: dict | None,
    duration_seconds: int | None = None,
    bitrate_mbps: float | None = None,
) -> int:
    if not titles:
        return 0

    try:
        title_count = len(titles)
    except TypeError:
        return 0

    if title_count <= 0:
        return 0

    if duration_seconds is None:
        from core.preview_config import load_preview_config

        config = load_preview_config()
        duration_seconds = int(config.get("duration_seconds", 120))

    if duration_seconds <= 0:
        return 0

    if bitrate_mbps is None:
        try:
            bitrate_mbps = float(os.getenv("MKVAUTO_PREVIEW_ESTIMATE_MBPS", "4"))
        except ValueError:
            bitrate_mbps = 4.0

    if bitrate_mbps <= 0:
        return 0

    bytes_per_second = (bitrate_mbps * 1_000_000) / 8
    return int(title_count * duration_seconds * bytes_per_second)


def calculate_required_rip_space_bytes(
    titles: dict | None,
    disc_size_bytes: int | None,
    buffer_multiplier: float = 1.3,
) -> int | None:
    total_disc_size = 0
    if titles:
        total_disc_size = calculate_disc_size_from_titles(titles)

    if total_disc_size <= 0 and disc_size_bytes:
        total_disc_size = disc_size_bytes

    if total_disc_size <= 0:
        return None

    preview_bytes = estimate_preview_size_bytes(titles)
    return int((total_disc_size + preview_bytes) * buffer_multiplier)


def check_disk_space_for_rip(output_dir: str, required_bytes: int) -> tuple[bool, str]:
    """
    Check if there's enough disk space for a rip operation.
    
    :param output_dir: Output directory path
    :param required_bytes: Required space in bytes (should include buffer)
    :return: Tuple of (is_sufficient: bool, error_message: str)
    """
    try:
        if not os.path.exists(output_dir):
            # If directory doesn't exist, check parent directory
            parent_dir = os.path.dirname(output_dir)
            if not parent_dir or not os.path.exists(parent_dir):
                return False, f"Output directory does not exist and cannot determine available space: {output_dir}"
            check_dir = parent_dir
        else:
            check_dir = output_dir
        
        usage = shutil.disk_usage(check_dir)
        available_bytes = usage.free
        
        if available_bytes < required_bytes:
            required_gb = required_bytes / (1024 ** 3)
            available_gb = available_bytes / (1024 ** 3)
            return False, (
                f"Insufficient disk space. Required: {required_gb:.2f} GB, "
                f"Available: {available_gb:.2f} GB. "
                f"Please free up at least {(required_bytes - available_bytes) / (1024 ** 3):.2f} GB."
            )
        
        return True, ""
    except Exception as exc:
        return False, f"Failed to check disk space: {exc}"


def notify_discord_space_error(message: str) -> None:
    """
    Send a Discord notification for disk space errors.
    Uses the configured Discord webhook if available.
    
    :param message: Error message to send
    """
    try:
        from core import discord_config
        webhook_url = discord_config.get_webhook_url()
        if webhook_url:
            formatted_message = f"❌ Disk Space Error: {message}"
            notify_discord(webhook_url, formatted_message)
    except Exception as exc:
        # Silently fail - don't raise exceptions for notification failures
        logger.warning(f"Failed to send Discord notification for space error: {exc}")


def reap_zombie_processes() -> int:
    """
    Reap any zombie child processes. Returns count of reaped zombies.
    Safe to call periodically as it uses WNOHANG (non-blocking).
    
    This provides defense-in-depth against zombie accumulation alongside tini.
    While tini (PID 1) should handle most zombie reaping, this periodic cleanup
    ensures any zombies that slip through are eventually reaped.
    
    :return: Number of zombie processes reaped
    """
    reaped = 0
    try:
        while True:
            # Use -1 to wait for any child, WNOHANG for non-blocking
            pid, status = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                # No more zombies to reap
                break
            reaped += 1
            if os.WIFEXITED(status):
                exit_code = os.WEXITSTATUS(status)
                logger.debug(f"Reaped zombie process {pid} with exit code {exit_code}")
            elif os.WIFSIGNALED(status):
                signal = os.WTERMSIG(status)
                logger.debug(f"Reaped zombie process {pid} killed by signal {signal}")
            else:
                logger.debug(f"Reaped zombie process {pid}")
    except ChildProcessError:
        # No children to reap - this is normal
        pass
    except Exception as exc:
        logger.warning(f"Error during zombie process cleanup: {exc}")
    
    return reaped
