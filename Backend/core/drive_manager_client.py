"""
Thin client for the drive manager service so the main backend never touches
/dev/sr* directly.
"""
from __future__ import annotations

import os
from typing import List, Tuple

import requests


class DriveManagerError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


BASE_URL = os.getenv("DRIVE_MANAGER_URL", "http://127.0.0.1:8100").rstrip("/")
DRIVE_MANAGER_TIMEOUT = float(os.getenv("DRIVE_MANAGER_TIMEOUT", "-1"))


def _url(path: str) -> str:
    return f"{BASE_URL}{path}"


def list_drives(timeout: float | None = None) -> List[Tuple[str, str]]:
    if timeout is None:
        timeout = DRIVE_MANAGER_TIMEOUT if DRIVE_MANAGER_TIMEOUT > 0 else None
    try:
        resp = requests.get(_url("/drives"), timeout=timeout)
    except Exception as exc:
        raise DriveManagerError(f"Drive manager unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise DriveManagerError(f"Drive manager drive scan failed: {resp.text}", resp.status_code)
    try:
        data = resp.json()
    except ValueError as exc:
        raise DriveManagerError(f"Invalid drive manager response: {resp.text}") from exc
    drives: List[Tuple[str, str]] = []
    for entry in data or []:
        try:
            drives.append((str(entry.get("disc_num")), entry.get("mount_point")))
        except Exception:
            continue
    return drives


def fetch_disc_info(disc_num: str, mount_point: str, timeout: float = 60.0, refresh: bool = False) -> dict:
    params = {"disc_num": disc_num, "mount_point": mount_point}
    if refresh:
        params["refresh"] = "1"
    try:
        resp = requests.get(_url("/discinfo"), params=params, timeout=timeout)
    except Exception as exc:
        raise DriveManagerError(f"Drive manager unreachable: {exc}") from exc

    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError as exc:
            raise DriveManagerError(f"Invalid discinfo response: {resp.text}") from exc

    # propagate 404 to allow callers to surface discdb_not_found
    if resp.status_code == 404:
        raise DriveManagerError(resp.text, status_code=404)

    raise DriveManagerError(f"Disc info fetch failed ({resp.status_code}): {resp.text}", resp.status_code)


def refresh_disc_info(disc_num: str, mount_point: str, timeout: float = 60.0) -> dict:
    """
    Force a rescan of the disc, bypassing cache.
    """
    params = {"disc_num": disc_num, "mount_point": mount_point}
    try:
        resp = requests.post(_url("/discinfo/refresh"), params=params, timeout=timeout)
    except Exception as exc:
        raise DriveManagerError(f"Drive manager unreachable: {exc}") from exc

    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError as exc:
            raise DriveManagerError(f"Invalid discinfo refresh response: {resp.text}") from exc

    if resp.status_code == 404:
        raise DriveManagerError(resp.text, status_code=404)

    raise DriveManagerError(f"Disc info refresh failed ({resp.status_code}): {resp.text}", resp.status_code)


def validate_disc_info(disc_num: str, mount_point: str, disc_hash: str, timeout: float = 30.0) -> dict:
    """
    Validate cached disc info for a drive against an expected hash without triggering a rescan.
    """
    if not disc_hash:
        raise DriveManagerError("disc_hash is required for validation")
    params = {"disc_num": disc_num, "mount_point": mount_point, "disc_hash": disc_hash}
    try:
        resp = requests.post(_url("/discinfo/validate"), params=params, timeout=timeout)
    except Exception as exc:
        raise DriveManagerError(f"Drive manager unreachable: {exc}") from exc

    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError as exc:
            raise DriveManagerError(f"Invalid discinfo validate response: {resp.text}") from exc
    if resp.status_code in (404, 409, 400):
        raise DriveManagerError(resp.text, status_code=resp.status_code)
    raise DriveManagerError(f"Disc info validate failed ({resp.status_code}): {resp.text}", resp.status_code)


def scan_disc_info(disc_num: str, mount_point: str, timeout: float = 300.0) -> dict:
    """
    Run info scan for a disc (with lock check).
    Internal use - called by Disc Manager.
    """
    params = {"disc_num": disc_num, "mount_point": mount_point}
    try:
        resp = requests.post(_url("/discinfo/scan"), params=params, timeout=timeout)
    except Exception as exc:
        raise DriveManagerError(f"Drive manager unreachable: {exc}") from exc

    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError as exc:
            raise DriveManagerError(f"Invalid discinfo scan response: {resp.text}") from exc
    if resp.status_code == 409:
        raise DriveManagerError(resp.text, status_code=409)
    raise DriveManagerError(f"Disc info scan failed ({resp.status_code}): {resp.text}", resp.status_code)


def hash_disc(disc_num: str, mount_point: str, timeout: float = 300.0) -> dict:
    """
    Calculate hash for a disc (with lock check).
    Internal use - called by Disc Manager.
    """
    params = {"disc_num": disc_num, "mount_point": mount_point}
    try:
        resp = requests.post(_url("/discinfo/hash"), params=params, timeout=timeout)
    except Exception as exc:
        raise DriveManagerError(f"Drive manager unreachable: {exc}") from exc

    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError as exc:
            raise DriveManagerError(f"Invalid disc hash response: {resp.text}") from exc
    if resp.status_code == 409:
        raise DriveManagerError(resp.text, status_code=409)
    raise DriveManagerError(f"Disc hash failed ({resp.status_code}): {resp.text}", resp.status_code)


def notify_disc_eject(disc_num: str, timeout: float = 5.0) -> dict:
    """
    Notify Drive Manager of disc ejection.
    Internal use - called by UDS server or Disc Manager.
    """
    params = {"disc_num": disc_num}
    try:
        resp = requests.post(_url("/disc/eject"), params=params, timeout=timeout)
    except Exception as exc:
        raise DriveManagerError(f"Drive manager unreachable: {exc}") from exc

    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError as exc:
            raise DriveManagerError(f"Invalid disc eject response: {resp.text}") from exc
    raise DriveManagerError(f"Disc eject failed ({resp.status_code}): {resp.text}", resp.status_code)


def notify_disc_insert(disc_num: str, timeout: float = 5.0) -> dict:
    """
    Notify Drive Manager of disc insertion.
    Internal use - called by UDS server or Disc Manager.
    """
    params = {"disc_num": disc_num}
    try:
        resp = requests.post(_url("/disc/insert"), params=params, timeout=timeout)
    except Exception as exc:
        raise DriveManagerError(f"Drive manager unreachable: {exc}") from exc

    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError as exc:
            raise DriveManagerError(f"Invalid disc insert response: {resp.text}") from exc
    raise DriveManagerError(f"Disc insert failed ({resp.status_code}): {resp.text}", resp.status_code)


def start_rip(job_id: str, disc_num: str, mount_point: str, mode: str, output_dir: str, progress_callback_url: str | None = None, timeout: float = 3600.0) -> dict:
    """
    Initiate a rip operation via drive manager.
    
    Args:
        job_id: Job ID for tracking
        disc_num: Disc number
        mount_point: Mount point (e.g., "/dev/sr1")
        mode: "copy" or "backup"
        output_dir: Output directory for ripped files
        progress_callback_url: Optional URL to POST progress updates to
        timeout: Request timeout in seconds (default 1 hour for long rips)
    
    Returns:
        Dict with status and log output
    """
    payload = {
        "job_id": job_id,
        "disc_num": disc_num,
        "mount_point": mount_point,
        "mode": mode,
        "output_dir": output_dir,
    }
    if progress_callback_url:
        payload["progress_callback_url"] = progress_callback_url
    
    try:
        resp = requests.post(_url("/rip"), json=payload, timeout=timeout)
    except requests.exceptions.Timeout:
        raise DriveManagerError(f"Rip operation timed out after {timeout}s", status_code=504) from None
    except Exception as exc:
        raise DriveManagerError(f"Drive manager unreachable: {exc}") from exc

    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError as exc:
            raise DriveManagerError(f"Invalid rip response: {resp.text}") from exc
    if resp.status_code == 409:
        raise DriveManagerError(resp.json().get("detail", "Drive busy"), status_code=409)
    raise DriveManagerError(f"Rip operation failed ({resp.status_code}): {resp.text}", resp.status_code)


def get_job_state(job_id: str, timeout: float = 5.0) -> dict:
    """
    Query drive manager for active operation by job_id.
    Used to verify if a job is actually running after server restart.
    
    Args:
        job_id: Job ID to check
        timeout: Request timeout in seconds
    
    Returns:
        Dict with "active" boolean and operation details if active
    """
    try:
        resp = requests.get(_url(f"/state/job/{job_id}"), timeout=timeout)
    except Exception as exc:
        raise DriveManagerError(f"Drive manager unreachable: {exc}") from exc
    
    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError as exc:
            raise DriveManagerError(f"Invalid job state response: {resp.text}") from exc
    
    raise DriveManagerError(f"Job state query failed ({resp.status_code}): {resp.text}", resp.status_code)
