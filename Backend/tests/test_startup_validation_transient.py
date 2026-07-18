from api import main as api_main
from api import models
from tests.postprocess_fixtures import job_with_rip_done_for_postprocess


def test_startup_validation_uses_post_paths(test_db, tmp_path, monkeypatch):
    job_id, title_id, paths = job_with_rip_done_for_postprocess(
        test_db,
        tmp_path,
        monkeypatch,
        num_titles=1,
    )

    raw_file = paths.raw / "test_t1.mkv"
    raw_file.unlink()

    with test_db() as session:
        job = session.query(models.Job).filter_by(id=job_id).one()
        job.rip_progress = 100
        job.job_status = "running"
        post_paths = dict(job.post_paths or {})
        session.commit()

    transient_file = paths.transient / post_paths[title_id]
    transient_file.parent.mkdir(parents=True, exist_ok=True)
    transient_file.write_bytes(b"x")

    class InlineThread:
        def __init__(self, *, target, args=(), daemon=None):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(api_main.threading, "Thread", InlineThread)

    api_main._recover_inflight_jobs()

    with test_db() as session:
        job = session.query(models.Job).filter_by(id=job_id).one()
        assert job.job_status == "running"
        assert job.error_reason is None
        assert job.post_paths == post_paths
        assert job.ripped_files == {title_id: "test_t1.mkv"}
