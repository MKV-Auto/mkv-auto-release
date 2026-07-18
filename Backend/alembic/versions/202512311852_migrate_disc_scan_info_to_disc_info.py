"""migrate_disc_scan_info_to_disc_info

Revision ID: 202512311852
Revises: 202512311851
Create Date: 2025-12-31 18:52:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import json

from core.logging_utils import get_logger

logger = get_logger(__name__)

# revision identifiers, used by Alembic.
revision: str = "202512311852"
down_revision: str | None = "202512311851"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Migrate disc scan info from job.disc_payload to disc.disc_info.
    
    Extracts the following fields from job.disc_payload and stores them in disc.disc_info:
    - raw_info_log
    - info_log / makemkv_info_log
    - titles_map
    - scan_tracks
    - titles
    - cinfo_lines
    - resolution
    """
    connection = op.get_bind()
    
    # Get all jobs with disc_payload that have disc_id
    jobs_query = """
        SELECT id, disc_id, disc_payload
        FROM jobs
        WHERE disc_id IS NOT NULL 
          AND disc_payload IS NOT NULL
          AND disc_payload::text != 'null'
    """
    
    jobs = connection.execute(sa.text(jobs_query)).fetchall()
    
    # Fields to extract from disc_payload
    disc_scan_fields = [
        'raw_info_log',
        'info_log',
        'makemkv_info_log',
        'titles_map',
        'scan_tracks',
        'titles',
        'cinfo_lines',
        'resolution'
    ]
    
    # Process each job
    for job_id, disc_id, disc_payload in jobs:
        if not disc_payload:
            continue
            
        try:
            # Parse disc_payload if it's a string
            if isinstance(disc_payload, str):
                payload = json.loads(disc_payload)
            else:
                payload = disc_payload
            
            # Extract disc scan info fields
            disc_info_data = {}
            updated_payload = dict(payload) if isinstance(payload, dict) else {}
            has_disc_info = False
            
            for field in disc_scan_fields:
                if field in payload:
                    disc_info_data[field] = payload[field]
                    # Remove from payload (will update job.disc_payload later)
                    updated_payload.pop(field, None)
                    has_disc_info = True
            
            # Only proceed if we found disc scan info to migrate
            if not has_disc_info:
                continue
            
            # Get current disc_info or initialize as empty dict
            disc_info_query = """
                SELECT disc_info
                FROM discs
                WHERE id = :disc_id
            """
            disc_result = connection.execute(
                sa.text(disc_info_query),
                {"disc_id": disc_id}
            ).fetchone()
            
            current_disc_info = {}
            if disc_result and disc_result[0]:
                current_disc_info = disc_result[0] if isinstance(disc_result[0], dict) else json.loads(disc_result[0])
            
            # Merge disc_info_data into current_disc_info (disc_info_data takes precedence)
            merged_disc_info = {**current_disc_info, **disc_info_data}
            
            # Update disc.disc_info
            update_disc_query = """
                UPDATE discs
                SET disc_info = CAST(:disc_info AS json)
                WHERE id = :disc_id
            """
            connection.execute(
                sa.text(update_disc_query),
                {
                    "disc_id": disc_id,
                    "disc_info": json.dumps(merged_disc_info)
                }
            )
            
            # Update job.disc_payload to remove migrated fields
            update_job_query = """
                UPDATE jobs
                SET disc_payload = CAST(:disc_payload AS json)
                WHERE id = :job_id
            """
            connection.execute(
                sa.text(update_job_query),
                {
                    "job_id": job_id,
                    "disc_payload": json.dumps(updated_payload)
                }
            )
            
        except Exception as e:
            # Log error but continue with other jobs
            logger.warning("Error migrating job %s: %s", job_id, e)
            continue
    
    connection.commit()


def downgrade() -> None:
    """
    Move disc scan info back from disc.disc_info to job.disc_payload.
    Note: This is a best-effort revert. We merge disc_info back into disc_payload.
    """
    connection = op.get_bind()
    
    # Get all discs with disc_info
    discs_query = """
        SELECT id, disc_info
        FROM discs
        WHERE disc_info IS NOT NULL
          AND disc_info::text != 'null'
    """
    
    discs = connection.execute(sa.text(discs_query)).fetchall()
    
    # For each disc, find jobs and merge disc_info back into disc_payload
    for disc_id, disc_info in discs:
        if not disc_info:
            continue
        
        try:
            # Parse disc_info if it's a string
            if isinstance(disc_info, str):
                disc_info_data = json.loads(disc_info)
            else:
                disc_info_data = disc_info
            
            # Get all jobs for this disc
            jobs_query = """
                SELECT id, disc_payload
                FROM jobs
                WHERE disc_id = :disc_id
                  AND disc_payload IS NOT NULL
            """
            
            jobs = connection.execute(
                sa.text(jobs_query),
                {"disc_id": disc_id}
            ).fetchall()
            
            for job_id, disc_payload in jobs:
                # Parse current disc_payload
                if isinstance(disc_payload, str):
                    payload = json.loads(disc_payload)
                else:
                    payload = disc_payload if disc_payload else {}
                
                # Merge disc_info_data into payload (disc_info_data takes precedence)
                merged_payload = {**disc_info_data, **payload}
                
                # Update job.disc_payload
                update_job_query = """
                    UPDATE jobs
                    SET disc_payload = CAST(:disc_payload AS json)
                    WHERE id = :job_id
                """
                connection.execute(
                    sa.text(update_job_query),
                    {
                        "job_id": job_id,
                        "disc_payload": json.dumps(merged_payload)
                    }
                )
            
        except Exception as e:
            # Log error but continue with other discs
            logger.warning("Error reverting disc %s: %s", disc_id, e)
            continue
    
    connection.commit()

