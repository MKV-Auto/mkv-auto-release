"""Tests for is_disc_read_error (core.utils)."""
import pytest

from core.utils import is_disc_read_error


def test_is_disc_read_error_msg_2003():
    """MSG:2003 indicates disc read error."""
    assert is_disc_read_error("makemkvcon read‐error detected (MSG:2003):") is True
    assert is_disc_read_error("MSG:2003,0,3,\"Error 'Scsi error") is True


def test_is_disc_read_error_read_error():
    """read-error / read‐error indicates disc read error."""
    assert is_disc_read_error("makemkvcon read-error detected") is True
    assert is_disc_read_error("read-error in stream") is True


def test_is_disc_read_error_no_such_device():
    """Posix 'No such device' indicates disc/drive error."""
    assert is_disc_read_error("Posix error - No such device") is True
    assert is_disc_read_error("Error 'No such device' occurred") is True


def test_is_disc_read_error_timeout_logical_unit():
    """SCSI TIMEOUT ON LOGICAL UNIT indicates hardware/drive error."""
    assert is_disc_read_error("Scsi error - HARDWARE ERROR:TIMEOUT ON LOGICAL UNIT") is True
    assert is_disc_read_error("TIMEOUT ON LOGICAL UNIT at offset") is True


def test_is_disc_read_error_failed_to_open_disc():
    """Failed to open disc indicates drive/disc error."""
    assert is_disc_read_error("MSG:5010,0,0,\"Failed to open disc\"") is True
    assert is_disc_read_error("Failed to open disc") is True


def test_is_disc_read_error_false_for_unrelated():
    """Unrelated errors return False."""
    assert is_disc_read_error("MakeMKV is expired (exit code 253)") is False
    assert is_disc_read_error("No space left on device") is False
    assert is_disc_read_error("Rip failed: unknown error") is False
    assert is_disc_read_error("") is False


def test_is_disc_read_error_case_insensitive():
    """Matching is case-insensitive for common phrases."""
    assert is_disc_read_error("NO SUCH DEVICE") is True
    assert is_disc_read_error("failed to open disc") is True
    assert is_disc_read_error("TIMEOUT ON LOGICAL UNIT") is True
