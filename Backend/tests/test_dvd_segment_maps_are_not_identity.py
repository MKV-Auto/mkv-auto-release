"""#831 — on a DVD the MakeMKV segment map is the PGC-relative cell list,
not a content identity. Every layer that groups titles by segment map must
stand down on DVD, and rows demoted before that was understood must heal.

The fixture is the prod shape that surfaced the bug: Star Wars Rebels
Season Two disc 2 (DVD) — six distinct ~22-minute episodes that all report
``1-5,6``, six distinct extras that all report ``1,2``. The Blu-ray twin
of the same fixture keeps today's behaviour exactly.
"""
from __future__ import annotations

import uuid

import pytest

from api import models
from core.duplicate_group_sync import (
    release_segment_map_demotions,
    sync_duplicate_group_labels_for_disc,
)
from core.duplicate_info import attach_duplicate_info
from core.path_a_trigger import evaluate_path_a_trigger
from core.path_b_dedupe import apply_path_b_marks_for_disc, compute_dedupe_groups
from core.segment_identity import segment_maps_identify_content


EPISODE_DURATIONS = [1323, 1322, 1324, 1323, 1323, 1322]
EXTRA_DURATIONS = [339, 353, 341, 316, 453, 353]


def _rebels_disc2(session, *, fmt: str):
    disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"rebels-s2d2-{fmt}-{uuid.uuid4().hex[:6]}", format=fmt)
    session.add(disc)
    session.flush()
    rows = []
    idx = 0
    for dur in EPISODE_DURATIONS:
        rows.append(models.DiscTitle(
            id=str(uuid.uuid4()), disc_id=disc.id, index=idx, order_index=idx,
            source_file=f"title-{idx}", segment_map="1-5,6", duration=dur,
        ))
        idx += 1
    rows.append(models.DiscTitle(
        id=str(uuid.uuid4()), disc_id=disc.id, index=idx, order_index=idx,
        source_file=f"title-{idx}", segment_map="1,2,3,4,5,6,7", duration=2155,
    ))
    idx += 1
    for dur in EXTRA_DURATIONS:
        rows.append(models.DiscTitle(
            id=str(uuid.uuid4()), disc_id=disc.id, index=idx, order_index=idx,
            source_file=f"title-{idx}", segment_map="1,2", duration=dur,
        ))
        idx += 1
    session.add_all(rows)
    session.commit()
    return disc, rows


def _payloads(rows):
    return {
        str(r.id): {
            "title_id": str(r.id), "segment_map": r.segment_map, "duration": r.duration,
            "index": r.index, "source_file": r.source_file, "type": r.type,
            "active": r.active, "size": None,
        }
        for r in rows
    }


def test_predicate_blu_ray_and_uhd_identify_dvd_does_not():
    assert segment_maps_identify_content("Blu-Ray") is True
    assert segment_maps_identify_content("UHD") is True
    assert segment_maps_identify_content("DVD") is False
    assert segment_maps_identify_content("dvd") is False
    # Unknown format keeps the legacy behaviour rather than silently
    # losing duplicate detection on a disc whose format was never recorded.
    assert segment_maps_identify_content(None) is True
    assert segment_maps_identify_content("") is True


def test_dvd_sync_creates_no_groups_every_episode_stays_active(test_db):
    session = test_db()
    try:
        disc, rows = _rebels_disc2(session, fmt="DVD")
        sync_duplicate_group_labels_for_disc(session, disc.id)
        session.commit()
        for r in rows:
            session.refresh(r)
        assert all(r.active is not False for r in rows), [(r.index, r.active) for r in rows]
        assert all((r.type or "") != "ignore" for r in rows)
    finally:
        session.close()


def test_blu_ray_sync_still_collapses_same_segment_map(test_db):
    """Pin the Blu-ray contract: identical maps → one primary, rest demoted."""
    session = test_db()
    try:
        disc, rows = _rebels_disc2(session, fmt="Blu-Ray")
        sync_duplicate_group_labels_for_disc(session, disc.id)
        session.commit()
        for r in rows:
            session.refresh(r)
        episodes = [r for r in rows if r.segment_map == "1-5,6"]
        assert sum(1 for r in episodes if r.active is True) == 1
        assert sum(1 for r in episodes if r.active is False) == len(episodes) - 1
    finally:
        session.close()


def test_dvd_path_b_computes_no_groups_and_blu_ray_does():
    rows_payload = {
        f"t{i}": {"title_id": f"t{i}", "segment_map": "1-5,6", "duration": d, "index": i,
                  "source_file": f"title-{i}"}
        for i, d in enumerate(EPISODE_DURATIONS)
    }
    assert compute_dedupe_groups(rows_payload, disc_format="DVD") == []
    bd = compute_dedupe_groups(rows_payload, disc_format="Blu-Ray")
    assert len(bd) == 1 and len(bd[0].sibling_title_ids) == len(EPISODE_DURATIONS) - 1
    # Default (no format) is the legacy path.
    assert len(compute_dedupe_groups(rows_payload)) == 1


def test_dvd_attach_duplicate_info_attaches_nothing():
    payloads = {
        f"t{i}": {"title_id": f"t{i}", "segment_map": "1-5,6", "duration": d, "index": i}
        for i, d in enumerate(EPISODE_DURATIONS)
    }
    attach_duplicate_info(payloads, "disc-x", disc_format="DVD")
    assert not any("duplicate_info" in p for p in payloads.values())
    attach_duplicate_info(payloads, "disc-x", disc_format="Blu-Ray")
    assert all(p.get("duplicate_info", {}).get("group_size") == len(EPISODE_DURATIONS) for p in payloads.values())


def test_dvd_path_a_trigger_never_fires():
    titles = {i: {"segment_map": "1-5,6", "duration": d, "size": 4 * 1024 ** 3} for i, d in enumerate(EPISODE_DURATIONS)}
    decision = evaluate_path_a_trigger(titles, 8 * 1024 ** 3, threshold_gb=1, disc_format="DVD")
    assert decision.needs_user_choice is False
    assert decision.reason == "segment_maps_not_identity_on_format"


def test_dvd_heal_releases_rows_demoted_by_the_old_behaviour(test_db):
    """Prod state: rows demoted (active=False, auto ignore, labels cleared)
    plus Path B's stale segment_set_sibling marks. One sync pass on the DVD
    releases them; a user's own ignore and a detector-backed ignore survive."""
    session = test_db()
    try:
        disc, rows = _rebels_disc2(session, fmt="DVD")
        from api.crud import set_title_type
        episodes = [r for r in rows if r.segment_map == "1-5,6"]
        # Old behaviour: title 1 primary, 2–6 demoted; Path B marked 1,3–6 siblings.
        for r in episodes[1:]:
            r.active = False
            set_title_type(r, "ignore", source="auto")
        for r in (episodes[0], *episodes[2:]):
            r.obfuscation_reason = "segment_set_sibling"
            r.obfuscation_flag = True
        # A row the user ignored themselves stays ignored + hidden.
        user_ignored = episodes[5]
        set_title_type(user_ignored, "ignore", source="user")
        # A demoted row whose ignore a detector backs keeps its ignore.
        detector_backed = episodes[4]
        detector_backed.obfuscation_reason = "duration_short"
        session.commit()

        modified = sync_duplicate_group_labels_for_disc(session, disc.id)
        session.commit()
        assert modified >= 4
        for r in rows:
            session.refresh(r)

        for r in (episodes[1], episodes[2], episodes[3]):
            assert r.active is True, r.index
            assert r.auto_type is None and (r.type or "") == "", (r.index, r.type, r.auto_type)
            assert r.obfuscation_reason is None and r.obfuscation_flag is False
        assert episodes[0].obfuscation_reason is None and episodes[0].obfuscation_flag is False
        assert user_ignored.active is False and user_ignored.user_type == "ignore"
        assert detector_backed.active is True
        assert detector_backed.auto_type == "ignore" and detector_backed.obfuscation_reason == "duration_short"

        # Idempotent: a second pass changes nothing.
        assert sync_duplicate_group_labels_for_disc(session, disc.id) == 0
    finally:
        session.close()


def test_dvd_apply_path_b_marks_clears_stale_sibling_reason_only(test_db):
    session = test_db()
    try:
        disc, rows = _rebels_disc2(session, fmt="DVD")
        rows[1].obfuscation_reason = "segment_set_sibling"
        rows[1].obfuscation_flag = True
        rows[2].obfuscation_reason = "makemkv_msg3307"
        rows[2].obfuscation_flag = True
        session.commit()
        cleared, set_sibling, marked, set_sub = apply_path_b_marks_for_disc(session, disc.id)
        session.commit()
        assert (cleared, set_sibling, marked, set_sub) == (1, 0, 0, 0)
        session.refresh(rows[1]); session.refresh(rows[2])
        assert rows[1].obfuscation_reason is None and rows[1].obfuscation_flag is False
        assert rows[2].obfuscation_reason == "makemkv_msg3307" and rows[2].obfuscation_flag is True
    finally:
        session.close()


def test_release_helper_is_noop_on_untouched_rows():
    class Row:
        active = None
        user_type = None
        auto_type = None
        obfuscation_reason = None
        obfuscation_flag = False
        detection_warning = False
        title_seq = 0
    assert release_segment_map_demotions([Row(), Row()]) == 0
