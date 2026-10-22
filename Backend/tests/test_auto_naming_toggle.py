"""#915: the per-disc "Automatic naming" toggle.

Clearing the disc name to regenerate it was unintuitive: it reverted text
the user was trying to replace. The toggle makes the choice explicit: on,
MKV-Auto renders the name and slug; off, the user's values are kept as typed.
"""
from __future__ import annotations

import uuid

from core.disc_naming import (
    apply_naming_from_label,
    effective_auto_naming,
    refresh_auto_disc_identity,
)
from tests.test_disc_naming import _disc


def _with_toggle(disc, value):
    disc.auto_naming = value
    return disc


# --- effective_auto_naming -------------------------------------------------

def test_an_explicit_choice_wins():
    assert effective_auto_naming(_with_toggle(_disc("Thor", fmt="DVD", disc_name="My Thor"), True)) is True
    assert effective_auto_naming(_with_toggle(_disc("Thor", fmt="DVD", disc_name="Thor - DVD"), False)) is False


def test_without_a_choice_it_follows_the_stored_name():
    assert effective_auto_naming(_disc("Thor", fmt="DVD", disc_name="Thor - DVD")) is True      # our convention
    assert effective_auto_naming(_disc("Thor", fmt="DVD", disc_name="DVD")) is True             # bare format
    assert effective_auto_naming(_disc("Thor", fmt="DVD", disc_name=None)) is True              # blank
    assert effective_auto_naming(_disc("Thor", fmt="DVD", disc_name="Thor SteelBook")) is False  # typed


def test_a_hand_typed_slug_starts_custom_so_it_is_never_overwritten():
    assert effective_auto_naming(_disc("Thor", fmt="DVD", disc_name="Thor - DVD", disc_slug="my-own")) is False
    assert effective_auto_naming(_disc("Thor", fmt="DVD", disc_name="Thor - DVD", disc_slug="thor_-_dvd")) is True


def test_a_discdb_composite_name_starts_automatic():
    """TheDiscDB's composite once overwrote the convention name and froze
    auto-naming by passing for user-typed; it stays machine-owned."""
    d = _disc("Star Wars: The Clone Wars", "tv", fmt="DVD", disc_number=12, season=4,
              release_name="Season 1-5 Collector's Edition",
              disc_name="Star Wars: The Clone Wars - Season 1-5 Collector's Edition - Season 4 - Disc 1 - DVD")
    assert effective_auto_naming(d) is True


# --- apply_naming_from_label ---------------------------------------------

def test_with_automatic_naming_on_stale_names_in_the_save_are_ignored():
    d = _with_toggle(_disc("Thor", fmt="DVD", disc_name="Thor - DVD", disc_slug="thor_-_dvd"), True)
    apply_naming_from_label(d, {"auto_naming": True, "disc_name": "Old", "disc_slug": "x"}, default_name="DVD")
    assert (d.disc_name, d.disc_slug) == ("Thor - DVD", "thor_-_dvd")


def test_a_name_sent_without_the_toggle_is_the_users():
    """Pre-#915 clients and API callers send disc_name only for user edits."""
    d = _disc("Thor", fmt="DVD", disc_name="Thor - DVD", disc_slug="thor_-_dvd")
    apply_naming_from_label(d, {"disc_name": "Thor SteelBook"}, default_name="DVD")
    assert d.auto_naming is False
    assert (d.disc_name, d.disc_slug) == ("Thor SteelBook", "thor_steelbook")


def test_turning_automatic_naming_off_keeps_the_users_values():
    d = _disc("Thor", fmt="DVD", disc_name="Thor - DVD", disc_slug="thor_-_dvd")
    apply_naming_from_label(d, {"auto_naming": False, "disc_name": "Thor SteelBook", "disc_slug": "steelbook"}, "DVD")
    assert d.auto_naming is False
    assert (d.disc_name, d.disc_slug) == ("Thor SteelBook", "steelbook")


def test_with_it_off_an_empty_name_keeps_the_stored_one_and_an_empty_slug_is_generated():
    d = _with_toggle(_disc("Thor", fmt="DVD", disc_name="Thor SteelBook", disc_slug="old"), False)
    apply_naming_from_label(d, {"disc_name": "", "disc_slug": ""}, "DVD")
    assert d.disc_name == "Thor SteelBook"          # a disc always has a name
    assert d.disc_slug == "thor_steelbook"


def test_a_blank_disc_gets_the_scan_default_either_way():
    d = _disc(None, fmt="DVD", disc_name=None, disc_slug=None)
    apply_naming_from_label(d, {}, "Some Title - DVD")
    assert d.disc_name == "Some Title - DVD" and d.disc_slug


# --- refresh_auto_disc_identity ------------------------------------------

def test_off_is_never_refreshed_even_when_the_name_looks_automatic():
    d = _with_toggle(_disc("Thor", fmt="DVD", disc_name="DVD"), False)
    assert refresh_auto_disc_identity(d) is False
    assert d.disc_name == "DVD"


def test_on_renders_the_convention_and_the_slug_follows():
    d = _with_toggle(_disc("Thor", fmt="DVD", disc_name="Thor SteelBook", disc_slug="steelbook"), True)
    assert refresh_auto_disc_identity(d) is True
    assert d.disc_name == "Thor - DVD"
    assert d.disc_slug == "thor_-_dvd"            # read-only while automatic


def test_turning_it_back_on_regenerates_through_the_label_save():
    d = _with_toggle(_disc("Thor", fmt="DVD", disc_name="Thor SteelBook", disc_slug="steelbook"), False)
    apply_naming_from_label(d, {"auto_naming": True}, "DVD")
    assert refresh_auto_disc_identity(d) is True
    assert (d.disc_name, d.disc_slug) == ("Thor - DVD", "thor_-_dvd")


# --- API round trip --------------------------------------------------------

def test_label_form_round_trips_the_toggle(test_db):
    from fastapi.testclient import TestClient

    from api import models
    from api.main import app

    with test_db() as db:
        movie = models.Movie(id=str(uuid.uuid4()), name="Thor", tmdb_type="movie")
        release = models.Release(id=str(uuid.uuid4()), movie_id=movie.id, name="Thor", slug=f"thor-{uuid.uuid4().hex[:6]}")
        disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:8]}", format="DVD",
                           release_id=release.id, disc_number=1, disc_name="Thor - DVD", disc_slug="thor_-_dvd")
        db.add_all([movie, release, disc]); db.flush()
        job = models.Job(id=str(uuid.uuid4()), disc_id=disc.id, disc_num="0", mount_point="/dev/sr0",
                         job_status="running", rip_state="completed", stage_profile="miss", workflow_step="disc")
        db.add(job); db.commit()
        job_id, disc_id = job.id, disc.id

    client = TestClient(app)
    got = client.get(f"/jobs/{job_id}/workflow-context").json()
    assert got["labelForm"]["auto_naming"] is True

    r = client.patch(f"/jobs/{job_id}/workflow-context", json={"labelForm": {
        "auto_naming": False, "disc_name": "Thor SteelBook", "disc_slug": "steelbook"}})
    assert r.status_code == 200, r.text
    with test_db() as db:
        d = db.get(models.Disc, disc_id)
        assert (d.auto_naming, d.disc_name, d.disc_slug) == (False, "Thor SteelBook", "steelbook")
    assert client.get(f"/jobs/{job_id}/workflow-context").json()["labelForm"]["auto_naming"] is False

    r = client.patch(f"/jobs/{job_id}/workflow-context", json={"labelForm": {"auto_naming": True}})
    assert r.status_code == 200, r.text
    with test_db() as db:
        d = db.get(models.Disc, disc_id)
        assert (d.auto_naming, d.disc_name, d.disc_slug) == (True, "Thor - DVD", "thor_-_dvd")
