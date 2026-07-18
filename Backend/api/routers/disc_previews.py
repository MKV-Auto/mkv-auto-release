"""
Disc-scoped preview routes (#355).

Serve HLS preview artifacts from the durable disc_previews/ tree,
parallel to the job-scoped preview routes in jobs.py.
"""
import logging
import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from core.preview_paths import get_disc_preview_root

router = APIRouter(prefix="/disc-previews", tags=["disc-previews"])
log = logging.getLogger("api.routers.disc_previews")


@router.get("/{disc_id}/{title_id}/{rel_path:path}")
def stream_disc_preview(disc_id: str, title_id: str, rel_path: str):
    """
    Serve a file from the disc-scoped preview tree.
    Supports HLS manifests (.m3u8), segments (.ts), and thumbnails (.jpg/.png).
    """
    root = get_disc_preview_root()
    # Sanitize: no parent traversal
    safe = Path(disc_id) / title_id / rel_path
    if ".." in safe.parts:
        raise HTTPException(400, detail="Invalid path")
    full = root / safe
    if not full.is_file():
        raise HTTPException(404, detail="Preview file not found")
    media_type, _ = mimetypes.guess_type(str(full))
    if not media_type:
        ext = full.suffix.lower()
        media_type = {
            ".m3u8": "application/vnd.apple.mpegurl",
            ".ts": "video/mp2t",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
        }.get(ext, "application/octet-stream")
    return FileResponse(str(full), media_type=media_type)
