"""
Credential encryption/decryption for secure storage.
"""
from typing import Dict, Optional
from sqlalchemy.orm import Session
from api import models
import os
import base64
from cryptography.fernet import Fernet
import logging

log = logging.getLogger(__name__)


def _get_encryption_key() -> bytes:
    """
    Get encryption key from environment or generate a default.
    In production, this should be set via environment variable.
    """
    key_str = os.getenv("TRANSFER_CREDENTIALS_KEY")
    if key_str:
        return key_str.encode()
    
    # Fallback: use a default key (not secure, but functional)
    # In production, this should be set via environment variable
    default_key = b"default_transfer_key_32_bytes_long!!"
    log.warning("Using default encryption key. Set TRANSFER_CREDENTIALS_KEY environment variable for production.")
    return default_key


def _get_cipher() -> Fernet:
    """Get Fernet cipher instance."""
    key = _get_encryption_key()
    # Fernet requires 32-byte key, so we hash it if needed
    if len(key) != 32:
        import hashlib
        key = hashlib.sha256(key).digest()
    
    # Fernet needs base64-encoded key
    key_b64 = base64.urlsafe_b64encode(key)
    return Fernet(key_b64)


def encrypt_value(value: str) -> str:
    """
    Encrypt a credential value.
    
    Args:
        value: Plain text value to encrypt
        
    Returns:
        Encrypted value (base64-encoded)
    """
    if not value:
        return ""
    
    cipher = _get_cipher()
    encrypted = cipher.encrypt(value.encode())
    return encrypted.decode()


def decrypt_value(encrypted_value: str) -> str:
    """
    Decrypt a credential value.
    
    Args:
        encrypted_value: Encrypted value (base64-encoded)
        
    Returns:
        Decrypted plain text value
    """
    if not encrypted_value:
        return ""
    
    try:
        cipher = _get_cipher()
        decrypted = cipher.decrypt(encrypted_value.encode())
        return decrypted.decode()
    except Exception as e:
        log.error(f"Error decrypting credential: {e}")
        raise


def encrypt_and_store_credentials(
    db: Session,
    config_id: str,
    credentials: Dict[str, str]
) -> None:
    """
    Encrypt and store credentials for a transfer config.
    
    Args:
        db: Database session
        config_id: Transfer config ID
        credentials: Dictionary of credential type -> value
    """
    for cred_type, value in credentials.items():
        if not value:
            continue
        
        encrypted = encrypt_value(value)
        
        # Check if credential already exists
        existing = db.query(models.TransferCredential).filter(
            models.TransferCredential.transfer_config_id == config_id,
            models.TransferCredential.type == cred_type
        ).first()
        
        if existing:
            existing.value = encrypted
        else:
            credential = models.TransferCredential(
                transfer_config_id=config_id,
                type=cred_type,
                value=encrypted
            )
            db.add(credential)
    
    db.commit()


def get_decrypted_credentials(
    db: Session,
    config_id: str
) -> Dict[str, str]:
    """
    Get and decrypt credentials for a transfer config.
    
    Args:
        db: Database session
        config_id: Transfer config ID
        
    Returns:
        Dictionary of credential type -> decrypted value
    """
    credentials = db.query(models.TransferCredential).filter(
        models.TransferCredential.transfer_config_id == config_id
    ).all()
    
    result = {}
    for cred in credentials:
        try:
            result[cred.type] = decrypt_value(cred.value)
        except Exception as e:
            log.error(f"Error decrypting credential {cred.type} for config {config_id}: {e}")
            result[cred.type] = ""
    
    return result











