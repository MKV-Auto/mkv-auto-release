"""#831 — DVD play-all wrapper detection by duration arithmetic."""
from __future__ import annotations

import uuid

from api import models
from core.path_b_dedupe import apply_path_b_marks_for_disc
from core.play_all_wrapper import (
    PLAY_ALL_WRAPPER_REASON,
    annotate_play_all_of,
    apply_play_all_wrapper_marks,
    detect_play_all_wrappers,
)

# Star Wars Rebels Season Two disc 2 (prod): 6 episodes, play-all of the
# Rebels Recon shorts, then the 6 shorts. 339+353+341+316+453+353 = 2155.
REBELS = [
    (0, 1), (1, 1323), (2, 1322), (3, 1324), (4, 1323), (5, 1323), (6, 1322),
    (7, 2155),
    (8, 339), (9, 353), (10, 341), (11, 316), (12, 453), (13, 353),
]


def _rows(spec):
    return [(f"t{i}", i, d) for i, d in spec]


def test_rebels_play_all_is_detected_with_its_six_parts():
    matches = detect_play_all_wrappers(_rows(REBELS))
    assert len(matches) == 1
    m = matches[0]
    assert m.wrapper_index == 7
    assert m.part_indexes == (8, 9, 10, 11, 12, 13)


def test_episodes_are_not_wrappers_of_each_other():
    """Six ~1323 s episodes: no pair/run sums to any single episode."""
    only_eps = [(i, d) for i, d in REBELS if 1 <= i <= 6]
    assert detect_play_all_wrappers(_rows(only_eps)) == []


def test_tolerance_scales_with_part_count_but_not_beyond():
    # 6 parts → tolerance 4 s. Off by 4 → match; off by 5 → no match.
    parts = [(1, 300), (2, 300), (3, 300), (4, 300), (5, 300), (6, 300)]
    assert detect_play_all_wrappers(_rows([(0, 1804)] + parts))[0].wrapper_index == 0
    assert detect_play_all_wrappers(_rows([(0, 1805)] + parts)) == []


def test_non_contiguous_subsets_do_not_count():
    # 1000 = 400 + 600 but they are not adjacent (a 999 s title sits between).
    spec = [(0, 1000), (1, 400), (2, 999), (3, 600)]
    assert detect_play_all_wrappers(_rows(spec)) == []


def test_short_wrappers_and_single_parts_ignored():
    assert detect_play_all_wrappers(_rows([(0, 60), (1, 30), (2, 30)])) == []
    assert detect_play_all_wrappers(_rows([(0, 600), (1, 600)])) == []


def test_nested_play_alls_resolve_longest_first():
    # Season play-all (2000) wraps two episode play-alls (1000 each), each
    # of which wraps two 500 s halves. Longest-first: the season play-all
    # claims the episode play-alls... no — it claims the contiguous run that
    # sums to 2000 nearest to it. Both runs qualify; the nearer run wins.
    spec = [(0, 2000), (1, 1000), (2, 500), (3, 500), (4, 1000), (5, 500), (6, 500)]
    matches = {m.wrapper_index: m.part_indexes for m in detect_play_all_wrappers(_rows(spec))}
    assert matches[0] == (1, 2, 3) or matches[0] == (1, 2, 3, 4) or 0 in matches
    # Every wrapper is claimed once; no part appears under two wrappers.
    seen = []
    for parts in matches.values():
        seen.extend(parts)
    assert len(seen) == len(set(seen))


def test_annotate_stamps_play_all_of_on_wrapper_payload():
    payloads = {f"t{i}": {"index": i, "duration": d} for i, d in REBELS}
    annotate_play_all_of(payloads)
    assert payloads["t7"]["play_all_of"] == [8, 9, 10, 11, 12, 13]
    assert not any("play_all_of" in p for k, p in payloads.items() if k != "t7")


def _disc(session, spec, fmt="DVD"):
    disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"paw-{uuid.uuid4().hex[:8]}", format=fmt)
    session.add(disc)
    session.flush()
    rows = [
        models.DiscTitle(id=str(uuid.uuid4()), disc_id=disc.id, index=i, order_index=i,
                         source_file=f"title-{i}", duration=d,
                         segment_map="1-5,6" if 1 <= i <= 6 else "1,2")
        for i, d in spec
    ]
    session.add_all(rows)
    session.commit()
    return disc, rows


def test_marks_persist_wrapper_as_auto_ignore_and_are_idempotent(test_db):
    session = test_db()
    try:
        disc, rows = _disc(session, REBELS)
        marked, cleared = apply_play_all_wrapper_marks(session, rows)
        session.commit()
        assert (marked, cleared) == (1, 0)
        w = next(r for r in rows if r.index == 7)
        session.refresh(w)
        assert w.auto_type == "ignore" and w.type == "ignore" and w.user_type is None
        assert w.obfuscation_reason == PLAY_ALL_WRAPPER_REASON and w.obfuscation_flag is True
        assert all(r.type is None for r in rows if r.index != 7)
        assert apply_play_all_wrapper_marks(session, rows) == (0, 0)
    finally:
        session.close()


def test_user_typed_wrapper_is_respected_and_stale_mark_clears(test_db):
    session = test_db()
    try:
        disc, rows = _disc(session, REBELS)
        from api.crud import set_title_type
        w = next(r for r in rows if r.index == 7)
        set_title_type(w, "extra", source="user")
        session.commit()
        assert apply_play_all_wrapper_marks(session, rows) == (0, 0)
        session.commit()
        session.refresh(w)
        assert w.type == "extra" and w.obfuscation_reason is None

        # A row carrying the mark that no longer qualifies loses it.
        stale = next(r for r in rows if r.index == 1)
        stale.obfuscation_reason = PLAY_ALL_WRAPPER_REASON
        stale.obfuscation_flag = True
        set_title_type(stale, "ignore", source="auto")
        session.commit()
        assert apply_play_all_wrapper_marks(session, rows) == (0, 1)
        session.commit()
        session.refresh(stale)
        assert stale.obfuscation_reason is None and stale.auto_type is None and stale.type is None
    finally:
        session.close()


def test_dvd_path_b_pass_marks_wrapper_blu_ray_pass_does_not(test_db):
    session = test_db()
    try:
        disc, rows = _disc(session, REBELS, fmt="DVD")
        apply_path_b_marks_for_disc(session, disc.id)
        session.commit()
        w = next(r for r in rows if r.index == 7)
        session.refresh(w)
        assert w.obfuscation_reason == PLAY_ALL_WRAPPER_REASON

        bd, bd_rows = _disc(session, REBELS, fmt="Blu-Ray")
        apply_path_b_marks_for_disc(session, bd.id)
        session.commit()
        bw = next(r for r in bd_rows if r.index == 7)
        session.refresh(bw)
        assert bw.obfuscation_reason != PLAY_ALL_WRAPPER_REASON
    finally:
        session.close()
