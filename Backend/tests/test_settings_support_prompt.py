# Tests for the bell-panel support-prompt state in core.settings
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from core import settings


def _state(**overrides) -> dict:
    base = {"dismissed_forever": False, "snoozed_until": None, "dismiss_count": 0}
    base.update(overrides)
    return base


class TestGetSupportPromptDict:
    def test_defaults_when_key_missing(self):
        with patch.object(settings, "load_settings", return_value={}):
            assert settings.get_support_prompt_dict() == _state()

    def test_defaults_when_value_is_not_a_dict(self):
        with patch.object(settings, "load_settings", return_value={"support_prompt": "junk"}):
            assert settings.get_support_prompt_dict() == _state()

    def test_reads_persisted_values(self):
        stored = _state(dismissed_forever=True, snoozed_until="2026-01-01T00:00:00+00:00", dismiss_count=2)
        with patch.object(settings, "load_settings", return_value={"support_prompt": stored}):
            assert settings.get_support_prompt_dict() == stored

    def test_coerces_corrupt_field_types(self):
        stored = {"dismissed_forever": "yes", "snoozed_until": 12345, "dismiss_count": -4}
        with patch.object(settings, "load_settings", return_value={"support_prompt": stored}):
            got = settings.get_support_prompt_dict()
        assert got["dismissed_forever"] is True
        assert got["snoozed_until"] is None
        assert got["dismiss_count"] == 0


class TestSupportPromptIsSuppressed:
    def test_fresh_install_is_not_suppressed(self):
        with patch.object(settings, "load_settings", return_value={"support_prompt": _state()}):
            assert settings.support_prompt_is_suppressed() is False

    def test_dismissed_forever_is_suppressed(self):
        stored = _state(dismissed_forever=True)
        with patch.object(settings, "load_settings", return_value={"support_prompt": stored}):
            assert settings.support_prompt_is_suppressed() is True

    def test_active_snooze_is_suppressed(self):
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        stored = _state(snoozed_until=(now + timedelta(days=10)).isoformat())
        with patch.object(settings, "load_settings", return_value={"support_prompt": stored}):
            assert settings.support_prompt_is_suppressed(now=now) is True

    def test_expired_snooze_is_not_suppressed(self):
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        stored = _state(snoozed_until=(now - timedelta(days=1)).isoformat())
        with patch.object(settings, "load_settings", return_value={"support_prompt": stored}):
            assert settings.support_prompt_is_suppressed(now=now) is False

    def test_naive_timestamp_is_treated_as_utc(self):
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        stored = _state(snoozed_until="2026-06-10T00:00:00")
        with patch.object(settings, "load_settings", return_value={"support_prompt": stored}):
            assert settings.support_prompt_is_suppressed(now=now) is True

    def test_unparseable_timestamp_suppresses_rather_than_reprompts(self):
        stored = _state(snoozed_until="not-a-date")
        with patch.object(settings, "load_settings", return_value={"support_prompt": stored}):
            assert settings.support_prompt_is_suppressed() is True


class TestRecordSupportPromptDismissal:
    def test_forever_sets_the_permanent_flag(self):
        with patch.object(settings, "load_settings", return_value={"support_prompt": _state()}):
            with patch.object(settings, "save_settings") as mock_save:
                got = settings.record_support_prompt_dismissal(forever=True)
        assert got["dismissed_forever"] is True
        assert mock_save.call_args[0][0]["support_prompt"]["dismissed_forever"] is True

    def test_later_snoozes_and_counts(self):
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        with patch.object(settings, "load_settings", return_value={"support_prompt": _state()}):
            with patch.object(settings, "save_settings"):
                got = settings.record_support_prompt_dismissal(forever=False, now=now)
        assert got["dismiss_count"] == 1
        assert got["dismissed_forever"] is False
        expected = (now + timedelta(days=settings.SUPPORT_PROMPT_SNOOZE_DAYS)).isoformat()
        assert got["snoozed_until"] == expected

    def test_final_snooze_stops_asking_for_good(self):
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        stored = _state(dismiss_count=settings.SUPPORT_PROMPT_MAX_DISMISSALS - 1)
        with patch.object(settings, "load_settings", return_value={"support_prompt": stored}):
            with patch.object(settings, "save_settings"):
                got = settings.record_support_prompt_dismissal(forever=False, now=now)
        assert got["dismiss_count"] == settings.SUPPORT_PROMPT_MAX_DISMISSALS
        assert got["dismissed_forever"] is True

    def test_repeated_later_never_exceeds_the_cap(self):
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        state = _state()
        for _ in range(settings.SUPPORT_PROMPT_MAX_DISMISSALS + 2):
            with patch.object(settings, "load_settings", return_value={"support_prompt": state}):
                with patch.object(settings, "save_settings"):
                    state = settings.record_support_prompt_dismissal(forever=False, now=now)
        assert state["dismissed_forever"] is True
        assert state["dismiss_count"] == settings.SUPPORT_PROMPT_MAX_DISMISSALS + 2


class TestSupportPromptSettingsPlumbing:
    def test_key_is_in_the_persisted_allowlist(self):
        # save_settings drops anything outside _KNOWN_TOP_LEVEL, so a missing
        # entry here would silently discard every dismissal.
        assert "support_prompt" in settings._KNOWN_TOP_LEVEL

    def test_defaults_include_the_block(self):
        assert settings._default_settings()["support_prompt"] == _state()
