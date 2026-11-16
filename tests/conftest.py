"""Pytest fixtures and mock generation helpers."""

from __future__ import annotations

from typing import List
import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from repo360.checks.base import CheckResult, CheckSeverity, CheckStatus, Persona
from repo360.validator import RepoTarget
from repo360.mock_generator import create_mock_results


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
