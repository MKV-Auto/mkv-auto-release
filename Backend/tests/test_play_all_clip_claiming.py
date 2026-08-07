"""A user who labels an m2ts inside a play-all MPLS gets that clip ripped.

A `.mpls` can be a "play all" playlist wrapping several `.m2ts` clips.
Subsumption marks the clips `auto_type='ignore'` and the duplicate-group
sync deactivated them unconditionally — so a user could label a clip,
watch resolution correctly yield their type, and still never get the file.

Observed on Star Wars Rebels S3 D2 (`00215.mpls`, segments 396-400): all
five clips carried `auto_type=ignore`, `user_type=BehindTheScenes`,
`type=BehindTheScenes` and `active=false`.
"""
import uuid

import pytest

from api import models
from core.duplicate_group_sync import (
    apply_secondary_duplicate_row,
    user_claimed_row,
)
from core.duplicate_info import attach_duplicate_info
from core.path_b_dedupe import apply_subsumption_marks


def _clip(**kw):
    kw.setdefault("id", str(uuid.uuid4()))
    kw.setdefault("disc_id", "disc-1")
    kw.setdefault("index", 1)
    return models.DiscTitle(**kw)


class TestUserClaimedRowsStayActive:
    def test_a_clip_the_user_typed_is_reactivated(self):
        row = _clip(auto_type="ignore", user_type="BehindTheScenes",
                    type="BehindTheScenes", active=False)
        assert apply_secondary_duplicate_row(row) is True
        assert row.active is True

    def test_second_pass_is_idempotent(self):
        """Every True return bumps title_seq and re-inflates sibling seqs (#775)."""
        row = _clip(auto_type="ignore", user_type="BehindTheScenes",
                    type="BehindTheScenes", active=False)
        apply_secondary_duplicate_row(row)
        assert apply_secondary_duplicate_row(row) is False
        assert row.active is True

    def test_an_unclaimed_clip_is_still_deactivated(self):
        row = _clip(auto_type="ignore", type="ignore", active=True)
        assert apply_secondary_duplicate_row(row) is True
        assert row.active is False

    def test_a_user_who_types_ignore_has_not_claimed_it(self):
        row = _clip(auto_type="ignore", user_type="ignore", type="ignore", active=True)
        assert user_claimed_row(row) is False
        apply_secondary_duplicate_row(row)
        assert row.active is False

    @pytest.mark.parametrize("user_type", [None, ""])
    def test_no_user_type_is_not_a_claim(self, user_type):
        row = _clip(auto_type="ignore", user_type=user_type, active=True)
        assert user_claimed_row(row) is False


class TestUngroupSurvivesSubsumption:
    def test_force_independent_group_is_honoured_by_the_subsumption_pass(self):
        """The segment-set pass honoured it; the absorption pass did not, so a
        row the user ungrouped was pulled straight back into the wrapper."""
        wrapper_id, clip_id = str(uuid.uuid4()), str(uuid.uuid4())
        titles = {
            wrapper_id: {"segment_map": "396,397", "title_id": wrapper_id},
            clip_id: {
                "segment_map": "396",
                "title_id": clip_id,
                "subsumed_by_title_id": wrapper_id,
                "force_independent_group": True,
            },
        }
        attach_duplicate_info(titles, "disc-1")
        wrapper_group = titles[wrapper_id].get("duplicate_group_id")
        clip_group = titles[clip_id].get("duplicate_group_id")
        assert clip_group != wrapper_group or clip_group is None

    def test_without_the_flag_the_clip_still_folds_into_the_wrapper(self):
        wrapper_id, clip_id = str(uuid.uuid4()), str(uuid.uuid4())
        titles = {
            wrapper_id: {"segment_map": "396,397", "title_id": wrapper_id},
            clip_id: {"segment_map": "396", "title_id": clip_id,
                      "subsumed_by_title_id": wrapper_id},
        }
        attach_duplicate_info(titles, "disc-1")
        assert titles[clip_id].get("duplicate_group_id") == titles[wrapper_id].get("duplicate_group_id")


class TestWrapperStepsAsideWhenClipsAreClaimed:
    def _disc_with_wrapper_and_clip(self, session, *, clip_user_type=None,
                                    wrapper_user_type=None):
        disc = models.Disc(id="disc-pa", content_hash="play-all-1")
        session.add(disc)
        session.flush()
        wrapper = models.DiscTitle(id="w1", disc_id="disc-pa", index=35,
                                   segment_map="396,397", source_file="00215.mpls",
                                   user_type=wrapper_user_type,
                                   type=wrapper_user_type)
        clip = models.DiscTitle(id="c1", disc_id="disc-pa", index=73,
                                segment_map="396", source_file="00396.m2ts",
                                user_type=clip_user_type, type=clip_user_type)
        session.add_all([wrapper, clip])
        session.commit()
        return wrapper, clip

    def test_wrapper_auto_ignores_when_a_clip_is_claimed(self, test_db):
        session = test_db()
        try:
            wrapper, clip = self._disc_with_wrapper_and_clip(
                session, clip_user_type="BehindTheScenes")
            apply_subsumption_marks(session, "disc-pa", {"c1": "w1"})
            session.commit()
            session.refresh(wrapper)
            # Otherwise the same footage rips twice: play-all plus each clip.
            assert (wrapper.auto_type or "").lower() == "ignore"
        finally:
            session.close()

    def test_wrapper_is_left_alone_when_no_clip_is_claimed(self, test_db):
        session = test_db()
        try:
            wrapper, clip = self._disc_with_wrapper_and_clip(session)
            apply_subsumption_marks(session, "disc-pa", {"c1": "w1"})
            session.commit()
            session.refresh(wrapper)
            assert (wrapper.auto_type or "").lower() != "ignore"
        finally:
            session.close()

    def test_a_wrapper_the_user_typed_is_never_auto_ignored(self, test_db):
        """source='auto' loses to the user's type by resolution, but we should
        not even write it — the user asked to keep the play-all."""
        session = test_db()
        try:
            wrapper, clip = self._disc_with_wrapper_and_clip(
                session, clip_user_type="BehindTheScenes",
                wrapper_user_type="BehindTheScenes")
            apply_subsumption_marks(session, "disc-pa", {"c1": "w1"})
            session.commit()
            session.refresh(wrapper)
            assert (wrapper.auto_type or "").lower() != "ignore"
            assert wrapper.type == "BehindTheScenes"
        finally:
            session.close()
