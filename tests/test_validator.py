"""Unit tests for configuration and repository inventory validator."""

import json
import os
import tempfile
import pytest

from src.validator import ConfigValidator, RepoTarget


def test_substitute_env_vars() -> None:
    """Test environment variable replacement with defaults and nested fallback."""
    os.environ["TEST_ORG"] = "my-test-org"
    text = '{"org": "${TEST_ORG}", "port": "${PORT:-8080}"}'
    result = ConfigValidator.substitute_env_vars(text)
    assert '"org": "my-test-org"' in result
    assert '"port": "8080"' in result

    nested_text = '{"api_key": "${LLM_API_KEY:-${GEMINI_API_KEY:-}}"}'
    os.environ.pop("LLM_API_KEY", None)
    os.environ.pop("GEMINI_API_KEY", None)
    res_empty = ConfigValidator.substitute_env_vars(nested_text)
    assert '"api_key": ""' in res_empty

    os.environ["GEMINI_API_KEY"] = "my-gemini-key"
    res_key = ConfigValidator.substitute_env_vars(nested_text)
    assert '"api_key": "my-gemini-key"' in res_key
    os.environ.pop("GEMINI_API_KEY", None)


def test_update_env_file() -> None:
    """Test safely updating and appending key-value pairs in .env."""
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".env", encoding="utf-8") as f:
        f.write("# Environment Config\nEXISTING_VAR=123\n")
        temp_env = f.name

    try:
        ok = ConfigValidator.update_env_file("REPO360_APP_INSTALLATION_ID", "998877", temp_env)
        assert ok is True
        with open(temp_env, "r", encoding="utf-8") as f:
            content = f.read()
        assert "EXISTING_VAR=123" in content
        assert "REPO360_APP_INSTALLATION_ID=998877" in content

        ok2 = ConfigValidator.update_env_file("REPO360_APP_INSTALLATION_ID", "112233", temp_env)
        assert ok2 is True
        with open(temp_env, "r", encoding="utf-8") as f:
            content2 = f.read()
        assert "REPO360_APP_INSTALLATION_ID=112233" in content2
        assert "998877" not in content2
    finally:
        if os.path.exists(temp_env):
            os.remove(temp_env)


def test_load_and_validate_json_configs() -> None:
    """Test default app and advisory configuration retrieval and validation."""
    app_cfg = ConfigValidator.get_default_app_config()
    assert "github" in app_cfg
    assert "reporting" in app_cfg

    advisory_cfg = ConfigValidator.get_default_advisory_config()
    assert "enabled_checks" in advisory_cfg
    assert "policy_thresholds" in advisory_cfg


def test_load_and_validate_json_file() -> None:
    """Test loading and validating JSON config file."""
    sample = {"key": "value"}
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as f:
        json.dump(sample, f)
        temp_json = f.name

    try:
        loaded = ConfigValidator.load_and_validate_json(temp_json, is_advisory=False)
        assert loaded == sample
    finally:
        if os.path.exists(temp_json):
            os.remove(temp_json)


def test_validate_advisory_structure_error() -> None:
    """Test advisory schema validation error on malformed input."""
    malformed = {"not_enabled_checks": {}}
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as f:
        json.dump(malformed, f)
        temp_json = f.name

    try:
        with pytest.raises(ValueError, match="Missing 'enabled_checks' object"):
            ConfigValidator.load_and_validate_json(temp_json, is_advisory=True)
    finally:
        if os.path.exists(temp_json):
            os.remove(temp_json)


def test_repo_target_dataclass() -> None:
    """Verify RepoTarget model attributes and defaults."""
    target = RepoTarget(org_name="org1", repo_name="repo1", target_personas={"all"})
    assert target.org_name == "org1"
    assert target.repo_name == "repo1"
    assert target.default_branch == "main"
    assert target.owner_team == ""
