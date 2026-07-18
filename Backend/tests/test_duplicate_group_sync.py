"""Tests for duplicate group sync (Option B) and set-primary enforcement."""
import uuid

import pytest
from fastapi.testclient import TestClient

from api import database, models
from api.main import app
from api.routers.jobs import _validate_all_titles_labeled
from core.duplicate_group_sync import sync_duplicate_group_labels_for_disc


@pytest.fixture
def client(test_db):
    def override_get_db():
        try:
            session = test_db()
            yield session
        finally:
            session.close()

    from api.routers import jobs

    app.dependency_overrides[database.get_db] = override_get_db
    if hasattr(jobs, "get_db"):
        app.dependency_overrides[jobs.get_db] = override_get_db

    yield TestClient(app)
    app.dependency_overrides.clear()


def test_sync_duplicate_group_one_primary_rest_ignore_and_cleared(test_db):
    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="dup-sync-1")
        session.add(disc)
        session.flush()
        sm = "1,2,3"
        tid_a = str(uuid.uuid4())
        tid_b = str(uuid.uuid4())
        t_a = models.DiscTitle(
            id=tid_a,
            disc_id=disc.id,
            source_file="a.mkv",
            segment_map=sm,
            type="movie",
            title="Primary Movie",
            active=True,
            order_index=0,
        )
        t_b = models.DiscTitle(
            id=tid_b,
            disc_id=disc.id,
            source_file="b.mkv",
            segment_map=sm,
            type="episode",
            title="Should be cleared",
            season=2,
            episode=5,
            edition="X",
            description="d",
            comment="c",
            active=False,
            order_index=1,
        )
        session.add_all([t_a, t_b])
        session.commit()
        modified = sync_duplicate_group_labels_for_disc(session, disc.id)
        assert modified >= 1
        session.flush()
        session.refresh(t_a)
        session.refresh(t_b)
        assert t_a.active is True
        assert (t_a.type or "").lower() == "movie"
        assert t_a.title == "Primary Movie"
        assert t_b.active is False
        assert (t_b.type or "").lower() == "ignore"
        assert t_b.title is None
        assert t_b.season is None
        assert t_b.episode is None
        assert t_b.edition is None
        assert t_b.description is None
        assert t_b.comment == "c"
    finally:
        session.close()


def test_validate_all_titles_labeled_passes_when_only_primary_is_labeled(test_db):
    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="dup-val-1")
        session.add(disc)
        session.flush()
        sm = "10,11"
        t_a = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="p.mkv",
            segment_map=sm,
            type="movie",
            title="Named",
            active=True,
            order_index=0,
        )
        t_b = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="s.mkv",
            segment_map=sm,
            type="episode",
            title="Incomplete",
            season=None,
            episode=None,
            # Both active until sync runs — otherwise validation skips the secondary (sole-primary rule).
            active=True,
            order_index=1,
        )
        session.add_all([t_a, t_b])
        session.commit()
        ok, bad = _validate_all_titles_labeled(disc, session)
        assert ok is False
        sync_duplicate_group_labels_for_disc(session, disc.id)
        session.commit()
        session.refresh(t_a)
        session.refresh(t_b)
        ok2, bad2 = _validate_all_titles_labeled(disc, session)
        assert ok2 is True
        assert bad2 == []
    finally:
        session.close()


def test_validate_all_titles_labeled_skips_active_false_duplicate_secondaries(test_db):
    """Label complete must match UI: only primary in a duplicate group is required (Option B secondaries)."""
    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="dup-skip-val-1")
        session.add(disc)
        session.flush()
        sm = "99,100"
        t_pri = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="primary.mpls",
            segment_map=sm,
            type="MainMovie",
            title="The Movie",
            active=True,
            order_index=0,
        )
        t_sec = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="secondary.mpls",
            segment_map=sm,
            type="episode",
            title=None,
            season=None,
            episode=None,
            active=None,
            order_index=1,
        )
        session.add_all([t_pri, t_sec])
        session.commit()
        ok, bad = _validate_all_titles_labeled(disc, session)
        assert ok is True
        assert bad == []
    finally:
        session.close()


def test_set_primary_swaps_metadata_and_demotes_old_primary(client, test_db):
    session = test_db()
    disc_id = None
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="dup-primary-1")
        session.add(disc)
        session.flush()
        disc_id = str(disc.id)
        sm = "7,8,9"
        tid_old = str(uuid.uuid4())
        tid_new = str(uuid.uuid4())
        old_p = models.DiscTitle(
            id=tid_old,
            disc_id=disc.id,
            source_file="old.mkv",
            segment_map=sm,
            type="movie",
            title="Shared Name",
            edition="Director",
            description="desc",
            comment="note",
            active=True,
            order_index=0,
        )
        new_t = models.DiscTitle(
            id=tid_new,
            disc_id=disc.id,
            source_file="new.mkv",
            segment_map=sm,
            type="ignore",
            title=None,
            comment="new-rip.mkv",
            active=False,
            order_index=1,
        )
        session.add_all([old_p, new_t])
        session.commit()
    finally:
        session.close()

    r = client.post(f"/discs/{disc_id}/titles/{tid_new}/set-primary")
    assert r.status_code == 200
    body = r.json()
    titles = {t["title_id"]: t for t in body["titles"]}
    assert titles[tid_new]["type"] == "MainMovie"
    assert titles[tid_new]["title"] == "Shared Name"
    assert titles[tid_old]["type"] == "ignore"
    assert titles[tid_old]["title"] is None
    assert titles[tid_old].get("comment") == "note"
    assert titles[tid_new].get("comment") == "new-rip.mkv"

    session = test_db()
    try:
        old_row = session.query(models.DiscTitle).filter(models.DiscTitle.id == tid_old).first()
        new_row = session.query(models.DiscTitle).filter(models.DiscTitle.id == tid_new).first()
        assert old_row.active is False
        assert (old_row.type or "").lower() == "ignore"
        assert new_row.active is True
        assert old_row.comment == "note"
        assert new_row.comment == "new-rip.mkv"
    finally:
        session.close()


def test_sync_leaves_null_type_on_primary_when_only_demoted_secondaries(test_db):
    """Primary stays NULL when its only sibling is an auto-demoted secondary.

    The secondary's ``type='ignore'`` was set by ``apply_secondary_duplicate_row``
    as a structural consequence of the primary being picked — it cannot
    legitimately "vote" to mark the primary as ignore too. Surfaced on
    Fallout S2 where every episode's .mpls primary was hidden because the
    matching .m2ts secondary's auto-ignore propagated back. Postprocess
    ``_rename_movie`` / ``_rename_series`` fall back to ``Track{tid}`` for
    NULL-typed titles, so leaving the primary unfilled is safe.
    """
    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="dup-null-fill-1")
        session.add(disc)
        session.flush()
        sm = "461"
        primary = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="01136.mpls",
            segment_map=sm,
            type=None,
            title=None,
            active=True,
            order_index=0,
        )
        secondary = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="00461.m2ts",
            segment_map=sm,
            type="ignore",
            title=None,
            active=False,
            order_index=1,
        )
        session.add_all([primary, secondary])
        session.commit()
        sync_duplicate_group_labels_for_disc(session, disc.id)
        session.commit()
        session.refresh(primary)
        session.refresh(secondary)
        assert primary.active is True
        assert primary.type is None, (
            f"primary must stay NULL — demoted secondary cannot vote it to ignore. got {primary.type!r}"
        )
        assert secondary.active is False
        assert (secondary.type or "").lower() == "ignore"
    finally:
        session.close()


def test_sync_does_not_overwrite_existing_primary_type(test_db):
    """Sync must not clobber an explicit user/auto choice on the primary."""
    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="dup-no-clobber-1")
        session.add(disc)
        session.flush()
        sm = "200"
        primary = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="primary.mpls",
            segment_map=sm,
            type="MainMovie",
            title="The Movie",
            active=True,
            order_index=0,
        )
        secondary = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="secondary.m2ts",
            segment_map=sm,
            type="ignore",
            active=False,
            order_index=1,
        )
        session.add_all([primary, secondary])
        session.commit()
        sync_duplicate_group_labels_for_disc(session, disc.id)
        session.commit()
        session.refresh(primary)
        assert (primary.type or "") == "MainMovie"
        assert primary.title == "The Movie"
    finally:
        session.close()


def test_apply_primary_duplicate_row_strict_consensus_at_call_time(test_db):
    """`apply_primary_duplicate_row` directly: a NULL sibling in `group_members` blocks NULL fill."""
    from core.duplicate_group_sync import apply_primary_duplicate_row

    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="dup-strict-1")
        session.add(disc)
        session.flush()
        sm = "777"
        primary = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="primary.mpls",
            segment_map=sm,
            type=None,
            active=True,
            order_index=0,
        )
        sib_ignore = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="a.m2ts",
            segment_map=sm,
            type="ignore",
            active=False,
            order_index=1,
        )
        sib_null = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="b.m2ts",
            segment_map=sm,
            type=None,
            active=False,
            order_index=2,
        )
        session.add_all([primary, sib_ignore, sib_null])
        session.commit()
        # Direct call (bypasses sync's two-pass demotion): NULL sibling forbids inference.
        apply_primary_duplicate_row(
            primary, group_members=[primary, sib_ignore, sib_null]
        )
        assert primary.type is None
        # And without the group_members hint, NULL is also preserved.
        apply_primary_duplicate_row(primary)
        assert primary.type is None
    finally:
        session.close()


def test_validate_accepts_null_primary_when_siblings_are_ignore(test_db):
    """Production bug repro: pre-sync state with NULL active primary + ignored secondary must validate."""
    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="dup-validate-null-1")
        session.add(disc)
        session.flush()
        sm = "461"
        primary = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="01136.mpls",
            segment_map=sm,
            type=None,
            title=None,
            active=True,
            order_index=0,
        )
        secondary = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="00461.m2ts",
            segment_map=sm,
            type="ignore",
            active=False,
            order_index=1,
        )
        session.add_all([primary, secondary])
        session.commit()
        ok, bad = _validate_all_titles_labeled(disc, session)
        assert ok is True
        assert bad == []
    finally:
        session.close()


def test_validate_rejects_null_type_for_solo_row_outside_duplicate_group(test_db):
    """A NULL-type row with no duplicate siblings is genuinely unlabeled — must fail."""
    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="dup-validate-solo-1")
        session.add(disc)
        session.flush()
        solo = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="solo.mpls",
            segment_map="555",
            type=None,
            title=None,
            active=True,
            order_index=0,
        )
        session.add(solo)
        session.commit()
        ok, bad = _validate_all_titles_labeled(disc, session)
        assert ok is False
        assert str(solo.id) in bad
    finally:
        session.close()


def test_validate_rejects_null_primary_when_sibling_has_real_type(test_db):
    """If any sibling has a real (non-ignore) type, the NULL primary is not auto-accepted."""
    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="dup-validate-mixed-1")
        session.add(disc)
        session.flush()
        sm = "999"
        primary = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="primary.mpls",
            segment_map=sm,
            type=None,
            title=None,
            active=True,
            order_index=0,
        )
        sibling = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="sibling.m2ts",
            segment_map=sm,
            type="MainMovie",
            title="Real",
            active=False,
            order_index=1,
        )
        session.add_all([primary, sibling])
        session.commit()
        ok, bad = _validate_all_titles_labeled(disc, session)
        assert ok is False
        assert str(primary.id) in bad
    finally:
        session.close()


def test_complete_label_leaves_null_primaries_when_only_demoted_secondaries(test_db, monkeypatch):
    """End-to-end: complete_label (via save_label path) must leave NULL primaries NULL when
    their only siblings are auto-demoted secondaries — postprocess ``_rename_movie`` now uses
    ``Track{tid}`` for any title with no resolved name, so the 17/20 collision can't occur.

    The previous behavior auto-filled the primaries with ``type='ignore'`` via consensus-fill,
    which falsely hid real episodes (Fallout S2 surfaced this). The validator
    ``_validate_all_titles_labeled`` accepts NULL primary + ignored siblings as labeled.
    """
    from api.routers.jobs import _apply_label_to_records, _validate_all_titles_labeled
    from core.duplicate_group_sync import sync_duplicate_group_labels_for_disc

    monkeypatch.setattr(
        "api.crud.normalize_disc_numbers_for_release",
        lambda db, rel, exclude_disc_id=None: {},
    )

    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="dup-complete-1")
        session.add(disc)
        session.flush()
        # Match the production fixture: 4 segment_map groups, each with one .mpls primary
        # (NULL/active=True) and one .m2ts secondary (ignore/active=False).
        primaries = []
        for sm in ("461", "516", "560", "572"):
            primary = models.DiscTitle(
                id=str(uuid.uuid4()),
                disc_id=disc.id,
                source_file=f"primary-{sm}.mpls",
                segment_map=sm,
                type=None,
                title=None,
                active=True,
                order_index=0,
            )
            secondary = models.DiscTitle(
                id=str(uuid.uuid4()),
                disc_id=disc.id,
                source_file=f"secondary-{sm}.m2ts",
                segment_map=sm,
                type="ignore",
                active=False,
                order_index=1,
            )
            session.add_all([primary, secondary])
            primaries.append(primary)
        session.commit()

        # Simulate complete_label's sequence: apply (no-op tracks), sync, validate.
        _apply_label_to_records(disc, {"tracks": []}, session)
        sync_duplicate_group_labels_for_disc(session, str(disc.id))
        session.commit()

        for p in primaries:
            session.refresh(p)
            assert p.type is None, (
                f"Primary {p.source_file} should stay NULL — demoted secondaries cannot "
                f"vote it to ignore. got {p.type!r}"
            )
            assert p.active is True

        ok, bad = _validate_all_titles_labeled(disc, session)
        assert ok is True
        assert bad == []
    finally:
        session.close()


def test_per_patch_sync_does_not_revert_user_unignore_on_primary(test_db):
    """Regression: V for Vendetta ignore-toggle revert.

    User clicks unignore on a primary whose duplicate sibling is still 'ignore'. The patch
    endpoint clears the primary's type (NULL); per-patch sync must NOT consensus-fill it back
    to 'ignore' from sibling state. Bulk-sync paths (label save/complete, scan) keep the
    auto-fill so groups still self-heal at the right boundaries — only per-patch is conservative.
    """
    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="dup-patch-unignore-1")
        session.add(disc)
        session.flush()
        sm = "61"
        primary = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="00800.mpls",
            segment_map=sm,
            type=None,            # user just unignored → patch normalized "" to NULL
            title="V for Vendetta",
            active=True,
            order_index=0,
        )
        secondary = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="00801.mpls",
            segment_map=sm,
            type="ignore",
            active=False,
            order_index=1,
        )
        session.add_all([primary, secondary])
        session.commit()

        # Per-patch: fill_null_type_from_consensus=False — must NOT clobber user's NULL.
        sync_duplicate_group_labels_for_disc(
            session, str(disc.id), fill_null_type_from_consensus=False
        )
        session.commit()
        session.refresh(primary)
        session.refresh(secondary)

        assert primary.type is None, (
            "primary type must remain NULL after per-patch sync — consensus-fill must be "
            f"gated off; got {primary.type!r}"
        )
        # Sibling still ignore/inactive — invariant intact.
        assert (secondary.type or "").lower() == "ignore"
        assert secondary.active is False
        # Primary remains active (set on every sync regardless of consensus-fill flag).
        assert primary.active is True
    finally:
        session.close()


def test_bulk_sync_leaves_null_type_on_primary_with_demoted_secondaries(test_db):
    """Bulk sync (default ``fill_null_type_from_consensus=True``) no longer consensus-fills.

    Mirror of ``test_sync_leaves_null_type_on_primary_when_only_demoted_secondaries`` —
    bulk sync paths used to flip NULL primaries to 'ignore' via the consensus-fill, which
    misfired whenever a 2-row duplicate group existed (demoted secondary "votes" ignore
    onto the primary). With the consensus-fill removed, both bulk and per-patch sync
    leave the primary's NULL alone; the ``fill_null_type_from_consensus`` parameter is
    now a no-op kept on the signature for callsite back-compat.
    """
    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="dup-bulk-fill-1")
        session.add(disc)
        session.flush()
        sm = "461"
        primary = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="primary.mpls",
            segment_map=sm,
            type=None,
            active=True,
            order_index=0,
        )
        secondary = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="secondary.m2ts",
            segment_map=sm,
            type="ignore",
            active=False,
            order_index=1,
        )
        session.add_all([primary, secondary])
        session.commit()

        sync_duplicate_group_labels_for_disc(session, str(disc.id))
        session.commit()
        session.refresh(primary)
        session.refresh(secondary)

        assert primary.type is None, (
            f"bulk sync must leave NULL primary alone — demoted secondary cannot vote "
            f"it to ignore. got {primary.type!r}"
        )
        assert primary.active is True
        assert (secondary.type or "").lower() == "ignore"
        assert secondary.active is False
    finally:
        session.close()


def test_validate_rejects_null_when_zero_actives_in_group(test_db):
    """The sole-active-primary precondition must be intact: zero actives means no skip and no acceptance."""
    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="dup-validate-no-active-1")
        session.add(disc)
        session.flush()
        sm = "1234"
        a = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="a.mpls",
            segment_map=sm,
            type=None,
            active=False,
            order_index=0,
        )
        b = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="b.m2ts",
            segment_map=sm,
            type="ignore",
            active=False,
            order_index=1,
        )
        session.add_all([a, b])
        session.commit()
        ok, bad = _validate_all_titles_labeled(disc, session)
        assert ok is False
        assert str(a.id) in bad
    finally:
        session.close()


def test_642_sub3_m2ts_folded_into_wrapping_mpls_group(test_db):
    """#642 sub-3: m2ts titles wrapped by an mpls (segment_map contains the
    m2ts's clip id) must be added to the wrapper's duplicate group and
    demoted to ignore. Repros the Harry Potter 8-Film Collection Blu-Ray
    Cell 7 case where 00101.mpls wraps 00218.m2ts / 00217.m2ts / etc.
    """
    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="dup-sync-hp")
        session.add(disc)
        session.flush()
        # 00101.mpls: wrapper playlist referencing three clips.
        mpls = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="00101.mpls",
            segment_map="212,217,218",
            index=14,
            type="MainMovie",
            title="Feature",
            active=True,
            order_index=0,
        )
        # m2ts component clips — each with a single-segment segment_map matching
        # their clip id. Type is NULL: user hasn't decided; auto detection
        # never marked them as subsumed.
        m2ts_a = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="00218.m2ts",
            segment_map="218",
            index=16,
            type=None,
            active=False,
            order_index=1,
        )
        m2ts_b = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="00217.m2ts",
            segment_map="217",
            index=18,
            type=None,
            active=False,
            order_index=2,
        )
        m2ts_c = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="00212.m2ts",
            segment_map="212",
            index=27,
            type=None,
            active=False,
            order_index=3,
        )
        session.add_all([mpls, m2ts_a, m2ts_b, m2ts_c])
        session.commit()
        modified = sync_duplicate_group_labels_for_disc(session, disc.id)
        # All three m2ts get demoted to ignore.
        assert modified >= 3
        session.flush()
        session.refresh(mpls)
        session.refresh(m2ts_a)
        session.refresh(m2ts_b)
        session.refresh(m2ts_c)
        # mpls stays as primary Main Movie.
        assert mpls.active is True
        assert mpls.type == "MainMovie"
        # m2ts wrappers demoted.
        for m in (m2ts_a, m2ts_b, m2ts_c):
            assert (m.type or "").lower() == "ignore", f"{m.source_file} type={m.type}"
            assert m.active is False
    finally:
        session.close()


def test_642_sub3_m2ts_not_folded_when_no_wrapper_present(test_db):
    """When an m2ts's clip id isn't referenced by any mpls's segment_map on
    the disc, it stays a standalone entity (no false folding).
    """
    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="dup-sync-orphan")
        session.add(disc)
        session.flush()
        # Standalone m2ts + a completely unrelated mpls.
        m2ts = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="00999.m2ts",
            segment_map="999",
            index=1,
            type=None,
            active=False,
            order_index=0,
        )
        mpls = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file="00500.mpls",
            segment_map="100,101,102",
            index=2,
            type=None,
            active=True,
            order_index=1,
        )
        session.add_all([m2ts, mpls])
        session.commit()
        sync_duplicate_group_labels_for_disc(session, disc.id)
        session.refresh(m2ts)
        # m2ts type is unchanged — no wrapper referenced its clip id.
        assert m2ts.type is None
    finally:
        session.close()
