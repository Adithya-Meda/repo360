"""Repository and Organization data collector for Repo360 governance checks."""

from __future__ import annotations

import base64
import logging
import math
import threading
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import quote
import requests

from ..common.concurrency import is_empty_repo, map_concurrent
from .client import Repo360Client

logger = logging.getLogger(__name__)


class RepoInspector:
    """Collects metadata, settings, and file contents from GitHub."""

    def __init__(self, client: Repo360Client) -> None:
        """Initialize with an authenticated client.

        Args:
            client: Authenticated Repo360Client instance.
        """
        self.client = client
        self._cache_lock = threading.RLock()
        self._repo_tree_cache: Dict[Tuple[str, str, str], Optional[Set[str]]] = {}
        self._file_content_cache: Dict[Tuple[str, str, str, str], Optional[str]] = {}
        self._repo_workflows_cache: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self._repo_metadata_cache: Dict[Tuple[str, str], Optional[Dict[str, Any]]] = {}
        self._repo_rulesets_cache: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self._repo_protection_cache: Dict[Tuple[str, str, str], Optional[Dict[str, Any]]] = {}
        self._repo_branch_rules_cache: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
        self._repo_environments_cache: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self._permission_errors: Dict[Tuple[str, Optional[str], str], str] = {}

    def _record_permission_error(
        self, org: str, control: str, message: str, repo: Optional[str] = None
    ) -> None:
        key = (org, repo, control)
        with self._cache_lock:
            if key not in self._permission_errors:
                logger.warning("Permission gap [%s/%s - %s]: %s", org, repo or "Organization", control, message)
            self._permission_errors[key] = message

    def get_permission_error(
        self, org: str, control: str, repo: Optional[str] = None
    ) -> Optional[str]:
        """Return recorded HTTP 403/permission error message for an org/repo control, if any."""
        with self._cache_lock:
            return self._permission_errors.get((org, repo, control))

    @staticmethod
    def _is_forbidden_error(err: requests.exceptions.HTTPError) -> bool:
        return getattr(err, "response", None) is not None and err.response.status_code == 403

    @staticmethod
    def _extract_forbidden_context(err: requests.exceptions.HTTPError) -> Tuple[str, str, str]:
        """Extract API error message, accepted-permissions header, and SSO header from a 403 error."""
        resp = getattr(err, "response", None)
        if resp is None:
            return "", "", ""

        api_msg = ""
        try:
            payload = resp.json()
            if isinstance(payload, dict):
                api_msg = str(payload.get("message") or "").strip()
        except Exception:
            raw_text = getattr(resp, "text", None)
            if isinstance(raw_text, str):
                api_msg = raw_text.strip()[:160]

        headers = getattr(resp, "headers", None)
        if headers is not None and hasattr(headers, "get") and not isinstance(headers, (str, bytes)):
            try:
                raw_acc = headers.get("x-accepted-github-permissions") or headers.get(
                    "X-Accepted-GitHub-Permissions"
                )
                raw_sso = headers.get("x-github-sso") or headers.get("X-GitHub-SSO")
                accepted = raw_acc.strip() if isinstance(raw_acc, str) else ""
                sso_hdr = raw_sso.strip() if isinstance(raw_sso, str) else ""
                return api_msg, accepted, sso_hdr
            except Exception:
                return api_msg, "", ""

        return api_msg, "", ""

    def _describe_forbidden_error(
        self,
        err: requests.exceptions.HTTPError,
        endpoint: str,
        default_hint: str,
        org: str = "",
        permission_key: Optional[str] = None,
        endpoint_requires_write: bool = False,
    ) -> str:
        """Build an actionable 403 diagnostic combining GitHub headers, payload, and App permissions."""
        api_msg, accepted, sso_hdr = self._extract_forbidden_context(err)
        lower_msg = api_msg.lower()

        if sso_hdr or "saml" in lower_msg:
            detail = api_msg or "SSO authorization required"
            return (
                f"HTTP 403 Forbidden on {endpoint} — blocked by Organization SAML SSO / "
                f"network policy ({detail})."
            )

        if any(tok in lower_msg for tok in ("upgrade", "enterprise", "plan", "subscription", "not enabled")):
            return (
                f"HTTP 403 Forbidden on {endpoint} — GitHub API returned '{api_msg}' "
                f"(feature unavailable on current GitHub plan tier, not a missing App permission)."
            )

        granted_perms: Optional[Dict[str, str]] = None
        auth = getattr(self.client, "auth", None)
        get_perms = getattr(auth, "get_installation_permissions", None)
        if callable(get_perms) and org:
            try:
                candidate = get_perms(org)
                if isinstance(candidate, dict):
                    granted_perms = candidate
            except Exception:
                granted_perms = None

        accepted_note = f" (required by GitHub: {accepted})" if accepted else ""
        msg_note = f" (GitHub API: {api_msg})" if api_msg else ""

        if granted_perms is not None and permission_key:
            granted_level = granted_perms.get(permission_key)
            if granted_level:
                if endpoint_requires_write and granted_level == "read":
                    return (
                        f"HTTP 403 Forbidden on {endpoint} — App installation has "
                        f"{permission_key}={granted_level}, but GitHub REST API requires "
                        f"'{permission_key}=write' (and GitHub Enterprise for org-level rulesets)"
                        f"{accepted_note}{msg_note}."
                    )
                if "write" in accepted.lower() and granted_level == "read":
                    return (
                        f"HTTP 403 Forbidden on {endpoint} — App installation has "
                        f"{permission_key}={granted_level}, but GitHub requires write access"
                        f"{accepted_note}{msg_note}."
                    )
                return (
                    f"HTTP 403 Forbidden on {endpoint} — App installation already grants "
                    f"{permission_key}={granted_level}, yet GitHub returned Forbidden"
                    f"{accepted_note}{msg_note} (check plan entitlement or endpoint permission tier)."
                )
            if granted_perms:
                return (
                    f"HTTP 403 Forbidden on {endpoint} — active org installation lacks "
                    f"'{permission_key}' (check Organization Settings -> GitHub Apps -> Accept new permissions); "
                    f"{default_hint}{msg_note}."
                )

        if endpoint_requires_write and accepted:
            return (
                f"HTTP 403 Forbidden on {endpoint} — GitHub REST API requires "
                f"{accepted}{msg_note} (read-only Apps default to Read)."
            )

        if accepted or api_msg:
            return f"HTTP 403 Forbidden on {endpoint}; {default_hint}{accepted_note}{msg_note}."

        return f"HTTP 403 Forbidden on {endpoint}; {default_hint}."

    def _record_forbidden_error(
        self,
        err: requests.exceptions.HTTPError,
        org: str,
        control: str,
        endpoint: str,
        default_hint: str,
        permission_key: Optional[str] = None,
        repo: Optional[str] = None,
        endpoint_requires_write: bool = False,
    ) -> None:
        """Format and record an actionable HTTP 403 diagnostic message."""
        message = self._describe_forbidden_error(
            err,
            endpoint=endpoint,
            default_hint=default_hint,
            org=org,
            permission_key=permission_key,
            endpoint_requires_write=endpoint_requires_write,
        )
        self._record_permission_error(org, control, message, repo=repo)

    def seed_repo_metadata(self, org: str, repo: str, metadata: Dict[str, Any]) -> None:
        """Pre-populate repository metadata cache from bulk discovery data to eliminate redundant API calls."""
        if org and repo and metadata and isinstance(metadata, dict):
            with self._cache_lock:
                self._repo_metadata_cache[(org, repo)] = metadata

    def prefetch_repo_tree(
        self, org: str, repo: str, ref: str = "main"
    ) -> Optional[Set[str]]:
        """Fetch and cache all file paths in repository via Git Trees API in 1 single call.

        Reduces API round-trips from ~20 calls per repo down to 2-3 calls by allowing
        file existence and workflow discovery to be evaluated in-memory.
        """
        key = (org, repo, ref)
        with self._cache_lock:
            if key in self._repo_tree_cache:
                return self._repo_tree_cache[key]
            cached_meta = self._repo_metadata_cache.get((org, repo))
            if is_empty_repo(cached_meta):
                empty_set: Set[str] = set()
                self._repo_tree_cache[key] = empty_set
                return empty_set

        try:
            data = self.client.get(
                f"/repos/{org}/{repo}/git/trees/{quote(ref, safe='')}",
                params={"recursive": "1"},
                allow_404=True,
                allow_403=True,
            )
        except requests.exceptions.HTTPError as err:
            if self._is_forbidden_error(err):
                data = None
            else:
                raise
        if not data or not isinstance(data, dict):
            # Empty repo or missing default branch ref
            empty_set = set()
            with self._cache_lock:
                self._repo_tree_cache[key] = empty_set
            return empty_set

        tree_items = data.get("tree", [])
        tree_set = {
            item["path"] for item in tree_items
            if isinstance(item, dict) and "path" in item
        }
        with self._cache_lock:
            self._repo_tree_cache[key] = tree_set
        return tree_set

    def get_org_info(self, org: str) -> Optional[Dict[str, Any]]:
        """Fetch general organization metadata."""
        try:
            return self.client.get(f"/orgs/{org}", allow_404=True)
        except requests.exceptions.HTTPError as err:
            if self._is_forbidden_error(err):
                self._record_forbidden_error(
                    err,
                    org,
                    "org_info",
                    "/orgs/{org}",
                    "grant Organization Administration (Read) permission",
                    permission_key="organization_administration",
                )
                return None
            raise

    def get_org_rulesets(self, org: str) -> List[Dict[str, Any]]:
        """Fetch all organization-level repository rulesets."""
        try:
            data = self.client.get(f"/orgs/{org}/rulesets", allow_404=True)
            return data if isinstance(data, list) else []
        except requests.exceptions.HTTPError as err:
            if self._is_forbidden_error(err):
                self._record_forbidden_error(
                    err,
                    org,
                    "org_rulesets",
                    "/orgs/{org}/rulesets",
                    "grant Organization Administration (Write) permission or GitHub Enterprise plan",
                    permission_key="organization_administration",
                    endpoint_requires_write=True,
                )
                return []
            raise

    def get_org_runner_groups(self, org: str) -> List[Dict[str, Any]]:
        """Fetch self-hosted runner groups in the organization."""
        try:
            data = self.client.get(f"/orgs/{org}/actions/runner-groups", allow_404=True)
            if isinstance(data, dict) and "runner_groups" in data:
                return data["runner_groups"]
            return []
        except requests.exceptions.HTTPError as err:
            if self._is_forbidden_error(err):
                self._record_forbidden_error(
                    err,
                    org,
                    "org_runner_groups",
                    "/orgs/{org}/actions/runner-groups",
                    "grant Self-hosted Runners (Read) permission",
                    permission_key="organization_self_hosted_runners",
                )
                return []
            raise

    def get_org_self_hosted_runners_count(self, org: str) -> Optional[int]:
        """Return count of registered org-level self-hosted runners, or None when inaccessible."""
        try:
            data = self.client.get(
                f"/orgs/{org}/actions/runners",
                params={"per_page": 1},
                allow_404=True,
                allow_403=True,
            )
            if isinstance(data, dict) and "total_count" in data:
                return int(data["total_count"])
            return None
        except requests.exceptions.HTTPError as err:
            if self._is_forbidden_error(err):
                return None
            raise

    def get_org_actions_permissions(self, org: str) -> Optional[Dict[str, Any]]:
        """Fetch top-level actions enablement permissions for the organization."""
        try:
            return self.client.get(f"/orgs/{org}/actions/permissions", allow_404=True)
        except requests.exceptions.HTTPError as err:
            if self._is_forbidden_error(err):
                self._record_forbidden_error(
                    err,
                    org,
                    "org_actions_permissions",
                    "/orgs/{org}/actions/permissions",
                    "grant Organization Administration (Read) permission",
                    permission_key="organization_administration",
                )
                return None
            raise

    def get_org_workflow_permissions(self, org: str) -> Optional[Dict[str, Any]]:
        """Fetch default GITHUB_TOKEN workflow permissions for the organization."""
        try:
            return self.client.get(f"/orgs/{org}/actions/permissions/workflow", allow_404=True)
        except requests.exceptions.HTTPError as err:
            if self._is_forbidden_error(err):
                self._record_forbidden_error(
                    err,
                    org,
                    "org_workflow_permissions",
                    "/orgs/{org}/actions/permissions/workflow",
                    "grant Organization Administration (Read) permission",
                    permission_key="organization_administration",
                )
                return None
            raise

    def get_org_saml_sso(self, org: str) -> Optional[Dict[str, Any]]:
        """Inspect GraphQL organization SAML SSO and 2FA enforcement metadata."""
        query = """
        query($org: String!) {
          organization(login: $org) {
            requiresTwoFactorAuthentication
            samlIdentityProvider {
              ssoUrl
            }
          }
        }
        """
        try:
            resp = self.client.graphql(query, variables={"org": org}, org=org)
            errors = resp.get("errors") if isinstance(resp, dict) else None
            org_node = (resp.get("data") or {}).get("organization") if isinstance(resp, dict) else None
            if errors and not org_node:
                err_msg = "; ".join(str(e.get("message", "")) for e in errors if isinstance(e, dict))
                if "forbidden" in err_msg.lower() or "resource not accessible" in err_msg.lower():
                    self._record_permission_error(
                        org,
                        "org_saml_sso",
                        f"GraphQL SAML SSO lookup denied: {err_msg}",
                    )
                    return {"error_403": True}
            if isinstance(org_node, dict):
                return org_node
            return None
        except requests.exceptions.HTTPError as err:
            if self._is_forbidden_error(err):
                self._record_forbidden_error(
                    err,
                    org,
                    "org_saml_sso",
                    "GraphQL SAML SSO",
                    "grant Organization Administration (Read) permission",
                    permission_key="organization_administration",
                )
                return {"error_403": True}
            return None
        except Exception as err:
            logger.debug("SAML SSO GraphQL check unavailable for %s: %s", org, err)
            return None

    def list_installation_repositories(
        self,
        installation_id: Optional[int | str] = None,
        per_page: int = 100,
        max_pages: int = 50,
    ) -> List[Dict[str, Any]]:
        """List all repositories accessible to a specific or current GitHub App installation.

        Fetches remaining pages concurrently when page 1 reveals ``total_count > per_page``.
        """
        first_data = self.client.get(
            "/installation/repositories",
            params={"per_page": per_page, "page": 1},
            allow_404=True,
            installation_id=installation_id,
        )
        if not first_data or not isinstance(first_data, dict):
            return []

        first_repos = first_data.get("repositories", [])
        if not first_repos:
            return []

        all_repos: List[Dict[str, Any]] = list(first_repos)
        total_count = first_data.get("total_count", 0)
        if len(first_repos) < per_page or max_pages <= 1:
            return all_repos
        if total_count and len(all_repos) >= total_count:
            return all_repos

        if total_count and total_count > len(all_repos):
            last_page = min(max_pages, math.ceil(total_count / per_page))
            remaining_pages = list(range(2, last_page + 1))

            def _fetch_page(p: int) -> List[Dict[str, Any]]:
                data = self.client.get(
                    "/installation/repositories",
                    params={"per_page": per_page, "page": p},
                    allow_404=True,
                    installation_id=installation_id,
                )
                if data and isinstance(data, dict):
                    return data.get("repositories", []) or []
                return []

            page_batches = map_concurrent(
                remaining_pages,
                _fetch_page,
                max_workers=min(8, len(remaining_pages)),
                max_cap=8,
                thread_name_prefix="inst-pages",
            )
            for batch in page_batches:
                all_repos.extend(batch)
            return all_repos

        page = 2
        while page <= max_pages:
            data = self.client.get(
                "/installation/repositories",
                params={"per_page": per_page, "page": page},
                allow_404=True,
                installation_id=installation_id,
            )
            if not data or not isinstance(data, dict):
                break
            repos = data.get("repositories", [])
            if not repos:
                break
            all_repos.extend(repos)
            if len(repos) < per_page:
                break
            page += 1

        return all_repos

    def get_repo_metadata(self, org: str, repo: str) -> Optional[Dict[str, Any]]:
        """Fetch repository details with thread-safe caching."""
        cache_key = (org, repo)
        with self._cache_lock:
            if cache_key in self._repo_metadata_cache:
                return self._repo_metadata_cache[cache_key]
        meta = self.client.get(f"/repos/{org}/{repo}", allow_404=True)
        with self._cache_lock:
            self._repo_metadata_cache[cache_key] = meta
        return meta

    def get_repo_rulesets(self, org: str, repo: str) -> List[Dict[str, Any]]:
        """Fetch repository-level rulesets (records 403 permission gaps explicitly)."""
        cache_key = (org, repo)
        with self._cache_lock:
            if cache_key in self._repo_rulesets_cache:
                return self._repo_rulesets_cache[cache_key]
        try:
            data = self.client.get(f"/repos/{org}/{repo}/rulesets", allow_404=True)
            res = data if isinstance(data, list) else []
            with self._cache_lock:
                self._repo_rulesets_cache[cache_key] = res
            return res
        except requests.exceptions.HTTPError as err:
            if self._is_forbidden_error(err):
                self._record_forbidden_error(
                    err,
                    org,
                    "repo_rulesets",
                    "/rulesets",
                    "grant Repository Administration (Read) permission",
                    permission_key="administration",
                    repo=repo,
                )
                with self._cache_lock:
                    self._repo_rulesets_cache[cache_key] = []
                return []
            raise

    def get_repo_branch_rules(
        self, org: str, repo: str, branch: str = "main"
    ) -> List[Dict[str, Any]]:
        """Fetch effective active branch rules (org + repo rulesets) targeting ``branch``."""
        cache_key = (org, repo, branch)
        with self._cache_lock:
            if cache_key in self._repo_branch_rules_cache:
                return self._repo_branch_rules_cache[cache_key]
            cached_meta = self._repo_metadata_cache.get((org, repo))
            if is_empty_repo(cached_meta):
                self._repo_branch_rules_cache[cache_key] = []
                return []
        try:
            data = self.client.get(
                f"/repos/{org}/{repo}/rules/branches/{quote(branch, safe='')}",
                allow_404=True,
            )
            res = data if isinstance(data, list) else []
            with self._cache_lock:
                self._repo_branch_rules_cache[cache_key] = res
            return res
        except requests.exceptions.HTTPError as err:
            if self._is_forbidden_error(err):
                self._record_forbidden_error(
                    err,
                    org,
                    "repo_branch_rules",
                    "/rules/branches",
                    "grant Repository Administration (Read) permission",
                    permission_key="administration",
                    repo=repo,
                )
                with self._cache_lock:
                    self._repo_branch_rules_cache[cache_key] = []
                return []
            raise

    def get_repo_ruleset_details(
        self, org: str, repo: str, ruleset_id: int
    ) -> Optional[Dict[str, Any]]:
        """Fetch full rule payload for a single repository ruleset ID."""
        try:
            data = self.client.get(f"/repos/{org}/{repo}/rulesets/{ruleset_id}", allow_404=True, allow_403=True)
            return data if isinstance(data, dict) else None
        except requests.exceptions.HTTPError as err:
            if self._is_forbidden_error(err):
                return None
            raise

    def get_branch_protection(
        self, org: str, repo: str, branch: str = "main"
    ) -> Optional[Dict[str, Any]]:
        """Fetch branch protection rules for a specific branch (records 403 permission gaps)."""
        cache_key = (org, repo, branch)
        with self._cache_lock:
            if cache_key in self._repo_protection_cache:
                return self._repo_protection_cache[cache_key]
            cached_meta = self._repo_metadata_cache.get((org, repo))
            if is_empty_repo(cached_meta):
                self._repo_protection_cache[cache_key] = None
                return None
        try:
            res = self.client.get(
                f"/repos/{org}/{repo}/branches/{quote(branch, safe='')}/protection", allow_404=True
            )
            with self._cache_lock:
                self._repo_protection_cache[cache_key] = res
            return res
        except requests.exceptions.HTTPError as err:
            if self._is_forbidden_error(err):
                self._record_forbidden_error(
                    err,
                    org,
                    "branch_protection",
                    "/branches/.../protection",
                    "grant Repository Administration (Read) or plan entitlement",
                    permission_key="administration",
                    repo=repo,
                )
                with self._cache_lock:
                    self._repo_protection_cache[cache_key] = None
                return None
            raise

    def get_file_content(
        self, org: str, repo: str, path: str, ref: str = "main"
    ) -> Optional[str]:
        """Fetch and decode utf-8 text file content from repository.

        Leverages the repository tree cache to avoid 404 HTTP round-trips for missing files.
        """
        path_clean = path.lstrip("/")
        content_key = (org, repo, path_clean, ref)
        tree_key = (org, repo, ref)
        with self._cache_lock:
            if content_key in self._file_content_cache:
                return self._file_content_cache[content_key]
            if tree_key in self._repo_tree_cache:
                tree_set = self._repo_tree_cache[tree_key]
                if tree_set is not None and path_clean not in tree_set:
                    self._file_content_cache[content_key] = None
                    return None

        try:
            data = self.client.get(
                f"/repos/{org}/{repo}/contents/{path_clean}",
                params={"ref": ref},
                allow_404=True,
                allow_403=True,
            )
        except requests.exceptions.HTTPError as err:
            if self._is_forbidden_error(err):
                data = None
            else:
                raise
        if not data or not isinstance(data, dict):
            with self._cache_lock:
                self._file_content_cache[content_key] = None
            return None

        content = data.get("content")
        encoding = data.get("encoding")
        decoded_str: Optional[str] = None
        if content and encoding == "base64":
            try:
                decoded_bytes = base64.b64decode(content)
                decoded_str = decoded_bytes.decode("utf-8", errors="replace")
            except Exception as err:
                logger.warning("Failed to decode %s in %s/%s: %s", path, org, repo, err)
                decoded_str = None

        with self._cache_lock:
            self._file_content_cache[content_key] = decoded_str
        return decoded_str

    def list_workflows(self, org: str, repo: str, ref: str = "main") -> List[Dict[str, Any]]:
        """List GitHub Actions workflow files and details.

        Discovers workflows directly from tree cache if present to eliminate redundant API calls.
        """
        cache_key = (org, repo)
        tree_key = (org, repo, ref)
        with self._cache_lock:
            if cache_key in self._repo_workflows_cache:
                return self._repo_workflows_cache[cache_key]
            if tree_key in self._repo_tree_cache:
                tree_set = self._repo_tree_cache[tree_key]
                if tree_set is not None:
                    wf_paths = [
                        p for p in tree_set
                        if p.startswith(".github/workflows/") and (p.endswith(".yml") or p.endswith(".yaml"))
                    ]
                    workflows = [
                        {"name": p.split("/")[-1], "path": p, "state": "active"}
                        for p in sorted(wf_paths)
                    ]
                    self._repo_workflows_cache[cache_key] = workflows
                    return workflows

        try:
            data = self.client.get(f"/repos/{org}/{repo}/actions/workflows", allow_404=True)
            workflows = data.get("workflows", []) if isinstance(data, dict) and "workflows" in data else []
        except requests.exceptions.HTTPError as err:
            if self._is_forbidden_error(err):
                self._record_forbidden_error(
                    err,
                    org,
                    "repo_workflows",
                    "/actions/workflows",
                    "grant Actions (Read) permission",
                    permission_key="actions",
                    repo=repo,
                )
                workflows = []
            else:
                raise
        with self._cache_lock:
            self._repo_workflows_cache[cache_key] = workflows
        return workflows

    def get_environments(self, org: str, repo: str) -> List[Dict[str, Any]]:
        """Fetch deployment environments configured for the repository with caching."""
        cache_key = (org, repo)
        with self._cache_lock:
            if cache_key in self._repo_environments_cache:
                return self._repo_environments_cache[cache_key]
        try:
            data = self.client.get(f"/repos/{org}/{repo}/environments", allow_404=True)
            res = data.get("environments", []) if isinstance(data, dict) and "environments" in data else []
        except requests.exceptions.HTTPError as err:
            if self._is_forbidden_error(err):
                self._record_forbidden_error(
                    err,
                    org,
                    "repo_environments",
                    "/environments",
                    "grant Actions (Read) permission",
                    permission_key="actions",
                    repo=repo,
                )
                res = []
            else:
                raise
        with self._cache_lock:
            self._repo_environments_cache[cache_key] = res
        return res

    def check_file_exists(
        self, org: str, repo: str, path: str, ref: str = "main"
    ) -> bool:
        """Check if a file or directory exists in the repo.

        Evaluates against local in-memory tree cache when available for 0ms execution.
        """
        path_clean = path.lstrip("/")
        tree_key = (org, repo, ref)
        with self._cache_lock:
            if tree_key in self._repo_tree_cache:
                tree_set = self._repo_tree_cache[tree_key]
                if tree_set is not None:
                    if path_clean in tree_set:
                        return True
                    prefix = path_clean + "/"
                    return any(p.startswith(prefix) for p in tree_set)

        try:
            data = self.client.get(
                f"/repos/{org}/{repo}/contents/{path_clean}",
                params={"ref": ref},
                allow_404=True,
                allow_403=True,
            )
        except requests.exceptions.HTTPError as err:
            if self._is_forbidden_error(err):
                data = None
            else:
                raise
        return data is not None

    def prefetch_repo_audit_context(
        self,
        org: str,
        repo: str,
        ref: str = "main",
        enabled_checks: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Concurrently pre-warm rulesets, protection, CODEOWNERS, manifests, and workflow files."""
        with self._cache_lock:
            tree_set = self._repo_tree_cache.get((org, repo, ref))
        if tree_set is None:
            tree_set = self.prefetch_repo_tree(org, repo, ref)
        if not tree_set:
            tree_set = set()

        def _is_enabled(check_key: str) -> bool:
            if not enabled_checks:
                return True
            val = enabled_checks.get(check_key)
            if isinstance(val, dict):
                return bool(val.get("enabled", True))
            return True if val is None else bool(val)

        tasks: List[Callable[[], Any]] = []

        if tree_set:
            if _is_enabled("codeowners_validation"):
                for co_path in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
                    if co_path in tree_set:
                        tasks.append(lambda p=co_path: self.get_file_content(org, repo, p, ref))
                        break

            if "package.json" in tree_set:
                tasks.append(lambda: self.get_file_content(org, repo, "package.json", ref))
            if "requirements.txt" in tree_set:
                tasks.append(lambda: self.get_file_content(org, repo, "requirements.txt", ref))
            elif "pyproject.toml" in tree_set:
                tasks.append(lambda: self.get_file_content(org, repo, "pyproject.toml", ref))

            if _is_enabled("workflow_sha_pinning") or _is_enabled("workflow_concurrency_groups"):
                wf_paths = sorted(
                    p for p in tree_set
                    if p.startswith(".github/workflows/") and (p.endswith(".yml") or p.endswith(".yaml"))
                )
                for wf_path in wf_paths:
                    tasks.append(lambda p=wf_path: self.get_file_content(org, repo, p, ref))

        if _is_enabled("branch_rulesets"):
            tasks.append(lambda: self.get_repo_rulesets(org, repo))
            tasks.append(lambda: self.get_repo_branch_rules(org, repo, ref))
            tasks.append(lambda: self.get_branch_protection(org, repo, ref))
        if _is_enabled("environment_protection_gates"):
            tasks.append(lambda: self.get_environments(org, repo))

        if len(tasks) <= 1:
            for fn in tasks:
                try:
                    fn()
                except Exception:
                    pass
            return

        def _safe_run(fn: Callable[[], Any]) -> None:
            try:
                fn()
            except Exception:
                pass

        map_concurrent(
            tasks,
            _safe_run,
            max_workers=min(6, len(tasks)),
            max_cap=6,
            thread_name_prefix="repo-prefetch",
        )

    def detect_tech_stack(
        self, org: str, repo: str, ref: str = "main"
    ) -> Dict[str, Any]:
        """Detect the technology stack, languages, and package manifests of a repository."""
        languages: List[str] = []
        manifests: List[str] = []
        frameworks: List[str] = []

        manifest_checks = [
            ("package.json", "Node.js / TypeScript"),
            ("requirements.txt", "Python"),
            ("pyproject.toml", "Python"),
            ("pom.xml", "Java (Maven)"),
            ("build.gradle", "Java / Kotlin (Gradle)"),
            ("go.mod", "Go"),
            ("Cargo.toml", "Rust"),
            ("global.json", "C# / .NET Core"),
            ("Directory.Build.props", "C# / .NET Core"),
            ("NuGet.config", "C# / .NET Core"),
            ("Dockerfile", "Docker"),
            ("docker-compose.yml", "Docker Compose"),
            ("CMakeLists.txt", "C / C++"),
        ]

        repo_meta = self.get_repo_metadata(org, repo) or {}
        primary_lang = repo_meta.get("language")
        if primary_lang:
            languages.append(primary_lang)

        tree_key = (org, repo, ref)
        root_files: Set[str] = set()
        has_tree_cache = False
        with self._cache_lock:
            tree_set = self._repo_tree_cache.get(tree_key)
        if tree_set is not None:
            has_tree_cache = True
            root_files = {p for p in tree_set if "/" not in p}
        else:
            try:
                contents = self.client.get(
                    f"/repos/{org}/{repo}/contents",
                    params={"ref": ref},
                    allow_404=True,
                    allow_403=True,
                )
            except requests.exceptions.HTTPError as err:
                if self._is_forbidden_error(err):
                    contents = None
                else:
                    raise
            if isinstance(contents, list):
                for item in contents:
                    if isinstance(item, dict) and "name" in item:
                        root_files.add(item["name"])

        for filename, lang in manifest_checks:
            exists = (filename in root_files) if (root_files or has_tree_cache) else self.check_file_exists(org, repo, filename, ref=ref)
            if exists:
                manifests.append(filename)
                if lang not in languages:
                    languages.append(lang)

        if "package.json" in manifests:
            pkg_content = self.get_file_content(org, repo, "package.json", ref=ref)
            if pkg_content:
                for kw in ["react", "next", "vue", "angular", "express", "nest"]:
                    if f'"{kw}"' in pkg_content.lower():
                        frameworks.append(kw.capitalize())

        if "requirements.txt" in manifests or "pyproject.toml" in manifests:
            req_content = (
                self.get_file_content(org, repo, "requirements.txt", ref=ref)
                or self.get_file_content(org, repo, "pyproject.toml", ref=ref)
                or ""
            )
            for kw in ["fastapi", "django", "flask", "pydantic", "pytest", "sqlalchemy"]:
                if kw in req_content.lower():
                    frameworks.append(kw.capitalize())

        # Prefer specific labels (e.g. 'C# / .NET Core') over bare parent names ('C#')
        specific_prefixes = {
            lang.split(" / ", 1)[0].strip().casefold()
            for lang in languages
            if " / " in lang
        }
        deduped_languages: List[str] = []
        seen_langs: Set[str] = set()
        for lang in languages:
            clean = lang.strip()
            key = clean.casefold()
            if not key or key in seen_langs:
                continue
            if " / " not in clean and key in specific_prefixes:
                continue
            seen_langs.add(key)
            deduped_languages.append(clean)

        return {
            "languages": deduped_languages or ["General Software"],
            "manifests": manifests,
            "frameworks": frameworks,
        }

