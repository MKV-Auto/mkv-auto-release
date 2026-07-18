"""
Destination-capability probe for TransferConfig.

Probes the four primitive operations we need to satisfy any
``conflict_resolution`` intent — new-write, in-place overwrite, delete,
rename — against a config's ``transfer_dir``. Result is cached on
``config.config_data['capabilities']`` and read by the strategy selector
in ``core.transfer.service.resolve_transfer_plan``.

Per-protocol probes always attempt cleanup of the probe artifact via
``try/finally`` — a failure at step N does not leak ``.mkvauto-probe-*``
files onto the destination.
"""
from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

log = logging.getLogger(__name__)

PROBE_PREFIX = ".mkvauto-probe-"
PROBE_CONTENT_A = b"mkvauto-capability-probe-A"
PROBE_CONTENT_B = b"mkvauto-capability-probe-B"


@dataclass
class TransferCapabilities:
    can_write_new: bool = False
    can_overwrite_in_place: bool = False
    can_delete: bool = False
    can_rename: bool = False
    probed_at: str = ""
    probe_error: Optional[str] = None
    notes: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TransferCapabilities":
        if not isinstance(data, dict):
            raise TypeError(f"TransferCapabilities.from_dict expected dict, got {type(data).__name__}")
        return cls(
            can_write_new=bool(data.get("can_write_new", False)),
            can_overwrite_in_place=bool(data.get("can_overwrite_in_place", False)),
            can_delete=bool(data.get("can_delete", False)),
            can_rename=bool(data.get("can_rename", False)),
            probed_at=str(data.get("probed_at", "") or ""),
            probe_error=data.get("probe_error"),
            notes=dict(data.get("notes") or {}),
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pessimistic(error: str, notes: Optional[Dict[str, Any]] = None) -> TransferCapabilities:
    return TransferCapabilities(
        probed_at=_now_iso(),
        probe_error=error,
        notes=dict(notes or {}),
    )


def probe(config, db=None) -> TransferCapabilities:
    """Dispatch on ``config.mode``. Wraps probe fn in try/except so a
    thrown exception yields a pessimistic result rather than propagating
    to the caller (the celery task must not fail because a share went
    offline mid-probe). ``db`` is optional and only used to decrypt
    credentials for remote modes."""
    mode = getattr(config, "mode", None)
    dispatch: Dict[str, Callable[..., TransferCapabilities]] = {
        "local": _probe_local,
        "smb": _probe_smb,
        "rsync": _probe_rsync,
        "nfs": _probe_nfs,
    }
    fn = dispatch.get(mode)
    if fn is None:
        return _pessimistic(f"unknown transfer mode: {mode!r}")
    try:
        if fn is _probe_local:
            return fn(config)
        return fn(config, db=db)
    except Exception as exc:
        log.warning("Capability probe raised for mode=%s: %s", mode, exc, exc_info=True)
        return _pessimistic(f"probe raised: {exc}")


def _probe_local(config) -> TransferCapabilities:
    transfer_dir = getattr(config, "transfer_dir", None)
    if not transfer_dir:
        return _pessimistic("transfer_dir not set")
    root = Path(transfer_dir)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return _pessimistic(f"transfer_dir mkdir failed: {exc}")

    probe_name = f"{PROBE_PREFIX}{uuid.uuid4().hex}.txt"
    probe_path = root / probe_name
    renamed_path = root / f"{probe_name}.renamed"

    caps = TransferCapabilities(probed_at=_now_iso())
    try:
        try:
            probe_path.write_bytes(PROBE_CONTENT_A)
            caps.can_write_new = probe_path.exists()
        except Exception as exc:
            caps.probe_error = f"write_new failed: {exc}"
            return caps

        try:
            probe_path.write_bytes(PROBE_CONTENT_B)
            caps.can_overwrite_in_place = probe_path.read_bytes() == PROBE_CONTENT_B
        except Exception as exc:
            caps.notes["overwrite_error"] = str(exc)

        try:
            probe_path.rename(renamed_path)
            caps.can_rename = renamed_path.exists() and not probe_path.exists()
        except Exception as exc:
            caps.notes["rename_error"] = str(exc)

        target = renamed_path if caps.can_rename else probe_path
        try:
            if target.exists():
                target.unlink()
                caps.can_delete = not target.exists()
            else:
                caps.can_delete = True
        except Exception as exc:
            caps.notes["delete_error"] = str(exc)
    finally:
        for p in (probe_path, renamed_path):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass
    return caps


def _smb_probe_config(config, db=None) -> Optional[Tuple[str, str, str, list, Dict[str, Any]]]:
    """Resolve (smb_url, remote_dir, base, auth_args, notes) for the SMB probe.

    Uses ``config.config_data`` shape used by ``transfer_smb`` (host, share,
    path, port). Credentials read via the same helper that the transfer path
    uses. Returns ``None`` when required fields are missing."""
    config_data = getattr(config, "config_data", None) or {}
    host = config_data.get("host")
    share = config_data.get("share")
    path = (config_data.get("path") or "").lstrip("/").replace("\\", "/")
    port = config_data.get("port", 445)
    if not host or not share:
        return None

    username = password = domain = ""
    if db is not None:
        try:
            from core.transfer.utils.credentials import get_decrypted_credentials
            creds = get_decrypted_credentials(db, getattr(config, "id", None)) or {}
            username = creds.get("smb_username") or ""
            password = creds.get("smb_password") or ""
            domain = creds.get("smb_domain") or ""
        except Exception as exc:
            log.debug("SMB probe credential lookup failed (proceeding anonymous): %s", exc)

    if username:
        auth_args = [f"-U{username}"]
        if password:
            auth_args[0] += f"%{password}"
        if domain:
            auth_args.insert(0, f"-W{domain}")
    else:
        auth_args = ["-U%", "-N"]

    smb_url = f"//{host}/{share}"
    if port and port != 445:
        smb_url = f"//{host}:{port}/{share}"
    return smb_url, path, path, auth_args, {"host": host, "share": share, "path": path}


def _probe_smb(config, db=None) -> TransferCapabilities:
    if not shutil.which("smbclient"):
        return _pessimistic("smbclient not installed")
    resolved = _smb_probe_config(config, db=db)
    if resolved is None:
        return _pessimistic("SMB host/share not configured")
    smb_url, remote_dir, _base, auth_args, notes = resolved

    from core.transfer.protocols.smb import (
        _smb_delete_remote_file,
        _smb_quote_path,
        _extract_nt_status_error,
    )

    probe_name = f"{PROBE_PREFIX}{uuid.uuid4().hex}.txt"
    remote_a = f"{remote_dir}/{probe_name}" if remote_dir else probe_name
    remote_b = f"{remote_dir}/{probe_name}.renamed" if remote_dir else f"{probe_name}.renamed"

    caps = TransferCapabilities(probed_at=_now_iso(), notes=dict(notes))
    tmp_a: Optional[Path] = None
    tmp_b: Optional[Path] = None
    try:
        tmp_a = Path(tempfile.mkstemp(prefix="mkvauto-probe-a-")[1])
        tmp_b = Path(tempfile.mkstemp(prefix="mkvauto-probe-b-")[1])
        tmp_a.write_bytes(PROBE_CONTENT_A)
        tmp_b.write_bytes(PROBE_CONTENT_B)

        def _put(local: Path, remote: str) -> Tuple[bool, str]:
            put_cmd = [
                "smbclient", smb_url, *auth_args,
                "-c", f"put {_smb_quote_path(local.name)} {_smb_quote_path(remote)}",
            ]
            try:
                r = subprocess.run(put_cmd, cwd=str(local.parent), capture_output=True, text=True, timeout=60)
            except subprocess.TimeoutExpired:
                return False, "smbclient put timed out"
            silent = _extract_nt_status_error(r.stdout or "", r.stderr or "")
            if r.returncode != 0 or silent:
                raw = (r.stderr or "").strip() or (r.stdout or "").strip()
                return False, silent or raw or f"smbclient put exited {r.returncode}"
            return True, ""

        ok_a, err_a = _put(tmp_a, remote_a)
        caps.can_write_new = ok_a
        if not ok_a:
            caps.probe_error = f"write_new failed: {err_a}"
            return caps

        ok_b, err_b = _put(tmp_b, remote_a)
        caps.can_overwrite_in_place = ok_b
        if not ok_b:
            caps.notes["overwrite_error"] = err_b

        rename_cmd = [
            "smbclient", smb_url, *auth_args,
            "-c", f"rename {_smb_quote_path(remote_a)} {_smb_quote_path(remote_b)}",
        ]
        try:
            r = subprocess.run(rename_cmd, capture_output=True, text=True, timeout=60)
            silent = _extract_nt_status_error(r.stdout or "", r.stderr or "")
            caps.can_rename = (r.returncode == 0 and not silent)
            if not caps.can_rename:
                caps.notes["rename_error"] = silent or (r.stderr or r.stdout or "").strip()
        except subprocess.TimeoutExpired:
            caps.notes["rename_error"] = "smbclient rename timed out"

        target_remote = remote_b if caps.can_rename else remote_a
        del_ok, del_err = _smb_delete_remote_file(smb_url, auth_args, target_remote)
        caps.can_delete = del_ok
        if not del_ok:
            caps.notes["delete_error"] = del_err
    finally:
        for remote in (remote_a, remote_b):
            try:
                _smb_delete_remote_file(smb_url, auth_args, remote)
            except Exception:
                pass
        for p in (tmp_a, tmp_b):
            if p is not None:
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
    return caps


def _rsync_probe_config(config) -> Optional[Tuple[str, str, str, int, Path]]:
    """Return (user, host, remote_dir, port, key_path) or None."""
    config_data = getattr(config, "config_data", None) or {}
    host = config_data.get("host")
    user = config_data.get("user")
    path = config_data.get("path") or getattr(config, "transfer_dir", None)
    port = int(config_data.get("port", 22) or 22)
    if not host or not user or not path:
        return None
    from core.transfer.protocols.rsync import KEY_PATH
    if not KEY_PATH.exists():
        return None
    return user, host, path, port, KEY_PATH


def _probe_rsync(config, db=None) -> TransferCapabilities:
    resolved = _rsync_probe_config(config)
    if resolved is None:
        return _pessimistic("rsync host/user/path or SSH key not configured")
    user, host, remote_dir, port, key_path = resolved
    ssh_target = f"{user}@{host}"
    remote_dir_q = shlex.quote(remote_dir.rstrip("/") or "/")
    probe_name = f"{PROBE_PREFIX}{uuid.uuid4().hex}.txt"
    remote_a = f"{remote_dir.rstrip('/')}/{probe_name}"
    remote_b = f"{remote_dir.rstrip('/')}/{probe_name}.renamed"
    remote_a_q = shlex.quote(remote_a)
    remote_b_q = shlex.quote(remote_b)

    def _ssh(cmd: str, timeout: int = 30) -> Tuple[int, str, str]:
        ssh_cmd = [
            "ssh",
            "-i", str(key_path),
            "-p", str(port),
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            ssh_target,
            cmd,
        ]
        try:
            r = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)
            return r.returncode, r.stdout or "", r.stderr or ""
        except subprocess.TimeoutExpired:
            return 124, "", "ssh timed out"

    caps = TransferCapabilities(
        probed_at=_now_iso(),
        notes={"host": host, "user": user, "path": remote_dir},
    )
    try:
        rc, _, err = _ssh(f"mkdir -p {remote_dir_q} && printf %s A > {remote_a_q}")
        caps.can_write_new = (rc == 0)
        if rc != 0:
            caps.probe_error = f"write_new failed: {err.strip()}"
            return caps

        rc, _, err = _ssh(f"printf %s B > {remote_a_q}")
        caps.can_overwrite_in_place = (rc == 0)
        if rc != 0:
            caps.notes["overwrite_error"] = err.strip()

        rc, _, err = _ssh(f"mv {remote_a_q} {remote_b_q}")
        caps.can_rename = (rc == 0)
        if rc != 0:
            caps.notes["rename_error"] = err.strip()

        target = remote_b_q if caps.can_rename else remote_a_q
        rc, _, err = _ssh(f"rm -f {target}")
        caps.can_delete = (rc == 0)
        if rc != 0:
            caps.notes["delete_error"] = err.strip()
    finally:
        _ssh(f"rm -f {remote_a_q} {remote_b_q}", timeout=15)
    return caps


def _probe_nfs(config, db=None) -> TransferCapabilities:
    config_data = getattr(config, "config_data", None) or {}
    server = config_data.get("server")
    export_path = config_data.get("export_path")
    sub_path = (config_data.get("path") or "").lstrip("/")
    if not server or not export_path:
        return _pessimistic("NFS server/export_path not configured")

    try:
        import libnfs  # noqa: F401
        return _probe_nfs_libnfs(config, server, export_path, sub_path)
    except ImportError:
        return _probe_nfs_mount(config, server, export_path, sub_path, db=db)


def _probe_nfs_libnfs(config, server: str, export_path: str, sub_path: str) -> TransferCapabilities:
    import libnfs
    caps = TransferCapabilities(
        probed_at=_now_iso(),
        notes={"server": server, "export_path": export_path, "backend": "libnfs"},
    )
    probe_name = f"{PROBE_PREFIX}{uuid.uuid4().hex}.txt"
    remote_a = f"{sub_path}/{probe_name}" if sub_path else probe_name
    remote_b = f"{sub_path}/{probe_name}.renamed" if sub_path else f"{probe_name}.renamed"
    nfs = None
    try:
        nfs = libnfs.NFS(f"nfs://{server}{export_path}")
        try:
            with nfs.open(remote_a, "w") as fh:
                fh.write(PROBE_CONTENT_A)
            caps.can_write_new = True
        except Exception as exc:
            caps.probe_error = f"write_new failed: {exc}"
            return caps

        try:
            with nfs.open(remote_a, "w") as fh:
                fh.write(PROBE_CONTENT_B)
            caps.can_overwrite_in_place = True
        except Exception as exc:
            caps.notes["overwrite_error"] = str(exc)

        try:
            nfs.rename(remote_a, remote_b)
            caps.can_rename = True
        except Exception as exc:
            caps.notes["rename_error"] = str(exc)

        target = remote_b if caps.can_rename else remote_a
        try:
            nfs.unlink(target)
            caps.can_delete = True
        except Exception as exc:
            caps.notes["delete_error"] = str(exc)
    finally:
        if nfs is not None:
            for remote in (remote_a, remote_b):
                try:
                    nfs.unlink(remote)
                except Exception:
                    pass
            try:
                nfs.close()
            except Exception:
                pass
    return caps


def _probe_nfs_mount(config, server: str, export_path: str, sub_path: str, db=None) -> TransferCapabilities:
    caps = TransferCapabilities(
        probed_at=_now_iso(),
        notes={"server": server, "export_path": export_path, "backend": "mount"},
    )
    try:
        from core.utils import _root_helper_mount_nfs, _root_helper_unmount
    except Exception as exc:
        caps.probe_error = f"mount helper unavailable: {exc}"
        return caps

    mount_dir = None
    actual_mount = None
    nfs_options = ""
    try:
        if db is not None:
            try:
                from core.transfer.utils.credentials import get_decrypted_credentials
                creds = get_decrypted_credentials(db, getattr(config, "id", None)) or {}
                nfs_options = creds.get("nfs_options") or ""
            except Exception:
                pass
        mount_dir = Path(tempfile.mkdtemp(prefix="mkvauto-probe-nfs-"))
        actual_mount, mount_err = _root_helper_mount_nfs(
            server, export_path, str(mount_dir), nfs_options
        )
        if mount_err:
            caps.probe_error = f"NFS mount failed: {mount_err}"
            return caps
        base = Path(actual_mount) if actual_mount else mount_dir
        root = base / sub_path if sub_path else base
        root.mkdir(parents=True, exist_ok=True)
        # Local-style probe on the mounted directory.
        local_caps = _probe_local_at(root)
        caps.can_write_new = local_caps.can_write_new
        caps.can_overwrite_in_place = local_caps.can_overwrite_in_place
        caps.can_delete = local_caps.can_delete
        caps.can_rename = local_caps.can_rename
        if local_caps.probe_error:
            caps.probe_error = local_caps.probe_error
        caps.notes.update(local_caps.notes)
    finally:
        try:
            if actual_mount or mount_dir:
                _root_helper_unmount(str(Path(actual_mount) if actual_mount else mount_dir))
        except Exception:
            pass
        if mount_dir and mount_dir.exists():
            try:
                mount_dir.rmdir()
            except Exception:
                pass
    return caps


def _probe_local_at(root: Path) -> TransferCapabilities:
    """Run the local-style four-op probe against ``root``. Used by NFS mount fallback."""
    probe_name = f"{PROBE_PREFIX}{uuid.uuid4().hex}.txt"
    probe_path = root / probe_name
    renamed_path = root / f"{probe_name}.renamed"
    caps = TransferCapabilities(probed_at=_now_iso())
    try:
        try:
            probe_path.write_bytes(PROBE_CONTENT_A)
            caps.can_write_new = probe_path.exists()
        except Exception as exc:
            caps.probe_error = f"write_new failed: {exc}"
            return caps
        try:
            probe_path.write_bytes(PROBE_CONTENT_B)
            caps.can_overwrite_in_place = probe_path.read_bytes() == PROBE_CONTENT_B
        except Exception as exc:
            caps.notes["overwrite_error"] = str(exc)
        try:
            probe_path.rename(renamed_path)
            caps.can_rename = renamed_path.exists() and not probe_path.exists()
        except Exception as exc:
            caps.notes["rename_error"] = str(exc)
        target = renamed_path if caps.can_rename else probe_path
        try:
            if target.exists():
                target.unlink()
            caps.can_delete = not target.exists()
        except Exception as exc:
            caps.notes["delete_error"] = str(exc)
    finally:
        for p in (probe_path, renamed_path):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass
    return caps
