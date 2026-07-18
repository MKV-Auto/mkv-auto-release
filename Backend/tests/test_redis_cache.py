"""Unit tests for core.redis_cache: get, set, invalidate, is_stale, _make_key with mocked Redis."""
import json
import pytest
from unittest.mock import MagicMock

from core import redis_cache


def test_make_key():
    assert redis_cache._make_key("a", "b") == "cache:a:b"


def test_get_returns_none_when_client_get_none(monkeypatch):
    client = MagicMock()
    client.get.return_value = None
    monkeypatch.setattr("core.redis_cache.get_redis_client", lambda: client)
    assert redis_cache.get("ns", "k") is None


def test_get_returns_parsed_dict_when_client_returns_json(monkeypatch):
    client = MagicMock()
    client.get.return_value = json.dumps({"x": 1})
    monkeypatch.setattr("core.redis_cache.get_redis_client", lambda: client)
    assert redis_cache.get("ns", "k") == {"x": 1}


def test_set_calls_setex_with_make_key(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr("core.redis_cache.get_redis_client", lambda: client)
    redis_cache.set("ns", "k", {"v": 1})
    calls = [c[0][0] for c in client.setex.call_args_list]
    assert "cache:ns:k" in calls


def test_invalidate_uses_keys_and_delete(monkeypatch):
    client = MagicMock()
    client.keys.return_value = ["cache:ns:k1", "cache:ns:k2"]
    client.delete.return_value = 2
    monkeypatch.setattr("core.redis_cache.get_redis_client", lambda: client)
    n = redis_cache.invalidate("ns", pattern=None)
    assert client.keys.called
    assert client.delete.called
    assert n == 2


def test_is_stale_true_when_stale_exists_and_cache_not(monkeypatch):
    client = MagicMock()
    client.exists.side_effect = [True, False]  # stale_key exists, cache_key does not
    monkeypatch.setattr("core.redis_cache.get_redis_client", lambda: client)
    assert redis_cache.is_stale("ns", "k") is True


def test_is_stale_false_when_cache_exists(monkeypatch):
    client = MagicMock()
    client.exists.side_effect = [True, True]  # stale exists, cache exists
    monkeypatch.setattr("core.redis_cache.get_redis_client", lambda: client)
    assert redis_cache.is_stale("ns", "k") is False


def test_get_returns_none_when_client_is_none(monkeypatch):
    monkeypatch.setattr("core.redis_cache.get_redis_client", lambda: None)
    assert redis_cache.get("ns", "k") is None
