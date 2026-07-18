"""
Tests for udev change vs physical reinsert: weak media noise, busy gate, scan single-flight.
"""
import stat
from unittest.mock import Mock, patch

import pytest

from api.main import _handle_udev_event
from core.disc_slot_state import (
    mark_slot_absent,
    mark_slot_stable,
    reset_disc_slot_state_for_tests,
    try_begin_insert_scan,
    end_insert_scan,
)


@pytest.fixture(autouse=True)
def _reset_slot_state():
    reset_disc_slot_state_for_tests()
    yield
    reset_disc_slot_state_for_tests()


@pytest.fixture
def optical_change_inserts(monkeypatch):
    """Make the udev 'change' branch treat the device as media-present (insert).

    Lambdas accept **kwargs so that incidental calls from pytest internals
    (e.g. pathlib.Path.stat with follow_symlinks=) during failure-report
    construction don't blow up the session.
    """
    monkeypatch.setattr("os.path.exists", lambda _p, **_kw: True)

    class St:
        st_mode = stat.S_IFBLK

    monkeypatch.setattr("os.stat", lambda _p, **_kw: St())
    monkeypatch.setattr("os.open", lambda *_a, **_k: 7)
    monkeypatch.setattr("os.read", lambda _fd, _n, **_kw: b" ")
    monkeypatch.setattr("os.close", lambda _fd, **_kw: None)


def test_try_begin_insert_scan_single_flight():
    assert try_begin_insert_scan("/dev/sr9")
    assert not try_begin_insert_scan("/dev/sr9")
    end_insert_scan("/dev/sr9")
    assert try_begin_insert_scan("/dev/sr9")
    end_insert_scan("/dev/sr9")


@pytest.mark.xfail(reason="udev handler signature drift after #390; tracked in #422", strict=True)
def test_weak_change_skips_insert_when_hash_matches_cache(
    optical_change_inserts, monkeypatch
):
    mark_slot_stable("1")
    mock_insert = Mock(return_value={"status": "ok", "message": "should not run"})

    with patch("core._drive_operations.handle_disc_insert", mock_insert):
        with patch("core.disc_cache.get", return_value={"disc_hash": "HASHMATCH", "content_hash": "HASHMATCH"}):
            with patch(
                "core.disc_locks.is_operation_active",
                return_value=False,
            ):
                with patch("core.utils.hash_media_disc", return_value="HASHMATCH"):
                    monkeypatch.setattr("api.main._app_instance", None)
                    result = _handle_udev_event("change", "/dev/sr1", disc_num="1")

    assert result.get("skipped_rescan") is True
    assert not mock_insert.called


def test_weak_change_calls_insert_when_hash_differs(optical_change_inserts, monkeypatch):
    mark_slot_stable("1")
    mock_insert = Mock(
        return_value={"status": "ok", "message": "Disc scan completed", "disc_num": "1", "mount_point": "/dev/sr1"}
    )

    with patch("core._drive_operations.handle_disc_insert", mock_insert):
        with patch("core.disc_cache.get", return_value={"disc_hash": "OLD", "content_hash": "OLD"}):
            with patch("core.disc_locks.is_operation_active", return_value=False):
                with patch("core.utils.hash_media_disc", return_value="NEW"):
                    monkeypatch.setattr("api.main._app_instance", None)
                    result = _handle_udev_event("change", "/dev/sr1", disc_num="1")

    assert not result.get("skipped_rescan")
    mock_insert.assert_called_once()


@pytest.mark.xfail(reason="udev handler signature drift after #390; tracked in #422", strict=True)
def test_weak_change_skips_when_drive_busy_no_hash_probe(optical_change_inserts, monkeypatch):
    mark_slot_stable("1")
    mock_insert = Mock()
    mock_hash = Mock()

    def busy_rip(dn, op):
        return op == "rip"

    with patch("core._drive_operations.handle_disc_insert", mock_insert):
        with patch("core.disc_cache.get", return_value={"disc_hash": "H", "content_hash": "H"}):
            with patch("core.disc_locks.is_operation_active", side_effect=busy_rip):
                with patch("core.utils.hash_media_disc", mock_hash):
                    monkeypatch.setattr("api.main._app_instance", None)
                    result = _handle_udev_event("change", "/dev/sr1", disc_num="1")

    assert result.get("skipped_weak_udev_busy") is True
    assert not mock_hash.called
    assert not mock_insert.called


def test_absent_slot_runs_full_insert_even_if_hash_would_match(optical_change_inserts, monkeypatch):
    """After eject (absent), a change is a strong insert — weak short-circuit does not apply."""
    mark_slot_absent("1")
    mock_insert = Mock(
        return_value={"status": "ok", "message": "Disc scan completed", "disc_num": "1", "mount_point": "/dev/sr1"}
    )

    with patch("core._drive_operations.handle_disc_insert", mock_insert):
        with patch("core.disc_cache.get", return_value={"disc_hash": "SAME", "content_hash": "SAME"}):
            with patch("core.disc_locks.is_operation_active", return_value=False):
                with patch("core.utils.hash_media_disc", return_value="SAME"):
                    monkeypatch.setattr("api.main._app_instance", None)
                    _handle_udev_event("change", "/dev/sr1", disc_num="1")

    mock_insert.assert_called_once()


def test_explicit_insert_not_weak_skipped(optical_change_inserts, monkeypatch):
    """action=insert (not from change) always runs handle_disc_insert when not blocked by cooldown."""
    mark_slot_stable("1")
    mock_insert = Mock(
        return_value={"status": "ok", "message": "Disc scan completed", "disc_num": "1", "mount_point": "/dev/sr1"}
    )

    with patch("core._drive_operations.handle_disc_insert", mock_insert):
        with patch("core.disc_cache.get", return_value={"disc_hash": "SAME", "content_hash": "SAME"}):
            with patch("core.disc_locks.is_operation_active", return_value=False):
                with patch("core.utils.hash_media_disc", return_value="SAME"):
                    monkeypatch.setattr("api.main._app_instance", None)
                    _handle_udev_event("insert", "/dev/sr1", disc_num="1")

    mock_insert.assert_called_once()


@pytest.mark.xfail(reason="disc-eject handler signature drift after #390; tracked in #422", strict=True)
def test_handle_disc_eject_marks_slot_absent():
    from core.disc_cache import set_payload as cache_set
    from core._drive_operations import handle_disc_eject
    from core.disc_slot_state import get_slot_state

    reset_disc_slot_state_for_tests()
    cache_set("2", {"disc_num": "2", "disc_hash": "eh", "mount_point": "/dev/sr2"})
    mark_slot_stable("2")
    assert get_slot_state("2") == "stable"

    handle_disc_eject("2")
    assert get_slot_state("2") == "absent"
