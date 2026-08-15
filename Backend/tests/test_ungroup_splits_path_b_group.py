"""Ungroup must actually split a title out of its Path B dedupe group.

``force_independent_group`` is the flag ``POST /discs/{d}/titles/{t}/ungroup-duplicate``
writes. Three grouping paths have to honour it, and only one did:

* ``attach_duplicate_info``            — honoured it (#797)
* ``compute_dedupe_groups``            — did NOT, so ``sibling_title_ids`` kept
  listing the row; that list is the only thing that hides a row from the left
  rail, so Ungroup fired its request and nothing moved (mkv-auto-release#8).
* ``sync_duplicate_group_labels_for_disc`` — did NOT, so save_label /
  complete_label re-demoted the row and reverted the ungroup.

The reporter's disc is the shape covered here: several same-length episodes
sharing a sorted segment set, wrongly collapsed into one duplicate group.
"""
import uuid

import pytest

from api import models
from core.duplicate_group_sync import sync_duplicate_group_labels_for_disc
from core.path_b_dedupe import compute_dedupe_groups


def _episodes(n=5, *, base_dur=1350):
    """n same-segment, same-length titles — one wrongly-detected dupe group."""
    return {
        f"t{i}": {
            "title_id": f"t{i}",
            "source_file": f"title-{i}",
            # Sorted-segment-set equivalence is the grouping authority.
            "segment_map": "1-5,6,7",
            "duration": base_dur + i,
            "type": None,
        }
        for i in range(n)
    }


class TestComputeDedupeGroups:
    def test_group_forms_without_the_flag(self):
        groups = compute_dedupe_groups(_episodes())
        assert len(groups) == 1
        members = {groups[0].representative_title_id, *groups[0].sibling_title_ids}
        assert members == {"t0", "t1", "t2", "t3", "t4"}

    def test_ungrouped_sibling_leaves_the_group(self):
        titles = _episodes()
        titles["t3"]["force_independent_group"] = True

        groups = compute_dedupe_groups(titles)

        assert len(groups) == 1
        members = {groups[0].representative_title_id, *groups[0].sibling_title_ids}
        assert "t3" not in members, "ungrouped title must not stay a sibling"
        assert members == {"t0", "t1", "t2", "t4"}

    def test_ungrouped_representative_leaves_the_group(self):
        # The rep is the visible row; ungrouping it must not strand the rest.
        titles = _episodes()
        rep = compute_dedupe_groups(titles)[0].representative_title_id
        titles[rep]["force_independent_group"] = True

        groups = compute_dedupe_groups(titles)

        assert len(groups) == 1
        members = {groups[0].representative_title_id, *groups[0].sibling_title_ids}
        assert rep not in members
        assert len(members) == 4

    def test_group_dissolves_when_only_one_member_remains(self):
        titles = _episodes(n=2)
        titles["t0"]["force_independent_group"] = True

        # A lone survivor is a singleton, and singletons are not groups.
        assert compute_dedupe_groups(titles) == []

    def test_ungrouping_every_member_leaves_no_groups(self):
        titles = _episodes()
        for p in titles.values():
            p["force_independent_group"] = True

        assert compute_dedupe_groups(titles) == []


@pytest.fixture
def session(test_db):
    s = test_db()
    try:
        yield s
    finally:
        s.close()


def _disc_with_dupes(session, n=5, *, ungrouped=()):
    disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:10]}")
    session.add(disc)
    session.flush()
    rows = []
    for i in range(n):
        t = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file=f"title-{i}",
            segment_map="1-5,6,7",
            index=i,
            order_index=i,
            duration=1350 + i,
            # All start active so a demotion to False is observable; a row
            # that starts False proves nothing about whether the sync spared it.
            active=True,
            force_independent_group=(i in ungrouped),
        )
        session.add(t)
        rows.append(t)
    session.commit()
    return disc, rows


class TestLabelSyncRespectsUngroup:
    def test_secondaries_are_demoted_when_nothing_is_ungrouped(self, session):
        # Baseline: the sync does demote, so the next test proves an exemption
        # rather than a sync that never fires.
        disc, rows = _disc_with_dupes(session)
        assert sync_duplicate_group_labels_for_disc(session, str(disc.id)) > 0
        session.commit()
        for t in rows[1:]:
            session.refresh(t)
            assert t.active is False

    def test_ungrouped_row_is_not_demoted(self, session):
        disc, rows = _disc_with_dupes(session, ungrouped=(3,))
        target = rows[3]

        sync_duplicate_group_labels_for_disc(session, str(disc.id))
        session.commit()

        session.refresh(target)
        assert target.active is True, (
            "save_label would revert the user's ungroup by demoting the row"
        )
        # The rest of the group is still deduped — only the split-off row is spared.
        session.refresh(rows[1])
        assert rows[1].active is False

    def test_ungrouped_row_keeps_its_type(self, session):
        # Consensus fill is the other way the sync overwrites a split-off row.
        disc, rows = _disc_with_dupes(session, ungrouped=(3,))
        for t in rows:
            if t is not rows[3]:
                t.type = "ignore"
        rows[3].type = None
        session.commit()

        sync_duplicate_group_labels_for_disc(session, str(disc.id))
        session.commit()

        session.refresh(rows[3])
        assert rows[3].type is None, "ungrouped row must not inherit sibling consensus"
