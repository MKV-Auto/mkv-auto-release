"""Record-first label storage with finalized flags and disc tracks

Revision ID: 9d6f3d7c4b21
Revises: 2b4c5d6e7f80
Create Date: 2025-03-20 00:00:00.000000
"""
from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Any, Dict, List

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = "9d6f3d7c4b21"
down_revision: str | None = "2b4c5d6e7f80"
branch_labels = None
depends_on = None


def _uuid_str() -> str:
    return str(uuid.uuid4())


def _first(*vals):
    for v in vals:
        if v is None:
            continue
        if isinstance(v, str) and v.strip() == "":
            continue
        return v
    return None


def upgrade() -> None:
    op.add_column("releases", sa.Column("finalized", sa.Boolean(), nullable=False, server_default=text("false")))
    op.add_column("releases", sa.Column("finalized_at", sa.TIMESTAMP(timezone=True), nullable=True))

    op.add_column("discs", sa.Column("finalized", sa.Boolean(), nullable=False, server_default=text("false")))
    op.add_column("discs", sa.Column("finalized_at", sa.TIMESTAMP(timezone=True), nullable=True))

    op.create_table(
        "disc_tracks",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("disc_id", sa.String(), sa.ForeignKey("discs.id"), nullable=False),
        sa.Column("track_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("season", sa.Integer(), nullable=True),
        sa.Column("episode", sa.Integer(), nullable=True),
        sa.Column("type", sa.String(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("duration", sa.Float(), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("streams", sa.JSON(), nullable=True),
        sa.Column("content", sa.Boolean(), nullable=False, server_default=text("true")),
        sa.Column("order_index", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"), onupdate=text("now()")),
        sa.UniqueConstraint("disc_id", "track_id", name="uq_disc_tracks_disc_trackid"),
    )

    _backfill_label_payloads()


def downgrade() -> None:
    op.drop_table("disc_tracks")
    op.drop_column("discs", "finalized_at")
    op.drop_column("discs", "finalized")
    op.drop_column("releases", "finalized_at")
    op.drop_column("releases", "finalized")


def _backfill_label_payloads() -> None:
    conn = op.get_bind()
    metadata = sa.MetaData()
    discs = sa.Table("discs", metadata, autoload_with=conn)
    releases = sa.Table("releases", metadata, autoload_with=conn)
    disc_tracks = sa.Table("disc_tracks", metadata, autoload_with=conn)

    rows = conn.execute(sa.select(discs)).fetchall()
    now = datetime.now(timezone.utc)

    for row in rows:
        label_payload: Dict[str, Any] = {}
        for candidate in (row.label_payload, row.label_draft):
            if isinstance(candidate, dict):
                label_payload = candidate
                break

        # Update release fields when present.
        if row.release_id:
            rel = conn.execute(
                sa.select(releases).where(releases.c.id == row.release_id)
            ).fetchone()
            if rel:
                rel_updates: Dict[str, Any] = {}
                name_val = _first(label_payload.get("release_name"), label_payload.get("release_title"))
                if name_val and not rel.name:
                    rel_updates["name"] = name_val
                    rel_updates["title"] = name_val
                tmdb_id = _first(label_payload.get("tmdb_id"))
                if tmdb_id and not rel.tmdb_id:
                    rel_updates["tmdb_id"] = tmdb_id
                upc = _first(label_payload.get("upc"))
                if upc and not rel.upc:
                    rel_updates["upc"] = upc
                asin = _first(label_payload.get("asin"))
                if asin and not rel.asin:
                    rel_updates["asin"] = asin
                cover_front = _first(label_payload.get("cover_front_url"))
                if cover_front and not rel.cover_front_url:
                    rel_updates["cover_front_url"] = cover_front
                cover_back = _first(label_payload.get("cover_back_url"))
                if cover_back and not rel.cover_back_url:
                    rel_updates["cover_back_url"] = cover_back
                rel_type = _first(label_payload.get("group_type"), label_payload.get("mode"))
                if rel_type and not rel.type:
                    rel_updates["type"] = rel_type

                finalized = bool(rel.finalize_state)
                if row.finalize_result:
                    finalized = True
                if finalized:
                    rel_updates.setdefault("finalized", True)
                    if not rel.finalized_at:
                        rel_updates["finalized_at"] = now

                if rel_updates:
                    conn.execute(
                        releases.update()
                        .where(releases.c.id == rel.id)
                        .values(**rel_updates)
                    )

        # Update disc fields and tracks.
        disc_updates: Dict[str, Any] = {}
        disc_name = _first(label_payload.get("disc_name"))
        if disc_name and not row.disc_name:
            disc_updates["disc_name"] = disc_name
        disc_slug = _first(label_payload.get("disc_slug"))
        if disc_slug and not row.disc_slug:
            disc_updates["disc_slug"] = disc_slug
        disc_number = label_payload.get("disc_number")
        if disc_number is not None and row.disc_number is None:
            try:
                disc_updates["disc_number"] = int(disc_number)
            except Exception:
                pass
        disc_format = _first(label_payload.get("disc_format"), label_payload.get("format"))
        if disc_format and not row.format:
            disc_updates["format"] = disc_format

        if row.finalize_result:
            disc_updates["finalized"] = True
            if not row.finalized_at:
                disc_updates["finalized_at"] = now

        if disc_updates:
            conn.execute(
                discs.update()
                .where(discs.c.id == row.id)
                .values(**disc_updates)
            )

        tracks = label_payload.get("tracks") or []
        if isinstance(tracks, list) and tracks:
            inserts: List[Dict[str, Any]] = []
            for idx, t in enumerate(tracks):
                if not isinstance(t, dict):
                    continue
                track_id = _first(t.get("track_id"), t.get("source_file"), t.get("output_file"))
                if track_id is None:
                    track_id = str(idx)
                inserts.append(
                    {
                        "id": _uuid_str(),
                        "disc_id": row.id,
                        "track_id": str(track_id),
                        "title": _first(t.get("title"), t.get("episode_name")),
                        "season": t.get("season"),
                        "episode": t.get("episode"),
                        "type": t.get("type"),
                        "note": t.get("note"),
                        "duration": _first(t.get("duration")),
                        "size": _first(t.get("size")),
                        "streams": t.get("streams"),
                        "content": True if t.get("content") is None else bool(t.get("content")),
                        "order_index": idx,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
            if inserts:
                conn.execute(sa.insert(disc_tracks), inserts)
