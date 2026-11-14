"""Tests for dynamic repository and organization discovery engine."""

from unittest.mock import MagicMock
from src.common.inventory import (
    discover_app_organizations,
    discover_app_repositories,
    resolve_scan_targets,
)


def test_discover_app_organizations():
    mock_auth = MagicMock()
    mock_auth.discover_installations.return_value = [
        {"id": 101, "account": {"login": "TestOrg-10"}},
        {"id": 102, "account": {"login": "DefaultOrg24"}},
    ]

    orgs = discover_app_organizations(auth=mock_auth)
    assert sorted(orgs, key=str.lower) == ["DefaultOrg24", "TestOrg-10"]

    filtered = discover_app_organizations(auth=mock_auth, org_filter=["TestOrg-10"])
    assert filtered == ["TestOrg-10"]

    none_found = discover_app_organizations(auth=mock_auth, org_filter=["non-existent"])
    assert none_found == []


def test_discover_app_organizations_empty():
    mock_auth = MagicMock()
    mock_auth.discover_installations.return_value = []
    assert discover_app_organizations(auth=mock_auth) == []


def test_discover_app_repositories_direct():
    mock_auth = MagicMock()
    mock_auth.discover_installations.return_value = [
        {"id": 101, "account": {"login": "fintech-core"}},
        {"id": 102, "account": {"login": "data-platform"}},
    ]

    mock_client = MagicMock()
    mock_client.paginate_enveloped.side_effect = lambda org, endpoint, key: [
        {"name": f"{org}-repo-1", "archived": False, "fork": False},
        {"name": f"{org}-repo-2", "archived": True, "fork": False},
    ]

    targets, meta = discover_app_repositories(
        auth=mock_auth,
        client=mock_client,
        include_archived=False,
    )
    assert len(targets) == 2
    assert ("fintech-core", "fintech-core-repo-1") in targets
    assert ("data-platform", "data-platform-repo-1") in targets

    targets_filtered, _ = discover_app_repositories(
        auth=mock_auth,
        client=mock_client,
        org_filter=["fintech-core"],
        include_archived=True,
    )
    assert len(targets_filtered) == 2
    assert ("fintech-core", "fintech-core-repo-1") in targets_filtered
    assert ("fintech-core", "fintech-core-repo-2") in targets_filtered


def test_resolve_scan_targets_delegates():
    mock_auth = MagicMock()
    mock_auth.discover_installations.return_value = [
        {"id": 101, "account": {"login": "my-org"}},
    ]
    mock_client = MagicMock()
    mock_client.paginate_enveloped.return_value = [
        {"name": "service-a", "archived": False, "fork": False},
    ]

    targets, meta = resolve_scan_targets(
        auth=mock_auth,
        client=mock_client,
        org_filter=["my-org"],
    )
    assert len(targets) == 1
    assert targets[0] == ("my-org", "service-a")
