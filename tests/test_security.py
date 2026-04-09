import pytest
import json
import shlex
import sqlite3
import tempfile
from pathlib import Path

pytest.importorskip("cryptography", reason="webapp optional deps not installed")
pytest.importorskip("fastapi", reason="webapp optional deps not installed")

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


class TestSlurmInjection:
    """Tests that shell metacharacters in API keys are safely escaped."""

    def test_shlex_quote_prevents_injection(self):
        """Malicious key values must be escaped before rendering into SLURM template."""
        from jinja2 import Template

        template_text = Path(__file__).parent.parent / "ark" / "webapp" / "slurm_template.sh"
        template = Template(template_text.read_text())

        malicious_keys = {
            "claude_oauth_token": '"; rm -rf / #',
            "gemini": "$(whoami)",
            "openai": "key with `backticks`",
        }
        safe_keys = {k: shlex.quote(v) for k, v in malicious_keys.items()}

        rendered = template.render(
            project_id="test",
            project_dir="/tmp/test",
            log_dir="/tmp/test/logs",
            mode="paper",
            max_iterations=2,
            partition="batch",
            account="",
            gres="",
            cpus_per_task=1,
            conda_env="ark",
            api_keys=safe_keys,
        )

        # shlex.quote wraps values in single quotes — verify safe versions appear
        # and dangerous unquoted patterns do not
        assert "CLAUDE_CODE_OAUTH_TOKEN='\"" in rendered  # single-quoted, not double-quoted
        assert "GEMINI_API_KEY='$(whoami)'" in rendered    # $(whoami) inside single quotes = safe
        assert "OPENAI_API_KEY='key with `backticks`'" in rendered

        # The raw values must NOT appear in double quotes (which would allow expansion)
        assert 'CLAUDE_CODE_OAUTH_TOKEN="' not in rendered
        assert 'GEMINI_API_KEY="' not in rendered
        assert 'OPENAI_API_KEY="' not in rendered


class TestDbMigration:
    """Tests that the DB migration adds the encrypted_keys column."""

    def test_migrate_adds_column(self):
        """Column is added to an existing table missing it."""
        from ark.webapp.db import _migrate
        from sqlalchemy import create_engine, text

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                "CREATE TABLE user (id TEXT PRIMARY KEY, email TEXT, name TEXT)"
            )
            conn.commit()
            conn.close()

            engine = create_engine(f"sqlite:///{db_path}", echo=False)
            try:
                _migrate(engine)

                with engine.connect() as c:
                    rows = c.execute(text("PRAGMA table_info(user)")).fetchall()
                    cols = {row[1] for row in rows}

                assert "encrypted_keys" in cols
            finally:
                engine.dispose()

    def test_migrate_idempotent(self):
        """Running migration twice does not fail."""
        from ark.webapp.db import _migrate
        from sqlalchemy import create_engine, text

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                "CREATE TABLE user (id TEXT PRIMARY KEY, email TEXT, encrypted_keys TEXT)"
            )
            conn.commit()
            conn.close()

            engine = create_engine(f"sqlite:///{db_path}", echo=False)
            try:
                _migrate(engine)  # should not raise
                _migrate(engine)  # second call, still should not raise
            finally:
                engine.dispose()
