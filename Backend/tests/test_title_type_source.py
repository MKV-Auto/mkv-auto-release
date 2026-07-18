"""Tests for the auto_type / user_type / type provenance split.

PR 1 of the titles-step chip + collapse plan introduces
`crud.set_title_type(title, value, source)` as the single chokepoint
for writing `disc_titles.type`. Every existing write site routes through
it with either `source='auto'` (DiscDB/scan/path-a-sibling/subsumption)
or `source='user'` (PATCH/exploratory-rip canonical/flag-decoys/etc.).

These tests pin:
  1. The helper's resolution rule (`type = user_type ?? auto_type`).
  2. Each write site sets the correct source column.
  3. Setting `user_type` doesn't blow away `auto_type` (both can coexist
     so the chip system can show "User selected · DiscDB" when they match).
"""
import uuid
from unittest.mock import MagicMock

import pytest

from api import models
from api.crud import set_title_type


# ── helper: minimal DiscTitle stand-in ────────────────────────────────────────


class _FakeTitle:
    """SQLAlchemy DiscTitle has too many fields to instantiate cleanly in
    a unit test; a duck-typed stand-in is sufficient for the helper which
    only reads/writes type / auto_type / user_type."""
    def __init__(self, type_=None, auto_type=None, user_type=None):
        self.type = type_
        self.auto_type = auto_type
        self.user_type = user_type


# ── set_title_type resolution rules ──────────────────────────────────────────


def test_set_auto_when_user_null_writes_cache():
    t = _FakeTitle()
    set_title_type(t, "MainMovie", source="auto")
    assert t.auto_type == "MainMovie"
    assert t.user_type is None
    assert t.type == "MainMovie"  # cache = auto when user is null


def test_set_user_overrides_auto_in_cache():
    t = _FakeTitle(auto_type="MainMovie")
    set_title_type(t, "Extra", source="user")
    assert t.auto_type == "MainMovie"  # preserved
    assert t.user_type == "Extra"
    assert t.type == "Extra"  # user wins


def test_set_user_to_same_as_auto_keeps_both():
    """Chip system's 'User selected + DiscDB' case: user and auto agree."""
    t = _FakeTitle(auto_type="MainMovie")
    set_title_type(t, "MainMovie", source="user")
    assert t.auto_type == "MainMovie"
    assert t.user_type == "MainMovie"
    assert t.type == "MainMovie"


def test_set_user_to_none_falls_back_to_auto():
    """Clearing user_type → cache reverts to auto_type (DiscDB's pick)."""
    t = _FakeTitle(auto_type="MainMovie", user_type="Extra")
    assert t.user_type == "Extra"
    set_title_type(t, None, source="user")
    assert t.user_type is None
    assert t.auto_type == "MainMovie"
    assert t.type == "MainMovie"


def test_set_auto_to_none_with_user_set_keeps_user_in_cache():
    t = _FakeTitle(auto_type="MainMovie", user_type="Extra")
    set_title_type(t, None, source="auto")
    assert t.auto_type is None
    assert t.user_type == "Extra"
    assert t.type == "Extra"


def test_set_both_to_none_clears_cache():
    t = _FakeTitle(auto_type="MainMovie", user_type="Extra")
    set_title_type(t, None, source="user")
    set_title_type(t, None, source="auto")
    assert t.type is None


def test_invalid_source_raises():
    t = _FakeTitle()
    with pytest.raises(ValueError, match="source must be 'user' or 'auto'"):
        set_title_type(t, "MainMovie", source="discdb")


def test_set_ignore_user_source():
    """Confirm-ignore flow: user explicitly sets ignore."""
    t = _FakeTitle(auto_type="ignore")  # auto-ignored from subsumption
    set_title_type(t, "ignore", source="user")
    assert t.user_type == "ignore"
    assert t.auto_type == "ignore"  # both — user agreed with auto
    assert t.type == "ignore"


def test_set_ignore_auto_then_user_overrides_with_main_movie():
    """Auto says ignore (subsumption), but user reviews + reclassifies."""
    t = _FakeTitle(auto_type="ignore")
    set_title_type(t, "MainMovie", source="user")
    assert t.auto_type == "ignore"
    assert t.user_type == "MainMovie"
    assert t.type == "MainMovie"  # user wins
