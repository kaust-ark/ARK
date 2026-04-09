import pytest
import json
from ark.webapp.crypto import encrypt_text, decrypt_text

class TestCredentialSecurity:
    """Tests for per-user credential isolation and encryption."""

    @pytest.fixture
    def test_users(self):
        return {
            "user_a": "user-apple-123",
            "user_b": "user-banana-456"
        }

    def test_encryption_isolation(self, test_users):
        """Verify that identical plaintexts result in different ciphertexts for different users."""
        test_secret = "sk-ant-1234567890abcdef"
        
        user_a = test_users["user_a"]
        user_b = test_users["user_b"]
        
        enc_a = encrypt_text(test_secret, user_a)
        enc_b = encrypt_text(test_secret, user_b)
        
        assert enc_a != enc_b, "Ciphertexts must be different for different users (salting check)"
        assert len(enc_a) > 0
        assert len(enc_b) > 0

    def test_decryption_isolation(self, test_users):
        """Verify that a user cannot decrypt another user's ciphertext."""
        test_secret = "sk-ant-isolation-test"
        
        user_a = test_users["user_a"]
        user_b = test_users["user_b"]
        
        enc_a = encrypt_text(test_secret, user_a)
        
        # Successful decryption for owner
        dec_a = decrypt_text(enc_a, user_a)
        assert dec_a == test_secret
        
        # Failed decryption for other user
        dec_b = decrypt_text(enc_a, user_b)
        assert dec_b == "", "User B should not be able to decrypt User A's data"

    def test_empty_inputs(self):
        """Verify handling of empty strings and missing user IDs."""
        assert encrypt_text("", "user-1") == ""
        assert encrypt_text("secret", "") == ""
        assert decrypt_text("", "user-1") == ""
        assert decrypt_text("cipher", "") == ""

    def test_invalid_ciphertext(self, test_users):
        """Verify that invalid ciphertexts return empty strings rather than crashing."""
        user_a = test_users["user_a"]
        assert decrypt_text("not-a-valid-fernet-token", user_a) == ""
