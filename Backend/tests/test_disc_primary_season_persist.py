"""Tests for disc-card primary-season persistence via discs.label_draft (#536).

The disc-card primary-season selector lets the user pick which TMDB season a
multi-disc TV release maps to. Today this PR adds persistence through the
existing label_draft JSON column: write via _labelform_to_ops (label_draft
op) → _patch_disc_ops_internal whitelist; read via _build_labelform_from_disc.
"""
from __future__ import annotations

import uuid

from api import models
from api.routers.discs import _build_labelform_from_disc, _labelform_to_ops
from api.routers.releases import _patch_disc_ops_internal


def _mk_disc(db, *, label_draft=None) -> models.Disc:
    disc = models.Disc(
        id=str(uuid.uuid4()),
        content_hash=f"hash-{uuid.uuid4().hex[:16]}",
        release_id=None,
        label_draft=label_draft,
    )
    db.add(disc)
    db.commit()
    db.refresh(disc)
    return disc


# ── _labelform_to_ops: labelForm.primary_season → label_draft op ─────────────


def test_labelform_to_ops_emits_label_draft_with_primary_season():
    ops = _labelform_to_ops({"primary_season": 3})
    label_draft_ops = [op for op in ops if op.get("target") == "label_draft"]
    assert len(label_draft_ops) == 1
    assert label_draft_ops[0]["fields"] == {"primary_season": 3}


def test_labelform_to_ops_coerces_string_primary_season_to_int():
    ops = _labelform_to_ops({"primary_season": "2"})
    label_draft_ops = [op for op in ops if op.get("target") == "label_draft"]
    assert label_draft_ops[0]["fields"]["primary_season"] == 2


def test_labelform_to_ops_drops_garbage_primary_season_to_none():
    """Junk values are dropped to None rather than rejecting the whole op,
    so a co-edit of movie_id + bad primary_season still persists movie_id."""
    for bad in ("junk", 0, -1, None):
        ops = _labelform_to_ops({"movie_id": "m1", "primary_season": bad})
        label_draft_ops = [op for op in ops if op.get("target") == "label_draft"]
        assert label_draft_ops[0]["fields"]["primary_season"] is None
        assert label_draft_ops[0]["fields"]["movie_id"] == "m1"


def test_labelform_to_ops_omits_label_draft_op_when_no_keys_present():
    """labelForm with no movie_id/group_type/primary_season produces no
    label_draft op — protects unrelated PATCHes from a no-op draft write."""
    ops = _labelform_to_ops({"disc_name": "X"})
    label_draft_ops = [op for op in ops if op.get("target") == "label_draft"]
    assert label_draft_ops == []


# ── _patch_disc_ops_internal: whitelist persists primary_season ──────────────


def test_patch_disc_ops_internal_persists_primary_season_into_label_draft(test_db):
    with test_db() as db:
        disc = _mk_disc(db)
        ops = [{"target": "label_draft", "fields": {"primary_season": 2}}]
        _patch_disc_ops_internal(disc.id, ops, db)
        db.commit()
        db.refresh(disc)
        assert disc.label_draft == {"primary_season": 2}


def test_patch_disc_ops_internal_merges_primary_season_alongside_movie_id(test_db):
    """Co-existing keys survive merge; this is the real-world co-edit path
    (user picks movie THEN bumps the season)."""
    with test_db() as db:
        disc = _mk_disc(db, label_draft={"movie_id": "m1", "group_type": "series"})
        ops = [{"target": "label_draft", "fields": {"primary_season": 4}}]
        _patch_disc_ops_internal(disc.id, ops, db)
        db.commit()
        db.refresh(disc)
        assert disc.label_draft == {
            "movie_id": "m1", "group_type": "series", "primary_season": 4,
        }


def test_patch_disc_ops_internal_preserves_primary_season_through_unrelated_patch(test_db):
    """Subsequent PATCH that doesn't touch primary_season must not lose it."""
    with test_db() as db:
        disc = _mk_disc(db, label_draft={"primary_season": 3})
        ops = [{"target": "label_draft", "fields": {"group_type": "series"}}]
        _patch_disc_ops_internal(disc.id, ops, db)
        db.commit()
        db.refresh(disc)
        assert disc.label_draft["primary_season"] == 3
        assert disc.label_draft["group_type"] == "series"


def test_patch_label_draft_with_only_primary_season_preserves_release_link(test_db):
    """Regression for #538: `setPrimarySeason` PATCHes only
    `{primary_season: N}` into label_draft. The unlink guard at
    `releases.py:1734` must NOT treat a missing `movie_id` key as "movie
    cleared" — otherwise the disc loses its release link and
    `cleanup_orphaned_release` silently deletes the release.
    """
    from api.crud import cleanup_orphaned_release  # noqa: F401  (proves import path exists)
    with test_db() as db:
        # Set up a release with the disc linked.
        movie = models.Movie(id=str(uuid.uuid4()), name="Fallout", tmdb_id="106379")
        db.add(movie)
        db.flush()
        release = models.Release(
            id=str(uuid.uuid4()), slug="fallout-s2-2026",
            name="Fallout Season 2", type="series", movie_id=movie.id,
            release_year=2026, upc="0001234567890",
            cover_front_url="https://example.com/cover.jpg",
        )
        db.add(release)
        db.flush()
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=f"hash-{uuid.uuid4().hex[:16]}",
            release_id=release.id,
            disc_number=1,
            label_draft={"movie_id": movie.id, "group_type": "series"},
        )
        db.add(disc)
        db.commit()
        db.refresh(disc)
        assert disc.release_id == release.id

        # The actual bug repro: primary_season-only PATCH.
        ops = [{"target": "label_draft", "fields": {"primary_season": 2}}]
        _patch_disc_ops_internal(disc.id, ops, db)
        db.commit()
        db.refresh(disc)

        # Disc must STILL be linked. Release must STILL exist.
        assert disc.release_id == release.id, "primary_season-only PATCH must not unlink the disc"
        assert (
            db.query(models.Release).filter(models.Release.id == release.id).first() is not None
        ), "release must not be deleted by orphan cleanup"
        # And the season was actually persisted.
        assert disc.label_draft["primary_season"] == 2


def test_patch_label_draft_with_explicit_movie_id_none_still_unlinks(test_db):
    """The intentional unlink path must keep working: PATCH with
    `{movie_id: None}` is the user's explicit "change movie" action and
    must unlink + cleanup. Pin so the #538 narrowing doesn't accidentally
    block this path too.
    """
    with test_db() as db:
        movie = models.Movie(id=str(uuid.uuid4()), name="X", tmdb_id="1")
        db.add(movie)
        db.flush()
        release = models.Release(
            id=str(uuid.uuid4()), slug="x-2026", name="X", type="movie", movie_id=movie.id,
        )
        db.add(release)
        db.flush()
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=f"hash-{uuid.uuid4().hex[:16]}",
            release_id=release.id,
            disc_number=1,
        )
        db.add(disc)
        db.commit()
        db.refresh(disc)

        ops = [{"target": "label_draft", "fields": {"movie_id": None}}]
        _patch_disc_ops_internal(disc.id, ops, db)
        db.commit()
        db.refresh(disc)

        assert disc.release_id is None, "explicit movie_id=None must unlink"
        # Orphan cleanup deletes the now-zero-disc release.
        assert (
            db.query(models.Release).filter(models.Release.id == release.id).first() is None
        ), "explicit clear must trigger orphan-release cleanup"


def test_patch_label_draft_with_group_type_only_preserves_release_link(test_db):
    """Same guard applied to `group_type`-only PATCHes (e.g. user toggles
    Movie/Series). Was latently broken pre-#536 too; tests pin both
    sibling paths."""
    with test_db() as db:
        movie = models.Movie(id=str(uuid.uuid4()), name="X", tmdb_id="1")
        db.add(movie)
        db.flush()
        release = models.Release(
            id=str(uuid.uuid4()), slug="x-2026-grp", name="X", type="movie", movie_id=movie.id,
            release_year=2026, upc="0009876543210",
            cover_front_url="https://example.com/cover-x.jpg",
        )
        db.add(release)
        db.flush()
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=f"hash-{uuid.uuid4().hex[:16]}",
            release_id=release.id,
            disc_number=1,
            label_draft={"movie_id": movie.id},
        )
        db.add(disc)
        db.commit()
        db.refresh(disc)

        ops = [{"target": "label_draft", "fields": {"group_type": "series"}}]
        _patch_disc_ops_internal(disc.id, ops, db)
        db.commit()
        db.refresh(disc)

        assert disc.release_id == release.id, "group_type-only PATCH must not unlink the disc"
        assert (
            db.query(models.Release).filter(models.Release.id == release.id).first() is not None
        )


def test_patch_disc_ops_internal_whitelist_drops_unknown_keys(test_db):
    """Whitelist allows movie_id/group_type/primary_season; anything else
    falls off (existing behaviour, asserted here to pin the whitelist scope)."""
    with test_db() as db:
        disc = _mk_disc(db)
        ops = [{"target": "label_draft", "fields": {
            "primary_season": 5, "rogue_key": "x",
        }}]
        _patch_disc_ops_internal(disc.id, ops, db)
        db.commit()
        db.refresh(disc)
        assert "rogue_key" not in disc.label_draft
        assert disc.label_draft["primary_season"] == 5


# ── _build_labelform_from_disc: surface persisted value on load ──────────────


def _disc_info_blank() -> dict:
    return {}


def test_build_labelform_returns_primary_season_from_label_draft(test_db):
    with test_db() as db:
        disc = _mk_disc(db, label_draft={"primary_season": 4})
        form = _build_labelform_from_disc(disc, _disc_info_blank(), None, db)
        assert form["primary_season"] == 4


def test_build_labelform_returns_none_when_label_draft_has_no_primary_season(test_db):
    with test_db() as db:
        disc = _mk_disc(db, label_draft={"movie_id": "m1"})
        form = _build_labelform_from_disc(disc, _disc_info_blank(), None, db)
        assert form["primary_season"] is None


def test_build_labelform_treats_garbage_primary_season_as_none(test_db):
    """Defense in depth: even if a bad value somehow lands in label_draft
    (legacy data, migration accident), the load path must not raise."""
    with test_db() as db:
        disc = _mk_disc(db, label_draft={"primary_season": "junk"})
        form = _build_labelform_from_disc(disc, _disc_info_blank(), None, db)
        assert form["primary_season"] is None


def test_build_labelform_treats_zero_and_negative_primary_season_as_none(test_db):
    for v in (0, -1, -99):
        with test_db() as db:
            disc = _mk_disc(db, label_draft={"primary_season": v})
            form = _build_labelform_from_disc(disc, _disc_info_blank(), None, db)
            assert form["primary_season"] is None, f"v={v}"


def test_build_labelform_rejects_bool_disguised_as_int(test_db):
    """`True`/`False` are technically `isinstance(int)` in Python — guard
    against booleans masquerading as season numbers."""
    with test_db() as db:
        disc = _mk_disc(db, label_draft={"primary_season": True})
        form = _build_labelform_from_disc(disc, _disc_info_blank(), None, db)
        assert form["primary_season"] is None


# ── Job-scoped builder must surface primary_season (parity) ──────────────────


def test_build_labelform_from_job_surfaces_primary_season_from_label_draft(test_db):
    """The job workflow-context path uses `_build_labelform_from_job`, which
    must mirror the disc-scoped builder — otherwise the SPA reads the
    persisted value from disc-context endpoints but loses it from the job
    endpoint (the path actually exercised by the Ripper deep-link)."""
    from api.routers.jobs import _build_labelform_from_job

    with test_db() as db:
        disc = _mk_disc(db, label_draft={"primary_season": 2, "group_type": "series"})
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            mode="rip",
            disc_payload={},
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        form = _build_labelform_from_job(job)
        assert form["primary_season"] == 2


def test_build_labelform_from_job_returns_none_when_label_draft_has_no_primary_season(test_db):
    from api.routers.jobs import _build_labelform_from_job

    with test_db() as db:
        disc = _mk_disc(db, label_draft={"movie_id": "m1"})
        job = models.Job(
            id=str(uuid.uuid4()), disc_id=disc.id, disc_num="1", mount_point="/dev/sr0",
            mode="rip", disc_payload={},
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        form = _build_labelform_from_job(job)
        assert form["primary_season"] is None


def test_build_labelform_from_job_defends_against_garbage_primary_season(test_db):
    from api.routers.jobs import _build_labelform_from_job

    for bad in ("junk", 0, -1, True):
        with test_db() as db:
            disc = _mk_disc(db, label_draft={"primary_season": bad})
            job = models.Job(
                id=str(uuid.uuid4()), disc_id=disc.id, disc_num="1", mount_point="/dev/sr0",
                mode="rip", disc_payload={},
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            form = _build_labelform_from_job(job)
            assert form["primary_season"] is None, f"bad={bad!r}"
