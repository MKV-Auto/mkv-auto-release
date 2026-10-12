"""Multi-angle main features are matched to their title after a rip.

2026-10-08 RC test, Star Wars Ep. III (Blu-ray): MakeMKV's disc scan put the
multi-angle main feature 00800.mpls at title index 10, but the rip numbered
it ``_t03.mkv`` and announced it with MSG 3308 ("(angle 1) was added as title
#3"), which the rip didn't recognise. Verification then matched only 10 of
the 11 files and waited out its 10-minute timer on a complete rip.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

from api import models
from workers.rip_verification_impl import _add_rip_time_matches
from workers.tasks import RIP_TITLE_ADDED_RE, _normalize_ripped_files_to_title_ids

# Real scan indices and rip filenames from the Ep. III job (b431a825).
SCAN = [(0, "00142.mpls"), (1, "00650.mpls"), (2, "00020.mpls"), (10, "00800.mpls"),
        (11, "00011.m2ts"), (12, "00122.m2ts"), (16, "00301.m2ts")]
RIP_NAMES = {"00142.mpls": "t00", "00650.mpls": "t01", "00020.mpls": "t02", "00800.mpls": "t03",
             "00011.m2ts": "t11", "00122.m2ts": "t12", "00301.m2ts": "t16"}


def _disc(session):
    disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:12]}")
    session.add(disc)
    session.flush()
    ids = {}
    for idx, src in SCAN:
        t = models.DiscTitle(id=str(uuid.uuid4()), disc_id=disc.id, index=idx, source_file=src)
        session.add(t)
        ids[src] = t.id
    session.commit()
    return disc, ids


def _on_disk() -> dict:
    """gather_final_outputs-style result: keyed by output filename."""
    return {f"Revenge of the Sith_{n}.mkv": f"Revenge of the Sith_{n}.mkv" for n in RIP_NAMES.values()}


def test_rip_numbered_angle_title_is_matched_from_the_rips_own_record(test_db):
    with test_db() as db:
        disc, ids = _disc(db)
        # What the rip recorded while MakeMKV wrote each file (correct for all).
        job = SimpleNamespace(ripped_files={
            ids[src]: f"Revenge of the Sith_{n}.mkv" for src, n in RIP_NAMES.items()
        })

        without = _normalize_ripped_files_to_title_ids(db, _on_disk(), disc_id=disc.id)
        assert ids["00800.mpls"] not in without          # the old 10/11 shape
        assert len(without) == len(SCAN) - 1

        with_rip = _normalize_ripped_files_to_title_ids(
            db, _on_disk(), disc_id=disc.id, filename_to_title_id=_add_rip_time_matches({}, job)
        )
        assert len(with_rip) == len(SCAN)
        assert with_rip[ids["00800.mpls"]] == "Revenge of the Sith_t03.mkv"


def test_existing_matches_win_and_bad_records_are_ignored():
    job = SimpleNamespace(ripped_files={"tid-a": "raw/x_t03.mkv", "": "y.mkv", "tid-b": None})
    out = _add_rip_time_matches({"x_t03.mkv": "from-payload"}, job)
    assert out == {"x_t03.mkv": "from-payload"}
    assert _add_rip_time_matches({}, SimpleNamespace(ripped_files=None)) == {}


def test_rip_recognises_multi_angle_title_lines():
    angle = 'MSG:3308,16777216,3,"File 00800.mpls (angle 1) was added as title #3","File %1 (angle %3) was added as title #%2","00800.mpls","3","1"'
    plain = 'MSG:3307,0,2,"File 00142.mpls was added as title #0","File %1 was added as title #%2","00142.mpls","0"'
    dup = 'MSG:3309,16777216,2,"Title 00850.mpls is equal to title 00800.mpls and was skipped"'
    assert RIP_TITLE_ADDED_RE.match(angle).groups() == ("00800.mpls", "3")
    assert RIP_TITLE_ADDED_RE.match(plain).groups() == ("00142.mpls", "0")
    assert RIP_TITLE_ADDED_RE.match(dup) is None


# --- #898: the real gather step, A New Hope (1.7.0-rc.2, job aa1c5ef8) -------
#
# The test above fed normalization filename-keyed results, but the real
# gather_final_outputs() maps files by the scan's predicted output name
# (DiscTitle.comment) and, once any file matches, drops the rest. The 9-angle
# main feature (00850.mpls, scan index 10, predicted "_t10") was written by the
# rip as "_t02", so it never reached normalization and verification counted
# 9/10 for 10 minutes.

NEW_HOPE_SCAN = [(0, "00650.mpls"), (1, "00020.mpls"), (10, "00850.mpls"), (11, "00011.m2ts"),
                 (12, "00122.m2ts"), (13, "00123.m2ts"), (14, "00720.m2ts"), (15, "00300.m2ts"),
                 (16, "00301.m2ts"), (17, "00198.m2ts")]
NEW_HOPE_RIP_INDEX = {10: 2}  # everything else is written under its scan index


def _new_hope(session, raw):
    disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:12]}")
    session.add(disc)
    session.flush()
    ids, ripped = {}, {}
    for idx, src in NEW_HOPE_SCAN:
        t = models.DiscTitle(id=str(uuid.uuid4()), disc_id=disc.id, index=idx, source_file=src,
                             comment=f"A New Hope_t{idx:02d}.mkv")
        session.add(t)
        ids[idx] = t.id
        name = f"A New Hope_t{NEW_HOPE_RIP_INDEX.get(idx, idx):02d}.mkv"
        (raw / name).write_bytes(f"mkv {idx}".encode())
        ripped[t.id] = name
    session.commit()
    return disc, ids, SimpleNamespace(ripped_files=ripped)


class _JobTask:
    def gather_final_outputs(self, root, **kw):
        from workers.tasks import gather_final_outputs
        return gather_final_outputs(root, **kw)


def test_gather_step_alone_drops_the_angle_feature(test_db, tmp_path):
    from workers.tasks import gather_final_outputs

    with test_db() as db:
        disc, ids, _job = _new_hope(db, tmp_path)
        paths, _ = gather_final_outputs(tmp_path, disc_id=disc.id, db=db, skip_hashes=True)
        assert len(paths) == len(NEW_HOPE_SCAN) - 1
        assert ids[10] not in paths


def test_verification_gather_keeps_the_angle_feature_and_hashes_it(test_db, tmp_path):
    from workers.rip_verification_impl import _gather_outputs

    with test_db() as db:
        disc, ids, job = _new_hope(db, tmp_path)
        f2t = _add_rip_time_matches({}, job)

        paths, _ = _gather_outputs(_JobTask(), tmp_path, disc.id, db, f2t, skip_hashes=True)
        assert len(paths) == len(NEW_HOPE_SCAN)
        assert paths[ids[10]] == "A New Hope_t02.mkv"

        normalized = _normalize_ripped_files_to_title_ids(db, paths, disc_id=disc.id, filename_to_title_id=f2t)
        assert len(normalized) == len(NEW_HOPE_SCAN)

        paths, hashes = _gather_outputs(_JobTask(), tmp_path, disc.id, db, f2t)
        assert ids[10] in hashes and len(hashes) == len(NEW_HOPE_SCAN)


def test_unmatched_file_never_takes_a_title_that_is_already_matched(test_db, tmp_path):
    """An unmatched file whose rip record points at a title the scan's names
    already matched is left out, not mapped twice."""
    from workers.rip_verification_impl import _gather_outputs

    with test_db() as db:
        disc, ids, job = _new_hope(db, tmp_path)
        (tmp_path / "A New Hope_t99.mkv").write_bytes(b"stray")
        f2t = {**_add_rip_time_matches({}, job), "A New Hope_t99.mkv": ids[11]}

        paths, _ = _gather_outputs(_JobTask(), tmp_path, disc.id, db, f2t, skip_hashes=True)
        assert paths[ids[11]] == "A New Hope_t11.mkv"
        assert "A New Hope_t99.mkv" not in paths.values()
