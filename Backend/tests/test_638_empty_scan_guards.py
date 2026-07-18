"""Tests for #638 race-condition guards:

1. ensure_disc_record_from_scan marks scan_state='failed' when a scan
   produces no format and no titles (races with prior-disc cleanup).
2. POST /jobs/rip refuses to dispatch when the target disc has 0
   DiscTitle rows.
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api import crud, models
from api.main import app


def _make_disc(session, *, disc_hash, disc_num="0", format=None, titles=0):
    disc = models.Disc(
        content_hash=disc_hash,
        format=format,
        scan_state="completed" if titles or format else None,
    )
    session.add(disc)
    session.commit()
    session.refresh(disc)
    for i in range(titles):
        session.add(
            models.DiscTitle(
                disc_id=disc.id,
                source_file=f"title-{i}.mkv",
                index=i,
            )
        )
    session.commit()
    session.refresh(disc)
    return disc


def test_empty_scan_output_marks_disc_failed(test_db):
    """Persist path receives payload with no format and no scan_tracks →
    disc.scan_state='failed' with a human-readable error."""
    session = test_db()
    try:
        payload = {
            "disc_hash": "hash-empty-1",
            "content_hash": "hash-empty-1",
            "scan_tracks": [],
            "tracks": {},
        }
        with patch("api.crud._hydrate_payload", side_effect=lambda *a, **kw: dict(payload)):
            disc = crud.ensure_disc_record_from_scan(
                session, disc_num="0", mount_point="/dev/sr0", payload=payload
            )
        assert disc is not None
        session.refresh(disc)
        assert disc.scan_state == "failed"
        assert disc.format is None
        assert disc.last_scan_error and "Empty scan output" in disc.last_scan_error
    finally:
        session.close()


def test_populated_scan_output_does_not_mark_failed(test_db):
    """Healthy scan (format + tracks) leaves scan_state untouched."""
    session = test_db()
    try:
        payload = {
            "disc_hash": "hash-good-1",
            "content_hash": "hash-good-1",
            "format": "DVD",
            "info_title": "Boondocks_s1_d1",
            "scan_tracks": [
                {"title_id": 0, "source_file": "title-0.mkv"},
                {"title_id": 1, "source_file": "title-1.mkv"},
            ],
            "tracks": {},
        }
        with patch("api.crud._hydrate_payload", side_effect=lambda *a, **kw: dict(payload)):
            disc = crud.ensure_disc_record_from_scan(
                session, disc_num="0", mount_point="/dev/sr0", payload=payload
            )
        assert disc is not None
        session.refresh(disc)
        assert disc.scan_state != "failed"
        assert disc.format == "DVD"
    finally:
        session.close()


def test_rip_dispatch_rejects_disc_with_zero_titles(test_db):
    """Rip endpoint blocks with 409 when target disc has 0 DiscTitle rows."""
    session = test_db()
    try:
        disc = _make_disc(session, disc_hash="hash-empty-2", titles=0, format=None)
        disc_id = disc.id
    finally:
        session.close()

    client = TestClient(app)
    with patch(
        "core.disc_scan_dispatch.disc_info_cache_satisfies", return_value=True
    ), patch(
        "core.disc_manager.get_cached_discs",
        return_value=[{"disc_num": "0", "mount_point": "/dev/sr0", "disc_hash": "hash-empty-2"}],
    ), patch(
        "core.makemkv_updater.validate_makemkv_installation",
        return_value={"can_rip": True, "missing_components": [], "error_message": None},
    ):
        resp = client.post(
            "/jobs/rip",
            json={
                "disc_num": "0",
                "mount_point": "/dev/sr0",
                "disc_id": disc_id,
                "mode": "copy",
            },
        )
    assert resp.status_code == 409, resp.text
    body = resp.json().get("detail") or {}
    assert body.get("code") == "disc_scan_incomplete"
    assert "eject" in body.get("error", "").lower()


def test_rip_dispatch_allows_disc_with_titles(test_db, monkeypatch):
    """Rip endpoint passes the title-count gate when disc has enumerated tracks."""
    session = test_db()
    try:
        disc = _make_disc(session, disc_hash="hash-good-2", titles=3, format="DVD")
        disc_id = disc.id
    finally:
        session.close()

    # Stub gatekeeper so we don't need the full rip pipeline — we only care
    # that the title-count gate does NOT block dispatch. Anything beyond the
    # gate (start_rip, celery, etc.) is out of scope for this test.
    def _fake_start_rip(self, *a, **kw):
        raise RuntimeError("gate passed — start_rip reached")

    monkeypatch.setattr(
        "core.drive_gatekeeper.DriveGatekeeper.start_rip", _fake_start_rip
    )
    monkeypatch.setattr(
        "core.drive_gatekeeper.DriveGatekeeper.can_start_rip",
        lambda self, *a, **kw: (True, None),
    )

    client = TestClient(app)
    with patch(
        "core.disc_scan_dispatch.disc_info_cache_satisfies", return_value=True
    ), patch(
        "core.disc_manager.get_cached_discs",
        return_value=[{"disc_num": "0", "mount_point": "/dev/sr0", "disc_hash": "hash-good-2"}],
    ), patch(
        "core.makemkv_updater.validate_makemkv_installation",
        return_value={"can_rip": True, "missing_components": [], "error_message": None},
    ):
        resp = client.post(
            "/jobs/rip",
            json={
                "disc_num": "0",
                "mount_point": "/dev/sr0",
                "disc_id": disc_id,
                "mode": "copy",
            },
        )
    # Any status other than 409-disc_scan_incomplete means the gate let it through.
    if resp.status_code == 409:
        body = resp.json().get("detail") or {}
        assert body.get("code") != "disc_scan_incomplete", (
            "title-count gate should not have blocked a disc with 3 titles"
        )
