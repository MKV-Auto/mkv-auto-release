"""Database models."""
import uuid
from typing import Optional
from sqlalchemy import (
    Column,
    String,
    Integer,
    JSON,
    Text,
    TIMESTAMP,
    text,
    ForeignKey,
    UniqueConstraint,
    Boolean,
    Float,
    BigInteger,
    Index,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import relationship, synonym
from api.database import Base


def _uuid_str():
    return str(uuid.uuid4())


class Movie(Base):
    __tablename__ = "movies"

    id = Column(String, primary_key=True, default=_uuid_str)
    name = Column(String, nullable=False)
    production_year = Column(Integer, nullable=True)
    tmdb_id = Column(String, nullable=True, unique=True)
    tmdb_type = Column(String, nullable=True)  # "movie" or "tv"
    cover_url = Column(Text, nullable=True)  # TMDB poster URL
    cover_path = Column(Text, nullable=True)  # Local file path after download
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )

    releases = relationship("Release", back_populates="movie")


class Release(Base):
    __tablename__ = "releases"
    __table_args__ = (
        Index("idx_releases_movie_id", "movie_id"),
        Index("idx_releases_boxset_id", "boxset_id"),
        # Declared so the shape lives with the model instead of only in
        # 202608220000. A movie may hold several standalone releases (seasons of
        # one show); an exact duplicate of (movie, name, upc) is still refused,
        # which is the race protection 202602080000 was written for.
        #
        # postgresql_where keeps SQLAlchemy from emitting this on SQLite, so the
        # create_all-based test fixture is unaffected. That also means SQLite
        # tests cannot see it — which is precisely why #821 went unnoticed — so
        # anything asserting this behaviour must run against Postgres.
        Index(
            "uq_releases_movie_edition_standalone",
            "movie_id",
            text("coalesce(name, '')"),
            text("coalesce(upc, '')"),
            unique=True,
            postgresql_where=text("boxset_id IS NULL"),
        ),
    )

    id = Column(String, primary_key=True, default=_uuid_str)
    slug = Column(String, nullable=False)
    type = Column(String, nullable=False, default="movie")
    name = Column(String, nullable=True)  # Edition name (can be blank)
    movie_id = Column(String, ForeignKey("movies.id"), nullable=False)
    upc = Column(String, nullable=True)
    asin = Column(String, nullable=True)
    cover_front_url = Column(Text, nullable=True)
    cover_back_url = Column(Text, nullable=True)
    finalize_state = Column(String, nullable=True)
    release_year = Column(Integer, nullable=True)
    resolution = Column(String, nullable=True)  # Highest resolution from DiscDB (e.g., "2160p", "1080p")
    finalized = Column(Boolean, nullable=False, server_default=text("false"))
    finalized_at = Column(TIMESTAMP(timezone=True), nullable=True)
    boxset_id = Column(String, ForeignKey("boxsets.id", ondelete="SET NULL"), nullable=True)
    modified = Column(Boolean, nullable=False, server_default=text("false"))
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )

    movie = relationship("Movie", back_populates="releases")
    discs = relationship("Disc", back_populates="release")
    boxset = relationship("Boxset", back_populates="releases")
    # Backwards compatibility: allow .title alias for .name even though the column was dropped.
    title = synonym("name")


class Disc(Base):
    __tablename__ = "discs"
    __table_args__ = (
        Index("idx_discs_release_id", "release_id"),
    )

    id = Column(String, primary_key=True, default=_uuid_str)
    content_hash = Column(String, nullable=False, unique=True)
    # Stamped only by the human edit paths (title PATCH, disc metadata PATCH),
    # never by pipeline writes. A DiscDB hit with this set is "dirty": the local
    # copy diverged from upstream, so it is worth exporting as an update.
    user_edited_at = Column(TIMESTAMP(timezone=True), nullable=True)
    # TheDiscDB's GlobalDiscId: SHA1 of the disc's AACS/Unit_Key_RO.inf, uppercase
    # hex. Identifies a pressing globally where content_hash identifies it by file
    # layout. Only obtainable from the physical disc, and absent on DVDs (no AACS
    # directory), so it stays nullable and is filled opportunistically at scan.
    global_disc_id = Column(String, nullable=True)
    release_id = Column(String, ForeignKey("releases.id"), nullable=True)
    disc_number = Column(Integer, nullable=True)
    discdb_disc_num = Column(Integer, nullable=True)  # TheDiscDB matched disc index (reference only)
    disc_slug = Column(String, nullable=True)
    disc_name = Column(String, nullable=True)
    format = Column(String, nullable=True)
    info_title = Column(Text, nullable=True)
    label_payload = Column(JSON, nullable=True)
    label_draft = Column(JSON, nullable=True)
    finalize_result = Column(JSON, nullable=True)
    artifacts = Column(JSON, nullable=True)
    finalized = Column(Boolean, nullable=False, server_default=text("false"))
    finalized_at = Column(TIMESTAMP(timezone=True), nullable=True)
    scan_state = Column(String, nullable=True)  # 'pending', 'scanning', 'completed', 'failed'
    scan_attempts = Column(Integer, nullable=False, server_default=text("0"))
    last_scan_error = Column(Text, nullable=True)
    last_scan_at = Column(TIMESTAMP(timezone=True), nullable=True)
    info_log_stored = Column(Boolean, nullable=False, server_default=text("false"))
    disc_info = Column(JSON, nullable=True)
    disc_size_bytes = Column(BigInteger, nullable=True)
    # DiscDB contribution tracking (#334)
    discdb_contribution_status = Column(String, nullable=True)  # not_submitted, draft, exported, submitted, accepted, rejected
    discdb_contribution_notes = Column(Text, nullable=True)
    discdb_exported_at = Column(TIMESTAMP(timezone=True), nullable=True)
    discdb_submitted_at = Column(TIMESTAMP(timezone=True), nullable=True)
    # DiscDB hit verification (#338): pending, confirmed, submitted, skipped, opted_out
    discdb_verification_status = Column(String, nullable=True)
    # Per-disc clip flags for the Path B iteration loop.
    # Shape: { "<clip_id>": "potentially" | "definitely" }. NULL/{} = no flags.
    segment_obfuscation_flags = Column(JSON, nullable=True)
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )

    release = relationship("Release", back_populates="discs")
    jobs = relationship("Job", back_populates="disc", cascade="all, delete-orphan")
    title_streams = relationship("TitleStream", back_populates="disc", cascade="all, delete-orphan")
    titles = relationship("DiscTitle", back_populates="disc", cascade="all, delete-orphan")


class Boxset(Base):
    __tablename__ = "boxsets"

    id = Column(String, primary_key=True, default=_uuid_str)
    slug = Column(String, nullable=False)
    name = Column(String, nullable=True)  # Display name
    title = Column(String, nullable=True)  # Full title
    sort_title = Column(String, nullable=True)
    upc = Column(String, nullable=True)
    asin = Column(String, nullable=True)
    year = Column(Integer, nullable=True)  # Boxset release year
    locale = Column(String, nullable=True)  # e.g., "en-us"
    region_code = Column(String, nullable=True)  # e.g., "1"
    cover_front_url = Column(Text, nullable=True)
    cover_back_url = Column(Text, nullable=True)
    image_url = Column(Text, nullable=True)  # Relative path for export
    release_date = Column(TIMESTAMP(timezone=True), nullable=True)
    finalized = Column(Boolean, nullable=False, server_default=text("false"))
    finalized_at = Column(TIMESTAMP(timezone=True), nullable=True)
    finalize_result = Column(JSON, nullable=True)  # Stores export paths
    modified = Column(Boolean, nullable=False, server_default=text("false"))
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )

    releases = relationship("Release", back_populates="boxset", foreign_keys="Release.boxset_id")


class TitleStream(Base):
    __tablename__ = "title_streams"
    __table_args__ = (
        UniqueConstraint("disc_id", "title_id", "stream_index", name="uq_title_streams_disc_title_stream"),
    )

    id = Column(String, primary_key=True, default=_uuid_str)
    disc_id = Column(String, ForeignKey("discs.id"), nullable=False)
    title_id = Column(String, ForeignKey("disc_titles.id"), nullable=False)
    stream_index = Column(Integer, nullable=True, server_default=text("0"))
    stream_type = Column(String, nullable=True)
    audio_type = Column(String, nullable=True)
    language_code = Column(String, nullable=True)
    language = Column(String, nullable=True)
    codec_short = Column(String, nullable=True)
    codec_hint = Column(String, nullable=True)
    name = Column(String, nullable=True)
    bitrate = Column(String, nullable=True)
    channels = Column(Integer, nullable=True)
    sample_rate = Column(String, nullable=True)
    bit_depth = Column(String, nullable=True)
    resolution = Column(String, nullable=True)
    aspect_ratio = Column(String, nullable=True)
    reference_frames = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    info = Column(Text, nullable=True)
    duration_seconds = Column(Float, nullable=True)
    flag = Column(String, nullable=True)
    default = Column(Boolean, nullable=True)
    layout = Column(String, nullable=True)
    frame_rate = Column(String, nullable=True)
    title = Column(String, nullable=True)
    note = Column(Text, nullable=True)
    duration = Column(Float, nullable=True)
    size = Column(BigInteger, nullable=True)
    streams = Column(JSON, nullable=True)
    content = Column(Boolean, nullable=False, server_default=text("true"))
    order_index = Column(Integer, nullable=True)
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )

    disc = relationship("Disc", back_populates="title_streams")
    title_ref = relationship("DiscTitle", back_populates="title_streams")


class DiscTitle(Base):
    __tablename__ = "disc_titles"
    # One row per (disc_id, source_file). MakeMKV index is mutable across rescans; merge on source_file.
    __table_args__ = (
        UniqueConstraint("disc_id", "source_file", name="uq_disc_titles_disc_sourcefile"),
    )

    id = Column(String, primary_key=True, default=_uuid_str)
    disc_id = Column(String, ForeignKey("discs.id"), nullable=False)
    index = Column(Integer, nullable=True)
    comment = Column(Text, nullable=True)
    source_file = Column(String, nullable=True)
    segment_map = Column(String, nullable=True)
    duration = Column(Float, nullable=True)
    duration_raw = Column(String, nullable=True)
    size = Column(BigInteger, nullable=True)
    display_size = Column(String, nullable=True)
    # ── Label fields ────────────────────────────────────────────────
    # Every user-editable label field is stored three ways: a resolved
    # cache (these legacy columns — every reader in the codebase), plus
    # a user_*/auto_* source split. Resolution: `resolved = user ?? auto`.
    # Writes MUST go through `api.crud.set_title_field(title, field,
    # value, source)` (or set_title_type for `type`) so all three stay
    # in sync. This is the `type` provenance model (below) generalized
    # to every label field — it is what makes an automated pass unable
    # to overwrite a human's value, by construction rather than by
    # defensive merge logic (title-state redesign, area 1).
    description = Column(Text, nullable=True)
    title = Column(String, nullable=True)
    edition = Column(String, nullable=True)  # Per-title edition (e.g. Director's Cut, Theatrical)
    auto_title = Column(String, nullable=True)
    user_title = Column(String, nullable=True)
    auto_edition = Column(String, nullable=True)
    user_edition = Column(String, nullable=True)
    auto_description = Column(Text, nullable=True)
    user_description = Column(Text, nullable=True)
    # Effective type, denormalized cache of `user_type ?? auto_type`. Reads
    # continue to go through this column; writes MUST go through
    # `api.crud.set_title_type(title, value, source)` so the source split
    # below stays in sync.
    type = Column(String, nullable=True)
    # Source-split of `type` for UI provenance (chip system on the titles
    # step distinguishes user-driven vs automated decisions). `auto_type`
    # is set by scan-time defaults, DiscDB import, Path A sibling-ignore,
    # m2ts subsumption marks, etc. `user_type` is set by PATCH edits, the
    # exploratory-rip canonical match, and the "previous order had decoys"
    # action. The cache `type` resolves user → auto if both are set.
    auto_type = Column(String, nullable=True)
    user_type = Column(String, nullable=True)
    season = Column(Integer, nullable=True)
    episode = Column(Integer, nullable=True)
    auto_season = Column(Integer, nullable=True)
    user_season = Column(Integer, nullable=True)
    auto_episode = Column(Integer, nullable=True)
    user_episode = Column(Integer, nullable=True)
    # Multi-part layout (#796). A disc's physical layout does not always
    # match one-file-per-episode, in either direction:
    #
    #   part / part_of  — this file is part N of M for ONE episode. Emits the
    #                     Plex/Jellyfin stacking suffix (`- part1`), which both
    #                     servers treat as a single episode split across files.
    #   episode_end     — this ONE file covers `episode`..`episode_end`. Emits
    #                     range naming (`s03e01-e02`).
    #
    # Both carry the user_/auto_ provenance split like season/episode, so
    # TMDB two-parter detection can populate them without ever overwriting a
    # hand-correction.
    part = Column(Integer, nullable=True)
    auto_part = Column(Integer, nullable=True)
    user_part = Column(Integer, nullable=True)
    part_of = Column(Integer, nullable=True)
    auto_part_of = Column(Integer, nullable=True)
    user_part_of = Column(Integer, nullable=True)
    episode_end = Column(Integer, nullable=True)
    auto_episode_end = Column(Integer, nullable=True)
    user_episode_end = Column(Integer, nullable=True)
    chapters = Column(JSON, nullable=True)
    streams = Column(JSON, nullable=True)
    content = Column(Boolean, nullable=False, server_default=text("true"))
    cover_url = Column(Text, nullable=True)
    order_index = Column(Integer, nullable=True)
    language_code = Column(String, nullable=True)
    language = Column(String, nullable=True)
    source_hash = Column(String, nullable=True)  # Hash of source file before post-processing
    output_hash = Column(String, nullable=True)  # Hash of post-processed output file
    title_seq = Column(Integer, nullable=False, server_default=text("0"))
    mkv_size = Column(BigInteger, nullable=True)  # File size in bytes after ripping
    detection_flags = Column(JSON, nullable=True)  # FFmpeg padding detection results (bitrate, black, silence, etc.)
    detection_confidence = Column(Float, nullable=True)  # 0.0–1.0, higher = more likely padding
    detection_warning = Column(Boolean, nullable=False, server_default=text("false"))  # Flag as suspicious
    metadata_scan = Column(JSON, nullable=True)  # FFprobe metadata scan (streams, chapters, quality hints, etc.)
    file_path = Column(Text, nullable=True)  # Current absolute path to the MKV file on disk
    file_path_stage = Column(String, nullable=True)  # Which pipeline stage last set file_path: "rip", "postprocess", "transfer"
    active = Column(Boolean, nullable=True)  # Primary within a duplicate group (True = primary, None/False = secondary)
    obfuscation_flag = Column(Boolean, nullable=False, server_default=text("false"))  # MakeMKV MSG:3307 flag bit 0x01000000 — playlist-obfuscation mass detector
    # Why this title is flagged as a decoy (drives the UI tier). NULL = not flagged.
    # Tiers: 'segment_set_sibling' (HIGH — member of a sorted-segment-set group),
    # 'path_a_decoy' (HIGH — Path A skipped it), 'duration_short' (HIGH —
    # post-ffprobe: declared duration much shorter than actual, #374),
    # 'low_bitrate_decoy' (HIGH — post-ffprobe: bitrate implausible for the
    # resolution, #374), 'makemkv_msg3307' (MEDIUM — MakeMKV's per-title bit
    # only, no group context). Relational reasons (segment_set_sibling /
    # path_a_decoy) win over post-ffprobe reasons when both apply.
    obfuscation_reason = Column(String, nullable=True)
    # When this title's clip ID is included in another title's segment_map on the
    # same disc, that other title's UUID lives here. The wrapping title (typically
    # an .mpls) is the row the user should label; the subsumed row (typically an
    # .m2ts) is auto-marked `type='ignore'` and surfaced under "Component clips"
    # in the wrapping title's DuplicateGroupPanel. NULL on every standalone title.
    subsumed_by_title_id = Column(String, nullable=True)
    # Per-title escape hatch: when TRUE, this row is excluded from the
    # sorted-segment-set grouping in duplicate_info.attach_duplicate_info
    # so it renders as its own left-rail row. Used for legitimate dup-
    # group false positives (two real playlists that share segments but
    # differ in audio/subs). Toggled via POST .../ungroup-duplicate.
    force_independent_group = Column(Boolean, nullable=False, server_default=text("false"))
    playitem_durations_s = Column(JSON, nullable=True)  # Per-PlayItem durations parsed from MPLS at scan time; None on m2ts-only or parse failure
    # Matroska Segment UID read from the file's container header via
    # `mkvmerge -J` at postprocess time. NULL for legacy titles produced
    # before #448 shipped. Used by the transient/-drop 5b'b src==dest
    # shortcut and by v2 self-healing reattach (#449) to identify "this
    # rip's files at the destination" by container identity rather than
    # fragile filename match.
    segment_uid = Column(String, nullable=True, index=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )

    disc = relationship("Disc", back_populates="titles")
    title_streams = relationship("TitleStream", back_populates="title_ref")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("idx_jobs_disc_id", "disc_id"),
        Index("idx_jobs_job_status", "job_status"),
        Index("idx_jobs_rip_state", "rip_state"),
        Index("idx_jobs_created_at", "created_at"),
        Index("idx_jobs_disc_id_status", "disc_id", "job_status"),
    )

    id = Column(String, primary_key=True, default=_uuid_str)
    disc_id = Column(String, ForeignKey("discs.id"), nullable=False)
    disc_num = Column(String, nullable=False)
    mount_point = Column(String, nullable=False)
    # Stable hardware identity from /dev/disk/by-id/ (#540). NULL means the job
    # was created before this column existed or against a drive that resolved
    # only via the by-path/sysfs fallback (multi-drive-unsafe per drive_policy).
    drive_by_id_serial = Column(String, nullable=True, index=True)
    # User dismissed this job from the active carousel (#543). The column has
    # lived in the DB schema for a while; this entry plugs it back into the
    # ORM so query_unfinished_jobs can filter on it.
    dismissed = Column(Boolean, nullable=False, default=False, server_default="false")
    mode = Column(String, nullable=False, default="copy")
    job_status = Column(String, nullable=False, default="pending")
    scan_state = Column(String, nullable=True)
    rip_state = Column(String, nullable=True)
    label_state = Column(String, nullable=True)
    finalize_state = Column(String, nullable=True)
    # #365 step 5 — post_state column dropped (Alembic 202606010000).
    # Use the Job.derived_post_state hybrid_property below for any
    # read; no writes go to this name any more.
    transfer_state = Column(String, nullable=True)
    finalize_release_state = Column(String, nullable=True)
    phase = Column(String, nullable=True)
    workflow_step = Column(String, nullable=True)
    rip_progress = Column(Integer, nullable=False, default=0)
    rip_phase = Column(String, nullable=True)  # "copy" | "verification" | null during rip
    post_progress = Column(Integer, nullable=False, default=0)
    logs = Column(JSON, nullable=False, default=list)
    # title_id (UUID) -> relative_path in raw/; used by postprocess, transfer, validation, preview recovery
    ripped_files = Column(JSON, nullable=True)  # Dict mapping title_id (UUID) -> relative_path (files in raw/ after rip)
    post_paths = Column(JSON, nullable=True)  # Dict mapping title_id -> relative_path (files in transient/ after post-processing)
    error_reason = Column(Text, nullable=True)
    disc_payload = Column(JSON, nullable=True)  # transitional snapshot for UI/compat
    # detailed progress
    titles_completed = Column(Integer, nullable=True)
    total_titles = Column(Integer, nullable=True)
    current_title_progress = Column(Integer, nullable=True)
    current_title_id = Column(String, nullable=True)
    current_title_number = Column(Integer, nullable=True)
    per_title_progress = Column(JSON, nullable=True)
    transfer_paths = Column(JSON, nullable=True)
    transfer_error = Column(Text, nullable=True)
    transfer_progress = Column(Integer, nullable=False, default=0)
    stage_profile = Column(String, nullable=True)
    discdb_result = Column(String, nullable=True)
    dev_mode = Column(Boolean, nullable=False, server_default=text("false"))
    dev_validation = Column(JSON, nullable=True)
    export_path = Column(Text, nullable=True)
    rip_started_at = Column(TIMESTAMP(timezone=True), nullable=True)   # UTC when rip copy began (#344)
    rip_completed_at = Column(TIMESTAMP(timezone=True), nullable=True)  # UTC when rip completed (#344)
    celery_task_id = Column(String, nullable=True)  # Store Celery task ID for linking
    rip_pid = Column(Integer, nullable=True)  # PID of makemkvcon process for this rip
    segment_reorder_state = Column(JSON, nullable=True)  # Path A workflow state; null when not running segment-reorder
    rip_set = Column(JSON, nullable=True)  # MakeMKV title indexes for the selective-rip per-title loop; null = all-mode
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )

    disc = relationship("Disc", back_populates="jobs")
    transfer_history = relationship("TransferHistory", back_populates="job")
    
    # Transfer enhancement fields
    transfer_verification_hash = Column(String, nullable=True)
    transfer_verification_status = Column(String, nullable=True)  # "pending", "verified", "failed", "skipped"
    transfer_retry_count = Column(Integer, nullable=False, server_default=text("0"))
    transfer_max_retries = Column(Integer, nullable=False, server_default=text("3"))
    transfer_speed_mbps = Column(Float, nullable=True)
    transfer_bytes_transferred = Column(BigInteger, nullable=True)
    transfer_total_bytes = Column(BigInteger, nullable=True)
    transfer_conflict_resolution = Column(String, nullable=True)
    transfer_source_cleaned = Column(Boolean, nullable=False, server_default=text("false"))
    transfer_validation_status = Column(String, nullable=True)  # "pending", "passed", "failed"
    transfer_validation_error = Column(Text, nullable=True)
    transfer_deduplicated = Column(Boolean, nullable=False, server_default=text("false"))
    # Sub-phase within the collapsed transfer stage (#325 + #365). NULL until
    # the unified transfer worker starts. Values: "preparing" (rename + hash
    # + output validation, was the standalone postprocess stage),
    # "transferring" (move/copy), "verifying" (destination validation).
    # See docs/ADR-001-postprocess-collapse.md.
    transfer_phase = Column(String, nullable=True)

    @hybrid_property
    def derived_post_state(self) -> Optional[str]:
        """``post_state`` derived from the rest of the job state.

        Step 1 of the ``post_state`` column drop (#365 follow-up after
        the transient/-drop). Mirrors what readers expect today from
        ``Job.post_state`` so callers can be migrated one at a time
        without behaviour change before the column itself is dropped.

        Decision table (in order; first match wins):

          1. ``rip_state`` not in (completed, skipped) → ``None`` —
             postprocess hasn't entered the picture yet.
          2. ``job_status == "failed"`` AND ``transfer_state`` is not
             ``failed``/``completed`` → ``"failed"`` — the job died at
             the postprocess phase (not rip-failed, not transfer-failed).
          3. ``transfer_phase == "preparing"`` → ``"running"`` — the
             collapsed model's "preparing" sub-phase IS the postprocess
             running phase.
          4. ``transfer_phase in {"transferring", "verifying"}`` OR
             ``transfer_state == "completed"`` → ``"completed"`` —
             we're past preparing.
          5. ``transfer_state in {"ready", "running", "failed"}`` →
             ``"completed"`` — transfer has started or finished, so
             postprocess must be done.
          6. ``label_state in {"completed", "skipped", None}`` →
             ``"ready"`` — postprocess can start (hit branch: label is
             ``"skipped"`` by rip_complete; miss branch: label has
             completed).
          7. Default → ``"pending"`` — miss branch waiting on label.

        The hybrid is Python-side only for now (no SQL expression
        variant). A SQL ``CASE`` translation can be added later if a
        reader needs to filter/sort by it at the query layer.
        """
        rip_state = self.rip_state
        if rip_state not in ("completed", "skipped"):
            return None
        if (
            self.job_status == "failed"
            and self.transfer_state not in ("failed", "completed")
        ):
            return "failed"
        transfer_phase = self.transfer_phase
        if transfer_phase == "preparing":
            return "running"
        if transfer_phase in ("transferring", "verifying"):
            return "completed"
        transfer_state = self.transfer_state
        if transfer_state == "completed":
            return "completed"
        if transfer_state in ("ready", "running", "failed"):
            return "completed"
        if self.label_state in ("completed", "skipped", None):
            return "ready"
        return "pending"


class TransferConfig(Base):
    __tablename__ = "transfer_configs"
    __table_args__ = (
        Index("idx_transfer_configs_is_active", "is_active"),
    )

    id = Column(String, primary_key=True, default=_uuid_str)
    mode = Column(String, nullable=False)  # "local", "rsync", "smb", "nfs"
    name = Column(String, nullable=True)  # User-friendly name
    is_active = Column(Boolean, nullable=False, server_default=text("false"))
    transfer_dir = Column(String, nullable=True)  # Base destination path (can contain templates)
    output_dir = Column(String, nullable=True)  # Source/output path
    path_template = Column(String, nullable=True)  # Dynamic path template
    path_template_schema_version = Column(String, nullable=True)  # Version from path_templates.PATH_TEMPLATE_SCHEMA_VERSION when template was set
    config_data = Column(JSON, nullable=True)  # Mode-specific configuration
    conflict_resolution = Column(String, nullable=False, server_default="overwrite")  # "overwrite", "skip", "rename", "fail"
    health_check_interval_minutes = Column(Integer, nullable=True)
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )

    credentials = relationship("TransferCredential", back_populates="transfer_config", cascade="all, delete-orphan")
    history = relationship("TransferHistory", back_populates="transfer_config", cascade="all, delete-orphan")
    health_checks = relationship("TransferHealthCheck", back_populates="transfer_config", cascade="all, delete-orphan")

    @classmethod
    def get_active_config(cls, db_session):
        """Get the currently active transfer config."""
        return db_session.query(cls).filter(cls.is_active == True).first()

    def activate(self, db_session):
        """Deactivate all other configs and activate this one."""
        db_session.query(TransferConfig).filter(TransferConfig.is_active == True).update({"is_active": False})
        self.is_active = True
        db_session.commit()


class TransferCredential(Base):
    __tablename__ = "transfer_credentials"
    __table_args__ = (
        UniqueConstraint("transfer_config_id", "type", name="uq_transfer_credentials_config_type"),
    )

    id = Column(String, primary_key=True, default=_uuid_str)
    transfer_config_id = Column(String, ForeignKey("transfer_configs.id", ondelete="CASCADE"), nullable=False)
    type = Column(String, nullable=False)  # "rsync_key", "smb_username", "smb_password", "smb_domain", "nfs_options"
    value = Column(Text, nullable=False)  # Encrypted credential value
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )

    transfer_config = relationship("TransferConfig", back_populates="credentials")


class TransferHistory(Base):
    __tablename__ = "transfer_history"
    __table_args__ = (
        Index("idx_transfer_history_job_id", "job_id"),
        Index("idx_transfer_history_config_id", "transfer_config_id"),
        Index("idx_transfer_history_created_at", "created_at"),
        Index("idx_transfer_history_status", "status"),
    )

    id = Column(String, primary_key=True, default=_uuid_str)
    job_id = Column(String, ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    transfer_config_id = Column(String, ForeignKey("transfer_configs.id", ondelete="SET NULL"), nullable=True)
    mode = Column(String, nullable=False)  # "local", "rsync", "smb", "nfs"
    source_path = Column(String, nullable=False)
    destination_path = Column(String, nullable=False)
    status = Column(String, nullable=False)  # "completed", "failed", "cancelled", "skipped", "deduplicated"
    bytes_transferred = Column(BigInteger, nullable=True)
    transfer_duration_seconds = Column(Float, nullable=True)
    average_speed_mbps = Column(Float, nullable=True)
    verification_status = Column(String, nullable=True)  # "verified", "failed", "skipped"
    verification_hash = Column(String, nullable=True)
    error_message = Column(Text, nullable=True)
    conflict_resolution = Column(String, nullable=True)
    source_cleaned = Column(Boolean, nullable=False, server_default=text("false"))
    was_deduplicated = Column(Boolean, nullable=False, server_default=text("false"))
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )

    job = relationship("Job", back_populates="transfer_history")
    transfer_config = relationship("TransferConfig", back_populates="history")


class TransferHealthCheck(Base):
    __tablename__ = "transfer_health_checks"
    __table_args__ = (
        Index("idx_transfer_health_config_checked", "transfer_config_id", "checked_at"),
        Index("idx_transfer_health_status", "status"),
    )

    id = Column(String, primary_key=True, default=_uuid_str)
    transfer_config_id = Column(String, ForeignKey("transfer_configs.id", ondelete="CASCADE"), nullable=False)
    check_type = Column(String, nullable=False)  # "connectivity", "authentication", "permissions", "space", "overall"
    status = Column(String, nullable=False)  # "healthy", "degraded", "unhealthy", "unknown"
    message = Column(Text, nullable=True)
    response_time_ms = Column(Integer, nullable=True)
    checked_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    transfer_config = relationship("TransferConfig", back_populates="health_checks")
