"""Regression test for #542: lock files must derive from ``mount_point``
(stable across hot-plug) rather than ``disc_num`` (volatile MakeMKV index).

The live 2026-06 diagnostic observed ``/data/mkvauto/tmp/disc_locks/0.rip.lock``
and ``2.rip.lock`` — filenames that encode ``disc_num``. Those locks would
follow the WRONG drive after MakeMKV re-enumerates indices on hot-plug.

This test only guards the *naming contract* — actual call-site updates
live in ``workers/tasks.py`` and ``core/_drive_operations.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.disc_locks import (
    OPERATION_RIP,
    OPERATION_HASH,
    OPERATION_INFO,
    get_operation_lock_path,
)


@pytest.fixture
def tmp_lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    lock_dir = tmp_path / "disc_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("core.disc_locks.get_mkvauto_tmp", lambda: tmp_path)
    return lock_dir


class TestMountPointLockNaming:
    def test_dev_sr1_produces_sanitized_filename(self, tmp_lock_dir):
        path = get_operation_lock_path("/dev/sr1", OPERATION_RIP)
        assert path.name == "dev_sr1.rip.lock"

    def test_dev_sr2_distinct_from_sr1(self, tmp_lock_dir):
        sr1 = get_operation_lock_path("/dev/sr1", OPERATION_RIP)
        sr2 = get_operation_lock_path("/dev/sr2", OPERATION_RIP)
        assert sr1 != sr2
        assert sr1.name == "dev_sr1.rip.lock"
        assert sr2.name == "dev_sr2.rip.lock"

    @pytest.mark.parametrize(
        "operation,expected_suffix",
        [
            (OPERATION_RIP, "dev_sr1.rip.lock"),
            (OPERATION_HASH, "dev_sr1.hash.lock"),
            (OPERATION_INFO, "dev_sr1.info.lock"),
        ],
    )
    def test_operation_type_in_filename(self, tmp_lock_dir, operation, expected_suffix):
        path = get_operation_lock_path("/dev/sr1", operation)
        assert path.name == expected_suffix

    def test_disc_num_input_still_accepted_for_legacy_paths(self, tmp_lock_dir):
        """The helper itself remains key-agnostic; callers are responsible
        for passing the stable identity. Defence against accidental regression
        if a future caller still has only disc_num available."""

        path = get_operation_lock_path("0", OPERATION_RIP)
        assert path.name == "0.rip.lock"
