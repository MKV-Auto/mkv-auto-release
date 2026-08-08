"""#318 ambiguity check vs. the #796 multi-part episode layout.

Two titles may share a name as long as the generated filename tells them apart.
``core.disc.compute_expected_path`` folds ``part`` into a ``- partN`` suffix and
``episode_end`` into an ``s03e01-e02`` range, so titles differing only by those
fields produce distinct files and must not be rejected as duplicates.

Regression: Star Wars Rebels S3D1 presents episode 1 as two playlists
(00801.mpls / 00802.mpls). Both were correctly labelled "Steps Into Shadow"
S3E1 part 1-of-2 and part 2-of-2, and finalising the disc failed because the
identity tuple omitted ``part``.
"""
import uuid

import pytest

from api import models
from api.routers.jobs import (
    _find_ambiguous_titles,
    _find_unlabeled_titles,
    _validate_all_titles_labeled,
)


def _disc(session, content_hash):
    disc = models.Disc(id=str(uuid.uuid4()), content_hash=content_hash)
    session.add(disc)
    session.flush()
    return disc


def _episode(disc, *, index, source_file, segment_map, **overrides):
    fields = dict(
        id=str(uuid.uuid4()),
        disc_id=disc.id,
        source_file=source_file,
        segment_map=segment_map,
        type="Episode",
        title="Steps Into Shadow",
        season=3,
        episode=1,
        order_index=index,
    )
    fields.update(overrides)
    return models.DiscTitle(**fields)


class TestMultiPartEpisodesAreNotAmbiguous:
    def test_part_1_and_part_2_of_the_same_episode_are_accepted(self, test_db):
        # The exact prod shape: distinct segment maps, distinct playlists, same
        # name/season/episode, told apart only by `part`.
        session = test_db()
        try:
            disc = _disc(session, "rebels-s3d1")
            session.add(_episode(disc, index=0, source_file="00801.mpls", segment_map="306", part=1, part_of=2))
            session.add(_episode(disc, index=1, source_file="00802.mpls", segment_map="307", part=2, part_of=2))
            session.commit()

            assert _find_unlabeled_titles(disc, session) == []
            assert _find_ambiguous_titles(disc, session) == []
            ok, offenders = _validate_all_titles_labeled(disc, session)
            assert ok is True
            assert offenders == []
        finally:
            session.close()

    def test_episode_end_alone_distinguishes_titles(self, test_db):
        # Same season and episode; only `episode_end` differs, rendering as
        # `s03e01` vs `s03e01-e02`. episode_end must be part of the identity.
        session = test_db()
        try:
            disc = _disc(session, "rebels-range")
            session.add(_episode(disc, index=0, source_file="a.mpls", segment_map="401"))
            session.add(_episode(disc, index=1, source_file="b.mpls", segment_map="402", episode_end=2))
            session.commit()

            assert _find_ambiguous_titles(disc, session) == []
            assert _validate_all_titles_labeled(disc, session)[0] is True
        finally:
            session.close()

    def test_two_different_ranges_are_accepted(self, test_db):
        # s03e01-e02 and s03e03-e04: distinguished by episode as well, but the
        # range form must not itself trip the check.
        session = test_db()
        try:
            disc = _disc(session, "rebels-two-ranges")
            session.add(_episode(disc, index=0, source_file="a.mpls", segment_map="411", episode_end=2))
            session.add(_episode(disc, index=1, source_file="b.mpls", segment_map="412", episode=3, episode_end=4))
            session.commit()

            assert _find_ambiguous_titles(disc, session) == []
        finally:
            session.close()

    def test_three_part_episode_is_accepted(self, test_db):
        session = test_db()
        try:
            disc = _disc(session, "rebels-three-part")
            for n in (1, 2, 3):
                session.add(
                    _episode(disc, index=n - 1, source_file=f"0080{n}.mpls", segment_map=str(300 + n), part=n, part_of=3)
                )
            session.commit()

            assert _find_ambiguous_titles(disc, session) == []
        finally:
            session.close()


class TestGenuineAmbiguityStillRejected:
    def test_identical_titles_with_no_distinguishing_field_are_flagged(self, test_db):
        session = test_db()
        try:
            disc = _disc(session, "rebels-truly-dupe")
            session.add(_episode(disc, index=0, source_file="a.mpls", segment_map="501"))
            session.add(_episode(disc, index=1, source_file="b.mpls", segment_map="502"))
            session.commit()

            ambiguous = _find_ambiguous_titles(disc, session)
            assert len(ambiguous) == 1, "second title of the colliding pair should be flagged"
            ok, offenders = _validate_all_titles_labeled(disc, session)
            assert ok is False
            assert offenders == ambiguous
        finally:
            session.close()

    def test_same_part_number_twice_is_still_ambiguous(self, test_db):
        # `part` distinguishes only when the values actually differ.
        session = test_db()
        try:
            disc = _disc(session, "rebels-same-part")
            session.add(_episode(disc, index=0, source_file="a.mpls", segment_map="601", part=1, part_of=2))
            session.add(_episode(disc, index=1, source_file="b.mpls", segment_map="602", part=1, part_of=2))
            session.commit()

            assert len(_find_ambiguous_titles(disc, session)) == 1
        finally:
            session.close()

    def test_one_part_set_and_one_null_is_distinguishable(self, test_db):
        # A bare episode and its part-1 sibling render as `...s03e01` and
        # `...s03e01 - part1`, which are different files.
        session = test_db()
        try:
            disc = _disc(session, "rebels-part-vs-null")
            session.add(_episode(disc, index=0, source_file="a.mpls", segment_map="701"))
            session.add(_episode(disc, index=1, source_file="b.mpls", segment_map="702", part=1, part_of=2))
            session.commit()

            assert _find_ambiguous_titles(disc, session) == []
        finally:
            session.close()


class TestUnlabeledAndAmbiguousAreDistinguished:
    def test_unlabeled_reports_only_the_unlabeled_title(self, test_db):
        session = test_db()
        try:
            disc = _disc(session, "rebels-unlabeled")
            good = _episode(disc, index=0, source_file="a.mpls", segment_map="801", part=1, part_of=2)
            blank = _episode(disc, index=1, source_file="b.mpls", segment_map="802", part=2, part_of=2, episode=None)
            session.add(good)
            session.add(blank)
            session.commit()

            unlabeled = _find_unlabeled_titles(disc, session)
            assert unlabeled == [str(blank.id)]
            # An unlabeled title is a separate problem from an ambiguous one.
            assert _find_ambiguous_titles(disc, session) == []
        finally:
            session.close()

    @pytest.mark.parametrize("type_name", ["ignore", "IGNORE", "Ignore"])
    def test_ignored_titles_never_collide(self, test_db, type_name):
        session = test_db()
        try:
            disc = _disc(session, f"rebels-ignore-{type_name}")
            session.add(_episode(disc, index=0, source_file="a.mpls", segment_map="901", type=type_name, title="dupe"))
            session.add(_episode(disc, index=1, source_file="b.mpls", segment_map="902", type=type_name, title="dupe"))
            session.commit()

            assert _find_ambiguous_titles(disc, session) == []
            assert _find_unlabeled_titles(disc, session) == []
        finally:
            session.close()
