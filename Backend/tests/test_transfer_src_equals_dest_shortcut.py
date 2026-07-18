"""
Coverage for ``_try_src_equals_dest_shortcut`` (#365 step 5b'b).

This helper is the production-safety blocker for step 5c (default the
``MKVAUTO_RENAME_DIRECT_TO_DEST`` flag on). Under flag-on + local mode
the rename step writes directly to ``config.transfer_dir`` and the
transfer step's helper resolves ``src_root`` to the same path. The
three downstream scenarios in :func:`transfer_job` (library_dirs /
use_final_map / regular) all assume ``src_root != dest_root`` and would
crash with :class:`shutil.SameFileError` or self-nest the destination.
The shortcut runs *before* those scenarios and either completes the
transfer via Segment UID identification or fails loud.

**Critical regression guard:** the "applies + completes" test asserts
``shutil.copy2`` is never invoked. A future refactor that accidentally
reverts the short-circuit would fall through to ``shutil.copy2(src,
dest)`` with the same path and fail this assertion loudly.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from api.routers.jobs import _try_src_equals_dest_shortcut


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _stub_completion_helpers(monkeypatch):
    """Replace _complete_transfer / _fail_transfer / _advance_transfer_phase
    with recording mocks so tests can assert call shape without touching
    the StageState machinery or DB writes those helpers normally do."""
    complete = MagicMock()
    fail = MagicMock()
    advance = MagicMock()
    monkeypatch.setattr("api.routers.jobs._complete_transfer", complete)
    monkeypatch.setattr("api.routers.jobs._fail_transfer", fail)
    monkeypatch.setattr("api.routers.jobs._advance_transfer_phase", advance)
    return SimpleNamespace(complete=complete, fail=fail, advance=advance)


def _block_copy(monkeypatch):
    """Sentinel mock on ``shutil.copy2`` so any accidental copy call in
    the shortcut path fails the test loudly. This is the regression
    guard against a future refactor reverting the shortcut."""
    sentinel = MagicMock(
        side_effect=AssertionError("shutil.copy2 must not be called in src==dest shortcut path"),
    )
    monkeypatch.setattr("shutil.copy2", sentinel)
    return sentinel


def _fake_titles(*uids):
    """Build DiscTitle-shaped fakes with the given segment_uids."""
    return [SimpleNamespace(id=f"t-{i}", segment_uid=uid) for i, uid in enumerate(uids)]


def _fake_job(*, titles=None, post_paths=None):
    """Job-shaped fake with .disc.titles and .post_paths populated."""
    disc = SimpleNamespace(titles=titles or []) if titles is not None else None
    return SimpleNamespace(
        id="j-1",
        disc=disc,
        post_paths=post_paths or {},
        stage_profile="miss",
    )


# ──────────────────────────────────────────────────────────────────────────
# Not-applicable cases — helper returns False, no side effects
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["rsync", "smb", "nfs"])
def test_returns_false_for_remote_modes(monkeypatch, tmp_path, mode):
    """Remote modes always use transient/ staging; the shortcut never
    applies regardless of whether transfer_dir happens to equal src_root.
    Returning False lets the caller hit the remote background path."""
    stubs = _stub_completion_helpers(monkeypatch)
    _block_copy(monkeypatch)
    config = SimpleNamespace(mode=mode, transfer_dir=str(tmp_path))
    result = _try_src_equals_dest_shortcut(
        _fake_job(), db=None, src_root=tmp_path.resolve(),
        config=config, job_metadata={},
    )
    assert result is False
    stubs.complete.assert_not_called()
    stubs.fail.assert_not_called()
    stubs.advance.assert_not_called()


def test_returns_false_when_src_neq_dest(monkeypatch, tmp_path):
    """Flag-off case: src_root resolves to paths.transient while
    config.transfer_dir is the library. They don't match → shortcut
    declines → caller runs the normal copy scenarios."""
    stubs = _stub_completion_helpers(monkeypatch)
    _block_copy(monkeypatch)
    src = (tmp_path / "transient").resolve()
    src.mkdir()
    dest = (tmp_path / "library").resolve()
    dest.mkdir()
    config = SimpleNamespace(mode="local", transfer_dir=str(dest))
    result = _try_src_equals_dest_shortcut(
        _fake_job(), db=None, src_root=src,
        config=config, job_metadata={},
    )
    assert result is False
    stubs.complete.assert_not_called()


def test_returns_false_when_transfer_dir_blank(monkeypatch, tmp_path):
    """Misconfigured local config (empty transfer_dir) — shortcut
    declines so the caller's existing validation surface reports it
    rather than the shortcut silently mismatching on Path('') vs src."""
    stubs = _stub_completion_helpers(monkeypatch)
    _block_copy(monkeypatch)
    config = SimpleNamespace(mode="local", transfer_dir="")
    result = _try_src_equals_dest_shortcut(
        _fake_job(), db=None, src_root=tmp_path.resolve(),
        config=config, job_metadata={},
    )
    assert result is False
    stubs.complete.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────
# Applicable + completes — the happy path that makes flag-on production-safe
# ──────────────────────────────────────────────────────────────────────────


def test_applies_and_completes_with_full_uid_match(monkeypatch, tmp_path):
    """Local + src==dest + every post_paths file is present and its
    Segment UID matches an expected title. Helper completes the transfer
    without copying. This IS the production-safety guarantee for
    MKVAUTO_RENAME_DIRECT_TO_DEST=1."""
    stubs = _stub_completion_helpers(monkeypatch)
    copy_sentinel = _block_copy(monkeypatch)

    dest = (tmp_path / "library").resolve()
    movies = dest / "Movies" / "Foo (2024)"
    movies.mkdir(parents=True)
    f1 = movies / "Foo (2024).mkv"
    f1.write_bytes(b"x")
    f2 = movies / "Foo (2024) - bonus.mkv"
    f2.write_bytes(b"x")

    titles = _fake_titles("uid-a", "uid-b")
    job = _fake_job(
        titles=titles,
        post_paths={
            titles[0].id: "Movies/Foo (2024)/Foo (2024).mkv",
            titles[1].id: "Movies/Foo (2024)/Foo (2024) - bonus.mkv",
        },
    )

    # read_segment_uid is the identity oracle — stub it to return the
    # expected UIDs in the same order as the post_paths values.
    uid_by_path = {str(f1.resolve()): "uid-a", str(f2.resolve()): "uid-b"}
    monkeypatch.setattr(
        "core.mkv_identity.read_segment_uid",
        lambda p: uid_by_path.get(p),
    )

    config = SimpleNamespace(mode="local", transfer_dir=str(dest))
    result = _try_src_equals_dest_shortcut(
        job, db=None, src_root=dest, config=config, job_metadata={"k": "v"},
    )

    assert result is True
    stubs.fail.assert_not_called()
    # Regression guard: NO copy happened.
    copy_sentinel.assert_not_called()
    # Sub-phase advanced to "verifying" before completion.
    stubs.advance.assert_called_once()
    advance_args = stubs.advance.call_args
    assert advance_args.args[2] == "verifying"
    # _complete_transfer called with the resolved dest paths and metadata.
    stubs.complete.assert_called_once()
    cc_args = stubs.complete.call_args
    assert cc_args.args[0] is job
    dest_paths_arg = cc_args.args[2]
    assert sorted(dest_paths_arg) == sorted([str(f1.resolve()), str(f2.resolve())])
    assert cc_args.args[3] == {"k": "v"}


# ──────────────────────────────────────────────────────────────────────────
# Fail-loud cases — applicable but precondition violated
# ──────────────────────────────────────────────────────────────────────────


def test_pre451_legacy_job_no_uids_fails_loud(monkeypatch, tmp_path):
    """Pre-#451 jobs never captured ``segment_uid``. Falling through to
    the broken copy paths would crash or self-nest; falling back to a
    filename-based predicate would defeat the point of pulling #448
    forward. So we fail loud and direct the operator at the real
    remediation — re-rip or ``mkvmerge -J`` backfill (the old
    ``MKVAUTO_RENAME_DIRECT_TO_DEST=0`` opt-out was removed in 5d.1)."""
    stubs = _stub_completion_helpers(monkeypatch)
    _block_copy(monkeypatch)
    titles = _fake_titles(None, None)  # No segment_uids — legacy job.
    job = _fake_job(titles=titles, post_paths={"t-0": "Movies/X/X.mkv"})
    dest = tmp_path.resolve()
    config = SimpleNamespace(mode="local", transfer_dir=str(dest))

    with pytest.raises(HTTPException) as excinfo:
        _try_src_equals_dest_shortcut(
            job, db=None, src_root=dest, config=config, job_metadata={},
        )
    assert excinfo.value.status_code == 500
    assert "segment_uids" in excinfo.value.detail
    # Error message should point operators at the real remediation.
    assert "mkvmerge" in excinfo.value.detail or "re-rip" in excinfo.value.detail
    stubs.fail.assert_called_once()
    stubs.complete.assert_not_called()


def test_no_disc_fails_loud(monkeypatch, tmp_path):
    """Defensive: job without a linked disc → no titles → no UIDs →
    same fail-loud path as a legacy job. Guards against an unexpected
    edge case becoming silent file loss."""
    stubs = _stub_completion_helpers(monkeypatch)
    _block_copy(monkeypatch)
    job = SimpleNamespace(id="j-1", disc=None, post_paths={"t-0": "x.mkv"}, stage_profile="miss")
    dest = tmp_path.resolve()
    config = SimpleNamespace(mode="local", transfer_dir=str(dest))

    with pytest.raises(HTTPException) as excinfo:
        _try_src_equals_dest_shortcut(
            job, db=None, src_root=dest, config=config, job_metadata={},
        )
    assert excinfo.value.status_code == 500
    stubs.fail.assert_called_once()
    stubs.complete.assert_not_called()


def test_count_mismatch_when_file_missing_fails_loud(monkeypatch, tmp_path):
    """Two expected UIDs, only one file on disk → count mismatch → fail
    loud. Completing with a partial set would silently lose a title."""
    stubs = _stub_completion_helpers(monkeypatch)
    _block_copy(monkeypatch)

    dest = (tmp_path / "library").resolve()
    dest.mkdir()
    present = dest / "a.mkv"
    present.write_bytes(b"x")
    # b.mkv is referenced in post_paths but never created on disk.

    titles = _fake_titles("uid-a", "uid-b")
    job = _fake_job(
        titles=titles,
        post_paths={titles[0].id: "a.mkv", titles[1].id: "b.mkv"},
    )
    monkeypatch.setattr(
        "core.mkv_identity.read_segment_uid",
        lambda p: "uid-a" if p.endswith("a.mkv") else None,
    )
    config = SimpleNamespace(mode="local", transfer_dir=str(dest))

    with pytest.raises(HTTPException) as excinfo:
        _try_src_equals_dest_shortcut(
            job, db=None, src_root=dest, config=config, job_metadata={},
        )
    assert excinfo.value.status_code == 500
    assert "1/2" in excinfo.value.detail
    stubs.fail.assert_called_once()
    stubs.complete.assert_not_called()


def test_uid_mismatch_at_expected_path_fails_loud(monkeypatch, tmp_path):
    """File present at the expected path but its segment_uid doesn't
    match any expected title. This indicates a wrong-rip-at-dest or
    library reorganisation that put a different rip's file at our
    expected slot — completing here would mark the wrong file as ours.
    Fail loud."""
    stubs = _stub_completion_helpers(monkeypatch)
    _block_copy(monkeypatch)

    dest = (tmp_path / "library").resolve()
    dest.mkdir()
    f = dest / "a.mkv"
    f.write_bytes(b"x")

    titles = _fake_titles("uid-expected")
    job = _fake_job(
        titles=titles,
        post_paths={titles[0].id: "a.mkv"},
    )
    # The file at the expected path has a DIFFERENT UID.
    monkeypatch.setattr(
        "core.mkv_identity.read_segment_uid",
        lambda p: "uid-some-other-rip",
    )
    config = SimpleNamespace(mode="local", transfer_dir=str(dest))

    with pytest.raises(HTTPException) as excinfo:
        _try_src_equals_dest_shortcut(
            job, db=None, src_root=dest, config=config, job_metadata={},
        )
    assert excinfo.value.status_code == 500
    assert "0/1" in excinfo.value.detail
    stubs.fail.assert_called_once()
    stubs.complete.assert_not_called()


def test_read_segment_uid_returns_none_treated_as_mismatch(monkeypatch, tmp_path):
    """``read_segment_uid`` returning None (mkvmerge failure, corrupted
    container, missing binary) for a file means we cannot confirm
    identity. Treat as missing → count mismatch → fail loud. This is
    intentional: silent fall-through is the failure mode the whole
    primitive was designed to prevent."""
    stubs = _stub_completion_helpers(monkeypatch)
    _block_copy(monkeypatch)

    dest = (tmp_path / "library").resolve()
    dest.mkdir()
    (dest / "a.mkv").write_bytes(b"x")
    titles = _fake_titles("uid-a")
    job = _fake_job(
        titles=titles,
        post_paths={titles[0].id: "a.mkv"},
    )
    monkeypatch.setattr("core.mkv_identity.read_segment_uid", lambda p: None)
    config = SimpleNamespace(mode="local", transfer_dir=str(dest))

    with pytest.raises(HTTPException) as excinfo:
        _try_src_equals_dest_shortcut(
            job, db=None, src_root=dest, config=config, job_metadata={},
        )
    assert excinfo.value.status_code == 500
    stubs.fail.assert_called_once()
    stubs.complete.assert_not_called()
