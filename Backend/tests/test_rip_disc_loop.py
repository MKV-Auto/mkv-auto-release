"""Tests for the per-title selective-rip loop in Disc.rip().

Default rip path (rip_set=None): single `mkv DEV all OUT` invocation, identical
to today. Selective-rip path (rip_set=[i,j,k]): per-title loop, one
makemkvcon invocation per index.

Mocks run_makemkv to capture the invocation pattern without touching
a real disc.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from core.disc import Disc


@pytest.fixture
def disc(tmp_path):
    """A minimally-initialized Disc whose `rip()` we can drive."""
    d = Disc(disc_num="1", mount_point="/dev/sr1")
    # _makemkv_source_spec reads mount_point; nothing else needed for these tests.
    d.titles = {}
    d.db_mapping = {}
    d.movie_name = None
    d.release_image = None
    d.disc_slug = None
    d.resolution = None
    d.disc_format = None
    d.title_type = None
    d.disc_hash = None
    d.release_year = None
    d.release_date = None
    d.original_year = None
    d.original_release_date = None
    d.raw_db_query = None
    d.errors = []
    return d


def test_default_path_invokes_all_mode_unchanged(disc, tmp_path):
    """When rip_set is None, behavior is identical to today: single `mkv DEV all OUT`."""
    out = tmp_path / "out"
    out.mkdir()
    with patch("core.disc.run_makemkv") as run_mk:
        run_mk.return_value = ("MSG:5036,260,1,\"Copy complete. 1 titles saved.\"", 12345)
        disc.rip(str(out), mode="copy")
    assert run_mk.call_count == 1
    args = run_mk.call_args[0][0]
    assert " all " in args
    assert "mkv " in args
    # No per-title indexes
    assert " 0 " not in args
    assert " 1 " not in args


def test_selective_rip_invokes_per_title(disc, tmp_path):
    """When rip_set is set, one invocation per index, each with the index in the args."""
    out = tmp_path / "out"
    out.mkdir()
    with patch("core.disc.run_makemkv") as run_mk:
        run_mk.return_value = ("MSG:5036,260,1,\"Copy complete. 1 titles saved.\"", 12345)
        disc.rip(str(out), mode="copy", rip_set=[0, 5, 9])

    assert run_mk.call_count == 3
    invoked_args = [c.args[0] for c in run_mk.call_args_list]
    # Each invocation targets one specific title index.
    assert any(" 0 " in a for a in invoked_args), f"missing title 0 in {invoked_args}"
    assert any(" 5 " in a for a in invoked_args), f"missing title 5 in {invoked_args}"
    assert any(" 9 " in a for a in invoked_args), f"missing title 9 in {invoked_args}"
    # No invocation uses `all`.
    assert all(" all " not in a for a in invoked_args)


def test_selective_rip_uses_minlength_zero(disc, tmp_path):
    """Per-title invocation uses --minlength=0 to bypass the global filter
    (we already know exactly which titles we want)."""
    out = tmp_path / "out"
    out.mkdir()
    with patch("core.disc.run_makemkv") as run_mk:
        run_mk.return_value = ("", None)
        disc.rip(str(out), mode="copy", rip_set=[42])
    args = run_mk.call_args[0][0]
    assert "--minlength=0" in args


def test_selective_rip_returns_last_pid(disc, tmp_path):
    """The returned PID is from the last invocation, used by the worker for cancel/recovery."""
    out = tmp_path / "out"
    out.mkdir()
    with patch("core.disc.run_makemkv") as run_mk:
        run_mk.side_effect = [("", 100), ("", 200), ("", 300)]
        pid = disc.rip(str(out), mode="copy", rip_set=[0, 1, 2])
    assert pid == 300


def test_selective_rip_passes_log_hook_to_each_invocation(disc, tmp_path):
    """The progress log_hook should be wired into every per-title invocation."""
    out = tmp_path / "out"
    out.mkdir()
    hook = MagicMock()
    with patch("core.disc.run_makemkv") as run_mk:
        run_mk.return_value = ("", None)
        disc.rip(str(out), mode="copy", rip_set=[0, 1], log_hook=hook)
    # Every invocation got the hook.
    for call in run_mk.call_args_list:
        assert call.kwargs.get("line_cb") is hook
    # And the hook saw the per-title progress messages.
    hook_msgs = [c.args[0] for c in hook.call_args_list]
    assert any("Selective rip: title 0" in m for m in hook_msgs)
    assert any("Selective rip: title 1" in m for m in hook_msgs)


def test_empty_rip_set_falls_through_to_all_mode(disc, tmp_path):
    """An empty rip_set is treated like None — caller is asking for the
    default. Better to do something safe than nothing on a typo."""
    out = tmp_path / "out"
    out.mkdir()
    with patch("core.disc.run_makemkv") as run_mk:
        run_mk.return_value = ("", None)
        disc.rip(str(out), mode="copy", rip_set=[])
    assert run_mk.call_count == 1
    assert " all " in run_mk.call_args[0][0]


def test_backup_mode_ignores_rip_set(disc, tmp_path):
    """Backup mode is for full-disc dumps; rip_set has no meaning here."""
    out = tmp_path / "out"
    out.mkdir()
    with patch("core.disc.run_makemkv") as run_mk:
        run_mk.return_value = ("", None)
        disc.rip(str(out), mode="backup", rip_set=[0, 1])
    # Single backup invocation, ignoring rip_set.
    assert run_mk.call_count == 1
    assert "backup" in run_mk.call_args[0][0]
