from types import SimpleNamespace

from api.routers.jobs import _derive_pipeline


# #365 step 5 — the post_state column was dropped; readers go through
# Job.derived_post_state. SimpleNamespace doesn't evaluate hybrid
# properties, so each fixture below sets derived_post_state explicitly
# to mirror what the derivation would compute from the other fields
# (rip_state, transfer_phase, transfer_state, label_state, job_status).


def test_pipeline_postprocess_when_rip_near_complete():
    job = SimpleNamespace(
        job_status="running",
        rip_progress=85,
        stage_profile="hit",
        rip_state="completed",
        transfer_state=None,
        derived_post_state="running",  # transfer_phase="preparing" equivalent
    )
    pipeline, phase = _derive_pipeline(job)
    assert pipeline["rip"] == "completed"
    assert pipeline["postprocess"] == "running"
    assert phase == "postprocess"


def test_pipeline_with_transfer_completion():
    job = SimpleNamespace(
        job_status="completed",
        rip_progress=100,
        stage_profile="hit",
        rip_state="completed",
        transfer_state="completed",
        derived_post_state="completed",
    )
    pipeline, phase = _derive_pipeline(job)
    assert pipeline["rip"] == "completed"
    assert pipeline["postprocess"] == "completed"
    assert pipeline["transfer"] == "completed"
    assert phase == "complete"


def test_pipeline_validating_maps_to_postprocess():
    job = SimpleNamespace(
        job_status="validating",
        rip_progress=100,
        stage_profile="hit",
        rip_state="completed",
        transfer_state=None,
        derived_post_state="running",
    )
    pipeline, phase = _derive_pipeline(job)
    assert pipeline["rip"] == "completed"
    assert pipeline["postprocess"] == "running"
    assert pipeline["transfer"] == "pending"
    assert phase == "postprocess"
