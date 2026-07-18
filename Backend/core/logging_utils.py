"""
Centralized logging utility for MKV-Auto backend.

Provides a unified logging interface with:
- Log levels: ERROR, WARNING, INFO, DEBUG
- Facility format: `module_name:function_name`
- Environment variable: `MKVAUTO_DEBUG_LEVEL` (case-insensitive)
- Formatter: `%(asctime)s [%(levelname)s] %(name)s: %(message)s`
"""

import logging
import os
import functools
from typing import Optional
from pathlib import Path
from logging.handlers import RotatingFileHandler


def _get_log_level_from_env() -> int:
    """
    Read MKVAUTO_DEBUG_LEVEL from environment and map to logging level.
    
    Returns:
        int: Logging level (ERROR=40, WARNING=30, INFO=20, DEBUG=10)
    """
    env_level = os.getenv("MKVAUTO_DEBUG_LEVEL", "INFO").upper()
    
    level_map = {
        "ERROR": logging.ERROR,      # 40
        "WARNING": logging.WARNING,   # 30
        "INFO": logging.INFO,         # 20
        "DEBUG": logging.DEBUG,        # 10
    }
    
    return level_map.get(env_level, logging.INFO)


def get_logger(module_name: str, function_name: Optional[str] = None) -> logging.Logger:
    """
    Get a configured logger instance with facility format.
    
    Args:
        module_name: Module name (e.g., "api.main", "core.utils")
        function_name: Optional function name (e.g., "_handle_udev_event")
    
    Returns:
        logging.Logger: Configured logger instance
    """
    # Build facility name: module_name:function_name or just module_name
    if function_name:
        facility = f"{module_name}:{function_name}"
    else:
        facility = module_name
    
    logger = logging.getLogger(facility)
    
    # Set log level from environment
    log_level = _get_log_level_from_env()
    logger.setLevel(log_level)
    
    # Configure formatter: "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    
    # Only add handler if logger doesn't already have one
    # This prevents duplicate log entries when multiple handlers exist
    if not logger.handlers:
        # Add console handler (for development)
        console_handler = logging.StreamHandler()
        console_handler.setLevel(log_level)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    
    # Ensure all handlers use the same formatter
    for handler in logger.handlers:
        handler.setFormatter(formatter)
        handler.setLevel(log_level)
    
    return logger


def log_function(func):
    """
    Decorator to automatically capture function name for logging.
    
    Usage:
        @log_function
        def my_function():
            logger = get_logger(__name__, my_function.__name__)
            logger.info("Function called")
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Get logger with function name
        module_name = func.__module__ if hasattr(func, '__module__') else __name__
        logger = get_logger(module_name, func.__name__)
        
        # Call original function
        return func(*args, **kwargs)
    
    return wrapper
