import pytest

from api import scan_guard


@pytest.fixture(autouse=True)
def _reset_inflight():
    scan_guard._inflight.clear()  # type: ignore[attr-defined]
    yield
    scan_guard._inflight.clear()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_try_start_reuses_inflight_future():
    started, fut1 = await scan_guard.try_start("1", "/mnt/a")
    assert started is True

    started2, fut2 = await scan_guard.try_start("1", "/mnt/a")
    assert started2 is False
    assert fut1 is fut2

    await scan_guard.complete("1", "/mnt/a", result="ok")
    assert fut1.done()
    assert fut1.result() == "ok"


@pytest.mark.asyncio
async def test_try_start_expires_stale_entry(monkeypatch):
    base_time = 100.0
    times = iter([
        base_time,  # first try_start
        base_time + scan_guard.SCAN_TIMEOUT + 5,  # second try_start
        base_time + scan_guard.SCAN_TIMEOUT + 5,  # any additional calls
    ])

    monkeypatch.setattr(scan_guard.time, "time", lambda: next(times))

    started, fut1 = await scan_guard.try_start("2", "/mnt/b")
    assert started is True

    started2, fut2 = await scan_guard.try_start("2", "/mnt/b")
    assert started2 is True
    assert fut2 is not fut1
