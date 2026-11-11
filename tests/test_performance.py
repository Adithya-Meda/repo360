"""Unit tests validating concurrency, adaptive throttling, GraphQL batching, and pruning optimizations."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.activity.collector import fetch_team_access
from src.bypass.collector import collect_bypass_data, profile_repo_bypasses
from src.bypass.models import BypassConfig
from src.collector.client import Repo360Client
from src.collector.repo_inspector import RepoInspector
from src.common.client import ResilientGitHubClient
from src.common.concurrency import (
    is_dormant_repo,
    is_empty_repo,
    map_concurrent,
    resolve_worker_count,
)
from src.common.inventory import discover_app_repositories
from src.dora.collector import build_dora_rows, collect_repo_deployments, profile_repo_dora
from src.dora.models import DeploymentRecord, RepoDORAMetrics
from src.finops.collector import (
    build_finops_rows,
    collect_org_packages,
    profile_single_repo,
)
from src.finops.models import ArtifactRecord, RepoStorageProfile


def test_resolve_worker_count_scales_by_volume():
    assert resolve_worker_count(0, 1) == 1
    assert resolve_worker_count(0, 10) == 10
    assert resolve_worker_count(0, 30) == 16
    assert resolve_worker_count(0, 100) == 24
    assert resolve_worker_count(0, 500) == 32
    assert resolve_worker_count(5, 500) == 5
    assert resolve_worker_count(64, 20) == 20


def test_map_concurrent_preserves_order():
    items = list(range(25))
    results = map_concurrent(items, lambda x: x * x, max_workers=8)
    assert results == [x * x for x in items]


def test_empty_and_dormant_repo_guards():
    since = datetime(2026, 10, 1, tzinfo=timezone.utc)
    parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))

    assert is_empty_repo({"size": 0}) is True
    assert is_empty_repo({"size": 120}) is False
    assert is_empty_repo({}) is False
    assert is_empty_repo(None) is False

    # Dormant when both pushed_at and updated_at precede lookback window
    assert is_dormant_repo(
        {"size": 100, "pushed_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-15T00:00:00Z"},
        since,
        parse,
    ) is True

    # Not dormant if updated_at is inside window (e.g. PR/issue/release update)
    assert is_dormant_repo(
        {"size": 100, "pushed_at": "2026-09-01T00:00:00Z", "updated_at": "2026-10-15T00:00:00Z"},
        since,
        parse,
    ) is False


def test_resilient_client_wire_speed_when_healthy():
    auth = MagicMock()
    auth.api_base_url = "https://api.github.com"
    client = ResilientGitHubClient(auth=auth)
    assert client.inter_request_delay_seconds == 0.0

    # Healthy quota org vs low-quota org
    client._org_remaining["healthy-org"] = 4800
    client._org_remaining["low-org"] = 200

    with patch("time.sleep") as mock_sleep:
        client._coordinate_rate_limit("healthy-org")
        client._coordinate_rate_limit("healthy-org")
        assert mock_sleep.call_count == 0

        # First call reserves the slot; second consecutive call triggers adaptive pacing.
        client._coordinate_rate_limit("low-org")
        client._coordinate_rate_limit("low-org")
        assert mock_sleep.call_count == 1
        assert mock_sleep.call_args[0][0] > 0.0


def test_repo_inspector_empty_repo_skips_http_and_multi_page_parallel():
    mock_auth = MagicMock()
    mock_auth.api_base_url = "https://api.github.com"
    client = Repo360Client(auth=mock_auth)
    inspector = RepoInspector(client=client)

    inspector.seed_repo_metadata("org", "empty-repo", {"size": 0, "default_branch": "main"})
    with patch.object(client, "get") as mock_get:
        tree = inspector.prefetch_repo_tree("org", "empty-repo", "main")
        prot = inspector.get_branch_protection("org", "empty-repo", "main")
        assert tree == set()
        assert prot is None
        assert mock_get.call_count == 0

    # Multi-page installation listing (total_count=3, per_page=2 -> page 1 + page 2)
    page1 = {"total_count": 3, "repositories": [{"name": "r1"}, {"name": "r2"}]}
    page2 = {"total_count": 3, "repositories": [{"name": "r3"}]}
    with patch.object(client, "get", side_effect=[page1, page2]) as mock_get:
        repos = inspector.list_installation_repositories(per_page=2, max_pages=5)
        assert [r["name"] for r in repos] == ["r1", "r2", "r3"]
        assert mock_get.call_count == 2


def test_dora_graphql_eliminates_n_plus_one_and_skips_dormant():
    mock_client = MagicMock()
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    since = now - timedelta(days=30)

    mock_client.graphql.return_value = {
        "data": {
            "repository": {
                "deployments": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "databaseId": 101,
                            "createdAt": "2026-10-15T12:00:00Z",
                            "environment": "production",
                            "ref": {"name": "main"},
                            "latestStatus": {"state": "SUCCESS"},
                        },
                        {
                            "databaseId": 102,
                            "createdAt": "2026-10-16T12:00:00Z",
                            "environment": "staging",
                            "ref": {"name": "main"},
                            "latestStatus": {"state": "FAILURE"},
                        },
                    ],
                }
            }
        }
    }

    records = collect_repo_deployments(mock_client, "org", "app", since, now)
    assert len(records) == 2
    assert records[0].status == "SUCCESS"
    assert records[1].status == "FAILURE"
    assert mock_client.graphql.call_count == 1
    assert mock_client.get.call_count == 0
    assert mock_client.paginate.call_count == 0

    # Dormant repo skips all API calls
    dormant_client = MagicMock()
    m = profile_repo_dora(
        dormant_client,
        "org",
        "dormant-repo",
        since,
        now,
        {"size": 10, "pushed_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z"},
    )
    assert m.total_deploys == 0
    assert dormant_client.graphql.call_count == 0
    assert dormant_client.paginate.call_count == 0


def test_finops_skips_empty_repo_and_collects_packages():
    mock_client = MagicMock()
    prof = profile_single_repo(mock_client, "org", "empty-repo", {"size": 0, "private": True})
    assert prof.artifacts == []
    assert prof.caches == []
    assert mock_client.paginate_enveloped.call_count == 0

    mock_client.graphql.return_value = {
        "data": {
            "organization": {
                "packages": {
                    "nodes": [
                        {
                            "name": "svc-image",
                            "repository": {"name": "service-a"},
                            "latestVersion": {"files": {"nodes": [{"size": 2048}]}},
                        }
                    ]
                }
            }
        }
    }
    mock_client.paginate.side_effect = (
        lambda org, endpoint, params: [{"id": 1, "name": "svc-image", "package_type": "container", "version_count": 2}]
        if params.get("package_type") == "container"
        else []
    )

    pkgs = collect_org_packages(mock_client, "org")
    assert len(pkgs) == 1
    assert pkgs[0].size_in_bytes == 2048
    assert pkgs[0].repo_name == "service-a"


def test_bypass_permission_and_batch_ai_optimization():
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=30)

    mock_auth = MagicMock()
    mock_auth.get_installation_permissions.return_value = {"contents": "read"}  # lacks secret_scanning_alerts
    mock_client = MagicMock()
    mock_client.auth = mock_auth
    mock_client.paginate.return_value = []

    mock_ai = MagicMock()
    mock_ai.is_available.return_value = True

    profiles = collect_bypass_data(
        client=mock_client,
        orgs=["org1"],
        targets=[("org1", "empty-repo"), ("org1", "active-repo")],
        since=since,
        cfg=BypassConfig(max_workers=2),
        metadata_cache={
            ("org1", "empty-repo"): {"size": 0},
            ("org1", "active-repo"): {
                "size": 500,
                "pushed_at": now.isoformat(),
                "updated_at": now.isoformat(),
            },
        },
        ai_engine=mock_ai,
    )

    assert len(profiles) == 2
    # Empty repo skipped completely; active repo only checked rulesets (secret_scanning_alerts skipped due to missing perm)
    called_endpoints = [call.args[1] for call in mock_client.paginate.call_args_list]
    assert all("/secret-scanning/alerts" not in ep for ep in called_endpoints)
    assert any("/repos/org1/active-repo/rulesets/rule-suites" in ep for ep in called_endpoints)


def test_activity_fetch_team_access_concurrent_multi_org():
    mock_client = MagicMock()

    def fake_paginate(org, path, params=None):
        if path == f"/orgs/{org}/teams":
            return [{"slug": "platform"}, {"slug": "backend"}]
        if path == f"/orgs/{org}/teams/platform/members":
            return [{"login": "alice", "type": "User"}]
        if path == f"/orgs/{org}/teams/backend/members":
            return [{"login": "bob", "type": "User"}, {"login": "bot[bot]", "type": "Bot"}]
        return []

    mock_client.paginate.side_effect = fake_paginate
    teams_by_user, status = fetch_team_access(mock_client, ["org-a", "org-b"], exclude_bots=True)

    assert status == {"org-a": True, "org-b": True}
    assert teams_by_user[("org-a", "alice")] == {"platform"}
    assert teams_by_user[("org-a", "bob")] == {"backend"}
    assert ("org-a", "bot[bot]") not in teams_by_user
    assert teams_by_user[("org-b", "alice")] == {"platform"}


def test_discover_app_repositories_concurrent_multi_org():
    mock_auth = MagicMock()
    mock_auth.discover_installations.return_value = [
        {"id": 1, "account": {"login": "org-1"}},
        {"id": 2, "account": {"login": "org-2"}},
        {"id": 3, "account": {"login": "org-3"}},
    ]
    mock_client = MagicMock()
    mock_client.paginate_enveloped.side_effect = lambda org, ep, key: [
        {"name": f"{org}-svc", "archived": False, "fork": False}
    ]

    targets, cache = discover_app_repositories(mock_auth, mock_client)
    assert targets == [("org-1", "org-1-svc"), ("org-2", "org-2-svc"), ("org-3", "org-3-svc")]
    assert ("org-3", "org-3-svc") in cache


def test_repo_inspector_prefetch_audit_context():
    import base64

    mock_auth = MagicMock()
    mock_auth.api_base_url = "https://api.github.com"
    client = Repo360Client(auth=mock_auth)
    inspector = RepoInspector(client=client)

    inspector._repo_tree_cache[("org", "repo", "main")] = {
        ".github/CODEOWNERS",
        ".github/workflows/ci.yml",
        ".github/workflows/deploy.yml",
        "requirements.txt",
    }

    def fake_get(path, params=None, allow_404=False, **kwargs):
        if path.endswith("/rulesets"):
            return [{"name": "default", "enforcement": "active"}]
        if path.endswith("/protection"):
            return {"required_pull_request_reviews": {"required_approving_review_count": 2}}
        if "/contents/" in path:
            return {"content": base64.b64encode(b"* @org/team\npytest\n").decode("utf-8"), "encoding": "base64"}
        return None

    with patch.object(client, "get", side_effect=fake_get) as mock_get:
        inspector.prefetch_repo_audit_context("org", "repo", "main")
        first_calls = mock_get.call_count
        assert first_calls == 6  # CODEOWNERS + requirements.txt + 2 workflows + rulesets + protection

        # Subsequent reads hit warm in-memory caches (0 extra HTTP calls)
        assert inspector.get_file_content("org", "repo", ".github/CODEOWNERS", "main") is not None
        assert inspector.get_file_content("org", "repo", ".github/workflows/ci.yml", "main") is not None
        assert inspector.get_repo_rulesets("org", "repo") == [{"name": "default", "enforcement": "active"}]
        assert inspector.get_branch_protection("org", "repo", "main") is not None
        assert mock_get.call_count == first_calls


def test_build_finops_and_dora_concurrent_ai():
    now = datetime.now(timezone.utc)
    mock_ai = MagicMock()
    mock_ai.is_available.return_value = True
    mock_ai.generate_finops_advisory.side_effect = lambda **kw: f"AI FinOps for {kw['repo']}"
    mock_ai.diagnose_dora_flow.side_effect = lambda **kw: f"AI DORA for {kw['repo']}"

    profiles = []
    for name in ("repo-a", "repo-b", "repo-c"):
        p = RepoStorageProfile(org="org", repo=name, visibility="private", disk_size_kb=5000000)
        p.artifacts.append(
            ArtifactRecord(id=1, name="art", size_in_bytes=1024, created_at=now, expires_at=now + timedelta(days=90))
        )
        profiles.append(p)

    repo_rows, _, _, _ = build_finops_rows(profiles, {"org": []}, ["org"], ai_engine=mock_ai)
    assert [r[12] for r in repo_rows] == ["AI FinOps for org/repo-a", "AI FinOps for org/repo-b", "AI FinOps for org/repo-c"]

    dora_metrics = []
    for name in ("svc-1", "svc-2"):
        m = RepoDORAMetrics(org="org", repo=name)
        m.deployments.append(
            DeploymentRecord(event_type="Deployment", event_id="1", name="Deploy", environment="prod", status="SUCCESS", ref="main", created_at=now)
        )
        dora_metrics.append(m)

    dora_rows, _, _, _ = build_dora_rows(dora_metrics, ["org"], window_days=90, ai_engine=mock_ai)
    assert [r[14] for r in dora_rows] == ["AI DORA for org/svc-1", "AI DORA for org/svc-2"]
