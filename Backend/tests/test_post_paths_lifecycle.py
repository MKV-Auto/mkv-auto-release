"""
Lifecycle integration: ``job.post_paths`` ↔ ``DiscTitle.file_path``.

At the end of post-process the worker emits per-title path information
through two writes that must stay consistent:

1. ``_update_title_file_paths(db, disc_id, post_paths, "postprocess",
   base_dir=trans_root)`` — writes **absolute** paths under the job's
   ``transient/`` root onto each ``DiscTitle.file_path``.
2. ``StageState.postprocess_complete(db, job, post_paths=...)`` —
   persists the **relative** dict onto ``job.post_paths`` (and advances
   the job to phase=transfer, transfer_state=ready).

Both writes happen back-to-back inside ``_post_postprocess_complete_callback``
(``workers/tasks.py`` — invoked from ``_run_prep_phase`` after the rename +
hash + validate steps succeed). Downstream consumers depend on the
two staying consistent: transfer reads ``job.post_paths`` to know the
relative layout under transient/; the history page reads
``DiscTitle.file_path`` to know where each file lives absolutely.

#365 Phase 2 § 6.6 — these tests originally exercised the
``POST /jobs/{id}/postprocess-complete`` HTTP endpoint, which was
removed in #427 once the worker started calling ``StageState`` directly
via the in-process ``_post_postprocess_complete_callback`` helper.
Tests now drive the same contract at the ``StageState`` boundary,
matching production.
"""
import os
import uuid

import pytest

from api import models
from core import mkv_identity
from core.job_state import StageState
from workers.tasks import _update_title_file_paths


def _seed(session, *, n_titles=2):
    """Disc + N titles + a job in phase=postprocess after rip completion.
    Returns (job_id, disc_id, [title_ids])."""
    disc_id = str(uuid.uuid4())
    session.add(models.Disc(id=disc_id, content_hash=f"h-{uuid.uuid4().hex[:8]}"))
    title_ids = []
    for i in range(n_titles):
        tid = str(uuid.uuid4())
        session.add(models.DiscTitle(
            id=tid, disc_id=disc_id,
            title=f"T{i}",
            source_file=f"0000{i}.mpls",
        ))
        title_ids.append(tid)
    job_id = str(uuid.uuid4())
    session.add(models.Job(
        id=job_id, disc_id=disc_id, disc_num="1", mount_point="/mnt/sr0",
        rip_state="completed",
        phase="postprocess",
        transfer_phase="preparing",
    ))
    session.commit()
    return job_id, disc_id, title_ids


def test_post_paths_and_disc_title_file_path_align_after_postprocess(test_db):
    """The end-of-postprocess rendezvous — same input dict, two writes,
    consistent state. Each ``DiscTitle.file_path`` must equal
    ``trans_root + job.post_paths[title.id]``."""
    session = test_db()
    try:
        job_id, disc_id, [tid_a, tid_b] = _seed(session)
    finally:
        session.close()

    trans_root = "/jobs/J/transient"
    post_paths = {
        tid_a: "Movies/Film A (2024)/Film A.1080p.mkv",
        tid_b: "Movies/Film B (2024)/Film B.1080p.mkv",
    }

    # Step 1: DiscTitle.file_path is populated with absolute paths.
    session = test_db()
    try:
        _update_title_file_paths(session, disc_id, post_paths, "postprocess",
                                 base_dir=trans_root)
        session.commit()
    finally:
        session.close()

    # Step 2: StageState.postprocess_complete persists the relative dict
    # onto the job and advances to phase=transfer / transfer_state=ready.
    # In production this runs back-to-back with step 1 inside
    # ``_post_postprocess_complete_callback`` (workers/tasks.py).
    session = test_db()
    try:
        job = session.query(models.Job).filter_by(id=job_id).first()
        StageState.postprocess_complete(
            session, job,
            post_paths=post_paths,
            reason="test: postprocess complete",
        )
    finally:
        session.close()

    # Step 3: both halves of the rendezvous are consistent.
    session = test_db()
    try:
        job = session.query(models.Job).filter_by(id=job_id).first()
        # #365 step 5 — post_state column dropped; derived via hybrid_property.
        assert job.derived_post_state == "completed"
        assert job.post_paths == post_paths
        assert job.phase == "transfer"
        assert job.transfer_state == "ready"

        for tid in (tid_a, tid_b):
            title = session.query(models.DiscTitle).filter_by(id=tid).first()
            assert title.file_path_stage == "postprocess"
            expected = os.path.join(trans_root, post_paths[tid])
            assert title.file_path == expected, (
                f"DiscTitle.file_path for {tid} must equal trans_root + "
                f"job.post_paths[{tid}]; got {title.file_path!r} vs {expected!r}"
            )
    finally:
        session.close()


def test_post_paths_survives_round_trip_through_state_machine_unchanged(test_db):
    """``StageState.postprocess_complete`` persists ``post_paths`` exactly as
    passed (no key rewriting, no path normalization). Transfer's source
    lookup depends on the exact relative paths — UTF-8, spaces,
    parenthesised years all survive verbatim."""
    session = test_db()
    try:
        job_id, _disc_id, [tid] = _seed(session, n_titles=1)
    finally:
        session.close()

    # Path with a UTF-8 char, a space, and a parenthesized year — three
    # things that path-normalization libraries like to mangle.
    rel = "Movies/Amélie Poulain (2001)/Amélie Poulain.1080p.mkv"

    session = test_db()
    try:
        job = session.query(models.Job).filter_by(id=job_id).first()
        StageState.postprocess_complete(
            session, job,
            post_paths={tid: rel},
            reason="test: utf-8 round-trip",
        )
    finally:
        session.close()

    session = test_db()
    try:
        job = session.query(models.Job).filter_by(id=job_id).first()
        assert job.post_paths == {tid: rel}
    finally:
        session.close()


def test_postprocess_block_writes_both_file_path_and_segment_uid(
    test_db, monkeypatch
):
    """#448 — the postprocess success block updates ``file_path`` AND
    ``segment_uid`` using the same ``post_paths`` dict. A row that gets a
    file_path must also be a candidate for a segment_uid write; both halves
    of the rendezvous land in the same DB session.

    ``read_segment_uid`` is monkeypatched: this test exercises the wiring
    contract (the two writes are issued back-to-back against the same
    keys), not the binary integration. The latter is covered by the manual
    smoke-test in the PR doc."""
    session = test_db()
    try:
        job_id, disc_id, [tid_a, tid_b] = _seed(session)
    finally:
        session.close()

    trans_root = "/jobs/J/transient"
    post_paths = {
        tid_a: "Movies/Film A (2024)/Film A.1080p.mkv",
        tid_b: "Movies/Film B (2024)/Film B.1080p.mkv",
    }
    uids = {
        tid_a: "11111111111111111111111111111111",
        tid_b: "22222222222222222222222222222222",
    }
    monkeypatch.setattr(
        mkv_identity,
        "read_segment_uid",
        lambda abs_path: next(
            (uids[tid] for tid, rel in post_paths.items() if abs_path.endswith(rel)),
            None,
        ),
    )

    session = test_db()
    try:
        _update_title_file_paths(
            session, disc_id, post_paths, "postprocess", base_dir=trans_root
        )
        mkv_identity.capture_segment_uids_for_titles(
            session, disc_id, post_paths, trans_root
        )
        session.commit()
    finally:
        session.close()

    session = test_db()
    try:
        for tid in (tid_a, tid_b):
            row = session.query(models.DiscTitle).filter_by(id=tid).first()
            assert row.file_path_stage == "postprocess"
            assert row.file_path == os.path.join(trans_root, post_paths[tid])
            assert row.segment_uid == uids[tid]
            assert len(row.segment_uid) == 32  # 128-bit hex
    finally:
        session.close()


def test_postprocess_complete_requires_post_paths_argument(test_db):
    """``StageState.postprocess_complete`` is the gatekeeper that stops the
    worker from ever advancing a job past postprocess without telling the
    API where the files ended up. Calling without ``post_paths`` is a
    keyword-only TypeError — Python's signature is the contract enforcer
    now that the HTTP endpoint (and its Pydantic validator) is gone."""
    session = test_db()
    try:
        job_id, _, _ = _seed(session, n_titles=1)
    finally:
        session.close()

    session = test_db()
    try:
        job = session.query(models.Job).filter_by(id=job_id).first()
        with pytest.raises(TypeError, match="post_paths"):
            StageState.postprocess_complete(  # type: ignore[call-arg]
                session, job, reason="test: missing post_paths"
            )
    finally:
        session.close()
