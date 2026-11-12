"""Centralized Credential and Configuration Management for Repo360.

Enables 100% zero-configuration execution:
- Eliminates the requirement for local configuration or environment files.
- Persists credentials securely in the user profile directory: ~/.repo360/config.json
- Stores RSA private keys in: ~/.repo360/secrets/
- Supports multi-tier resolution: Environment Variables > Local .env > User Profile Config.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("repo360-credentials")


def resolve_user_config_dir() -> str:
    """Resolve the OS-standard user configuration directory for Repo360 without creating it.

    Can be overridden via the REPO360_CONFIG_DIR environment variable.
    """
    custom = os.getenv("REPO360_CONFIG_DIR")
    if custom:
        return os.path.abspath(custom)
    return os.path.expanduser("~/.repo360")


def get_user_config_dir() -> str:
    """Return OS-standard user configuration directory for Repo360, creating it if absent.

    Can be overridden via REPO360_CONFIG_DIR environment variable.
    """
    path = resolve_user_config_dir()
    os.makedirs(path, exist_ok=True)
    return path


def get_credentials_path() -> str:
    """Return path to saved credentials JSON file."""
    return os.path.join(get_user_config_dir(), "config.json")


def get_secrets_dir() -> str:
    """Return path to secrets directory where private keys are stored."""
    path = os.path.join(get_user_config_dir(), "secrets")
    os.makedirs(path, exist_ok=True)
    return path


def enforce_strict_permissions(file_path: str) -> None:
    """Enforce strict owner permissions (0600) with safe cross-platform fallback."""
    try:
        os.chmod(file_path, 0o600)
    except Exception as err:
        logger.debug("Failed setting file permissions on %s: %s", file_path, err)


def save_private_key(key_content: str, app_slug: str = "repo360", dest_dir: Optional[str] = None) -> str:
    """Save RSA private key with standardized naming convention and safe permissions."""
    today_str = time.strftime("%Y-%m-%d")
    filename = f"{app_slug}.{today_str}.private-key.pem"
    target_dir = dest_dir or get_secrets_dir()
    os.makedirs(target_dir, exist_ok=True)
    key_path = os.path.join(target_dir, filename)

    with open(key_path, "w", encoding="utf-8") as f:
        f.write(key_content)

    enforce_strict_permissions(key_path)
    return key_path


def save_user_credentials(
    app_id: int | str,
    private_key_path: str,
    installation_id: Optional[int | str] = None,
    api_base_url: str = "https://api.github.com",
    llm_provider: str = "gemini",
    llm_model: str = "gemini-3.5-flash",
    llm_api_key: str = "",
    llm_enabled: bool = True,
) -> str:
    """Persist user credentials and AI preferences to ~/.repo360/config.json."""
    config_data: Dict[str, Any] = {
        "github": {
            "app_id": int(app_id) if str(app_id).isdigit() else app_id,
            "installation_id": int(installation_id) if installation_id and str(installation_id).isdigit() else installation_id,
            "private_key_path": os.path.abspath(private_key_path) if os.path.isfile(private_key_path) else private_key_path,
            "api_base_url": api_base_url.rstrip("/"),
        },
        "llm": {
            "provider": llm_provider,
            "model": llm_model,
            "api_key": llm_api_key.strip(),
            "enabled": llm_enabled and (llm_provider != "none"),
        },
    }

    config_path = get_credentials_path()
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2)

    enforce_strict_permissions(config_path)
    return config_path


def load_user_credentials() -> Dict[str, Any]:
    """Load configuration from ~/.repo360/config.json if it exists."""
    config_path = get_credentials_path()
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as err:
            logger.debug("Failed reading user config from %s: %s", config_path, err)
    return {}


def find_private_key(candidate_path: Optional[str] = None) -> Optional[str]:
    """Search for a valid private key in specified path, user secrets dir, or local secrets/."""
    if candidate_path:
        expanded = os.path.expanduser(candidate_path)
        if os.path.isfile(expanded):
            return os.path.abspath(expanded)
        if candidate_path.startswith("-----BEGIN"):
            return candidate_path

    user_secrets = get_secrets_dir()
    if os.path.isdir(user_secrets):
        keys = [
            os.path.join(user_secrets, f)
            for f in sorted(os.listdir(user_secrets), reverse=True)
            if f.endswith(".private-key.pem") or f.endswith(".pem")
        ]
        if keys:
            return keys[0]

    local_secrets = os.path.join(os.getcwd(), "secrets")
    if os.path.isdir(local_secrets):
        keys = [
            os.path.join(local_secrets, f)
            for f in sorted(os.listdir(local_secrets), reverse=True)
            if f.endswith(".private-key.pem") or f.endswith(".pem")
        ]
        if keys:
            return keys[0]

    return None


def resolve_runtime_credentials() -> Dict[str, Any]:
    """Resolve credentials across environment variables, local .env, and user profile config.

    Precedence:
    1. System environment variables (REPO360_*)
    2. Local .env file (if running inside a cloned repository)
    3. User profile config (~/.repo360/config.json)
    """
    user_cfg = load_user_credentials()
    user_gh = user_cfg.get("github", {})
    user_llm = user_cfg.get("llm", {})

    app_id = (os.getenv("REPO360_APP_ID") or user_gh.get("app_id") or "")
    if app_id:
        app_id = str(app_id).strip()

    installation_id = (
        os.getenv("REPO360_APP_INSTALLATION_ID")
        or user_gh.get("installation_id")
        or ""
    )
    if installation_id:
        installation_id = str(installation_id).strip()

    api_base_url = (
        os.getenv("REPO360_API_BASE_URL")
        or user_gh.get("api_base_url")
        or "https://api.github.com"
    ).rstrip("/")

    raw_key = os.getenv("REPO360_APP_PRIVATE_KEY") or ""
    key_path = (
        os.getenv("REPO360_APP_PRIVATE_KEY_PATH")
        or user_gh.get("private_key_path")
        or ""
    )

    resolved_key = ""
    if raw_key and raw_key.startswith("-----BEGIN"):
        resolved_key = raw_key
    else:
        found = find_private_key(key_path or raw_key)
        if found:
            resolved_key = found

    llm_provider = (
        os.getenv("AI_PROVIDER")
        or os.getenv("LLM_PROVIDER")
        or user_llm.get("provider")
        or "gemini"
    ).lower().strip()

    env_model = (
        os.getenv("AI_MODEL")
        or os.getenv("LLM_MODEL")
        or user_llm.get("model")
        or ""
    ).strip()
    if env_model:
        m_lower = env_model.lower()
        if m_lower.startswith(("gpt-", "o1", "o3", "chatgpt-")):
            llm_provider = "openai"
        elif m_lower.startswith("claude-"):
            llm_provider = "anthropic"
        elif m_lower.startswith("gemini-"):
            llm_provider = "gemini"

    default_models = {
        "gemini": "gemini-3.5-flash",
        "openai": "gpt-4o",
        "anthropic": "claude-3-5-sonnet-20241022",
    }
    llm_model = env_model or default_models.get(llm_provider, "gemini-3.5-flash")

    llm_api_key = (
        os.getenv("AI_API_KEY")
        or os.getenv("LLM_API_KEY")
        or ""
    ).strip()

    if not llm_api_key:
        if llm_provider in ("openai", "azure"):
            llm_api_key = (
                os.getenv("OPENAI_API_KEY")
                or user_llm.get("api_key")
                or ""
            ).strip()
        elif llm_provider == "anthropic":
            llm_api_key = (
                os.getenv("ANTHROPIC_API_KEY")
                or user_llm.get("api_key")
                or ""
            ).strip()
        else:
            llm_api_key = (
                os.getenv("GEMINI_API_KEY")
                or user_llm.get("api_key")
                or ""
            ).strip()

    if llm_api_key.startswith("${") and llm_api_key.endswith("}"):
        llm_api_key = ""

    llm_enabled = user_llm.get("enabled", True)
    if os.getenv("LLM_ENABLED") is not None:
        llm_enabled = os.getenv("LLM_ENABLED", "true").lower() in ("true", "1", "yes")

    return {
        "app_id": app_id,
        "installation_id": installation_id,
        "private_key": resolved_key,
        "api_base_url": api_base_url,
        "llm_provider": llm_provider,
        "llm_model": llm_model,
        "llm_api_key": llm_api_key,
        "llm_enabled": llm_enabled,
    }
