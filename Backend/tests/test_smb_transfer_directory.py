"""Tests for SMB directory transfer (full remote paths, mkdir fail-fast)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.transfer.monitoring import SpeedTracker
from core.transfer.protocols import smb as smb_mod
from core.transfer.protocols.smb import _transfer_smb_directory_smbclient


@pytest.fixture
def transient_with_movie(tmp_path: Path) -> Path:
    """jobs/<id>/transient/Movies/Test (2020)/file.mkv layout."""
    root = tmp_path / "transient"
    movie_dir = root / "Movies" / "Test (2020)"
    movie_dir.mkdir(parents=True)
    (movie_dir / "feature.mkv").write_bytes(b"0" * 2048)
    return root


def _speed() -> SpeedTracker:
    st = SpeedTracker()
    st.start()
    return st


def test_smb_directory_put_uses_full_remote_path_not_cd_chain(transient_with_movie: Path) -> None:
    """put must target full path from share root (Movies/...), not cd+basename."""
    captured: list[str] = []

    def fake_run(cmd: list, **kwargs):  # type: ignore[no-untyped-def]
        assert cmd[0] == "smbclient"
        flag_idx = cmd.index("-c")
        captured.append(cmd[flag_idx + 1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch.object(smb_mod.shutil, "which", return_value="/usr/bin/smbclient"), patch.object(
        smb_mod.subprocess, "run", side_effect=fake_run
    ):
        result = _transfer_smb_directory_smbclient(
            job_id="job-1",
            src_path=transient_with_movie,
            host="10.0.0.5",
            share="Media",
            path="PLEX Media",
            port=445,
            username="u",
            password="p",
            domain="",
            source_hash=None,
            speed_tracker=_speed(),
            progress_callback=None,
            speed_callback=None,
        )

    assert result.get("success") is True
    put_cmds = [c for c in captured if c.strip().lower().startswith("put ")]
    assert put_cmds, "expected at least one put command"
    full_put = put_cmds[0]
    assert "cd " not in full_put.lower(), "should not rely on cd before put"
    assert "PLEX Media/Movies/Test (2020)/feature.mkv" in full_put
    assert "feature.mkv" in full_put

    ls_cmds = [c for c in captured if c.strip().lower().startswith("ls ")]
    assert ls_cmds, "expected ls verify"
    assert "PLEX Media/Movies/Test (2020)/feature.mkv" in ls_cmds[0]
    assert "cd " not in ls_cmds[0].lower()


def test_smb_directory_transfer_fails_when_mkdir_not_benign(transient_with_movie: Path) -> None:
    """Non-benign mkdir failure must abort before put (no silent flat upload)."""
    mkdir_count = 0

    def fake_run(cmd: list, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal mkdir_count
        flag_idx = cmd.index("-c")
        inner = cmd[flag_idx + 1]
        if inner.startswith("mkdir "):
            mkdir_count += 1
            # First mkdir ok; second fails hard (not collision)
            if mkdir_count >= 2:
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="NT_STATUS_ACCESS_DENIED creating directory",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if inner.startswith("put "):
            pytest.fail("put must not run after failed mkdir")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch.object(smb_mod.shutil, "which", return_value="/usr/bin/smbclient"), patch.object(
        smb_mod.subprocess, "run", side_effect=fake_run
    ):
        result = _transfer_smb_directory_smbclient(
            job_id="job-2",
            src_path=transient_with_movie,
            host="10.0.0.5",
            share="Media",
            path="PLEX Media",
            port=445,
            username="u",
            password="p",
            domain="",
            source_hash=None,
            speed_tracker=_speed(),
            progress_callback=None,
            speed_callback=None,
        )

    assert result.get("success") is False
    err = (result.get("error") or "").lower()
    assert (
        "mkdir" in err
        or "destination base" in err
        or "access_denied" in err.replace("_", "")
        or "failed to transfer" in err
    )


def test_smb_mkdir_collision_still_ok(transient_with_movie: Path) -> None:
    """Benign NT_STATUS_OBJECT_NAME_COLLISION should not abort mkdir."""
    captured: list[str] = []

    def fake_run(cmd: list, **kwargs):  # type: ignore[no-untyped-def]
        flag_idx = cmd.index("-c")
        inner = cmd[flag_idx + 1]
        if inner.startswith("mkdir "):
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="NT_STATUS_OBJECT_NAME_COLLISION",
            )
        captured.append(inner)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch.object(smb_mod.shutil, "which", return_value="/usr/bin/smbclient"), patch.object(
        smb_mod.subprocess, "run", side_effect=fake_run
    ):
        result = _transfer_smb_directory_smbclient(
            job_id="job-3",
            src_path=transient_with_movie,
            host="h",
            share="s",
            path="root",
            port=445,
            username="",
            password="",
            domain="",
            source_hash=None,
            speed_tracker=_speed(),
            progress_callback=None,
            speed_callback=None,
        )

    assert result.get("success") is True
    assert any(c.strip().lower().startswith("put ") for c in captured)


# Regression: smbclient often exits 0 on mkdir while writing NT_STATUS_ACCESS_DENIED
# to stdout/stderr (the "silent" failure that produced the V for Vendetta cascaded
# OBJECT_PATH_NOT_FOUND from put). We must surface the real error.

def test_smb_run_mkdir_detects_silent_access_denied_in_stdout() -> None:
    """`_smb_run_mkdir` must NOT trust returncode=0 alone — scan stdout for NT_STATUS errors."""
    from core.transfer.protocols.smb import _smb_run_mkdir

    def fake_run(cmd: list, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            returncode=0,
            stdout="NT_STATUS_ACCESS_DENIED making remote directory \\Movies\\X (2024)\n",
            stderr="",
        )

    with patch.object(smb_mod.subprocess, "run", side_effect=fake_run):
        ok, err = _smb_run_mkdir("//host/share", ["-U%", "-N"], "Movies/X (2024)")

    assert ok is False, "mkdir must fail when stdout contains NT_STATUS_ACCESS_DENIED, even at exit 0"
    assert "NT_STATUS_ACCESS_DENIED" in err
    assert "Movies/X (2024)" in err


def test_smb_run_mkdir_benign_collision_with_exit_zero_still_succeeds() -> None:
    """Benign markers in output remain success regardless of returncode."""
    from core.transfer.protocols.smb import _smb_run_mkdir

    def fake_run(cmd: list, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            returncode=0,
            stdout="NT_STATUS_OBJECT_NAME_COLLISION making remote directory \\Movies\n",
            stderr="",
        )

    with patch.object(smb_mod.subprocess, "run", side_effect=fake_run):
        ok, err = _smb_run_mkdir("//host/share", ["-U%", "-N"], "Movies")

    assert ok is True
    assert err == ""


def test_smb_run_mkdir_clean_output_with_exit_zero_succeeds() -> None:
    """Clean smbclient output (no NT_STATUS_*) at exit 0 is a real success."""
    from core.transfer.protocols.smb import _smb_run_mkdir

    def fake_run(cmd: list, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch.object(smb_mod.subprocess, "run", side_effect=fake_run):
        ok, err = _smb_run_mkdir("//host/share", ["-U%", "-N"], "Movies/Title (2024)")

    assert ok is True
    assert err == ""


def test_smb_directory_silent_mkdir_failure_aborts_before_put(transient_with_movie: Path) -> None:
    """End-to-end: mkdir exits 0 with NT_STATUS_ACCESS_DENIED in stdout — put must NOT run."""
    captured: list[str] = []
    mkdir_count = 0

    def fake_run(cmd: list, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal mkdir_count
        flag_idx = cmd.index("-c")
        inner = cmd[flag_idx + 1]
        captured.append(inner)
        if inner.startswith("mkdir "):
            mkdir_count += 1
            # First mkdir (parent "Movies") returns benign collision (already exists).
            # Second mkdir (deeper path) silently fails with ACCESS_DENIED in stdout, exit 0.
            if mkdir_count == 1:
                return SimpleNamespace(
                    returncode=0,
                    stdout="NT_STATUS_OBJECT_NAME_COLLISION making remote directory \\Movies\n",
                    stderr="",
                )
            return SimpleNamespace(
                returncode=0,
                stdout="NT_STATUS_ACCESS_DENIED making remote directory \\Movies\\Test (2020)\n",
                stderr="",
            )
        if inner.startswith("put "):
            pytest.fail("put must not run when mkdir silently failed")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch.object(smb_mod.shutil, "which", return_value="/usr/bin/smbclient"), patch.object(
        smb_mod.subprocess, "run", side_effect=fake_run
    ):
        result = _transfer_smb_directory_smbclient(
            job_id="job-silent-mkdir",
            src_path=transient_with_movie,
            host="10.0.0.5",
            share="Media",
            path="",  # transient → root, so per-file mkdir handles Movies/...
            port=445,
            username="",
            password="",
            domain="",
            source_hash=None,
            speed_tracker=_speed(),
            progress_callback=None,
            speed_callback=None,
        )

    assert result.get("success") is False
    err = result.get("error") or ""
    # Real cause must surface in transfer_error, not the cascaded OBJECT_PATH_NOT_FOUND from put.
    assert "NT_STATUS_ACCESS_DENIED" in err, f"expected ACCESS_DENIED in error, got: {err!r}"
    assert "Movies/Test (2020)" in err or "Test (2020)" in err
    # And no put or ls verify must have run for that file.
    assert not any(c.strip().lower().startswith("put ") for c in captured)


def test_smb_put_with_silent_nt_status_in_stdout_is_treated_as_failure(transient_with_movie: Path) -> None:
    """Defense in depth: if mkdir succeeds but put exits 0 with NT_STATUS in stdout,
    the file must still be reported as failed (not falsely verified)."""
    captured: list[str] = []

    def fake_run(cmd: list, **kwargs):  # type: ignore[no-untyped-def]
        flag_idx = cmd.index("-c")
        inner = cmd[flag_idx + 1]
        captured.append(inner)
        if inner.startswith("mkdir "):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if inner.startswith("put "):
            # Simulate smbclient exit 0 with hidden write error.
            return SimpleNamespace(
                returncode=0,
                stdout="NT_STATUS_DISK_FULL writing remote file\n",
                stderr="",
            )
        # ls verify path — should not be reached if put was flagged.
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch.object(smb_mod.shutil, "which", return_value="/usr/bin/smbclient"), patch.object(
        smb_mod.subprocess, "run", side_effect=fake_run
    ):
        result = _transfer_smb_directory_smbclient(
            job_id="job-silent-put",
            src_path=transient_with_movie,
            host="h",
            share="s",
            path="",
            port=445,
            username="",
            password="",
            domain="",
            source_hash=None,
            speed_tracker=_speed(),
            progress_callback=None,
            speed_callback=None,
        )

    assert result.get("success") is False
    err = result.get("error") or ""
    assert "NT_STATUS_DISK_FULL" in err


# #635 commit A — reactive overwrite fallback: on NT_STATUS_ACCESS_DENIED
# when conflict_resolution=overwrite, delete the existing file at destination
# and retry the put once. Write-once SMB shares (Unraid/Synology default) hit
# this every re-rip.


def _make_put_then_delete_then_put_stub(put_returns: list, del_returns: list):
    """Sequence subprocess.run responses for mkdir/put/del/put/ls calls."""
    mkdir_seen = 0
    put_seen = 0
    del_seen = 0

    def fake_run(cmd: list, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal mkdir_seen, put_seen, del_seen
        flag_idx = cmd.index("-c")
        inner = cmd[flag_idx + 1]
        if inner.startswith("mkdir "):
            mkdir_seen += 1
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if inner.startswith("put "):
            i = min(put_seen, len(put_returns) - 1)
            put_seen += 1
            return put_returns[i]
        if inner.startswith("del "):
            i = min(del_seen, len(del_returns) - 1)
            del_seen += 1
            return del_returns[i]
        # ls verify → succeed
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return fake_run


def test_reactive_overwrite_fallback_deletes_then_retries_on_access_denied(
    transient_with_movie: Path,
) -> None:
    """conflict_resolution=overwrite + first put NT_STATUS_ACCESS_DENIED →
    del succeeds → retry put succeeds → whole transfer reports success."""
    put_returns = [
        # First put: silent access denied on stdout
        SimpleNamespace(returncode=0, stdout="NT_STATUS_ACCESS_DENIED", stderr=""),
        # Retry put: succeeds
        SimpleNamespace(returncode=0, stdout="", stderr=""),
    ]
    del_returns = [SimpleNamespace(returncode=0, stdout="", stderr="")]

    with patch.object(smb_mod.shutil, "which", return_value="/usr/bin/smbclient"), patch.object(
        smb_mod.subprocess, "run", side_effect=_make_put_then_delete_then_put_stub(put_returns, del_returns)
    ):
        result = _transfer_smb_directory_smbclient(
            job_id="job-reactive",
            src_path=transient_with_movie,
            host="h",
            share="s",
            path="root",
            port=445,
            username="",
            password="",
            domain="",
            source_hash=None,
            speed_tracker=_speed(),
            progress_callback=None,
            speed_callback=None,
            conflict_resolution="overwrite",
        )

    assert result.get("success") is True


def test_reactive_overwrite_fallback_skipped_when_conflict_resolution_not_overwrite(
    transient_with_movie: Path,
) -> None:
    """Non-overwrite intent (skip / fail / rename) must NOT trigger the delete
    retry — those strategies are supposed to handle collision differently and
    silently deleting the destination would violate user intent."""
    put_returns = [
        SimpleNamespace(returncode=0, stdout="NT_STATUS_ACCESS_DENIED", stderr=""),
    ]
    del_returns = [SimpleNamespace(returncode=0, stdout="", stderr="")]
    del_calls_seen: list[str] = []

    def fake_run(cmd: list, **kwargs):  # type: ignore[no-untyped-def]
        flag_idx = cmd.index("-c")
        inner = cmd[flag_idx + 1]
        if inner.startswith("del "):
            del_calls_seen.append(inner)
        stub = _make_put_then_delete_then_put_stub(put_returns, del_returns)
        return stub(cmd, **kwargs)

    with patch.object(smb_mod.shutil, "which", return_value="/usr/bin/smbclient"), patch.object(
        smb_mod.subprocess, "run", side_effect=fake_run
    ):
        result = _transfer_smb_directory_smbclient(
            job_id="job-skip",
            src_path=transient_with_movie,
            host="h",
            share="s",
            path="root",
            port=445,
            username="",
            password="",
            domain="",
            source_hash=None,
            speed_tracker=_speed(),
            progress_callback=None,
            speed_callback=None,
            conflict_resolution="skip",
        )

    assert result.get("success") is False
    assert "NT_STATUS_ACCESS_DENIED" in (result.get("error") or "")
    assert not del_calls_seen, "must not attempt delete when conflict_resolution != overwrite"


def test_reactive_overwrite_fallback_reports_failure_when_delete_denied(
    transient_with_movie: Path,
) -> None:
    """When delete itself is denied, transfer must fail with both errors surfaced."""
    put_returns = [
        SimpleNamespace(returncode=0, stdout="NT_STATUS_ACCESS_DENIED", stderr=""),
    ]
    del_returns = [
        # Delete also access-denied (write-locked share)
        SimpleNamespace(returncode=0, stdout="NT_STATUS_ACCESS_DENIED", stderr=""),
    ]

    with patch.object(smb_mod.shutil, "which", return_value="/usr/bin/smbclient"), patch.object(
        smb_mod.subprocess, "run", side_effect=_make_put_then_delete_then_put_stub(put_returns, del_returns)
    ):
        result = _transfer_smb_directory_smbclient(
            job_id="job-noput-nodel",
            src_path=transient_with_movie,
            host="h",
            share="s",
            path="root",
            port=445,
            username="",
            password="",
            domain="",
            source_hash=None,
            speed_tracker=_speed(),
            progress_callback=None,
            speed_callback=None,
            conflict_resolution="overwrite",
        )

    assert result.get("success") is False
    err = result.get("error") or ""
    assert "NT_STATUS_ACCESS_DENIED" in err
    assert "delete-then-retry fallback failed" in err


def test_reactive_overwrite_fallback_reports_failure_when_retry_put_fails(
    transient_with_movie: Path,
) -> None:
    """Retry put after successful delete still fails → surface the retry error."""
    put_returns = [
        SimpleNamespace(returncode=0, stdout="NT_STATUS_ACCESS_DENIED", stderr=""),
        SimpleNamespace(returncode=0, stdout="NT_STATUS_DISK_FULL", stderr=""),
    ]
    del_returns = [SimpleNamespace(returncode=0, stdout="", stderr="")]

    with patch.object(smb_mod.shutil, "which", return_value="/usr/bin/smbclient"), patch.object(
        smb_mod.subprocess, "run", side_effect=_make_put_then_delete_then_put_stub(put_returns, del_returns)
    ):
        result = _transfer_smb_directory_smbclient(
            job_id="job-retry-fails",
            src_path=transient_with_movie,
            host="h",
            share="s",
            path="root",
            port=445,
            username="",
            password="",
            domain="",
            source_hash=None,
            speed_tracker=_speed(),
            progress_callback=None,
            speed_callback=None,
            conflict_resolution="overwrite",
        )

    assert result.get("success") is False
    err = result.get("error") or ""
    assert "NT_STATUS_DISK_FULL" in err
    assert "after delete+retry" in err


def test_reactive_overwrite_default_conflict_resolution_is_overwrite(
    transient_with_movie: Path,
) -> None:
    """Callers that omit conflict_resolution get the default (overwrite) — same
    reactive behaviour applies. Guards against regressions where a new call site
    forgets to pass the argument."""
    put_returns = [
        SimpleNamespace(returncode=0, stdout="NT_STATUS_ACCESS_DENIED", stderr=""),
        SimpleNamespace(returncode=0, stdout="", stderr=""),
    ]
    del_returns = [SimpleNamespace(returncode=0, stdout="", stderr="")]

    with patch.object(smb_mod.shutil, "which", return_value="/usr/bin/smbclient"), patch.object(
        smb_mod.subprocess, "run", side_effect=_make_put_then_delete_then_put_stub(put_returns, del_returns)
    ):
        result = _transfer_smb_directory_smbclient(
            job_id="job-default",
            src_path=transient_with_movie,
            host="h",
            share="s",
            path="root",
            port=445,
            username="",
            password="",
            domain="",
            source_hash=None,
            speed_tracker=_speed(),
            progress_callback=None,
            speed_callback=None,
            # conflict_resolution intentionally omitted — default is "overwrite"
        )

    assert result.get("success") is True
