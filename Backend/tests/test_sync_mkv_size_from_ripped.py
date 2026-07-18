"""Tests for _sync_disc_title_mkv_sizes_from_ripped."""

from pathlib import Path
from unittest.mock import Mock

import uuid

from workers.tasks import _sync_disc_title_mkv_sizes_from_ripped


def test_sync_mkv_size_uses_pk_for_uuid_keys(tmp_path: Path):
    tid = str(uuid.uuid4())
    mkv = tmp_path / "x.mkv"
    mkv.write_bytes(b"12345")

    tr = Mock()
    tr.mkv_size = None
    mock_q = Mock()
    mock_q.filter.return_value.first.return_value = tr
    db = Mock()
    db.query.return_value = mock_q

    _sync_disc_title_mkv_sizes_from_ripped(db, tmp_path, {tid: "x.mkv"}, disc_id="disc-1")

    assert tr.mkv_size == 5
    db.flush.assert_called()
