"""
MockDrive: test double for drive and disc-info I/O.

Use this instead of real optical hardware or a separate drive manager. MockDrive
implements the same surface as core._drive_operations (list_drives, get_disc_info,
refresh_disc_info, validate_disc_info, scan_disc_info, hash_disc, handle_disc_eject,
handle_disc_insert) and writes to the real core.disc_cache so list_discs and
get_cached_discs stay consistent. No real discs are read; all data comes from
the mock's discinfo_payload template.

When to use: Request the mock_drive fixture in tests that need drive/disc behavior
without hardware. Pair with mock_mkv for rip flows and test_db for DB needs.
Tests that must exercise real _drive_operations (e.g. test_drive_manager_endpoints,
test_drive_operations_comprehensive) do NOT use mock_drive; they patch at a
lower level (get_drives, hash_media_disc, run_makemkv).

Config:
- drives: list of (disc_num, mount_point) for list_drives.
- discinfo_payload: template for get/refresh/scan; disc_num and mount_point are
  overridden per call. Should include disc_hash, content_hash, info_log, raw_info_log,
  and any keys consumers expect.
- failures: optional dict of method_name -> HTTPException to simulate errors,
  e.g. {"get_disc_info": HTTPException(404, "not found")}.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from core.disc_cache import clear_key, get as cache_get, set_payload as cache_set


# Minimal TINFO/SINFO so parsing and consumers that expect these do not break.
_DEFAULT_INFO_LOG = (
    'TINFO:0,0,0,"Test Title"\n'
    'SINFO:0,0,19,0,"1920x1080"\n'
    'MSG:3307,0,2,"File 00001.mpls was added as title #1"\n'
)


class MockDrive:
    """
    Test double for _drive_operations. Implements all 8 operations, writes to
    real disc_cache, and supports configurable failures. Do not call
    disc_manager.on_disc_inserted or on_disc_scan_complete from handle_disc_insert.
    """

    def __init__(
        self,
        *,
        drives: Optional[List[Tuple[str, str]]] = None,
        discinfo_payload: Optional[Dict[str, Any]] = None,
        failures: Optional[Dict[str, HTTPException]] = None,
    ):
        self.drives: List[Tuple[str, str]] = drives if drives is not None else [("1", "/mnt/dvd")]
        self.discinfo_payload: Dict[str, Any] = dict(
            discinfo_payload
            if discinfo_payload is not None
            else {
                "disc_num": "1",
                "mount_point": "/mnt/dvd",
                "show_title": "Test Show",
                "show_image": None,
                "tracks": {
                    "00001.mpls": {
                        "season": "1",
                        "episode": "1",
                        "episode_name": "Pilot",
                        "format": "MainFeature",
                    }
                },
                "disc_hash": "FAKEHASH",
                "content_hash": "FAKEHASH",
                "info_log": _DEFAULT_INFO_LOG,
                "raw_info_log": _DEFAULT_INFO_LOG,
            }
        )
        self.failures: Dict[str, HTTPException] = failures or {}

    def _maybe_raise(self, method: str) -> None:
        exc = self.failures.get(method)
        if exc is not None:
            raise exc

    def _build_payload(self, disc_num: str, mount_point: str) -> Dict[str, Any]:
        p = dict(self.discinfo_payload)
        p["disc_num"] = disc_num
        p["mount_point"] = mount_point
        p.setdefault("disc_hash", "FAKEHASH")
        p.setdefault("content_hash", p["disc_hash"])
        p.setdefault("info_log", _DEFAULT_INFO_LOG)
        p.setdefault("raw_info_log", p["info_log"])
        return p

    def list_drives(self, **kwargs: Any) -> List[Dict[str, str]]:
        self._maybe_raise("list_drives")
        return [{"disc_num": str(n), "mount_point": mp} for n, mp in self.drives]

    def get_disc_info(self, disc_num: str, mount_point: str, refresh: bool = False, **kwargs: Any) -> dict:
        self._maybe_raise("get_disc_info")
        if not refresh:
            cached = cache_get(str(disc_num))
            if cached:
                return cached
            raise HTTPException(status_code=404, detail="Disc info not cached; trigger rescan to refresh")
        payload = self._build_payload(disc_num, mount_point)
        cache_set(str(disc_num), payload)
        return payload

    def refresh_disc_info(self, disc_num: str, mount_point: str, **kwargs: Any) -> dict:
        self._maybe_raise("refresh_disc_info")
        payload = self._build_payload(disc_num, mount_point)
        cache_set(str(disc_num), payload)
        return payload

    def validate_disc_info(self, disc_num: str, mount_point: str, disc_hash: str, **kwargs: Any) -> dict:
        self._maybe_raise("validate_disc_info")
        if not disc_hash:
            raise HTTPException(status_code=400, detail="disc_hash is required")
        cached = cache_get(str(disc_num))
        if not cached:
            raise HTTPException(status_code=404, detail="Disc info not cached; refresh discinfo first")
        ch = cached.get("disc_hash")
        if not ch:
            raise HTTPException(status_code=409, detail="Cached disc info missing hash; refresh discinfo first")
        if str(ch) != str(disc_hash):
            raise HTTPException(
                status_code=409,
                detail=f"Disc hash mismatch (expected {disc_hash}, cached {ch}); refresh discinfo to proceed",
            )
        mp = cached.get("mount_point")
        if mp and str(mp) != str(mount_point):
            raise HTTPException(status_code=409, detail="Requested mount point does not match cached disc")
        return cached

    def scan_disc_info(self, disc_num: str, mount_point: str, **kwargs: Any) -> dict:
        self._maybe_raise("scan_disc_info")
        payload = self._build_payload(disc_num, mount_point)
        cache_set(str(disc_num), payload)
        return payload

    def hash_disc(self, disc_num: str, mount_point: str, **kwargs: Any) -> dict:
        self._maybe_raise("hash_disc")
        h = self.discinfo_payload.get("disc_hash") or self.discinfo_payload.get("content_hash") or "FAKEHASH"
        return {
            "disc_num": str(disc_num),
            "mount_point": mount_point,
            "disc_hash": h,
            "content_hash": h,
        }

    def handle_disc_eject(self, disc_num: str, **kwargs: Any) -> dict:
        self._maybe_raise("handle_disc_eject")
        cached = cache_get(str(disc_num))
        disc_hash = None
        if cached:
            disc_hash = cached.get("disc_hash") or cached.get("content_hash")
        clear_key(str(disc_num))
        out: Dict[str, Any] = {"status": "ok", "message": "Cache cleared"}
        if disc_hash is not None:
            out["disc_hash"] = disc_hash
        return out

    def handle_disc_insert(self, disc_num: str, mount_point: str, **kwargs: Any) -> dict:
        self._maybe_raise("handle_disc_insert")
        clear_key(str(disc_num))
        payload = self._build_payload(disc_num, mount_point)
        cache_set(str(disc_num), payload)
        return {
            "status": "ok",
            "message": "Disc inserted",
            "disc_num": disc_num,
            "mount_point": mount_point,
        }
