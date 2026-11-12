"""Tests for flexible repository inventory parser."""

import os
import pytest
from src.common.inventory import RepoEntry, load_flexible_repo_entries


def test_load_flexible_inventory_standard(tmp_path):
    csv_file = tmp_path / "repos.csv"
    csv_file.write_text("org,repo,enabled\nmy-org,repo-a,true\nmy-org,repo-b,false\n", encoding="utf-8")

    entries = load_flexible_repo_entries(csv_file)
    assert len(entries) == 2
    assert entries[0] == RepoEntry("my-org", "repo-a", True)
    assert entries[1] == RepoEntry("my-org", "repo-b", False)


def test_load_flexible_inventory_aliases(tmp_path):
    csv_file = tmp_path / "repos.csv"
    csv_file.write_text("org_name,repo_name\nplatform-team,infra-service\n", encoding="utf-8")

    entries = load_flexible_repo_entries(csv_file)
    assert len(entries) == 1
    assert entries[0] == RepoEntry("platform-team", "infra-service", True)


def test_load_flexible_inventory_wildcard(tmp_path):
    csv_file = tmp_path / "repos.csv"
    csv_file.write_text("# Comment line\norganization,repository,active\n# Another comment\nenterprise,*,yes\n", encoding="utf-8")

    entries = load_flexible_repo_entries(csv_file)
    assert len(entries) == 1
    assert entries[0].repo == "*"
    assert entries[0].enabled is True


def test_load_flexible_inventory_missing_file():
    with pytest.raises(FileNotFoundError):
        load_flexible_repo_entries("non_existent_inventory.csv")


def test_load_flexible_inventory_invalid_org(tmp_path):
    csv_file = tmp_path / "repos.csv"
    csv_file.write_text("org,repo\n-invalid-org-,repo1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="validation failed"):
        load_flexible_repo_entries(csv_file)


def test_load_flexible_inventory_no_enabled(tmp_path):
    csv_file = tmp_path / "repos.csv"
    csv_file.write_text("org,repo,enabled\nmy-org,repo1,false\n", encoding="utf-8")

    with pytest.raises(ValueError, match="contains no enabled repositories"):
        load_flexible_repo_entries(csv_file)


def test_discover_app_repositories_direct():
    from unittest.mock import MagicMock
    from src.common.inventory import discover_app_repositories

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


def test_discover_app_organizations():
    from unittest.mock import MagicMock
    from src.common.inventory import discover_app_organizations

    mock_auth = MagicMock()
    mock_auth.discover_installations.return_value = [
        {"id": 101, "account": {"login": "TestOrg-10"}},
        {"id": 102, "account": {"login": "DefaultOrg24"}},
    ]

    orgs = discover_app_organizations(auth=mock_auth)
    assert orgs == ["defaultorg24", "testorg-10"] or orgs == ["DefaultOrg24", "TestOrg-10"]

    filtered = discover_app_organizations(auth=mock_auth, org_filter=["TestOrg-10"])
    assert filtered == ["TestOrg-10"]

    none_found = discover_app_organizations(auth=mock_auth, org_filter=["non-existent"])
    assert none_found == []
