"""Every title in a workflow context must carry ``disc_id``.

The client addresses per-title endpoints with it —
``POST /discs/{disc_id}/titles/{title_id}/ungroup-duplicate`` and set-primary.
``get_job_workflow_context`` built its title dicts with 30+ fields but not this
one, so those buttons silently did nothing: the handler could not form a URL,
returned early, and issued no request at all (mkv-auto-release#8).

``schemas.DiscTitleRecord`` already declares ``disc_id`` as required, so the
contract was never in doubt — only this builder disagreed with it.
"""
import uuid

import pytest

from api import models
from api.routers.jobs import get_job_workflow_context


@pytest.fixture
def session(test_db):
    s = test_db()
    try:
        yield s
    finally:
        s.close()


def _disc_with_titles(session, n=3, *, group=False):
    disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:10]}")
    session.add(disc)
    session.flush()
    for i in range(n):
        session.add(
            models.DiscTitle(
                id=str(uuid.uuid4()),
                disc_id=disc.id,
                source_file=f"0000{i}.mpls",
                # A shared segment_map is what puts rows in one duplicate group,
                # i.e. exactly the state where Ungroup is offered.
                segment_map="1-5" if group else str(100 + i),
                index=i,
                order_index=i,
                type="Episode",
                title=f"Episode {i + 1}",
                season=1,
                episode=i + 1,
            )
        )
    job = models.Job(
        id=str(uuid.uuid4()),
        disc_id=disc.id,
        disc_num="1",
        mount_point="/dev/sr0",
        mode="copy",
        job_status="running",
        rip_state="completed",
    )
    session.add(job)
    session.commit()
    return disc, job


class TestTitlesCarryDiscId:
    def test_every_title_has_the_disc_id(self, session):
        disc, job = _disc_with_titles(session)
        ctx = get_job_workflow_context(str(job.id), session)
        titles = ctx.titles or []

        assert len(titles) == 3
        for t in titles:
            assert t.get("disc_id") == str(disc.id), (
                "a title without disc_id makes ungroup/set-primary unaddressable"
            )

    def test_holds_for_a_duplicate_group(self, session):
        # The reported case: rows sharing a segment_map, where Ungroup is shown.
        disc, job = _disc_with_titles(session, n=3, group=True)
        ctx = get_job_workflow_context(str(job.id), session)
        titles = ctx.titles or []

        assert titles, "expected titles for a grouped disc"
        assert all(t.get("disc_id") == str(disc.id) for t in titles)

    def test_title_id_and_disc_id_together_address_the_endpoint(self, session):
        # Both halves of POST /discs/{disc_id}/titles/{title_id}/ungroup-duplicate.
        disc, job = _disc_with_titles(session, n=1)
        ctx = get_job_workflow_context(str(job.id), session)
        t = (ctx.titles or [])[0]

        assert t.get("disc_id"), "disc_id is the half that was missing"
        assert t.get("title_id"), "title_id was always present"

    def test_payload_sourced_titles_also_carry_it(self, session):
        """The fallback branch: titles from disc_payload, not DB rows.

        Those dicts may predate the field entirely, so the builder fills it
        from the job's own disc.
        """
        disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:10]}")
        session.add(disc)
        session.flush()
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            mode="copy",
            job_status="running",
            # No DiscTitle rows — forces the disc_payload branch.
            disc_payload={"titles": {"00001.mpls": {"title_id": "t-legacy-1", "source_file": "00001.mpls"}}},
        )
        session.add(job)
        session.commit()

        ctx = get_job_workflow_context(str(job.id), session)
        titles = ctx.titles or []
        assert titles, "payload titles should still be returned"
        assert titles[0].get("disc_id") == str(disc.id)
