"""Unit tests for GitHub App authentication and token management."""

import time
from unittest.mock import MagicMock, patch
import jwt
import pytest

from src.auth.github_app_auth import GitHubAppAuth


def test_auth_init_valid(test_rsa_key_pem: str) -> None:
    """Test successful initialization with valid PEM string."""
    auth = GitHubAppAuth(
        app_id=123456,
        installation_id=789012,
        private_key=test_rsa_key_pem,
        api_base_url="https://api.github.com",
    )
    assert auth.app_id == 123456
    assert auth.installation_id == 789012
    assert "credentials redacted" in repr(auth)


def test_auth_init_invalid_key() -> None:
    """Test error handling when private key format is malformed."""
    with pytest.raises(ValueError, match="Invalid private key format"):
        GitHubAppAuth(
            app_id=123,
            installation_id=456,
            private_key="INVALID_KEY_DATA",
        )


def test_jwt_generation(test_rsa_key_pem: str) -> None:
    """Verify that generated JWT has correct claims and can be decoded."""
    auth = GitHubAppAuth(
        app_id=99999,
        installation_id=11111,
        private_key=test_rsa_key_pem,
    )
    token = auth.generate_jwt(expiration_seconds=300)
    assert isinstance(token, str)

    decoded = jwt.decode(token, options={"verify_signature": False})
    assert decoded["iss"] == "99999"
    assert decoded["exp"] > time.time()


@patch("requests.post")
def test_token_caching(mock_post: MagicMock, test_rsa_key_pem: str) -> None:
    """Ensure installation token is cached and reused until near expiry."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "token": "ghs_mock_token_1234567890",
        "expires_at": "2026-10-29T23:59:59Z",
    }
    mock_post.return_value = mock_response

    auth = GitHubAppAuth(
        app_id=123,
        installation_id=456,
        private_key=test_rsa_key_pem,
    )

    token1 = auth.get_installation_token()
    assert token1 == "ghs_mock_token_1234567890"
    assert mock_post.call_count == 1

    token2 = auth.get_installation_token()
    assert token2 == "ghs_mock_token_1234567890"
    assert mock_post.call_count == 1

    headers = auth.get_auth_headers()
    assert headers["Authorization"] == "Bearer ghs_mock_token_1234567890"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"


@patch("requests.get")
def test_discover_installations(mock_get: MagicMock, test_rsa_key_pem: str) -> None:
    """Verify listing installations via GitHub App JWT."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = [
        {
            "id": 987654,
            "account": {"login": "enterprise-org", "type": "Organization"},
            "repository_selection": "all",
        }
    ]
    mock_get.return_value = mock_response

    auth = GitHubAppAuth(
        app_id=123,
        private_key=test_rsa_key_pem,
    )
    assert auth.installation_id is None

    installations = auth.discover_installations()
    assert len(installations) == 1
    assert installations[0]["id"] == 987654
    assert installations[0]["account"]["login"] == "enterprise-org"
    assert mock_get.called


@patch("requests.get")
@patch("requests.post")
def test_auto_discover_during_token_exchange(mock_post: MagicMock, mock_get: MagicMock, test_rsa_key_pem: str) -> None:
    """Verify that omitting installation_id auto-discovers and exchanges token."""
    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.json.return_value = [
        {"id": 555666, "account": {"login": "auto-org"}, "repository_selection": "all"}
    ]
    mock_get.return_value = mock_get_resp

    mock_post_resp = MagicMock()
    mock_post_resp.status_code = 200
    mock_post_resp.json.return_value = {"token": "ghs_auto_discovered_token"}
    mock_post.return_value = mock_post_resp

    auth = GitHubAppAuth(app_id=123, private_key=test_rsa_key_pem)
    token = auth.get_installation_token()
    assert token == "ghs_auto_discovered_token"
    assert auth.installation_id == 555666


@patch("requests.get")
@patch("requests.post")
def test_multi_org_token_resolution_and_caching(
    mock_post: MagicMock,
    mock_get: MagicMock,
    test_rsa_key_pem: str,
) -> None:
    """Verify dynamic token resolution and distinct caching across multiple organizations."""
    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.json.return_value = [
        {"id": 1001, "account": {"login": "DefaultOrg24"}, "repository_selection": "all"},
        {"id": 1002, "account": {"login": "PaymentsCore"}, "repository_selection": "all"},
    ]
    mock_get.return_value = mock_get_resp

    def mock_post_side_effect(url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "1001" in url:
            resp.json.return_value = {"token": "ghs_token_defaultorg24"}
        elif "1002" in url:
            resp.json.return_value = {"token": "ghs_token_paymentscore"}
        else:
            resp.status_code = 404
        return resp

    mock_post.side_effect = mock_post_side_effect

    auth = GitHubAppAuth(app_id=123, private_key=test_rsa_key_pem)

    token_org1 = auth.get_installation_token(org="DefaultOrg24")
    assert token_org1 == "ghs_token_defaultorg24"
    assert mock_post.call_count == 1

    token_org2 = auth.get_installation_token(org="paymentscore")
    assert token_org2 == "ghs_token_paymentscore"
    assert mock_post.call_count == 2

    cached_org1 = auth.get_installation_token(org="defaultorg24")
    assert cached_org1 == "ghs_token_defaultorg24"
    assert mock_post.call_count == 2

    headers_org2 = auth.get_auth_headers(org="PaymentsCore")
    assert headers_org2["Authorization"] == "Bearer ghs_token_paymentscore"
    assert mock_post.call_count == 2


@patch("requests.get")
def test_multi_org_uninstalled_org_raises_error(
    mock_get: MagicMock,
    test_rsa_key_pem: str,
) -> None:
    """Verify error raised when requesting token for uninstalled organization."""
    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.json.return_value = [
        {"id": 1001, "account": {"login": "DefaultOrg24"}},
    ]
    mock_get.return_value = mock_get_resp

    auth = GitHubAppAuth(app_id=123, private_key=test_rsa_key_pem)
    with pytest.raises(RuntimeError, match="No GitHub App installation found for organization 'NonExistentOrg'"):
        auth.get_installation_token(org="NonExistentOrg")


@patch("requests.get")
def test_get_installation_permissions(mock_get: MagicMock, test_rsa_key_pem: str) -> None:
    """Verify retrieval of installation permissions dictionary."""
    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.json.return_value = [
        {
            "id": 1001,
            "account": {"login": "DefaultOrg24"},
            "permissions": {"pull_requests": "read", "issues": "read", "contents": "read"},
        },
    ]
    mock_get.return_value = mock_get_resp

    auth = GitHubAppAuth(app_id=123, private_key=test_rsa_key_pem)
    perms = auth.get_installation_permissions("DefaultOrg24")
    assert perms.get("pull_requests") == "read"
    assert perms.get("issues") == "read"
    assert auth.get_installation_permissions("UnknownOrg") == {}


@patch("src.auth.github_app_auth.time.sleep")
@patch("requests.get")
def test_discover_installations_recovers_on_transient_connection_error(
    mock_get: MagicMock,
    mock_sleep: MagicMock,
    test_rsa_key_pem: str,
) -> None:
    """Verify that discover_installations automatically retries on RemoteDisconnected / ConnectionError."""
    import requests.exceptions

    mock_success = MagicMock()
    mock_success.status_code = 200
    mock_success.json.return_value = [
        {"id": 1001, "account": {"login": "DefaultOrg24"}},
    ]
    mock_get.side_effect = [
        requests.exceptions.ConnectionError("('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))"),
        mock_success,
    ]

    auth = GitHubAppAuth(app_id=123, private_key=test_rsa_key_pem)
    installations = auth.discover_installations(force_refresh=True)

    assert len(installations) == 1
    assert installations[0]["account"]["login"] == "DefaultOrg24"
    assert mock_get.call_count == 2
    assert mock_sleep.call_count == 1


