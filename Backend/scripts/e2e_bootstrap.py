"""
E2E bootstrap: apply MockDrive and MockMKV at process startup.

Import this module before api.main or workers.tasks. The caller must set
DATABASE_URL, MKVAUTO_DATA (or MKVAUTO_ROOT), MKVAUTO_E2E, and
CELERY_TASK_ALWAYS_EAGER before the first import of api or database.

The disc profile loaded is selectable via the ``E2E_FIXTURE`` env var (default
``"miss"``); fixture modules live in ``tests/fixtures/e2e_fixtures/``. Each
fixture supplies the MockDrive ``discinfo_payload``, the MockMKV titles list,
and optional settings overrides (e.g. ``discdb_disabled`` to force MISS path,
or ``discdb_url_override`` to simulate a DiscDB outage).

Patches:
- core._drive_operations: list_drives, get_disc_info, refresh_disc_info,
  validate_disc_info, scan_disc_info, hash_disc, handle_disc_eject,
  handle_disc_insert; _internal_only no-op.
- api.routers.jobs.validate_disc_info
- core.utils.run_makemkv, core.disc.run_makemkv, api.crud.run_makemkv

After patching, seeds core.disc_cache via refresh_disc_info("1", "/dev/sr0").
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure Backend is on path so tests.fixtures can be imported
_backend = Path(__file__).resolve().parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))

# Stub out Matroska Segment UID capture in the test stack (#195/#196).
#
# Post-collapse (#325, #365 step 5c flipped MKVAUTO_RENAME_DIRECT_TO_DEST=1 by
# default, and step 5d.1 removed the env-var conditional entirely), the
# transfer step's src==dest shortcut requires that every disc_title row carry
# a Segment UID captured at postprocess via ``mkvmerge -J`` (#451). The UIDs
# identify which files at the destination belong to the current rip.
#
# MockMKV writes 1500-byte stub MKVs that have no real Matroska header, so
# ``mkvmerge -J`` returns no segment_uid for them; the transfer step then
# fails with "Transfer src==dest but no segment_uids captured" and the spec
# can't reach ``transfer_state=completed``. Patch ``core.mkv_identity.
# read_segment_uid`` to return a deterministic synthetic UID per path so the
# capture-then-lookup contract holds without involving real mkvmerge.

from tests.fixtures.e2e_fixtures import load_fixture
from tests.fixtures.mock_drive import MockDrive
from tests.fixtures.mock_mkv import MockMKV

# Apply the segment-UID stub from the docstring above. Deterministic per path
# so the same file always reports the same UID (the contract the lookup at
# transfer time relies on). 32-char lowercase hex matches the real format.
# Production callers all do ``from core.mkv_identity import read_segment_uid``
# *inside* function bodies, so patching the module attribute affects every
# call site at the next import.
import hashlib as _hashlib

import core.mkv_identity as _mkv_identity


def _fake_read_segment_uid(mkv_path: str):
    return _hashlib.sha256(str(mkv_path).encode("utf-8")).hexdigest()[:32]


_mkv_identity.read_segment_uid = _fake_read_segment_uid
print(
    "[e2e_bootstrap] Patched core.mkv_identity.read_segment_uid to a "
    "deterministic SHA-256 prefix — MockMKV stubs now satisfy the #451 "
    "Segment UID contract.",
    file=sys.stderr,
)

# Force the Celery app onto an in-memory broker so eager tasks cannot publish to
# a co-located production worker via the host Redis (issue #378). REDIS_URL has
# already been pointed at a dedicated test Redis (6380) for progress / cache /
# notifications, but the Celery broker is the dangerous channel: if the prod
# container's worker is subscribed to the same broker URL, our test apply_async
# calls land there and execute against the prod DB. memory:// is unreachable to
# any external worker by construction.
from workers import tasks as _worker_tasks
try:
    _worker_tasks.celery_app.conf.update(
        broker_url="memory://",
        result_backend="cache+memory://",
        task_always_eager=True,
        task_eager_propagates=True,
    )
    print(
        "[e2e_bootstrap] Celery broker forced to memory:// "
        "(task_always_eager=True) — tasks cannot escape this process",
        file=sys.stderr,
    )
except Exception as _ce:
    print(f"[e2e_bootstrap] WARNING: failed to override Celery broker: {_ce}", file=sys.stderr)

# Intercept worker callback POSTs (rip-progress, rip-complete,
# rip-verification-complete, postprocess-complete) so they apply state directly
# via StageState instead of hitting the live API over localhost HTTP. With
# CELERY_TASK_ALWAYS_EAGER, each task runs synchronously in the caller's worker
# process. The original HTTP callback POST blocks the caller's thread, and any
# uvicorn worker handling it then needs to enqueue the next stage's task —
# which also tries to call back over HTTP, blocking another worker. The chain
# rip → rip_verification → postprocess → transfer needs more uvicorn workers
# than we want to spin up, so we sidestep the loop entirely. Issue #378.
try:
    from tests.fixtures.stage_callback_intercept import make_stage_callback_fake_requests as _make_fake_req
    _worker_tasks.requests = _make_fake_req()
    print(
        "[e2e_bootstrap] Patched workers.tasks.requests with stage_callback_intercept — "
        "rip-complete / postprocess-complete callbacks now apply state in-process",
        file=sys.stderr,
    )
except Exception as _re:
    print(f"[e2e_bootstrap] WARNING: failed to patch stage callback requests: {_re}", file=sys.stderr)

# Disc cache: clear and disable persist to avoid leaking from prior runs
try:
    from core import disc_cache

    disc_cache.clear()
    disc_cache.DISK_PERSIST_ENABLED = False
    try:
        if getattr(disc_cache, "_cache_file", None) is not None:
            disc_cache._cache_file.unlink(missing_ok=True)
    except Exception:
        pass
except Exception:
    pass

# Load the selected fixture (default to "miss" — the previous hardcoded behavior,
# now via settings.discdb_disabled instead of relying on an unknown content_hash).
_fixture_name = os.environ.get("E2E_FIXTURE", "miss")
_fixture = load_fixture(_fixture_name)
print(f"[e2e_bootstrap] loaded fixture: {_fixture.name} "
      f"(expected_workflow={_fixture.expected_workflow}, "
      f"discdb_disabled={_fixture.discdb_disabled})", file=sys.stderr)

_discinfo_payload = dict(_fixture.discinfo_payload)
# Derive ``scan_tracks`` from ``mockmkv_titles`` so ``crud._apply_scan_tracks``
# creates DiscTitle rows with ``index`` matching MockMKV's ``test_t<N>.mkv``
# output naming. Without these rows the rip flow runs to completion but the
# rip-verification step (``_normalize_ripped_files_to_title_ids``) can't map
# the output filenames back to title_ids and fails the rip with
# "no MKV outputs found under raw/" (#194, same root cause as #423).
#
# ``_hydrated: True`` keeps ``hydrate_disc_payload`` from re-parsing the
# minimal info_log and clobbering these explicit scan_tracks.
#
# Existing scan_tracks on the fixture take precedence — fixtures that already
# emit them (e.g. ``_base.make_simple_payload``) keep their custom shape.
if "scan_tracks" not in _discinfo_payload:
    _tracks_meta = _discinfo_payload.get("tracks") or {}
    _scan_tracks = []
    for _i, _mkv in enumerate(_fixture.mockmkv_titles, start=1):
        _sf = _mkv.get("file")
        if not _sf:
            continue
        _meta = _tracks_meta.get(_sf, {}) if isinstance(_tracks_meta, dict) else {}
        _fmt = (_meta.get("format") or "").lower()
        # MakeMKV "MainFeature" → movie/feature row; other types fall back to
        # the raw format string so _apply_scan_tracks doesn't ignore the row.
        _type = "movie" if _fmt == "mainfeature" else (_meta.get("format") or None)
        _scan_tracks.append({
            "source_file": _sf,
            "index": _i,
            "title": _meta.get("episode_name") or "",
            "type": _type,
        })
    if _scan_tracks:
        _discinfo_payload["scan_tracks"] = _scan_tracks
        _discinfo_payload.setdefault("_hydrated", True)
        print(
            f"[e2e_bootstrap] injected {len(_scan_tracks)} scan_tracks into "
            f"{_fixture.name} payload",
            file=sys.stderr,
        )
_mock_drive = MockDrive(drives=[("1", "/dev/sr0")], discinfo_payload=_discinfo_payload)

# Patch core._drive_operations (all 8 ops + _internal_only)
import core._drive_operations as _drv

_drv.list_drives = _mock_drive.list_drives
_drv.get_disc_info = _mock_drive.get_disc_info
_drv.refresh_disc_info = _mock_drive.refresh_disc_info
_drv.validate_disc_info = _mock_drive.validate_disc_info
_drv.scan_disc_info = _mock_drive.scan_disc_info
_drv.hash_disc = _mock_drive.hash_disc
_drv.handle_disc_eject = _mock_drive.handle_disc_eject
_drv.handle_disc_insert = _mock_drive.handle_disc_insert


def _noop_internal_only(allowed_callers=None):
    def _decorator(f):
        return f

    return _decorator


_drv._internal_only = _noop_internal_only

# Seed disc_cache so get_cached_discs works
_mock_drive.refresh_disc_info("1", "/dev/sr0")

# api.routers.jobs.validate_disc_info (used by jobs router)
import api.routers.jobs as _jobs

_jobs.validate_disc_info = _mock_drive.validate_disc_info

# MockMKV (titles from the selected fixture)
_mock_mkv = MockMKV(titles=_fixture.mockmkv_titles, progress=True)

import core.utils as _utils
import core.disc as _disc
import api.crud as _crud

_utils.run_makemkv = _mock_mkv.run_makemkv
_disc.run_makemkv = _mock_mkv.run_makemkv
_crud.run_makemkv = _mock_mkv.run_makemkv

# Short-circuit the post-rip preview/detect chain in the test stack (#195/#196).
#
# Under CELERY_TASK_ALWAYS_EAGER=true + single-uvicorn-worker, rip_disc's body
# chains rip_verification, which dispatches preview_raw_titles.delay(...) on
# the MISS branch (Backend/api/routers/jobs.py:3265-3269), which in turn
# dispatches detect_raw_titles.delay(...) from inside its body
# (Backend/workers/tasks.py:4173). All of that runs inline before the
# POST /jobs/rip HTTP handler returns. For the 1917 fixture's MISS branch that
# adds ~3 minutes of preview+detect work under the rip lock, which blows past
# the e2e specs' polling deadlines and traps subsequent specs at "Drive busy".
#
# The integration tests at Backend/tests/test_rip_with_detection.py already
# cover the preview/detect task internals; the dispatch wiring itself is
# backfilled by Backend/tests/test_rip_verification_dispatches_preview_detect.py.
# So no real e2e coverage is lost by no-op'ing dispatch here.
import workers.tasks as _wtasks


class _NoopAsyncResult:
    """Mock for Celery's AsyncResult — exposes the ``id`` attribute the
    dispatch sites read on the return value (see jobs.py:2358, etc.)."""
    id = "e2e-noop"


def _noop_dispatch(*_args, **_kwargs):
    return _NoopAsyncResult()


# Patching .delay AND .apply_async on each task object covers every dispatch
# convention in the codebase (the rip-verification-complete callback uses
# .delay; some other call sites use .apply_async). Patching both costs
# nothing and is defense against future refactors. Patching preview_raw_titles
# alone is functionally sufficient (detect is only dispatched from inside
# preview's body), but we patch both for symmetry.
_wtasks.preview_raw_titles.delay = _noop_dispatch
_wtasks.preview_raw_titles.apply_async = _noop_dispatch
_wtasks.detect_raw_titles.delay = _noop_dispatch
_wtasks.detect_raw_titles.apply_async = _noop_dispatch
print(
    "[e2e_bootstrap] Patched preview_raw_titles/detect_raw_titles dispatch to "
    "no-op — rip_disc's eager chain stops at rip_verification (#195/#196).",
    file=sys.stderr,
)

# Apply fixture-level overrides (DiscDB miss-forcing, DiscDB URL override).
# These ride on top of normal mkv-auto behavior; non-overridden fixtures (e.g.
# real-hit cases) leave settings untouched so live DiscDB lookups happen.
if _fixture.discdb_disabled:
    try:
        from core import settings as _settings

        _settings.set_discdb_disabled(True)
    except Exception as _e:
        print(f"[e2e_bootstrap] WARNING: failed to set discdb_disabled: {_e}",
              file=sys.stderr)
        # Last resort: monkey-patch the getter so downstream code reads True.
        try:
            from core import settings as _settings  # noqa: F811

            _settings.get_discdb_disabled = lambda: True  # type: ignore[assignment]
        except Exception:
            pass

if _fixture.discdb_url_override is not None:
    try:
        _utils.DISKDBURL = _fixture.discdb_url_override  # type: ignore[attr-defined]
    except Exception as _e:
        print(f"[e2e_bootstrap] WARNING: failed to override DISKDBURL: {_e}",
              file=sys.stderr)

# Run ``on_disc_scan_complete`` so the cache reflects what production sees
# after a real scan completes: DiscDB lookup attempted (and possibly failed),
# ``label_required`` / ``discdb_hit`` / ``discdb_miss`` set accordingly, and
# the enriched payload written back to disc_cache.
#
# Without this step, ``refresh_disc_info`` above only dumps the raw fixture
# payload into cache. Fixtures that pre-bake ``label_required`` (e.g. miss.py)
# still work, but fixtures like ``discdb_error`` that rely on the lookup
# *failing* to set ``label_required=True`` end up with ``stage_profile='hit'``
# in ``crud.create_job`` — masking the very "API down → MISS" invariant the
# spec is trying to assert (#195, #196).
#
# The honest test of "API unreachable treated as MISS" requires that the
# unreachable URL actually be dialled during bootstrap; this is where that
# happens. Errors are swallowed (the production callback also logs-and-
# continues — we don't want a transient network blip to refuse to start
# the test stack).
try:
    from core.disc_manager import on_disc_scan_complete as _on_disc_scan_complete

    _on_disc_scan_complete({
        "disc_num": _discinfo_payload.get("disc_num", "1"),
        "mount_point": _discinfo_payload.get("mount_point", "/dev/sr0"),
        "disc_hash": _discinfo_payload.get("disc_hash") or _discinfo_payload.get("content_hash"),
        "info_log": _discinfo_payload.get("info_log") or _discinfo_payload.get("raw_info_log"),
        **_discinfo_payload,
    })
    print(
        f"[e2e_bootstrap] ran on_disc_scan_complete for fixture {_fixture.name} — "
        f"DiscDB lookup attempted against {getattr(_utils, 'DISKDBURL', '<default>')}",
        file=sys.stderr,
    )
except Exception as _e:
    print(
        f"[e2e_bootstrap] WARNING: on_disc_scan_complete raised: {_e}",
        file=sys.stderr,
    )

# Pre-seed records needed for the MISS happy-path spec to drive label → postprocess → transfer:
#   - Movie + Release so labelForm can link disc.release_id without UI navigation.
#   - Active local TransferConfig so POST /jobs/{id}/transfer doesn't 400 on "no active config".
# IDs are deterministic so Frontend/e2e/happy-path-miss.spec.ts can reference them.
if _fixture.expected_workflow == "miss":
    from api.database import SessionLocal as _SessionLocal
    from api import models as _db_models

    _seed_db = _SessionLocal()
    try:
        _movie_id = "e2e-miss-movie-0001"
        _release_id = "e2e-miss-release-0001"

        if not _seed_db.query(_db_models.Movie).filter(_db_models.Movie.id == _movie_id).first():
            _seed_db.add(_db_models.Movie(
                id=_movie_id,
                name="E2E Miss Fixture Movie",
                production_year=2024,
                tmdb_type="movie",
            ))
            _seed_db.flush()

        if not _seed_db.query(_db_models.Release).filter(_db_models.Release.id == _release_id).first():
            _seed_db.add(_db_models.Release(
                id=_release_id,
                slug="e2e-miss-fixture-movie-2024",
                type="movie",
                name="E2E Miss Fixture Movie",
                movie_id=_movie_id,
                release_year=2024,
            ))

        _transfer_dest = (Path(os.environ.get("MKVAUTO_DATA", str(_backend.parent / ".e2e_data"))) / "transfer-dest").resolve()
        _transfer_dest.mkdir(parents=True, exist_ok=True)
        if not _seed_db.query(_db_models.TransferConfig).filter(_db_models.TransferConfig.is_active == True).first():
            _seed_db.add(_db_models.TransferConfig(
                id="e2e-miss-transfer-0001",
                mode="local",
                name="e2e-local-dest",
                is_active=True,
                transfer_dir=str(_transfer_dest),
                config_data={"transfer_dir": str(_transfer_dest)},
            ))

        _seed_db.commit()
        print(
            f"[e2e_bootstrap] seeded MISS records: movie={_movie_id} release={_release_id} "
            f"transfer_dest={_transfer_dest}",
            file=sys.stderr,
        )
    except Exception as _seed_exc:
        _seed_db.rollback()
        print(f"[e2e_bootstrap] WARNING: failed to seed MISS records: {_seed_exc}", file=sys.stderr)
    finally:
        _seed_db.close()
