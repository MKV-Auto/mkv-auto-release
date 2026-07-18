import hashlib
import urllib.error
from unittest.mock import Mock

import pytest
import requests

from core import makemkv_updater


class _RunResult:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _patch_strings(monkeypatch, output: str):
    def fake_run(*_args, **_kwargs):
        return _RunResult(stdout=output, stderr="", returncode=0)

    monkeypatch.setattr(makemkv_updater.subprocess, "run", fake_run)
    monkeypatch.setattr(
        makemkv_updater.shutil,
        "which",
        lambda name: "/usr/bin/strings" if name == "strings" else None,
    )


def test_get_installed_version_selects_most_frequent(monkeypatch, tmp_path):
    binary = tmp_path / "makemkvcon"
    binary.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(makemkv_updater, "get_makemkvcon_path", lambda: str(binary))
    _patch_strings(monkeypatch, "v1.18.2 v1.1.0 v1.18.2 v1.18.2")

    assert makemkv_updater.get_installed_version() == "1.18.2"


def test_get_installed_version_tie_breaks_by_semver(monkeypatch, tmp_path):
    binary = tmp_path / "makemkvcon"
    binary.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(makemkv_updater, "get_makemkvcon_path", lambda: str(binary))
    _patch_strings(monkeypatch, "v1.18.1 v1.18.2")

    assert makemkv_updater.get_installed_version() == "1.18.2"


def test_get_installed_version_prefers_frequency_over_semver(monkeypatch, tmp_path):
    binary = tmp_path / "makemkvcon"
    binary.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(makemkv_updater, "get_makemkvcon_path", lambda: str(binary))
    _patch_strings(monkeypatch, "v1.18.2 v9.99.3 v1.18.2 v1.1.0")

    assert makemkv_updater.get_installed_version() == "1.18.2"


def test_get_installed_version_ignores_unprefixed_versions(monkeypatch, tmp_path):
    binary = tmp_path / "makemkvcon"
    binary.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(makemkv_updater, "get_makemkvcon_path", lambda: str(binary))
    _patch_strings(monkeypatch, "1.18.2 9.99.3 9.99.3 v1.18.2")

    assert makemkv_updater.get_installed_version() == "1.18.2"


def test_get_installed_version_returns_none_when_no_versions(monkeypatch, tmp_path):
    binary = tmp_path / "makemkvcon"
    binary.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(makemkv_updater, "get_makemkvcon_path", lambda: str(binary))
    _patch_strings(monkeypatch, "no version here")

    assert makemkv_updater.get_installed_version() is None


def test_get_makemkvcon_metadata_includes_hash_and_mtime(monkeypatch, tmp_path):
    binary = tmp_path / "makemkvcon"
    content = b"makemkv-binary"
    binary.write_bytes(content)
    monkeypatch.setattr(makemkv_updater, "get_makemkvcon_path", lambda: str(binary))

    meta = makemkv_updater.get_makemkvcon_metadata()

    assert meta["binary_path"] == str(binary)
    assert meta["resolved_path"] == str(binary.resolve())
    assert meta["binary_sha256"] == hashlib.sha256(content).hexdigest()
    assert meta["binary_mtime"] is not None


def test_download_http_403_appends_to_logs_and_raises_makemkv_update_error(
    monkeypatch, tmp_path
):
    """When _download gets HTTP 403, logs get failure lines and MakeMKVUpdateError is raised."""
    url = "https://www.makemkv.com/download/makemkv-bin-1.18.2.tar.gz"
    dest = tmp_path / "out.tar.gz"
    logs = []

    def urlopen_raising_403(_request_url, timeout=None):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(makemkv_updater.urllib.request, "urlopen", urlopen_raising_403)

    with pytest.raises(makemkv_updater.MakeMKVUpdateError) as excinfo:
        makemkv_updater._download(url, dest, logs)

    assert "403" in excinfo.value.args[0]
    assert "Forbidden" in excinfo.value.args[0]
    assert url in excinfo.value.args[0]
    assert any("403" in line or "Forbidden" in line for line in logs)
    assert any(url in line for line in logs)


def test_run_failure_logs_to_logger(monkeypatch, caplog):
    """When _run gets non-zero exit, log.error is called with command and stderr."""
    from unittest.mock import Mock

    logs = []
    cmd = ["false"]
    stderr_text = "failure message"

    # _run uses subprocess.Popen, not subprocess.run; mock Popen to return a process
    # that has already "exited" with returncode 1 and stderr content
    mock_stdout = Mock()
    mock_stdout.read.return_value = ""
    mock_stderr = Mock()
    mock_stderr.read.return_value = stderr_text
    mock_process = Mock()
    mock_process.poll.return_value = 1
    mock_process.stdout = mock_stdout
    mock_process.stderr = mock_stderr
    mock_process.returncode = 1

    def fake_popen(*args, **kwargs):
        return mock_process

    monkeypatch.setattr(makemkv_updater.subprocess, "Popen", fake_popen)

    with pytest.raises(makemkv_updater.MakeMKVUpdateError):
        makemkv_updater._run(cmd, logs=logs)

    err_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(err_records) >= 1
    err_msg = err_records[0].message
    assert "false" in err_msg
    assert "1" in err_msg
    assert "failure message" in err_msg


def test_fetch_latest_version_uses_wayback_when_primary_fails(monkeypatch):
    """When primary URLs timeout/fail, fetch_latest_version uses Wayback and returns version."""
    primary_calls = []

    def failing_get(url, timeout=None):
        primary_calls.append(url)
        raise requests.RequestException("timeout")

    monkeypatch.setattr(requests, "get", failing_get)

    # First two calls are primary (download page, forum); then we try Wayback.
    # Mock _fetch_versions_from_wayback to return a version so we don't need real archive.org
    def wayback_returns_versions():
        return ["1.18.3"]

    monkeypatch.setattr(
        makemkv_updater, "_fetch_versions_from_wayback", wayback_returns_versions
    )

    result = makemkv_updater.fetch_latest_version()
    assert result == "1.18.3"
    assert len(primary_calls) == 2  # both primary URLs tried


def test_fetch_latest_version_raises_when_primary_and_wayback_fail(monkeypatch):
    """When primary and Wayback both fail, MakeMKVUpdateError is raised."""
    def failing_get(url, timeout=None):
        raise requests.RequestException("timeout")

    monkeypatch.setattr(requests, "get", failing_get)

    def wayback_returns_empty():
        return []

    monkeypatch.setattr(
        makemkv_updater, "_fetch_versions_from_wayback", wayback_returns_empty
    )

    with pytest.raises(makemkv_updater.MakeMKVUpdateError) as excinfo:
        makemkv_updater.fetch_latest_version()
    assert "primary" in excinfo.value.args[0].lower() or "wayback" in excinfo.value.args[0].lower()


@pytest.mark.xfail(reason="staging baseline fail; tracked in #406", strict=True)
def test_download_with_fallback_uses_wayback_when_primary_fails(monkeypatch, tmp_path):
    """When primary download fails, _download_with_fallback tries Wayback and succeeds."""
    url = "https://www.makemkv.com/download/makemkv-bin-1.18.2.tar.gz"
    dest = tmp_path / "out.tar.gz"
    logs = []

    # First urlopen call (primary) raises; second call (wayback) returns readable content
    call_count = [0]

    def urlopen_side_effect(request, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            raise urllib.error.URLError("timeout")
        # Second call is for wayback URL: return a response that read() returns content then b""
        chunks = [b"fake-tarball-content", b""]

        class FakeResponse:
            def getheader(self, name):
                return None

            def read(self, size=-1):
                if chunks:
                    return chunks.pop(0)
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        return FakeResponse()

    monkeypatch.setattr(
        makemkv_updater.urllib.request, "urlopen", urlopen_side_effect
    )
    wayback_url = "https://web.archive.org/web/20200101000000id_/" + url
    monkeypatch.setattr(
        makemkv_updater, "_wayback_url_for", lambda u: wayback_url if u == url else None
    )

    makemkv_updater._download_with_fallback(url, dest, logs)

    assert dest.read_bytes() == b"fake-tarball-content"
    assert any("Wayback" in line for line in logs)


def test_download_with_fallback_raises_when_primary_and_wayback_fail(
    monkeypatch, tmp_path
):
    """When primary fails and no Wayback snapshot exists, MakeMKVUpdateError is raised."""
    url = "https://www.makemkv.com/download/makemkv-bin-1.18.2.tar.gz"
    dest = tmp_path / "out.tar.gz"
    logs = []

    def urlopen_fail(request, timeout=None):
        raise urllib.error.URLError("timeout")

    monkeypatch.setattr(makemkv_updater.urllib.request, "urlopen", urlopen_fail)
    monkeypatch.setattr(makemkv_updater, "_wayback_url_for", lambda u: None)

    with pytest.raises(makemkv_updater.MakeMKVUpdateError) as excinfo:
        makemkv_updater._download_with_fallback(url, dest, logs)
    assert "Wayback" in excinfo.value.args[0] or "Primary" in excinfo.value.args[0]
