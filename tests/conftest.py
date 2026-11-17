"""Pytest fixtures and mock generation helpers."""

from __future__ import annotations

import os
from typing import Iterator, List
import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from repo360.auth.github_app_auth import GitHubAppAuth
from repo360.validator import RepoTarget

_CREDENTIAL_ENV_PREFIXES = ("REPO360_", "AI_", "LLM_", "GEMINI_", "OPENAI_", "ANTHROPIC_")


@pytest.fixture(autouse=True)
def _hermetic_environment(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> Iterator[None]:
    """Keep tests offline and isolated from the developer's real credentials and config."""
    for key in list(os.environ):
        if key.startswith(_CREDENTIAL_ENV_PREFIXES) and key != "REPO360_TIMEZONE":
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("REPO360_CONFIG_DIR", str(tmp_path_factory.mktemp("repo360-config")))
    if "real_clock_skew" not in request.keywords:
        monkeypatch.setattr(GitHubAppAuth, "_calculate_clock_skew", lambda self: 0.0)
    yield


@pytest.fixture(scope="session")
def test_rsa_key_pem() -> str:
    """Generate an ephemeral 2048-bit RSA private key for testing."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("utf-8")


@pytest.fixture
def sample_targets() -> List[RepoTarget]:
    """Sample repository targets."""
    return [
        RepoTarget(
            org_name="fintech-core",
            repo_name="payment-gateway",
            target_personas={"all"},
            default_branch="main",
            owner_team="@fintech-core/payments",
        ),
        RepoTarget(
            org_name="fintech-core",
            repo_name="auth-service",
            target_personas={"org_admin", "repo_admin"},
            default_branch="main",
            owner_team="@fintech-core/security",
        ),
    ]
