"""Per-title mutations must invalidate the cached workflow context.

`workflow-context` responses are cached in-process for
``_CONTEXT_CACHE_TTL_SECONDS``. Five mutating endpoints already invalidate;
``ungroup-duplicate`` and ``set-primary`` did not.

That made Ungroup look broken in a way no amount of client-side work could fix:
the button POSTs, the client refetches immediately, and the cache serves the
pre-ungroup grouping for up to 10s — so the left rail does not move. Waiting
out the TTL or reloading "fixed" it, which is what made the failure look
intermittent (mkv-auto-release#8).
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from api import models, database
from api.main import app
from api.routers import discs as discs_router


@pytest.fixture
def client(test_db):
    def override_get_db():
        session = test_db()
        try:
            yield session
        finally:
            session.close()

    from api.routers import jobs, events

    app.dependency_overrides[database.get_db] = override_get_db
    for mod in (discs_router, jobs, events):
        if hasattr(mod, "get_db"):
            app.dependency_overrides[mod.get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def clear_cache():
    discs_router._workflow_context_cache.clear()
    yield
    discs_router._workflow_context_cache.clear()


def _disc_with_group(session, n=3):
    disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:10]}")
    session.add(disc)
    session.flush()
    ids = []
    for i in range(n):
        t = models.DiscTitle(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            source_file=f"title-{i}",
            segment_map="1-5,6,7",
            index=i,
            order_index=i,
            duration=1350 + i,
            active=True,
        )
        session.add(t)
        ids.append(str(t.id))
    session.commit()
    return str(disc.id), ids


class TestCacheInvalidation:
    def test_ungroup_evicts_the_cached_context(self, client, test_db):
        session = test_db()
        try:
            disc_id, title_ids = _disc_with_group(session)
        finally:
            session.close()

        # Prime the cache.
        client.get(f"/discs/{disc_id}/workflow-context")
        assert f"disc:{disc_id}" in discs_router._workflow_context_cache

        client.post(f"/discs/{disc_id}/titles/{title_ids[1]}/ungroup-duplicate")

        assert f"disc:{disc_id}" not in discs_router._workflow_context_cache, (
            "a stale context would hide the ungroup from the left rail"
        )

    def test_refetch_right_after_ungroup_reflects_it(self, client, test_db):
        """The behaviour the client actually depends on — no TTL wait."""
        session = test_db()
        try:
            disc_id, title_ids = _disc_with_group(session)
        finally:
            session.close()

        before = client.get(f"/discs/{disc_id}/workflow-context").json()
        assert before["dedupeGroups"], "expected a group to start from"

        client.post(f"/discs/{disc_id}/titles/{title_ids[1]}/ungroup-duplicate")
        after = client.get(f"/discs/{disc_id}/workflow-context").json()

        flagged = [t["source_file"] for t in after["titles"] if t.get("force_independent_group")]
        assert flagged == ["title-1"]
        members = {
            m
            for g in after["dedupeGroups"]
            for m in [g["representative_title_id"], *g["sibling_title_ids"]]
        }
        assert title_ids[1] not in members, "ungrouped title still served from cache"

    def test_set_primary_evicts_the_cached_context(self, client, test_db):
        session = test_db()
        try:
            disc_id, title_ids = _disc_with_group(session)
        finally:
            session.close()

        client.get(f"/discs/{disc_id}/workflow-context")
        assert f"disc:{disc_id}" in discs_router._workflow_context_cache

        client.post(f"/discs/{disc_id}/titles/{title_ids[2]}/set-primary")

        assert f"disc:{disc_id}" not in discs_router._workflow_context_cache
