"""Tests for the Path B iteration-loop endpoints (PR 2 of the segment-
reorder iterative-elimination feature).

Covers:
  - POST /jobs/{id}/segment-order/confirm — confirmation-gate endpoint:
    marks confirmed_segment_order, returns matcher results with
    subsequence_supersets filtered by the disc's per-clip flags.
  - POST /jobs/{id}/segment-order/flag-decoys — marks the exploratory
    mpls AND every sibling sharing its sorted-segment-set as
    type='ignore'; bumps state.eliminated_title_indexes; records
    iteration_history with outcome='flagged_decoys'.
  - submit_segment_order extension — disc_flags drive matcher;
    subsequence_supersets surfaced in the no-match response.
"""
import uuid
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from api import models
from api.main import app

pytestmark = pytest.mark.integration


@pytest.fixture
def client(e2e_test_environment):
    return TestClient(app)


def _midway_disc_and_job(session):
    """Create a Midway-shaped fixture: one disc + one job mid-iteration.

    Disc has:
      - Title index 0  — canonical mpls `1,2,3` (the "real" movie)
      - Title index 1  — decoy permutation `3,1,2` (same sorted set)
      - Title index 10 — superset of `1,2,3` with one extra `X` inserted
      - Title index 11 — superset of `1,2,3` with `Y` instead of `X`
    """
    disc_id = str(uuid.uuid4())
    disc = models.Disc(id=disc_id, content_hash=f"H-{uuid.uuid4().hex[:8]}")
    session.add(disc)
    session.flush()
    for idx, src, seg in [
        (0, "00001.mpls", "1,2,3"),
        (1, "00002.mpls", "3,1,2"),
        (10, "00010.mpls", "1,X,2,3"),
        (11, "00011.mpls", "1,Y,2,3"),
    ]:
        session.add(models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc_id,
            index=idx,
            source_file=src,
            segment_map=seg,
        ))
    job = models.Job(
        disc_id=disc_id,
        disc_num="1",
        mount_point="/mnt/test",
        mode="rip",
        job_status="running",
        rip_state="completed",
        workflow_step="exploratory_rip",
        stage_profile="miss",
        segment_reorder_state={
            "stage": "awaiting_segment_order",
            "exploratory_title_index": 0,
            "group_member_indexes": [0, 1],
        },
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return str(disc.id), str(job.id)


class TestConfirmSegmentOrder:

    def test_confirm_returns_supersets_for_user_validated_order(
        self, client, test_db
    ):
        with test_db() as session:
            disc_id, job_id = _midway_disc_and_job(session)
        resp = client.post(
            f"/jobs/{job_id}/segment-order/confirm",
            json={"order": ["1", "2", "3"]},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["confirmed"] is True
        # Two superset mpls (indexes 10 + 11), both 4-segment with 1 extra.
        idxs = sorted(c["title_index"] for c in data["subsequence_supersets"])
        assert idxs == [10, 11]

    def test_confirm_persists_confirmed_order_and_history(self, client, test_db):
        with test_db() as session:
            disc_id, job_id = _midway_disc_and_job(session)
        client.post(
            f"/jobs/{job_id}/segment-order/confirm",
            json={"order": ["1", "2", "3"]},
        )
        with test_db() as session:
            job = session.query(models.Job).filter_by(id=job_id).first()
            state = job.segment_reorder_state
            assert state["confirmed_segment_order"] == ["1", "2", "3"]
            history = state["iteration_history"]
            assert len(history) == 1
            assert history[0]["outcome"] == "no_match"
            assert history[0]["submitted_order"] == ["1", "2", "3"]
            assert history[0]["exploratory_title_idx"] == 0

    def test_confirm_filters_supersets_by_disc_definitely_flag(
        self, client, test_db
    ):
        with test_db() as session:
            disc_id, job_id = _midway_disc_and_job(session)
        # Flag X as definitely obfuscation — title 10 contains X, should
        # be excluded.
        client.patch(
            f"/discs/{disc_id}/segment-flags",
            json={"clip_id": "X", "flag": "definitely"},
        )
        resp = client.post(
            f"/jobs/{job_id}/segment-order/confirm",
            json={"order": ["1", "2", "3"]},
        )
        idxs = [c["title_index"] for c in resp.json()["subsequence_supersets"]]
        assert 10 not in idxs
        assert idxs == [11]

    def test_confirm_potentially_flag_ranks_omitter_first(
        self, client, test_db
    ):
        with test_db() as session:
            disc_id, job_id = _midway_disc_and_job(session)
        client.patch(
            f"/discs/{disc_id}/segment-flags",
            json={"clip_id": "X", "flag": "potentially"},
        )
        resp = client.post(
            f"/jobs/{job_id}/segment-order/confirm",
            json={"order": ["1", "2", "3"]},
        )
        idxs = [c["title_index"] for c in resp.json()["subsequence_supersets"]]
        # Title 11 omits X, sorts first; title 10 contains X, sorts after.
        assert idxs == [11, 10]

    def test_confirm_unknown_job_returns_404(self, client):
        resp = client.post(
            f"/jobs/{uuid.uuid4()}/segment-order/confirm",
            json={"order": ["1", "2"]},
        )
        assert resp.status_code == 404


class TestFlagDecoys:

    def test_flag_decoys_marks_exploratory_and_siblings_as_ignore(
        self, client, test_db
    ):
        with test_db() as session:
            disc_id, job_id = _midway_disc_and_job(session)
        resp = client.post(
            f"/jobs/{job_id}/segment-order/flag-decoys",
            json={"exploratory_title_index": 0},
        )
        assert resp.status_code == 200, resp.text
        # Indexes 0 + 1 share sorted_set {1,2,3} → both eliminated.
        assert sorted(resp.json()["eliminated_title_indexes"]) == [0, 1]
        assert resp.json()["newly_eliminated_count"] == 2
        with test_db() as session:
            titles = session.query(models.DiscTitle).filter_by(disc_id=disc_id).all()
            by_idx = {t.index: t.type for t in titles}
            assert by_idx[0] == "ignore"
            assert by_idx[1] == "ignore"
            assert by_idx[10] != "ignore"  # superset preserved
            assert by_idx[11] != "ignore"

    def test_flag_decoys_skips_user_set_types(self, client, test_db):
        """A title already labeled by the user (any non-empty type) is left
        alone — flag-decoys must respect prior user intent (mirrors the
        path A skipped-siblings idempotency pattern)."""
        with test_db() as session:
            disc_id, job_id = _midway_disc_and_job(session)
            session.query(models.DiscTitle).filter_by(
                disc_id=disc_id, index=1
            ).update({"type": "movie"})
            session.commit()
        resp = client.post(
            f"/jobs/{job_id}/segment-order/flag-decoys",
            json={"exploratory_title_index": 0},
        )
        # Title 1 was user-typed → not in newly_eliminated; only 0 marked.
        assert resp.json()["eliminated_title_indexes"] == [0]
        with test_db() as session:
            t1 = session.query(models.DiscTitle).filter_by(
                disc_id=disc_id, index=1
            ).first()
            assert t1.type == "movie"  # preserved

    def test_flag_decoys_appends_iteration_history(self, client, test_db):
        with test_db() as session:
            disc_id, job_id = _midway_disc_and_job(session)
        client.post(
            f"/jobs/{job_id}/segment-order/flag-decoys",
            json={"exploratory_title_index": 0},
        )
        with test_db() as session:
            job = session.query(models.Job).filter_by(id=job_id).first()
            state = job.segment_reorder_state
            assert state["stage"] == "awaiting_segment_order"
            assert state["submitted_order"] is None
            history = state["iteration_history"]
            assert history[-1]["outcome"] == "flagged_decoys"
            assert history[-1]["exploratory_title_idx"] == 0

    def test_flag_decoys_unknown_title_returns_404(self, client, test_db):
        with test_db() as session:
            disc_id, job_id = _midway_disc_and_job(session)
        resp = client.post(
            f"/jobs/{job_id}/segment-order/flag-decoys",
            json={"exploratory_title_index": 999},
        )
        assert resp.status_code == 404

    def test_flag_decoys_singleton_segment_map_marks_only_that_title(
        self, client, test_db
    ):
        """Titles with a non-multi-segment segment_map (e.g. single m2ts
        clip) have no sibling set — only the exploratory itself gets
        marked."""
        with test_db() as session:
            disc_id, job_id = _midway_disc_and_job(session)
            session.add(models.DiscTitle(
                id=str(uuid.uuid4()),
                disc_id=disc_id,
                index=100,
                source_file="02799.m2ts",
                segment_map="2799",
            ))
            session.commit()
        resp = client.post(
            f"/jobs/{job_id}/segment-order/flag-decoys",
            json={"exploratory_title_index": 100},
        )
        # Singleton clip — only itself marked, not other singletons.
        assert resp.json()["eliminated_title_indexes"] == [100]


class TestRipSupersetCandidate:
    """POST /jobs/{id}/segment-order/rip-superset — picker action wiring."""

    def test_rip_superset_dispatches_and_updates_state(self, client, test_db):
        with test_db() as session:
            disc_id, job_id = _midway_disc_and_job(session)
        with _patched_rip_dispatch() as fake_apply:
            resp = client.post(
                f"/jobs/{job_id}/segment-order/rip-superset",
                json={"title_index": 10},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["dispatched"] is True
        assert body["exploratory_title_index"] == 10
        assert body["rip_set_size"] == 1
        # rip_disc.apply_async fired exactly once.
        assert fake_apply.call_count == 1
        with test_db() as session:
            job = session.query(models.Job).filter_by(id=job_id).first()
            state = job.segment_reorder_state
            assert state["stage"] == "exploratory_ripping"
            assert state["exploratory_title_index"] == 10
            assert state["previews_manifest"] == []
            assert state["submitted_order"] is None
            assert job.rip_set == [10]
            assert job.workflow_step == "exploratory_rip"
            assert state["iteration_history"][-1]["outcome"] == "rip_superset"
            assert state["iteration_history"][-1]["picked_title_index"] == 10

    def test_rip_superset_unknown_title_returns_404(self, client, test_db):
        with test_db() as session:
            disc_id, job_id = _midway_disc_and_job(session)
        with _patched_rip_dispatch():
            resp = client.post(
                f"/jobs/{job_id}/segment-order/rip-superset",
                json={"title_index": 999},
            )
        assert resp.status_code == 404

    def test_rip_superset_groups_siblings_by_sorted_set(self, client, test_db):
        with test_db() as session:
            disc_id, job_id = _midway_disc_and_job(session)
            # Add an extra mpls with the same sorted set as title 10
            # ({1,2,3,X}) — should become a sibling.
            session.add(models.DiscTitle(
                id=str(uuid.uuid4()),
                disc_id=disc_id,
                index=50,
                source_file="00050.mpls",
                segment_map="X,3,1,2",
            ))
            session.commit()
        with _patched_rip_dispatch():
            resp = client.post(
                f"/jobs/{job_id}/segment-order/rip-superset",
                json={"title_index": 10},
            )
        with test_db() as session:
            job = session.query(models.Job).filter_by(id=job_id).first()
            assert sorted(job.segment_reorder_state["group_member_indexes"]) == [10, 50]


class TestRipTheRest:
    """POST /jobs/{id}/rip-the-rest — final escape hatch."""

    def test_rip_the_rest_dispatches_when_under_threshold(self, client, test_db):
        with test_db() as session:
            disc_id, job_id = _midway_disc_and_job(session)
            # Give the existing titles small sizes so total < 200 GB cap.
            for t in session.query(models.DiscTitle).filter_by(disc_id=disc_id).all():
                t.size = 100 * 1024 * 1024  # 100 MB each
            session.commit()
        with _patched_rip_dispatch() as fake_apply:
            resp = client.post(f"/jobs/{job_id}/rip-the-rest")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["dispatched"] is True
        assert body["rip_set_size"] == 4
        assert fake_apply.call_count == 1
        with test_db() as session:
            job = session.query(models.Job).filter_by(id=job_id).first()
            assert sorted(job.rip_set) == [0, 1, 10, 11]
            assert job.workflow_step == "titles"
            assert job.segment_reorder_state["stage"] == "rip_the_rest"

    def test_rip_the_rest_skips_ignored_titles(self, client, test_db):
        with test_db() as session:
            disc_id, job_id = _midway_disc_and_job(session)
            for t in session.query(models.DiscTitle).filter_by(disc_id=disc_id).all():
                t.size = 50 * 1024 * 1024
                if t.index in (0, 1):  # Mark the two decoys as ignored.
                    t.type = "ignore"
            session.commit()
        with _patched_rip_dispatch():
            resp = client.post(f"/jobs/{job_id}/rip-the-rest")
        with test_db() as session:
            job = session.query(models.Job).filter_by(id=job_id).first()
            assert sorted(job.rip_set) == [10, 11]

    def test_rip_the_rest_409_when_over_threshold(self, client, test_db, monkeypatch):
        with test_db() as session:
            disc_id, job_id = _midway_disc_and_job(session)
            for t in session.query(models.DiscTitle).filter_by(disc_id=disc_id).all():
                t.size = 200 * 1024 * 1024 * 1024  # 200 GB each — way over.
            session.commit()
        # Pin the threshold low to make assertion deterministic regardless
        # of host disk size.
        from core import segment_reorder
        monkeypatch.setattr(segment_reorder, "RIP_THE_REST_HARD_CAP_BYTES", 100 * 1024 * 1024)
        with _patched_rip_dispatch() as fake_apply:
            resp = client.post(f"/jobs/{job_id}/rip-the-rest")
        assert resp.status_code == 409
        # No rip dispatched.
        assert fake_apply.call_count == 0

    def test_rip_the_rest_400_when_no_titles_left(self, client, test_db):
        with test_db() as session:
            disc_id, job_id = _midway_disc_and_job(session)
            for t in session.query(models.DiscTitle).filter_by(disc_id=disc_id).all():
                t.type = "ignore"
            session.commit()
        with _patched_rip_dispatch():
            resp = client.post(f"/jobs/{job_id}/rip-the-rest")
        assert resp.status_code == 400
        assert "No rippable titles" in resp.json()["detail"]


@contextmanager
def _patched_rip_dispatch():
    """Mock out rip_disc.apply_async so the test doesn't try to enqueue
    a Celery task against the real broker."""
    from unittest.mock import MagicMock, patch as _patch
    fake_result = MagicMock(id="fake-task-id")
    with _patch("workers.tasks.rip_disc.apply_async", return_value=fake_result) as m:
        yield m
