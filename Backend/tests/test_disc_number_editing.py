"""#873: the user can set a disc's number when discs were scanned out of order.

Numbers used to come only from scan order, and every scan of any sibling
renumbered the whole release, so a hand-set number couldn't survive.
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest
from fastapi.testclient import TestClient

from api import crud, models


def _release(db, *, tv=True, n=3, transferred=()):
    """A release with ``n`` discs scanned in order 1..n (created_at)."""
    movie = models.Movie(id=str(uuid.uuid4()), name="Ted Lasso", tmdb_type="tv" if tv else "movie")
    release = models.Release(id=str(uuid.uuid4()), movie_id=movie.id, name="Season 2",
                             slug=f"tl-{uuid.uuid4().hex[:6]}")
    db.add_all([movie, release]); db.flush()
    base = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    discs = []
    for i in range(n):
        d = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:8]}", format="Blu-Ray",
                        release_id=release.id, created_at=base + dt.timedelta(minutes=i),
                        label_draft={"primary_season": 2})
        db.add(d); discs.append(d)
    db.flush()
    crud.normalize_disc_numbers_for_release(db, release)
    from core.disc_naming import refresh_auto_disc_identity
    db.refresh(release)
    for d in discs:
        db.refresh(d)
        refresh_auto_disc_identity(d)
    for i in transferred:
        db.add(models.Job(id=str(uuid.uuid4()), disc_id=discs[i].id, disc_num="0", mount_point="/dev/sr0",
                          job_status="completed", rip_state="completed", transfer_state="completed"))
    db.commit()
    return release, discs


def _numbers(db, discs):
    return [db.get(models.Disc, d.id).disc_number for d in discs]


def test_the_normalizer_keeps_numbers_the_user_set(test_db):
    with test_db() as db:
        release, discs = _release(db)
        discs[2].disc_number, discs[2].disc_number_user = 1, True    # the third scan is really disc 1
        crud.normalize_disc_numbers_for_release(db, release)
        db.commit()
        assert _numbers(db, discs) == [2, 3, 1]
        crud.normalize_disc_numbers_for_release(db, release)        # a later scan doesn't undo it
        assert _numbers(db, discs) == [2, 3, 1]


def test_taking_an_occupied_number_swaps_the_two_discs(test_db):
    with test_db() as db:
        _, discs = _release(db)
        result = crud.set_disc_number(db, discs[2], 1)
        db.commit()
        assert _numbers(db, discs) == [3, 2, 1]
        assert result["swapped_with"].id == discs[0].id
        assert db.get(models.Disc, discs[0].id).disc_number_user is True


def test_automatic_names_follow_the_new_numbers(test_db):
    with test_db() as db:
        _, discs = _release(db)
        assert db.get(models.Disc, discs[2].id).disc_name == "Ted Lasso: Season 2 - Disc 3 - Blu-Ray"
        crud.set_disc_number(db, discs[2], 1)
        db.commit()
        assert db.get(models.Disc, discs[2].id).disc_name == "Ted Lasso: Season 2 - Disc 1 - Blu-Ray"
        assert db.get(models.Disc, discs[0].id).disc_name == "Ted Lasso: Season 2 - Disc 3 - Blu-Ray"


def test_a_custom_name_is_left_alone(test_db):
    with test_db() as db:
        _, discs = _release(db)
        d = db.get(models.Disc, discs[1].id)
        d.auto_naming, d.disc_name = False, "My Disc"
        db.commit()
        crud.set_disc_number(db, discs[1], 3)
        db.commit()
        assert db.get(models.Disc, discs[1].id).disc_name == "My Disc"


def test_the_api_warns_only_when_a_moved_disc_was_transferred(test_db):
    from api.main import app

    with test_db() as db:
        _, discs = _release(db, transferred=(0,))
        ids = [d.id for d in discs]
    client = TestClient(app)

    r = client.put(f"/discs/{ids[1]}/disc-number", json={"disc_number": 3})   # swaps 2 and 3: neither transferred
    assert r.status_code == 200, r.text
    assert r.json()["transferred"] is False and r.json()["message"] is None
    assert r.json()["swapped_with"]["disc_id"] == ids[2]

    r = client.put(f"/discs/{ids[1]}/disc-number", json={"disc_number": 1})   # swaps with transferred disc 1
    body = r.json()
    assert body["transferred"] is True
    assert "keep their current names" in body["message"]
    assert {c["disc_id"] for c in body["changed"]} >= {ids[0], ids[1]}


def test_the_api_rejects_zero(test_db):
    from api.main import app

    with test_db() as db:
        _, discs = _release(db)
        disc_id = discs[0].id
    r = TestClient(app).put(f"/discs/{disc_id}/disc-number", json={"disc_number": 0})
    assert r.status_code == 400


def test_reset_to_scan_order(test_db):
    from api.main import app

    with test_db() as db:
        _, discs = _release(db)
        crud.set_disc_number(db, discs[2], 1)
        db.commit()
        ids = [d.id for d in discs]
    r = TestClient(app).post(f"/discs/{ids[0]}/disc-number/reset")
    assert r.status_code == 200, r.text
    with test_db() as db:
        assert [db.get(models.Disc, i).disc_number for i in ids] == [1, 2, 3]
        assert all(not db.get(models.Disc, i).disc_number_user for i in ids)
        assert db.get(models.Disc, ids[2]).disc_name == "Ted Lasso: Season 2 - Disc 3 - Blu-Ray"
