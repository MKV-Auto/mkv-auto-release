"""
WebSocket connection manager for workflow contexts.

Manages active websocket connections for:
- Master coordinator (all discs and jobs)
- Per-disc workflow contexts
- Per-job workflow contexts

Implements connection limits, thread-safe operations, and error handling.
"""
import asyncio
import logging
import json
from typing import Dict, Set, Optional, Any
from collections import defaultdict
from fastapi import WebSocket, WebSocketDisconnect

from core.loop_local import LoopLocalLock

logger = logging.getLogger("core.websocket_manager")

# Connection limits
MAX_CONNECTIONS_PER_WORKFLOW = 10
MAX_TOTAL_CONNECTIONS = 100


class WebSocketManager:
    """
    Thread-safe manager for websocket connections.
    
    Tracks connections by:
    - 'master' - Master coordinator websocket
    - 'disc:{disc_id}' - Per-disc workflow websockets
    - 'job:{job_id}' - Per-job workflow websockets
    """
    
    def __init__(self):
        # Connection registries: key -> Set[WebSocket]
        self._connections: Dict[str, Set[WebSocket]] = defaultdict(set)
        # Lock for thread-safe operations. Loop-local because the manager is a
        # process-wide singleton (see get_websocket_manager) and a bare
        # asyncio.Lock would stick to the first loop that contends on it.
        self._lock = LoopLocalLock()
        # Track total connections
        self._total_connections = 0
    
    async def connect(self, key: str, websocket: WebSocket) -> bool:
        """
        Register a new websocket connection.
        
        Args:
            key: Connection key ('master', 'disc:{disc_id}', or 'job:{job_id}')
            websocket: WebSocket connection
            
        Returns:
            True if connection was added, False if limit exceeded
        """
        async with self._lock:
            # Check total connection limit
            if self._total_connections >= MAX_TOTAL_CONNECTIONS:
                logger.warning(f"Total connection limit ({MAX_TOTAL_CONNECTIONS}) exceeded, rejecting connection for {key}")
                return False
            
            # Check per-workflow connection limit (not for master)
            if key != 'master' and len(self._connections[key]) >= MAX_CONNECTIONS_PER_WORKFLOW:
                logger.warning(f"Per-workflow connection limit ({MAX_CONNECTIONS_PER_WORKFLOW}) exceeded for {key}")
                return False
            
            self._connections[key].add(websocket)
            self._total_connections += 1
            logger.info(f"WebSocket connected: {key} (total: {self._total_connections})")
            return True
    
    async def disconnect(self, key: str, websocket: WebSocket) -> None:
        """
        Unregister a websocket connection.
        
        Args:
            key: Connection key
            websocket: WebSocket connection to remove
        """
        async with self._lock:
            if websocket in self._connections[key]:
                self._connections[key].remove(websocket)
                self._total_connections -= 1
                logger.info(f"WebSocket disconnected: {key} (total: {self._total_connections})")
                
                # Clean up empty sets
                if not self._connections[key]:
                    del self._connections[key]
    
    async def broadcast(self, key: str, message: Dict[str, Any]) -> int:
        """
        Broadcast a message to all connections for a given key.
        
        Args:
            key: Connection key
            message: Message dict to send (will be JSON-encoded)
            
        Returns:
            Number of connections that received the message
        """
        async with self._lock:
            connections = self._connections[key].copy()
        
        if not connections:
            return 0
        
        # Convert Pydantic models to dicts if needed (defensive programming)
        if hasattr(message, 'model_dump'):
            # Pydantic v2
            message = message.model_dump()
        elif hasattr(message, 'dict'):
            # Pydantic v1
            message = message.dict()
        elif not isinstance(message, dict):
            logger.error(f"Invalid message type for broadcast to {key}: {type(message)}. Expected dict or Pydantic model.")
            return 0
        
        # Send to all connections (outside lock to avoid blocking)
        sent_count = 0
        disconnected = []
        
        for ws in connections:
            try:
                await ws.send_json(message)
                sent_count += 1
            except WebSocketDisconnect:
                disconnected.append(ws)
            except RuntimeError as exc:
                if "close message has been sent" in str(exc):
                    disconnected.append(ws)
                else:
                    raise
            except Exception as exc:
                logger.error(f"Error sending message to {key}: {exc}", exc_info=True)
                disconnected.append(ws)
        
        # Clean up disconnected connections
        if disconnected:
            async with self._lock:
                for ws in disconnected:
                    if ws in self._connections[key]:
                        self._connections[key].remove(ws)
                        self._total_connections -= 1
                if not self._connections[key]:
                    del self._connections[key]
        
        return sent_count
    
    async def get_connection_count(self, key: Optional[str] = None) -> int:
        """
        Get connection count for a specific key or total connections.
        
        Args:
            key: Connection key (None for total)
            
        Returns:
            Connection count
        """
        async with self._lock:
            if key is None:
                return self._total_connections
            return len(self._connections.get(key, set()))
    
    async def has_connections(self, key: str) -> bool:
        """
        Check if there are any connections for a given key.
        
        Args:
            key: Connection key
            
        Returns:
            True if there are connections
        """
        async with self._lock:
            return len(self._connections.get(key, set())) > 0


# Global websocket manager instance
_manager: Optional[WebSocketManager] = None


def get_websocket_manager() -> WebSocketManager:
    """
    Get the global websocket manager instance.
    
    Returns:
        WebSocketManager instance
    """
    global _manager
    if _manager is None:
        _manager = WebSocketManager()
    return _manager




