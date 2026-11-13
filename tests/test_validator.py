"""Unit tests for configuration and repository inventory validator."""

import os
import tempfile
import pytest

from src.validator import ConfigValidator


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



def test_validate_repos_csv_valid() -> None:
    """Test parsing a valid repos.csv file."""
    csv_content = """org_name,repo_name,target_personas,default_branch,owner_team
test-org,core-api,all,main,@test-org/backend
test-org,frontend,contributor,develop,@test-org/web
"""
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv", encoding="utf-8") as f:
        f.write(csv_content)
        temp_path = f.name

    try:
        targets = ConfigValidator.load_and_validate_repos_csv(temp_path)
        assert len(targets) == 2
        assert targets[0].org_name == "test-org"
        assert targets[0].repo_name == "core-api"
        assert "all" in targets[0].target_personas

        assert targets[1].repo_name == "frontend"
        assert targets[1].default_branch == "develop"
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def test_validate_repos_csv_empty_names() -> None:
    """Ensure empty repo names raise validation errors."""
    csv_content = "org_name,repo_name\ntest-org,\n"
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv", encoding="utf-8") as f:
        f.write(csv_content)
        temp_path = f.name

    try:
        with pytest.raises(ValueError, match="'org_name' and 'repo_name' cannot be empty"):
            ConfigValidator.load_and_validate_repos_csv(temp_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def test_validate_repos_csv_missing_columns() -> None:
    """Ensure missing required columns trigger schema errors."""
    csv_content = "org_name,some_other_column\ntest-org,val\n"
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv", encoding="utf-8") as f:
        f.write(csv_content)
        temp_path = f.name

    try:
        with pytest.raises(ValueError, match="missing required columns"):
            ConfigValidator.load_and_validate_repos_csv(temp_path, optional=False)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def test_validate_repos_csv_optional_missing() -> None:
    """Verify that optional=True returns empty list when file does not exist."""
    targets = ConfigValidator.load_and_validate_repos_csv("non_existent_repos.csv", optional=True)
    assert targets == []


def test_validate_repos_csv_path_traversal() -> None:
    """Ensure path traversal payloads in org_name or repo_name are rejected."""
    traversal_csv = "org_name,repo_name\n../../evil,valid-repo\n"
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv", encoding="utf-8") as f:
        f.write(traversal_csv)
        temp_path = f.name
    try:
        with pytest.raises(ValueError, match=r"Path separators and '\.\.' are prohibited"):
            ConfigValidator.load_and_validate_repos_csv(temp_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    traversal_repo_csv = "org_name,repo_name\nvalid-org,../secret\n"
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv", encoding="utf-8") as f:
        f.write(traversal_repo_csv)
        temp_path2 = f.name
    try:
        with pytest.raises(ValueError, match=r"Path separators and '\.\.' are prohibited"):
            ConfigValidator.load_and_validate_repos_csv(temp_path2)
    finally:
        if os.path.exists(temp_path2):
            os.remove(temp_path2)


def test_validate_repos_csv_flexible_aliases_and_flags() -> None:
    """Test flexible column aliases (org, repo, enabled, etc.) and comment handling."""
    csv_content = """# Comment line to ignore
org,repo,enabled,personas,branch,team
my-org,active-svc,true,all,main,@my-org/core
my-org,disabled-svc,false,repo_admin,main,@my-org/core
# Trailing comment
"""
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv", encoding="utf-8") as f:
        f.write(csv_content)
        temp_path = f.name
    try:
        targets = ConfigValidator.load_and_validate_repos_csv(temp_path)
        assert len(targets) == 1
        assert targets[0].org_name == "my-org"
        assert targets[0].repo_name == "active-svc"
        assert targets[0].owner_team == "@my-org/core"
        # Disabled repo should be excluded
        assert not any(t.repo_name == "disabled-svc" for t in targets)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    # Test alternate aliases: organization, repository, active
    csv_alt = """organization,repository,active
other-org,frontend,yes
other-org,inactive,0
"""
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv", encoding="utf-8") as f:
        f.write(csv_alt)
        temp_path2 = f.name
    try:
        targets2 = ConfigValidator.load_and_validate_repos_csv(temp_path2)
        assert len(targets2) == 1
        assert targets2[0].org_name == "other-org"
        assert targets2[0].repo_name == "frontend"
    finally:
        if os.path.exists(temp_path2):
            os.remove(temp_path2)
