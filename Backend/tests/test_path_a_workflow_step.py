"""Tests for `workflow_step` persistence around the Path A flow.

Path A injects a new `exploratory_rip` workflow step in the breadcrumb between
`film` and `boxset` (miss profile). These unit tests cover the three mutation
points in `Backend/api/routers/jobs.py`:

1. Start of Path A — `start_rip_with_segment_reorder` sets
   `job.workflow_step = 'exploratory_rip'`. Covered indirectly via the helper
   below; the endpoint's mutation is just a literal assignment.
2. Canonical complete — `_maybe_advance_canonical_complete` advances the
   stage AND bumps `workflow_step` off `'exploratory_rip'` to the next step.
3. Cancel — `cancel_segment_reorder` clears `workflow_step` when it's still
   `'exploratory_rip'`.

We mock the job and db with simple attribute objects so the test stays away
from FastAPI / SQLAlchemy plumbing.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.routers.jobs import (
    _clear_path_a_canonical_obfuscation_flag,
    _mark_path_a_skipped_siblings_as_ignore,
    _maybe_advance_canonical_complete,
)


def _fake_db():
    db = SimpleNamespace()
    db.commit = MagicMock()
    db.refresh = MagicMock()
    return db


def _fake_job_at_canonical_pending(workflow_step="exploratory_rip"):
    return SimpleNamespace(
        id="job-001",
        segment_reorder_state={
            "stage": "canonical_ripping_pending",
            "exploratory_title_index": 40,
            "group_member_indexes": [1, 2, 3],
            "sorted_segment_key": "501,502,503",
            "submitted_order": ["503", "502", "501"],
            "matched_playlist_index": 109,
        },
        workflow_step=workflow_step,
    )


class TestMaybeAdvanceCanonicalComplete:
    def test_miss_branch_advances_stage_and_workflow_step_to_boxset(self):
        job = _fake_job_at_canonical_pending()
        db = _fake_db()

        advanced = _maybe_advance_canonical_complete(job, job.id, db, branch="miss")

        assert advanced is True
        assert job.segment_reorder_state["stage"] == "canonical_complete"
        assert job.workflow_step == "boxset"
        db.commit.assert_called_once()

    def test_hit_branch_advances_workflow_step_to_transfer(self):
        # #365 Phase 2 § 6.4 — the standalone postprocess WorkflowStep
        # was collapsed into transfer's "preparing" sub-phase, so the
        # hit branch now advances straight to workflow_step="transfer"
        # (was "postprocess" before the cleanup).
        job = _fake_job_at_canonical_pending()
        db = _fake_db()

        _maybe_advance_canonical_complete(job, job.id, db, branch="hit")

        assert job.segment_reorder_state["stage"] == "canonical_complete"
        assert job.workflow_step == "transfer"

    def test_does_nothing_when_stage_is_not_canonical_ripping_pending(self):
        job = _fake_job_at_canonical_pending(workflow_step="exploratory_rip")
        job.segment_reorder_state = {"stage": "exploratory_ripping"}
        db = _fake_db()

        advanced = _maybe_advance_canonical_complete(job, job.id, db, branch="miss")

        assert advanced is False
        # Stage and workflow_step both untouched.
        assert job.segment_reorder_state["stage"] == "exploratory_ripping"
        assert job.workflow_step == "exploratory_rip"
        db.commit.assert_not_called()

    def test_preserves_existing_workflow_step_when_not_exploratory_rip(self):
        """Defensive: if workflow_step has already been advanced past
        exploratory_rip (e.g. by a duplicate callback), don't clobber it."""
        job = _fake_job_at_canonical_pending(workflow_step="boxset")
        db = _fake_db()

        _maybe_advance_canonical_complete(job, job.id, db, branch="miss")

        # Stage still advances (idempotency on stage is the helper's job),
        # but workflow_step is left at boxset since it's already past
        # exploratory_rip.
        assert job.segment_reorder_state["stage"] == "canonical_complete"
        assert job.workflow_step == "boxset"

    def test_unknown_branch_falls_back_to_miss_destination(self):
        """Defensive: unrecognised branch values default to the miss path
        (boxset) rather than dropping the user into postprocess unexpectedly."""
        job = _fake_job_at_canonical_pending()
        db = _fake_db()

        _maybe_advance_canonical_complete(job, job.id, db, branch="unknown")

        assert job.workflow_step == "boxset"


class _FakeQuery:
    """Minimal SQLAlchemy-like query stand-in for the ignore-marking test."""
    def __init__(self, rows):
        self._rows = rows
        self._filters = []

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeTitle:
    def __init__(self, index, type_=None, obfuscation_flag=False, obfuscation_reason=None,
                 auto_type=None, user_type=None):
        self.index = index
        self.type = type_
        # crud.set_title_type expects the source-split columns. Treat the
        # legacy `type` arg as the user-set value when not otherwise
        # supplied — these tests pre-date the auto/user split and assume
        # `type` was user-initiated.
        self.auto_type = auto_type
        self.user_type = user_type if user_type is not None else (type_ if type_ else None)
        self.obfuscation_flag = obfuscation_flag
        self.obfuscation_reason = obfuscation_reason


def _fake_db_with_titles(titles):
    db = SimpleNamespace()
    db.commit = MagicMock()
    db.refresh = MagicMock()
    db.query = MagicMock(return_value=_FakeQuery(titles))
    return db


class TestMarkPathASkippedSiblingsAsIgnore:
    def test_skipped_only_when_index_in_group_and_not_in_rip_set(self):
        # Reorder: prove the filter math by passing rows that match the
        # skipped set exactly. The helper's SELECT in real code already
        # narrows to `index IN (skipped)`, so the loop only sees skipped rows.
        rows = [_FakeTitle(1), _FakeTitle(3)]
        db = _fake_db_with_titles(rows)
        job = SimpleNamespace(disc_id="disc-1", rip_set=[2, 4])
        sr = {"group_member_indexes": [1, 2, 3]}

        n = _mark_path_a_skipped_siblings_as_ignore(job, sr, db)

        assert n == 2
        assert all(r.type == "ignore" for r in rows)

    def test_idempotent_skips_titles_already_ignored(self):
        rows = [_FakeTitle(1, type_="ignore"), _FakeTitle(3)]
        db = _fake_db_with_titles(rows)
        job = SimpleNamespace(disc_id="disc-1", rip_set=[2])
        sr = {"group_member_indexes": [1, 2, 3]}

        n = _mark_path_a_skipped_siblings_as_ignore(job, sr, db)

        # Only the second row gets mutated — the first is already 'ignore'.
        assert n == 1
        assert rows[0].type == "ignore"
        assert rows[1].type == "ignore"

    def test_respects_user_applied_type(self):
        # If the user has already labeled a sibling (e.g. as 'extra'), the
        # helper should leave it alone rather than clobber the label.
        rows = [_FakeTitle(1, type_="extra"), _FakeTitle(3)]
        db = _fake_db_with_titles(rows)
        job = SimpleNamespace(disc_id="disc-1", rip_set=[2])
        sr = {"group_member_indexes": [1, 2, 3]}

        n = _mark_path_a_skipped_siblings_as_ignore(job, sr, db)

        assert n == 1  # Only row[1] (index 3) gets ignored
        assert rows[0].type == "extra"  # User-applied type preserved
        assert rows[1].type == "ignore"

    def test_no_op_when_no_disc_id(self):
        rows = [_FakeTitle(1)]
        db = _fake_db_with_titles(rows)
        job = SimpleNamespace(disc_id=None, rip_set=[2])
        sr = {"group_member_indexes": [1, 2, 3]}

        assert _mark_path_a_skipped_siblings_as_ignore(job, sr, db) == 0
        assert rows[0].type is None

    def test_no_op_when_skipped_set_is_empty(self):
        # Every group member was ripped — nothing to ignore.
        rows = [_FakeTitle(1), _FakeTitle(2), _FakeTitle(3)]
        db = _fake_db_with_titles(rows)
        job = SimpleNamespace(disc_id="disc-1", rip_set=[1, 2, 3])
        sr = {"group_member_indexes": [1, 2, 3]}

        assert _mark_path_a_skipped_siblings_as_ignore(job, sr, db) == 0

    def test_canonical_complete_advance_calls_ignore_helper(self):
        """Integration: canonical_complete advance also runs the ignore mark."""
        rows = [_FakeTitle(1), _FakeTitle(3)]
        db = _fake_db_with_titles(rows)
        job = SimpleNamespace(
            id="job-001",
            segment_reorder_state={
                "stage": "canonical_ripping_pending",
                "group_member_indexes": [1, 2, 3],
            },
            workflow_step="exploratory_rip",
            disc_id="disc-1",
            rip_set=[2],
        )

        advanced = _maybe_advance_canonical_complete(job, job.id, db, branch="miss")

        assert advanced is True
        assert job.segment_reorder_state["stage"] == "canonical_complete"
        assert job.workflow_step == "boxset"
        assert all(r.type == "ignore" for r in rows)

    def test_skipped_siblings_get_path_a_decoy_reason(self):
        """Phase 3: the same helper that auto-marks skipped siblings as
        type='ignore' also stamps them with the HIGH-tier obfuscation
        reason so the UI badge reflects Path A's certainty."""
        rows = [_FakeTitle(1), _FakeTitle(3)]
        db = _fake_db_with_titles(rows)
        job = SimpleNamespace(disc_id="disc-1", rip_set=[2])
        sr = {"group_member_indexes": [1, 2, 3]}

        n = _mark_path_a_skipped_siblings_as_ignore(job, sr, db)

        assert n == 2
        assert all(r.obfuscation_reason == "path_a_decoy" for r in rows)
        assert all(r.obfuscation_flag is True for r in rows)

    def test_skipped_helper_upgrades_already_ignored_rows_to_path_a_reason(self):
        """Defensive: a row that was already type='ignore' from a prior
        run still gets the HIGH-tier reason upgrade — Path A is more
        specific than MakeMKV-only or NULL."""
        rows = [_FakeTitle(1, type_="ignore", obfuscation_reason="makemkv_msg3307"),
                _FakeTitle(3, type_="ignore", obfuscation_reason=None)]
        db = _fake_db_with_titles(rows)
        job = SimpleNamespace(disc_id="disc-1", rip_set=[2])
        sr = {"group_member_indexes": [1, 2, 3]}

        n = _mark_path_a_skipped_siblings_as_ignore(job, sr, db)

        # Returns 0 — no NEW ignores — but the reason on both rows is upgraded.
        assert n == 0
        assert all(r.obfuscation_reason == "path_a_decoy" for r in rows)
        assert all(r.obfuscation_flag is True for r in rows)


class TestClearPathACanonicalObfuscationFlag:
    def test_clears_flag_and_reason_on_matched_canonical_row(self):
        row = _FakeTitle(109, obfuscation_flag=True, obfuscation_reason="makemkv_msg3307")
        db = _fake_db_with_titles([row])
        job = SimpleNamespace(disc_id="disc-1")
        sr = {"matched_playlist_index": 109}

        changed = _clear_path_a_canonical_obfuscation_flag(job, sr, db)

        assert changed is True
        assert row.obfuscation_flag is False
        assert row.obfuscation_reason is None

    def test_clears_reason_even_when_flag_already_false(self):
        """The reason may be set by Path B dedupe to 'segment_set_sibling'
        on a row that didn't have the legacy boolean. Path A's
        canonical confirmation overrides both signals."""
        row = _FakeTitle(109, obfuscation_flag=False, obfuscation_reason="segment_set_sibling")
        db = _fake_db_with_titles([row])
        job = SimpleNamespace(disc_id="disc-1")
        sr = {"matched_playlist_index": 109}

        changed = _clear_path_a_canonical_obfuscation_flag(job, sr, db)

        assert changed is True
        assert row.obfuscation_reason is None

    def test_idempotent_when_flag_and_reason_already_clear(self):
        row = _FakeTitle(109, obfuscation_flag=False, obfuscation_reason=None)
        db = _fake_db_with_titles([row])
        job = SimpleNamespace(disc_id="disc-1")
        sr = {"matched_playlist_index": 109}

        changed = _clear_path_a_canonical_obfuscation_flag(job, sr, db)

        assert changed is False
        assert row.obfuscation_flag is False
        assert row.obfuscation_reason is None

    def test_no_op_when_matched_index_absent(self):
        row = _FakeTitle(109, obfuscation_flag=True)
        db = _fake_db_with_titles([row])
        job = SimpleNamespace(disc_id="disc-1")
        sr = {}

        assert _clear_path_a_canonical_obfuscation_flag(job, sr, db) is False
        assert row.obfuscation_flag is True

    def test_no_op_when_disc_id_missing(self):
        row = _FakeTitle(109, obfuscation_flag=True)
        db = _fake_db_with_titles([row])
        job = SimpleNamespace(disc_id=None)
        sr = {"matched_playlist_index": 109}

        assert _clear_path_a_canonical_obfuscation_flag(job, sr, db) is False
        assert row.obfuscation_flag is True

    def test_no_op_when_row_not_found(self):
        # disc has no matching row at the given index
        db = _fake_db_with_titles([])
        job = SimpleNamespace(disc_id="disc-1")
        sr = {"matched_playlist_index": 109}

        assert _clear_path_a_canonical_obfuscation_flag(job, sr, db) is False

    def test_canonical_complete_advance_clears_canonical_flag(self):
        """Integration: canonical_complete advance runs the obfuscation clear
        for the matched playlist index. We construct the row at index 109 so
        the _FakeQuery.first() returns it (the fake doesn't actually filter,
        so we keep only the row we care about for this test)."""
        canonical_row = _FakeTitle(109, obfuscation_flag=True)
        db = _fake_db_with_titles([canonical_row])
        job = SimpleNamespace(
            id="job-001",
            segment_reorder_state={
                "stage": "canonical_ripping_pending",
                "group_member_indexes": [109],
                "matched_playlist_index": 109,
            },
            workflow_step="exploratory_rip",
            disc_id="disc-1",
            rip_set=[109],
        )

        advanced = _maybe_advance_canonical_complete(job, job.id, db, branch="miss")

        assert advanced is True
        assert canonical_row.obfuscation_flag is False
