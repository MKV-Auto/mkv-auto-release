"""Seed application settings from environment variables, on every startup.

Without this the container cannot be deployed unattended: every user-facing
setting — TMDB key, MakeMKV registration key, media server, Discord webhook —
was reachable only through the setup wizard, so a fresh container always landed
on a modal waiting for a human.

**Environment is authoritative, and re-applied on every boot.** Not a first-boot
seed: a seed silently stops mattering once ``settings.json`` exists, so editing
your compose file would appear to do nothing after the first run. Re-applying
every boot makes the container declarative — the environment is the desired
state and a restart converges to it.

The consequence is that a setting present in the environment cannot be
meaningfully changed in the UI, because the next restart would revert it.
:func:`env_managed_keys` reports which settings are currently env-backed so the
frontend can disable those fields and say why, rather than letting a user make
an edit that silently disappears later.

Only ``settings.json``-backed settings are handled here. Transfer destinations
live in a database table with encrypted credentials and need their own
mechanism.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, NamedTuple, Optional

_log = logging.getLogger("core.env_settings")


def _as_bool(raw: str) -> Optional[bool]:
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return None


def _as_int(raw: str) -> Optional[int]:
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _as_str(raw: str) -> Optional[str]:
    v = raw.strip()
    return v or None


def _as_media_server(raw: str) -> Optional[str]:
    v = raw.strip().lower()
    return v if v in ("plex", "jellyfin") else None


class EnvSetting(NamedTuple):
    """One environment variable and the settings key it drives.

    ``path`` is dotted for nested settings (``discord.webhook_url``). ``parse``
    returns ``None`` for a value that cannot be used, which is reported and
    skipped rather than written — a typo should not silently disable a feature.
    """

    env: str
    path: str
    parse: Callable[[str], Any]
    note: str = ""


# Order is display order in logs and the API. Keep related settings together.
ENV_SETTINGS: List[EnvSetting] = [
    EnvSetting("MKVAUTO_MAKEMKV_KEY", "makemkv_registration_key", _as_str,
               "also written to MakeMKV's own settings.conf at startup"),
    EnvSetting("MKVAUTO_TMDB_API_KEY", "tmdb_api_key", _as_str),
    EnvSetting("MKVAUTO_MEDIA_SERVER", "media_server", _as_media_server,
               "plex | jellyfin"),
    EnvSetting("MKVAUTO_DISCORD_WEBHOOK_URL", "discord.webhook_url", _as_str),
    EnvSetting("MKVAUTO_DISCORD_ENABLED", "discord.enabled", _as_bool),
    EnvSetting("MKVAUTO_AUTO_RIP", "auto_rip_enabled", _as_bool),
    EnvSetting("MKVAUTO_EJECT_ON_FINISH", "eject_on_finish", _as_bool),
    EnvSetting("MKVAUTO_EJECT_ON_RESTART", "eject_on_restart", _as_bool),
    EnvSetting("MKVAUTO_PATH_TEMPLATE_MOVIE", "path_template_movie", _as_str),
    EnvSetting("MKVAUTO_PATH_TEMPLATE_SERIES", "path_template_series", _as_str),
    EnvSetting("MKVAUTO_PREVIEW_DURATION_SECONDS", "preview_duration_seconds", _as_int),
    EnvSetting("MKVAUTO_PREVIEW_MAX_PARALLEL", "preview_max_parallel", _as_int),
]

_BY_ENV = {e.env: e for e in ENV_SETTINGS}


def _nest(path: str, value: Any) -> Dict[str, Any]:
    """`discord.webhook_url` + v -> {"discord": {"webhook_url": v}}"""
    parts = path.split(".")
    out: Dict[str, Any] = {parts[-1]: value}
    for key in reversed(parts[:-1]):
        out = {key: out}
    return out


def _present() -> List[EnvSetting]:
    """Settings whose variable is set to a non-empty value.

    An empty string means "not configured" rather than "set to empty" —
    otherwise an unset compose variable (`FOO=${FOO}`) would blank a setting.
    """
    return [e for e in ENV_SETTINGS if (os.environ.get(e.env) or "").strip()]


def env_managed_keys() -> List[str]:
    """Dotted setting paths currently driven by the environment.

    Computed live rather than stored, so it cannot go stale against a container
    that was restarted with different variables. The frontend uses this to
    disable the corresponding fields.

    A variable whose value fails to parse is **excluded**: its setting still
    holds whatever it held before, so reporting it as env-managed would disable
    a field while showing a value the environment did not supply.
    """
    return [e.path for e in _present() if e.parse(os.environ[e.env]) is not None]


def apply_env_settings() -> Dict[str, Any]:
    """Write every environment-provided setting into settings.json.

    Returns the applied ``{path: value}``. Safe to call repeatedly; called once
    per startup. Never raises — a bad value is logged and skipped, because
    failing to boot over a malformed optional setting is worse than ignoring it.
    """
    from core import settings as settings_mod

    applied: Dict[str, Any] = {}
    updates: Dict[str, Any] = {}

    for entry in _present():
        raw = os.environ[entry.env]
        value = entry.parse(raw)
        if value is None:
            # Do not log raw: MKVAUTO_MAKEMKV_KEY and the Discord webhook are secrets.
            _log.warning(
                "Ignoring %s: value is not valid for %s%s",
                entry.env, entry.path,
                f" ({entry.note})" if entry.note else "",
            )
            continue
        _deep_update(updates, _nest(entry.path, value))
        applied[entry.path] = value

    if not updates:
        return {}

    try:
        settings_mod.save_settings(updates)
    except Exception as exc:  # pragma: no cover - defensive
        _log.error("Failed to persist environment settings: %s", exc)
        return {}

    _log.info(
        "Applied %d setting(s) from the environment: %s",
        len(applied), ", ".join(sorted(applied)),
    )
    return applied


def _deep_update(target: Dict[str, Any], src: Dict[str, Any]) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(target.get(k), dict):
            _deep_update(target[k], v)
        else:
            target[k] = v


def describe() -> List[Dict[str, Any]]:
    """Every supported variable and whether it is currently set.

    Powers both the API (so the UI can explain a disabled field) and the
    documentation table, so the two cannot drift from the code.
    """
    return [
        {
            "env": e.env,
            "setting": e.path,
            "set": bool((os.environ.get(e.env) or "").strip()),
            "note": e.note,
        }
        for e in ENV_SETTINGS
    ]
