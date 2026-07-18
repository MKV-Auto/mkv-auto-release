"""
Tests for transfer health monitoring system.
"""
import pytest
from api import models
from core.transfer import monitoring as transfer_health


def test_check_destination_health_local(test_db):
    """Test health check for local transfer."""
    with test_db() as session:
        config = models.TransferConfig(mode="local", name="Test")
        session.add(config)
        session.commit()
        results = transfer_health.check_destination_health(session, config)
    
    assert "overall" in results
    assert "connectivity" in results
    assert results["connectivity"]["status"] in ["healthy", "unhealthy", "unknown"]


def test_record_health_check(test_db):
    """Test recording health check results."""
    with test_db() as session:
        config = models.TransferConfig(
            mode="local",
            name="Test",
        )
        session.add(config)
        session.commit()
        
        results = {
            "overall": {"status": "healthy", "message": "OK", "response_time_ms": 10},
            "connectivity": {"status": "healthy", "message": "OK", "response_time_ms": 5},
        }
        
        transfer_health.record_health_check(session, config.id, results)
        
        checks = session.query(models.TransferHealthCheck).filter(
            models.TransferHealthCheck.transfer_config_id == config.id
        ).all()
        
        assert len(checks) == 2


def test_get_health_status(test_db):
    """Test getting health status."""
    with test_db() as session:
        config = models.TransferConfig(
            mode="local",
            name="Test",
        )
        session.add(config)
        session.commit()
        
        # Record a health check
        results = {
            "overall": {"status": "healthy", "message": "OK", "response_time_ms": 10},
        }
        transfer_health.record_health_check(session, config.id, results)
        
        status = transfer_health.get_health_status(session, config.id)
        
        assert "overall" in status


def test_get_health_history(test_db):
    """Test getting health check history."""
    with test_db() as session:
        config = models.TransferConfig(
            mode="local",
            name="Test",
        )
        session.add(config)
        session.commit()
        
        results = {
            "overall": {"status": "healthy", "message": "OK", "response_time_ms": 10},
        }
        transfer_health.record_health_check(session, config.id, results)
        
        history = transfer_health.get_health_history(session, config.id, days=7)
        
        assert len(history) >= 1
        assert history[0]["check_type"] == "overall"

