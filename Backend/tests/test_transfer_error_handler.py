"""Unit tests for core.transfer.utils.error_handler."""
import pytest
from unittest.mock import MagicMock

from core.transfer.utils.error_handler import (
    categorize_error,
    can_retry_automatically,
    can_retry,
    retry_transfer,
    handle_transfer_error,
    get_transfer_error_details,
)


# --- categorize_error (pure) ---


def test_categorize_error_connection():
    assert categorize_error(Exception("connection refused")) == "connection"


def test_categorize_error_permission_denied():
    assert categorize_error(Exception("permission denied")) == "authentication"


def test_categorize_error_disk_full():
    assert categorize_error(Exception("disk full")) == "space"


def test_categorize_error_hash_mismatch():
    assert categorize_error(Exception("hash mismatch")) == "verification"


def test_categorize_error_unknown():
    assert categorize_error(Exception("something else")) == "unknown"


# --- can_retry_automatically (pure) ---


def test_can_retry_automatically_connection():
    assert can_retry_automatically("connection") is True


def test_can_retry_automatically_space():
    assert can_retry_automatically("space") is False


# --- can_retry, get_transfer_error_details (mock Session/Job) ---


def test_can_retry_true_when_under_limit():
    job = MagicMock()
    job.transfer_retry_count = 0
    job.transfer_max_retries = 3
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = job
    db.query.return_value.filter.return_value.first.return_value = job
    assert can_retry("j1", db) is True


def test_can_retry_false_when_over_limit():
    job = MagicMock()
    job.transfer_retry_count = 3
    job.transfer_max_retries = 3
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = job
    assert can_retry("j1", db) is False


def test_get_transfer_error_details_shape():
    job = MagicMock()
    job.transfer_error = "connection refused"
    job.transfer_retry_count = 1
    job.transfer_max_retries = 3
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = job
    got = get_transfer_error_details("j1", db)
    assert "job_id" in got
    assert got["job_id"] == "j1"
    assert got["error_message"] == "connection refused"
    assert got["error_category"] == "connection"
    assert "can_retry" in got
    assert "can_retry_automatically" in got
