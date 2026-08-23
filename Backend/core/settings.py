"""
Persisted app settings: preview, Discord, MakeMKV key copy, and dev-only toggles.
Stored in a single settings.json under get_mkvauto_root()/backend.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from core.notification_preferences import (
    default_notification_preferences,
    migrate_levels_list_to_preferences,
    normalize_notification_preferences,
)
from core.utils import get_mkvauto_data, get_mkvauto_root
from core.logging_utils import get_logger

_log = get_logger("core.settings", "load_settings")

_SETTINGS_FILE = get_mkvauto_root() / "backend" / "settings.json"
_PREVIEW_LEGACY = get_mkvauto_data() / "preview_config.json"
_DISCORD_LEGACY = get_mkvauto_data() / "discord_config.json"

_KNOWN_TOP_LEVEL = frozenset({
    "preview_duration_seconds", "preview_max_parallel", "disable_ffmpeg_junk_detection", "disable_ffprobe_metadata_scan",
    "discdb_miss_workflow_with_prefill",
    "makemkv_registration_key", "discord", "dev", "media_server",
    "first_time_setup_complete", "setup_step",
    "eject_on_finish",
    "eject_on_restart",
    "path_template_movie",
    "path_template_series",
    # Auto-rip toggle (#331). When enabled and a disc finishes scanning,
    # the backend automatically dispatches the rip without user action.
    # DiscDB-hit: full auto. DiscDB-miss: rips into raw/ but still requires
    # the user to link the disc to a movie/release before postprocess runs.
    "auto_rip_enabled",
    # Optional TMDB v3 API key (#369). When set, enables search-by-title
    # against disc.info_title at scan time and episode catalog lookups.
    # Empty/None means TMDB enrichment is disabled (existing scrape path
    # for URL-paste in /movies/lookup still works without a key).
    "tmdb_api_key",
    # How the user reaches this MKV Auto (#841): "http://192.0.2.10:8080"
    # or "https://mkv.example.com", no trailing slash. Only consumer today is
    # Discord notification deep links; unset leaves messages link-free.
    "base_url",
    # State for the one-off "support the project" prompt in the bell panel.
    # Lives server-side rather than in localStorage so a dismissal sticks
    # across every browser and device pointed at this install — dismissing
    # on a desktop and then being asked again on a phone reads as the app
    # ignoring the answer.
    "support_prompt",
})

# Successful rips required before the support prompt is eligible to appear.
SUPPORT_PROMPT_MIN_RIPS = 5
# "Maybe later" pushes the prompt out by this long.
SUPPORT_PROMPT_SNOOZE_DAYS = 90
# After this many "maybe later"s, stop asking permanently.
SUPPORT_PROMPT_MAX_DISMISSALS = 3


def _default_settings() -> dict:
    cpu_count = os.cpu_count() or 1
    return {
        "preview_duration_seconds": 120,
        "preview_max_parallel": max(1, cpu_count),
        "disable_ffmpeg_junk_detection": False,
        "disable_ffprobe_metadata_scan": False,
        # #615: prefill is the useful path — when DiscDB returns a miss, use
        # whatever title hint we have to pre-fill the labeling form rather
        # than ship a blank baseline. Users on 0.x with the flag explicitly
        # saved keep their preference; only fresh installs see this default.
        "discdb_miss_workflow_with_prefill": True,
        "makemkv_registration_key": None,
        "discord": {
            "webhook_url": None,
            "enabled": False,
            "notification_preferences": default_notification_preferences(),
        },
        "dev": {
            "quick_postprocess_tests_enabled": False,
            "ffmpeg_detection_enabled": True,
            "discdb_disabled": False,
            "tmdb_disabled": False,
        },
        "media_server": "plex",
        "eject_on_finish": False,
        "eject_on_restart": False,
        "path_template_movie": None,   # Custom path template for movies (#131), e.g. "{type_dir}/{movie} ({year})/{title}.{resolution}.mkv"
        "path_template_series": None,   # Custom path template for series (#131)
        "first_time_setup_complete": False,
        "setup_step": 1,
        "auto_rip_enabled": False,
        "tmdb_api_key": None,
        "support_prompt": {
            "dismissed_forever": False,
            "snoozed_until": None,  # ISO-8601 UTC timestamp, or None
            "dismiss_count": 0,
        },
    }


def _deep_merge(base: dict, updates: dict, allowed_keys: Optional[frozenset] = None) -> None:
    """Merge updates into base in-place. Only merge keys in allowed_keys if given."""
    for k, v in updates.items():
        if allowed_keys is not None and k not in allowed_keys:
            continue
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v, allowed_keys=None)
        else:
            base[k] = v


def _migrate_from_legacy(data: dict) -> dict:
    """Merge legacy preview_config.json and discord_config.json into data if they exist."""
    if _PREVIEW_LEGACY.exists():
        try:
            leg = json.loads(_PREVIEW_LEGACY.read_text())
            if isinstance(leg, dict):
                if isinstance(leg.get("duration_seconds"), int) and leg["duration_seconds"] > 0:
                    data["preview_duration_seconds"] = leg["duration_seconds"]
                if isinstance(leg.get("max_parallel"), int) and leg["max_parallel"] > 0:
                    data["preview_max_parallel"] = leg["max_parallel"]
                if isinstance(leg.get("disable_ffmpeg_junk_detection"), bool):
                    data["disable_ffmpeg_junk_detection"] = leg["disable_ffmpeg_junk_detection"]
                if isinstance(leg.get("disable_ffprobe_metadata_scan"), bool):
                    data["disable_ffprobe_metadata_scan"] = leg["disable_ffprobe_metadata_scan"]
        except Exception:
            pass
    if _DISCORD_LEGACY.exists():
        try:
            leg = json.loads(_DISCORD_LEGACY.read_text())
            if isinstance(leg, dict):
                d = data.setdefault("discord", {})
                if not isinstance(d, dict):
                    d = {}
                    data["discord"] = d
                if isinstance(leg.get("webhook_url"), str) and leg["webhook_url"].strip():
                    d["webhook_url"] = leg["webhook_url"].strip()
                if isinstance(leg.get("enabled"), bool):
                    d["enabled"] = leg["enabled"]
        except Exception:
            pass
    return data


def load_settings() -> dict:
    """
    Read settings.json. If missing or invalid, return defaults.
    On first load when settings.json does not exist, merge from preview_config.json
    and discord_config.json, then write settings.json.
    """
    t0 = time.perf_counter()
    defaults = _default_settings()
    if not _SETTINGS_FILE.exists():
        data = _migrate_from_legacy(dict(defaults))
        try:
            _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            _SETTINGS_FILE.write_text(json.dumps(data, indent=2))
        except Exception:
            pass
        elapsed = time.perf_counter() - t0
        _log.debug("load_settings: path=missing migrated_and_wrote elapsed_sec=%.3f", elapsed)
        return data
    try:
        raw = json.loads(_SETTINGS_FILE.read_text())
        if not isinstance(raw, dict):
            elapsed = time.perf_counter() - t0
            _log.debug("load_settings: path=invalid elapsed_sec=%.3f", elapsed)
            return defaults
        # Renamed from disable_discdb_lookup (skip API) to discdb_miss_workflow_with_prefill (still query; miss UI path).
        if "disable_discdb_lookup" in raw:
            try:
                raw["discdb_miss_workflow_with_prefill"] = bool(raw.pop("disable_discdb_lookup"))
            except Exception:
                raw.pop("disable_discdb_lookup", None)
        # Ensure required structure
        for k, v in defaults.items():
            if k not in raw:
                raw[k] = v
            elif k == "discord" and not isinstance(raw.get(k), dict):
                raw[k] = dict(defaults["discord"])
            elif k == "dev":
                if not isinstance(raw.get(k), dict):
                    raw[k] = dict(defaults["dev"])
                else:
                    for dk, dv in defaults["dev"].items():
                        if dk not in raw[k]:
                            raw[k][dk] = dv
            elif k == "media_server" and raw.get(k) not in ("plex", "jellyfin"):
                raw[k] = "plex"
            elif k == "discdb_miss_workflow_with_prefill" and not isinstance(raw.get(k), bool):
                raw[k] = True  # #615: matches the new default
            elif k == "first_time_setup_complete" and not isinstance(raw.get(k), bool):
                raw[k] = False
            elif k == "setup_step":
                v = raw.get(k)
                if not isinstance(v, int) or v < 1 or v > 6:
                    raw[k] = 1
        elapsed = time.perf_counter() - t0
        _log.debug("load_settings: path=ok elapsed_sec=%.3f", elapsed)
        return raw
    except Exception:
        elapsed = time.perf_counter() - t0
        _log.debug("load_settings: path=exception elapsed_sec=%.3f", elapsed)
        return defaults


def save_settings(updates: dict) -> dict:
    """
    Deep-merge updates into current settings (only into known keys), write to settings.json.
    Returns the merged settings.
    """
    data = load_settings()
    _deep_merge(data, updates, allowed_keys=_KNOWN_TOP_LEVEL)
    # Restrict to known shape
    out = {k: data.get(k) if k in _KNOWN_TOP_LEVEL else (data.get(k) if k in data else None)
           for k in _KNOWN_TOP_LEVEL}
    for k in ("discord", "dev"):
        if not isinstance(out.get(k), dict):
            out[k] = dict(_default_settings()[k])
    try:
        _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SETTINGS_FILE.write_text(json.dumps(out, indent=2))
    except Exception:
        pass
    return out


def get_quick_postprocess_tests_enabled() -> bool:
    return (load_settings().get("dev") or {}).get("quick_postprocess_tests_enabled", False)


def set_quick_postprocess_tests_enabled(enabled: bool) -> None:
    data = load_settings()
    dev = data.get("dev")
    if not isinstance(dev, dict):
        dev = {}
    dev["quick_postprocess_tests_enabled"] = bool(enabled)
    save_settings({"dev": dev})


def get_ffmpeg_detection_enabled() -> bool:
    return (load_settings().get("dev") or {}).get("ffmpeg_detection_enabled", True)


def set_ffmpeg_detection_enabled(enabled: bool) -> None:
    data = load_settings()
    dev = data.get("dev")
    if not isinstance(dev, dict):
        dev = {}
    dev["ffmpeg_detection_enabled"] = bool(enabled)
    save_settings({"dev": dev})


def get_discdb_miss_workflow_with_prefill() -> bool:
    return bool(load_settings().get("discdb_miss_workflow_with_prefill", False))


def set_discdb_miss_workflow_with_prefill(value: bool) -> None:
    save_settings({"discdb_miss_workflow_with_prefill": bool(value)})


def get_auto_rip_enabled() -> bool:
    """Auto-rip toggle (#331). When True, dispatch the rip automatically on
    scan completion without waiting for the user to click Start Rip."""
    return bool(load_settings().get("auto_rip_enabled", False))


def set_auto_rip_enabled(value: bool) -> None:
    save_settings({"auto_rip_enabled": bool(value)})


def apply_discdb_miss_workflow_prefill_to_payload(d: dict) -> None:
    """
    When discdb_miss_workflow_with_prefill is enabled and the payload is a DiscDB hit,
    force full labeling (label_required) while keeping discdb_hit and track/metadata prefill.
    """
    if not get_discdb_miss_workflow_with_prefill():
        return
    if not d.get("discdb_hit"):
        return
    d["label_required"] = True
    d["label_ready"] = False


def get_discdb_disabled() -> bool:
    """Default OFF — production runs the real TheDiscDB lookup. Toggle ON
    in dev to skip the lookup and force every disc into the miss branch.
    Legacy: respects the inverse of the old `workflow_mode_discdb_hit`
    key when the new key isn't present, so dev installs with the old
    setting still behave as before.
    """
    dev = (load_settings().get("dev") or {})
    if "discdb_disabled" in dev:
        return bool(dev.get("discdb_disabled"))
    if "workflow_mode_discdb_hit" in dev:
        # Old key: True (Hit) = real lookup → discdb_disabled=False;
        # False (Miss) = simulated miss → discdb_disabled=True.
        return not bool(dev.get("workflow_mode_discdb_hit"))
    return False


def set_discdb_disabled(disabled: bool) -> None:
    data = load_settings()
    dev = data.get("dev")
    if not isinstance(dev, dict):
        dev = {}
    dev["discdb_disabled"] = bool(disabled)
    # Drop the legacy key so it can't drift out of sync with the new one.
    dev.pop("workflow_mode_discdb_hit", None)
    save_settings({"dev": dev})


def get_tmdb_api_key() -> Optional[str]:
    """Return the configured TMDB v3 API key, or None if unset/empty.

    When None, TMDB-backed features (search-by-title at scan time, episode
    catalog lookups) are disabled. Existing URL-paste scrape in /movies/lookup
    is independent of this and continues to work without a key.
    """
    v = load_settings().get("tmdb_api_key")
    if not isinstance(v, str):
        return None
    s = v.strip()
    return s or None


def set_tmdb_api_key(key: Optional[str]) -> None:
    """Set the TMDB v3 API key. Pass None or empty string to clear."""
    if key is None:
        save_settings({"tmdb_api_key": None})
        return
    s = str(key).strip()
    save_settings({"tmdb_api_key": s or None})


def get_tmdb_disabled() -> bool:
    """Default OFF — production uses TMDB when a key is configured. Toggle ON
    in dev to short-circuit TMDB lookups and force the no-suggestion path.
    """
    return bool((load_settings().get("dev") or {}).get("tmdb_disabled", False))


def set_tmdb_disabled(disabled: bool) -> None:
    data = load_settings()
    dev = data.get("dev")
    if not isinstance(dev, dict):
        dev = {}
    dev["tmdb_disabled"] = bool(disabled)
    save_settings({"dev": dev})


def get_preview_dict() -> dict:
    """Return dict with duration_seconds, max_parallel, disable_ffmpeg_junk_detection, disable_ffprobe_metadata_scan for preview_config callers.

    Clamps `max_parallel` to the server's CPU count on read. A persisted value
    written by a host with more cores (or from an older build with no ceiling)
    is reported back capped so the UI slider thumb never lands outside its track.
    Also surfaces `max_parallel_ceiling` for the UI to bind its slider `[max]` to.
    """
    s = load_settings()
    ceiling = max(1, os.cpu_count() or 1)
    persisted = s.get("preview_max_parallel") or ceiling
    return {
        "duration_seconds": s.get("preview_duration_seconds") or 120,
        "max_parallel": min(int(persisted), ceiling),
        "max_parallel_ceiling": ceiling,
        "disable_ffmpeg_junk_detection": bool(s.get("disable_ffmpeg_junk_detection", False)),
        "disable_ffprobe_metadata_scan": bool(s.get("disable_ffprobe_metadata_scan", False)),
    }


def save_preview_dict(
    duration_seconds: Optional[int] = None,
    max_parallel: Optional[int] = None,
    disable_ffmpeg_junk_detection: Optional[bool] = None,
    disable_ffprobe_metadata_scan: Optional[bool] = None,
) -> dict:
    """Update preview-related keys in settings.json. Omitted args are left unchanged."""
    updates: dict = {}
    if isinstance(duration_seconds, int) and duration_seconds > 0:
        updates["preview_duration_seconds"] = duration_seconds
    if isinstance(max_parallel, int) and max_parallel > 0:
        updates["preview_max_parallel"] = max_parallel
    if disable_ffmpeg_junk_detection is not None:
        updates["disable_ffmpeg_junk_detection"] = bool(disable_ffmpeg_junk_detection)
    if disable_ffprobe_metadata_scan is not None:
        updates["disable_ffprobe_metadata_scan"] = bool(disable_ffprobe_metadata_scan)
    if updates:
        save_settings(updates)
    return get_preview_dict()


def get_discord_dict() -> dict:
    """Return dict with webhook_url, enabled, notification_preferences for discord_config callers."""
    d = load_settings().get("discord") or {}
    if not isinstance(d, dict):
        d = {}

    prefs_raw = d.get("notification_preferences")
    levels_legacy = d.get("notification_levels")

    if isinstance(prefs_raw, dict):
        prefs = normalize_notification_preferences(prefs_raw)
        if "notification_levels" in d:
            nd = dict(d)
            nd["notification_preferences"] = prefs
            nd.pop("notification_levels", None)
            save_settings({"discord": nd})
            d = nd
    elif isinstance(levels_legacy, list) and levels_legacy:
        prefs = migrate_levels_list_to_preferences([str(x) for x in levels_legacy])
        nd = dict(d)
        nd["notification_preferences"] = prefs
        nd.pop("notification_levels", None)
        save_settings({"discord": nd})
        d = nd
    else:
        prefs = default_notification_preferences()
        nd = dict(d)
        nd["notification_preferences"] = prefs
        nd.pop("notification_levels", None)
        save_settings({"discord": nd})
        d = nd

    return {
        "webhook_url": d.get("webhook_url"),
        "enabled": bool(d.get("enabled", False)),
        "notification_preferences": prefs,
    }


def save_discord_dict(
    webhook_url: Optional[str] = None,
    enabled: Optional[bool] = None,
    notification_levels: Optional[list] = None,
    notification_preferences: Optional[dict] = None,
) -> dict:
    """Update discord keys in settings.json. Omitted args are left unchanged. notification_levels is deprecated."""
    data = load_settings()
    disc = data.get("discord") or {}
    if not isinstance(disc, dict):
        disc = {}
    if webhook_url is not None:
        disc["webhook_url"] = webhook_url.strip() if webhook_url else None
    if enabled is not None:
        disc["enabled"] = bool(enabled)
    if notification_preferences is not None:
        disc["notification_preferences"] = normalize_notification_preferences(notification_preferences)
    disc.pop("notification_levels", None)
    save_settings({"discord": disc})
    return get_discord_dict()


def get_webhook_url() -> Optional[str]:
    """Return Discord webhook URL if enabled, else None."""
    d = get_discord_dict()
    if d.get("enabled") and d.get("webhook_url"):
        return d["webhook_url"]
    return None


def get_media_server() -> str:
    """Return media_server ("plex" | "jellyfin"). Default "plex"."""
    v = load_settings().get("media_server")
    if v in ("plex", "jellyfin"):
        return v
    return "plex"


def set_media_server(value: str) -> None:
    """Set media_server to "plex" or "jellyfin". Invalid values are coerced to "plex"."""
    normalized = "plex" if value not in ("plex", "jellyfin") else value
    save_settings({"media_server": normalized})


def normalize_base_url(value) -> str | None:
    """Validate + normalize the deep-link base URL (#841).

    Accepts http/https origins, with optional port and optional path prefix
    (reverse-proxy mounts); strips trailing slashes and whitespace. Returns
    None for empty input. Raises ValueError for anything else — the settings
    endpoint turns that into a 422 with the reason.
    """
    if value is None:
        return None
    text = str(value).strip().rstrip("/")
    if not text:
        return None
    if not (text.startswith("http://") or text.startswith("https://")):
        raise ValueError("Base URL must start with http:// or https://")
    rest = text.split("://", 1)[1]
    if not rest or rest.startswith("/"):
        raise ValueError("Base URL needs a host, e.g. http://192.0.2.10:8080")
    if any(c.isspace() for c in text):
        raise ValueError("Base URL must not contain spaces")
    return text


def get_base_url() -> str | None:
    """Normalized deep-link base URL, or None when unset/invalid."""
    raw = load_settings().get("base_url")
    try:
        return normalize_base_url(raw)
    except ValueError:
        return None


def set_base_url(value) -> str | None:
    normalized = normalize_base_url(value)
    save_settings({"base_url": normalized})
    return normalized


def get_first_time_setup_complete() -> bool:
    """Return whether first-time setup has been completed."""
    return bool(load_settings().get("first_time_setup_complete", False))


def set_first_time_setup_complete(value: bool) -> None:
    """Mark first-time setup as complete or incomplete."""
    save_settings({"first_time_setup_complete": bool(value)})


def get_setup_step() -> int:
    """Return current setup wizard step (1-6). Default 1."""
    v = load_settings().get("setup_step")
    if isinstance(v, int) and 1 <= v <= 6:
        return v
    return 1


def set_setup_step(step: int) -> None:
    """Set setup wizard step (1-6). Values outside range are clamped."""
    clamped = max(1, min(6, int(step)))
    save_settings({"setup_step": clamped})


def get_support_prompt_dict() -> dict:
    """Return support-prompt state, filling defaults for missing/corrupt fields."""
    raw = load_settings().get("support_prompt")
    defaults = _default_settings()["support_prompt"]
    if not isinstance(raw, dict):
        return dict(defaults)
    snoozed = raw.get("snoozed_until")
    count = raw.get("dismiss_count")
    return {
        "dismissed_forever": bool(raw.get("dismissed_forever", defaults["dismissed_forever"])),
        "snoozed_until": snoozed if isinstance(snoozed, str) and snoozed else None,
        "dismiss_count": count if isinstance(count, int) and count >= 0 else 0,
    }


def support_prompt_is_suppressed(now: Optional[datetime] = None) -> bool:
    """Whether dismissal state alone rules the prompt out, ignoring rip count.

    An unparseable ``snoozed_until`` is treated as suppressing rather than
    showing: a corrupt timestamp should not turn into a re-prompt.
    """
    state = get_support_prompt_dict()
    if state["dismissed_forever"]:
        return True
    snoozed_until = state["snoozed_until"]
    if not snoozed_until:
        return False
    current = now or datetime.now(timezone.utc)
    try:
        deadline = datetime.fromisoformat(snoozed_until)
    except ValueError:
        return True
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return current < deadline


def record_support_prompt_dismissal(forever: bool, now: Optional[datetime] = None) -> dict:
    """Record a dismissal and return the resulting state.

    ``forever=True`` silences the prompt permanently. Otherwise it snoozes for
    ``SUPPORT_PROMPT_SNOOZE_DAYS`` and, once the user has deferred
    ``SUPPORT_PROMPT_MAX_DISMISSALS`` times, stops asking for good — repeatedly
    declining is an answer.
    """
    state = get_support_prompt_dict()
    if forever:
        state["dismissed_forever"] = True
    else:
        current = now or datetime.now(timezone.utc)
        state["dismiss_count"] += 1
        state["snoozed_until"] = (
            current + timedelta(days=SUPPORT_PROMPT_SNOOZE_DAYS)
        ).isoformat()
        if state["dismiss_count"] >= SUPPORT_PROMPT_MAX_DISMISSALS:
            state["dismissed_forever"] = True
    save_settings({"support_prompt": state})
    return state
