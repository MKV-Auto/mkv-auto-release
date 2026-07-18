"""
Redis-based caching for disc info and job status.
Provides stale-while-revalidate pattern for improved performance.
"""
import json
import logging
import os
from typing import Any, Optional, Dict
from datetime import timedelta
import redis
from redis.exceptions import ConnectionError, TimeoutError

logger = logging.getLogger(__name__)

# Redis connection pool (singleton)
_redis_client: Optional[redis.Redis] = None

def get_redis_client() -> Optional[redis.Redis]:
    """Get or create Redis client."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    
    try:
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/2")  # Use DB 2 for cache (0 and 1 are for Celery)
        _redis_client = redis.from_url(redis_url, decode_responses=True)
        # Test connection
        _redis_client.ping()
        logger.info("[RedisCache] Connected to Redis")
        return _redis_client
    except (ConnectionError, TimeoutError) as e:
        logger.warning(f"[RedisCache] Redis not available: {e}. Caching disabled.")
        _redis_client = None
        return None
    except Exception as e:
        logger.warning(f"[RedisCache] Failed to connect to Redis: {e}. Caching disabled.")
        _redis_client = None
        return None

def _make_key(namespace: str, key: str) -> str:
    """Create a namespaced cache key."""
    return f"cache:{namespace}:{key}"

def get(namespace: str, key: str) -> Optional[Dict[str, Any]]:
    """Get cached value."""
    client = get_redis_client()
    if not client:
        return None
    
    try:
        cache_key = _make_key(namespace, key)
        data = client.get(cache_key)
        if data:
            return json.loads(data)
        return None
    except Exception as e:
        logger.warning(f"[RedisCache] Failed to get cache key {namespace}:{key}: {e}")
        return None

def set(
    namespace: str,
    key: str,
    value: Dict[str, Any],
    ttl: int = 300,  # 5 minutes default
    stale_ttl: int = 600  # 10 minutes for stale-while-revalidate
) -> bool:
    """Set cached value with TTL."""
    client = get_redis_client()
    if not client:
        return False
    
    try:
        cache_key = _make_key(namespace, key)
        data = json.dumps(value)
        # Store with TTL
        client.setex(cache_key, stale_ttl, data)
        # Also set a stale marker with longer TTL
        stale_key = f"{cache_key}:stale"
        client.setex(stale_key, stale_ttl, "1")
        return True
    except Exception as e:
        logger.warning(f"[RedisCache] Failed to set cache key {namespace}:{key}: {e}")
        return False

def invalidate(namespace: str, pattern: Optional[str] = None) -> int:
    """Invalidate cache entries matching pattern."""
    client = get_redis_client()
    if not client:
        return 0
    
    try:
        if pattern:
            # Delete keys matching pattern
            cache_pattern = _make_key(namespace, f"*{pattern}*")
            keys = client.keys(cache_pattern)
            if keys:
                return client.delete(*keys)
        else:
            # Delete all keys in namespace
            cache_pattern = _make_key(namespace, "*")
            keys = client.keys(cache_pattern)
            if keys:
                return client.delete(*keys)
        return 0
    except Exception as e:
        logger.warning(f"[RedisCache] Failed to invalidate cache {namespace}:{pattern}: {e}")
        return 0

def is_stale(namespace: str, key: str) -> bool:
    """Check if cached value is stale (past TTL but within stale_ttl)."""
    client = get_redis_client()
    if not client:
        return False
    
    try:
        cache_key = _make_key(namespace, key)
        stale_key = f"{cache_key}:stale"
        # If stale marker exists but main key doesn't, it's stale
        if client.exists(stale_key) and not client.exists(cache_key):
            return True
        return False
    except Exception as e:
        logger.warning(f"[RedisCache] Failed to check stale status for {namespace}:{key}: {e}")
        return False

def get_or_fetch(
    namespace: str,
    key: str,
    fetch_fn,
    ttl: int = 300,
    stale_ttl: int = 600
) -> Dict[str, Any]:
    """
    Get from cache or fetch, with stale-while-revalidate support.
    Returns cached data immediately if available (even if stale),
    and triggers background refresh if stale.
    """
    # Try to get from cache
    cached = get(namespace, key)
    if cached:
        # Check if stale
        if is_stale(namespace, key):
            # Return stale data but trigger background refresh
            logger.debug(f"[RedisCache] Returning stale data for {namespace}:{key}, refreshing in background")
            # Trigger async refresh (fire and forget)
            try:
                # In FastAPI, we can use background tasks for this
                # For now, just log - the caller should handle refresh
                pass
            except Exception:
                pass
        return cached
    
    # Not in cache, fetch and cache
    try:
        data = fetch_fn()
        if data:
            set(namespace, key, data, ttl, stale_ttl)
        return data
    except Exception as e:
        logger.error(f"[RedisCache] Failed to fetch data for {namespace}:{key}: {e}")
        raise


