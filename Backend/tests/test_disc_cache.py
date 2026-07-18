"""Unit tests for core.disc_cache: get, set_payload, set, clear, clear_key."""
import concurrent.futures

import pytest

# Import after possible env/monkeypatch in conftest; we patch _persist and clear _cache in each test.


@pytest.fixture(autouse=True)
def _patch_and_clear(monkeypatch):
    """Patch _persist_unlocked to no-op, _find_makemkvcon to return no PID, and clear _cache."""
    import core.disc_cache as disc_cache

    monkeypatch.setattr("core.disc_cache._persist_unlocked", lambda: None)
    monkeypatch.setattr("core.utils._find_makemkvcon_process_for_disc", lambda k: (None, None))
    disc_cache.clear()
    yield
    disc_cache.clear()


def test_get_returns_none_when_empty():
    from core import disc_cache

    assert disc_cache.get("disc1") is None


def test_set_payload_then_get_by_disc_num():
    from core import disc_cache

    payload = {"disc_num": "1", "mount_point": "/mnt/dvd", "info_title": "Test"}
    disc_cache.set_payload("1", payload)
    assert disc_cache.get("1") == payload


def test_set_payload_stores_by_disc_hash_and_disc_id():
    from core import disc_cache

    payload = {
        "disc_num": "1",
        "disc_hash": "ABC123",
        "disc_id": "disc-uuid-456",
        "mount_point": "/mnt/dvd",
    }
    disc_cache.set_payload("1", payload)
    assert disc_cache.get("1") == payload
    assert disc_cache.get("ABC123") == payload
    assert disc_cache.get("disc-uuid-456") == payload


def test_set_payload_empty_does_not_store():
    from core import disc_cache

    disc_cache.set_payload("1", {})
    assert disc_cache.get("1") is None


def test_set_is_alias_of_set_payload():
    from core import disc_cache

    payload = {"disc_num": "2", "mount_point": "/mnt/sr1"}
    disc_cache.set("2", payload)
    assert disc_cache.get("2") == payload


def test_clear_empties_cache():
    from core import disc_cache

    disc_cache.set_payload("1", {"disc_num": "1"})
    assert disc_cache.get("1") is not None
    disc_cache.clear()
    assert disc_cache.get("1") is None


def test_clear_key_removes_entry_when_makemkvcon_not_running(monkeypatch):
    from core import disc_cache

    # _find_makemkvcon_process_for_disc patched to (None, None) in fixture
    disc_cache.set_payload("1", {"disc_num": "1", "mount_point": "/mnt/dvd"})
    assert disc_cache.get("1") is not None
    disc_cache.clear_key("1")
    assert disc_cache.get("1") is None


def test_clear_key_removes_all_associated_keys(monkeypatch):
    """Test that clear_key removes disc_num, disc_hash, and disc_id entries."""
    from core import disc_cache

    # Set up a payload with all three keys
    payload = {
        "disc_num": "1",
        "disc_hash": "abc123hash",
        "disc_id": "uuid-disc-456",
        "mount_point": "/mnt/dvd",
        "info_title": "Test Disc",
    }
    disc_cache.set_payload("1", payload)
    
    # Verify all three keys exist in cache
    assert disc_cache.get("1") == payload
    assert disc_cache.get("abc123hash") == payload
    assert disc_cache.get("uuid-disc-456") == payload
    
    # Clear by disc_num should remove all three
    disc_cache.clear_key("1")
    
    # Verify all three keys are removed
    assert disc_cache.get("1") is None
    assert disc_cache.get("abc123hash") is None
    assert disc_cache.get("uuid-disc-456") is None


def test_clear_key_by_disc_hash_removes_all_associated_keys(monkeypatch):
    """Test that clear_key can be called with disc_hash and still removes all keys."""
    from core import disc_cache

    # Set up a payload with all three keys
    payload = {
        "disc_num": "1",
        "disc_hash": "xyz789hash",
        "disc_id": "uuid-disc-789",
        "mount_point": "/mnt/dvd",
    }
    disc_cache.set_payload("1", payload)
    
    # Verify all three keys exist
    assert disc_cache.get("1") == payload
    assert disc_cache.get("xyz789hash") == payload
    assert disc_cache.get("uuid-disc-789") == payload
    
    # Clear by disc_hash should remove all three
    disc_cache.clear_key("xyz789hash")
    
    # Verify all three keys are removed
    assert disc_cache.get("1") is None
    assert disc_cache.get("xyz789hash") is None
    assert disc_cache.get("uuid-disc-789") is None


def test_concurrent_set_payload_distinct_disc_nums(monkeypatch):
    """Parallel startup rescans must not drop cache entries (RLock on _cache)."""
    from core import disc_cache

    monkeypatch.setattr("core.disc_cache._persist_unlocked", lambda: None)
    monkeypatch.setattr("core.utils._find_makemkvcon_process_for_disc", lambda k: (None, None))
    disc_cache.clear()

    def set_one(i: int) -> None:
        disc_cache.set_payload(
            str(i),
            {"disc_num": str(i), "mount_point": f"/dev/sr{i}", "tag": i},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(set_one, range(12)))

    for i in range(12):
        got = disc_cache.get(str(i))
        assert got is not None
        assert got.get("tag") == i

    disc_cache.clear()
