"""Miss fixture: synthetic disc with discdb_disabled=True.

Forces the MISS workflow path (rip → label → postprocess) via the existing
devmode toggle. This is deliberate even though the synthetic ``content_hash``
("e2efixture_miss_0000000001") would naturally miss against TheDiscDB today —
once we start contributing back via #86, any deterministic test hash could
end up in the database. Forcing the miss via ``discdb_disabled`` keeps the
test reliable regardless of TheDiscDB state.

Matches the pre-fixture-catalog default ``discinfo_payload`` shape closely
so existing rip-happy.spec.ts continues to pass under this fixture.
"""
from __future__ import annotations

from ._base import Fixture, make_simple_payload


FIXTURE = Fixture(
    name="miss",
    discinfo_payload=make_simple_payload(
        content_hash="e2efixture_miss_0000000001",
        info_title="E2E Miss Fixture Movie",
        disc_format="Blu-Ray",
        resolution="1080p",
        title_type="movie",
        # MockDrive returns this payload verbatim — the disc identification
        # path in crud.create_job_for_disc reads discdb_result + label_required
        # directly from it. Set both explicitly so the job ends up with
        # stage_profile='miss' and label_state='pending'.
        discdb_result="miss",
        label_required=True,
    ),
    mockmkv_titles=[{"file": "00001.mpls"}],
    discdb_disabled=True,
    expected_workflow="miss",
    notes=(
        "Forces MISS via two complementary mechanisms: (1) the synthetic "
        "discinfo_payload includes discdb_result='miss' + label_required=True "
        "so crud.create_job_for_disc sets stage_profile='miss' deterministically; "
        "(2) settings.discdb_disabled=True belt-and-suspenders for any code path "
        "that calls disc_manager.process_disc directly with devmode on."
    ),
)
