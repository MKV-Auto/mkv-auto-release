"""#712: the smbclient put timeout must scale with file size.

A blunt hardcoded 1-hour cap killed large UHD transfers ("Transfer timeout").
The per-put timeout now scales by size with a floor and an env override.
"""
import importlib

import pytest

from core.transfer.protocols import smb


GiB = 1024 * 1024 * 1024


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in ("MKVAUTO_SMB_PUT_TIMEOUT", "MKVAUTO_SMB_MIN_BYTES_PER_SEC", "MKVAUTO_SMB_PUT_TIMEOUT_FLOOR"):
        monkeypatch.delenv(k, raising=False)
    yield


def test_small_file_gets_the_one_hour_floor():
    # 1 GiB at the 2 MiB/s floor is ~512s, below the 3600s floor → floor wins.
    assert smb._smb_put_timeout(1 * GiB) == 3600


def test_large_uhd_scales_well_past_an_hour():
    # 80 GiB at 2 MiB/s ≈ 40960s — the exact case that failed under the old cap.
    t = smb._smb_put_timeout(80 * GiB)
    assert t > 3600
    assert t == pytest.approx(80 * GiB / (2 * 1024 * 1024), rel=0.01)


def test_none_or_zero_size_falls_back_to_floor():
    assert smb._smb_put_timeout(None) == 3600
    assert smb._smb_put_timeout(0) == 3600


def test_absolute_env_override_wins(monkeypatch):
    monkeypatch.setenv("MKVAUTO_SMB_PUT_TIMEOUT", "7200")
    assert smb._smb_put_timeout(80 * GiB) == 7200
    assert smb._smb_put_timeout(1 * GiB) == 7200


def test_min_throughput_env_tunes_scaling(monkeypatch):
    # Slower assumed floor (1 MiB/s) → longer timeout for the same file.
    monkeypatch.setenv("MKVAUTO_SMB_MIN_BYTES_PER_SEC", str(1024 * 1024))
    t = smb._smb_put_timeout(80 * GiB)
    assert t == pytest.approx(80 * GiB / (1024 * 1024), rel=0.01)


def test_bad_env_values_ignored(monkeypatch):
    monkeypatch.setenv("MKVAUTO_SMB_PUT_TIMEOUT", "not-a-number")
    monkeypatch.setenv("MKVAUTO_SMB_MIN_BYTES_PER_SEC", "-5")
    # override ignored (non-int); min-bps ignored (<=0) → default 2 MiB/s, floor applies
    assert smb._smb_put_timeout(1 * GiB) == 3600
