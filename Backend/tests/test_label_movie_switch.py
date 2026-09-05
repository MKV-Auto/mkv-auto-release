"""#858: switching a disc's movie must not carry the old release's identity.

Prod incident: a Clone Wars disc first labeled against the Resident Evil
Limited Edition Collection was re-labeled to the right movie — the stale
labelForm still carried the RE release's name/year, and a brand-new release
named "Resident Evil: Limited Edition Collection" was created under the
Clone Wars movie. The guard only covered movie CLEAR, not movie SWITCH.
"""

from __future__ import annotations

import uuid

import pytest

from api import models


@pytest.fixture
def patched_disc_numbers(monkeypatch):
    monkeypatch.setattr(
        "api.crud.normalize_disc_numbers_for_release",
        lambda db, rel, exclude_disc_id=None: {},
    )


def _seed(db, *, extra_disc_on_release=False):
    """Movie A with release R_old carrying the incident-shaped identity, one
    disc linked to it (season 5 in the draft), movie B to switch to."""
    movie_a = models.Movie(id=str(uuid.uuid4()), name="Resident Evil")
    movie_b = models.Movie(id=str(uuid.uuid4()), name="Star Wars: The Clone Wars", tmdb_type="tv")
    db.add_all([movie_a, movie_b])
    db.flush()
    rel_old = models.Release(
        id=str(uuid.uuid4()),
        slug="resident-evil-limited-edition-collection",
        type="movie",
        name="Resident Evil: Limited Edition Collection",
        release_year=2020,
        movie_id=movie_a.id,
    )
    db.add(rel_old)
    db.flush()
    disc = models.Disc(
        id=str(uuid.uuid4()),
        content_hash=f"hash-{uuid.uuid4().hex[:16]}",
        release_id=rel_old.id,
        disc_number=7,
        format="DVD",
        label_draft={"movie_id": movie_a.id, "release_id": rel_old.id, "primary_season": 5},
    )
    db.add(disc)
    if extra_disc_on_release:
        db.add(models.Disc(
            id=str(uuid.uuid4()),
            content_hash=f"hash-{uuid.uuid4().hex[:16]}",
            release_id=rel_old.id,
            disc_number=1,
        ))
    db.commit()
    return movie_a, movie_b, rel_old, db.query(models.Disc).filter(models.Disc.id == disc.id).first()


def test_movie_switch_unlinks_and_drops_stale_release_identity(test_db, patched_disc_numbers):
    """The incident shape: switch movie while the form still carries the old
    release's name/year. The old release must survive untouched (movie AND
    name), the disc must unlink, and the stale hints must not persist."""
    from api.routers.jobs import _apply_label_to_records

    with test_db() as db:
        movie_a, movie_b, rel_old, disc = _seed(db, extra_disc_on_release=True)
        lp = {
            "movie_id": movie_b.id,
            "release_id": rel_old.id,  # stale merged form still points at the old release
            "release_slug": rel_old.slug,
            "release_name": "Resident Evil: Limited Edition Collection",
            "release_year": 2020,
        }
        _apply_label_to_records(disc, lp, db)
        db.commit()

        db.refresh(disc)
        rel_old = db.query(models.Release).filter(models.Release.id == rel_old.id).first()
        # Shared release neither deleted nor re-pointed nor renamed.
        assert rel_old is not None
        assert rel_old.movie_id == movie_a.id
        assert rel_old.name == "Resident Evil: Limited Edition Collection"
        # Disc detached from the old identity; new movie kept for the next step.
        assert disc.release_id is None
        assert disc.label_draft.get("movie_id") == movie_b.id
        assert disc.label_draft.get("release_id") is None
        # The season (disc-scoped fact) survives the unlink (#845 rc.2 class).
        assert disc.label_draft.get("primary_season") == 5


def test_movie_switch_orphan_old_release_is_cleaned_up(test_db, patched_disc_numbers):
    from api.routers.jobs import _apply_label_to_records

    with test_db() as db:
        movie_a, movie_b, rel_old, disc = _seed(db, extra_disc_on_release=False)
        _apply_label_to_records(disc, {"movie_id": movie_b.id, "release_name": rel_old.name}, db)
        db.commit()
        assert db.query(models.Release).filter(models.Release.id == rel_old.id).first() is None


def test_combined_movie_and_release_selection_still_links(test_db, patched_disc_numbers):
    """Picking a release that already belongs to the NEW movie in the same
    save is legitimate and must keep working."""
    from api.routers.jobs import _apply_label_to_records

    with test_db() as db:
        movie_a, movie_b, rel_old, disc = _seed(db, extra_disc_on_release=True)
        rel_new = models.Release(
            id=str(uuid.uuid4()),
            slug="cw-season-1-5",
            type="series",
            name="Season 1-5 Collector's Edition",
            movie_id=movie_b.id,
        )
        db.add(rel_new)
        db.commit()

        _apply_label_to_records(disc, {"movie_id": movie_b.id, "release_id": rel_new.id}, db)
        db.commit()

        db.refresh(disc)
        assert disc.release_id == rel_new.id
        rel_old = db.query(models.Release).filter(models.Release.id == rel_old.id).first()
        assert rel_old is not None and rel_old.movie_id == movie_a.id


def test_same_movie_save_still_applies_release_fields(test_db, patched_disc_numbers):
    """No switch: release edits (edition rename) keep flowing to the release."""
    from api.routers.jobs import _apply_label_to_records

    with test_db() as db:
        movie_a, movie_b, rel_old, disc = _seed(db, extra_disc_on_release=True)
        _apply_label_to_records(
            disc,
            {"movie_id": movie_a.id, "release_id": rel_old.id, "release_name": "Renamed Edition"},
            db,
        )
        db.commit()
        db.refresh(rel_old)
        assert rel_old.name == "Renamed Edition"
        assert disc.release_id == rel_old.id
