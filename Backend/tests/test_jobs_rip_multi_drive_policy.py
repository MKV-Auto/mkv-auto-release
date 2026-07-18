"""Tests for the multi-drive policy gate on POST /jobs/rip.

The 2026-06 diagnostic motivated the fail-closed Decision policy in
``core/drive_policy.py``. This test verifies the gate is wired into the
rip-start API so a drive whose identity falls back below
``/dev/disk/by-id/`` cannot start a rip while other drives are attached.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api import database
from api.main import app
from core.drive_identity import DriveIdentity
from core.drive_policy import CODE_UNIDENTIFIABLE, CODE_UNSAFE_WITH_OTHERS


@pytest.fixture
def client(test_db):
    def override_get_db():
        try:
            session = test_db()
            yield session
        finally:
            session.close()

    app.dependency_overrides[database.get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _id(source: str, mount: str, serial: str = "S") -> DriveIdentity:
    return DriveIdentity(
        by_id_serial=serial,
        vendor="V",
        model="M",
        bus="b",
        by_id_name="",
        hardware_name=None,
        identity_source=source,  # type: ignore[arg-type]
    )


def _bypass_makemkv_validation(monkeypatch):
    """The rip route blocks on a MakeMKV install check and the #562
    disc-info cache-precondition gate before the policy gate — bypass both
    so the policy logic is what we exercise."""

    monkeypatch.setattr(
        "core.makemkv_updater.validate_makemkv_installation",
        lambda: {"can_rip": True, "missing_components": [], "error_message": None},
    )
    monkeypatch.setattr(
        "core.disc_scan_dispatch.disc_info_cache_satisfies",
        lambda *a, **k: True,
    )


class TestMultiDrivePolicyOnRipStart:
    """The gate runs after the MakeMKV check and the Path A modal,
    before ``can_start_rip``. Each case stubs ``build_identity_map`` to
    paint a specific drive landscape."""

    def test_blocks_unsafe_drive_with_others_attached(self, client, monkeypatch):
        _bypass_makemkv_validation(monkeypatch)
        target = _id("by-path", "/dev/sr1", serial="WEAK")
        other = _id("by-id", "/dev/sr2", serial="STRONG")

        with patch(
            "core.drive_identity.build_identity_map",
            return_value={"/dev/sr1": target, "/dev/sr2": other},
        ):
            response = client.post(
                "/jobs/rip",
                json={"mount_point": "/dev/sr1", "disc_num": "1"},
            )

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["code"] == CODE_UNSAFE_WITH_OTHERS
        assert detail["mount_point"] == "/dev/sr1"

    def test_blocks_unidentifiable_drive_alone(self, client, monkeypatch):
        _bypass_makemkv_validation(monkeypatch)
        target = _id("unknown", "/dev/sr1", serial="unknown:sr1")

        with patch(
            "core.drive_identity.build_identity_map",
            return_value={"/dev/sr1": target},
        ):
            response = client.post(
                "/jobs/rip",
                json={"mount_point": "/dev/sr1", "disc_num": "1"},
            )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == CODE_UNIDENTIFIABLE

    def test_allows_unsafe_drive_when_sole(self, client, monkeypatch):
        """The "Sole-only" state: a degraded drive can still rip as long as
        no multi-drive-safe drives are also attached. The 409 must NOT be
        the policy gate here — it would be downstream gating (no disc info,
        etc.), which is outside the scope of this test."""

        _bypass_makemkv_validation(monkeypatch)
        target = _id("by-path", "/dev/sr1", serial="WEAK")

        with patch(
            "core.drive_identity.build_identity_map",
            return_value={"/dev/sr1": target},
        ):
            response = client.post(
                "/jobs/rip",
                json={"mount_point": "/dev/sr1", "disc_num": "1"},
            )

        # Whatever code we get, it should NOT be a policy-gate rejection.
        if response.status_code == 409:
            detail = response.json().get("detail")
            if isinstance(detail, dict):
                assert detail.get("code") not in (
                    CODE_UNIDENTIFIABLE,
                    CODE_UNSAFE_WITH_OTHERS,
                )

    def test_allows_safe_drive_with_others(self, client, monkeypatch):
        """Two healthy by-id drives → policy gate stays out of the way."""

        _bypass_makemkv_validation(monkeypatch)
        target = _id("by-id", "/dev/sr1", serial="SR1")
        other = _id("by-id", "/dev/sr2", serial="SR2")

        with patch(
            "core.drive_identity.build_identity_map",
            return_value={"/dev/sr1": target, "/dev/sr2": other},
        ):
            response = client.post(
                "/jobs/rip",
                json={"mount_point": "/dev/sr1", "disc_num": "1"},
            )

        if response.status_code == 409:
            detail = response.json().get("detail")
            if isinstance(detail, dict):
                assert detail.get("code") not in (
                    CODE_UNIDENTIFIABLE,
                    CODE_UNSAFE_WITH_OTHERS,
                )

    def test_unknown_mount_point_falls_open(self, client, monkeypatch):
        """If the target mount_point isn't in the identity map at all, the
        policy gate has nothing to evaluate — must NOT 409 on policy grounds."""

        _bypass_makemkv_validation(monkeypatch)

        with patch(
            "core.drive_identity.build_identity_map",
            return_value={},  # no drives detected
        ):
            response = client.post(
                "/jobs/rip",
                json={"mount_point": "/dev/sr1", "disc_num": "1"},
            )

        if response.status_code == 409:
            detail = response.json().get("detail")
            if isinstance(detail, dict):
                assert detail.get("code") not in (
                    CODE_UNIDENTIFIABLE,
                    CODE_UNSAFE_WITH_OTHERS,
                )
