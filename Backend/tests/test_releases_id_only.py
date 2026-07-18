import pytest
import uuid
from fastapi import HTTPException

from api import crud, models
from api.routers import releases
from api.schemas import DiscMetadataUpdate, DiscMetadataPatch, ReleaseMetadataPatch, TitleLabel


def _make_release(session, slug: str = "creed-bluray"):
    movie = models.Movie(name="Creed")
    session.add(movie)
    session.flush()
    rel = models.Release(slug=slug, type="movie", name="Creed", title="Creed", movie_id=movie.id)
    session.add(rel)
    session.commit()
    session.refresh(rel)
    return rel


def _make_disc(session, release, content_hash="HASH1"):
    disc = models.Disc(content_hash=content_hash, release_id=release.id, disc_number=1, disc_slug="disc-1")
    session.add(disc)
    session.commit()
    session.refresh(disc)
    return disc


def test_list_release_discs_uses_id_only_and_includes_release_id(test_db):
    with test_db() as session:
        rel = _make_release(session)
        disc = _make_disc(session, rel)

        # Create a latest job so DiscSummary can expose latest pipeline/phase.
        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/a",
            mode="copy",
            job_status="running",
            rip_state="completed",
            transfer_state="ready",
            rip_progress=100,
        )
        session.add(job)
        session.commit()

        discs = releases.list_release_discs(rel.id, db=session)
        assert len(discs) == 1
        assert discs[0].release_id == rel.id
        assert discs[0].release_slug == rel.slug
        assert discs[0].latest_pipeline is not None
        assert discs[0].latest_pipeline.get("transfer") == "ready"
        assert discs[0].latest_phase == "transfer"
        assert discs[0].latest_job_updated_at is not None

        # list_release_discs supports slug fallback (for Library page)
        discs_by_slug = releases.list_release_discs(rel.slug, db=session)
        assert len(discs_by_slug) == 1
        assert discs_by_slug[0].release_id == rel.id
        assert discs_by_slug[0].release_slug == rel.slug


def test_update_release_by_id_persists_changes(test_db):
    with test_db() as session:
        rel = _make_release(session)
        payload = ReleaseMetadataPatch(
            release_name="Creed Updated",
            release_slug="creed-updated",
            upc="12345",
        )
        updated = releases.update_release(rel.id, payload, db=session)
        assert updated.id == rel.id
        assert updated.slug == "creed-updated"
        assert updated.name == "Creed Updated"
        # id-only lookup still works after slug change
        fetched = releases.get_release(rel.id, db=session)
        assert fetched.slug == "creed-updated"


def test_update_disc_metadata_keeps_release_id_and_tracks(test_db):
    with test_db() as session:
        rel = _make_release(session)
        disc = _make_disc(session, rel, content_hash="HASH-T1")

        payload = DiscMetadataUpdate(
            release=ReleaseMetadataPatch(
                release_id=rel.id,
                release_slug=rel.slug,
                release_name="Creed",
                release_year=2015,
                original_year=2015,
            ),
            disc=DiscMetadataPatch(
                disc_number=1,
                disc_slug="disc-1",
                disc_name="Disc One",
                disc_format="Blu-Ray",
            ),
            tracks=[TitleLabel(track_id="00001.mpls", title="Main Feature")],
        )

        response = releases.update_disc_metadata(disc.id, payload, db=session)
        disc_summary = response["disc"]
        release_summary = response["release"]

        assert disc_summary.release_id == rel.id
        assert release_summary.id == rel.id
        stored_disc = session.get(models.Disc, disc.id)
        assert stored_disc.release_id == rel.id
        assert stored_disc.title_streams
        assert stored_disc.title_streams[0].title_id == stored_disc.titles[0].id


def test_update_disc_metadata_no_auto_create_release(test_db):
    """PATCH disc metadata with movie_id but no release_id does NOT create a release or set disc.release_id."""
    with test_db() as session:
        movie = models.Movie(name="No Auto Movie")
        session.add(movie)
        session.flush()
        disc = models.Disc(content_hash="HASH-NO-AUTO", release_id=None)
        session.add(disc)
        session.commit()
        session.refresh(disc)

        release_count_before = session.query(models.Release).count()

        payload = DiscMetadataUpdate(
            release=ReleaseMetadataPatch(movie_id=movie.id, release_name="No Auto"),
            disc=DiscMetadataPatch(disc_slug="disc-1"),
        )
        response = releases.update_disc_metadata(disc.id, payload, db=session)

        release_count_after = session.query(models.Release).count()
        session.refresh(disc)

        assert disc.release_id is None
        assert release_count_after == release_count_before
        assert response["disc"].release_id is None


def test_update_disc_metadata_boxset_before_link_ready_pending_release_no_upc(test_db):
    """
    One PATCH with release_id (standalone candidate missing UPC) + complete boxset_id must link.
    Regression: link-readiness was checked before add_release_to_boxset, causing 400 release_not_link_ready
    while the UI omits UPC when a boxset is selected (boxset is authoritative).
    """
    with test_db() as session:
        movie = models.Movie(
            name="Dune",
            production_year=2024,
            tmdb_id=f"tmdb-{uuid.uuid4().hex[:8]}",
        )
        session.add(movie)
        session.flush()

        boxset = models.Boxset(
            slug="dune-2film",
            name="Dune 2-Film Collection",
            year=2024,
            upc="012345678905",
            cover_front_url="https://example.com/boxset-cover.jpg",
        )
        session.add(boxset)
        session.flush()

        pending = models.Release(
            slug="pending-dune",
            type="movie",
            name="Dune",
            title="Dune",
            movie_id=movie.id,
            release_year=2024,
            cover_front_url="https://example.com/rel-cover.jpg",
            upc=None,
            boxset_id=None,
        )
        session.add(pending)
        session.flush()

        disc = models.Disc(
            content_hash=f"HASH-BOXSET-{uuid.uuid4().hex[:8]}",
            release_id=None,
            label_draft={"movie_id": movie.id, "group_type": "movie"},
            disc_info={
                "pending_release_id": pending.id,
                "release_missing_required_fields": ["upc"],
                "release_link_ready": False,
            },
        )
        session.add(disc)
        session.commit()
        session.refresh(disc)
        session.refresh(pending)

        from api import crud as crud_mod

        assert not crud_mod.release_link_ready(session, pending)

        payload = DiscMetadataUpdate(
            release=ReleaseMetadataPatch(
                release_id=pending.id,
                movie_id=movie.id,
                boxset_id=boxset.id,
                release_name="Dune",
                release_year=2024,
                group_type="movie",
            ),
            disc=DiscMetadataPatch(disc_slug="disc-1"),
        )

        response = releases.update_disc_metadata(disc.id, payload, db=session)
        session.refresh(disc)
        session.refresh(pending)

        assert response["disc"].release_id == pending.id
        assert pending.boxset_id == boxset.id
        assert pending.upc == boxset.upc
        assert crud_mod.release_link_ready(session, pending)
        info = disc.disc_info if isinstance(disc.disc_info, dict) else {}
        assert "pending_release_id" not in info
        assert info.get("release_link_ready") is not False


def test_update_disc_metadata_404_when_release_id_not_found(test_db):
    """PATCH disc metadata with release_id that does not exist returns 404."""
    import uuid
    with test_db() as session:
        movie = models.Movie(name="Movie")
        session.add(movie)
        session.flush()
        disc = models.Disc(content_hash="HASH-404", release_id=None)
        session.add(disc)
        session.commit()
        session.refresh(disc)

        fake_release_id = str(uuid.uuid4())
        payload = DiscMetadataUpdate(
            release=ReleaseMetadataPatch(release_id=fake_release_id, movie_id=movie.id),
            disc=DiscMetadataPatch(),
        )
        with pytest.raises(HTTPException) as exc_info:
            releases.update_disc_metadata(disc.id, payload, db=session)
        assert exc_info.value.status_code == 404
        assert "Release not found" in str(exc_info.value.detail)


def test_get_disc_by_hash_returns_titles_when_streams_are_lists(test_db):
    """#528 regression: a disc with persisted DiscTitle rows must not 500.

    Since #380/#500 `DiscSummary.titles` is the typed TitleSummary
    projection (keyed `title_id`, no `streams`); per-title streams live in
    `title_streams` rows. The endpoint used to dump DiscTitleRecord dicts
    keyed `id` and blow up TitleSummary validation."""
    with test_db() as session:
        rel = _make_release(session)
        disc = _make_disc(session, rel, content_hash="HASH-LIST-STREAMS")

        title_id = str(uuid.uuid4())
        disc.titles = [
            models.DiscTitle(
                id=title_id,
                disc_id=disc.id,
                title="Main Feature",
                streams=[{"codec": "h264"}],
            )
        ]
        session.add_all(disc.titles)
        session.flush()
        disc.title_streams = [
            models.TitleStream(
                disc_id=disc.id,
                title_id=title_id,
                title="Main Feature",
                stream_index=0,
                streams=[{"codec": "dts"}],
            )
        ]
        session.add(disc)
        session.commit()
        session.refresh(disc)

        resp = releases.get_disc_by_hash(content_hash=disc.content_hash, disc_id=None, db=session)
        disc_summary = resp["disc"]

        assert disc_summary.title_streams and len(disc_summary.title_streams) == 1
        assert isinstance(disc_summary.title_streams[0].get("streams"), list)
        assert disc_summary.titles and len(disc_summary.titles) == 1
        title = disc_summary.titles[0]
        assert title.title_id == title_id
        assert title.title == "Main Feature"
        # Persisted titles present → no scan-payload fallback.
        assert disc_summary.tracks is None


def test_get_disc_by_hash_scan_payload_fallback_goes_to_tracks(test_db):
    """#528: with no persisted titles, raw scan tracks from the latest job
    payload hydrate `tracks` (the documented disc_payload-tracks field) and
    never the typed `titles`."""
    with test_db() as session:
        rel = _make_release(session)
        disc = _make_disc(session, rel, content_hash="HASH-SCAN-FALLBACK")
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/bd",
            job_status="completed",
            rip_state="completed",
            disc_payload={
                "scan_tracks": {
                    "0": {"title": "Track 0", "duration": 10, "source_file": "00001.mpls"},
                }
            },
        )
        session.add(job)
        session.commit()

        resp = releases.get_disc_by_hash(content_hash=disc.content_hash, disc_id=None, db=session)
        disc_summary = resp["disc"]

        assert disc_summary.titles is None
        assert disc_summary.tracks and len(disc_summary.tracks) == 1
        assert disc_summary.tracks[0]["title"] == "Track 0"


def test_get_library_includes_release_name_and_movie_on_each_release(test_db):
    """GET /releases/library returns each release with release_name (edition name) and movie (with cover_url)."""
    with test_db() as session:
        movie = models.Movie(
            name="Creed",
            production_year=2015,
            cover_url="https://example.com/poster.jpg",
            cover_path="/local/creed.jpg",
        )
        session.add(movie)
        session.flush()
        rel = models.Release(
            slug="creed-4k-bluray",
            type="movie",
            name="4K UHD Edition",
            movie_id=movie.id,
        )
        session.add(rel)
        session.commit()
        session.refresh(rel)

        lib = releases.get_library(db=session)

        assert len(lib.releases) == 1
        r = lib.releases[0]
        assert r.release_name == "4K UHD Edition", "release_name must be the release edition name (rel.name)"
        assert r.name == "Creed", "name (display) must be movie name when movie is linked"
        assert r.movie is not None
        assert r.movie.name == "Creed"
        assert r.movie.cover_url == "https://example.com/poster.jpg"
        assert r.movie.cover_path == "/local/creed.jpg"


def test_get_or_create_release_backfills_movie_cover_when_null(test_db):
    """When creating or updating a release with cover_front_url and movie has no cover, movie.cover_url is set."""
    with test_db() as session:
        movie = models.Movie(name="No Cover Movie", tmdb_id="tmdb-nocover", cover_url=None)
        session.add(movie)
        session.flush()
        payload = {
            "movie_id": movie.id,
            "release_name": "2020 Edition",
            "release_year": 2020,
            "upc": "1234567890123",
            "cover_front_url": "https://thediscdb.com/images/no-cover-2020.jpg",
            "group_type": "movie",
        }
        rel = crud.get_or_create_release(session, payload, disc_hash=None)
        assert rel is not None
        session.refresh(movie)
        assert movie.cover_url == "https://thediscdb.com/images/no-cover-2020.jpg"
