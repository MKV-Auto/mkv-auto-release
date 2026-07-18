from __future__ import annotations

from pathlib import Path

from core.utils import resolve_jobs_root

# Rip output contract version (see docs/RIP_OUTPUT_CONTRACT.md)
RIP_OUTPUT_CONTRACT_VERSION = 1


class JobPaths:
    """
    Centralized layout for a job's filesystem footprint.

    root/
      raw/        - MakeMKV outputs
      previews/   - ffmpeg preview segments/manifests
      metadata/   - logs, summaries, disc info
      finalize/   - finalize outputs (discNN.json/txt, summaries)
      transient/  - local staging area; see ``transient`` property below

    See docs/RIP_OUTPUT_CONTRACT.md for the versioned rip output contract.
    """

    RIP_OUTPUT_CONTRACT_VERSION = 1  # docs/RIP_OUTPUT_CONTRACT.md; keep in sync with module constant

    def __init__(self, jobs_root: Path, job_id: str):
        self.jobs_root = Path(jobs_root).expanduser()
        self.job_id = str(job_id)

    @property
    def root(self) -> Path:
        return self.jobs_root / self.job_id

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def previews(self) -> Path:
        return self.root / "previews"

    @property
    def metadata(self) -> Path:
        return self.root / "metadata"

    @property
    def finalize(self) -> Path:
        return self.root / "finalize"

    @property
    def transient(self) -> Path:
        """Per-job local staging directory.

        **Post-5d (#365):** for local-mode jobs ``rename_outputs`` writes
        directly to ``config.transfer_dir`` (the library), so transient/
        is typically empty for those. The directory still serves two
        purposes:

          1. **Remote-mode staging:** rsync/smb/nfs rename writes here
             first so the eventual atomic upload to the remote has a
             single committed source. ``core/transfer/protocols/smb.py``
             has a special-case that strips the ``"transient"`` segment
             from the destination path so contents land at the remote
             root rather than under ``<remote>/transient/``.
          2. **Safe fallback:** any path resolver
             (``core/transfer/path_resolution.py``) falls back here when
             no active local ``TransferConfig`` exists or the lookup
             fails — defensive default so a transient DB hiccup never
             becomes a "wrote to a random location" bug.

        Historically (pre-5b) this held post-processed MKVs awaiting
        transfer for every job; the ``transient/-drop`` migration
        (#365 steps 5a–5d) eliminated that purpose for local mode.
        """
        return self.root / "transient"

    @property
    def transient_movies(self) -> Path:
        return self.transient / "Movies"

    @property
    def transient_series(self) -> Path:
        return self.transient / "Series"

    def ensure_layout(self) -> "JobPaths":
        for path in (self.root, self.raw, self.previews, self.metadata, self.finalize, self.transient):
            path.mkdir(parents=True, exist_ok=True)
        return self

    @classmethod
    def for_id(cls, job_id: str) -> "JobPaths":
        """Build JobPaths from just a job ID, using the configured jobs root."""
        return cls(resolve_jobs_root(None), str(job_id))

    @classmethod
    def from_job(cls, job, out_dir: str | None = None) -> "JobPaths":
        """Build JobPaths from a job record."""
        job_id = str(getattr(job, "id"))
        jobs_root = resolve_jobs_root(out_dir)
        return cls(jobs_root, job_id)
