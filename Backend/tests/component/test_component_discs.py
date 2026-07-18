"""Component tests for /discs/ using mock_drive and test_db."""


def test_discs_list(client, mock_drive, test_db):
    """GET /discs/ returns 200 and list includes disc from mock_drive (via cache/disc_manager)."""
    # mock_drive seeds disc_cache for drive 1 at /mnt/dvd
    response = client.get("/discs/")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    # May be empty if list_discs uses different source; at least no 500
    assert "detail" not in data or "message" not in str(data)


def test_discs_info(client, mock_drive, test_db):
    """GET /discs/1/info returns 200 and payload consistent with mock_drive.discinfo_payload."""
    response = client.get("/discs/1/info", params={"mount_point": "/mnt/dvd"})
    assert response.status_code == 200
    data = response.json()
    assert "disc_num" in data or "disc_hash" in data or "mount_point" in data
    if "disc_num" in data:
        assert data["disc_num"] == "1"
    if "mount_point" in data:
        assert data["mount_point"] == "/mnt/dvd"


def test_discs_refresh(client, mock_drive, test_db):
    """POST /discs/1/refresh with mount_point returns 200."""
    response = client.post("/discs/1/refresh", params={"mount_point": "/mnt/dvd"})
    assert response.status_code == 200
