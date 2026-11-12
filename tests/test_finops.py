"""Unit and integration tests for Repo360 FinOps storage & artifact profiling."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
import requests
from openpyxl import load_workbook

from src.finops.models import (
    ArtifactRecord,
    CacheRecord,
    FinOpsConfig,
    PackageRecord,
    RepoStorageProfile,
    format_bytes_to_gb,
    format_bytes_to_mb,
)
from src.finops.collector import (
    build_finops_rows,
    collect_org_packages,
    collect_repo_artifacts,
    collect_repo_caches,
    profile_single_repo,
)
from src.finops.reporter import write_finops_workbook


def test_byte_formatters():
    assert format_bytes_to_mb(1024 * 1024 * 50) == 50.0
    assert format_bytes_to_gb(1024 * 1024 * 1024 * 2) == 2.0


def test_artifact_record_properties():
    now = datetime.now(timezone.utc)
    art_default = ArtifactRecord(
        id=1,
        name="build-artifacts",
        size_in_bytes=104857600,
        created_at=now,
        expires_at=now + timedelta(days=90),
        expired=False,
        workflow_run_id=555,
    )
    assert art_default.size_mb == 100.0
    assert art_default.retention_days == 90
    assert art_default.is_default_90_day is True

    art_custom = ArtifactRecord(
        id=2,
        name="test-reports",
        size_in_bytes=10485760,
        created_at=now,
        expires_at=now + timedelta(days=7),
        expired=False,
    )
    assert art_custom.size_mb == 10.0
    assert art_custom.retention_days == 7
    assert art_custom.is_default_90_day is False


def test_cache_record_stale():
    now = datetime.now(timezone.utc)
    fresh_cache = CacheRecord(
        id=10,
        key="npm-cache-v1",
        size_in_bytes=52428800,
        created_at=now - timedelta(days=5),
        last_accessed_at=now - timedelta(days=2),
    )
    assert fresh_cache.size_mb == 50.0
    assert fresh_cache.is_stale is False

    stale_cache = CacheRecord(
        id=11,
        key="pip-cache-old",
        size_in_bytes=104857600,
        created_at=now - timedelta(days=60),
        last_accessed_at=now - timedelta(days=40),
    )
    assert stale_cache.is_stale is True


def test_repo_storage_profile_optimizations():
    now = datetime.now(timezone.utc)
    profile = RepoStorageProfile(
        org="fintech-org",
        repo="payments-api",
        visibility="private",
        disk_size_kb=512000,
    )
    for i in range(12):
        profile.artifacts.append(
            ArtifactRecord(
                id=i,
                name=f"artifact-{i}",
                size_in_bytes=52428800,
                created_at=now,
                expires_at=now + timedelta(days=90),
                expired=False,
            )
        )
    profile.caches.append(
        CacheRecord(
            id=101,
            key="maven-cache",
            size_in_bytes=104857600,
            last_accessed_at=now - timedelta(days=45),
        )
    )

    assert profile.disk_size_mb == 500.0
    assert profile.active_artifact_mb == 600.0
    assert profile.default_90_day_count == 12
    assert profile.cache_mb == 100.0
    assert profile.total_storage_mb == 1200.0
    assert profile.optimization_status == "High Waste"
    assert "Reduce retention-days" in profile.recommendation
    assert "Purge 1 stale Actions caches" in profile.recommendation


def test_build_finops_rows():
    now = datetime.now(timezone.utc)
    profile = RepoStorageProfile(
        org="fintech-org",
        repo="core-service",
        visibility="internal",
        disk_size_kb=204800,
    )
    profile.artifacts.append(
        ArtifactRecord(
            id=1,
            name="app-binaries",
            size_in_bytes=52428800,
            created_at=now,
            expires_at=now + timedelta(days=90),
            expired=False,
            workflow_run_id=999,
        )
    )
    package = PackageRecord(
        id=77,
        name="docker-core",
        package_type="container",
        version_count=3,
        updated_at=now,
        repo_name="core-service",
    )

    repo_rows, org_rows, artifact_rows, cache_package_rows = build_finops_rows(
        profiles=[profile],
        org_packages={"fintech-org": [package]},
        orgs=["fintech-org"],
    )

    assert len(repo_rows) == 1
    assert repo_rows[0][0] == "fintech-org"
    assert repo_rows[0][1] == "core-service"
    assert repo_rows[0][2] == "Internal"
    assert repo_rows[0][3] == 200.0
    assert repo_rows[0][4] == 1
    assert repo_rows[0][5] == 50.0

    assert len(org_rows) == 1
    assert org_rows[0][0] == "fintech-org"
    assert org_rows[0][1] == 1
    assert org_rows[0][2] == 200.0
    assert org_rows[0][4] == 50.0

    assert len(artifact_rows) == 1
    assert artifact_rows[0][0] == "fintech-org"
    assert artifact_rows[0][3] == "app-binaries"
    assert artifact_rows[0][10] == "999"

    assert len(cache_package_rows) == 1
    assert cache_package_rows[0][2] == "Package (container)"
    assert cache_package_rows[0][3] == "docker-core"


def test_write_finops_workbook(tmp_path: Path):
    now = datetime.now(timezone.utc)
    profile = RepoStorageProfile(
        org="demo-org",
        repo="demo-repo",
        visibility="public",
        disk_size_kb=10240,
    )
    repo_rows, org_rows, artifact_rows, cache_package_rows = build_finops_rows(
        profiles=[profile],
        org_packages={"demo-org": []},
        orgs=["demo-org"],
    )

    out_file = tmp_path / "finops_test.xlsx"
    cfg = FinOpsConfig(output_dir=str(tmp_path))

    write_finops_workbook(
        path=out_file,
        cfg=cfg,
        now=now,
        repo_rows=repo_rows,
        org_rows=org_rows,
        artifact_rows=artifact_rows,
        cache_package_rows=cache_package_rows,
        failures=[],
        execution_duration=1.5,
    )

    assert out_file.exists()
    wb = load_workbook(out_file)
    sheet_names = wb.sheetnames
    assert "Executive Summary" in sheet_names
    assert "Storage by Repository" in sheet_names
    assert "Artifact Inventory" in sheet_names
    assert "Cache & Package Inventory" in sheet_names

    summary_ws = wb["Executive Summary"]
    assert "Repo360 FinOps" in summary_ws["A1"].value


def test_finops_hardened_packages_pagination_and_reconciliation(tmp_path: Path):
    """Verify multi-page multi-version packages count toward totals, org-wide packages reconcile, and 403s surface."""
    now = datetime.now(timezone.utc)
    ts_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    client = MagicMock()
    page1 = {
        "data": {
            "organization": {
                "packages": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                    "nodes": [
                        {
                            "id": "pkg-1",
                            "name": "img-a",
                            "repository": {"name": "svc-a"},
                            "versions": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {"files": {"nodes": [{"size": 10 * 1024 * 1024}]}},
                                    {"files": {"nodes": [{"size": 5 * 1024 * 1024}]}},
                                ],
                            },
                        }
                    ],
                }
            }
        }
    }
    page2 = {
        "data": {
            "organization": {
                "packages": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "id": "pkg-2",
                            "name": "org-base",
                            "repository": None,
                            "versions": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [{"files": {"nodes": [{"size": 20 * 1024 * 1024}]}}],
                            },
                        }
                    ],
                }
            }
        }
    }
    client.graphql.side_effect = [page1, page2]

    def _fake_paginate(org, endpoint, params=None):
        if params and params.get("package_type") == "container":
            return [
                {"id": 1, "name": "img-a", "package_type": "container", "version_count": 2, "created_at": ts_str, "updated_at": ts_str, "repository": {"name": "svc-a"}},
                {"id": 2, "name": "org-base", "package_type": "container", "version_count": 1, "created_at": ts_str, "updated_at": ts_str, "repository": None},
            ]
        return []

    client.paginate.side_effect = _fake_paginate
    pkgs = collect_org_packages(client, "acme")
    assert len(pkgs) == 2
    img_a = next(p for p in pkgs if p.name == "img-a")
    assert img_a.size_mb == 15.0

    err_resp = requests.Response()
    err_resp.status_code = 403
    client.paginate_enveloped.side_effect = requests.HTTPError(response=err_resp)
    errs = []
    assert collect_repo_artifacts(client, "acme", "svc-a", errors_out=errs) == []
    assert any("403 Forbidden" in e for e in errs)

    p = RepoStorageProfile(org="acme", repo="svc-a", disk_size_kb=10240, packages=[img_a], errors=errs)
    assert p.package_mb == 15.0
    assert p.total_storage_mb == 25.0

    repo_rows, org_rows, art_rows, cp_rows = build_finops_rows([p], {"acme": pkgs}, ["acme"])
    assert org_rows[0][8] == 2
    assert org_rows[0][9] == 45.0

    out_file = tmp_path / "finops_hardened.xlsx"
    write_finops_workbook(
        path=out_file,
        cfg=FinOpsConfig(output_dir=str(tmp_path)),
        now=now,
        repo_rows=repo_rows,
        org_rows=org_rows,
        artifact_rows=art_rows,
        cache_package_rows=cp_rows,
        failures=[("acme/svc-a", errs[0])],
    )
    wb = load_workbook(out_file)
    assert "Collection & Permission Gaps" in wb.sheetnames
    exec_kpis = {
        row[0].value: row[1].value
        for row in wb["Executive Summary"].iter_rows()
        if len(row) > 1 and row[0].value is not None
    }
    assert exec_kpis["Packages & Containers Tracked"] == 2
    assert exec_kpis["Package & Container Storage (MB)"] == 35.0
    assert exec_kpis["Total Storage Footprint (MB)"] == 45.0
    assert exec_kpis["Collection / Permission Gaps"] == 1


def test_finops_retention_rounding_precision_lfs_and_billing(tmp_path: Path):
    """Verify off-by-one retention rounding, sub-0.01 MB precision, package type disambiguation, LFS, and billing."""
    import base64
    from src.finops.collector import (
        build_billing_rows,
        collect_finops_data,
        collect_org_billing,
        collect_org_lfs_usage,
    )
    from src.finops.models import format_kb_to_mb

    created = datetime(2026, 11, 12, 8, 44, 11, tzinfo=timezone.utc)
    expires_60 = datetime(2027, 1, 11, 8, 44, 10, tzinfo=timezone.utc)
    art_60 = ArtifactRecord(id=1, name="audit-report", size_in_bytes=103814, created_at=created, expires_at=expires_60)
    assert art_60.retention_days == 60
    assert art_60.is_default_90_day is False

    expires_90 = datetime(2027, 2, 10, 8, 44, 10, tzinfo=timezone.utc)
    art_90 = ArtifactRecord(id=2, name="default-art", size_in_bytes=1024, created_at=created, expires_at=expires_90)
    assert art_90.retention_days == 90
    assert art_90.is_default_90_day is True

    assert format_kb_to_mb(2) == 0.002
    assert format_kb_to_mb(0) == 0.0

    # Disambiguate identical package names across packageType
    pkg_client = MagicMock()
    pkg_client.graphql.return_value = {
        "data": {
            "organization": {
                "packages": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "id": "p-npm",
                            "name": "shared-lib",
                            "packageType": "NPM",
                            "repository": {"name": "repo-a"},
                            "versions": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [{"id": "v1", "files": {"pageInfo": {"hasNextPage": False}, "nodes": [{"size": 1000}]}}],
                            },
                        },
                        {
                            "id": "p-cont",
                            "name": "shared-lib",
                            "packageType": "CONTAINER",
                            "repository": {"name": "repo-b"},
                            "versions": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [{"id": "v2", "files": {"pageInfo": {"hasNextPage": False}, "nodes": [{"size": 5000000}]}}],
                            },
                        },
                    ],
                }
            }
        }
    }

    def _pkg_pag(org, ep, params=None):
        pt = (params or {}).get("package_type")
        if pt == "npm":
            return [{"id": 10, "name": "shared-lib", "package_type": "npm", "version_count": 1}]
        if pt == "container":
            return [{"id": 11, "name": "shared-lib", "package_type": "container", "version_count": 1}]
        return []

    pkg_client.paginate.side_effect = _pkg_pag
    pkgs = collect_org_packages(pkg_client, "org")
    by_type = {p.package_type: p for p in pkgs}
    assert by_type["npm"].size_in_bytes == 1000
    assert by_type["container"].size_in_bytes == 5000000

    # Git LFS and Billing end-to-end collection
    live_client = MagicMock()
    pointer_b64 = base64.b64encode(
        b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 120338944\n"
    ).decode("ascii")

    def _gql_router(org, query, variables=None):
        if "repositories(" in query:
            return {
                "data": {
                    "organization": {
                        "repositories": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {"name": "Opsera", "object": {"text": "*.avi filter=lfs diff=lfs merge=lfs -text\n"}},
                                {"name": "Repo-Audit", "object": None},
                            ],
                        }
                    }
                }
            }
        return {"data": {"organization": {"packages": {"pageInfo": {"hasNextPage": False}, "nodes": []}}}}

    def _get_router(org, path, params=None):
        if path.endswith("/artifact-and-log-retention") and "/repos/" in path:
            return {"days": 90, "maximum_allowed_days": 90}
        if path.endswith("/artifact-and-log-retention"):
            return {"days": 90}
        if path.endswith("/git/trees/HEAD"):
            return {"tree": [{"type": "blob", "path": "demo.avi", "size": 130}]}
        if path.endswith("/contents/demo.avi"):
            return {"content": pointer_b64}
        if path.endswith("/settings/billing/usage/summary"):
            return {
                "timePeriod": {"year": 2026, "month": 11},
                "usageItems": [
                    {
                        "product": "Actions",
                        "sku": "actions_linux",
                        "grossQuantity": 34.0,
                        "grossAmount": 0.204,
                        "discountAmount": 0.204,
                        "netAmount": 0.0,
                        "unitType": "minutes",
                    },
                    {
                        "product": "Git LFS",
                        "sku": "git_lfs_storage",
                        "grossQuantity": 30.82,
                        "grossAmount": 0.0029,
                        "discountAmount": 0.0029,
                        "netAmount": 0.0,
                        "unitType": "gigabyte-hours",
                    },
                ],
            }
        if path.endswith("/settings/billing/usage"):
            return {
                "usageItems": [
                    {
                        "product": "git_lfs",
                        "sku": "Git LFS storage",
                        "quantity": 840.82,
                        "grossAmount": 0.0791,
                        "discountAmount": 0.0791,
                        "netAmount": 0.0,
                        "unitType": "GigabyteHours",
                        "repositoryName": "Opsera",
                    }
                ]
            }
        return {}

    def _pag_env(org, path, envelope_key, params=None):
        if envelope_key == "repository_cache_usages":
            return []
        if envelope_key == "artifacts" and "Repo-Audit" in path:
            return [
                {
                    "id": 13928192358,
                    "name": "repo360-audit-reports",
                    "size_in_bytes": 690176,
                    "created_at": "2026-11-12T08:44:11Z",
                    "expires_at": "2027-01-11T08:44:10Z",
                    "expired": False,
                    "workflow_run": {"id": 43184975601},
                }
            ]
        return []

    live_client.graphql.side_effect = _gql_router
    live_client.get.side_effect = _get_router
    live_client.paginate_enveloped.side_effect = _pag_env
    live_client.paginate.return_value = []

    billing_out = {}
    profiles, org_pkgs = collect_finops_data(
        live_client,
        ["DefaultOrg24"],
        [("DefaultOrg24", "Opsera"), ("DefaultOrg24", "Repo-Audit")],
        FinOpsConfig(output_dir=str(tmp_path)),
        {
            ("defaultorg24", "opsera"): {"size": 20, "private": True},
            ("defaultorg24", "repo-audit"): {"size": 2, "private": True},
        },
        billing_out=billing_out,
    )
    repo_rows, org_rows, art_rows, cp_rows = build_finops_rows(profiles, org_pkgs, ["DefaultOrg24"])
    bill_rows = build_billing_rows(billing_out)

    opsera_row = next(r for r in repo_rows if r[1] == "Opsera")
    audit_row = next(r for r in repo_rows if r[1] == "Repo-Audit")
    assert opsera_row[10] == 114.78
    assert opsera_row[11] == "Moderate Waste"
    assert audit_row[3] == 0.002
    assert art_rows[0][7] == 60
    assert art_rows[0][11] == "90 Days"

    out_xlsx = tmp_path / "finops_prod.xlsx"
    write_finops_workbook(
        out_xlsx,
        FinOpsConfig(output_dir=str(tmp_path)),
        datetime.now(timezone.utc),
        repo_rows,
        org_rows,
        art_rows,
        cp_rows,
        [],
        billing_rows=bill_rows,
        org_billing=billing_out,
    )
    wb = load_workbook(out_xlsx)
    assert "Billing & Compute Usage" in wb.sheetnames
    prod_kpis = {
        row[0].value: row[1].value
        for row in wb["Executive Summary"].iter_rows()
        if len(row) > 1 and row[0].value is not None
    }
    assert prod_kpis["Git LFS Storage Tracked (MB)"] == 114.76
    assert prod_kpis["Actions Compute Minutes (Current Period)"] == 34.0
