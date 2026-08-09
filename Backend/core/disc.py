import os, json, shutil, re, traceback
from pathlib import Path
from .utils import (
    run_makemkv,
    parse_log,
    hash_media_disc,
    retrieve_discdb_data,
    parse_discdb_data,
    move_with_progress,
    is_dev_mode,
    sanitize_path_component,
)
from .utils import infer_resolution_from_log
from pathvalidate import sanitize_filepath
from typing import Callable, Optional
import logging
from core.logging_utils import get_logger
from core import settings as app_settings
from core.title_type_extras_layout import extras_subfolder_for_type, plex_episode_extra_suffix_for_type
from core.title_type_normalize import normalize_title_type_for_api

logger = get_logger("core.disc")


def _apply_path_template(template: str, variables: dict) -> str | None:
    """
    Apply a user-defined path template with {variable} substitution.
    Returns the rendered path, or None if any required variable is missing.
    Variables: type_dir, movie, year, title, resolution, edition, season, episode, format.
    """
    try:
        # Simple str.format_map with a defaultdict that returns empty string for missing keys
        from collections import defaultdict
        safe_vars = defaultdict(str, {k: sanitize_path_component(str(v)) if v else "" for k, v in variables.items()})
        result = template.format_map(safe_vars)
        # Ensure .mkv extension
        if not result.lower().endswith(".mkv"):
            result += ".mkv"
        # Clean up double separators from empty variables
        while "//" in result:
            result = result.replace("//", "/")
        return result
    except Exception:
        return None


def format_episode_designator(
    season: int,
    episode: int,
    episode_end: int | None = None,
    media_server: str = "plex",
) -> str:
    """``s03e01``, or ``s03e01-e02`` when one file covers several episodes.

    Shared by ``compute_expected_path`` and ``Disc._rename_series`` — those
    two must produce identical names or the transfer stage's expected-file
    check (core/stage_validation.py) fails on every episode.
    """
    ss, ee = int(season), int(episode)
    if (media_server or "plex").strip().lower() == "jellyfin":
        name = f"S{ss:02}E{ee:02}"
        if episode_end is not None and int(episode_end) > ee:
            name += f"-E{int(episode_end):02}"
        return name
    name = f"s{ss:02}e{ee:02}"
    if episode_end is not None and int(episode_end) > ee:
        name += f"-e{int(episode_end):02}"
    return name


def format_part_suffix(part: int | None) -> str:
    """`` - part1`` — the Plex/Jellyfin *stacking* suffix.

    Both servers treat files sharing a basename and differing only by
    ``partN`` as ONE episode split across files, which is what a disc does
    when it presents an episode as "Part 1" / "Part 2" (#796).

    Deliberately not used for a two-parter TMDB numbers as separate episodes
    (E20 "Zero Hour (1)", E21 "Zero Hour (2)") — stacking requires the same
    episode number, so those stay separate episodes or use the range form.
    """
    if part is None:
        return ""
    try:
        n = int(part)
    except (TypeError, ValueError):
        return ""
    return f" - part{n}" if n > 0 else ""


def compute_expected_path(
    title_metadata: dict,
    release_metadata: dict,
    movie_metadata: dict,
    media_server: str = "plex",
    resolution: str | None = None,
) -> str:
    """Pure function: compute the expected relative path for a title.

    Args:
        title_metadata: {title, type, season, episode, edition, description}
        release_metadata: {release_type, release_name}
        movie_metadata: {movie_name, production_year}
        media_server: "plex" or "jellyfin"
        resolution: e.g. "1080p", "4k", "2160p"

    Returns:
        Relative path like "Movies/Title (2024)/Title.1080p.mkv" or
        "Series/Show/Season 01/Show - s01e01 - Episode.mkv".
    """
    # Check for custom path template (#131)
    try:
        from core.settings import load_settings
        settings = load_settings()
        release_type = (release_metadata.get("release_type") or "movie").strip().lower()
        is_series = release_type in ("series", "tv") or title_metadata.get("season") is not None
        tpl_key = "path_template_series" if is_series else "path_template_movie"
        tpl = settings.get(tpl_key)
        if tpl and isinstance(tpl, str) and tpl.strip():
            tpl_vars = {
                "type_dir": "Series" if is_series else "Movies",
                "movie": movie_metadata.get("movie_name") or "",
                "title": title_metadata.get("title") or "",
                "year": movie_metadata.get("production_year") or "",
                "resolution": resolution or "",
                "edition": title_metadata.get("edition") or "",
                "season": f"{int(title_metadata['season']):02}" if title_metadata.get("season") is not None else "",
                "episode": f"{int(title_metadata['episode']):02}" if title_metadata.get("episode") is not None else "",
                "format": release_metadata.get("disc_format") or "",
                "release": release_metadata.get("release_name") or "",
            }
            result = _apply_path_template(tpl.strip(), tpl_vars)
            if result:
                return result
            # Template failed, fall through to default logic
    except Exception:
        pass  # Settings unavailable, use default logic

    ms = (media_server or "plex").strip().lower()
    title_name = (title_metadata.get("title") or "").strip()
    title_type = (title_metadata.get("type") or "").strip()
    edition = (title_metadata.get("edition") or "").strip()
    season = title_metadata.get("season")
    episode = title_metadata.get("episode")
    movie_name = (movie_metadata.get("movie_name") or "").strip()
    production_year = movie_metadata.get("production_year")
    release_type = (release_metadata.get("release_type") or "movie").strip().lower()

    safe_movie = sanitize_path_component(movie_name) if movie_name else ""
    is_series = release_type in ("series", "tv") or season is not None

    # Top-level type directory
    if release_type in ("movie", "boxset"):
        type_dir = "Movies"
    elif release_type in ("series", "tv"):
        type_dir = "Series"
    else:
        type_dir = release_type.capitalize() or "Movies"

    # Show folder name
    folder_name = safe_movie
    if production_year and not is_series:
        folder_name = f"{safe_movie} ({production_year})" if safe_movie else str(production_year)

    # Sub-directory within show folder (season, Plex/Jellyfin extras folder)
    canon_type = normalize_title_type_for_api(title_type) or ""
    extra_sub = extras_subfolder_for_type(canon_type, ms)

    # Episode-level extra (Plex only). Plex attaches an extra to an episode by
    # FILENAME, not folder: the file sits in the season folder and must begin
    # with the episode's own filename, then the extra's name, then the type
    # suffix — three segments, hyphen-joined. The episode's filename is
    # reconstructed from ``episode_ref_name`` (the sibling Episode row's
    # title), which the caller resolves; without it we cannot build a prefix
    # Plex would match, so we fall back to the season folder — same as
    # Jellyfin, which has no episode-level extras at all. No resolution or
    # edition suffix: the prefix must stay identical to the episode filename.
    episode_ref_name = (title_metadata.get("episode_ref_name") or "").strip()
    if (
        extra_sub
        and is_series
        and ms != "jellyfin"
        and season is not None
        and episode is not None
        and episode_ref_name
        and safe_movie
        and title_name
    ):
        suffix_word = plex_episode_extra_suffix_for_type(canon_type) or "other"
        designator = format_episode_designator(season, episode, None, ms)
        episode_base = f"{safe_movie} - {designator} - {sanitize_path_component(episode_ref_name)}"
        extra_seg = sanitize_path_component(title_name) or title_name
        filename = sanitize_filepath(f"{episode_base}-{extra_seg}-{suffix_word}.mkv")
        parts = [type_dir]
        if folder_name:
            parts.append(folder_name)
        parts.append(f"Season {int(season):02}")
        parts.append(filename)
        return os.path.join(*parts)

    sub_dir = ""
    if is_series:
        if season is not None:
            sub_dir = f"Season {int(season):02}"
        if extra_sub:
            seg = sanitize_path_component(extra_sub) or extra_sub
            sub_dir = os.path.join(sub_dir, seg) if sub_dir else seg
    else:
        if extra_sub:
            seg = sanitize_path_component(extra_sub) or extra_sub
            sub_dir = seg

    # Base name. The episode-designator form is for Episode rows only: an
    # extra carrying season+episode (scoped, but degraded to the folder form)
    # keeps its own name — naming it "Show - s07e03 - X" would make the media
    # server read the extra as the episode itself.
    base_name = ""
    if is_series and not extra_sub and season is not None and episode is not None and safe_movie:
        designator = format_episode_designator(
            season, episode, title_metadata.get("episode_end"), ms
        )
        ep_part = sanitize_path_component(title_name) if title_name else ""
        if ms == "jellyfin":
            base_name = f"{safe_movie} {designator}"
            if ep_part:
                base_name += f" {ep_part}"
        else:
            base_name = f"{safe_movie} - {designator}"
            if ep_part:
                base_name += f" - {ep_part}"
        base_name += format_part_suffix(title_metadata.get("part"))
    elif title_name:
        base_name = sanitize_path_component(title_name)
    elif safe_movie:
        base_name = safe_movie
        if production_year and not is_series:
            base_name = f"{safe_movie} ({production_year})"

    if not base_name:
        base_name = "Unknown"

    # Edition suffix
    edition_suffix = ""
    if edition:
        safe_edition = sanitize_path_component(edition) or edition
        if ms == "jellyfin":
            edition_suffix = f" - [{safe_edition}]"
        else:
            edition_suffix = f" {{edition-{safe_edition}}}"

    # Resolution suffix
    res_suffix = ""
    if resolution:
        res_str = str(resolution).strip()
        if ms == "jellyfin":
            res_norm = "2160p" if res_str.lower() == "4k" else res_str.lower()
            res_suffix = f" [{res_norm}]"
        else:
            res_norm = "4k" if res_str.lower() == "2160p" else res_str.lower()
            res_suffix = f".{res_norm}"

    # Assemble filename
    if ms == "jellyfin":
        filename = f"{base_name}{edition_suffix}{res_suffix}.mkv"
    else:
        filename = f"{base_name}{res_suffix}{edition_suffix}.mkv"

    filename = sanitize_filepath(filename)

    # Assemble relative path: type_dir/folder_name[/sub_dir]/filename
    parts = [type_dir]
    if folder_name:
        parts.append(folder_name)
    if sub_dir:
        parts.append(sub_dir)
    parts.append(filename)
    return os.path.join(*parts)


def rename_title_file(old_path: str, new_path: str) -> dict:
    """Move a file from old_path to new_path.

    - Creates parent directories as needed.
    - Uses shutil.move (atomic on same filesystem, copy+delete across).

    Returns:
        {"success": True/False, "old_path": ..., "new_path": ..., "error": ...}
    """
    result = {"success": False, "old_path": old_path, "new_path": new_path, "error": None}
    try:
        if not os.path.exists(old_path):
            result["error"] = f"Source file does not exist: {old_path}"
            return result
        if os.path.abspath(old_path) == os.path.abspath(new_path):
            result["success"] = True
            return result
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        shutil.move(old_path, new_path)
        result["success"] = True
    except Exception as exc:
        result["error"] = str(exc)
    return result


class OutputCollisionError(RuntimeError):
    """Two titles resolved to the same output filename.

    Not a per-file error: the loop's ``except Exception`` records those in
    ``self.errors`` and carries on, which for a collision means the second
    file is silently left behind while the job still reports success. This
    type is re-raised by that handler so the stage fails loudly instead.
    """


class Disc:
    """
    Encapsulates disc ripping and renaming logic.
    """
    def __init__(self, disc_num: str, mount_point: str):
        #TheDiscDB Data
        self.raw_db_query = None
        self.db_mapping = {}
        self.movie_name = None  # Movie name (replaces legacy show_title)
        self.release_image = None  # Release cover image (replaces legacy show_image)
        self.disc_slug = None
        self.resolution = None
        self.title_type = None
        self.disc_group = None
        self.group_type = None
        self.disc_format = None
        self.release_year = None
        self.release_date = None
        self.original_year = None
        self.original_release_date = None
        self.release_year = None
        self.release_date = None
        self.release_discs = []
        self.label_required = False
        self.info_log = None  # raw makemkv info output when available
        # Movie data from DiscDB
        self.tmdb_id = None
        self.tmdb_type = None
        self.production_year = None
        self.movie_id = None
        self._discdb_disc_number = None  # Disc number from DiscDB for the matched disc (1-based)
        self.discdb_boxset = None  # TheDiscDB boxset payload for Phase B (crud.ensure_release_from_discdb)

        #Disc Data
        self.disc_num = disc_num
        self.mount_point = mount_point
        self.disc_hash = None
        self.titles = {}
        self.errors = {}
        self.skip_autoscan = os.getenv("MKVAUTO_DISABLE_AUTOSCAN", "").lower() in ("1", "true", "yes")
        self.log_fn = None
        self.release_resolution = None  # Highest resolution across all discs in the release

    def _makemkv_source_spec(self) -> str:
        """Return ``dev:{mount_point}`` for MakeMKV commands.

        When :attr:`by_id_serial` is set (#540), re-resolve the mount_point at
        call time from the stable hardware identity. This defends against the
        catastrophic ``/dev/srN`` renumbering observed in the 2026-06
        diagnostic — between job creation and rip start, the kernel can
        reassign ``/dev/sr1`` to a different physical drive after a USB bus
        reset. Using the cached mount_point in that case would target the
        WRONG drive.

        If ``by_id_serial`` is set but no current ``/dev/srN`` resolves to it,
        the drive has been disconnected entirely → raise so the caller fails
        the job loudly instead of writing to a dead device handle.

        ``disc:{N}`` is unreliable in multi-drive setups and only used as a
        last resort when no mount_point and no by_id_serial are available.
        """

        # If a stable identity was recorded, prefer the currently-resolved
        # mount_point over the (possibly stale) cached one.
        cached_serial = getattr(self, "by_id_serial", None)
        if cached_serial:
            from core.drive_identity import resolve_current_mount_point_for_serial

            fresh_mp = resolve_current_mount_point_for_serial(cached_serial)
            if fresh_mp is None:
                raise ValueError(
                    f"Drive {cached_serial!r} is no longer attached; refusing "
                    "to issue MakeMKV command against stale mount_point "
                    f"{self.mount_point!r}"
                )
            cached_mp = (self.mount_point or "").strip()
            if cached_mp and fresh_mp != cached_mp:
                logger.warning(
                    "drive_identity swap detected: by_id_serial=%s was at %s, "
                    "now at %s; using fresh mount_point",
                    cached_serial, cached_mp, fresh_mp,
                )
            return f"dev:{fresh_mp}"

        mp = (self.mount_point or "").strip()
        if mp:
            return f"dev:{mp}"
        dn = str(self.disc_num or "").strip()
        if dn.isdigit():
            return f"disc:{dn}"
        raise ValueError(
            f"Cannot determine MakeMKV source: no mount_point or valid disc_num "
            f"(mount_point={self.mount_point!r}, disc_num={self.disc_num!r})"
        )

    def load_db_info(self, allow_reentrant: bool = False) -> dict:
        """Fetch and parse DiscDB info."""
        # allow_reentrant=True lets callers that already hold the rip lock avoid
        # deadlocking when computing the hash.
        try:
            self.content_hash = hash_media_disc(self.mount_point, allow_reentrant=allow_reentrant)
            self.disc_hash = self.content_hash
        except Exception as hash_exc:
            raise
        discdb_parse_success = False
        try:
            if is_dev_mode():
                pass
            self.raw_db_query = retrieve_discdb_data(self.content_hash)
            (
                self.movie_name,  # Movie name (replaces legacy show_title)
                self.release_image,  # Release cover image (replaces legacy show_image)
                self.disc_slug,
                self.db_mapping,
                self.resolution,
                self.disc_format,
                self.title_type,
                self.disc_group,
                self.release_year,
                self.release_date,
                self.original_year,
                self.original_release_date,
                self.release_discs,
                self.tmdb_id,
                self.release_resolution,  # Release-level resolution (highest across all discs)
                self.tmdb_type,
                self.production_year,
                self._discdb_disc_number,
                self.discdb_boxset,
            ) = parse_discdb_data(self.raw_db_query, self.content_hash)
            self.group_type = self.title_type or "movie"
        except Exception as exc:
            logger.warning("DiscDB lookup failed, falling back to makemkv info: %s", exc)
            self.raw_db_query = {"error": str(exc)}
            self.label_required = True
            self._fallback_from_makemkv()
        else:
            # If DiscDB payload didn't include resolution/format, try to infer from makemkv info.
            if not self.resolution or not self.disc_format:
                self._fallback_from_makemkv()
            # DiscDB hit: labels are not required up front (unless prefill+miss-workflow setting overrides)
            self.label_required = False
            discdb_parse_success = True
            _prefill_patch = {"discdb_hit": True, "label_required": False, "label_ready": True}
            app_settings.apply_discdb_miss_workflow_prefill_to_payload(_prefill_patch)
            self.label_required = _prefill_patch["label_required"]

        movie_data = self.get_movie_data()
        result = {
            "movie_name": self.movie_name or movie_data.get("name") or "",
            "release_image": self.release_image,
            "resolution": self.resolution,  # Current disc resolution
            "disc_format": self.disc_format,
            "tracks":     self.db_mapping,
            "title_type": self.title_type,
            "disc_hash":  self.disc_hash,
            "titles":     self.titles,
            "disc_group": self.disc_group or self.disc_slug,
            "group_type": self.group_type,
            "release_year": self.release_year,
            "release_date": self.release_date,
            "original_year": self.original_year,
            "original_release_date": self.original_release_date,
            "release_discs": self.release_discs,
            "label_required": self.label_required,
            "label_ready": False if self.label_required else True,
            "info_log": self.info_log,
            "tmdb_id": movie_data.get("tmdb_id"),
            "tmdb_type": movie_data.get("tmdb_type"),
            "production_year": movie_data.get("production_year"),
            "movie_cover_url": movie_data.get("cover_url"),
            "media_type": self.title_type,
            "discdb_hit": discdb_parse_success,
            "release_resolution": self.release_resolution,
        }
        if self._discdb_disc_number is not None:
            result["discdb_disc_num"] = self._discdb_disc_number
        if self.discdb_boxset:
            result["discdb_boxset"] = self.discdb_boxset
        return result


    def convert_to_json(self):
        return {
            "movie_name": self.movie_name or "",
            "release_image": self.release_image,
            "resolution": self.resolution,
            "disc_format": self.disc_format,
            "tracks":     self.db_mapping,
            "title_type": self.title_type,
            "disc_hash":  self.disc_hash,
            "titles":     self.titles,
            "disc_group": self.disc_group or self.disc_slug,
            "release_year": self.release_year,
            "release_date": self.release_date,
            "release_discs": self.release_discs,
        }

    def get_disc_data(self, output_folder):
        """Fetch TheDiscDB Data & Actual disc data"""
        self.load_db_info()
        self.load_disc_map(output_folder)

    def rip(
        self,
        output_folder: str,
        mode: str = "copy",
        log_hook: Optional[Callable[[str], None]] = None,
        rip_set: Optional[list[int]] = None,
        pid_callback: Optional[Callable[[int], None]] = None,
    ) -> int | None:
        """
        Rip disc in mkv or backup mode, then save titles_map.json and disc state.

        Args:
            output_folder: where ripped MKVs and logs are written.
            mode: "copy" (mkv) or "backup".
            log_hook: optional per-line callback for progress streaming.
            rip_set: optional list of MakeMKV title indexes. When provided in
                "copy" mode, runs a per-title loop instead of `mkv DEV all OUT`.
                Used only by the Phase 2 selective-rip path on Midway-class
                obfuscated discs to skip the unmatched same-sorted-segment-map
                siblings. None (default) preserves today's all-mode behavior.
            pid_callback: optional callable invoked with the makemkvcon PID
                the moment the subprocess is spawned. Workers use this to
                persist Job.rip_pid before the (potentially hours-long) rip
                begins, so restart-during-rip recovery can validate the job
                against the OS process table. See #541.

        Returns:
            PID of the most recent makemkvcon process, or None if unavailable.
        """
        try:
            info_log_path = Path(output_folder) / "makemkv_info.log"
            # REMOVED: Info scan before rip - disc info should already be available from the frontend
            # The info scan was causing interruptions when a second rip task was triggered.
            # If titles are needed, they will be parsed from the rip output log.

            # Try to load titles from existing info log if available (from previous scan)
            if info_log_path.exists():
                try:
                    with open(info_log_path, 'r', encoding='utf-8') as f:
                        info_output = f.read()
                    parsed_info = parse_log(info_output)
                    if parsed_info:
                        self.titles = parsed_info
                except Exception:
                    pass

            min_title_len = int(os.getenv("MKVAUTO_MIN_TITLE_LENGTH", "0"))
            src = self._makemkv_source_spec()
            os.makedirs(output_folder, exist_ok=True)
            progress_log_path = Path(output_folder) / "makemkv_progress.log"

            if mode == "copy" and rip_set:
                # Selective-rip path: per-title loop. ~1 min disc-enumeration
                # overhead per call on Midway (--noscan does not suppress
                # enumeration in `mkv` mode), so this is only worth it on
                # discs where the alternative is writing 7.4 TB of duplicates.
                log_parts: list[str] = []
                makemkv_pid: int | None = None
                total = len(rip_set)
                for n, title_idx in enumerate(rip_set, start=1):
                    msg = f"Selective rip: title {title_idx} ({n}/{total})"
                    if log_hook:
                        try:
                            log_hook(msg)
                        except Exception:
                            pass
                    logger.info(msg)
                    args = (
                        f"mkv {src} {title_idx} {output_folder} -r "
                        f"--progress=-same --minlength=0"
                    )
                    seg_log, makemkv_pid = run_makemkv(
                        args, line_cb=log_hook, log_path=progress_log_path,
                        pid_callback=pid_callback,
                    )
                    log_parts.append(seg_log)
                log = "\n".join(log_parts)
            else:
                if mode == "copy":
                    args = f"mkv {src} all {output_folder} -r --progress=-same --minlength={min_title_len}"
                else:
                    args = f"backup --decrypt --cache=16 --noscan -r --progress=-same --minlength={min_title_len} {src} {output_folder}"

                log, makemkv_pid = run_makemkv(
                    args, line_cb=log_hook, log_path=progress_log_path,
                    pid_callback=pid_callback,
                )

            if not self.titles:
                self.titles = parse_log(log)

            os.makedirs(output_folder, exist_ok=True)

            # Dump object state to JSON
            info = {
                "disc_num":     self.disc_num,
                "mount_point":  self.mount_point,
                "titles":       self.titles,
                "db_mapping":   self.db_mapping,
                "movie_name":   self.movie_name,
                "release_image": self.release_image,
                "disc_slug":    self.disc_slug,
                "resolution":   self.resolution,
                "disc_format":  self.disc_format,
                "type":         self.title_type,
                "disc_hash":    self.disc_hash,
                "release_year": self.release_year,
                "release_date": self.release_date,
                "original_year": self.original_year,
                "original_release_date": self.original_release_date,
            }
            with open(os.path.join(output_folder, "disc_info.json"), "w") as f:
                json.dump(info, f, indent=2)

            with open(os.path.join(output_folder, "disc_db_query.json"), "w") as f:
                json.dump(self.raw_db_query, f, indent=2)

            # Persist raw makemkv output (progress)
            with open(os.path.join(output_folder, "makemkv_progress.log"), "w") as f:
                f.write(log)

            # Write File Errors if any
            if self.errors:
                with open(os.path.join(output_folder, "disc_errors.json"), "w") as f:
                    json.dump(self.errors, f, indent=2)

            return makemkv_pid

        except OSError as e:
            # Check if this is a "No space left on device" error
            if e.errno == 28:  # Errno 28 = No space left on device
                error_msg = (
                    f"Rip failed: No space left on device. "
                    f"Please free up disk space and try again. "
                    f"Output folder: {output_folder}"
                )
                logger.error("Disk space error during rip: %s", e, exc_info=True)
                # Try to write error log, but don't fail if we can't (no space)
                try:
                    error_log_path = os.path.join(output_folder, "rip_error.log")
                    os.makedirs(output_folder, exist_ok=True)
                    with open(error_log_path, "w") as f:
                        f.write("An error occurred during the rip process:\n")
                        f.write(f"Error: {error_msg}\n")
                        f.write(traceback.format_exc())
                except Exception:
                    # If we can't write the error log (no space), just log to console
                    pass
                logger.error("Rip failed: %s", error_msg)
                # Re-raise as OSError so the caller can handle it specifically
                raise OSError(e.errno, error_msg) from e
            else:
                # Other OSError - handle normally
                error_log_path = os.path.join(output_folder, "rip_error.log")
                try:
                    os.makedirs(output_folder, exist_ok=True)
                    with open(error_log_path, "w") as f:
                        f.write("An error occurred during the rip process:\n")
                        f.write(traceback.format_exc())
                except Exception:
                    pass
                logger.error("Rip failed: %s (see %s)", e, error_log_path)
                raise
        except Exception as e:
            error_log_path = os.path.join(output_folder, "rip_error.log")
            try:
                os.makedirs(output_folder, exist_ok=True)
                with open(error_log_path, "w") as f:
                    f.write("An error occurred during the rip process:\n")
                    f.write(traceback.format_exc())
            except Exception:
                pass
            logger.error("Rip failed: %s (see %s)", e, error_log_path)
            raise

    def load_disc_map(self, output_folder: str):
        """Load disc state from disc_info.json, or fall back to titles_map.json."""
        disc_info_path = os.path.join(output_folder, "disc_info.json")
        titles_map_path = os.path.join(output_folder, "titles_map.json")

        if os.path.isfile(disc_info_path):
            with open(disc_info_path, "r") as f:
                info = json.load(f)

            self.disc_num     = info.get("disc_num", self.disc_num)
            self.mount_point  = info.get("mount_point", self.mount_point)
            # Handle None values - if titles is None or not a dict, default to empty dict
            titles_data = info.get("titles") or {}
            if not isinstance(titles_data, dict):
                titles_data = {}
            self.titles       = {int(k): v for k, v in titles_data.items()}
            # Handle None values - if db_mapping is None or not a dict, default to empty dict
            db_mapping_data = info.get("db_mapping") or {}
            if not isinstance(db_mapping_data, dict):
                db_mapping_data = {}
            self.db_mapping   = db_mapping_data
            
            self.movie_name   = info.get("movie_name") or info.get("show_title")  # Backward compat
            self.release_image = info.get("release_image") or info.get("show_image")  # Backward compat
            self.disc_slug    = info.get("disc_slug")
            self.resolution   = info.get("resolution")
            self.disc_format  = info.get("disc_format")
            self.title_type   = info.get("type")
            self.disc_hash    = info.get("disc_hash")
            self.release_year = info.get("release_year")
            self.release_date = info.get("release_date")
            self.original_year = info.get("original_year")
            self.original_release_date = info.get("original_release_date")

        elif os.path.isfile(titles_map_path):
            logger.warning("disc_info.json missing, loading only titles_map.json")
            with open(titles_map_path, "r") as f:
                raw = json.load(f)
            # Handle None or non-dict values
            if not isinstance(raw, dict):
                raw = {}
            self.titles = {int(k): v for k, v in raw.items()}
            # So _rename_series fallback track = self.db_mapping.get(str(tid)) works (DiscDB miss: no disc_info.json)
            self.db_mapping = {str(k): v for k, v in self.titles.items()}
        else:
            if self.skip_autoscan:
                logger.info("Autoscan disabled; skipping makemkvcon title map generation")
                self.titles = {}
            else:
                logger.info("No titles_map.json or disc_info.json found, scanning disc to generate map")
                min_title_len = int(os.getenv("MKVAUTO_MIN_TITLE_LENGTH", "0"))
                # Use robot mode (-r) so downstream parsing can read MSG/TINFO/SINFO lines.
                args = f"info {self._makemkv_source_spec()} -r --minlength={min_title_len}"
                log, _ = run_makemkv(args, log_path=Path(output_folder) / "makemkv_info.log")
                self.titles = parse_log(log)
                self.db_mapping = {str(k): v for k, v in self.titles.items()}
                os.makedirs(output_folder, exist_ok=True)
            with open(titles_map_path, "w") as f:
                json.dump({str(k): v for k, v in self.titles.items()}, f, indent=2)

    def _fallback_from_makemkv(self):
        """
        Derive resolution/disc_format (and basic tracks) from a makemkv info scan
        so we can still surface useful metadata when DiscDB is missing.
        """
        func_logger = get_logger("core.disc", "_fallback_from_makemkv")
        func_logger.debug("_fallback_from_makemkv called disc_num=%s mount_point=%s", self.disc_num, self.mount_point)
        # CRITICAL: Don't run makemkv info if makemkv mkv is already running for this disc
        # This prevents conflicts where both processes try to access the drive simultaneously
        try:
            from core.utils import _is_makemkvcon_running_for_disc
            idx = str(self.disc_num or "").strip()
            is_running = _is_makemkvcon_running_for_disc(
                self.mount_point,
                makemkv_disc_index=idx if idx.isdigit() else None,
            )
            func_logger.debug("Checking if makemkvcon mkv is running disc_num=%s mount_point=%s is_running=%s", 
                            self.disc_num, self.mount_point, is_running)
            if is_running:
                logger.warning("Skipping makemkv info fallback: makemkvcon mkv is already running for device %s", self.mount_point)
                func_logger.debug("Skipping fallback scan - makemkvcon mkv is running disc_num=%s mount_point=%s", 
                                self.disc_num, self.mount_point)
                return
        except Exception as check_exc:
            # If check fails, proceed anyway (better to try than skip silently)
            func_logger.debug("Check for running makemkvcon failed - PROCEEDING WITH SCAN disc_num=%s mount_point=%s error=%s", 
                            self.disc_num, self.mount_point, str(check_exc))
            pass
        
        try:
            # Robot mode (-r) ensures MSG/TINFO/SINFO lines are present for parsing.
            min_title_len = int(os.getenv("MKVAUTO_MIN_TITLE_LENGTH", "0"))
            func_logger.debug("Starting makemkv info scan in fallback disc_num=%s mount_point=%s", self.disc_num, self.mount_point)
            log, _ = run_makemkv(f"info {self._makemkv_source_spec()} -r --minlength={min_title_len}")
            self.info_log = log
        except Exception as exc:
            logger.warning("makemkv info fallback failed: %s", exc)
            return

        # Reuse the existing title parser for minimal track mapping.
        try:
            self.titles = parse_log(log)
            if not self.db_mapping:
                self.db_mapping = self.titles
        except Exception:
            # best-effort; continue
            pass

        res, fmt = infer_resolution_from_log(log)
        if res:
            self.resolution = self.resolution or res
        if fmt:
            self.disc_format = self.disc_format or fmt
    
    def get_movie_data(self) -> dict:
        """
        DiscDB / TMDB hints for auto-creation. Includes tmdb_id and tmdb_type even when
        MediaItem.title is empty so scan payloads never drop external ids.
        """
        out: dict = {}
        if self.tmdb_id is not None:
            tid = str(self.tmdb_id).strip()
            if tid:
                out["tmdb_id"] = tid
        if self.tmdb_type is not None:
            tt = str(self.tmdb_type).strip()
            if tt:
                out["tmdb_type"] = tt
        if self.movie_name:
            out["name"] = self.movie_name
        py = self.production_year or self.original_year or self.release_year
        if py is not None:
            out["production_year"] = py
        if self.release_image:
            out["cover_url"] = self.release_image
        return out
                
    def rename_outputs(self, base_directory: str, job_id: str = None, release_type: str = None,
                       movie_name: str = None, production_year: int = None, release_name: str = None,
                       final_paths: dict = None, source_file_to_title: dict = None, source_file_to_type: dict = None,
                       title_id_to_title: dict = None, title_id_to_type: dict = None, title_id_to_source_file: dict = None,
                       title_id_to_edition: dict = None, title_id_to_resolution: dict = None,
                       title_id_to_season: dict = None, title_id_to_episode: dict = None,
                       title_id_to_part: dict = None, title_id_to_episode_end: dict = None,
                       progress_cb: Callable[[int, int, str], None] | None = None, source_hashes: dict = None,
                       media_server: str = "plex",
                       dest_root: Path | None = None):
        """
        Master entry. Creates show folder, picks series vs movie, and runs
        the appropriate renamer.

        If job_id, release_type, movie_name, and production_year are provided,
        files will be copied to: ``<dest_root>/<Type>/<Movie> (<Production Year>)/``.
        Default ``dest_root`` is ``jobs/<job_id>/transient`` for backward
        compatibility; callers can override to write directly to the final
        destination (or a pre-transfer staging area), which is the path the
        ``transient/`` drop in the #365 postprocess collapse is moving toward
        (see ``docs/ADR-001-postprocess-collapse.md``).

        release_name is accepted but not used for destination structure (per Plex/Jellyfin naming).
        Otherwise, uses the legacy behavior with MAKEMKV_LIBRARY_ROOT.
        
        final_paths: Optional mapping of title_id -> relative_path (new format) or source_file -> output_file (legacy).
                     New format preferred - uses title_id keys.
        source_file_to_title: DEPRECATED - Optional mapping of source_file -> title (legacy format).
        source_file_to_type: DEPRECATED - Optional mapping of source_file -> type (legacy format).
        title_id_to_title: Optional mapping of title_id -> title from disc_titles table for destination filenames (new format).
        title_id_to_type: Optional mapping of title_id -> type from disc_titles table for organizing files (new format).
        title_id_to_resolution: Optional mapping of title_id -> resolution (preferred over disc.resolution).
        source_hashes: Optional mapping of title_id -> hash (new format) or source_file -> hash (legacy).
                      If provided, when a source file doesn't exist but the destination file does (partial processing scenario),
                      the hash of the destination file will be verified against the expected hash.
                      If hashes match, the file is skipped (already processed).
        """
        # Most rips land directly in base_directory. Some older runs used a
        # nested folder named after the disc slug (e.g., ".../<job_id>/<slug>/").
        # Prefer that folder if it exists, otherwise operate on base_directory.
        origin_folder = base_directory
        if self.disc_slug:
            slugged = os.path.join(base_directory, self.disc_slug)
            if os.path.isdir(slugged):
                origin_folder = slugged
            else:
                try:
                    # Fallback: if there is exactly one subdirectory, use it
                    subdirs = [d for d in os.listdir(base_directory)
                               if os.path.isdir(os.path.join(base_directory, d))]
                    if len(subdirs) == 1:
                        origin_folder = os.path.join(base_directory, subdirs[0])
                except Exception:
                    pass
	
        # Log rename_outputs start
        from core.logging_utils import get_logger
        logger = get_logger("core.disc", "rename_outputs")
        logger.info(f"rename_outputs: Starting rename from {base_directory} (job_id={job_id}, release_type={release_type}, movie_name={movie_name})")
        if self.log_fn:
            try:
                self.log_fn(f"[postprocess] rename_outputs: Starting rename from {base_directory}")
            except Exception:
                pass
        
        # Log source files found
        origin_path = Path(origin_folder)
        if origin_path.exists():
            mkv_files = list(origin_path.rglob("*.mkv"))
            if mkv_files:
                total_size = sum(f.stat().st_size for f in mkv_files if f.exists())
                logger.info(f"rename_outputs: Found {len(mkv_files)} MKV files in {origin_folder} (total size: {total_size} bytes)")
                for mkv in mkv_files:
                    size = mkv.stat().st_size if mkv.exists() else 0
                    rel_path = mkv.relative_to(origin_path)
                    logger.debug(f"rename_outputs: Source file: {rel_path} ({size} bytes)")
        
        # Determine destination folder
        transients_dir = None
        if job_id and release_type and movie_name:
            # Phase 2 of the postprocess collapse (#365): the destination
            # root is now configurable. Default keeps the pre-collapse
            # behavior (jobs/<job_id>/transient/) so all current callers
            # are unaffected; callers passing dest_root explicitly opt
            # into writing somewhere else (final destination or
            # pre-transfer staging) as the transient/ drop proceeds.
            if dest_root is not None:
                transients_dir = Path(dest_root)
            else:
                from core.utils import resolve_jobs_root
                jobs_root = resolve_jobs_root(None)
                transients_dir = jobs_root / job_id / "transient"
            safe_movie = sanitize_path_component(movie_name or "")
            folder_name = safe_movie
            if production_year:
                folder_name = f"{safe_movie} ({production_year})"
            # Normalize release_type (movie/boxset -> Movies, series/tv -> Series)
            type_dir = release_type.strip().lower() or "movie"
            if type_dir in ("movie", "boxset"):
                type_dir = "Movies"
            elif type_dir in ("series", "tv"):
                type_dir = "Series"
            else:
                type_dir = type_dir.capitalize()
            
            show_folder = sanitize_filepath(str(transients_dir / type_dir / folder_name))
            logger.info(f"rename_outputs: Using new structure - destination: {show_folder}")
        else:
            # Legacy behavior: use MAKEMKV_LIBRARY_ROOT
            library_root = os.getenv("MAKEMKV_LIBRARY_ROOT", base_directory)
            safe_legacy_name = sanitize_path_component(self.movie_name or ("Unknown Show" if self._is_series() else "Unknown Movie"))
            if self._is_series():
                show_folder = sanitize_filepath(os.path.join(library_root, "TV", safe_legacy_name))
            else:
                show_folder = sanitize_filepath(os.path.join(library_root, "Movies", safe_legacy_name))
            logger.info(f"rename_outputs: Using legacy structure - destination: {show_folder}")
        
        os.makedirs(show_folder, exist_ok=True)
        logger.info(f"rename_outputs: Created/verified destination directory: {show_folder}")

        # Calculate transient_root for capturing relative paths (only if job_id provided)
        transient_root = transients_dir if job_id else None

        is_series_result = self._is_series()
        ms = (media_server or "plex").strip().lower()
        if is_series_result:
            renamed_paths = self._rename_series(origin_folder, show_folder, final_paths=final_paths, source_file_to_title=source_file_to_title, source_file_to_type=source_file_to_type, title_id_to_title=title_id_to_title, title_id_to_type=title_id_to_type, title_id_to_source_file=title_id_to_source_file, title_id_to_resolution=title_id_to_resolution, title_id_to_season=title_id_to_season, title_id_to_episode=title_id_to_episode, title_id_to_part=title_id_to_part, title_id_to_episode_end=title_id_to_episode_end, movie_name=movie_name, production_year=production_year, release_name=release_name, progress_cb=progress_cb, source_hashes=source_hashes, transient_root=transient_root, media_server=ms)
        else:
            renamed_paths = self._rename_movie(origin_folder, show_folder, final_paths=final_paths, source_file_to_title=source_file_to_title, source_file_to_type=source_file_to_type, title_id_to_title=title_id_to_title, title_id_to_type=title_id_to_type, title_id_to_source_file=title_id_to_source_file, title_id_to_edition=title_id_to_edition, title_id_to_resolution=title_id_to_resolution, movie_name=movie_name, production_year=production_year, release_name=release_name, progress_cb=progress_cb, source_hashes=source_hashes, transient_root=transient_root, media_server=ms)
        
        # Return mapping of title_id -> final relative path (from transient root)
        return renamed_paths or {}

    def _is_series(self) -> bool:
        """
        If any title in the DB mapping has a season number, treat this as a series.
        """
        return self.title_type == "Series"

    def _rename_series(self, origin_folder: str, show_folder: str, final_paths: dict = None, source_file_to_title: dict = None, source_file_to_type: dict = None, title_id_to_title: dict = None, title_id_to_type: dict = None, title_id_to_source_file: dict = None, title_id_to_resolution: dict = None, title_id_to_season: dict = None, title_id_to_episode: dict = None, title_id_to_part: dict = None, title_id_to_episode_end: dict = None, movie_name: str = None, production_year: int = None, release_name: str = None, progress_cb: Callable[[int, int, str], None] | None = None, source_hashes: dict = None, transient_root: Path = None, media_server: str = "plex"):
        """
        Rename each .mkv under origin_folder into:
        Plex: {ShowTitle}/Season {SS}/ShowTitle - s{SS}e{EE} - EpisodeName.1080p.mkv (4K uses .4k)
        Jellyfin: {ShowTitle}/Season {SS}/ShowTitle S{SS}E{EE} EpisodeName [1080p].mkv (4K uses [2160p])
        
        Returns:
            dict: Mapping of title_id -> final relative path (from transient root)
        
        movie_name: Show name for fallback naming (Plex/Jellyfin format)
        production_year: Production year (optional for series)
        release_name: Release name (optional for series)
        media_server: "plex" | "jellyfin" — controls separator and s01e01 vs S01E01 style
        """
        series_logger = get_logger("core.disc", "_rename_series")
        series_logger.info(f"_rename_series: Starting rename from {origin_folder} to {show_folder}")
        if self.log_fn:
            try:
                self.log_fn(f"[postprocess] Starting series rename: {origin_folder} -> {show_folder}")
            except Exception:
                pass
        
        # Track renamed paths: title_id -> final relative path (from transient root)
        renamed_paths = {}
        
        try:
            series_logger.info("_rename_series: Verifying extracted titles")
            extracted_files = os.listdir(origin_folder)
        except Exception as e:
            series_logger.error(f"_rename_series: Failed to list files in {origin_folder}: {e}")
            if self.log_fn:
                try:
                    self.log_fn(f"[postprocess] ERROR: Failed to list files in {origin_folder}: {e}")
                except Exception:
                    pass
            return renamed_paths  # Return empty dict if we can't list files

        for title in self.titles:
            try:
                exists = any(
                    int(m.group(1)) == title
                    for file in extracted_files
                    if (m := re.match(r".*_t(\d+)\.mkv$", file))
                )

                if not exists:
                    file_name = self.titles.get(title)["file"]
                    episode = self.db_mapping.get(file_name, {}).get("episode_name", "Unknown")
                    self.errors[episode] = "Title Not Extracted"
                    series_logger.warning(f"_rename_series: Title {title} not extracted (file: {file_name}, episode: {episode})")
            except Exception as e:
                series_logger.warning(f"_rename_series: Error verifying title {title}: {e}")
                continue

        # Count total MKV files for progress tracking
        mkv_files = [f for f in extracted_files if f.lower().endswith(".mkv")]
        total_files = len(mkv_files)
        series_logger.info(f"_rename_series: Found {total_files} MKV files to process in {origin_folder}")
        if self.log_fn:
            try:
                self.log_fn(f"[postprocess] Found {total_files} MKV files to process")
            except Exception:
                pass
        files_processed = 0

        # Destinations this run has already claimed, dst -> the input file that
        # claimed it. The discriminator for the collision check below: a dst
        # that existed *before* this run is a resume, a dst claimed *during*
        # this run means two titles resolved to one filename.
        claimed_dsts: dict[str, str] = {}

        # Build reverse maps once (same pattern as _rename_movie) so fn -> title_id resolution works for DiscDB hit and miss
        input_to_title_id = {}  # input filename (fn) -> title_id — primary lookup for DiscDB miss
        source_file_to_title_id = {}
        output_to_title_id_series = {}
        output_to_source_series = {}
        if title_id_to_source_file:
            for title_id, src_file in title_id_to_source_file.items():
                if src_file:
                    src_file = src_file.strip()
                    source_file_to_title_id[src_file] = title_id
                    # Also key by basename so fn from listdir(origin_folder) matches when ripped_files has e.g. raw/filename.mkv
                    bn = os.path.basename(src_file)
                    if bn and bn != src_file:
                        source_file_to_title_id[bn] = title_id
                        source_file_to_title_id[bn.strip()] = title_id
        if final_paths:
            sample_key = next(iter(final_paths.keys())) if final_paths else None
            is_title_id_format = sample_key and len(sample_key) == 36 and '-' in sample_key
            if is_title_id_format:
                # Same as _rename_movie: final_paths values are input filenames (ripped_files); key by basename for fn lookup
                for title_id, input_filename in final_paths.items():
                    if input_filename:
                        raw = str(input_filename).strip()
                        input_fn = os.path.basename(raw) if raw else ""
                        if input_fn:
                            input_to_title_id[input_fn] = title_id
                            input_to_title_id[input_fn.strip()] = title_id
                        if raw and raw != input_fn:
                            input_to_title_id[raw] = title_id
                for title_id, out_file in final_paths.items():
                    out_filename = os.path.basename(out_file) if out_file else None
                    if out_filename:
                        output_to_title_id_series[out_filename] = title_id
            else:
                for src_file, out_file in final_paths.items():
                    out_filename = os.path.basename(out_file) if out_file else None
                    if out_filename:
                        output_to_source_series[out_filename] = src_file

        # Episode titles by (season, episode), for Plex episode-level extras:
        # the extra's filename must begin with its episode's filename, so we
        # need the sibling Episode row's title to reconstruct that prefix.
        episode_name_by_se: dict[tuple[int, int], str] = {}
        for tid_, ty_ in (title_id_to_type or {}).items():
            if (normalize_title_type_for_api(ty_) or "").lower() != "episode":
                continue
            s_ = (title_id_to_season or {}).get(tid_)
            e_ = (title_id_to_episode or {}).get(tid_)
            nm_ = (title_id_to_title or {}).get(tid_)
            if s_ is None or e_ is None or not nm_:
                continue
            try:
                episode_name_by_se[(int(s_), int(e_))] = str(nm_)
            except (TypeError, ValueError):
                continue

        for fn in extracted_files:
            try:
                if not fn.lower().endswith(".mkv"):
                    series_logger.debug(f"_rename_series: Skipping {fn} - not a .mkv file")
                    continue

                m = re.match(r".*_t(\d+)\.mkv$", fn)
                if not m:
                    series_logger.debug(f"_rename_series: Skipping {fn} - filename doesn't match pattern")
                    continue

                tid = int(m.group(1))
                title_info = self.titles.get(tid)
                if not title_info:
                    series_logger.warning(f"_rename_series: No title_info for tid {tid}, skipping {fn}")
                    if self.log_fn:
                        try:
                            self.log_fn(f"[postprocess] Warning: No title_info for tid {tid}, skipping {fn}")
                        except Exception:
                            pass
                    continue

                source_file = title_info.get("file") if isinstance(title_info, dict) else None
                title_id_from_final_paths = None
                source_file_from_final_paths = None

                # Resolve title_id: fn (on-disk) first so DiscDB miss works (source_file_to_title_id is keyed by ripped filename),
                # then source_file (segment), input fn, output fn, legacy, iterate.
                if source_file_to_title_id and (fn in source_file_to_title_id or (fn.strip() in source_file_to_title_id)):
                    title_id_from_final_paths = source_file_to_title_id.get(fn) or source_file_to_title_id.get(fn.strip())
                    if title_id_to_source_file and title_id_from_final_paths:
                        source_file = title_id_to_source_file.get(title_id_from_final_paths) or fn
                elif source_file and source_file_to_title_id and source_file in source_file_to_title_id:
                    title_id_from_final_paths = source_file_to_title_id.get(source_file)
                elif input_to_title_id and (fn in input_to_title_id or fn.strip() in input_to_title_id):
                    title_id_from_final_paths = input_to_title_id.get(fn) or input_to_title_id.get(fn.strip())
                    if title_id_to_source_file and title_id_from_final_paths:
                        source_file = title_id_to_source_file.get(title_id_from_final_paths) or fn
                elif output_to_title_id_series and (fn in output_to_title_id_series or fn.strip() in output_to_title_id_series):
                    title_id_from_final_paths = output_to_title_id_series.get(fn) or output_to_title_id_series.get(fn.strip())
                    if title_id_to_source_file and title_id_from_final_paths:
                        source_file = title_id_to_source_file.get(title_id_from_final_paths) or fn
                elif output_to_source_series and fn in output_to_source_series:
                    source_file_from_final_paths = output_to_source_series.get(fn)
                    source_file = source_file_from_final_paths
                elif final_paths:
                    fn_norm = fn.strip()
                    for path_title_id, path_filename in final_paths.items():
                        if not path_filename:
                            continue
                        p = str(path_filename).strip()
                        if fn_norm == p or fn_norm == os.path.basename(p):
                            if len(str(path_title_id)) == 36 and '-' in str(path_title_id):
                                title_id_from_final_paths = path_title_id
                                if title_id_to_source_file and title_id_from_final_paths:
                                    source_file = title_id_to_source_file.get(title_id_from_final_paths) or fn
                                else:
                                    source_file = fn
                            else:
                                source_file = path_title_id
                            break

                track = None
                if source_file and self.db_mapping:
                    track = self.db_mapping.get(source_file)
                if not track and self.db_mapping:
                    track = self.db_mapping.get(str(tid))
                # Same as _rename_movie: don't skip when we have title_id (DB labels) even if db_mapping has no entry
                # Use explicit non-None + non-empty str so we don't rely on truthiness of UUID/other types
                has_title_id = title_id_from_final_paths is not None and str(title_id_from_final_paths).strip() != ""
                if not track and has_title_id:
                    track = {}
                if title_id_from_final_paths is not None and not isinstance(title_id_from_final_paths, str):
                    title_id_from_final_paths = str(title_id_from_final_paths)
                # Skip only when no track at all (None); track = {} is valid for DiscDB-miss and must not skip
                if track is None:
                    series_logger.warning(f"_rename_series: No DB info for {source_file} (tid {tid}), skipping {fn}")
                    if self.log_fn:
                        try:
                            self.log_fn(f"[postprocess] Warning: No DB info for {source_file} (tid {tid}), skipping {fn}")
                        except Exception:
                            pass
                    continue

                season     = track.get("season")
                episode    = track.get("episode")
                ep_name    = track.get("episode_name")
                # Prefer season/episode from DB when available (DiscDB miss: disc_info.json has none)
                if title_id_from_final_paths:
                    if title_id_to_season is not None and title_id_from_final_paths in title_id_to_season:
                        s_val = title_id_to_season.get(title_id_from_final_paths)
                        if s_val is not None and (isinstance(s_val, int) or (isinstance(s_val, str) and str(s_val).strip() != "")):
                            try:
                                season = int(s_val) if isinstance(s_val, int) else int(str(s_val).strip())
                            except (ValueError, TypeError):
                                pass
                    if title_id_to_episode is not None and title_id_from_final_paths in title_id_to_episode:
                        e_val = title_id_to_episode.get(title_id_from_final_paths)
                        if e_val is not None and (isinstance(e_val, int) or (isinstance(e_val, str) and str(e_val).strip() != "")):
                            try:
                                episode = int(e_val) if isinstance(e_val, int) else int(str(e_val).strip())
                            except (ValueError, TypeError):
                                pass
                
                # Multi-part layout (#796): part/part_of split one episode
                # across files, episode_end covers several in one file.
                def _int_or_none(mapping, key):
                    if not mapping or key is None or key not in mapping:
                        return None
                    raw = mapping.get(key)
                    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
                        return None
                    try:
                        return int(str(raw).strip()) if not isinstance(raw, int) else raw
                    except (ValueError, TypeError):
                        return None

                part = _int_or_none(title_id_to_part, title_id_from_final_paths)
                episode_end = _int_or_none(title_id_to_episode_end, title_id_from_final_paths)

                # Get type from disc_titles table (user labels), prefer new format, fallback to legacy
                title_type = None
                if title_id_from_final_paths:
                    # New format: use title_id mappings
                    if title_id_to_type:
                        title_type = title_id_to_type.get(title_id_from_final_paths)
                # Fallback to legacy format
                if not title_type and source_file_to_type and source_file:
                    title_type = source_file_to_type.get(source_file)
                # Final fallback to db_mapping
                if not title_type:
                    title_type = track.get("type")
                title_type = str(title_type).strip() if title_type else ""

                # Skip titles with type="ignore" - only check type field (content is legacy)
                if title_type and str(title_type).strip().lower() == "ignore":
                    series_logger.info(f"_rename_series: Skipping ignored title: {source_file} (tid {tid}, type={title_type}) for {fn}")
                    if self.log_fn:
                        try:
                            self.log_fn(f"[postprocess] Skipping ignored title: {source_file} (tid {tid}, type={title_type})")
                        except Exception:
                            pass
                    continue

                series_logger.debug(f"_rename_series: Processing {fn} - season={season}, episode={episode}, name={ep_name}, type={title_type}")

                dest_dir = show_folder
                if season is not None and season != '':
                    # Plex/Jellyfin format: Season ## (with zero-padding)
                    season_folder = os.path.join(show_folder, f"Season {int(season):02}")
                    os.makedirs(season_folder, exist_ok=True)
                    dest_dir = season_folder

                # For series: extras live under Season XX/<Plex|Jellyfin extras folder> when applicable.
                # Exception: a Plex extra scoped to a specific EPISODE stays in
                # the season folder itself — Plex attaches it by filename
                # (<episode filename>-<name>-<suffix>), not by folder. Only
                # taken when the sibling Episode row is on this disc, because
                # its title is needed to reconstruct the episode filename;
                # otherwise (and always on Jellyfin, which has no episode
                # extras) it degrades to the season extras folder.
                canon_t = normalize_title_type_for_api(title_type) or ""
                extra_sub = extras_subfolder_for_type(canon_t, media_server)
                episode_extra_ref: str | None = None
                if (
                    extra_sub
                    and (media_server or "plex").strip().lower() != "jellyfin"
                    and season is not None and season != ''
                    and episode is not None and episode != ''
                ):
                    try:
                        episode_extra_ref = episode_name_by_se.get((int(season), int(episode)))
                    except (TypeError, ValueError):
                        episode_extra_ref = None
                if extra_sub and not episode_extra_ref:
                    seg = sanitize_path_component(extra_sub) or extra_sub
                    dest_dir = os.path.join(dest_dir, seg)
                    os.makedirs(dest_dir, exist_ok=True)

                # Determine base name following Plex/Jellyfin conventions
                # Priority: 1) When we have season+episode+show: "Show - s01e01 - EpisodeTitle" (episode title from DB or episode_name)
                #           2) Else title from disc_titles as full base_name
                #           3) episode_name, 4) Track{tid}
                base_name = None
                show_name_s = sanitize_path_component(movie_name or "")
                ep_name_s = sanitize_path_component(ep_name or "") if ep_name else ""
                # Episode title from DB (for use in "Show - s01e01 - EpisodeTitle")
                episode_title_from_db = None
                if title_id_from_final_paths and title_id_to_title:
                    raw = title_id_to_title.get(title_id_from_final_paths)
                    episode_title_from_db = sanitize_path_component(raw) if raw else None
                if not episode_title_from_db and source_file_to_title and source_file:
                    raw = source_file_to_title.get(source_file)
                    episode_title_from_db = sanitize_path_component(raw) if raw else None
                episode_part = episode_title_from_db or ep_name_s or ""
                
                # Plex episode-level extra: <episode filename>-<own name>-<suffix>.
                # episode_title_from_db here is THIS row's title — the extra's
                # own name — while episode_extra_ref is the sibling Episode
                # row's title, which supplies the prefix Plex matches on.
                if episode_extra_ref and show_name_s:
                    designator = format_episode_designator(season, episode, None, media_server)
                    ref_s = sanitize_path_component(episode_extra_ref) or episode_extra_ref
                    own_name = episode_title_from_db or ep_name_s or f"Track{tid}"
                    suffix_word = plex_episode_extra_suffix_for_type(canon_t) or "other"
                    base_name = f"{show_name_s} - {designator} - {ref_s}-{own_name}-{suffix_word}"

                # Prefer full "Show - s01e01 - EpisodeTitle" when we have season, episode, and show name.
                # Never for extras: an extra with season+episode set is not an
                # episode file, and naming it like one would make Plex/Jellyfin
                # treat it as the episode itself.
                if not base_name and not extra_sub and season is not None and episode is not None and show_name_s:
                    designator = format_episode_designator(
                        season, episode, episode_end, media_server
                    )
                    if (media_server or "plex").lower() == "jellyfin":
                        if episode_part:
                            base_name = f"{show_name_s} {designator} {episode_part}"
                        else:
                            base_name = f"{show_name_s} {designator}"
                    else:
                        if episode_part:
                            base_name = f"{show_name_s} - {designator} - {episode_part}"
                        else:
                            base_name = f"{show_name_s} - {designator}"
                    base_name += format_part_suffix(part)
                
                # Fallback: Use title from disc_titles table as full base_name (legacy or when no season/episode)
                if not base_name and episode_title_from_db:
                    base_name = episode_title_from_db
                if not base_name and source_file_to_title and source_file:
                    raw = source_file_to_title.get(source_file)
                    base_name = sanitize_path_component(raw) if raw else None
                
                # Fallback: Use episode_name if available
                if not base_name and ep_name_s:
                    base_name = ep_name_s
                
                # Last resort: Use Track{tid}
                if not base_name:
                    base_name = sanitize_path_component(track.get("format") or "") or f"Track{tid}"

                # Optional resolution: prefer per-title resolution, fallback to disc resolution
                res = None
                if title_id_from_final_paths and title_id_to_resolution:
                    res = title_id_to_resolution.get(title_id_from_final_paths)
                if not res:
                    res = getattr(self, "resolution", None)
                # Episode-extra names carry no resolution suffix: the prefix
                # must stay identical to the episode filename for Plex to
                # attach the file.
                if episode_extra_ref:
                    res = None
                if res:
                    res_str = str(res).strip()
                    if (media_server or "plex").lower() == "jellyfin":
                        res_norm = "2160p" if res_str.lower() == "4k" else res_str.lower()
                        base_name += f" [{res_norm}]"
                    else:
                        res_norm = "4k" if res_str.lower() == "2160p" else res_str.lower()
                        base_name += f".{res_norm}"

                new_name = sanitize_filepath(base_name + ".mkv")

                src = os.path.join(origin_folder, fn)
                dst = os.path.join(dest_dir, new_name)

                # Check if source file exists - if not, check if destination already exists (partial processing)
                src_exists = os.path.exists(src)
                dst_exists = os.path.exists(dst)
                src_size = os.path.getsize(src) if src_exists else 0
                dst_size = os.path.getsize(dst) if dst_exists else 0
                
                series_logger.info(f"_rename_series: Processing {fn} ({src_size} bytes) -> {new_name} (source: {src}, dest: {dst})")

                # Two titles resolving to one filename is data loss, not a
                # resume. Whichever branch below runs, the second file would be
                # skipped: never moved out of transient, never recorded in
                # renamed_paths, never present in expected_files — and the job
                # would still report success.
                #
                # Seen on Star Wars Rebels S3 D1, where the disc splits one
                # TMDB episode ("Steps Into Shadow") across two files and both
                # title rows carry season=3 episode=1.
                if dst in claimed_dsts:
                    error_msg = (
                        f"Two titles resolve to the same output file: "
                        f"{os.path.basename(dst)!r} is claimed by both "
                        f"{claimed_dsts[dst]!r} and {fn!r}. Give them distinct "
                        f"episode numbers, or mark them as parts of one episode."
                    )
                    series_logger.error(f"_rename_series: {error_msg}")
                    if self.log_fn:
                        try:
                            self.log_fn(f"[postprocess] {error_msg}")
                        except Exception:
                            pass
                    raise OutputCollisionError(error_msg)
                claimed_dsts[dst] = fn

                if not src_exists and dst_exists:
                    # Source file doesn't exist but destination does - likely already processed
                    # Verify hash if source_hashes is available
                    if source_hashes and source_file:
                        try:
                            import hashlib
                            expected_hash = source_hashes.get(source_file)
                            if expected_hash:
                                # Calculate hash of existing destination file
                                hasher = hashlib.sha256()
                                with open(dst, 'rb') as f:
                                    while chunk := f.read(8*1024*1024):
                                        hasher.update(chunk)
                                actual_hash = hasher.hexdigest()
                                
                                if actual_hash == expected_hash:
                                    # Hash matches - file already processed correctly, skip move
                                    files_processed += 1
                                    if progress_cb:
                                        progress_cb(files_processed, total_files, fn)
                                    series_logger.info(f"_rename_series: Skipping {fn} - destination exists with matching hash (source: {src}, dest: {dst}, size: {dst_size} bytes)")
                                    if self.log_fn:
                                        try:
                                            self.log_fn(f"[postprocess] Source file missing but destination exists with matching hash - skipping {src} -> {dst}")
                                        except Exception:
                                            pass
                                    continue
                                else:
                                    # Hash mismatch - log warning but continue
                                    series_logger.warning(f"_rename_series: Destination file exists but hash mismatch for {fn} (expected {expected_hash[:8]}..., got {actual_hash[:8]}...)")
                                    if self.log_fn:
                                        try:
                                            self.log_fn(f"[postprocess] Warning: Destination file exists but hash mismatch for {src} -> {dst}")
                                        except Exception:
                                            pass
                            else:
                                # No hash available - assume file is already processed, skip move
                                files_processed += 1
                                if progress_cb:
                                    progress_cb(files_processed, total_files, fn)
                                series_logger.info(f"_rename_series: Skipping {fn} - destination exists (no hash to verify) (source: {src}, dest: {dst}, size: {dst_size} bytes)")
                                if self.log_fn:
                                    try:
                                        self.log_fn(f"[postprocess] Source file missing but destination exists (no hash to verify) - skipping {src} -> {dst}")
                                    except Exception:
                                        pass
                                continue
                        except Exception as hash_exc:
                            # Hash verification failed - log warning but skip move
                            series_logger.warning(f"_rename_series: Failed to verify hash for existing destination file {dst}: {hash_exc}")
                            if self.log_fn:
                                try:
                                    self.log_fn(f"[postprocess] Warning: Failed to verify hash for existing destination file {dst}: {hash_exc}")
                                except Exception:
                                    pass
                            files_processed += 1
                            if progress_cb:
                                progress_cb(files_processed, total_files, fn)
                            continue
                elif dst_exists:
                    # NOTE: src_exists is necessarily True here — the branch
                    # above already handled "source missing". A pre-existing
                    # destination from an earlier run is a genuine resume; a
                    # second title claiming it is caught by the collision guard
                    # above, before we get here.
                    files_processed += 1
                    if progress_cb:
                        progress_cb(files_processed, total_files, fn)
                    series_logger.info(f"_rename_series: Skipping {fn} - destination exists (no source_hashes provided) (source: {src}, dest: {dst}, size: {dst_size} bytes)")
                    if self.log_fn:
                        try:
                            self.log_fn(f"[postprocess] Destination already exists from an earlier run - skipping {src} -> {dst}")
                        except Exception:
                            pass
                    continue
                
                if not src_exists:
                    # Source doesn't exist and destination doesn't exist - this is an error
                    error_msg = f"Source file not found: {src} (and destination doesn't exist)"
                    series_logger.error(f"_rename_series: {error_msg}")
                    if self.log_fn:
                        try:
                            self.log_fn(f"[postprocess] {error_msg}")
                        except Exception:
                            pass
                    self.errors[base_name] = error_msg
                    continue

                # Log file processing start
                series_logger.info(f"_rename_series: Moving {fn} ({src_size} bytes) -> {new_name} (source: {src}, dest: {dst})")
                if self.log_fn:
                    try:
                        self.log_fn(f"[postprocess] Processing file {files_processed + 1}/{total_files}: {fn} -> {dst}")
                    except Exception:
                        pass

                # Create progress callback for this file if overall progress callback is provided
                file_progress_cb = None
                if progress_cb and total_files > 0:
                    def make_file_cb(file_idx: int, total: int, filename: str):
                        def file_cb(percent: int):
                            # percent is 0-100 for this file, convert to overall progress
                            overall_done = file_idx + (percent / 100.0)
                            progress_cb(int(overall_done), total, filename)
                        return file_cb
                    file_progress_cb = make_file_cb(files_processed, total_files, fn)
                    
                move_with_progress(src, dst, log_fn=self.log_fn, progress_cb=file_progress_cb)
                files_processed += 1
                if progress_cb:
                    progress_cb(files_processed, total_files, fn)
                
                # Verify file was created and log completion
                if not os.path.exists(dst):
                    error_msg = f"ERROR: Destination file was not created after move: {dst}"
                    series_logger.error(f"_rename_series: {error_msg}")
                    if self.log_fn:
                        try:
                            self.log_fn(f"[postprocess] {error_msg}")
                        except Exception:
                            pass
                else:
                    final_size = os.path.getsize(dst)
                    series_logger.info(f"_rename_series: Moved {fn} -> {new_name} (source: {src}, dest: {dst}, source_size: {src_size} bytes, dest_size: {final_size} bytes)")
                    series_logger.debug(f"_rename_series: Verified destination file exists: {dst}")
                    
                    # Capture the mapping: title_id -> final relative path (from transient root)
                    if title_id_from_final_paths and transient_root:
                        try:
                            # Calculate relative path from transient root to destination file
                            rel_path = os.path.relpath(dst, transient_root)
                            renamed_paths[str(title_id_from_final_paths)] = rel_path
                            series_logger.debug(f"_rename_series: Captured mapping: title_id={title_id_from_final_paths} -> rel_path={rel_path}")
                        except Exception as rel_exc:
                            series_logger.warning(f"_rename_series: Failed to calculate relative path for {dst}: {rel_exc}")
                
                # Log file processing completion
                if self.log_fn:
                    try:
                        self.log_fn(f"[postprocess] Completed file {files_processed}/{total_files}: {fn} -> {dst}")
                    except Exception:
                        pass
            except OutputCollisionError:
                # Losing a file is not something to record and continue past.
                raise
            except Exception as e:
                error_msg = f"Unexpected error processing {fn}: {e}"
                series_logger.error(f"_rename_series: {error_msg}", exc_info=True)
                if self.log_fn:
                    try:
                        self.log_fn(f"[postprocess] ERROR: {error_msg}")
                    except Exception:
                        pass
                traceback.print_exc()
                self.errors[fn] = f"Unexpected error: {str(e)}"
        
        # Return mapping of title_id -> final relative path
        return renamed_paths

    def _rename_movie(self, origin_folder: str, show_folder: str, final_paths: dict = None, source_file_to_title: dict = None, source_file_to_type: dict = None, title_id_to_title: dict = None, title_id_to_type: dict = None, title_id_to_source_file: dict = None, title_id_to_edition: dict = None, title_id_to_resolution: dict = None, movie_name: str = None, production_year: int = None, release_name: str = None, progress_cb: Callable[[int, int, str], None] | None = None, source_hashes: dict = None, transient_root: Path = None, media_server: str = "plex"):
        """
        Rename each .mkv under origin_folder into:
        Plex: Title.1080p.mkv or Title.1080p {edition-Edition}.mkv (4K uses .4k)
        Jellyfin: Title.mkv or Title - [Edition] [1080p].mkv (4K uses [2160p])
        
        Returns:
            dict: Mapping of title_id -> final relative path (from transient root)
        
        movie_name: Movie name for fallback naming (Plex/Jellyfin format)
        production_year: Production year for main movie naming
        release_name: Accepted but not used for destination naming (per Plex/Jellyfin docs)
        media_server: "plex" | "jellyfin" — controls edition/resolution suffix format
        """
        # Log to unified logger for api.log visibility
        from core.logging_utils import get_logger
        logger = get_logger("core.disc", "_rename_movie")
        logger.info(f"_rename_movie: Starting rename from {origin_folder} to {show_folder}")
        if self.log_fn:
            try:
                self.log_fn(f"[postprocess] Starting movie rename: {origin_folder} -> {show_folder}")
            except Exception:
                pass
        
        # Track renamed paths: title_id -> final relative path (from transient root)
        renamed_paths = {}
        
        # Build reverse maps from final_paths if provided
        # final_paths may have title_id keys (new format) or source_file keys (legacy)
        # IMPORTANT: final_paths values are INPUT filenames (from ripped_files), not output paths
        output_to_title_id = {}
        output_to_source = {}
        input_to_title_id = {}  # Input filename -> title_id map (most reliable for lookup)
        source_file_to_title_id = {}  # source_file -> title_id map (from title_id_to_source_file reverse)
        
        # Build reverse map from title_id_to_source_file if available
        if title_id_to_source_file:
            for title_id, src_file in title_id_to_source_file.items():
                if src_file:
                    source_file_to_title_id[src_file] = title_id
        
        if final_paths:
            # Detect format: check if keys look like UUIDs (title_id) or source files
            sample_key = next(iter(final_paths.keys())) if final_paths else None
            is_title_id_format = sample_key and len(sample_key) == 36 and '-' in sample_key  # UUID format
            
            if is_title_id_format:
                # New format: title_id keys, values are INPUT filenames (from ripped_files)
                for title_id, input_filename in final_paths.items():
                    # final_paths values are input filenames like "HARRY POTTER...t35.mkv" or relative paths
                    # Build input filename -> title_id map (this is what we need for lookup)
                    if input_filename:
                        # Use basename in case it's a path (e.g., "raw/test_t1.mkv" -> "test_t1.mkv")
                        input_fn = os.path.basename(input_filename)
                        input_to_title_id[input_fn] = title_id
                        # Also try matching with the full value (in case directory structure matters)
                        # But prioritize basename match
                        if input_filename != input_fn:
                            input_to_title_id[input_filename] = title_id
                        # Also build output filename map (for backwards compatibility, though values are actually input filenames)
                        output_to_title_id[input_fn] = title_id
                        if input_filename != input_fn:
                            output_to_title_id[input_filename] = title_id
            else:
                # Legacy format: source_file keys
                for source_file, output_file in final_paths.items():
                    output_filename = os.path.basename(output_file) if output_file else None
                    if output_filename:
                        output_to_source[output_filename] = source_file
        
        try:
            all_files = os.listdir(origin_folder)
        except Exception as list_exc:
            logger.error(f"_rename_movie: Failed to list files in {origin_folder}: {list_exc}")
            if self.log_fn:
                try:
                    self.log_fn(f"[postprocess] ERROR: Failed to list files in {origin_folder}: {list_exc}")
                except Exception:
                    pass
            return
        mkv_files = [f for f in all_files if f.lower().endswith(".mkv")]
        total_files = len(mkv_files)
        logger.info(f"_rename_movie: Found {total_files} MKV files to process in {origin_folder}")
        if self.log_fn:
            try:
                self.log_fn(f"[postprocess] Found {total_files} MKV files to process")
            except Exception:
                pass
        files_processed = 0
        
        # Validate that self.titles exists and is a dict
        if not isinstance(self.titles, dict):
            error_msg = f"_rename_movie: self.titles is not a dict (type: {type(self.titles)})"
            logger.error(error_msg)
            if self.log_fn:
                try:
                    self.log_fn(f"[postprocess] ERROR: {error_msg}")
                except Exception:
                    pass
            raise AttributeError(error_msg)
        
        # Validate that self.db_mapping exists and is a dict (if it's set)
        if self.db_mapping is not None and not isinstance(self.db_mapping, dict):
            error_msg = f"_rename_movie: self.db_mapping is not a dict (type: {type(self.db_mapping)})"
            logger.error(error_msg)
            if self.log_fn:
                try:
                    self.log_fn(f"[postprocess] ERROR: {error_msg}")
                except Exception:
                    pass
            raise AttributeError(error_msg)
        
        for fn in mkv_files:
            m = re.match(r".*_t(\d+)\.mkv$", fn)
            if not m:
                logger.debug(f"_rename_movie: Skipping {fn} - filename doesn't match pattern")
                continue

            tid = int(m.group(1))
            
            title_info = self.titles.get(tid)
            if not title_info:
                logger.debug(f"_rename_movie: Skipping {fn} - no title_info for tid {tid}")
                continue

            source_file = title_info.get("file") if isinstance(title_info, dict) else None
            title_id_from_final_paths = None
            source_file_from_final_paths = None
            
            # Try to get title_id using multiple strategies:
            # 1. source_file -> title_id (from title_id_to_source_file reverse map) - most reliable when source_file matches
            # 2. input_to_title_id (from final_paths) - reliable when filename matches
            # 3. output_to_title_id (from final_paths) - fallback
            # 4. output_to_source (legacy format)
            # 5. Direct iteration through final_paths
            if source_file and source_file_to_title_id and source_file in source_file_to_title_id:
                # Strategy 1: Use source_file to look up title_id (most reliable when we have source_file)
                title_id_from_final_paths = source_file_to_title_id.get(source_file)
            elif input_to_title_id and fn in input_to_title_id:
                # New format: input filename -> title_id (most reliable)
                title_id_from_final_paths = input_to_title_id.get(fn)
                # Convert title_id -> source_file using mapping if available
                if title_id_to_source_file and title_id_from_final_paths:
                    source_file = title_id_to_source_file.get(title_id_from_final_paths)
            elif output_to_title_id and fn in output_to_title_id:
                # Fallback: try output filename match (for backwards compatibility)
                title_id_from_final_paths = output_to_title_id.get(fn)
                # Convert title_id -> source_file using mapping if available
                if title_id_to_source_file and title_id_from_final_paths:
                    source_file = title_id_to_source_file.get(title_id_from_final_paths)
            elif output_to_source and fn in output_to_source:
                # Legacy format: source_file keys
                source_file_from_final_paths = output_to_source.get(fn)
                source_file = source_file_from_final_paths
            elif final_paths:
                # Last resort: iterate through final_paths to find matching filename
                # This handles cases where the reverse map wasn't built correctly
                for path_title_id, path_filename in final_paths.items():
                    if path_filename and (fn == path_filename or fn == os.path.basename(path_filename)):
                        # Check if this looks like a title_id (UUID format)
                        if len(path_title_id) == 36 and '-' in path_title_id:
                            title_id_from_final_paths = path_title_id
                            # Convert title_id -> source_file using mapping if available
                            if title_id_to_source_file and title_id_from_final_paths:
                                source_file = title_id_to_source_file.get(title_id_from_final_paths)
                            break
                        else:
                            # Legacy format: source_file key
                            source_file = path_title_id
                            break
            
            track = None
            lookup_method = None
            if source_file and self.db_mapping:
                track = self.db_mapping.get(source_file)
                if track:
                    lookup_method = "source_file"
            # Fallback: try looking up by title ID (tid) as string if source_file lookup fails
            # (db_mapping may be keyed by numeric strings when loaded from labels/disc_info.json)
            if not track and self.db_mapping:
                track = self.db_mapping.get(str(tid))
                if track:
                    lookup_method = "tid_str"
            
            # Don't skip if we have title_id_from_final_paths (database lookup) even if track (db_mapping) is missing
            # We can use title_id_to_type and title_id_to_title from the database instead
            if not track and not title_id_from_final_paths:
                logger.warning(f"_rename_movie: No DB info for {source_file} (tid {tid}), skipping {fn}")
                if self.log_fn:
                    try:
                        self.log_fn(f"[postprocess] Warning: No DB info for {source_file} (tid {tid}), skipping {fn}")
                    except Exception:
                        pass
                continue

            # Get type from disc_titles table (user labels), prefer new format, fallback to legacy
            title_type = None
            title_name = None
            if title_id_from_final_paths:
                # New format: use title_id mappings
                if title_id_to_type:
                    title_type = title_id_to_type.get(title_id_from_final_paths)
                if title_id_to_title:
                    title_name = title_id_to_title.get(title_id_from_final_paths)
            # Fallback to legacy format
            if not title_type and source_file_to_type and source_file:
                title_type = source_file_to_type.get(source_file)
            if not title_name and source_file_to_title and source_file:
                title_name = source_file_to_title.get(source_file)
            # Final fallback to db_mapping (only if track is available)
            if not title_type and track:
                title_type_raw = track.get("type")
                title_type = str(title_type_raw).strip() if title_type_raw else ""
            elif not title_type:
                # No title_type found anywhere - default to empty (will use Track{tid} for naming)
                title_type = ""
            else:
                title_type = str(title_type).strip()
            
            # Get episode_name from track (db_mapping) if available, otherwise None
            ep_name = track.get("episode_name") if track else None
            
            # Skip titles with type="ignore" - only check type field (content is legacy)
            if title_type and str(title_type).strip().lower() == "ignore":
                logger.info(f"_rename_movie: Skipping ignored title: {source_file} (tid {tid}, type={title_type}) for {fn}")
                if self.log_fn:
                    try:
                        self.log_fn(f"[postprocess] Skipping ignored title: {source_file} (tid {tid}, type={title_type})")
                    except Exception:
                        pass
                continue
            
            logger.debug(f"_rename_movie: Processing {fn} - title_type={title_type}, track={track}")

            dest_dir = show_folder
            canon_t = normalize_title_type_for_api(title_type) or ""
            extra_sub = extras_subfolder_for_type(canon_t, media_server)
            if extra_sub:
                seg = sanitize_path_component(extra_sub) or extra_sub
                dest_dir = os.path.join(show_folder, seg)
                os.makedirs(dest_dir, exist_ok=True)

            
            # Determine base name following Plex/Jellyfin conventions
            # Priority: 1) title from disc_titles table, 2) movie_name for main movie, 3) episode_name, 4) Track{tid}
            base_name = None
            title_source = None
            safe_movie = sanitize_path_component(movie_name or "")
            
            # Primary: Use title from disc_titles table if available (prefer new format)
            if title_id_from_final_paths and title_id_to_title:
                raw = title_id_to_title.get(title_id_from_final_paths)
                base_name = sanitize_path_component(raw) if raw else None
                if base_name:
                    title_source = "disc_title"
            # Fallback to legacy format
            if not base_name and source_file_to_title and source_file:
                raw = source_file_to_title.get(source_file)
                base_name = sanitize_path_component(raw) if raw else None
                if base_name:
                    title_source = "disc_title"
            
            # For main movie: Use movie_name (Year) format (Plex/Jellyfin convention)
            if not base_name and title_type and title_type.lower() in ("mainmovie", "main", "movie"):
                if safe_movie:
                    base_name = safe_movie
                    if production_year:
                        base_name = f"{safe_movie} ({production_year})"
                    title_source = "movie_name"
            
            # Fallback: Use episode_name if available
            if not base_name and ep_name:
                base_name = sanitize_path_component(" ".join(ep_name.split("/")))
                if base_name:
                    title_source = "ep_name"
            
            # Last resort: Use Track{tid}. Earlier revisions fell back to
            # ``movie_name + (year)`` here to "avoid Track{tid}", but that
            # caused every NULL-typed primary to collide on the same
            # destination path (the 17/20 regression — N unlabeled rows
            # all writing to ``Movie (Year).mkv``). The main-movie branch
            # above (line ~1577) already covers ``title_type in
            # {mainmovie, main, movie}`` cases, which is gated on type and
            # safe by construction (at most one main per disc). Anything
            # else with no resolved title falls through to a unique
            # ``Track{tid}`` — ugly but distinct, matching the
            # ``_rename_series`` behavior at line ~1131.
            if not base_name:
                base_name = f"Track{tid}"
                title_source = "title_id_fallback"

            # Per-title edition suffix: Plex uses {edition-...}; Jellyfin uses " - [Edition]"
            edition_suffix = ""
            edition_str = None
            if title_id_from_final_paths and title_id_to_edition:
                edition_str = title_id_to_edition.get(title_id_from_final_paths)
            if edition_str and (edition_str := (edition_str or "").strip()):
                safe_edition = sanitize_path_component(edition_str) or edition_str
                if (media_server or "plex").lower() == "jellyfin":
                    edition_suffix = f" - [{safe_edition}]"
                else:
                    edition_suffix = f" {{edition-{safe_edition}}}"

            # Log base name source for debugging
            source_labels = {
                "disc_title": "disc_titles.title",
                "movie_name": "movie_name",
                "ep_name": "episode_name",
                "title_id_fallback": f"title_id (tid {tid})"
            }
            source_label = source_labels.get(title_source, title_source)
            logger.debug(f"_rename_movie: Base name: {base_name} (from {source_label})")

            # Optional resolution tag: Plex uses ".1080p"/".4k" before edition; Jellyfin uses "[1080p]/[2160p]"
            res_suffix = ""
            res = None
            if title_id_from_final_paths and title_id_to_resolution:
                res = title_id_to_resolution.get(title_id_from_final_paths)
            if not res:
                res = getattr(self, "resolution", None)
            if res:
                res_str = str(res).strip()
                if (media_server or "plex").lower() == "jellyfin":
                    res_norm = "2160p" if res_str.lower() == "4k" else res_str.lower()
                    res_suffix = f" [{res_norm}]"
                else:
                    res_norm = "4k" if res_str.lower() == "2160p" else res_str.lower()
                    res_suffix = f".{res_norm}"

            if (media_server or "plex").lower() == "jellyfin":
                base_name = f"{base_name}{edition_suffix}{res_suffix}"
            else:
                base_name = f"{base_name}{res_suffix}{edition_suffix}"

            new_name = sanitize_filepath(base_name + ".mkv")

            src = os.path.join(origin_folder, fn)
            dst = os.path.join(dest_dir, new_name)
            
            # Check if source file exists - if not, check if destination already exists (partial processing)
            src_exists = os.path.exists(src)
            dst_exists = os.path.exists(dst)
            src_size = os.path.getsize(src) if src_exists else 0
            dst_size = os.path.getsize(dst) if dst_exists else 0
            
            logger.info(f"_rename_movie: Processing {fn} ({src_size} bytes) -> {new_name} (source: {src}, dest: {dst})")
            logger.debug(f"_rename_movie: File {fn}: src_exists={src_exists}, dst_exists={dst_exists}, src={src}, dst={dst}")
            
            if not src_exists and dst_exists:
                # Source file doesn't exist but destination does - likely already processed
                # Verify hash if source_hashes is available
                if source_hashes and source_file:
                    try:
                        import hashlib
                        expected_hash = source_hashes.get(source_file)
                        if expected_hash:
                            # Calculate hash of existing destination file
                            hasher = hashlib.sha256()
                            with open(dst, 'rb') as f:
                                while chunk := f.read(8*1024*1024):
                                    hasher.update(chunk)
                            actual_hash = hasher.hexdigest()
                            
                            if actual_hash == expected_hash:
                                # Hash matches - file already processed correctly, skip move
                                files_processed += 1
                                if progress_cb:
                                    progress_cb(files_processed, total_files, fn)
                                logger.info(f"_rename_movie: Skipping {fn} - destination exists with matching hash (source: {src}, dest: {dst}, size: {dst_size} bytes)")
                                if self.log_fn:
                                    try:
                                        self.log_fn(f"[postprocess] Source file missing but destination exists with matching hash - skipping {src} -> {dst}")
                                    except Exception:
                                        pass
                                continue
                            else:
                                # Hash mismatch - log warning but continue
                                logger.warning(f"_rename_movie: Destination file exists but hash mismatch for {fn} (expected {expected_hash[:8]}..., got {actual_hash[:8]}...)")
                                if self.log_fn:
                                    try:
                                        self.log_fn(f"[postprocess] Warning: Destination file exists but hash mismatch for {src} -> {dst}")
                                    except Exception:
                                        pass
                        else:
                            # No hash available - assume file is already processed, skip move
                            files_processed += 1
                            if progress_cb:
                                progress_cb(files_processed, total_files, fn)
                            logger.info(f"_rename_movie: Skipping {fn} - destination exists (no hash to verify) (source: {src}, dest: {dst}, size: {dst_size} bytes)")
                            if self.log_fn:
                                try:
                                    self.log_fn(f"[postprocess] Source file missing but destination exists (no hash to verify) - skipping {src} -> {dst}")
                                except Exception:
                                    pass
                            continue
                    except Exception as hash_exc:
                        # Hash verification failed - log warning but skip move
                        logger.warning(f"_rename_movie: Failed to verify hash for existing destination file {dst}: {hash_exc}")
                        if self.log_fn:
                            try:
                                self.log_fn(f"[postprocess] Warning: Failed to verify hash for existing destination file {dst}: {hash_exc}")
                            except Exception:
                                pass
                        files_processed += 1
                        if progress_cb:
                            progress_cb(files_processed, total_files, fn)
                        continue
                elif dst_exists:
                    # Destination exists but no source_hashes - assume already processed, skip move
                    files_processed += 1
                    if progress_cb:
                        progress_cb(files_processed, total_files, fn)
                    logger.info(f"_rename_movie: Skipping {fn} - destination exists (no source_hashes provided) (source: {src}, dest: {dst}, size: {dst_size} bytes)")
                    if self.log_fn:
                        try:
                            self.log_fn(f"[postprocess] Source file missing but destination exists - skipping {src} -> {dst}")
                        except Exception:
                            pass
                    continue
                
                if not src_exists:
                    # Source doesn't exist and destination doesn't exist - this is an error
                    error_msg = f"Source file not found: {src} (and destination doesn't exist)"
                    logger.warning(f"_rename_movie: {error_msg}")
                if self.log_fn:
                    try:
                        self.log_fn(f"[postprocess] {error_msg}")
                    except Exception:
                        pass
                self.errors[base_name] = error_msg
                continue

            try:
                # Log file processing start
                logger.info(f"_rename_movie: Moving {fn} ({src_size} bytes) -> {new_name} (source: {src}, dest: {dst})")
                if self.log_fn:
                    try:
                        self.log_fn(f"[postprocess] Processing file {files_processed + 1}/{total_files}: {fn} -> {dst}")
                    except Exception:
                        pass
                
                # Create progress callback for this file if overall progress callback is provided
                file_progress_cb = None
                if progress_cb and total_files > 0:
                    def make_file_cb(file_idx: int, total: int, filename: str):
                        def file_cb(percent: int):
                            # percent is 0-100 for this file, convert to overall progress
                            overall_done = file_idx + (percent / 100.0)
                            progress_cb(int(overall_done), total, filename)
                        return file_cb
                    file_progress_cb = make_file_cb(files_processed, total_files, fn)
                
                move_with_progress(src, dst, log_fn=self.log_fn, progress_cb=file_progress_cb)
                files_processed += 1
                if progress_cb:
                    progress_cb(files_processed, total_files, fn)
                
                # Verify file was created and log completion
                if not os.path.exists(dst):
                    error_msg = f"ERROR: Destination file was not created after move: {dst}"
                    logger.error(f"_rename_movie: {error_msg}")
                    if self.log_fn:
                        try:
                            self.log_fn(f"[postprocess] {error_msg}")
                        except Exception:
                            pass
                else:
                    final_size = os.path.getsize(dst)
                    logger.info(f"_rename_movie: Moved {fn} -> {new_name} (source: {src}, dest: {dst}, source_size: {src_size} bytes, dest_size: {final_size} bytes)")
                    logger.debug(f"_rename_movie: Verified destination file exists: {dst}")
                    
                    # Capture the mapping: title_id -> final relative path (from transient root)
                    if title_id_from_final_paths and transient_root:
                        try:
                            # Calculate relative path from transient root to destination file
                            rel_path = os.path.relpath(dst, transient_root)
                            renamed_paths[str(title_id_from_final_paths)] = rel_path
                            logger.debug(f"_rename_movie: Captured mapping: title_id={title_id_from_final_paths} -> rel_path={rel_path}")
                        except Exception as rel_exc:
                            logger.warning(f"_rename_movie: Failed to calculate relative path for {dst}: {rel_exc}")
                
                # Log file processing completion
                if self.log_fn:
                    try:
                        self.log_fn(f"[postprocess] Completed file {files_processed}/{total_files}: {fn} -> {dst}")
                    except Exception:
                        pass
                logger.info(f"Completed file {files_processed}/{total_files}: {fn} -> {dst}")
            except Exception as e:
                error_msg = f"Failed to move {src} to {dst}: {e}"
                logger.error(f"_rename_movie: {error_msg}", exc_info=True)
                if self.log_fn:
                    try:
                        self.log_fn(f"[postprocess] Failed to move {src} -> {dst}: {e}")
                    except Exception:
                        pass
                self.errors[base_name] = f"Failed to move file: {e}"
        
        # Return mapping of title_id -> final relative path
        return renamed_paths
