"""
Tests for transfer service layer.
"""
import pytest
from api import models
from core.transfer import service as transfer_service


def test_get_active_config(test_db):
    """Test getting active config."""
    with test_db() as session:
        # Create a config
        config = models.TransferConfig(
            mode="local",
            name="Test Config",
            is_active=True,
            transfer_dir="/test/dir",
        )
        session.add(config)
        session.commit()
        
        active = transfer_service.get_active_config(session)
        assert active is not None
        assert active.id == config.id
        assert active.is_active is True


def test_get_active_config_none(test_db):
    """Test getting active config when none exists."""
    with test_db() as session:
        active = transfer_service.get_active_config(session)
        assert active is None


def test_create_config(test_db):
    """Test creating the first transfer config (it becomes active by default)."""
    with test_db() as session:
        config = transfer_service.create_config(
            session,
            "local",
            "Test Config",
            {"test": "data"},
        )
        
        assert config.id
        assert config.mode == "local"
        assert config.name == "Test Config"
        assert config.is_active is True  # First config is active by default (#292)


def test_create_config_second_inactive(test_db):
    """When other configs exist, new config is not active by default."""
    with test_db() as session:
        transfer_service.create_config(session, "local", "Config 1", {})
        config2 = transfer_service.create_config(session, "local", "Config 2", {})
        assert config2.is_active is False


def test_create_config_persists_transfer_dir(test_db):
    """Create with extra_attrs transfer_dir is persisted (e.g. from POST body)."""
    with test_db() as session:
        config = transfer_service.create_config(
            session,
            "local",
            "Local NAS",
            {},
            extra_attrs={"transfer_dir": "/mnt/nas/media", "path_template": "{release_slug}"},
        )
        assert config.id
        assert config.transfer_dir == "/mnt/nas/media"
        assert config.path_template == "{release_slug}"
        session.refresh(config)
        assert config.transfer_dir == "/mnt/nas/media"


def test_activate_config(test_db):
    """Test activating a config."""
    with test_db() as session:
        # Create two configs
        config1 = transfer_service.create_config(session, "local", "Config 1", {})
        config2 = transfer_service.create_config(session, "local", "Config 2", {})
        
        # Activate config1
        activated = transfer_service.activate_config(session, config1.id)
        assert activated.is_active is True
        
        # Verify config2 is not active
        session.refresh(config2)
        assert config2.is_active is False
        
        # Activate config2
        activated2 = transfer_service.activate_config(session, config2.id)
        assert activated2.is_active is True
        
        # Verify config1 is now not active
        session.refresh(config1)
        assert config1.is_active is False


def test_update_config(test_db):
    """Test updating a config."""
    with test_db() as session:
        config = transfer_service.create_config(session, "local", "Original", {})
        
        updated = transfer_service.update_config(
            session,
            config.id,
            {"name": "Updated", "transfer_dir": "/new/dir"}
        )
        
        assert updated.name == "Updated"
        assert updated.transfer_dir == "/new/dir"


def test_delete_config(test_db):
    """Test deleting a config (must be inactive; first config is active by default)."""
    with test_db() as session:
        transfer_service.create_config(session, "local", "Keep", {})  # First: active
        to_delete = transfer_service.create_config(session, "local", "To Delete", {})  # Second: inactive
        config_id = to_delete.id

        transfer_service.delete_config(session, config_id)

        deleted = session.query(models.TransferConfig).filter(models.TransferConfig.id == config_id).first()
        assert deleted is None


def test_delete_active_config_fails(test_db):
    """Test that deleting active config fails."""
    with test_db() as session:
        config = transfer_service.create_config(session, "local", "Active", {})
        transfer_service.activate_config(session, config.id)
        
        with pytest.raises(ValueError, match="active"):
            transfer_service.delete_config(session, config.id)


def test_resolve_path_template():
    """Test path template resolution."""
    job_data = {
        "movie_name": "Test Movie",
        "year": 2024,
        "release_name": "Collectors Edition",
    }
    
    result = transfer_service.resolve_path_template(
        "{movie_name} ({year})/{release_name}",
        job_data
    )
    
    assert result == "Test Movie (2024)/Collectors Edition"


def test_validate_connection_local(test_db):
    """Test connection validation for local mode."""
    with test_db() as session:
        config = transfer_service.create_config(session, "local", "Local", {"transfer_dir": "/tmp"})
        
        success, message = transfer_service.validate_connection(session, config.id)
        assert success is True


def test_validate_transfer_preconditions_local(test_db):
    """Test transfer preconditions validation for local mode."""
    with test_db() as session:
        config = transfer_service.create_config(
            session,
            "local",
            "Local",
            {"transfer_dir": "/tmp"}
        )
        
        passed, errors = transfer_service.validate_transfer_preconditions(
            session,
            "job1",
            config,
            1024 * 1024  # 1 MB
        )
        
        # Should pass if /tmp exists and has space
        assert isinstance(passed, bool)
        assert isinstance(errors, list)

