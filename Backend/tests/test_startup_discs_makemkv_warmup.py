"""Tests for MakeMKV-gated startup drive warmup and disc workflow health fields."""

from unittest.mock import Mock

import pytest

import core.startup_discs as startup_discs
import core.utils as u


@pytest.fixture
def reset_warmup_state():
    with startup_discs._warmup_state_lock:
        startup_discs._drive_warmup_pending_after_key = False
        startup_discs._last_warmup_error_kind = None
    yield
    with startup_discs._warmup_state_lock:
        startup_discs._drive_warmup_pending_after_key = False
        startup_discs._last_warmup_error_kind = None


def test_run_startup_drive_warmup_skips_when_not_installed(monkeypatch, reset_warmup_state):
    monkeypatch.setattr(
        "core.makemkv_updater.validate_makemkv_installation",
        Mock(return_value={"is_valid": False}),
    )
    mock_enum = Mock()
    monkeypatch.setattr(startup_discs, "startup_enumerate_and_rescan_loaded_discs", mock_enum)
    assert startup_discs.run_startup_drive_warmup_if_makemkv_ready() == []
    mock_enum.assert_not_called()


def test_run_startup_drive_warmup_runs_when_installed(monkeypatch, reset_warmup_state):
    monkeypatch.setattr(
        "core.makemkv_updater.validate_makemkv_installation",
        Mock(return_value={"is_valid": True}),
    )
    drives = [("0", "/dev/sr0")]
    mock_enum = Mock(return_value=drives)
    monkeypatch.setattr(startup_discs, "startup_enumerate_and_rescan_loaded_discs", mock_enum)
    assert startup_discs.run_startup_drive_warmup_if_makemkv_ready() == drives
    mock_enum.assert_called_once_with(reraise_if_registration_required=True)
    assert startup_discs.drive_warmup_pending_after_key() is False


def test_run_startup_drive_warmup_pending_on_registration_error(monkeypatch, reset_warmup_state):
    monkeypatch.setattr(
        "core.makemkv_updater.validate_makemkv_installation",
        Mock(return_value={"is_valid": True}),
    )
    monkeypatch.setattr(
        startup_discs,
        "startup_enumerate_and_rescan_loaded_discs",
        Mock(side_effect=u.MakeMKVError("253 registration required")),
    )
    assert startup_discs.run_startup_drive_warmup_if_makemkv_ready() == []
    assert startup_discs.drive_warmup_pending_after_key() is True
    assert startup_discs.get_warmup_state()[1] == "registration_required"


def test_run_startup_drive_warmup_non_registration_error_no_pending(monkeypatch, reset_warmup_state):
    monkeypatch.setattr(
        "core.makemkv_updater.validate_makemkv_installation",
        Mock(return_value={"is_valid": True}),
    )
    monkeypatch.setattr(
        startup_discs,
        "startup_enumerate_and_rescan_loaded_discs",
        Mock(side_effect=u.MakeMKVError("device busy")),
    )
    assert startup_discs.run_startup_drive_warmup_if_makemkv_ready() == []
    assert startup_discs.drive_warmup_pending_after_key() is False
    assert startup_discs.get_warmup_state()[1] == "makemkv_error"


def test_disc_workflow_block_fields_not_installed(reset_warmup_state):
    wf = startup_discs.disc_workflow_block_fields({"is_valid": False}, registration_expired=False)
    assert wf == {
        "disc_workflow_blocked": True,
        "disc_workflow_block_reason": "makemkv_not_installed",
    }


def test_disc_workflow_block_fields_expired_key(reset_warmup_state):
    wf = startup_discs.disc_workflow_block_fields({"is_valid": True}, registration_expired=True)
    assert wf["disc_workflow_blocked"] is True
    assert wf["disc_workflow_block_reason"] == "registration_required"


def test_disc_workflow_block_fields_pending_warmup(reset_warmup_state):
    startup_discs.record_drive_warmup_result(u.MakeMKVError("shareware period expired"))
    wf = startup_discs.disc_workflow_block_fields({"is_valid": True}, registration_expired=False)
    assert wf["disc_workflow_blocked"] is True
    assert wf["disc_workflow_block_reason"] == "registration_required"


def test_disc_workflow_block_fields_makemkv_error(reset_warmup_state):
    startup_discs.record_drive_warmup_result(u.MakeMKVError("I/O error"))
    wf = startup_discs.disc_workflow_block_fields({"is_valid": True}, registration_expired=False)
    assert wf["disc_workflow_blocked"] is True
    assert wf["disc_workflow_block_reason"] == "makemkv_error"


def test_disc_workflow_unblocked_after_success(reset_warmup_state):
    startup_discs.record_drive_warmup_result(u.MakeMKVError("253"))
    startup_discs.record_drive_warmup_result(None)
    wf = startup_discs.disc_workflow_block_fields({"is_valid": True}, registration_expired=False)
    assert wf["disc_workflow_blocked"] is False
    assert wf["disc_workflow_block_reason"] == "none"
