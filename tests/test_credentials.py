"""Unit tests for user credentials and configuration manager."""

import json
import os
import tempfile
import pytest

from src.credentials import (
    find_private_key,
    get_credentials_path,
    get_secrets_dir,
    get_user_config_dir,
    load_user_credentials,
    resolve_runtime_credentials,
    save_private_key,
    save_user_credentials,
)


def test_custom_config_dir() -> None:
    """Verify that REPO360_CONFIG_DIR overrides the default home directory."""
    with tempfile.TemporaryDirectory() as temp_dir:
        os.environ["REPO360_CONFIG_DIR"] = temp_dir
        try:
            assert get_user_config_dir() == os.path.abspath(temp_dir)
            assert get_credentials_path() == os.path.join(temp_dir, "config.json")
            assert get_secrets_dir() == os.path.join(temp_dir, "secrets")
        finally:
            os.environ.pop("REPO360_CONFIG_DIR", None)


def test_save_and_load_credentials() -> None:
    """Test persisting and loading user credentials."""
    with tempfile.TemporaryDirectory() as temp_dir:
        os.environ["REPO360_CONFIG_DIR"] = temp_dir
        try:
            saved_path = save_user_credentials(
                app_id=123456,
                private_key_path="/fake/path/key.pem",
                installation_id=78910,
                api_base_url="https://api.github.com",
                llm_provider="openai",
                llm_model="gpt-4o",
                llm_api_key="sk-test-12345",
                llm_enabled=True,
            )
            assert os.path.isfile(saved_path)

            loaded = load_user_credentials()
            assert loaded["github"]["app_id"] == 123456
            assert loaded["github"]["installation_id"] == 78910
            assert loaded["github"]["api_base_url"] == "https://api.github.com"
            assert loaded["llm"]["provider"] == "openai"
            assert loaded["llm"]["model"] == "gpt-4o"
            assert loaded["llm"]["api_key"] == "sk-test-12345"
            assert loaded["llm"]["enabled"] is True
        finally:
            os.environ.pop("REPO360_CONFIG_DIR", None)


def test_save_private_key() -> None:
    """Test saving private key with standardized naming convention."""
    with tempfile.TemporaryDirectory() as temp_dir:
        os.environ["REPO360_CONFIG_DIR"] = temp_dir
        try:
            sample_pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----"
            key_path = save_private_key(sample_pem, app_slug="test-app")
            assert os.path.isfile(key_path)
            assert "test-app" in key_path
            assert key_path.endswith(".private-key.pem")

            with open(key_path, "r", encoding="utf-8") as f:
                content = f.read()
            assert content == sample_pem
        finally:
            os.environ.pop("REPO360_CONFIG_DIR", None)


def test_resolve_runtime_credentials_env_priority() -> None:
    """Verify environment variables take precedence over saved user configuration."""
    with tempfile.TemporaryDirectory() as temp_dir:
        os.environ["REPO360_CONFIG_DIR"] = temp_dir
        try:
            save_user_credentials(
                app_id=111111,
                private_key_path="dummy.pem",
                installation_id=222222,
                llm_provider="gemini",
                llm_api_key="key-from-config",
            )

            os.environ["REPO360_APP_ID"] = "999999"
            os.environ["REPO360_APP_INSTALLATION_ID"] = "888888"
            os.environ["GEMINI_API_KEY"] = "key-from-env"

            creds = resolve_runtime_credentials()
            assert creds["app_id"] == "999999"
            assert creds["installation_id"] == "888888"
            assert creds["llm_api_key"] == "key-from-env"
        finally:
            os.environ.pop("REPO360_CONFIG_DIR", None)
            os.environ.pop("REPO360_APP_ID", None)
            os.environ.pop("REPO360_APP_INSTALLATION_ID", None)
            os.environ.pop("GEMINI_API_KEY", None)


def test_resolve_runtime_credentials_unified_ai_key() -> None:
    """Verify single unified AI_API_KEY and model provider auto-detection."""
    with tempfile.TemporaryDirectory() as temp_dir:
        os.environ["REPO360_CONFIG_DIR"] = temp_dir
        os.environ["AI_API_KEY"] = "sk-unified-key-12345"
        os.environ["AI_MODEL"] = "gpt-4o"
        try:
            creds = resolve_runtime_credentials()
            assert creds["llm_api_key"] == "sk-unified-key-12345"
            assert creds["llm_provider"] == "openai"
            assert creds["llm_model"] == "gpt-4o"

            os.environ["AI_MODEL"] = "claude-3-5-sonnet-20241022"
            creds_claude = resolve_runtime_credentials()
            assert creds_claude["llm_provider"] == "anthropic"
            assert creds_claude["llm_model"] == "claude-3-5-sonnet-20241022"
        finally:
            os.environ.pop("REPO360_CONFIG_DIR", None)
            os.environ.pop("AI_API_KEY", None)
            os.environ.pop("AI_MODEL", None)


def test_posix_directory_and_file_permissions(tmp_path) -> None:
    """POSIX mode creates config/secrets dirs with 0700 and sensitive files with 0600."""
    from unittest.mock import patch

    cfg_dir = tmp_path / ".repo360"
    with patch.dict(os.environ, {"REPO360_CONFIG_DIR": str(cfg_dir)}), \
         patch("src.credentials.os.name", "posix"), \
         patch("src.credentials.os.chmod") as mock_chmod:
        key_path = save_private_key("PRIVATE_KEY", app_slug="demo")
        cfg_path = save_user_credentials(app_id=123, private_key_path=key_path)

    chmod_calls = [call.args for call in mock_chmod.call_args_list]
    assert (str(cfg_dir), 0o700) in chmod_calls
    assert (str(cfg_dir / "secrets"), 0o700) in chmod_calls
    assert (key_path, 0o600) in chmod_calls
    assert (cfg_path, 0o600) in chmod_calls
