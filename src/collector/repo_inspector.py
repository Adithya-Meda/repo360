"""Repository and Organization data collector for Repo360 governance checks."""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional, Set, Tuple
import requests

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
        self._repo_tree_cache: Dict[Tuple[str, str, str], Optional[Set[str]]] = {}
        self._file_content_cache: Dict[Tuple[str, str, str, str], Optional[str]] = {}
        self._repo_workflows_cache: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self._repo_metadata_cache: Dict[Tuple[str, str], Optional[Dict[str, Any]]] = {}
        self._repo_rulesets_cache: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self._repo_protection_cache: Dict[Tuple[str, str, str], Optional[Dict[str, Any]]] = {}
        self._repo_environments_cache: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}

    def seed_repo_metadata(self, org: str, repo: str, metadata: Dict[str, Any]) -> None:
        """Pre-populate repository metadata cache from bulk discovery data to eliminate redundant API calls."""
        if org and repo and metadata and isinstance(metadata, dict):
            self._repo_metadata_cache[(org, repo)] = metadata

    def prefetch_repo_tree(
        self, org: str, repo: str, ref: str = "main"
    ) -> Optional[Set[str]]:
        """Fetch and cache all file paths in repository via Git Trees API in 1 single call.

        Reduces API round-trips from ~20 calls per repo down to 2-3 calls by allowing
        file existence and workflow discovery to be evaluated in-memory.
        """
        key = (org, repo, ref)
        if key in self._repo_tree_cache:
            return self._repo_tree_cache[key]

        data = self.client.get(
            f"/repos/{org}/{repo}/git/trees/{ref}",
            params={"recursive": "1"},
            allow_404=True,
        )
        if not data or not isinstance(data, dict):
            # Repository is empty or default branch ref doesn't exist
            empty_set: Set[str] = set()
            self._repo_tree_cache[key] = empty_set
            return empty_set

        tree_items = data.get("tree", [])
        tree_set = {
            item["path"] for item in tree_items
            if isinstance(item, dict) and "path" in item
        }
        self._repo_tree_cache[key] = tree_set
        return tree_set

    # --- Organization-Level Collectors ---

    def get_org_info(self, org: str) -> Optional[Dict[str, Any]]:
        """Fetch general organization metadata."""
        return self.client.get(f"/orgs/{org}", allow_404=True)

    def get_org_rulesets(self, org: str) -> List[Dict[str, Any]]:
        """Fetch all organization-level repository rulesets."""
        data = self.client.get(f"/orgs/{org}/rulesets", allow_404=True)
        return data if isinstance(data, list) else []

    def get_org_runner_groups(self, org: str) -> List[Dict[str, Any]]:
        """Fetch self-hosted runner groups in the organization."""
        data = self.client.get(f"/orgs/{org}/actions/runner-groups", allow_404=True)
        if isinstance(data, dict) and "runner_groups" in data:
            return data["runner_groups"]
        return []

    def get_org_actions_permissions(self, org: str) -> Optional[Dict[str, Any]]:
        """Fetch default actions permissions for the organization."""
        return self.client.get(f"/orgs/{org}/actions/permissions", allow_404=True)

    def list_installation_repositories(
        self,
        installation_id: Optional[int | str] = None,
        per_page: int = 100,
        max_pages: int = 50,
    ) -> List[Dict[str, Any]]:
        """List all repositories accessible to a specific or current GitHub App installation.

        Args:
            installation_id: Optional installation ID. If omitted, uses active default.
            per_page: Repositories per page (max 100).
            max_pages: Maximum pages to retrieve.

        Returns:
            List of repository metadata objects.
        """
        all_repos: List[Dict[str, Any]] = []
        page = 1

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
            total_count = data.get("total_count", 0)
            if len(all_repos) >= total_count or len(repos) < per_page:
                break
            page += 1

        return all_repos

    # --- Repository-Level Collectors ---

    def get_repo_metadata(self, org: str, repo: str) -> Optional[Dict[str, Any]]:
        """Fetch repository details with caching."""
        cache_key = (org, repo)
        if cache_key in self._repo_metadata_cache:
            return self._repo_metadata_cache[cache_key]
        meta = self.client.get(f"/repos/{org}/{repo}", allow_404=True)
        self._repo_metadata_cache[cache_key] = meta
        return meta

    def get_repo_rulesets(self, org: str, repo: str) -> List[Dict[str, Any]]:
        """Fetch repository-level rulesets (falls back gracefully if 403 forbidden or unconfigured)."""
        cache_key = (org, repo)
        if cache_key in self._repo_rulesets_cache:
            return self._repo_rulesets_cache[cache_key]
        try:
            data = self.client.get(f"/repos/{org}/{repo}/rulesets", allow_404=True)
            res = data if isinstance(data, list) else []
            self._repo_rulesets_cache[cache_key] = res
            return res
        except requests.exceptions.HTTPError as err:
            if getattr(err, "response", None) is not None and err.response.status_code == 403:
                logger.debug("Rulesets API returned 403 on %s/%s (falling back to classic protection).", org, repo)
                self._repo_rulesets_cache[cache_key] = []
                return []
            raise

    def get_branch_protection(
        self, org: str, repo: str, branch: str = "main"
    ) -> Optional[Dict[str, Any]]:
        """Fetch branch protection rules for a specific branch (falls back gracefully if 403/404)."""
        cache_key = (org, repo, branch)
        if cache_key in self._repo_protection_cache:
            return self._repo_protection_cache[cache_key]
        try:
            res = self.client.get(
                f"/repos/{org}/{repo}/branches/{branch}/protection", allow_404=True
            )
            self._repo_protection_cache[cache_key] = res
            return res
        except requests.exceptions.HTTPError as err:
            if getattr(err, "response", None) is not None and err.response.status_code == 403:
                logger.debug("Branch protection API returned 403 on %s/%s:%s.", org, repo, branch)
                self._repo_protection_cache[cache_key] = None
                return None
            raise

    def get_file_content(
        self, org: str, repo: str, path: str, ref: str = "main"
    ) -> Optional[str]:
        """Fetch and decode utf-8 text file content from repository.

        Leverages the repository tree cache to avoid 404 HTTP round-trips for missing files.

        Args:
            org: Organization name.
            repo: Repository name.
            path: Relative path to file (e.g., 'CODEOWNERS' or '.github/copilot-instructions.md').
            ref: Branch or commit ref.

        Returns:
            Decoded file text content, or None if file does not exist.
        """
        path_clean = path.lstrip("/")
        content_key = (org, repo, path_clean, ref)
        if content_key in self._file_content_cache:
            return self._file_content_cache[content_key]

        tree_key = (org, repo, ref)
        if tree_key in self._repo_tree_cache:
            tree_set = self._repo_tree_cache[tree_key]
            if tree_set is not None and path_clean not in tree_set:
                self._file_content_cache[content_key] = None
                return None

        data = self.client.get(
            f"/repos/{org}/{repo}/contents/{path_clean}",
            params={"ref": ref},
            allow_404=True,
        )
        if not data or not isinstance(data, dict):
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

        self._file_content_cache[content_key] = decoded_str
        return decoded_str

    def list_workflows(self, org: str, repo: str, ref: str = "main") -> List[Dict[str, Any]]:
        """List GitHub Actions workflow files and details.

        Discovers workflows directly from tree cache if present to eliminate redundant API calls.
        """
        cache_key = (org, repo)
        if cache_key in self._repo_workflows_cache:
            return self._repo_workflows_cache[cache_key]

        tree_key = (org, repo, ref)
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

        data = self.client.get(f"/repos/{org}/{repo}/actions/workflows", allow_404=True)
        workflows = data.get("workflows", []) if isinstance(data, dict) and "workflows" in data else []
        self._repo_workflows_cache[cache_key] = workflows
        return workflows

    def get_environments(self, org: str, repo: str) -> List[Dict[str, Any]]:
        """Fetch deployment environments configured for the repository with caching."""
        cache_key = (org, repo)
        if cache_key in self._repo_environments_cache:
            return self._repo_environments_cache[cache_key]
        data = self.client.get(f"/repos/{org}/{repo}/environments", allow_404=True)
        res = data.get("environments", []) if isinstance(data, dict) and "environments" in data else []
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
        if tree_key in self._repo_tree_cache:
            tree_set = self._repo_tree_cache[tree_key]
            if tree_set is not None:
                if path_clean in tree_set:
                    return True
                prefix = path_clean + "/"
                return any(p.startswith(prefix) for p in tree_set)

        data = self.client.get(
            f"/repos/{org}/{repo}/contents/{path_clean}",
            params={"ref": ref},
            allow_404=True,
        )
        return data is not None

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

        # Use primary language from repo metadata if available
        repo_meta = self.get_repo_metadata(org, repo) or {}
        primary_lang = repo_meta.get("language")
        if primary_lang:
            languages.append(primary_lang)

        # Single API call or tree cache to list root directory contents
        tree_key = (org, repo, ref)
        root_files: Set[str] = set()
        if tree_key in self._repo_tree_cache and self._repo_tree_cache[tree_key] is not None:
            tree_set = self._repo_tree_cache[tree_key]
            root_files = {p for p in tree_set if "/" not in p}
        else:
            contents = self.client.get(
                f"/repos/{org}/{repo}/contents",
                params={"ref": ref},
                allow_404=True,
            )
            if isinstance(contents, list):
                for item in contents:
                    if isinstance(item, dict) and "name" in item:
                        root_files.add(item["name"])

        for filename, lang in manifest_checks:
            exists = (filename in root_files) if root_files else self.check_file_exists(org, repo, filename, ref=ref)
            if exists:
                manifests.append(filename)
                if lang not in languages:
                    languages.append(lang)

        # Quick peek at package.json or requirements.txt for key frameworks
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

        # Deduplicate languages and prefer specific ecosystem labels over bare parent tokens (e.g. 'C# / .NET Core' over 'C#')
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

