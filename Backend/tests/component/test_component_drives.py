"""Component tests for /drives/ using mock_drive.

These were quarantined under #392 with `xfail(strict=False)` because the
readiness gate (added in #373/#490) ran a `SELECT 1` against an
unreachable production Postgres URL and 503'd every non-allowlisted
route. The component conftest's `client` fixture now pulls in `test_db`,
which monkeypatches `api.database.SessionLocal` to a SQLite engine the
gate's ping succeeds against. See `tests/component/conftest.py`.
"""


def test_drives_list(client, mock_drive):
    """GET /drives/drives returns 200 and [{\"disc_num\",\"mount_point\"}] matching mock_drive.drives."""
    response = client.get("/drives/drives")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    assert "disc_num" in data[0] and "mount_point" in data[0]
    assert data[0]["disc_num"] == "1"
    assert data[0]["mount_point"] == "/mnt/dvd"


def test_drives_discinfo(client, mock_drive):
    """GET /drives/discinfo with disc_num and mount_point (refresh=false) returns 200 and matches mock."""
    response = client.get(
        "/drives/discinfo",
        params={"disc_num": "1", "mount_point": "/mnt/dvd", "refresh": "false"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data.get("disc_num") == "1"
    assert data.get("mount_point") == "/mnt/dvd"
    assert "disc_hash" in data or "content_hash" in data


def test_drives_disc_eject(client, mock_drive):
    """POST /drives/disc/eject with disc_num returns 200; then GET /drives/discinfo for that disc is 404 or empty."""
    # Ensure cache has something for disc 1
    client.get("/drives/discinfo", params={"disc_num": "1", "mount_point": "/mnt/dvd", "refresh": "true"})
    response = client.post("/drives/disc/eject", params={"disc_num": "1"})
    assert response.status_code == 200
    data = response.json()
    assert data.get("status") == "ok"
    # After eject, cache is cleared; discinfo without refresh should 404
    get_resp = client.get(
        "/drives/discinfo",
        params={"disc_num": "1", "mount_point": "/mnt/dvd", "refresh": "false"},
    )
    assert get_resp.status_code == 404
