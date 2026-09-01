"""The resume guard's expectation math (resume_expected_title_count).

Extracted from _run_prep_phase so it can be tested without the quarantined
resume_postprocess integration harness (#416). Prod incident this pins:
disc_info.json held 121 titles, MakeMKV saved exactly the 119 in its rip
set, the user ignored 116 of them — and the old map-based count aborted a
resume ("only 119/121 titles found") that had every file it needed.
"""
from types import SimpleNamespace

from workers.tasks import resume_expected_title_count


def _row(type_=None):
    return SimpleNamespace(type=type_)


def _job(db_titles=None, disc_payload=None, disc_info=None):
    db_disc = None
    if db_titles is not None or disc_info is not None:
        db_disc = SimpleNamespace(titles=db_titles or [], disc_info=disc_info)
    return SimpleNamespace(disc=db_disc, disc_payload=disc_payload or {})


def test_db_rows_win_over_a_stale_map_and_subtract_user_ignores():
    # The prod shape, scaled down: map says 8, DB has 6 rows, user kept 2.
    job = _job(db_titles=[_row("MainMovie"), _row("Trailer")] + [_row("ignore")] * 4)
    scan_map_disc = SimpleNamespace(titles=[SimpleNamespace(type=None)] * 8)
    assert resume_expected_title_count(job, scan_map_disc) == 2


def test_discdb_hit_uses_the_selected_title_map():
    job = _job(
        db_titles=[_row("Episode")] * 10,
        disc_payload={"discdb_hit": True, "title_filename_map": {f"t{i}": f"f{i}.mkv" for i in range(4)}},
    )
    assert resume_expected_title_count(job, SimpleNamespace(titles=[])) == 4


def test_map_fallback_subtracts_map_typed_ignores_when_no_db_rows():
    job = _job(db_titles=[])
    scan_map_disc = SimpleNamespace(titles=[_row("ignore"), _row("ignore"), _row(None), _row("Episode")])
    assert resume_expected_title_count(job, scan_map_disc) == 2


def test_payload_tracks_fallback_when_nothing_else_exists():
    job = _job(disc_payload={"tracks": {"a": {}, "b": {}, "c": {}}})
    assert resume_expected_title_count(job, SimpleNamespace(titles=[])) == 3


def test_empty_everything_yields_zero():
    assert resume_expected_title_count(_job(), SimpleNamespace(titles=[])) == 0
