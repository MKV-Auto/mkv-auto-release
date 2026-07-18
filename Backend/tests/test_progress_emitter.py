"""Unit tests for core.progress_emitter: emit_job_progress_debounced, debounce and merge of _pending_progress."""
import asyncio
import pytest
from unittest.mock import patch, MagicMock

from core import progress_emitter


@pytest.fixture(autouse=True)
def _reset_emitter_state():
    progress_emitter._last_emission.clear()
    progress_emitter._pending_progress.clear()
    yield
    progress_emitter._last_emission.clear()
    progress_emitter._pending_progress.clear()


@pytest.fixture
def _patch_emit_and_schedule(monkeypatch):
    seen = []
    async def _record(job_id, progress_data):
        seen.append((job_id, progress_data))

    def _run_coro(coro, loop):
        asyncio.run(coro)

    monkeypatch.setattr("core.progress_emitter._emit_progress_async", _record)
    monkeypatch.setattr("core.progress_emitter._global_event_loop", MagicMock(is_running=MagicMock(return_value=True)))
    monkeypatch.setattr("asyncio.run_coroutine_threadsafe", lambda coro, l: asyncio.run(coro))
    return seen


def test_debounce_two_rapid_calls_only_one_emit(_patch_emit_and_schedule):
    seen = _patch_emit_and_schedule
    with patch("core.progress_emitter.time.time", side_effect=[1000.0, 1000.5]):
        progress_emitter.emit_job_progress_debounced("j1", {"p": 50})
        progress_emitter.emit_job_progress_debounced("j1", {"p": 60})
    assert len(seen) == 1
    assert seen[0][0] == "j1"
    assert seen[0][1] == {"p": 50}


def test_debounce_after_interval_second_call_emits(_patch_emit_and_schedule):
    seen = _patch_emit_and_schedule
    with patch("core.progress_emitter.time.time", side_effect=[1000.0, 1001.5]):
        progress_emitter.emit_job_progress_debounced("j1", {"p": 50})
        progress_emitter.emit_job_progress_debounced("j1", {"p": 90})
    assert len(seen) == 2
    assert seen[1][1] == {"p": 90}


def test_merge_pending_progress_on_emit(_patch_emit_and_schedule):
    seen = _patch_emit_and_schedule
    # First emit at 1000; second at 1000.3 goes to pending; third at 1001.5 emits merged
    with patch("core.progress_emitter.time.time", side_effect=[1000.0, 1000.3, 1001.5]):
        progress_emitter.emit_job_progress_debounced("j1", {"a": 1})
        progress_emitter.emit_job_progress_debounced("j1", {"b": 2})
        progress_emitter.emit_job_progress_debounced("j1", {"c": 3})
    assert len(seen) == 2  # first and third
    assert seen[1][1] == {"b": 2, "c": 3}
