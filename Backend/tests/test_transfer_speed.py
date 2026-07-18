"""
Tests for transfer speed monitoring.
"""
import time
import pytest
from core.transfer.monitoring import SpeedTracker, calculate_speed


def test_calculate_speed():
    """Test speed calculation."""
    # 100 MB in 10 seconds = 10 MB/s
    speed = calculate_speed(100 * 1024 * 1024, 10.0)
    assert abs(speed - 10.0) < 0.01


def test_calculate_speed_zero_time():
    """Test speed calculation with zero time."""
    speed = calculate_speed(1000, 0.0)
    assert speed == 0.0


def test_speed_tracker_start():
    """Test speed tracker initialization."""
    tracker = SpeedTracker()
    tracker.start()
    
    assert tracker.start_time is not None
    assert tracker.bytes_transferred == 0


def test_speed_tracker_update():
    """Test speed tracker update."""
    tracker = SpeedTracker()
    tracker.start()
    
    time.sleep(0.1)  # Small delay
    speed = tracker.update(1024 * 1024)  # 1 MB
    
    assert speed >= 0
    assert tracker.bytes_transferred == 1024 * 1024


def test_speed_tracker_average_speed():
    """Test average speed calculation."""
    tracker = SpeedTracker()
    tracker.start()
    
    tracker.update(100 * 1024 * 1024)  # 100 MB
    time.sleep(1.0)
    
    avg_speed = tracker.get_average_speed()
    assert avg_speed > 0


def test_speed_tracker_elapsed_time():
    """Test elapsed time calculation."""
    tracker = SpeedTracker()
    tracker.start()
    
    time.sleep(0.5)
    elapsed = tracker.get_elapsed_time()
    
    assert elapsed >= 0.5
    assert elapsed < 1.0











