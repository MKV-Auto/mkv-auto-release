"""
Debounced progress emitter for job progress updates via websocket.

Prevents overwhelming websocket connections with rapid progress updates.
"""
import asyncio
import logging
import time
import json
from typing import Dict, Optional, Any
from collections import defaultdict

logger = logging.getLogger("core.progress_emitter")

# Debounce settings
PROGRESS_DEBOUNCE_SECONDS = 1.0  # Max 1 update per second per job

# Track last emission time per job
_last_emission: Dict[str, float] = {}
# Track pending progress data per job
_pending_progress: Dict[str, Dict[str, Any]] = {}
# Global event loop reference (set by FastAPI app at startup)
_global_event_loop: Optional[asyncio.AbstractEventLoop] = None


async def _emit_progress_async(job_id: str, progress_data: Dict[str, Any]) -> None:
    """
    Emit progress update to websocket (async).
    
    Args:
        job_id: Job ID
        progress_data: Progress data dict
    """
    try:
        # Try to import _emit_job_progress - this may fail in Celery worker context
        try:
            from api.routers.websockets import _emit_job_progress
        except ImportError as import_err:
            # If import fails, try to get it from sys.modules
            import sys
            if 'api.routers.websockets' in sys.modules:
                _emit_job_progress = sys.modules['api.routers.websockets']._emit_job_progress
            else:
                raise import_err
        
        await _emit_job_progress(job_id, progress_data)
    except ImportError as exc:
        # Websocket module not available (e.g., during tests or Celery worker)
        logger.warning(f"Cannot import websocket module to emit progress for job {job_id}: {exc}")
    except Exception as exc:
        logger.warning(f"Failed to emit progress update for job {job_id}: {exc}")


def emit_job_progress_debounced(job_id: str, progress_data: Dict[str, Any]) -> None:
    """
    Emit job progress update with debouncing.
    
    This function can be called from sync context (worker threads).
    Uses asyncio.run_coroutine_threadsafe to bridge to async context.
    
    Args:
        job_id: Job ID
        progress_data: Progress data dict (rip_progress, post_progress, per_title_progress, etc.)
    """
    try:
        current_time = time.time()
        
        # Check if we should emit (debounce)
        should_emit = False
        last_emit_time = _last_emission.get(job_id, 0)
        time_since_last = current_time - last_emit_time
        
        if time_since_last >= PROGRESS_DEBOUNCE_SECONDS:
            should_emit = True
            _last_emission[job_id] = current_time
        else:
            # Store pending progress (merge with existing)
            if job_id not in _pending_progress:
                _pending_progress[job_id] = {}
            _pending_progress[job_id].update(progress_data)
        
        if should_emit:
            # Merge with any pending progress
            final_data = _pending_progress.pop(job_id, {})
            final_data.update(progress_data)
            
            # Schedule async emission
            # Try multiple approaches to get the event loop
            loop = None
            try:
                # First, try to get the running loop (if we're in an async context)
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop - try global reference first (fastest)
                if _global_event_loop is not None:
                    loop = _global_event_loop
                else:
                    # Try to get from app instance
                    try:
                        # Use a more defensive import that won't fail if api.main has import issues
                        import sys
                        if 'api.main' in sys.modules:
                            _app_instance = sys.modules['api.main']._app_instance
                            if _app_instance and hasattr(_app_instance, "state") and hasattr(_app_instance.state, "event_loop"):
                                loop = _app_instance.state.event_loop
                    except (AttributeError, KeyError, ImportError) as e:
                        logger.warning(f"No event loop available to emit progress for job {job_id}: {e}")
            
            if loop:
                try:
                    if loop.is_running():
                        # Loop is running - use run_coroutine_threadsafe
                        asyncio.run_coroutine_threadsafe(_emit_progress_async(job_id, final_data), loop)
                    else:
                        # Loop not running - use create_task (but this might fail)
                        asyncio.create_task(_emit_progress_async(job_id, final_data))
                except Exception as exc:
                    logger.warning(f"Failed to schedule progress emission for job {job_id}: {exc}")
            else:
                # No event loop available - use Redis pub/sub as fallback
                # This allows Celery workers to send progress updates to the FastAPI app
                try:
                    import redis
                    redis_client = redis.Redis(host='localhost', port=6379, db=2, decode_responses=True)
                    channel = f"progress_updates:{job_id}"
                    message = json.dumps({
                        "job_id": job_id,
                        **final_data
                    })
                    subscribers = redis_client.publish(channel, message)
                    logger.debug(f"Published progress update to Redis channel {channel} for job {job_id} (subscribers: {subscribers})")
                except Exception as redis_exc:
                    logger.warning(f"No event loop available and Redis publish failed for job {job_id}: {redis_exc}")
    except Exception as exc:
        logger.warning(f"Error in emit_job_progress_debounced for job {job_id}: {exc}", exc_info=True)




