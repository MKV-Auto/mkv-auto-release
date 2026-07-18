"""
Coverage for the files_already_moved per-rip discovery precedence
(#365 transient/ drop step 4b).

The ``files_already_moved`` branch of ``_run_prep_phase`` runs when
prep crashed mid-flight and we resume. Previously it tried two
sources in order:

1. ``job.disc_payload["post_paths"]`` (if UUID-keyed)
2. Fall back to ``gather_final_outputs(trans_root, None, ...)``
   which walks trans_root looking for disc_titles matches

Step 3b's ``MKVAUTO_RENAME_DIRECT_TO_DEST`` flag made (2) unsafe
when trans_root resolves to a shared library — the walk would
discover MKVs from unrelated rips.

This PR adds an intermediate step + tightens the fallback:

1. Restored ``disc_payload.post_paths`` (UUID-keyed)
2. **Persisted ``job.post_paths`` column** (UUID-keyed) — NEW
3. ``gather_final_outputs(trans_root, persisted_or_restored, ...)``
   passing post_paths as seed so the walk is skipped entirely when
   possible

These tests reproduce that precedence chain in isolation. The actual
production code is inline in _run_prep_phase, but the helper logic
is pure: input (disc_payload, job.post_paths) → output (which dict
takes precedence + log message).
"""
from types import SimpleNamespace

import pytest


def _resolve_files_already_moved_source(job):
    """Reproduces the precedence logic from the
    files_already_moved branch in _run_prep_phase.

    Returns (source_name, paths_dict).
    """
    def _keys_are_uuids(d):
        return d and all(len(str(k)) == 36 and "-" in str(k) for k in (d or {}))

    restored = (getattr(job, "disc_payload", None) or {}).get("post_paths") or {}
    persisted = getattr(job, "post_paths", None) or {}

    if _keys_are_uuids(restored):
        return "disc_payload.post_paths", restored
    if _keys_are_uuids(persisted):
        return "job.post_paths", persisted
    return "gather_final_outputs_fallback", {}


# A valid UUID-shaped key for tests
UUID_KEY = "0000fffe-0000-4000-8000-000000000001"


# ──────────────────────────────────────────────────────────────────────────
# Precedence: disc_payload wins when it has UUID keys
# ──────────────────────────────────────────────────────────────────────────


def test_disc_payload_post_paths_wins_when_uuid_keyed():
    """When disc_payload has post_paths with UUID keys, use it
    (highest priority — most-recent in-process write)."""
    job = SimpleNamespace(
        disc_payload={"post_paths": {UUID_KEY: "Movies/A/A.mkv"}},
        post_paths={UUID_KEY: "Movies/B_OLD/B.mkv"},  # stale, ignored
    )
    source, paths = _resolve_files_already_moved_source(job)
    assert source == "disc_payload.post_paths"
    assert paths == {UUID_KEY: "Movies/A/A.mkv"}


def test_disc_payload_non_uuid_keys_skipped():
    """If disc_payload has legacy non-UUID keys (e.g. source filenames
    from a much older code path), skip it — UUID keys are required
    for trustworthy per-title mapping."""
    job = SimpleNamespace(
        disc_payload={"post_paths": {"00001.mpls": "Movies/A/A.mkv"}},  # legacy
        post_paths={UUID_KEY: "Movies/B/B.mkv"},
    )
    source, paths = _resolve_files_already_moved_source(job)
    assert source == "job.post_paths"
    assert paths == {UUID_KEY: "Movies/B/B.mkv"}


# ──────────────────────────────────────────────────────────────────────────
# Precedence: job.post_paths is the new intermediate fallback (4b)
# ──────────────────────────────────────────────────────────────────────────


def test_job_post_paths_used_when_disc_payload_empty():
    """When disc_payload has no post_paths but job.post_paths exists
    with UUID keys, use that. This is the canonical persisted
    location after a successful postprocess-complete callback."""
    job = SimpleNamespace(
        disc_payload={},
        post_paths={UUID_KEY: "Movies/X/X.mkv"},
    )
    source, paths = _resolve_files_already_moved_source(job)
    assert source == "job.post_paths"
    assert paths == {UUID_KEY: "Movies/X/X.mkv"}


def test_job_post_paths_used_when_disc_payload_post_paths_missing():
    """disc_payload exists but doesn't have a 'post_paths' key →
    fall through to job.post_paths."""
    job = SimpleNamespace(
        disc_payload={"other_key": "other_value"},
        post_paths={UUID_KEY: "Movies/Y/Y.mkv"},
    )
    source, paths = _resolve_files_already_moved_source(job)
    assert source == "job.post_paths"


def test_job_post_paths_skipped_with_non_uuid_keys():
    """job.post_paths with non-UUID keys also fails the precedence
    check; falls through to the gather walk fallback."""
    job = SimpleNamespace(
        disc_payload={},
        post_paths={"some-legacy-key": "Movies/Z/Z.mkv"},
    )
    source, paths = _resolve_files_already_moved_source(job)
    assert source == "gather_final_outputs_fallback"


# ──────────────────────────────────────────────────────────────────────────
# Fallback: when neither source has UUID-keyed data
# ──────────────────────────────────────────────────────────────────────────


def test_fallback_when_both_sources_empty():
    """Neither disc_payload nor job.post_paths usable → fall to
    gather_final_outputs walk. The walk is the LAST resort because
    it's the unsafe one under the shared-trans_root flag."""
    job = SimpleNamespace(disc_payload={}, post_paths={})
    source, _ = _resolve_files_already_moved_source(job)
    assert source == "gather_final_outputs_fallback"


def test_fallback_when_disc_payload_is_none():
    """Defensive: disc_payload being None (legacy jobs) shouldn't
    crash the resolver."""
    job = SimpleNamespace(disc_payload=None, post_paths={})
    source, _ = _resolve_files_already_moved_source(job)
    assert source == "gather_final_outputs_fallback"


def test_fallback_when_post_paths_is_none():
    """Defensive: post_paths being None (legacy jobs) shouldn't crash."""
    job = SimpleNamespace(disc_payload={}, post_paths=None)
    source, _ = _resolve_files_already_moved_source(job)
    assert source == "gather_final_outputs_fallback"


# ──────────────────────────────────────────────────────────────────────────
# Safety property: the fallback is now reachable only when there's no
# trustworthy per-rip mapping at all. Under the shared-trans_root flag
# this is the only scenario where the walk runs, and the walk uses
# disc_titles to filter — so cross-rip contamination is still bounded.
# ──────────────────────────────────────────────────────────────────────────


def test_fallback_used_only_when_persisted_state_missing():
    """Sanity: the precedence ordering means the fallback only runs
    when BOTH preferred sources are empty/invalid. The fallback is
    no longer the default for files_already_moved — it's the last
    resort. This is the structural change step 4b makes."""
    # Every "has data" combination should NOT fall to the walk.
    has_data_cases = [
        SimpleNamespace(
            disc_payload={"post_paths": {UUID_KEY: "a.mkv"}}, post_paths={},
        ),
        SimpleNamespace(
            disc_payload={}, post_paths={UUID_KEY: "a.mkv"},
        ),
        SimpleNamespace(
            disc_payload={"post_paths": {UUID_KEY: "a.mkv"}},
            post_paths={UUID_KEY: "b.mkv"},
        ),
    ]
    for job in has_data_cases:
        source, _ = _resolve_files_already_moved_source(job)
        assert source != "gather_final_outputs_fallback", (
            f"{job} should resolve via the per-rip mapping, not fall to "
            f"the unsafe walk; got source={source}"
        )
