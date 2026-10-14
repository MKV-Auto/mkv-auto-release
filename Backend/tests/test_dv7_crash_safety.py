"""#902: a stopped container never loses a library original.

The swap that puts a converted file in place is killed at every file
operation in turn (a ``Killed`` BaseException, which no ``except OSError`` in
the code can swallow, like SIGKILL). At the instant of the kill a complete
original or a complete verified new file must exist under one of the known
names, and ``recover`` must then end with exactly one file at the original
name, the original or the new one, and no leftovers.

Also: no rename ever targets an existing file (SMB's client deletes the
target first), library recovery re-queues or completes interrupted rows, and
the postprocess hook recovers its own files.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from api import models
from core import dv_remux
from core import library_catalog as lc
from core import library_destination as ld

ORIGINAL = b"DV7-ORIGINAL" * 1000
CONVERTED = b"DV8-CONVERTED" * 900


class Killed(BaseException):
    """The container going away mid-operation."""


def _setup(tmp_path):
    lib = tmp_path / "library" / "Movies" / "The Matrix (1999)"
    lib.mkdir(parents=True)
    src = lib / "The Matrix.4k.mkv"
    src.write_bytes(ORIGINAL)
    new = dv_remux.push_temp_path(src)
    new.write_bytes(CONVERTED)
    return src, new


def _names(src: Path, new: Path):
    return [src, new, dv_remux.backup_path(src)]


def _contents(src, new):
    return {p.read_bytes() for p in _names(src, new) if p.exists()}


def _kill_after(monkeypatch, n):
    """Let ``n`` rename/unlink/replace calls through, kill on the next."""
    count = {"n": 0}
    real_rename, real_replace, real_unlink = dv_remux.os.rename, dv_remux.os.replace, Path.unlink

    def gate():
        if count["n"] >= n:
            raise Killed()
        count["n"] += 1

    def rename(a, b):
        gate()
        return real_rename(a, b)

    def replace(a, b):
        gate()
        return real_replace(a, b)

    def unlink(self, missing_ok=False):
        gate()
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(dv_remux.os, "rename", rename)
    monkeypatch.setattr(dv_remux.os, "replace", replace)
    monkeypatch.setattr(Path, "unlink", unlink)
    return lambda: (monkeypatch.setattr(dv_remux.os, "rename", real_rename),
                    monkeypatch.setattr(dv_remux.os, "replace", real_replace),
                    monkeypatch.setattr(Path, "unlink", real_unlink))


# journal tmp->final replace, rename aside, rename in, unlink original,
# unlink journal: kill before each, and once after all of them.
@pytest.mark.parametrize("kill_at", range(6))
def test_a_kill_at_any_step_never_loses_the_original(monkeypatch, tmp_path, kill_at):
    src, new = _setup(tmp_path)
    restore = _kill_after(monkeypatch, kill_at)
    try:
        dv_remux._swap_in(src, new, "library")
        finished = True
    except Killed:
        finished = False
    restore()

    # At the moment of the kill: the original, or the verified new file, is whole.
    assert _contents(src, new) & {ORIGINAL, CONVERTED}
    assert ORIGINAL in _contents(src, new) or src.read_bytes() == CONVERTED

    entry = dv_remux.journal_entry_for(src)
    if entry is None and not finished:
        # Killed while writing the journal: nothing had moved yet. The temp
        # file is a leftover the row-level recovery deletes (tested below).
        assert src.read_bytes() == ORIGINAL
        new.unlink()
    if entry:
        dv_remux.recover(entry)
    assert src.read_bytes() in (ORIGINAL, CONVERTED)
    assert not new.exists() and not dv_remux.backup_path(src).exists()
    assert dv_remux.journal_entry_for(src) is None
    if finished:
        assert src.read_bytes() == CONVERTED


def test_no_rename_ever_lands_on_an_existing_file(monkeypatch, tmp_path):
    """The SMB client turns rename-over-existing into delete-then-rename."""
    src, new = _setup(tmp_path)
    real = dv_remux.os.rename

    def careful(a, b):
        assert not Path(b).exists(), f"rename onto existing {b}"
        return real(a, b)

    monkeypatch.setattr(dv_remux.os, "rename", careful)
    assert dv_remux._swap_in(src, new, "library") is None
    assert src.read_bytes() == CONVERTED


def test_a_failed_rename_in_puts_the_original_back(monkeypatch, tmp_path):
    src, new = _setup(tmp_path)
    real = dv_remux.os.rename

    def flaky(a, b):
        if Path(a) == new:
            raise OSError("share went away")
        return real(a, b)

    monkeypatch.setattr(dv_remux.os, "rename", flaky)
    error = dv_remux._swap_in(src, new, "library")
    monkeypatch.setattr(dv_remux.os, "rename", real)
    assert error and "restored" in error
    assert src.read_bytes() == ORIGINAL
    assert not dv_remux.backup_path(src).exists()


def test_a_short_file_at_the_final_name_is_rolled_back(monkeypatch, tmp_path):
    src, new = _setup(tmp_path)
    real_stat = Path.stat

    def stat(self, *a, **k):
        # The file at the final name reads one byte short (a truncated write).
        st = real_stat(self, *a, **k)
        if self == src and self.read_bytes() == CONVERTED:
            return SimpleNamespace(st_size=st.st_size - 1, st_mtime=st.st_mtime, st_mode=st.st_mode)
        return st

    monkeypatch.setattr(Path, "stat", stat)
    error = dv_remux._swap_in(src, new, "library")
    monkeypatch.setattr(Path, "stat", real_stat)
    assert error and "restored" in error
    assert src.read_bytes() == ORIGINAL


def test_a_leftover_set_aside_original_blocks_a_new_swap(tmp_path):
    src, new = _setup(tmp_path)
    dv_remux.backup_path(src).write_bytes(ORIGINAL)
    assert "needs recovering first" in dv_remux._swap_in(src, new, "library")
    assert src.read_bytes() == ORIGINAL and dv_remux.backup_path(src).exists()


# ── library-level recovery ──────────────────────────────────────────────

def _row(db, src, status=lc.CONVERTING):
    row = models.LibraryMediaFile(path=str(src), library_root=str(src.parents[2]), size=len(ORIGINAL),
                                  mtime=src.stat().st_mtime if src.exists() else 0, dv_profile=7,
                                  status=status)
    db.add(row)
    db.commit()
    return row


def _dest(tmp_path):
    root = str(tmp_path / "library")
    return lambda _db: ld.LibraryDestination(True, "smb", "Plex", "smb://nas/PLEX Media", root)


def test_recovery_restores_an_original_set_aside_mid_swap(test_db, monkeypatch, tmp_path):
    src, new = _setup(tmp_path)
    with test_db() as db:
        row = _row(db, src)
        restore = _kill_after(monkeypatch, 2)      # journal written, original set aside
        with pytest.raises(Killed):
            dv_remux._swap_in(src, new, "library", {"destination": "smb://nas/PLEX Media"})
        restore()
        assert not src.exists()

        assert lc.recover_interrupted(db, destination=_dest(tmp_path)) == [row.id]
        db.refresh(row)
    assert src.read_bytes() == ORIGINAL
    assert row.status == lc.QUEUED and row.error == lc.INTERRUPTED_NOTE
    assert not list(src.parent.glob(".dv7remux-*"))


def test_recovery_keeps_a_conversion_that_was_fully_in_place(test_db, monkeypatch, tmp_path):
    src, new = _setup(tmp_path)
    with test_db() as db:
        row = _row(db, src)
        restore = _kill_after(monkeypatch, 3)      # new file in place, original not yet deleted
        with pytest.raises(Killed):
            dv_remux._swap_in(src, new, "library", {"destination": "smb://nas/PLEX Media"})
        restore()

        assert lc.recover_interrupted(db, destination=_dest(tmp_path)) == []
        db.refresh(row)
    assert src.read_bytes() == CONVERTED
    assert row.status == lc.CONVERTED and row.dv_profile == 8 and row.size == len(CONVERTED)
    assert not list(src.parent.glob(".dv7remux-*"))


def test_recovery_requeues_a_conversion_that_never_reached_the_swap(test_db, tmp_path, monkeypatch):
    src, new = _setup(tmp_path)                    # a half-copied push temp, no journal
    work = dv_remux.work_dir_for(src)
    work.mkdir()
    (work / "BL_RPU.hevc").write_bytes(b"x")
    scratch = tmp_path / "scratch"
    (scratch / ".dv7remux-Other").mkdir(parents=True)
    monkeypatch.setattr(lc, "scratch_dir", lambda: scratch)
    with test_db() as db:
        row = _row(db, src)
        assert lc.recover_interrupted(db, destination=_dest(tmp_path)) == [row.id]
        db.refresh(row)
    assert src.read_bytes() == ORIGINAL and row.status == lc.QUEUED
    assert not new.exists() and not work.exists()
    assert not list(scratch.iterdir())


def test_recovery_waits_when_the_share_is_unreachable(test_db, monkeypatch, tmp_path):
    src, new = _setup(tmp_path)
    with test_db() as db:
        row = _row(db, src)
        restore = _kill_after(monkeypatch, 2)
        with pytest.raises(Killed):
            dv_remux._swap_in(src, new, "library", {"destination": "smb://nas/PLEX Media"})
        restore()
        down = lambda _db: ld.LibraryDestination(False, "smb", "Plex", "smb://nas/PLEX Media", problem="Host is down")
        assert lc.recover_interrupted(db, destination=down) == []
        db.refresh(row)
    assert row.status == lc.CONVERTING                       # untouched until it can be fixed
    assert dv_remux.backup_path(src).read_bytes() == ORIGINAL
    assert dv_remux.journal_entry_for(src) is not None


def test_recovery_leaves_a_swap_from_another_share_alone(test_db, monkeypatch, tmp_path):
    src, new = _setup(tmp_path)
    with test_db() as db:
        _row(db, src)
        restore = _kill_after(monkeypatch, 2)
        with pytest.raises(Killed):
            dv_remux._swap_in(src, new, "library", {"destination": "smb://old-nas/Media"})
        restore()
        lc.recover_interrupted(db, destination=_dest(tmp_path))
    assert dv_remux.backup_path(src).read_bytes() == ORIGINAL   # not touched
    assert dv_remux.journal_entry_for(src) is not None


def test_postprocess_hook_recovers_its_own_file_first(test_db, monkeypatch, tmp_path):
    from core import settings
    from workers.tasks import _maybe_convert_dv7_outputs

    monkeypatch.setattr(settings, "get_dv7_conversion", lambda: {"enabled": True, "convert_new_rips": True})
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    src = job_dir / "Movie.mkv"
    src.write_bytes(ORIGINAL)
    new = job_dir / ".dv7remux-Movie" / "out.mkv"
    new.parent.mkdir()
    new.write_bytes(CONVERTED)
    restore = _kill_after(monkeypatch, 2)
    with pytest.raises(Killed):
        dv_remux._swap_in(src, new, "postprocess")
    restore()
    assert not src.exists()

    monkeypatch.setattr(dv_remux, "needs_dv7_remux", lambda p: False)
    logs = []
    task = SimpleNamespace(add_log=lambda job, db, msg: logs.append(msg))
    _maybe_convert_dv7_outputs(task, SimpleNamespace(id="job-1"), None, job_dir, {"t1": "Movie.mkv"})
    assert src.read_bytes() == ORIGINAL
    assert any("recovered an interrupted conversion" in m for m in logs)
