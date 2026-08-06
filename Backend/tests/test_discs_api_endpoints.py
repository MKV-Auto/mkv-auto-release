"""
Tests for disc-centric API endpoints.
Tests the new disc-centric endpoints that use Disc Manager and persist to DB.
"""
import pytest
import uuid
from unittest.mock import Mock, patch, MagicMock
from fastapi.testclient import TestClient
from api import models, database
from api.main import app
from core.disc_manager import get_disc_info, refresh_disc_info, list_discs
from core.disc_cache import set_payload as cache_set


@pytest.fixture
def client(test_db, monkeypatch):
    """Create FastAPI TestClient with test database dependency override."""
    def override_get_db():
        try:
            session = test_db()
            yield session
        finally:
            session.close()
    
    from api.routers import discs
    app.dependency_overrides[database.get_db] = override_get_db
    if hasattr(discs, "get_db"):
        app.dependency_overrides[discs.get_db] = override_get_db
    
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_patch_disc_title_respects_title_seq(client, test_db):
    session = test_db()
    try:
        disc_id = str(uuid.uuid4())
        title_id = str(uuid.uuid4())
        disc = models.Disc(id=disc_id, content_hash="TESTHASH_SEQ")
        title = models.DiscTitle(id=title_id, disc_id=disc_id, title="Original")
        session.add(disc)
        session.add(title)
        session.commit()

        response = client.patch(
            f"/discs/{disc_id}/titles",
            json={"title_id": title_id, "title": "First", "title_seq": 1},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["result"]["success"] is True
        assert data["result"]["updated_title"]["title_seq"] == 1
        session.refresh(title)
        assert title.title == "First"
        assert title.title_seq == 1

        response = client.patch(
            f"/discs/{disc_id}/titles",
            json={"title_id": title_id, "title": "Stale", "title_seq": 0},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["result"]["success"] is False
        assert data["result"]["error_code"] == "stale_seq"
        session.refresh(title)
        assert title.title == "First"
        assert title.title_seq == 1
    finally:
        session.close()


def test_list_disc_titles_projects_title_seq(client, test_db):
    """#383: the lightweight title-list endpoint must include `title_seq`
    so the frontend can include it in PATCH payloads and detect stale_seq
    responses on concurrent edits. Without this, every PATCH starts at
    incoming_seq=None → current_seq+1 and silently wins, hiding conflicts."""
    session = test_db()
    try:
        disc_id = str(uuid.uuid4())
        disc = models.Disc(id=disc_id, content_hash="TESTHASH_LISTSEQ")
        session.add(disc)
        # Two titles, one bumped to seq=3 (post-edit), one at the default 0.
        edited = models.DiscTitle(id=str(uuid.uuid4()), disc_id=disc_id, title="Edited", title_seq=3)
        fresh = models.DiscTitle(id=str(uuid.uuid4()), disc_id=disc_id, title="Fresh", title_seq=0)
        session.add_all([edited, fresh])
        session.commit()

        # Summary mode (default).
        response = client.get(f"/discs/{disc_id}/titles")
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert len(items) == 2
        by_title = {item["title"]: item for item in items}
        assert by_title["Edited"]["title_seq"] == 3
        assert by_title["Fresh"]["title_seq"] == 0

        # Full mode — still has title_seq (it lives outside the summary/full gate).
        response = client.get(f"/discs/{disc_id}/titles?detail=full")
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        by_title = {item["title"]: item for item in items}
        assert by_title["Edited"]["title_seq"] == 3
        assert by_title["Fresh"]["title_seq"] == 0
    finally:
        session.close()


@pytest.fixture
def mock_disc_manager(monkeypatch):
    """Mock Disc Manager functions."""
    def mock_get_disc_info(disc_num, mount_point, refresh=False):
        return {
            "disc_num": disc_num,
            "mount_point": mount_point,
            "disc_hash": "TESTHASH123",
            "movie_name": "Test Movie",
            "disc_format": "Blu-Ray",
            "resolution": "1080p",
            "tracks": {"00001.mpls": {"type": "MainFeature"}},
            "discdb_hit": True,
            "label_required": False,
        }
    
    def mock_refresh_disc_info(disc_num, mount_point):
        return mock_get_disc_info(disc_num, mount_point, refresh=True)
    
    def mock_list_discs():
        return [
            {
                "disc_num": "1",
                "mount_point": "/mnt/sr1",
                "disc_hash": "TESTHASH123",
                "movie_name": "Test Movie",
            }
        ]
    
    monkeypatch.setattr("api.routers.discs.get_disc_info", mock_get_disc_info)
    monkeypatch.setattr("api.routers.discs.refresh_disc_info", mock_refresh_disc_info)
    monkeypatch.setattr("api.routers.discs.list_discs", mock_list_discs)
    # Ensure cache miss so get_disc_info is called and returns our mock (with movie_name)
    monkeypatch.setattr("api.routers.discs.cache_get", lambda key: None)

    return {
        "get_disc_info": mock_get_disc_info,
        "refresh_disc_info": mock_refresh_disc_info,
        "list_discs": mock_list_discs,
    }


class TestListAllDiscs:
    """Tests for GET /discs endpoint."""
    
    def test_list_all_discs(self, client, mock_disc_manager, test_db):
        """Test listing all discs."""
        response = client.get("/discs/")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["disc_num"] == "1"
        assert data[0]["mount_point"] == "/mnt/sr1"
        assert data[0]["movie_name"] == "Test Movie"
    
    def test_list_all_discs_enriches_with_db(self, client, mock_disc_manager, test_db):
        """Test listing discs enriches with database data."""
        # Create a disc record in DB
        session = test_db()
        try:
            movie = models.Movie(id=str(uuid.uuid4()), name="Test Movie")
            release = models.Release(
                id=str(uuid.uuid4()),
                slug="test-movie",
                type="movie",
                name="Test Movie",
                movie_id=movie.id,
                # release_link_ready fields — without these the scan persistence
                # path (persist_disc_scan_with_discdb) refuses to keep disc→release
                # linked, and the enrichment assertions below fail.
                release_year=2020,
                upc="012345678901",
                cover_front_url="https://example.com/cover.jpg",
            )
            session.add(movie)
            session.add(release)
            session.commit()
            
            disc = models.Disc(
                id=str(uuid.uuid4()),
                content_hash="TESTHASH123",
                release_id=release.id,
                disc_number=1,
                disc_slug="test-movie",
            )
            session.add(disc)
            session.commit()
        finally:
            session.close()
        
        response = client.get("/discs/")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        # Should have DB-enriched fields
        assert "disc_id" in data[0]
        assert data[0]["disc_number"] == 1
        assert "release_id" in data[0]


class TestGetDiscInfo:
    """Tests for GET /discs/{disc_num}/info endpoint."""
    
    def test_get_disc_info(self, client, mock_disc_manager, test_db):
        """Test getting disc info."""
        response = client.get("/discs/1/info", params={"mount_point": "/mnt/sr1"})
        assert response.status_code == 200
        data = response.json()
        
        assert data["disc_num"] == "1"
        assert data["mount_point"] == "/mnt/sr1"
        assert data["disc_hash"] == "TESTHASH123"
        assert data["movie_name"] == "Test Movie"
    
    def test_get_disc_info_persists_to_db(self, client, mock_disc_manager, test_db):
        """Test getting disc info persists to database."""
        response = client.get("/discs/1/info", params={"mount_point": "/mnt/sr1"})
        assert response.status_code == 200
        
        # Verify disc was created in DB
        session = test_db()
        try:
            disc = session.query(models.Disc).filter(models.Disc.content_hash == "TESTHASH123").first()
            assert disc is not None
            assert disc.content_hash == "TESTHASH123"
        finally:
            session.close()
    
    def test_get_disc_info_enriches_with_db_data(self, client, mock_disc_manager, test_db):
        """Test getting disc info enriches with database data."""
        # Create disc record first
        session = test_db()
        try:
            movie = models.Movie(id=str(uuid.uuid4()), name="Test Movie")
            release = models.Release(
                id=str(uuid.uuid4()),
                slug="test-movie",
                type="movie",
                name="Test Movie",
                movie_id=movie.id,
                # release_link_ready fields — without these the scan persistence
                # path (persist_disc_scan_with_discdb) refuses to keep disc→release
                # linked, and the enrichment assertions below fail.
                release_year=2020,
                upc="012345678901",
                cover_front_url="https://example.com/cover.jpg",
            )
            session.add(movie)
            session.add(release)
            session.commit()
            
            disc = models.Disc(
                id=str(uuid.uuid4()),
                content_hash="TESTHASH123",
                release_id=release.id,
                disc_number=1,
            )
            session.add(disc)
            session.commit()
        finally:
            session.close()
        
        response = client.get("/discs/1/info", params={"mount_point": "/mnt/sr1"})
        assert response.status_code == 200
        data = response.json()
        
        # Should have DB-enriched fields
        assert "disc_id" in data
        assert data["disc_number"] == 1
        assert "release_id" in data
    
    def test_get_disc_info_not_found(self, client, mock_disc_manager):
        """Test getting disc info when not found."""
        def mock_get_disc_info_not_found(disc_num, mount_point, refresh=False):
            from core.drive_manager_client import DriveManagerError
            raise DriveManagerError("Not found", status_code=404)
        
        with patch("api.routers.discs._get_disc_info_from_cache_or_scan", return_value=None):
            response = client.get("/discs/1/info", params={"mount_point": "/mnt/sr1"})
            assert response.status_code in [404, 500]


class TestRefreshDiscInfo:
    """Tests for POST /discs/{disc_num}/refresh endpoint."""
    
    def test_refresh_disc_info(self, client, mock_disc_manager, test_db):
        """Test refreshing disc info."""
        response = client.post("/discs/1/refresh", params={"mount_point": "/mnt/sr1"})
        assert response.status_code == 200
        data = response.json()
        
        assert data["disc_num"] == "1"
        assert data["disc_hash"] == "TESTHASH123"
    
    def test_refresh_disc_info_persists_to_db(self, client, mock_disc_manager, test_db):
        """Test refreshing disc info persists to database."""
        response = client.post("/discs/1/refresh", params={"mount_point": "/mnt/sr1"})
        assert response.status_code == 200
        
        # Verify disc was created/updated in DB
        session = test_db()
        try:
            disc = session.query(models.Disc).filter(models.Disc.content_hash == "TESTHASH123").first()
            assert disc is not None
        finally:
            session.close()
    
    def test_refresh_disc_info_with_active_operation(self, client, mock_disc_manager):
        """Test refreshing disc info when operation is active."""
        def mock_refresh_raises(disc_num, mount_point):
            from core.drive_manager_client import DriveManagerError
            raise DriveManagerError("Operation active", status_code=409)
        
        with patch("api.routers.discs.refresh_disc_info", mock_refresh_raises):
            response = client.post("/discs/1/refresh", params={"mount_point": "/mnt/sr1"})
            assert response.status_code == 409


class TestDiscInfoIncludesDriveInfo:
    """Tests that disc info responses include drive information."""
    
    def test_disc_info_includes_drive_info(self, client, mock_disc_manager, test_db):
        """Test disc info includes drive information."""
        response = client.get("/discs/1/info", params={"mount_point": "/mnt/sr1"})
        assert response.status_code == 200
        data = response.json()
        
        # Should include drive info for UI drive selection
        assert "disc_num" in data
        assert "mount_point" in data
        assert data["disc_num"] == "1"
        assert data["mount_point"] == "/mnt/sr1"
    
    def test_list_discs_includes_drive_info(self, client, mock_disc_manager, test_db):
        """Test list discs includes drive information."""
        response = client.get("/discs/")
        assert response.status_code == 200
        data = response.json()

        assert len(data) > 0
        for disc in data:
            assert "disc_num" in disc
            assert "mount_point" in disc


class TestPatchSegmentFlag:
    """PATCH /discs/{disc_id}/segment-flags — per-disc clip obfuscation flags."""

    @pytest.fixture
    def _disc_id(self, test_db):
        session = test_db()
        try:
            did = str(uuid.uuid4())
            session.add(models.Disc(id=did, content_hash=f"HASH-{did[:8]}"))
            session.commit()
            return did
        finally:
            session.close()

    def test_set_definitely_flag(self, client, test_db, _disc_id):
        resp = client.patch(
            f"/discs/{_disc_id}/segment-flags",
            json={"clip_id": "3113", "flag": "definitely"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"disc_id": _disc_id, "flags": {"3113": "definitely"}}

    def test_set_potentially_flag(self, client, test_db, _disc_id):
        resp = client.patch(
            f"/discs/{_disc_id}/segment-flags",
            json={"clip_id": "504", "flag": "potentially"},
        )
        assert resp.status_code == 200
        assert resp.json()["flags"] == {"504": "potentially"}

    def test_clear_flag_with_null(self, client, test_db, _disc_id):
        # Set then clear.
        client.patch(
            f"/discs/{_disc_id}/segment-flags",
            json={"clip_id": "X", "flag": "definitely"},
        )
        resp = client.patch(
            f"/discs/{_disc_id}/segment-flags",
            json={"clip_id": "X", "flag": None},
        )
        assert resp.status_code == 200
        assert resp.json()["flags"] == {}

    def test_multiple_clips_accumulate(self, client, test_db, _disc_id):
        client.patch(
            f"/discs/{_disc_id}/segment-flags",
            json={"clip_id": "X", "flag": "definitely"},
        )
        resp = client.patch(
            f"/discs/{_disc_id}/segment-flags",
            json={"clip_id": "Y", "flag": "potentially"},
        )
        assert resp.status_code == 200
        assert resp.json()["flags"] == {"X": "definitely", "Y": "potentially"}

    def test_unknown_disc_returns_404(self, client, test_db):
        resp = client.patch(
            f"/discs/{uuid.uuid4()}/segment-flags",
            json={"clip_id": "1", "flag": "definitely"},
        )
        assert resp.status_code == 404

    def test_invalid_flag_value_returns_422(self, client, test_db, _disc_id):
        resp = client.patch(
            f"/discs/{_disc_id}/segment-flags",
            json={"clip_id": "1", "flag": "maybe"},
        )
        assert resp.status_code == 422

    def test_get_returns_empty_dict_when_no_flags_set(self, client, test_db, _disc_id):
        resp = client.get(f"/discs/{_disc_id}/segment-flags")
        assert resp.status_code == 200
        assert resp.json() == {"disc_id": _disc_id, "flags": {}}

    def test_get_returns_flags_set_via_patch(self, client, test_db, _disc_id):
        client.patch(
            f"/discs/{_disc_id}/segment-flags",
            json={"clip_id": "504", "flag": "potentially"},
        )
        client.patch(
            f"/discs/{_disc_id}/segment-flags",
            json={"clip_id": "3113", "flag": "definitely"},
        )
        resp = client.get(f"/discs/{_disc_id}/segment-flags")
        assert resp.status_code == 200
        assert resp.json()["flags"] == {"504": "potentially", "3113": "definitely"}

    def test_get_unknown_disc_returns_404(self, client, test_db):
        resp = client.get(f"/discs/{uuid.uuid4()}/segment-flags")
        assert resp.status_code == 404


class TestRemainingPlaylistSize:
    """GET /discs/{disc_id}/remaining-playlist-size — disk-pressure snapshot."""

    @pytest.fixture
    def _disc_with_titles(self, test_db):
        from api import models as m
        session = test_db()
        try:
            did = str(uuid.uuid4())
            session.add(m.Disc(id=did, content_hash=f"H-{did[:8]}"))
            session.flush()
            for idx, src, sz, type_ in [
                (0, "00001.mpls", 1_000_000_000, None),
                (1, "00002.mpls", 1_000_000_000, "ignore"),  # excluded
                (2, "00003.mpls", 2_000_000_000, None),
            ]:
                session.add(m.DiscTitle(
                    id=str(uuid.uuid4()),
                    disc_id=did,
                    index=idx,
                    source_file=src,
                    size=sz,
                    type=type_,
                ))
            session.commit()
            return did
        finally:
            session.close()

    def test_returns_remaining_excluding_ignored(self, client, _disc_with_titles):
        resp = client.get(f"/discs/{_disc_with_titles}/remaining-playlist-size")
        assert resp.status_code == 200
        body = resp.json()
        assert body["disc_id"] == _disc_with_titles
        assert body["total_count"] == 3
        assert body["ignored_count"] == 1
        # remaining = 1 GB (idx 0) + 2 GB (idx 2); 1 GB ignored.
        assert body["remaining_size_b"] == 3_000_000_000
        assert body["total_size_b"] == 4_000_000_000

    def test_allows_rip_rest_when_under_threshold(
        self, client, _disc_with_titles, monkeypatch
    ):
        # Bump the threshold above remaining-size so the CTA is unlocked.
        from core import segment_reorder
        monkeypatch.setattr(
            segment_reorder, "RIP_THE_REST_HARD_CAP_BYTES", 100_000_000_000,
        )
        resp = client.get(f"/discs/{_disc_with_titles}/remaining-playlist-size")
        assert resp.json()["allows_rip_rest"] is True

    def test_disallows_rip_rest_when_over_threshold(
        self, client, _disc_with_titles, monkeypatch
    ):
        from core import segment_reorder
        monkeypatch.setattr(
            segment_reorder, "RIP_THE_REST_HARD_CAP_BYTES", 1_000_000,
        )
        resp = client.get(f"/discs/{_disc_with_titles}/remaining-playlist-size")
        assert resp.json()["allows_rip_rest"] is False

    def test_404_for_unknown_disc(self, client):
        resp = client.get(f"/discs/{uuid.uuid4()}/remaining-playlist-size")
        assert resp.status_code == 404



def test_patch_reports_titles_the_group_sync_modified(client, test_db):
    """#775 contract, re-anchored by redesign area 2: when the sweep runs,
    the response must name every row it touched (a client that isn't told
    holds a stale seq cache; its next sibling edit conflicts and the
    recovery wipes the form). Area 2 narrows WHEN it runs: only
    group-shaping writes — a `type` patch — trigger the sweep. Text-only
    patches are single-row writes: no sweep, no sibling churn, and that
    is pinned here too."""
    session = test_db()
    try:
        disc_id = str(uuid.uuid4())
        primary_id = str(uuid.uuid4())
        sibling_id = str(uuid.uuid4())
        sm = "100,200,300"
        session.add(models.Disc(id=disc_id, content_hash="TESTHASH_775"))
        session.add_all([
            models.DiscTitle(
                id=primary_id, disc_id=disc_id, source_file="a.mkv",
                segment_map=sm, type="movie", title="Primary",
                active=True, order_index=0,
            ),
            models.DiscTitle(
                id=sibling_id, disc_id=disc_id, source_file="b.mkv",
                segment_map=sm, type="episode", title="Sibling",
                season=2, episode=5, active=False, order_index=1, title_seq=0,
            ),
        ])
        session.commit()

        # Area 2: a TEXT-ONLY patch is not group-shaping — no sweep, the
        # sibling is untouched, and the response carries no synced noise.
        response = client.patch(
            f"/discs/{disc_id}/titles",
            json={"title_id": primary_id, "title": "Primary renamed", "title_seq": 1},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["result"]["success"] is True
        assert not data.get("synced_titles")
        untouched = session.get(models.DiscTitle, sibling_id)
        session.refresh(untouched)
        assert untouched.title_seq == 0, "text patches must not churn sibling seqs"
        assert untouched.title == "Sibling"

        # A TYPE patch shapes groups: the sweep runs, the sibling gets
        # demoted + seq-bumped, and the response reports it.
        response = client.patch(
            f"/discs/{disc_id}/titles",
            json={"title_id": primary_id, "type": "MainMovie", "title_seq": 2},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["result"]["success"] is True

        synced = data.get("synced_titles") or []
        synced_ids = {t["title_id"] for t in synced}
        assert sibling_id in synced_ids, \
            "the demoted sibling must be reported so the client can update its seq cache"
        # The patched title itself is already in result.updated_title.
        assert primary_id not in synced_ids

        sibling_row = next(t for t in synced if t["title_id"] == sibling_id)
        session.refresh(session.get(models.DiscTitle, sibling_id))
        db_sibling = session.get(models.DiscTitle, sibling_id)
        assert sibling_row["title_seq"] == db_sibling.title_seq
        assert sibling_row["title_seq"] >= 1, "seq bump must be visible to the client"

        # A quiet disc (sync changes nothing on the second identical pass)
        # reports nothing — no noise for the client to churn on.
        response2 = client.patch(
            f"/discs/{disc_id}/titles",
            json={"title_id": primary_id, "type": "MainMovie", "title_seq": 3},
        )
        assert response2.status_code == 200
        assert not response2.json().get("synced_titles")
    finally:
        session.close()


def test_batch_patch_reports_synced_titles_once(client, test_db):
    """#775, batch flavor: one sync pass after the loop, one synced_titles
    list — rows already present as per-patch results are not repeated."""
    session = test_db()
    try:
        disc_id = str(uuid.uuid4())
        primary_id = str(uuid.uuid4())
        sibling_id = str(uuid.uuid4())
        sm = "111,222"
        session.add(models.Disc(id=disc_id, content_hash="TESTHASH_775B"))
        session.add_all([
            models.DiscTitle(
                id=primary_id, disc_id=disc_id, source_file="a.mkv",
                segment_map=sm, type="movie", title="P",
                active=True, order_index=0,
            ),
            models.DiscTitle(
                id=sibling_id, disc_id=disc_id, source_file="b.mkv",
                segment_map=sm, type="episode", title="S",
                active=False, order_index=1, title_seq=0,
            ),
        ])
        session.commit()

        # Area 2: the batch sweeps only when some patch in it wrote `type`.
        response = client.patch(
            f"/discs/{disc_id}/titles/batch",
            json={"patches": [
                {"title_id": primary_id, "title": "P2", "type": "MainMovie", "title_seq": 1},
            ]},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["results"][0]["success"] is True

        synced_ids = {t["title_id"] for t in (data.get("synced_titles") or [])}
        assert sibling_id in synced_ids
        assert primary_id not in synced_ids

        # Text-only batch: no sweep, no synced noise.
        response2 = client.patch(
            f"/discs/{disc_id}/titles/batch",
            json={"patches": [
                {"title_id": primary_id, "title": "P3", "title_seq": 2},
            ]},
        )
        assert response2.status_code == 200, response2.text
        assert response2.json()["results"][0]["success"] is True
        assert not response2.json().get("synced_titles")
    finally:
        session.close()


def test_base_seq_is_if_match_and_server_assigns_the_next_version(client, test_db):
    """#778 stage 2: the client sends the version it READ; the server compares
    and assigns the next one. The client never computes a version, so it can
    never guess wrong — which is what turned an unobserved background write
    into a bogus conflict."""
    session = test_db()
    try:
        disc_id = str(uuid.uuid4())
        title_id = str(uuid.uuid4())
        session.add(models.Disc(id=disc_id, content_hash="BASESEQ_1"))
        session.add(models.DiscTitle(id=title_id, disc_id=disc_id, title="Orig", title_seq=7))
        session.commit()

        r = client.patch(f"/discs/{disc_id}/titles",
                         json={"title_id": title_id, "title": "New", "base_seq": 7})
        assert r.status_code == 200, r.text
        result = r.json()["result"]
        assert result["success"] is True
        # Server-assigned, not client-supplied.
        assert result["updated_title"]["title_seq"] == 8
        session.refresh(session.get(models.DiscTitle, title_id))
        assert session.get(models.DiscTitle, title_id).title == "New"
    finally:
        session.close()


def test_base_seq_mismatch_returns_the_current_row_for_in_place_reconcile(client, test_db):
    """A conflict must hand back the row as it now is. Without it the client
    refetched every title on the disc, which is how one conflict wiped a whole
    label form (#775/#778)."""
    session = test_db()
    try:
        disc_id = str(uuid.uuid4())
        title_id = str(uuid.uuid4())
        session.add(models.Disc(id=disc_id, content_hash="BASESEQ_2"))
        session.add(models.DiscTitle(id=title_id, disc_id=disc_id, title="Winner", title_seq=9))
        session.commit()

        # Client read version 4; the row has since moved to 9.
        r = client.patch(f"/discs/{disc_id}/titles",
                         json={"title_id": title_id, "title": "Loser", "base_seq": 4})
        assert r.status_code == 200
        result = r.json()["result"]
        assert result["success"] is False
        assert result["error_code"] == "stale_seq"
        assert result["current_title"]["title"] == "Winner"
        assert result["current_title"]["title_seq"] == 9
        session.refresh(session.get(models.DiscTitle, title_id))
        assert session.get(models.DiscTitle, title_id).title == "Winner"
    finally:
        session.close()


def test_base_seq_ahead_of_the_server_is_also_a_conflict(client, test_db):
    """A client holding a NEWER version than the server has read something we
    never wrote. Treating that as 'close enough' would let it overwrite state
    it cannot have seen."""
    session = test_db()
    try:
        disc_id = str(uuid.uuid4())
        title_id = str(uuid.uuid4())
        session.add(models.Disc(id=disc_id, content_hash="BASESEQ_3"))
        session.add(models.DiscTitle(id=title_id, disc_id=disc_id, title="Server", title_seq=2))
        session.commit()

        r = client.patch(f"/discs/{disc_id}/titles",
                         json={"title_id": title_id, "title": "FromFuture", "base_seq": 5})
        assert r.json()["result"]["error_code"] == "stale_seq"
        session.refresh(session.get(models.DiscTitle, title_id))
        assert session.get(models.DiscTitle, title_id).title == "Server"
    finally:
        session.close()


def test_legacy_title_seq_clients_still_work(client, test_db):
    """Older clients send the version they claim to write. Backward compatible."""
    session = test_db()
    try:
        disc_id = str(uuid.uuid4())
        title_id = str(uuid.uuid4())
        session.add(models.Disc(id=disc_id, content_hash="BASESEQ_4"))
        session.add(models.DiscTitle(id=title_id, disc_id=disc_id, title="Orig", title_seq=3))
        session.commit()

        ok = client.patch(f"/discs/{disc_id}/titles",
                          json={"title_id": title_id, "title": "Legacy", "title_seq": 4})
        assert ok.json()["result"]["success"] is True
        stale = client.patch(f"/discs/{disc_id}/titles",
                             json={"title_id": title_id, "title": "Old", "title_seq": 2})
        assert stale.json()["result"]["error_code"] == "stale_seq"
        # Legacy conflicts get the current row too.
        assert stale.json()["result"]["current_title"]["title"] == "Legacy"
    finally:
        session.close()


# ── Title-state redesign area 1: field provenance ──────────────────────────

def test_set_title_field_resolution_user_wins_and_retraction_falls_back():
    """resolved = user ?? auto, for every provenanced field; a user write of
    None retracts the user value and automation's opinion shows through
    (matching user_type semantics)."""
    from api.crud import set_title_field
    from api import models

    t = models.DiscTitle(id="t1", disc_id="d1")
    set_title_field(t, "title", "Scanned Name", source="auto")
    assert t.title == "Scanned Name" and t.auto_title == "Scanned Name" and t.user_title is None

    set_title_field(t, "title", "Hand Typed", source="user")
    assert t.title == "Hand Typed" and t.user_title == "Hand Typed"
    assert t.auto_title == "Scanned Name"  # auto opinion preserved, not clobbered

    # Automation writing under a user value updates auto only — the
    # collision class behind #775/#778 is impossible by construction.
    set_title_field(t, "season", 3, source="user")
    set_title_field(t, "season", 1, source="auto")
    assert t.season == 3 and t.user_season == 3 and t.auto_season == 1

    # User retraction: resolved falls back to automation's answer.
    set_title_field(t, "title", None, source="user")
    assert t.user_title is None and t.title == "Scanned Name"


def test_patch_sets_user_provenance_and_response_carries_it(client, test_db):
    """A UI PATCH is a user write: user_* owns the value, auto_* is
    untouched, and the patch echo serializes the split."""
    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4()}")
        title = models.DiscTitle(
            id=str(uuid.uuid4()), disc_id=disc.id, source_file="00001.mpls",
            title="Auto Name", auto_title="Auto Name",
        )
        session.add_all([disc, title])
        session.commit()

        response = client.patch(
            f"/discs/{disc.id}/titles",
            json={"title_id": title.id, "title": "User Name", "season": 2, "base_seq": 0},
        )
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["success"] is True
        updated = result["updated_title"]
        assert updated["title"] == "User Name"
        assert updated["user_title"] == "User Name"
        assert updated["auto_title"] == "Auto Name"
        assert updated["user_season"] == 2

        session.refresh(title)
        assert title.user_title == "User Name"
        assert title.auto_title == "Auto Name"
        assert title.title == "User Name"
    finally:
        session.close()


def test_discdb_overlay_cannot_overwrite_user_labels(client, test_db):
    """The DiscDB hit overlay is automation: it writes auto_* only, so a
    re-lookup can no longer clobber a hand-typed label (the #775/#778
    collision class, removed by construction)."""
    from api.crud import _apply_discdb_metadata_to_titles

    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4()}")
        labeled = models.DiscTitle(
            id=str(uuid.uuid4()), disc_id=disc.id, source_file="00800.mpls",
            title="My Episode Name", user_title="My Episode Name", user_type="Episode",
            type="Episode",
        )
        unlabeled = models.DiscTitle(
            id=str(uuid.uuid4()), disc_id=disc.id, source_file="00801.mpls",
        )
        session.add_all([disc, labeled, unlabeled])
        session.commit()
        session.refresh(disc)

        _apply_discdb_metadata_to_titles(disc, {
            "00800.mpls": {"type": "Episode", "episode_name": "DiscDB Name A", "season": 1, "episode": 1},
            "00801.mpls": {"type": "Episode", "episode_name": "DiscDB Name B", "season": 1, "episode": 2},
        })
        session.commit()
        session.refresh(labeled); session.refresh(unlabeled)

        # User-labeled row: resolved keeps the human's value; the DiscDB
        # opinion is recorded in auto_* for the chip system.
        assert labeled.title == "My Episode Name"
        assert labeled.auto_title == "DiscDB Name A"
        # Untouched row: automation's value resolves normally.
        assert unlabeled.title == "DiscDB Name B"
        assert unlabeled.auto_title == "DiscDB Name B"
        assert unlabeled.user_title is None
    finally:
        session.close()
