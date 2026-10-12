"""#892: filler stubs (≤10 s and ≤1 MiB) are auto-ignored — and nothing else is.

Thresholds come from the prod library as labelled by hand: 965 titles under
both limits were ignored and none kept; the smallest title ever kept is the
3.05 MB, 5 s Matrix backdrop.
"""
from __future__ import annotations

import uuid

import pytest

from api import models
from core.filler_stub import FILLER_STUB_REASON, apply_filler_stub_marks, is_filler_stub
from core.path_b_dedupe import apply_path_b_marks_for_disc

MB = 1024 * 1024


@pytest.mark.parametrize("duration, size, expected", [
    (0, 0.92 * MB, True),        # Star Wars 00650.mpls
    (6, 0.55 * MB, True),        # Star Wars 00142.mpls
    (10, 1 * MB, True),          # both limits are inclusive
    (5, 3.05 * MB, False),       # Matrix backdrop the user kept
    (9, 14.17 * MB, False),      # Matrix UHD extra the user kept
    (10.5, 0.5 * MB, False),     # just too long
    (30, 0.2 * MB, False),       # long-but-tiny (still-image loop) is not a stub
    (None, 0.5 * MB, False),     # unknown duration never qualifies
    (5, None, False),            # unknown size never qualifies
])
def test_is_filler_stub(duration, size, expected):
    assert is_filler_stub(duration, size) is expected


def _disc(session, spec, fmt="Blu-Ray"):
    disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"fs-{uuid.uuid4().hex[:8]}", format=fmt)
    session.add(disc)
    session.flush()
    rows = []
    for i, fields in enumerate(spec):
        row = models.DiscTitle(id=str(uuid.uuid4()), disc_id=disc.id, index=i, order_index=i,
                               source_file=f"{i:05d}.mpls", **fields)
        session.add(row)
        rows.append(row)
    session.commit()
    return disc, rows


def test_stub_is_auto_ignored_with_a_reason_and_is_idempotent(test_db):
    session = test_db()
    try:
        _, rows = _disc(session, [
            dict(duration=6, size=int(0.55 * MB)),       # stub
            dict(duration=5, size=int(3.05 * MB)),       # kept-size backdrop
            dict(duration=5640, size=40 * 1024 * MB),    # feature
        ])
        assert apply_filler_stub_marks(session, rows) == (1, 0)
        session.commit()
        stub, backdrop, feature = rows
        assert stub.auto_type == "ignore" and stub.type == "ignore"
        assert stub.obfuscation_reason == FILLER_STUB_REASON
        assert not stub.obfuscation_flag            # a stub is not obfuscation
        assert backdrop.auto_type is None and backdrop.obfuscation_reason is None
        assert feature.auto_type is None
        assert apply_filler_stub_marks(session, rows) == (0, 0)
    finally:
        session.close()


def test_user_and_discdb_labels_are_respected(test_db):
    from api.crud import set_title_type
    session = test_db()
    try:
        _, rows = _disc(session, [dict(duration=4, size=200_000), dict(duration=4, size=200_000)])
        user_kept, discdb_typed = rows
        set_title_type(user_kept, "Extra", source="user")
        set_title_type(discdb_typed, "Backdrop", source="auto")
        session.commit()
        assert apply_filler_stub_marks(session, rows) == (0, 0)
        assert user_kept.type == "Extra" and discdb_typed.type == "Backdrop"
        assert user_kept.obfuscation_reason is None and discdb_typed.obfuscation_reason is None
    finally:
        session.close()


def test_a_stronger_reason_is_kept(test_db):
    session = test_db()
    try:
        _, rows = _disc(session, [dict(duration=3, size=100_000, obfuscation_reason="segment_set_sibling",
                                       obfuscation_flag=True)])
        apply_filler_stub_marks(session, rows)
        assert rows[0].auto_type == "ignore"
        assert rows[0].obfuscation_reason == "segment_set_sibling"
    finally:
        session.close()


def test_mark_clears_when_a_rescan_moves_the_title_out_of_range(test_db):
    session = test_db()
    try:
        _, rows = _disc(session, [dict(duration=6, size=500_000)])
        apply_filler_stub_marks(session, rows)
        session.commit()
        rows[0].size = 40 * MB                      # corrected by a later scan
        assert apply_filler_stub_marks(session, rows) == (0, 1)
        assert rows[0].auto_type is None and rows[0].obfuscation_reason is None
    finally:
        session.close()


@pytest.mark.parametrize("fmt", ["Blu-Ray", "UHD", "DVD"])
def test_runs_from_the_per_disc_pass_for_every_format(test_db, fmt):
    session = test_db()
    try:
        disc, rows = _disc(session, [dict(duration=2, size=300_000), dict(duration=2700, size=2 * 1024 * MB)], fmt=fmt)
        apply_path_b_marks_for_disc(session, disc.id)
        session.commit()
        session.refresh(rows[0])
        assert rows[0].type == "ignore" and rows[0].obfuscation_reason == FILLER_STUB_REASON
        session.refresh(rows[1])
        assert rows[1].obfuscation_reason != FILLER_STUB_REASON
    finally:
        session.close()
