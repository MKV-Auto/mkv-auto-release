"""
Conflict resolution system for handling file conflicts during transfers.
"""
from pathlib import Path
from typing import Literal, Tuple


ConflictResolution = Literal["overwrite", "skip", "rename", "fail"]


def check_conflict(dest_path: Path) -> bool:
    """
    Check if a file exists at the destination path.
    
    Args:
        dest_path: Destination file path
        
    Returns:
        True if file exists, False otherwise
    """
    return dest_path.exists() and dest_path.is_file()


def generate_unique_name(base_path: Path) -> Path:
    """
    Generate a unique filename by appending a numeric suffix.
    
    Args:
        base_path: Base file path
        
    Returns:
        Path with unique name (e.g., "filename (1).mkv", "filename (2).mkv")
    """
    if not base_path.exists():
        return base_path
    
    stem = base_path.stem
    suffix = base_path.suffix
    parent = base_path.parent
    
    counter = 1
    while True:
        new_name = f"{stem} ({counter}){suffix}"
        new_path = parent / new_name
        if not new_path.exists():
            return new_path
        counter += 1
        if counter > 1000:  # Safety limit
            raise RuntimeError(f"Could not generate unique name for {base_path}")


def resolve_conflict(dest_path: Path, strategy: ConflictResolution) -> Tuple[Path, bool]:
    """
    Apply conflict resolution strategy.
    
    Args:
        dest_path: Destination file path
        strategy: Resolution strategy
        
    Returns:
        Tuple of (resolved_path, should_proceed)
        - resolved_path: The path to use (may be modified based on strategy)
        - should_proceed: Whether to proceed with transfer (False for skip/fail)
    """
    if not check_conflict(dest_path):
        return dest_path, True
    
    if strategy == "overwrite":
        return dest_path, True
    elif strategy == "skip":
        return dest_path, False
    elif strategy == "rename":
        return generate_unique_name(dest_path), True
    elif strategy == "fail":
        raise FileExistsError(f"File already exists at {dest_path} and conflict resolution is 'fail'")
    else:
        # Default to overwrite
        return dest_path, True

