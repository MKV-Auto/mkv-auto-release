# api/schemas.py
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import List, Optional, Dict, Any, Literal
from datetime import datetime
import os

from core.notification_preferences import default_notification_preferences, normalize_notification_preferences


class JobCreate(BaseModel):
    mount_point: str  # Required - identifies the drive
    disc_id: Optional[str] = None  # Optional - if disc exists in DB (backend can derive disc_num and disc_hash from it)
    disc_num: Optional[str] = None  # Optional - disc number from drive manager (avoids derivation if provided)
    mode: Optional[str] = 'copy'  # copy or rip
    output_dir: Optional[str] = None  # Optional output directory
    # Bypass the Path A threshold modal — set when the user has explicitly
    # picked "Rip whole disc anyway" on a Midway-class disc.
    force_full_rip: Optional[bool] = False
    # #578: bypass the USB-bus-saturation gate — set when the user has
    # explicitly acknowledged the bandwidth contention warning in the UI
    # and wants to proceed with concurrent rips on a sub-SuperSpeed bus
    # anyway. Defaults False; the gate's 409 carries the same field name
    # back so the frontend knows which flag to flip on retry.
    force_concurrent_on_saturated_bus: Optional[bool] = False
    # Removed: disc_hash, labelForm - backend derives these from disc_id or mount_point

class JobStatus(BaseModel):
    jobId: Optional[str] = None
    disc_id: Optional[str] = None
    release_id: Optional[str] = None
    movie_name: Optional[str] = None
    boxset_id: Optional[str] = None
    release_year: Optional[int] = None  # Boxset/release year (e.g., 2017)
    production_year: Optional[int] = None  # Movie production year (e.g., 2001, 2002, 2004, 2005)
    resolution: Optional[str] = None
    job_status: str
    scan_state: Optional[str] = None
    rip_progress: int
    rip_phase: Optional[str] = None  # "copy" | "verification" | null during rip
    post_progress: int
    logs: List[str]
    job_dir: Optional[str] = None  # Root job directory
    ripped_files: Optional[Dict[str, str]] = None  # title_id -> relative_path (files in raw/ after rip)
    post_paths: Optional[Dict[str, str]] = None  # title_id -> relative_path (files in transient/ after post-processing)
    artifacts: Optional[Dict[str, Any]] = None
    error_reason: Optional[str] = None
    transfer_paths: Optional[List[str]] = None
    transfer_error: Optional[str] = None
    transfer_progress: Optional[int] = None
    transfer_verification_hash: Optional[str] = None
    transfer_verification_status: Optional[str] = None
    transfer_retry_count: Optional[int] = None
    transfer_max_retries: Optional[int] = None
    transfer_speed_mbps: Optional[float] = None
    transfer_bytes_transferred: Optional[int] = None
    transfer_total_bytes: Optional[int] = None
    transfer_conflict_resolution: Optional[str] = None
    transfer_source_cleaned: Optional[bool] = None
    transfer_validation_status: Optional[str] = None
    transfer_validation_error: Optional[str] = None
    transfer_deduplicated: Optional[bool] = None
    # Sub-phase within the collapsed transfer stage (#365): "preparing" |
    # "transferring" | "verifying". Null on legacy jobs that predate the
    # collapse; the frontend's transferPhaseLabel falls back to the
    # transferState-based inference in that case.
    transfer_phase: Optional[str] = None
    # Backend-derived card contract (#839): the card renders these verbatim.
    card_state: Optional[str] = None
    card_family: Optional[str] = None  # your_turn | working | done | fix
    card_pill: Optional[str] = None
    stage_profile: Optional[str] = None
    discdb_result: Optional[str] = None
    pipeline: Optional[Dict[str, str]] = None
    phase: Optional[str] = None
    rip_state: Optional[str] = None
    rip_started_at: Optional[datetime] = None   # UTC when rip began (#344/#26)
    rip_completed_at: Optional[datetime] = None  # UTC when rip completed (#344/#26)
    label_state: Optional[str] = None
    finalize_state: Optional[str] = None
    post_state: Optional[str] = None
    transfer_state: Optional[str] = None
    finalize_release_state: Optional[str] = None
    titlesCompleted: Optional[int] = None
    totalTitles: Optional[int] = None
    currentTitleProgress: Optional[int] = None
    currentTitleId: Optional[str] = None
    currentTitleNumber: Optional[int] = None
    perTitleProgress: Optional[Dict[str, int]] = None
    perTitleStatus: Optional[Dict[str, str]] = None  # title_id -> "completed"|"running"|"pending"|"skipped" (derived from per_title_progress)
    disc_hash: Optional[str] = None
    disc_group: Optional[str] = None
    group_type: Optional[str] = None
    disc_payload: Optional[Dict[str, Any]] = None
    label_draft: Optional[Dict[str, Any]] = None  # kept for backward compatibility in SSE; always None now.
    label_required: Optional[bool] = None
    label_ready: Optional[bool] = None
    preview: Optional["PreviewInfo"] = None
    dev_mode: Optional[bool] = None
    dev_validation: Optional[Dict[str, Any]] = None
    export_path: Optional[str] = None
    workflow_step: Optional[str] = None  # Set on POST responses that advance step (rip, label/complete, postprocess, transfer, workflow/step/complete)
    # POST /jobs/rip only: True when a new job was created and task dispatched, False when returning an existing job
    job_created: Optional[bool] = None
    # Path A — segment-reorder state machine. Populated only on jobs running
    # the selective-rip workflow; null on every other job. Carries:
    #   stage: exploratory_ripping | awaiting_segment_order | matching_playlists |
    #          canonical_ripping_pending | cancelled | previews_failed
    #   exploratory_title_index, group_member_indexes, sorted_segment_key
    #   previews_manifest: list of PreviewSpec dicts (clip_name, path, etc.)
    #   submitted_order, matched_playlist_index
    segment_reorder_state: Optional[Dict[str, Any]] = None
    # Per-title rip set used by the selective-rip path. Null on default
    # all-mode rips. Frontend uses len(rip_set) to render "title K of N".
    rip_set: Optional[List[int]] = None

class JobResponse(BaseModel):
    jobId: str


class StepCompleteRequest(BaseModel):
    """Request to advance workflow_step only (no stage/phase changes)."""
    to_step: Literal["boxset", "disc", "titles", "postprocess", "transfer"]


class SegmentFlagPatchRequest(BaseModel):
    """Set or clear one clip's obfuscation flag on a disc.

    `flag = None` removes the flag for the clip. `'definitely'` excludes
    mpls containing that clip from subsequence-superset matches;
    `'potentially'` rank-deprioritises but does not exclude.
    """
    clip_id: str
    flag: Optional[Literal["potentially", "definitely"]] = None


class SegmentFlagPatchResponse(BaseModel):
    disc_id: str
    flags: Dict[str, Literal["potentially", "definitely"]]


class RemainingPlaylistSizeResponse(BaseModel):
    """Disk-pressure snapshot for the Path B iteration loop.

    `allows_rip_rest` is True when `remaining_size_b` fits under
    `threshold_b` AND there's at least one non-ignored, non-subsumed
    title left to rip. Drives the frontend's "Rip the rest" CTA.
    """
    disc_id: str
    remaining_size_b: int
    total_size_b: int
    ignored_count: int
    total_count: int
    free_disk_b: Optional[int] = None
    threshold_b: int
    allows_rip_rest: bool

class DriveInfo(BaseModel):
    disc_num: str
    mount_point: str
    makemkv_disc_index: Optional[str] = None
    drive_hardware_name: Optional[str] = None
    friendly_label: Optional[str] = None
    name: Optional[str] = None

class TrackInfo(BaseModel):
    type:          str
    # we persist season/episode as strings (often empty ""), so accept str here
    season:        Optional[str]
    episode:       Optional[str]
    format:        Optional[str]
    episode_name:  Optional[str]
    title: Optional[str] = Field(
        default=None,
        description="Canonical display name; synced with episode_name on drive payloads.",
    )

    @model_validator(mode="after")
    def _sync_track_display_name(self) -> "TrackInfo":
        raw_t = self.title if self.title is not None else ""
        raw_e = self.episode_name if self.episode_name is not None else ""
        t = str(raw_t).strip() if raw_t else ""
        e = str(raw_e).strip() if raw_e else ""
        eff = t or e or None
        if eff:
            self.title = eff
            self.episode_name = eff
        return self

class DiscDetail(BaseModel):
    # these *will* be injected below
    disc_num:      str
    mount_point:   str
    disc_id:       Optional[str] = None  # disc_id for linking to workflow context

    movie_name:    Optional[str] = ""  # Movie name (from movie.name), replaces legacy show_title
    release_image: Optional[str] = None  # Release cover image (from DiscDB release.imageUrl), replaces legacy show_image

    # any other fields load_db_info gives you can be included,
    # but at minimum you need tracks and the above two
    tracks:        Dict[str, TrackInfo]

    # if you also want to expose resolution, title_type, etc.:
    resolution:    Optional[str] = None
    title_type:    Optional[str] = None
    disc_hash:     Optional[str] = None
    disc_group:    Optional[str] = None
    disc_format:   Optional[str] = None
    release_year:  Optional[int] = None
    release_date:  Optional[str] = None
    original_year: Optional[int] = None
    original_release_date: Optional[str] = None
    info_title:    Optional[str] = None  # MakeMKV info_title from disc scan
    discdb_disc_num: Optional[int] = None  # TheDiscDB matched disc index (reference; not sequencing)
    # Titles map can include nested dicts (e.g., {"1": {"file": "00001.mpls"}}); accept anything.
    titles:        Dict[str, Any] = Field(default_factory=dict)
    # TMDB auto-suggestion (#388) persisted on disc.disc_info.tmdb_suggestion.
    # The film step (#389) reads this to render the suggestion card. Pydantic
    # would drop unknown fields, so the type must be declared explicitly here —
    # accept ``Any`` for forward-compat (candidates list etc. are nested dicts).
    tmdb_suggestion: Optional[Dict[str, Any]] = None

class DiscMetadata(BaseModel):
    """Basic disc metadata for card display (used by Workflow Coordinator)."""
    disc_id: str
    disc_num: Optional[str] = None
    mount_point: Optional[str] = None
    disc_hash: Optional[str] = None
    disc_state: Literal['in_drive', 'unfinished']  # Disc state: in drive or unfinished
    job_id: Optional[str] = None  # For unfinished discs, the associated job_id
    job_status: Optional[str] = None  # Job row status when job_id is set (e.g. failed on in-drive card)
    # Scan state management
    scan_state: Optional[Literal['pending', 'scanning', 'ready', 'failed']] = None  # Disc scan state
    scan_error: Optional[str] = None  # Error message if scan failed
    # Card display metadata
    movie_name: Optional[str] = None
    release_name: Optional[str] = None  # Release title (for card display)
    # The disc's own name/slug. Auto-renamed at label time (#845), so the
    # disc_metadata_updated event must carry them or every surface shows the
    # stale pre-label name until a hard refresh.
    disc_name: Optional[str] = None
    disc_slug: Optional[str] = None
    info_title: Optional[str] = None  # MakeMKV info_title from disc scan
    disc_number: Optional[int] = None  # Ordinal in release (e.g. Disc 1, 2) for card title
    discdb_disc_num: Optional[int] = None  # TheDiscDB matched disc index (reference only)
    # The disc's own season, present only when its release spans multiple
    # seasons (#846) — the card renders it as "S2".
    disc_season: Optional[int] = None
    release_image: Optional[str] = None
    disc_format: Optional[str] = None
    resolution: Optional[str] = None
    release_year: Optional[int] = None
    production_year: Optional[int] = None
    last_modified_at: Optional[datetime] = None  # For multi-device sync
    created_at: Optional[datetime] = None  # Job creation time for unfinished discs (rip creation time)
    has_completed_job: Optional[bool] = None  # True when a completed job exists for this disc (#356)
    # Backend-derived card contract for unfinished-job cards (#839).
    card_state: Optional[str] = None
    card_family: Optional[str] = None
    card_pill: Optional[str] = None
    card_progress: Optional[int] = None
    # #603: When the inserted disc's content_hash matches a row that's already
    # finalized in the Library, the carousel collapses the usual "Now Reading"
    # treatment into a single "Already in Library" card with a Re-rip button.
    # All three fields populate together — finalized=True implies the others
    # are set (or None if join failed defensively).
    finalized: Optional[bool] = None
    finalized_release_id: Optional[str] = None
    finalized_release_name: Optional[str] = None
    finalized_release_slug: Optional[str] = None

class CurrentJobResponse(BaseModel):
    jobId:      str
    createdAt:  datetime
    disc:       DiscDetail
    job_status: str
    rip_progress: int
    rip_phase: Optional[str] = None
    post_progress: int

class DiscJobState(BaseModel):
    disc: DiscDetail
    job: Optional[JobStatus] = None


class MakeMKVInfo(BaseModel):
    version: Optional[str]
    binary_path: str
    resolved_path: Optional[str] = None
    binary_sha256: Optional[str] = None
    binary_mtime: Optional[float] = None


class MakeMKVUpdateRequest(BaseModel):
    version: Optional[str] = None
    build_ffmpeg: bool = True
    ffmpeg_advanced_features: bool = True
    install_prefix: Optional[str] = None
    work_dir: Optional[str] = None
    use_root_helper: bool = False

    @field_validator("version")
    @classmethod
    def version_must_not_be_blank(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not v.strip():
            raise ValueError("version must not be empty")
        return v.strip()


class MakeMKVUpdateResponse(BaseModel):
    version: str
    ffmpeg_built: bool
    logs: List[str]


class MakeMKVUpdateJobResponse(BaseModel):
    jobId: str


class MakeMKVUpdateJobStatus(BaseModel):
    jobId: str
    status: str  # pending, running, completed, failed
    logs: List[str]
    error: Optional[str] = None
    version: Optional[str] = None
    createdAt: datetime
    updatedAt: datetime


class MakeMKVUpdateActiveResponse(BaseModel):
    """Response for GET /system/makemkv/update/active: in-progress job if any."""
    active: bool
    jobId: Optional[str] = None
    status: Optional[str] = None
    logs: Optional[List[str]] = None
    error: Optional[str] = None
    version: Optional[str] = None
    createdAt: Optional[datetime] = None
    updatedAt: Optional[datetime] = None


class LatestVersionResponse(BaseModel):
    version: str


class MakeMKVRegistrationStatus(BaseModel):
    expired: bool
    message: Optional[str] = None
    currentKey: Optional[str] = None


class InformativeCategoryChannels(BaseModel):
    model_config = ConfigDict(extra="forbid")
    in_app: bool = True
    discord: bool = True


class InformativePreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    categories: Dict[str, InformativeCategoryChannels]


class TypeChannelPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    in_app: bool = True
    discord: bool = True


class NotificationPreferencesSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    informative: InformativePreferences
    action_required: TypeChannelPreferences
    errors: TypeChannelPreferences

    @model_validator(mode="before")
    @classmethod
    def _normalize_prefs(cls, data: Any):
        if isinstance(data, dict):
            return normalize_notification_preferences(data)
        return data


class DiscordSettings(BaseModel):
    """GET returns full notification_preferences; POST may omit it to leave stored prefs unchanged."""

    webhook_url: Optional[str] = None
    enabled: bool = False
    notification_preferences: Optional[NotificationPreferencesSchema] = None
    # Deep-link base for notification links (#841); stored app-wide, surfaced
    # here because Settings → Notifications is where the user meets it.
    base_url: Optional[str] = None

    @staticmethod
    def defaults() -> "DiscordSettings":
        return DiscordSettings(
            webhook_url=None,
            enabled=False,
            notification_preferences=NotificationPreferencesSchema.model_validate(
                default_notification_preferences()
            ),
        )


class PreviewSettings(BaseModel):
    duration_seconds: int = Field(default=120, gt=0)
    # Hard ceiling (le=128) protects against absurd values regardless of host;
    # the practical ceiling (max_parallel_ceiling) is server-CPU-derived and
    # enforced at save time by the router. The slider in the UI binds [max] to
    # max_parallel_ceiling so the thumb always agrees with the persisted value.
    max_parallel: int = Field(default=1, gt=0, le=128)
    disable_ffmpeg_junk_detection: Optional[bool] = Field(default=False)
    # Read-only response field. Source of truth = the server's os.cpu_count().
    # The client uses it as the slider's [max]. POST requests echo it back but
    # the router ignores any client-supplied value.
    max_parallel_ceiling: int = Field(default_factory=lambda: max(1, os.cpu_count() or 1), gt=0, le=128)

    @staticmethod
    def defaults() -> "PreviewSettings":
        ceiling = max(1, os.cpu_count() or 1)
        return PreviewSettings(
            duration_seconds=120,
            max_parallel=ceiling,
            disable_ffmpeg_junk_detection=False,
            max_parallel_ceiling=ceiling,
        )


class MediaServerSettings(BaseModel):
    media_server: Literal["plex", "jellyfin"] = "plex"


class SupportPromptStatus(BaseModel):
    """Whether the bell-panel "support the project" prompt should be shown."""

    should_show: bool = False
    completed_rips: int = 0
    dismissed_forever: bool = False


class SupportPromptDismissRequest(BaseModel):
    """``forever`` silences the prompt for good; otherwise it snoozes."""

    forever: bool = False


class DiscDbLookupSettings(BaseModel):
    """Copy settings: DiscDB prefill toggle and eject-on-finish toggle."""

    discdb_miss_workflow_with_prefill: bool = False
    eject_on_finish: bool = False


# Short artifact listing
class JobArtifacts(BaseModel):
    jobId: str
    job_dir: Optional[str] = None
    ripped_files: Optional[Dict[str, str]] = None  # title_id -> relative_path (files in raw/ after rip)
    post_paths: Optional[Dict[str, str]] = None  # title_id -> relative_path (files in transient/ after post-processing)

class JobListItem(BaseModel):
    jobId: str
    disc_num: str
    mount_point: str
    disc_hash: Optional[str] = None
    disc_group: Optional[str] = None
    release_id: Optional[str] = None
    release_slug: Optional[str] = None
    group_type: Optional[str] = None
    job_status: str
    scan_state: Optional[str] = None
    mode: str
    rip_progress: int
    rip_phase: Optional[str] = None
    post_progress: int
    created_at: datetime
    updated_at: datetime
    job_dir: Optional[str] = None
    show_title: Optional[str] = None
    movie_name: Optional[str] = None
    transfer_progress: Optional[int] = None
    pipeline: Optional[Dict[str, str]] = None
    phase: Optional[str] = None
    discdb_hit: Optional[bool] = None
    titles_completed: Optional[int] = None
    total_titles: Optional[int] = None
    per_title_progress: Optional[Dict[str, int]] = None
    dev_mode: Optional[bool] = None
    dev_validation: Optional[Dict[str, Any]] = None
    export_path: Optional[str] = None
    resolution: Optional[str] = None
    release_year: Optional[int] = None

class ReleaseProgress(BaseModel):
    release_slug: str
    total_discs: int
    completed_discs: int
    finalized_discs: int
    finalize_state: Optional[str] = None


class LibraryResponse(BaseModel):
    """Pre-structured payload for the Library (History) page: releases with discs, boxsets with details."""
    releases: List["ReleaseSummary"] = []
    release_discs: Dict[str, List["DiscSummary"]] = {}
    boxsets: List["BoxsetSummary"] = []
    boxset_details: List["BoxsetRecord"] = []


class LibraryReattachMatch(BaseModel):
    """One row in the reattach report: a DiscTitle ↔ on-disk MKV match
    that the endpoint identified (and either applied or would apply in
    dry_run mode)."""
    title_id: str
    old_path: Optional[str] = None
    new_path: str
    # "segment_uid" (deterministic) | "filename" | "uri" | "hash"
    tier: str


class LibraryReattachConflict(BaseModel):
    """A file at the destination that matches more than one DiscTitle —
    operator must disambiguate (e.g. duplicate segment_uids from a
    re-rip of the same source). Reported but not applied."""
    file_path: str
    candidate_title_ids: List[str] = []
    tier: str


class LibraryReattachReport(BaseModel):
    """Self-healing library reattach report (#449).

    Produced by ``POST /releases/library/reattach``. ``dry_run=true`` (the
    default) returns the report without writing; ``dry_run=false`` applies
    the deterministic + heuristic matches via
    :func:`workers.tasks._update_title_file_paths` and re-returns the
    report so the caller can show what happened.

    Fields:
      * ``deterministic_matches`` — segment_uid hits (the trust path)
      * ``heuristic_matches`` — filename / URI / hash fallback for titles
        whose ``segment_uid IS NULL`` (legacy rows from before PR #451)
      * ``orphan_files`` — MKVs at the destination with no matching title
      * ``orphan_titles`` — DiscTitle.id rows with no on-disk match
      * ``conflicts`` — files matching multiple titles (skipped, reported)
      * ``transfer_dir`` — the walked directory (for the UI to display)
      * ``dry_run`` — echo of the input flag
      * ``applied`` — true when writes occurred (dry_run=false + matches existed)
    """
    deterministic_matches: List[LibraryReattachMatch] = []
    heuristic_matches: List[LibraryReattachMatch] = []
    orphan_files: List[str] = []
    orphan_titles: List[str] = []
    conflicts: List[LibraryReattachConflict] = []
    transfer_dir: str
    dry_run: bool
    applied: bool = False


# ────────────────────────────────────────────────────────────────────────
# #325 — Rename-after-postprocess response models
# ────────────────────────────────────────────────────────────────────────

class RenamePreviewEntry(BaseModel):
    """One row of the rename preview/apply response.

    ``status`` semantics (driven by ``Backend/api/routers/releases.py:rename_disc_titles``):
      * ``preview`` — dry-run: this is what *will* happen if applied.
      * ``renamed`` — execute mode: the file was successfully moved.
      * ``collision`` — destination already exists (and isn't the source);
        execute would clobber, so the row is skipped.
      * ``missing`` — execute mode: source file disappeared between preview
        and apply.
      * ``error`` — execute mode: the move call failed (permissions, I/O).

    ``changed`` is ``False`` when old_path resolves to the same file as
    new_path (idempotent re-run). UI hides unchanged rows from the
    "files will change" count.
    """
    title_id: str
    old_path: str
    new_path: str
    changed: bool
    status: Literal["preview", "renamed", "collision", "missing", "error"]
    error: Optional[str] = None


class RenameResponse(BaseModel):
    """Response from ``POST /releases/disc/{disc_id}/rename`` (#325).

    Returned for both dry-run preview and execute. ``dry_run`` echoes the
    input flag so the UI can render the correct CTA ("Apply" vs "Done")
    without tracking it client-side.
    """
    disc_id: str
    dry_run: bool
    results: List[RenamePreviewEntry]


class LibraryPageResponse(BaseModel):
    """Paginated library response for infinite scroll."""
    items: List["ReleaseSummary"] = []
    release_discs: Dict[str, List["DiscSummary"]] = {}
    boxsets: List["BoxsetSummary"] = []
    boxset_details: List["BoxsetRecord"] = []
    next_cursor: Optional[str] = None
    has_more: bool = False
    total_count: Optional[int] = None


class MovieSummary(BaseModel):
    id: Optional[str] = None
    name: str
    production_year: Optional[int] = None
    tmdb_id: Optional[str] = None
    tmdb_type: Optional[str] = None
    cover_url: Optional[str] = None
    cover_path: Optional[str] = None


class MovieRecord(BaseModel):
    id: str
    name: str
    production_year: Optional[int] = None
    tmdb_id: Optional[str] = None
    tmdb_type: Optional[str] = None
    cover_url: Optional[str] = None
    cover_path: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ReleaseSummary(BaseModel):
    id: Optional[str] = None
    slug: str
    type: Optional[str] = None
    name: Optional[str] = None
    release_name: Optional[str] = None  # Release edition name (rel.name); for Library release cards under movie/series
    movie_id: Optional[str] = None
    movie: Optional[MovieSummary] = None
    tmdb_id: Optional[str] = None
    upc: Optional[str] = None
    asin: Optional[str] = None
    cover_front_url: Optional[str] = None
    cover_back_url: Optional[str] = None
    title_cover_url: Optional[str] = None
    finalize_state: Optional[str] = None
    finalized: Optional[bool] = None
    finalized_at: Optional[datetime] = None
    total_discs: int = 0
    completed_discs: int = 0
    finalized_discs: int = 0
    resolution: Optional[str] = None
    release_year: Optional[int] = None
    original_year: Optional[int] = None
    production_year: Optional[int] = None
    discdb_hit: Optional[bool] = None
    boxset_id: Optional[str] = None
    boxset_slug: Optional[str] = None
    # Link readiness (standalone vs parent boxset rules); None when not computed
    release_link_ready: Optional[bool] = None
    release_missing_required_fields: Optional[List[str]] = None
    modified: Optional[bool] = None


class BoxsetSummary(BaseModel):
    id: Optional[str] = None
    slug: str
    name: Optional[str] = None
    title: Optional[str] = None
    sort_title: Optional[str] = None
    upc: Optional[str] = None
    asin: Optional[str] = None
    year: Optional[int] = None
    locale: Optional[str] = None
    region_code: Optional[str] = None
    cover_front_url: Optional[str] = None
    cover_back_url: Optional[str] = None
    image_url: Optional[str] = None
    release_date: Optional[datetime] = None
    finalized: bool = False
    finalized_at: Optional[datetime] = None
    release_count: int = 0
    modified: Optional[bool] = None
    # Link readiness (same rules as disc linking); None when not computed
    boxset_link_ready: Optional[bool] = None
    boxset_missing_required_fields: Optional[List[str]] = None


class BoxsetRecord(BaseModel):
    id: Optional[str] = None
    slug: str
    name: Optional[str] = None
    title: Optional[str] = None
    sort_title: Optional[str] = None
    upc: Optional[str] = None
    asin: Optional[str] = None
    year: Optional[int] = None
    locale: Optional[str] = None
    region_code: Optional[str] = None
    cover_front_url: Optional[str] = None
    cover_back_url: Optional[str] = None
    image_url: Optional[str] = None
    release_date: Optional[datetime] = None
    finalized: bool = False
    finalized_at: Optional[datetime] = None
    finalize_result: Optional[Dict[str, Any]] = None
    releases: Optional[List[ReleaseSummary]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class BoxsetCreate(BaseModel):
    name: str = Field(..., description="Boxset name/title (required)")
    title: Optional[str] = None
    sort_title: Optional[str] = None
    year: int = Field(..., description="Boxset release year (required, must be 4-digit: 1000-9999)")
    upc: str = Field(..., description="UPC (required, must be exactly 12 numeric digits)")
    asin: Optional[str] = None
    locale: Optional[str] = None
    region_code: Optional[str] = None
    cover_front_url: str = Field(..., description="Front cover URL (required, must be valid http:// or https:// URL)")
    cover_back_url: Optional[str] = None
    release_date: Optional[datetime] = None


class BoxsetUpdate(BaseModel):
    name: Optional[str] = None
    title: Optional[str] = None
    sort_title: Optional[str] = None
    year: Optional[int] = None
    upc: Optional[str] = None
    asin: Optional[str] = None
    locale: Optional[str] = None
    region_code: Optional[str] = None
    cover_front_url: Optional[str] = None
    cover_back_url: Optional[str] = None
    release_date: Optional[datetime] = None


class TitleSummary(BaseModel):
    """Per-title row projection on the Library response (#380 / #500).

    Subset of ``DiscTitleRecord`` carrying just the fields the Library page
    drawer needs to render and edit. Importantly includes ``file_path`` +
    ``file_path_stage`` so the drawer can show *where the bytes actually
    landed* — the leaf-level answer to "where did my Midway MKV go?". Also
    includes ``title_seq`` so the drawer's optimistic-edit pattern (#383)
    has a starting seq for PATCH payloads without an extra round-trip.
    """
    title_id: str
    title: Optional[str] = None
    type: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    edition: Optional[str] = None
    description: Optional[str] = None
    duration: Optional[float] = None
    size: Optional[int] = None
    mkv_size: Optional[int] = None
    # The transferred-file destination is the same column as the rip-output
    # path — `file_path_stage` tells you which stage last wrote it.
    file_path: Optional[str] = None
    file_path_stage: Optional[str] = None  # 'rip' | 'postprocess' | 'transfer'
    title_seq: int = 0
    active: Optional[bool] = None


class DiscSummary(BaseModel):
    id: Optional[str] = None
    content_hash: str
    release_id: Optional[str] = None
    release_slug: Optional[str] = None
    disc_number: Optional[int] = None
    discdb_disc_num: Optional[int] = None
    disc_slug: Optional[str] = None
    disc_name: Optional[str] = None
    format: Optional[str] = None
    label_present: bool = False
    finalized: bool = False
    finalized_at: Optional[datetime] = None
    latest_job_id: Optional[str] = None
    latest_job_status: Optional[str] = None
    scan_state: Optional[str] = None
    latest_job_progress: Optional[int] = None
    latest_pipeline: Optional[Dict[str, str]] = None
    latest_phase: Optional[str] = None
    latest_job_updated_at: Optional[datetime] = None
    transfer_state: Optional[str] = None
    discdb_hit: Optional[bool] = None
    titles_completed: Optional[int] = None
    total_titles: Optional[int] = None
    per_title_progress: Optional[Dict[str, int]] = None
    tracks: Optional[List[Dict[str, Any]]] = None  # Job disc_payload tracks (MakeMKV titles map), not DB streams
    title_streams: Optional[List[Dict[str, Any]]] = None  # Persisted stream rows from title_streams table
    # #380: Library-side per-title projection (typed). Populated from
    # `Disc.titles` (DiscTitle rows) in `_build_disc_summaries_for_release`.
    # Was an unpopulated Optional[List[Dict[str, Any]]] before.
    # #530: the Library page response omits this (count below instead);
    # GET /releases/{slug}/discs still ships it.
    titles: Optional[List[TitleSummary]] = None
    # #530: persisted-title count for the Library card meta line — lets the
    # page response drop the inline `titles` arrays (~1.1MB on real data).
    title_count: Optional[int] = None
    finalize_result: Optional[Dict[str, Any]] = None
    artifacts: Optional[Dict[str, Any]] = None


class MakeMKVRegistrationRequest(BaseModel):
    key: str

class StorageInfo(BaseModel):
    path: str
    total: int
    used: int
    free: int

class StorageSummary(BaseModel):
    data_root: StorageInfo
    transfer_root: StorageInfo

class StorageDirEntry(BaseModel):
    name: str
    path: str
    is_dir: bool = True

class MkdirRequest(BaseModel):
    path: str
    name: str


class RsyncConfig(BaseModel):
    host: str
    user: str
    path: str
    port: int = 22
    bwlimit: Optional[int] = None


class RsyncConfigResponse(BaseModel):
    config: Optional[RsyncConfig] = None
    hasKey: bool = False

# Transfer config schemas
class TransferConfigCreate(BaseModel):
    mode: Literal["local", "rsync", "smb", "nfs"]
    name: Optional[str] = None
    transfer_dir: Optional[str] = None
    output_dir: Optional[str] = None
    path_template: Optional[str] = None
    path_template_schema_version: Optional[str] = None  # Ignored on create; backend sets from path_templates.PATH_TEMPLATE_SCHEMA_VERSION
    config_data: Optional[Dict[str, Any]] = None
    conflict_resolution: Literal["overwrite", "skip", "rename", "fail"] = "overwrite"
    health_check_interval_minutes: Optional[int] = None
    credentials: Optional[Dict[str, str]] = None


class TransferConfigUpdate(BaseModel):
    name: Optional[str] = None
    transfer_dir: Optional[str] = None
    output_dir: Optional[str] = None
    path_template: Optional[str] = None
    path_template_schema_version: Optional[str] = None  # Ignored on update; backend sets when path_template is updated
    config_data: Optional[Dict[str, Any]] = None
    conflict_resolution: Optional[Literal["overwrite", "skip", "rename", "fail"]] = None
    health_check_interval_minutes: Optional[int] = None
    credentials: Optional[Dict[str, str]] = None


class TransferCapabilities(BaseModel):
    """Destination-capability probe result (#635 commit B).

    Cached on ``TransferConfig.config_data['capabilities']``; the
    strategy selector in ``core.transfer.service.resolve_transfer_plan``
    consumes this to decide the actual protocol primitive."""
    can_write_new: bool = False
    can_overwrite_in_place: bool = False
    can_delete: bool = False
    can_rename: bool = False
    probed_at: str = ""
    probe_error: Optional[str] = None
    notes: Optional[Dict[str, Any]] = None


class TransferConfigSummary(BaseModel):
    id: str
    mode: str
    name: Optional[str] = None
    is_active: bool
    transfer_dir: Optional[str] = None
    path_template: Optional[str] = None
    path_template_schema_version: Optional[str] = None
    conflict_resolution: str
    health_check_interval_minutes: Optional[int] = None
    health_status: Optional[str] = None  # "healthy", "degraded", "unhealthy", "unknown"
    capabilities: Optional[TransferCapabilities] = None
    created_at: str
    updated_at: str


class TransferConfigRecord(BaseModel):
    id: str
    mode: str
    name: Optional[str] = None
    is_active: bool
    transfer_dir: Optional[str] = None
    output_dir: Optional[str] = None
    path_template: Optional[str] = None
    path_template_schema_version: Optional[str] = None
    config_data: Optional[Dict[str, Any]] = None
    conflict_resolution: str
    health_check_interval_minutes: Optional[int] = None
    capabilities: Optional[TransferCapabilities] = None
    created_at: str
    updated_at: str
    # Credentials are never returned in API responses


class ValidationResult(BaseModel):
    success: bool
    message: str
    errors: Optional[List[str]] = None


class TransferHistorySummary(BaseModel):
    id: str
    job_id: Optional[str] = None
    transfer_config_id: Optional[str] = None
    mode: str
    source_path: str
    destination_path: str
    status: str
    bytes_transferred: Optional[int] = None
    transfer_duration_seconds: Optional[float] = None
    average_speed_mbps: Optional[float] = None
    verification_status: Optional[str] = None
    was_deduplicated: bool
    created_at: str
    # #593: human-readable identity resolved server-side via Job → Disc →
    # Release → Movie. All None for orphaned rows where job_id was set NULL
    # after a job deletion; the UI falls back to the source-path parser then
    # to the UUID in that case.
    movie_name: Optional[str] = None
    release_name: Optional[str] = None
    release_year: Optional[int] = None
    disc_name: Optional[str] = None


class TransferStatistics(BaseModel):
    total_transfers: int
    completed: int
    failed: int
    deduplicated: int
    success_rate: float
    average_speed_mbps: float
    total_bytes_transferred: int
    period_days: int


class HealthCheckResult(BaseModel):
    check_type: str
    status: str
    message: Optional[str] = None
    response_time_ms: Optional[int] = None


class TransferHealthStatus(BaseModel):
    overall: Optional[HealthCheckResult] = None
    connectivity: Optional[HealthCheckResult] = None
    authentication: Optional[HealthCheckResult] = None
    permissions: Optional[HealthCheckResult] = None
    space: Optional[HealthCheckResult] = None


class PreviewTrackStatus(BaseModel):
    status: Literal["queued", "running", "ready", "failed"]
    manifest: Optional[str] = None
    error: Optional[str] = None


class PreviewInfo(BaseModel):
    status: Literal["queued", "running", "ready", "failed"]
    tracks: Dict[str, PreviewTrackStatus] = Field(default_factory=dict)
    queue_position: Optional[int] = None
    updated_at: Optional[str] = None


# resolve forward refs
JobStatus.model_rebuild()


class TransferRequest(BaseModel):
    type: Optional[str] = Field(default=None, pattern="^(local|rsync)$")
    target_dir: Optional[str] = None
    rsync: Optional[RsyncConfig] = None
    use_saved_rsync: bool = True


class TitleLabel(BaseModel):
    track_id: Optional[str] = Field(None, description="MakeMKV track id / playlist filename")
    title_id: Optional[str] = Field(None, description="Alternate title identifier", alias="title_id")
    source_file: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = Field(None, alias="description")
    comment: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    type: Optional[
        Literal[
            "episode",
            "movie",
            "main",
            "MainMovie",
            "Episode",
            "extra",
            "Extra",
            "trailer",
            "Trailer",
            "deleted",
            "deletedscene",
            "DeletedScene",
            "ignore",
            "BehindTheScenes",
            "behindthescenes",
            "Featurette",
            "featurette",
            "Interview",
            "interview",
            "Scene",
            "scene",
            "Short",
            "short",
            "Other",
            "other",
            "Sample",
            "sample",
            "Clip",
            "clip",
            "ThemeMusic",
            "thememusic",
            "theme-music",
            "Backdrop",
            "backdrop",
        ]
    ] = None
    note: Optional[str] = None
    duration: Optional[float] = None
    size: Optional[int] = None
    streams: Optional[Dict[str, Any] | List[Any]] = None
    content: Optional[bool] = True
    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def _ensure_ids(self):
        # Prefer explicit track_id; fall back to title_id for back-compat.
        if not self.track_id and self.title_id:
            self.track_id = self.title_id
        if not self.source_file:
            self.source_file = self.track_id or self.title_id
        if not self.track_id:
            raise ValueError("track_id is required")
        # Backfill description from note for older payloads.
        if self.description is None and self.note is not None:
            self.description = self.note
        if self.note is None and self.description is not None:
            self.note = self.description
        if self.comment is None and self.note is not None:
            self.comment = self.note
        return self


class MovieLookupRequest(BaseModel):
    tmdb_url: str


class TmdbConfigRequest(BaseModel):
    """Set/clear the TMDB v3 API key. Empty string or null clears the key."""
    api_key: Optional[str] = None


class TmdbConfigResponse(BaseModel):
    """Current TMDB configuration state. api_key_set indicates a key is configured.

    #610: api_key now echoes the persisted value so the Settings → TMDB field
    can pre-populate (mirrors how MakeMKV registration echoes ``currentKey``).
    Both keys live on the same disk under the same trust boundary; the prior
    asymmetric "never echo TMDB but echo MakeMKV" posture created UX confusion
    (the field rendered empty even when configured) without delivering
    meaningful security. ``api_key_set`` stays for backward compatibility and
    for clients that still want the boolean status indicator.

    backfill (only present on POST) reports how many unlabeled discs received
    a fresh tmdb_suggestion when the key was just saved — lets the UI show
    'Found N suggestions for existing discs.'"""
    api_key_set: bool
    api_key: Optional[str] = None
    backfill: Optional[dict] = None


class TmdbSearchRequest(BaseModel):
    """Search TMDB by title text (with normalization). Distinct from /movies/search
    (DB autocomplete) and /movies/lookup (URL-paste scrape)."""
    query: str
    year_hint: Optional[int] = None
    media_type: Optional[Literal["movie", "tv"]] = None
    limit: int = 3


class TmdbSearchCandidate(BaseModel):
    tmdb_id: str
    tmdb_type: Literal["movie", "tv"]
    title: str
    year: Optional[int] = None
    cover_url: Optional[str] = None
    score: float


class TmdbSearchResponse(BaseModel):
    candidates: List[TmdbSearchCandidate]
    normalized_query: str
    hints: dict


class TmdbEpisodeSummary(BaseModel):
    """One episode from the TMDB TV season catalog (#368).

    Shape mirrors ``core.tmdb_client.TmdbEpisode`` — the labeling UI fills
    season / episode / title fields from these; still_url is shown as a
    small thumbnail in the dropdown."""
    season_number: int
    episode_number: int
    name: str
    overview: Optional[str] = None
    air_date: Optional[str] = None
    runtime: Optional[int] = None
    still_url: Optional[str] = None


class TmdbSeasonEpisodesResponse(BaseModel):
    """Per-season episode catalog + show-level metadata folded in (#368).

    ``number_of_seasons`` and ``series_name`` come from a second TMDB call
    (`/tv/{id}`); both are cached LRU per process so the cost is one
    network round-trip the first time, free after that. The frontend
    needs them on the first fetch to bound the disc-card primary-season
    selector (#371)."""
    tmdb_id: str
    season_number: int
    episodes: List[TmdbEpisodeSummary]
    number_of_seasons: int = 1
    series_name: Optional[str] = None


class MovieCreate(BaseModel):
    name: str
    production_year: Optional[int] = None
    tmdb_id: Optional[str] = None
    tmdb_type: Optional[str] = None
    cover_url: Optional[str] = None


class MovieUpdate(BaseModel):
    name: Optional[str] = None
    production_year: Optional[int] = None
    tmdb_id: Optional[str] = None
    tmdb_type: Optional[str] = None
    cover_url: Optional[str] = None
    cover_path: Optional[str] = None


class LabelRequest(BaseModel):
    boxset_slug: Optional[str] = None
    boxset_id: Optional[str] = None
    mode: Literal["movie", "series"]
    movie_id: Optional[str] = None
    tmdb_id: Optional[str] = Field(None, description="TMDB numeric id (deprecated, use movie_id)")
    disc_format: Literal["Blu-Ray", "UHD", "DVD"]
    disc_number: Optional[int] = None
    release_slug: Optional[str] = None
    release_name: Optional[str] = None
    release_year: Optional[int] = None
    original_year: Optional[int] = None
    production_year: Optional[int] = None
    upc: Optional[str] = None
    asin: Optional[str] = None
    cover_front_url: Optional[str] = None
    cover_back_url: Optional[str] = None
    disc_name: Optional[str] = None
    disc_slug: Optional[str] = None
    titles: List[TitleLabel] = Field(default_factory=list)


class LabelUpdate(BaseModel):
    """
    Partial label update payload for auto-save on field blur.
    Accepts any subset of LabelRequest fields and merges into stored label_payload.
    """
    data: Dict[str, Any]


class ReleaseMetadataPatch(BaseModel):
    release_id: Optional[str] = None
    release_slug: Optional[str] = None
    release_name: Optional[str] = None
    release_year: Optional[int] = None
    original_year: Optional[int] = None
    production_year: Optional[int] = None
    info_title: Optional[str] = None
    tmdb_id: Optional[str] = None
    upc: Optional[str] = None
    asin: Optional[str] = None
    cover_front_url: Optional[str] = None
    cover_back_url: Optional[str] = None
    group_type: Optional[str] = None
    mode: Optional[str] = None
    boxset_id: Optional[str] = None


class DiscMetadataPatch(BaseModel):
    disc_number: Optional[int] = None
    disc_slug: Optional[str] = None
    disc_name: Optional[str] = None
    disc_format: Optional[str] = None
    disc_group: Optional[str] = None
    release_id: Optional[str] = None
    info_title: Optional[str] = None


class DiscMetadataUpdate(BaseModel):
    """
    Dedicated payload for persisting release/disc/track metadata (autosave).
    """
    release: Optional[ReleaseMetadataPatch] = None
    disc: Optional[DiscMetadataPatch] = None
    titles: List[TitleLabel] = Field(default_factory=list)
    tracks: Optional[List[TitleLabel]] = Field(default=None, description="Legacy track list alias for titles")


class PatchOp(BaseModel):
    target: Literal["release", "disc", "title", "stream", "label_draft"]
    id: Optional[str] = None
    fields: Dict[str, Any]


class PatchRequest(BaseModel):
    ops: List[PatchOp]


class TitleStreamRecord(BaseModel):
    """One row in title_streams: a video/audio/sub stream under a disc title."""

    id: Optional[str] = None
    disc_id: Optional[str] = None
    title_id: Optional[str] = None
    stream_index: Optional[int] = None
    stream_type: Optional[str] = None
    audio_type: Optional[str] = None
    language_code: Optional[str] = None
    language: Optional[str] = None
    codec_short: Optional[str] = None
    codec_hint: Optional[str] = None
    name: Optional[str] = None
    bitrate: Optional[str] = None
    channels: Optional[int] = None
    sample_rate: Optional[str] = None
    bit_depth: Optional[str] = None
    resolution: Optional[str] = None
    aspect_ratio: Optional[str] = None
    frame_rate: Optional[str] = None
    reference_frames: Optional[str] = None
    description: Optional[str] = None
    info: Optional[str] = None
    duration_seconds: Optional[float] = None
    flag: Optional[str] = None
    default: Optional[bool] = None
    layout: Optional[str] = None
    title: Optional[str] = None
    note: Optional[str] = None
    duration: Optional[float] = None
    size: Optional[int] = None
    streams: Optional[Dict[str, Any] | List[Any]] = None
    content: Optional[bool] = True
    order_index: Optional[int] = None
    model_config = ConfigDict(populate_by_name=True)


class DiscRecord(BaseModel):
    id: str
    content_hash: str
    release_id: Optional[str] = None
    release_slug: Optional[str] = None
    disc_number: Optional[int] = None
    discdb_disc_num: Optional[int] = None
    disc_slug: Optional[str] = None
    disc_name: Optional[str] = None
    format: Optional[str] = None
    info_title: Optional[str] = None
    finalized: bool = False
    finalized_at: Optional[datetime] = None
    artifacts: Optional[Dict[str, Any]] = None
    finalize_result: Optional[Dict[str, Any]] = None
    title_streams: List[TitleStreamRecord] = Field(default_factory=list)
    titles: List["DiscTitleRecord"] = Field(default_factory=list)


class ReleaseRecord(BaseModel):
    id: str
    slug: str
    type: Optional[str] = None
    name: Optional[str] = None
    movie_id: Optional[str] = None
    movie: Optional[MovieRecord] = None
    tmdb_id: Optional[str] = None
    upc: Optional[str] = None
    asin: Optional[str] = None
    cover_front_url: Optional[str] = None
    cover_back_url: Optional[str] = None
    title_cover_url: Optional[str] = None
    info_title: Optional[str] = None
    finalized: bool = False
    finalized_at: Optional[datetime] = None
    release_year: Optional[int] = None
    discs: List[DiscRecord] = Field(default_factory=list)
    boxset_id: Optional[str] = None
    boxset_slug: Optional[str] = None


class DiscWithJobStatus(BaseModel):
    """Disc with full JobStatus for workflow postprocess/transfer steps."""
    disc_id: str
    disc_number: Optional[int] = None
    discdb_disc_num: Optional[int] = None
    disc_name: Optional[str] = None
    disc_format: Optional[str] = None
    job_status: Optional[JobStatus] = None


class ReleaseFullResponse(BaseModel):
    """Release metadata plus all discs with full JobStatus (one-call workflow data)."""
    id: str
    slug: str
    name: Optional[str] = None
    movie_name: Optional[str] = None
    production_year: Optional[int] = None
    release_name: Optional[str] = None
    release_slug: Optional[str] = None
    cover_url: Optional[str] = None
    discs: List[DiscWithJobStatus] = Field(default_factory=list)


class BoxsetFullResponse(BaseModel):
    """Boxset metadata plus all discs with full JobStatus (one-call workflow data)."""
    id: str
    slug: str
    name: Optional[str] = None
    year: Optional[int] = None
    cover_url: Optional[str] = None
    discs: List[DiscWithJobStatus] = Field(default_factory=list)


class DiscTitleRecord(BaseModel):
    id: str
    disc_id: str
    title_id: Optional[str] = Field(default=None, alias="title_id")
    track_id: Optional[str] = Field(None, alias="track_id")
    index: Optional[int] = None
    order_index: Optional[int] = None
    comment: Optional[str] = None
    source_file: Optional[str] = None
    segment_map: Optional[str] = None
    duration: Optional[float] = None
    duration_raw: Optional[str] = None
    size: Optional[int] = None
    display_size: Optional[str] = None
    description: Optional[str] = None
    title: Optional[str] = None
    edition: Optional[str] = None  # Per-title edition (e.g. Director's Cut, Theatrical)
    type: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    # Multi-part layout (#796): part/part_of split one episode across files,
    # episode_end covers several episodes in one file.
    part: Optional[int] = None
    part_of: Optional[int] = None
    episode_end: Optional[int] = None
    chapters: Optional[Dict[str, Any]] = None
    streams: Optional[Dict[str, Any] | List[Any]] = None
    content: Optional[bool] = True
    cover_url: Optional[str] = None
    language_code: Optional[str] = None
    language: Optional[str] = None
    title_seq: Optional[int] = None
    mkv_size: Optional[int] = None  # File size in bytes after ripping
    detection_flags: Optional[Dict[str, Any]] = None  # FFmpeg padding detection (bitrate, black, silence, etc.)
    detection_confidence: Optional[float] = None  # 0.0–1.0, higher = more likely padding
    detection_warning: Optional[bool] = None  # True if flagged as suspicious
    metadata_scan: Optional[Dict[str, Any]] = None  # FFprobe metadata scan (streams, chapters, quality hints, etc.)
    file_path: Optional[str] = None  # Current absolute path to the MKV file on disk
    file_path_stage: Optional[str] = None  # Pipeline stage that last set file_path: "rip", "postprocess", "transfer"
    active: Optional[bool] = None  # Primary within a duplicate group (True = primary, None/False = secondary)
    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def _backfill_track(self):
        if not self.track_id:
            self.track_id = self.title_id
        return self


class ImportSummary(BaseModel):
    """Summary of import operation."""
    movies_imported: int = 0
    releases_imported: int = 0
    discs_imported: int = 0
    jobs_imported: int = 0
    disc_titles_imported: int = 0
    title_streams_imported: int = 0
    boxsets_imported: int = 0
    boxset_releases_imported: int = 0
    movies_skipped: int = 0
    releases_skipped: int = 0
    discs_skipped: int = 0
    jobs_skipped: int = 0
    disc_titles_skipped: int = 0
    title_streams_skipped: int = 0
    boxsets_skipped: int = 0
    boxset_releases_skipped: int = 0
    errors: List[str] = []


class WorkflowContextUpdate(BaseModel):
    """Request schema for updating workflow context (PUT/PATCH)."""
    labelForm: Dict[str, Any]
    fields: Optional[List[str]] = None  # For PATCH operations to specify which fields to update


class TitlePatchRequest(BaseModel):
    """Request schema for patching a single title."""
    title_id: str
    title: Optional[str] = None
    edition: Optional[str] = None  # Per-title edition (e.g. Director's Cut, Theatrical)
    description: Optional[str] = None
    comment: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    # Multi-part layout (#796): part/part_of split one episode across files,
    # episode_end covers several episodes in one file.
    part: Optional[int] = None
    part_of: Optional[int] = None
    episode_end: Optional[int] = None
    type: Optional[str] = None
    duration: Optional[float] = None
    size: Optional[int] = None
    streams: Optional[Any] = None
    order_index: Optional[int] = None
    # LEGACY (stage 2, #778): the version the client *claims to be writing*,
    # computed client-side as cached+1. A guess, and wrong for any write the
    # client did not observe — which then surfaces as a bogus "conflict".
    # Kept so older clients keep working; prefer base_seq.
    title_seq: Optional[int] = None
    # The version the client READ, If-Match style. The server compares it to
    # the current row and assigns the next version itself. The client never
    # computes a version, so it can never guess wrong.
    base_seq: Optional[int] = None
    active: Optional[bool] = None


class TitlePatchBatchRequest(BaseModel):
    """Request schema for patching multiple titles."""
    patches: List[TitlePatchRequest]


class TitlePatchResult(BaseModel):
    """Per-title patch result."""
    title_id: str
    success: bool
    error: Optional[str] = None
    error_code: Optional[str] = None
    updated_title: Optional[Dict[str, Any]] = None
    # On a stale_seq conflict: the row as it actually is now. Lets the client
    # reconcile in place instead of refetching every title on the disc, which
    # is what made a single conflict wipe an entire label form (#775/#778).
    current_title: Optional[Dict[str, Any]] = None


class TitlePatchResponse(BaseModel):
    """Response schema for a single title patch."""
    titles_version: int
    result: TitlePatchResult
    # Rows the duplicate-group sync modified as a side effect of this patch —
    # demoted siblings, primary adjustments — each with its bumped title_seq.
    # Without these the client's per-title seq cache goes stale invisibly and
    # the user's next edit to a sibling is rejected as a conflict (#775).
    synced_titles: Optional[List[Dict[str, Any]]] = None


class TitlePatchBatchResponse(BaseModel):
    """Response schema for a batch title patch."""
    titles_version: int
    results: List[TitlePatchResult]
    # See TitlePatchResponse.synced_titles (#775).
    synced_titles: Optional[List[Dict[str, Any]]] = None


class WorkflowContextResponse(BaseModel):
    """Response schema for workflow context endpoints."""
    # Identification
    id: str  # jobId for jobs, disc_id for discs (or mount_point if disc_id not available)
    type: Literal['job', 'disc']
    discId: Optional[str] = None  # disc_id if available (for discs)
    mountPoint: Optional[str] = None  # mount_point if disc_id not available (for discs)
    discNum: Optional[str] = None  # disc_num from drive manager (for discs and jobs)
    
    # Core workflow data
    labelForm: Optional[Dict[str, Any]] = None
    titles: List[Dict[str, Any]] = []
    titleOrder: List[str] = []
    titlesVersion: Optional[int] = None
    
    # Job/Disc status
    jobStatus: Optional[JobStatus] = None  # Only for jobs
    discInfo: Optional[DiscDetail] = None  # Disc detail info (for discs) - needed for frontend to extract disc_id
    
    # Options (pre-loaded from DB)
    movieOptions: List[MovieSummary] = []
    boxsetOptions: List[BoxsetSummary] = []
    releaseOptions: List[ReleaseSummary] = []
    # When disc has a DiscDB candidate but disc.release_id is null: summary + link flags
    pendingRelease: Optional[ReleaseSummary] = None
    groupOptions: List[Dict[str, Any]] = []
    
    # State flags
    labelDraftProcessed: bool = False
    discNameLocked: bool = False
    discSlugLocked: bool = False
    isSeries: bool = False
    discdbHit: bool = False
    # TheDiscDB lookup outcome (hit/miss) for UI badges; independent of discdbHit (short vs full workflow).
    discdb_result: Optional[str] = None
    discMode: Literal['copy', 'rip'] = 'copy'
    
    # Additional data
    lastReleaseDetails: Optional[Dict[str, Any]] = None
    releaseNameHint: str = ""
    releaseSlugHint: str = ""
    postProcessFiles: List[Dict[str, Any]] = []
    transferDestination: Optional[Dict[str, Any]] = None
    releaseDiscs: List[Dict[str, Any]] = []
    boxsetMovies: List[Dict[str, Any]] = []
    movieCover: Optional[str] = None
    movieName: Optional[str] = None
    productionYear: Optional[int] = None
    # Path B sorted-segment-set dedupe groups. Each entry carries the group's
    # representative_title_id, sibling_title_ids, representative_source
    # (discdb / makemkv_flag / heuristic), and an optional `disagreement` block
    # when DiscDB and the obfuscation flag pick different siblings. Empty
    # list when the disc has no Path B groups (the typical case).
    dedupeGroups: List[Dict[str, Any]] = []
