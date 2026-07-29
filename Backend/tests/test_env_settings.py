"""Environment-driven settings (unattended container deployment).

The contract these protect: the environment is authoritative and re-applied on
every boot, so a container is declarative. Two failure modes matter more than
the happy path — a malformed value must not disable a feature silently, and an
unset compose variable must not blank an existing setting.
"""
import os
from unittest.mock import patch

import pytest

from core import env_settings


@pytest.fixture
def clean_env(monkeypatch):
    """No MKVAUTO_* setting vars leaking in from the developer's shell."""
    for entry in env_settings.ENV_SETTINGS:
        monkeypatch.delenv(entry.env, raising=False)
    return monkeypatch


def test_nothing_set_applies_nothing(clean_env):
    with patch("core.settings.save_settings") as save:
        assert env_settings.apply_env_settings() == {}
    save.assert_not_called()
    assert env_settings.env_managed_keys() == []


def test_applies_scalar_and_nested(clean_env):
    clean_env.setenv("MKVAUTO_TMDB_API_KEY", "abc123")
    clean_env.setenv("MKVAUTO_DISCORD_WEBHOOK_URL", "https://example.com/hook")

    with patch("core.settings.save_settings") as save:
        applied = env_settings.apply_env_settings()

    assert applied == {
        "tmdb_api_key": "abc123",
        "discord.webhook_url": "https://example.com/hook",
    }
    # Nested paths must arrive as nested dicts, not literal "discord.webhook_url"
    assert save.call_args[0][0] == {
        "tmdb_api_key": "abc123",
        "discord": {"webhook_url": "https://example.com/hook"},
    }


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("1", True), ("yes", True), ("ON", True),
    ("false", False), ("0", False), ("no", False), ("Off", False),
])
def test_bool_forms(clean_env, raw, expected):
    clean_env.setenv("MKVAUTO_AUTO_RIP", raw)
    with patch("core.settings.save_settings"):
        assert env_settings.apply_env_settings()["auto_rip_enabled"] is expected


def test_malformed_value_is_skipped_not_written(clean_env):
    """A typo must not silently disable a feature.

    Writing a coerced default would be worse than ignoring it: `AUTO_RIP=ture`
    would turn auto-rip off while the user believes they turned it on.
    """
    clean_env.setenv("MKVAUTO_AUTO_RIP", "ture")          # typo
    clean_env.setenv("MKVAUTO_PREVIEW_MAX_PARALLEL", "many")
    clean_env.setenv("MKVAUTO_MEDIA_SERVER", "emby")      # unsupported

    with patch("core.settings.save_settings") as save:
        assert env_settings.apply_env_settings() == {}
    save.assert_not_called()


def test_malformed_value_is_not_reported_as_env_managed(clean_env):
    """Otherwise the UI disables a field showing a value env never supplied."""
    clean_env.setenv("MKVAUTO_AUTO_RIP", "ture")
    assert "auto_rip_enabled" not in env_settings.env_managed_keys()


def test_empty_value_is_treated_as_unset(clean_env):
    """`FOO=${FOO}` in compose with FOO unset must not blank the setting."""
    clean_env.setenv("MKVAUTO_TMDB_API_KEY", "")
    clean_env.setenv("MKVAUTO_MEDIA_SERVER", "   ")

    with patch("core.settings.save_settings") as save:
        assert env_settings.apply_env_settings() == {}
    save.assert_not_called()
    assert env_settings.env_managed_keys() == []


def test_reapplying_is_idempotent(clean_env):
    """Every boot re-applies; the second run must produce the same result."""
    clean_env.setenv("MKVAUTO_MEDIA_SERVER", "jellyfin")
    with patch("core.settings.save_settings"):
        first = env_settings.apply_env_settings()
        second = env_settings.apply_env_settings()
    assert first == second == {"media_server": "jellyfin"}


def test_env_managed_keys_tracks_the_live_environment(clean_env):
    """Computed, not stored — a restart with different vars must be reflected."""
    assert env_settings.env_managed_keys() == []
    clean_env.setenv("MKVAUTO_TMDB_API_KEY", "k")
    assert env_settings.env_managed_keys() == ["tmdb_api_key"]
    clean_env.delenv("MKVAUTO_TMDB_API_KEY")
    assert env_settings.env_managed_keys() == []


def test_secrets_are_not_logged(clean_env, caplog):
    """Rejected values must not echo the raw string — two of these are secrets."""
    clean_env.setenv("MKVAUTO_MEDIA_SERVER", "super-secret-looking-value")
    with patch("core.settings.save_settings"):
        env_settings.apply_env_settings()
    assert "super-secret-looking-value" not in caplog.text


def test_save_failure_does_not_raise(clean_env):
    """A settings write failure must not stop the container booting."""
    clean_env.setenv("MKVAUTO_TMDB_API_KEY", "abc")
    with patch("core.settings.save_settings", side_effect=OSError("read-only fs")):
        assert env_settings.apply_env_settings() == {}


def test_describe_covers_every_declared_setting(clean_env):
    """describe() powers both the API and the docs table — it must not drift."""
    described = env_settings.describe()
    assert len(described) == len(env_settings.ENV_SETTINGS)
    assert all({"env", "setting", "set", "note"} <= set(d) for d in described)
    assert all(d["set"] is False for d in described)


def test_every_declared_setting_is_a_real_settings_key():
    """Guards against a typo'd path that would silently never apply."""
    from core.settings import _default_settings

    defaults = _default_settings()
    for entry in env_settings.ENV_SETTINGS:
        head, _, rest = entry.path.partition(".")
        assert head in defaults, f"{entry.env} -> unknown setting {entry.path}"
        if rest:
            assert isinstance(defaults[head], dict), f"{entry.path} is not nested"
            assert rest in defaults[head], f"{entry.env} -> unknown key {entry.path}"


class TestSetupStatusExposure:
    """The wizard and the settings screens both read env-management from the API.

    Without it the frontend would have to guess, and a user would be handed an
    editable field whose value a restart silently reverts.

    Called directly rather than through TestClient: app startup pre-downloads
    MakeMKV over the network, which has nothing to do with what is under test.
    """

    def test_setup_status_reports_env_managed(self, clean_env):
        import asyncio
        from api.routers.system import get_setup_status

        clean_env.setenv("MKVAUTO_TMDB_API_KEY", "abc")
        assert asyncio.run(get_setup_status()).env_managed == ["tmdb_api_key"]

    def test_env_managed_endpoint_lists_supported_and_managed(self, clean_env):
        import asyncio
        from api.routers.system import get_env_managed_settings

        clean_env.setenv("MKVAUTO_MEDIA_SERVER", "jellyfin")
        body = asyncio.run(get_env_managed_settings())

        assert body["managed"] == ["media_server"]
        # `supported` lists every variable, set or not, so the UI can show what a
        # deployment could pin without hard-coding a list that would drift.
        assert len(body["supported"]) == len(env_settings.ENV_SETTINGS)
        assert [d["env"] for d in body["supported"] if d["set"]] == ["MKVAUTO_MEDIA_SERVER"]

    def test_status_survives_a_broken_env_lookup(self, clean_env):
        """A failure here must cost the hint, not the setup screen."""
        import asyncio
        from api.routers.system import get_setup_status

        with patch("core.env_settings.env_managed_keys", side_effect=RuntimeError("boom")):
            assert asyncio.run(get_setup_status()).env_managed == []


def test_the_configuration_guide_documents_every_variable():
    """The table is hand-written; ENV_SETTINGS is the source of truth.

    A variable added to the code and not the guide is invisible to the people
    the feature exists for — nobody can set what they cannot find.

    Lives in CONFIGURATION.md since #742 split the guides; the assertion below
    also fails if the file is moved again without repointing this.
    """
    import re
    from pathlib import Path

    guide = Path(__file__).resolve().parents[2] / "docs" / "Guides" / "CONFIGURATION.md"
    assert guide.exists(), f"the guide moved: {guide} does not exist"
    documented = set(re.findall(r"`(MKVAUTO_[A-Z_]+)`", guide.read_text()))
    declared = {e.env for e in env_settings.ENV_SETTINGS}

    assert not declared - documented, f"undocumented: {sorted(declared - documented)}"
    # The reverse direction matters too: a documented variable that no longer
    # exists sends people to set something that silently does nothing.
    stale = {d for d in documented - declared} - {"MKVAUTO_DEBUG_LEVEL", "MKVAUTO_ROOT",
                                                 "MKVAUTO_DATA", "MKVAUTO_TMP_DIR"}
    assert not stale, f"documented but not declared: {sorted(stale)}"


def test_the_unraid_template_exposes_every_variable():
    """Unraid users configure through the template UI, so a variable missing
    there is unreachable for them without hand-editing extra parameters."""
    from pathlib import Path

    template = (Path(__file__).resolve().parents[2] / "Unraid" / "mkv-auto.xml").read_text()
    missing = [e.env for e in env_settings.ENV_SETTINGS if f'Target="{e.env}"' not in template]
    assert not missing, f"not in the Unraid template: {missing}"


def test_secret_variables_are_masked_in_the_unraid_template():
    """Keys and webhooks must not render as plain text in the template UI."""
    import re
    from pathlib import Path

    template = (Path(__file__).resolve().parents[2] / "Unraid" / "mkv-auto.xml").read_text()
    for env in ("MKVAUTO_MAKEMKV_KEY", "MKVAUTO_TMDB_API_KEY", "MKVAUTO_DISCORD_WEBHOOK_URL"):
        row = re.search(rf'<Config[^>]*Target="{env}"[^>]*>', template)
        assert row, f"{env} missing from the template"
        assert 'Mask="true"' in row.group(0), f"{env} is not masked"
