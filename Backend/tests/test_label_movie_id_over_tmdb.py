"""Regression: label payload movie_id must win over stale tmdb_id for release.movie_id and labelForm."""

from __future__ import annotations

import uuid

import pytest

from api import models


@pytest.mark.xfail(reason="staging baseline fail; tracked in #405", strict=True)
def test_apply_label_to_records_prefers_movie_id_over_tmdb_id(test_db, monkeypatch):
    """Stale tmdb_id must not set releases.movie_id when movie_id is present."""
    from api.routers.jobs import _apply_label_to_records

    monkeypatch.setattr(
        "api.crud.normalize_disc_numbers_for_release",
        lambda db, rel, exclude_disc_id=None: {},
    )

    with test_db() as db:
        movie_a = models.Movie(id=str(uuid.uuid4()), name="Film A", tmdb_id="111")
        movie_b = models.Movie(id=str(uuid.uuid4()), name="Film B", tmdb_id="222")
        db.add_all([movie_a, movie_b])
        db.flush()

        rel = models.Release(
            id=str(uuid.uuid4()),
            slug="test-rel",
            type="movie",
            name="R",
            movie_id=movie_a.id,
        )
        db.add(rel)
        db.flush()

        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=f"hash-{uuid.uuid4().hex[:16]}",
            release_id=rel.id,
            disc_number=1,
        )
        db.add(disc)
        db.commit()

        disc = db.query(models.Disc).filter(models.Disc.id == disc.id).first()
        rel = db.query(models.Release).filter(models.Release.id == rel.id).first()

        lp = {
            "release_id": rel.id,
            "movie_id": movie_b.id,
            "tmdb_id": movie_a.tmdb_id,
            "disc_number": 1,
        }
        _apply_label_to_records(disc, lp, db)
        db.refresh(rel)
        assert rel.movie_id == movie_b.id


def test_apply_label_to_records_updates_titles_from_tracks_key(test_db, monkeypatch):
    """complete_label sends labelForm with tracks[]; backend must apply them like titles[]."""
    from api.routers.jobs import _apply_label_to_records, _validate_all_titles_labeled

    monkeypatch.setattr(
        "api.crud.normalize_disc_numbers_for_release",
        lambda db, rel, exclude_disc_id=None: {},
    )

    with test_db() as db:
        tid = str(uuid.uuid4())
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=f"hash-{uuid.uuid4().hex[:16]}",
            release_id=None,
        )
        title = models.DiscTitle(
            id=tid,
            disc_id=disc.id,
            source_file="00800.mpls",
            type=None,
            title=None,
            order_index=0,
        )
        db.add_all([disc, title])
        db.commit()

        disc = db.query(models.Disc).filter(models.Disc.id == disc.id).first()
        lp = {
            "tracks": [
                {
                    "title_id": tid,
                    "source_file": "00800.mpls",
                    "type": "ignore",
                    "title": "",
                }
            ]
        }
        _apply_label_to_records(disc, lp, db)
        db.commit()
        db.refresh(title)
        assert (title.type or "").lower() == "ignore"

        ok, bad = _validate_all_titles_labeled(disc, db)
        assert ok is True
        assert bad == []


def test_build_labelform_derives_tmdb_from_movie_id(test_db):
    """GET labelForm tmdb_id must match Movie.tmdb_id when movie_id is set (not stale merged tmdb)."""
    from api.routers.jobs import _build_labelform_from_job

    with test_db() as db:
        movie = models.Movie(id=str(uuid.uuid4()), name="Synced", tmdb_id="99999")
        db.add(movie)
        db.flush()

        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=f"hash-{uuid.uuid4().hex[:16]}",
            release_id=None,
            label_draft={"movie_id": movie.id, "group_type": "movie"},
        )
        db.add(disc)
        db.flush()

        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            mode="copy",
            disc_payload={
                "label_payload": {"tmdb_id": "111", "release_name": "X"},
            },
        )
        db.add(job)
        db.commit()

        job = db.query(models.Job).filter(models.Job.id == job.id).first()
        form = _build_labelform_from_job(job)
        assert form.get("movie_id") == movie.id
        assert form.get("tmdb_id") == "99999"
