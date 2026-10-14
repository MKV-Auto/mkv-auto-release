import os
import sys
from pathlib import Path
import pytest

# Ensure project root is importable for api/core modules.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Re-export backend fixtures
from tests.conftest_backend import *  # noqa: F401,F403

# Re-export e2e fixtures
from tests.conftest_e2e import *  # noqa: F401,F403


@pytest.fixture(autouse=True)
def _isolated_dv7_journal(monkeypatch, tmp_path_factory):
    """DV7 conversions journal their file swaps under MKVAUTO_ROOT (#902);
    keep every test's journal out of the real one."""
    monkeypatch.setenv("MKVAUTO_DV7_JOURNAL_DIR", str(tmp_path_factory.mktemp("dv7-journal")))


def pytest_configure(config):
    """Register custom marks."""
    # Rip verification: test fixtures use non-ffprobe-valid byte blobs; keep quiescence short.
    os.environ.setdefault("MKVAUTO_RIP_VERIFY_SKIP_FFPROBE", "1")
    os.environ.setdefault("MKVAUTO_RIP_QUIESCENCE_STABLE_SECONDS", "1")
    os.environ.setdefault("MKVAUTO_RIP_SHORT_INTERVAL_SECONDS", "1")
    # Incomplete-rip wait uses SHORT_STABLE_SECONDS (default 600s in production); keep tests fast.
    os.environ.setdefault("MKVAUTO_RIP_SHORT_STABLE_SECONDS", "2")
    config.addinivalue_line(
        "markers",
        "requires_uds: run only when UDS server is available (excluded from default run)",
    )
    config.addinivalue_line(
        "markers",
        "integration: stack integration tests (API+workers+DB, three mocks; no network). Run with: pytest -m integration",
    )


def pytest_addoption(parser):
    """Add custom pytest options."""
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="include tests that require network access (e.g. TMDB scraper). For stack integration (API+workers+DB, three mocks), use: pytest -m integration",
    )
