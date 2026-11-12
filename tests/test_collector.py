"""Unit tests for Repo360Client and RepoInspector."""

from unittest.mock import MagicMock, patch
import pytest
import requests

from src.collector.client import Repo360Client
from src.collector.repo_inspector import RepoInspector


@pytest.fixture
def mock_client() -> Repo360Client:
    auth = MagicMock()
    auth.api_base_url = "https://api.github.com"
    auth.get_auth_headers.return_value = {"Authorization": "Bearer mock_token"}
    return Repo360Client(auth=auth, timeout_seconds=10)


def test_client_get_success(mock_client: Repo360Client) -> None:
    """Test successful GET request."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"x-ratelimit-remaining": "4990"}
    mock_resp.json.return_value = {"id": 1234, "name": "sample-repo"}

    with patch.object(mock_client.session, "get", return_value=mock_resp):
        res = mock_client.get("/repos/org/sample-repo")
        assert res["name"] == "sample-repo"


def test_client_get_404_allowed(mock_client: Repo360Client) -> None:
    """Test 404 handling when allow_404=True."""
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.headers = {}

    with patch.object(mock_client.session, "get", return_value=mock_resp):
        res = mock_client.get("/repos/org/non-existent", allow_404=True)
        assert res is None


@patch("time.sleep")
def test_client_secondary_rate_limit_retry(mock_sleep: MagicMock, mock_client: Repo360Client) -> None:
    """Verify that secondary rate limits (403/429) automatically back off and retry."""
    resp_rate_limited = MagicMock()
    resp_rate_limited.status_code = 403
    resp_rate_limited.text = "You have exceeded a secondary rate limit. Please wait."
    resp_rate_limited.headers = {"retry-after": "1"}

    resp_success = MagicMock()
    resp_success.status_code = 200
    resp_success.headers = {"x-ratelimit-remaining": "4500"}
    resp_success.json.return_value = {"id": 999, "name": "recovered-repo"}

    with patch.object(mock_client.session, "get", side_effect=[resp_rate_limited, resp_success]):
        res = mock_client.get("/repos/org/recovered-repo")
        assert res["name"] == "recovered-repo"
        assert mock_sleep.called



def test_client_pagination(mock_client: Repo360Client) -> None:
    """Test pagination helper aggregates multi-page results."""
    page1 = [{"id": 1}, {"id": 2}]
    page2 = [{"id": 3}]

    with patch.object(mock_client, "get", side_effect=[page1, page2]):
        items = mock_client.list_paginated("/orgs/test/repos", per_page=2, max_pages=2)
        assert len(items) == 3


def test_repo_inspector_file_content(mock_client: Repo360Client) -> None:
    """Test base64 file content retrieval in RepoInspector."""
    inspector = RepoInspector(client=mock_client)

    # Base64 string for "hello world"
    mock_data = {
        "content": "aGVsbG8gd29ybGQ=",
        "encoding": "base64",
    }
    with patch.object(mock_client, "get", return_value=mock_data):
        content = inspector.get_file_content("org", "repo", "README.md")
        assert content == "hello world"


def test_repo_inspector_file_exists(mock_client: Repo360Client) -> None:
    """Test check_file_exists returns True when endpoint returns data."""
    inspector = RepoInspector(client=mock_client)

    with patch.object(mock_client, "get", return_value={"type": "file"}):
        assert inspector.check_file_exists("org", "repo", ".devcontainer.json") is True

    with patch.object(mock_client, "get", return_value=None):
        assert inspector.check_file_exists("org", "repo", ".missing") is False


def test_list_installation_repositories(mock_client: Repo360Client) -> None:
    """Test auto-discovery of repositories via App installation."""
    inspector = RepoInspector(client=mock_client)
    mock_payload = {
        "total_count": 2,
        "repositories": [
            {"name": "service-a", "owner": {"login": "fintech-core"}, "default_branch": "main"},
            {"name": "service-b", "owner": {"login": "fintech-core"}, "default_branch": "develop"},
        ],
    }

    with patch.object(mock_client, "get", return_value=mock_payload):
        repos = inspector.list_installation_repositories()
        assert len(repos) == 2
        assert repos[0]["name"] == "service-a"
        assert repos[1]["default_branch"] == "develop"


def test_detect_tech_stack_optimized_root_listing(mock_client: Repo360Client) -> None:
    """Test detect_tech_stack identifies tech stack from single root contents call."""
    inspector = RepoInspector(client=mock_client)

    def mock_get(path, *args, **kwargs):
        if path == "/repos/org/repo":
            return {"language": "Python"}
        if path == "/repos/org/repo/contents":
            return [
                {"name": "requirements.txt", "type": "file"},
                {"name": "Dockerfile", "type": "file"},
                {"name": "README.md", "type": "file"},
            ]
        if path == "/repos/org/repo/contents/requirements.txt":
            import base64
            return {"content": base64.b64encode(b"fastapi==0.110.0\nuvicorn\n").decode("utf-8"), "encoding": "base64"}
        return None

    with patch.object(mock_client, "get", side_effect=mock_get):
        stack = inspector.detect_tech_stack("org", "repo")
        assert "Python" in stack["languages"]
        assert "Docker" in stack["languages"]
        assert "requirements.txt" in stack["manifests"]
        assert "Dockerfile" in stack["manifests"]
        assert "Fastapi" in stack["frameworks"]


def test_extract_org_from_path(mock_client: Repo360Client) -> None:
    """Verify endpoint path parsing extracts organization login."""
    assert mock_client._extract_org_from_path("/repos/fintech-core/payment-gateway") == "fintech-core"
    assert mock_client._extract_org_from_path("/orgs/fintech-core/rulesets") == "fintech-core"
    assert mock_client._extract_org_from_path("https://api.github.com/repos/payments/billing/contents") == "payments"
    assert mock_client._extract_org_from_path("/installation/repositories") is None
    assert mock_client._extract_org_from_path("/user/repos") is None


def test_client_routes_org_to_auth(mock_client: Repo360Client) -> None:
    """Verify that Repo360Client automatically extracts org and passes to auth.get_auth_headers."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"x-ratelimit-remaining": "4900"}
    mock_resp.json.return_value = {"id": 100}

    with patch.object(mock_client.session, "get", return_value=mock_resp):
        mock_client.get("/repos/target-org/target-repo")
        mock_client.auth.get_auth_headers.assert_called_with(
            installation_id=None,
            org="target-org",
        )


def test_list_installation_repositories_with_explicit_installation_id(mock_client: Repo360Client) -> None:
    """Verify list_installation_repositories passes installation_id down to client."""
    inspector = RepoInspector(client=mock_client)
    mock_payload = {
        "total_count": 1,
        "repositories": [{"name": "repo-1", "owner": {"login": "org-1"}, "default_branch": "main"}],
    }

    with patch.object(mock_client, "get", return_value=mock_payload) as mock_get:
        inspector.list_installation_repositories(installation_id=999888)
        assert mock_get.called
        assert mock_get.call_args[1]["installation_id"] == 999888


def test_client_get_409_empty_repo_allowed(mock_client: Repo360Client) -> None:
    """Test that HTTP 409 Conflict (empty repo) returns None when allow_404=True without retrying."""
    mock_resp = MagicMock()
    mock_resp.status_code = 409
    mock_resp.text = '{"message": "Git Repository is empty."}'
    mock_resp.headers = {}

    with patch.object(mock_client.session, "get", return_value=mock_resp):
        res = mock_client.get("/repos/org/empty-repo/contents", allow_404=True)
        assert res is None


def test_repo_inspector_tree_cache_missing_file_zero_http_calls(mock_client: Repo360Client) -> None:
    """Verify that tree prefetching prevents unnecessary HTTP round-trips for non-existent files."""
    inspector = RepoInspector(client=mock_client)

    tree_payload = {
        "tree": [
            {"path": "README.md", "type": "blob"},
            {"path": ".github/workflows/build.yml", "type": "blob"},
            {"path": ".github/copilot-instructions.md", "type": "blob"},
        ]
    }

    with patch.object(mock_client, "get", return_value=tree_payload):
        tree = inspector.prefetch_repo_tree("org", "repo", "main")
        assert "README.md" in tree
        assert ".github/copilot-instructions.md" in tree

    with patch.object(mock_client, "get") as mock_get:
        assert inspector.check_file_exists("org", "repo", ".github/copilot-instructions.md") is True
        assert inspector.check_file_exists("org", "repo", "CODEOWNERS") is False
        assert inspector.check_file_exists("org", "repo", ".devcontainer/devcontainer.json") is False
        assert inspector.get_file_content("org", "repo", ".github/dependabot.yml") is None
        assert mock_get.call_count == 0

    with patch.object(mock_client, "get") as mock_get:
        wfs = inspector.list_workflows("org", "repo", "main")
        assert len(wfs) == 1
        assert wfs[0]["name"] == "build.yml"
        assert mock_get.call_count == 0


