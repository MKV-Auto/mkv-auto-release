"""The disc-scoped workflow-context must report ``force_independent_group``.

`GET /discs/{disc_id}/workflow-context` builds each title dict twice: once
merged from the cached ``disc_payload``, and — for titles absent from that
cache — from scratch. The from-scratch branch replaced the dict wholesale and
omitted this flag, so the endpoint recomputed dedupe groups as if the user had
never pressed Ungroup.

That made the endpoint disagree with the DB and with the job-scoped builder:
after Ungroup the client refetched here, got the *old* grouping back, and the
left rail never changed (mkv-auto-release#8).
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from api import models, database
from api.main import app


@pytest.fixture
def client(test_db):
    def override_get_db():
        session = test_db()
        try:
            yield session
        finally:
            session.close()

    from api.routers import discs, jobs, events

    app.dependency_overrides[database.get_db] = override_get_db
    for mod in (discs, jobs, events):
        if hasattr(mod, "get_db"):
            app.dependency_overrides[mod.get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _disc_with_group(session, *, ungrouped_index=None):
    """Five same-segment, same-length titles — one wrongly-detected dupe group.

    No ``disc_payload``, so every title takes the from-scratch branch: the one
    that dropped the flag.
    """
    disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:10]}")
    session.add(disc)
    session.flush()
    rows = []
    for i in range(5):
        t = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file=f"title-{i}",
            segment_map="1-5,6,7",
            index=i,
            order_index=i,
            duration=1350 + i,
            active=True,
            force_independent_group=(i == ungrouped_index),
        )
        session.add(t)
        rows.append(t)
    session.commit()
    # Return the id, not the instance: the caller closes the session and a
    # detached Disc cannot refresh its attributes.
    return str(disc.id)


def _members(payload):
    by_id = {t["title_id"]: t["source_file"] for t in payload["titles"]}
    out = []
    for g in payload.get("dedupeGroups") or []:
        out.append({
            by_id[g["representative_title_id"]],
            *[by_id[s] for s in (g.get("sibling_title_ids") or [])],
        })
    return out


class TestDiscContextUngroupFlag:
    def test_flag_is_reported_on_titles(self, client, test_db):
        session = test_db()
        try:
            disc_id = _disc_with_group(session, ungrouped_index=3)
        finally:
            session.close()

        body = client.get(f"/discs/{disc_id}/workflow-context").json()

        flagged = [t["source_file"] for t in body["titles"] if t.get("force_independent_group")]
        assert flagged == ["title-3"], "the endpoint dropped the user's Ungroup"

    def test_ungrouped_title_is_not_a_group_member(self, client, test_db):
        session = test_db()
        try:
            disc_id = _disc_with_group(session, ungrouped_index=3)
        finally:
            session.close()

        body = client.get(f"/discs/{disc_id}/workflow-context").json()

        groups = _members(body)
        assert groups, "expected the remaining titles to still form a group"
        assert all("title-3" not in g for g in groups)

    def test_group_is_intact_when_nothing_is_ungrouped(self, client, test_db):
        # Guards the assertion above against passing because grouping broke.
        session = test_db()
        try:
            disc_id = _disc_with_group(session)
        finally:
            session.close()

        body = client.get(f"/discs/{disc_id}/workflow-context").json()

        groups = _members(body)
        assert len(groups) == 1
        assert groups[0] == {f"title-{i}" for i in range(5)}
