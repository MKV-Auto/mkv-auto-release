"""
Shared fixtures and helpers for postprocess tests.

job_with_rip_done_for_postprocess: creates Job, Disc, DiscTitle(s), Release, Movie;
raw MKV files and disc_info.json; optional DummyDisc for rename_outputs.
Parameters: movie vs series (release_type), number of titles, file sizes (raw_file_size
to control has_real in dev+quick). Used by test_resume_postprocess_integration and
others.

DummyDisc / use_dummy_disc: For postprocess-only flows (no rip), DummyDisc is a
minimal stub that implements rename_outputs. For tests that run the rip path, use
mock_mkv + real Disc instead of fake_disc/DummyDisc.
"""
import json
import uuid
from pathlib import Path

from api import models
from core.job_paths import JobPaths
from workers import tasks


def job_with_rip_done_for_postprocess(db, tmp_path, monkeypatch, *,
                                      release_type="movie", movie_name="Test Movie",
                                      production_year=2020, num_titles=1, raw_file_size=1500,
                                      use_dummy_disc=True):
    """
    Create Job, Disc, DiscTitle(s), Release, Movie; raw MKV(s) and disc_info.json;
    optional DummyDisc. Returns (job_id, title_id, paths).

    Parameters:
        db: SessionLocal factory (e.g. test_db from conftest_backend).
        tmp_path, monkeypatch: pytest builtins.
        release_type: "movie" or "series".
        movie_name, production_year: metadata for rename paths.
        num_titles: number of DiscTitles and raw MKV files.
        raw_file_size: bytes per raw MKV (use >100KB to trigger dev+quick has_real).
        use_dummy_disc: if True, patch tasks.Disc with a DummyDisc that copies
            the first .mkv from raw to transient/Movies/{movie_name} ({year})/...
            (Postprocess-only. For rip-involving tests, use mock_mkv + real Disc.)
    """
    monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(tasks, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr("core.job_paths.resolve_jobs_root", lambda _out_dir=None: tmp_path / "data")

    with db() as session:
        movie = models.Movie(id=str(uuid.uuid4()), name=movie_name, production_year=production_year)
        release = models.Release(
            id=str(uuid.uuid4()), slug="testslug", type=release_type, name="Ed", movie_id=movie.id
        )
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="inttest", release_id=release.id, disc_number=1, disc_slug="disc01")
        session.add_all([movie, release, disc])
        session.flush()

        title_ids = []
        for i in range(num_titles):
            t = models.DiscTitle(
                id=str(uuid.uuid4()),
                disc_id=disc.id,
                source_file=f"0010{i}.mpls",
                title=movie_name,
                index=i + 1,
                order_index=i + 1,
                mkv_size=raw_file_size,
            )
            session.add(t)
            session.flush()
            title_ids.append(str(t.id))

        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            job_status="running",
            scan_state="completed",
            rip_state="completed",
            label_state="completed",
            transfer_state="pending",
            finalize_state="completed",
            finalize_release_state="pending",
            stage_profile="miss",
            ripped_files={title_ids[i]: f"test_t{i+1}.mkv" for i in range(num_titles)},
            disc_payload={
                "source_hashes": {f"0010{i}.mpls": "abc" + str(i) for i in range(num_titles)},
                "titles": {str(i + 1): {"file": f"0010{i}.mpls"} for i in range(num_titles)},
            },
        )
        session.add(job)
        session.flush()
        job_id = str(job.id)
        if use_dummy_disc and num_titles >= 1:
            job.post_paths = {
                title_ids[0]: f"Movies/{movie_name} ({production_year})/{movie_name} ({production_year}) [1080p].mkv"
            }
        session.commit()

    paths = JobPaths(tmp_path / "data", job_id)
    paths.ensure_layout()
    for i in range(num_titles):
        (paths.raw / f"test_t{i+1}.mkv").write_bytes(b"x" * raw_file_size)
    disc_info = {
        "titles": {str(i + 1): {"file": f"0010{i}.mpls"} for i in range(num_titles)},
        "db_mapping": {f"0010{i}.mpls": {"episode_name": movie_name, "type": "MainMovie", "format": "MainFeature"} for i in range(num_titles)},
        "movie_name": movie_name,
        "type": "Movie",
        "resolution": "1080p",
    }
    (paths.raw / "disc_info.json").write_text(json.dumps(disc_info))

    if use_dummy_disc:
        def _rename(base: str, **kw):
            mn = kw.get("movie_name") or "Test Movie"
            py = kw.get("production_year") or 2020
            trans = Path(base).resolve().parent / "transient"
            show = trans / "Movies" / f"{mn} ({py})"
            show.mkdir(parents=True, exist_ok=True)
            dest = show / f"{mn} ({py}) [1080p].mkv"
            import shutil
            renamed_paths = {}
            for f in Path(base).rglob("*.mkv"):
                if f.suffix.lower() == ".mkv":
                    shutil.copy2(f, dest)
                    break
            # Return empty dict - tests set post_paths manually anyway
            return renamed_paths

        # DummyDisc: postprocess-only stub implementing rename_outputs. For rip-involving
        # tests, use mock_mkv + real Disc instead of DummyDisc (see module docstring).
        class DummyDisc:
            def __init__(self, *a, **k):
                self.titles = {}
                self.db_mapping = {}
                self.title_type = "Movie"
                self.movie_name = "Test Movie"
                self.resolution = "1080p"
                self.disc_slug = None
                self.errors = {}
                self.log_fn = None

            def load_disc_map(self, output_folder: str):
                p = Path(output_folder) / "disc_info.json"
                if p.exists():
                    with open(p) as f:
                        info = json.load(f)
                    self.titles = {int(k): v for k, v in (info.get("titles") or {}).items()}
                    self.db_mapping = info.get("db_mapping") or {}
                    self.movie_name = info.get("movie_name") or self.movie_name
                    self.title_type = info.get("type") or self.title_type
                    self.resolution = info.get("resolution") or self.resolution

            rename_outputs = staticmethod(_rename)

        monkeypatch.setattr(tasks, "Disc", DummyDisc)

    return job_id, title_ids[0], paths
