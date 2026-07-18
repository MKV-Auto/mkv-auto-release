"""Unit tests for core.makemkv_state: is_disabled, set_disabled, get_reason, clear_disabled."""
import pytest

from core import makemkv_state


@pytest.fixture(autouse=True)
def _clear_state():
    makemkv_state.clear_disabled()
    yield
    makemkv_state.clear_disabled()


def test_is_disabled_false_initially():
    assert makemkv_state.is_disabled() is False


def test_set_disabled_then_is_disabled_true():
    makemkv_state.set_disabled("reason")
    assert makemkv_state.is_disabled() is True


def test_set_disabled_then_get_reason():
    makemkv_state.set_disabled("reason")
    assert makemkv_state.get_reason() == "reason"


def test_clear_disabled_then_is_disabled_false():
    makemkv_state.set_disabled("reason")
    makemkv_state.clear_disabled()
    assert makemkv_state.is_disabled() is False
