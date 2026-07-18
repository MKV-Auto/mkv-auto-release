"""Regression: release summaries include link-readiness (labeling UI needs-info / complete form)."""
import uuid

from api import models
from api.routers.releases import _release_summary


def test_release_summary_not_link_ready_when_upc_is_all_zeros(test_db):
    """Invalid GTIN (all zeros) must surface as not link-ready with upc in missing fields."""
    session = test_db()
    try:
        mid = str(uuid.uuid4())
        session.add(
            models.Movie(
                id=mid,
                name="Dune",
                production_year=2022,
                tmdb_id=f"tmdb-{uuid.uuid4().hex[:8]}",
            )
        )
        rid = str(uuid.uuid4())
        session.add(
            models.Release(
                id=rid,
                slug="dune-2022",
                type="movie",
                name="Dune",
                movie_id=mid,
                upc="000000000000",
                cover_front_url="https://example.com/cover.jpg",
                release_year=2022,
            )
        )
        session.commit()
        rel = session.query(models.Release).filter(models.Release.id == rid).first()
        summ = _release_summary(rel, session)
        assert summ.release_link_ready is False
        assert summ.release_missing_required_fields
        assert "upc" in summ.release_missing_required_fields
    finally:
        session.close()
