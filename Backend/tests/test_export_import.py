"""
Unit tests for api.export_import._model_to_dict.
"""
from datetime import datetime, timezone

import pytest

from api.export_import import _model_to_dict
from api.models import Movie


def test_model_to_dict_returns_column_values():
    """Normal case: object with id, name, and datetime. Datetime becomes ISO string."""
    created = datetime(2020, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    updated = datetime(2020, 5, 2, 12, 0, 0, tzinfo=timezone.utc)
    obj = Movie(
        id="m1",
        name="Test Movie",
        production_year=2020,
        created_at=created,
        updated_at=updated,
    )
    d = _model_to_dict(obj)
    assert d["id"] == "m1"
    assert d["name"] == "Test Movie"
    assert d["production_year"] == 2020
    assert d["created_at"] == created.isoformat()
    assert d["updated_at"] == updated.isoformat()
    assert "releases" not in d


def test_model_to_dict_handles_none_and_nullable():
    """Nullable columns can be None; value is left as None (no isoformat)."""
    created = datetime(2020, 1, 1, tzinfo=timezone.utc)
    obj = Movie(
        id="m2",
        name="Other",
        production_year=None,
        tmdb_id=None,
        created_at=created,
        updated_at=created,
    )
    d = _model_to_dict(obj)
    assert d["production_year"] is None
    assert d["tmdb_id"] is None
    assert d["created_at"] == created.isoformat()
