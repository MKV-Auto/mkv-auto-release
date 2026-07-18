"""
Tests for transfer credential encryption/decryption.
"""
import pytest
from core.transfer.utils.credentials import (
    encrypt_value,
    decrypt_value,
    encrypt_and_store_credentials,
    get_decrypted_credentials,
)


def test_encrypt_decrypt_round_trip():
    """Test that encryption and decryption work correctly."""
    original = "test password"
    
    encrypted = encrypt_value(original)
    assert encrypted != original
    assert encrypted  # Should not be empty
    
    decrypted = decrypt_value(encrypted)
    assert decrypted == original


def test_encrypt_empty_string():
    """Test encrypting empty string."""
    encrypted = encrypt_value("")
    assert encrypted == ""


def test_decrypt_empty_string():
    """Test decrypting empty string."""
    decrypted = decrypt_value("")
    assert decrypted == ""


def test_encrypt_decrypt_special_characters():
    """Test encryption with special characters."""
    original = "p@ssw0rd!#$%^&*()"
    
    encrypted = encrypt_value(original)
    decrypted = decrypt_value(encrypted)
    
    assert decrypted == original


def test_encrypt_and_store_credentials(test_db):
    """Test storing encrypted credentials."""
    from api import models
    
    with test_db() as session:
        config = models.TransferConfig(
            mode="rsync",
            name="Test",
        )
        session.add(config)
        session.commit()
        
        credentials = {
            "rsync_key": "test key data",
            "smb_password": "test password",
        }
        
        encrypt_and_store_credentials(session, config.id, credentials)
        
        # Verify credentials were stored
        stored = session.query(models.TransferCredential).filter(
            models.TransferCredential.transfer_config_id == config.id
        ).all()
        
        assert len(stored) == 2
        assert stored[0].value != "test key data"  # Should be encrypted


def test_get_decrypted_credentials(test_db):
    """Test retrieving and decrypting credentials."""
    from api import models
    
    with test_db() as session:
        config = models.TransferConfig(
            mode="rsync",
            name="Test",
        )
        session.add(config)
        session.commit()
        
        credentials = {
            "rsync_key": "test key data",
        }
        
        encrypt_and_store_credentials(session, config.id, credentials)
        
        decrypted = get_decrypted_credentials(session, config.id)
        assert decrypted["rsync_key"] == "test key data"

