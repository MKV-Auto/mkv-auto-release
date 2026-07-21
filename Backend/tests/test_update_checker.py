"""Tests for core.update_checker (#699).

The checker must: compare semver correctly, cache per TTL, never raise on
network/parse failures, and never touch the network for dev builds.
"""

from unittest.mock import MagicMock, patch

import pytest

from core import update_checker


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    update_checker.reset_cache()
    monkeypatch.setenv("MKVAUTO_VERSION", "1.0.1")
    yield
    update_checker.reset_cache()


def _gh_response(tag: str, url: str = "https://github.com/MKV-Auto/mkv-auto-release/releases/tag/x") -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "tag_name": tag,
        "html_url": url,
        "name": f"MKV-Auto {tag.lstrip('v')}",
        "published_at": "2026-07-20T22:28:47Z",
    }
    return resp


def test_newer_release_flags_update():
    with patch.object(update_checker.requests, "get", return_value=_gh_response("v1.0.2")) as mock_get:
        status = update_checker.get_update_status()
    assert status["update_available"] is True
    assert status["current_version"] == "1.0.1"
    assert status["latest_version"] == "1.0.2"
    assert status["release_url"]
    mock_get.assert_called_once()


def test_same_release_is_not_an_update():
    with patch.object(update_checker.requests, "get", return_value=_gh_response("v1.0.1")):
        status = update_checker.get_update_status()
    assert status["update_available"] is False
    assert status["latest_version"] == "1.0.1"


def test_older_release_is_not_an_update():
    # e.g. a rollback of the published Release must not nag users ahead of it
    with patch.object(update_checker.requests, "get", return_value=_gh_response("v1.0.0")):
        status = update_checker.get_update_status()
    assert status["update_available"] is False


def test_semver_compare_is_numeric_not_lexicographic():
    with patch.object(update_checker.requests, "get", return_value=_gh_response("v1.0.10")):
        status = update_checker.get_update_status()
    assert status["update_available"] is True
    assert status["latest_version"] == "1.0.10"


def test_malformed_tag_degrades_to_no_info():
    with patch.object(update_checker.requests, "get", return_value=_gh_response("nightly-build")):
        status = update_checker.get_update_status()
    assert status["update_available"] is False
    assert status["latest_version"] is None


def test_network_error_degrades_to_no_info():
    with patch.object(update_checker.requests, "get", side_effect=OSError("no route to host")):
        status = update_checker.get_update_status()
    assert status["update_available"] is False
    assert status["latest_version"] is None
    assert status["current_version"] == "1.0.1"


def test_dev_build_never_touches_the_network(monkeypatch):
    monkeypatch.setenv("MKVAUTO_VERSION", "dev")
    with patch.object(update_checker.requests, "get") as mock_get:
        status = update_checker.get_update_status()
    mock_get.assert_not_called()
    assert status["update_available"] is False
    assert status["current_version"] == "dev"


def test_cache_prevents_repeat_fetch_within_ttl():
    with patch.object(update_checker.requests, "get", return_value=_gh_response("v1.0.2")) as mock_get:
        first = update_checker.get_update_status()
        second = update_checker.get_update_status()
    assert mock_get.call_count == 1
    assert second == first


def test_force_bypasses_cache():
    with patch.object(update_checker.requests, "get", return_value=_gh_response("v1.0.2")) as mock_get:
        update_checker.get_update_status()
        update_checker.get_update_status(force=True)
    assert mock_get.call_count == 2
