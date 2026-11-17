"""GitHub App Authentication Manager.

Handles RS256 JWT generation and Installation Access Token retrieval
for GitHub organizations without leaking credentials.
"""

from __future__ import annotations

import base64
import contextlib
import email.utils
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import jwt
import requests

from .. import __version__
from ..common.interrupts import interruptible_sleep, is_shutting_down

logger = logging.getLogger(__name__)

USER_AGENT = f"Repo360/{__version__}"
DEFAULT_HTTP_TIMEOUT_SECONDS = 30
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def validate_api_base_url(api_base_url: str) -> str:
    """Return a normalized API base URL, rejecting non-HTTPS endpoints that would expose tokens.

    Plain HTTP is only tolerated for loopback hosts (local test doubles).
    """
    cleaned = (api_base_url or "").strip().rstrip("/")
    parsed = urlparse(cleaned)
    if not parsed.netloc or parsed.scheme not in ("https", "http"):
        raise ValueError(f"Invalid GitHub API base URL: {api_base_url!r}")
    if parsed.scheme == "http" and (parsed.hostname or "") not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"Refusing insecure GitHub API base URL {cleaned!r}: HTTPS is required to protect credentials."
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("GitHub API base URL must not contain credentials, query strings, or fragments.")
    return cleaned


def _request_with_retry(
    method: str,
    url: str,
    max_retries: int = 3,
    backoff_factor: float = 1.0,
    **kwargs: Any,
) -> requests.Response:
    """Execute an HTTP request with automatic retries on transient network/connection drops."""
    timeout = kwargs.pop("timeout", DEFAULT_HTTP_TIMEOUT_SECONDS)
    last_err: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        if is_shutting_down():
            raise KeyboardInterrupt
        try:
            verb = method.upper()
            if verb == "GET":
                return requests.get(url, timeout=timeout, **kwargs)
            if verb == "POST":
                return requests.post(url, timeout=timeout, **kwargs)
            return requests.request(verb, url, timeout=timeout, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as err:
            last_err = err
            if attempt < max_retries:
                logger.warning(
                    "Network connection aborted during %s %s (attempt %d/%d): %s. Retrying in %.1fs...",
                    method.upper(),
                    url,
                    attempt,
                    max_retries,
                    err,
                    backoff_factor * attempt,
                )
                interruptible_sleep(backoff_factor * attempt, sleep_fn=time.sleep)
            else:
                break
    if last_err is not None:
        raise last_err
    raise RuntimeError(f"Unexpected failure executing {method} {url}")


class GitHubAppAuth:
    """Manages secure token generation and caching for GitHub Apps."""

    def __init__(
        self,
        app_id: int | str,
        installation_id: Optional[int | str] = None,
        private_key: str = "",
        api_base_url: str = "https://api.github.com",
    ) -> None:
        """Initialize GitHub App Auth.

        Args:
            app_id: Numeric GitHub App ID.
            installation_id: Numeric Installation ID (optional; auto-discovered if omitted).
            private_key: Path to PEM file, or raw PEM string content.
            api_base_url: Base API URL for GitHub (defaults to https://api.github.com).
        """
        self.app_id = int(app_id) if str(app_id).isdigit() else str(app_id)
        self.installation_id = int(installation_id) if installation_id else None
        self.api_base_url = validate_api_base_url(api_base_url)
        self._private_key_pem = self._load_private_key(private_key)
        self._token_cache: Dict[int, Tuple[str, float]] = {}
        self._token_lock = threading.RLock()
        self._discovery_lock = threading.Lock()
        self._org_to_installation: Dict[str, int] = {}
        self._installations_cache: Optional[List[Dict[str, Any]]] = None
        self._clock_skew: float = self._calculate_clock_skew()

    def set_installation_id(self, installation_id: int | str) -> None:
        """Set or update the active installation ID."""
        self.installation_id = int(installation_id)

    def discover_installations(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """List all active installations of this GitHub App across organizations using JWT auth.

        Args:
            force_refresh: Whether to ignore cached installation inventory.

        Returns:
            List of installation dictionaries from GitHub API.

        Raises:
            RuntimeError: If GitHub rejects the App credentials or the inventory cannot be retrieved.
        """
        with self._discovery_lock:
            if not force_refresh and self._installations_cache is not None:
                return self._installations_cache

            endpoint = f"{self.api_base_url}/app/installations"
            headers = {
                "Authorization": f"Bearer {self.generate_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": USER_AGENT,
            }

            all_installations: List[Dict[str, Any]] = []
            page = 1
            per_page = 100

            while True:
                try:
                    response = _request_with_retry(
                        "GET",
                        endpoint,
                        headers=headers,
                        params={"per_page": per_page, "page": page},
                        timeout=15,
                    )
                    response.raise_for_status()
                except requests.exceptions.RequestException as err:
                    status = getattr(getattr(err, "response", None), "status_code", None)
                    if status in (401, 403, 404):
                        raise RuntimeError(
                            f"GitHub rejected the App credentials (HTTP {status}) while listing installations. "
                            "Verify the App ID, private key, and API base URL, then run 'repo360 health'."
                        ) from err
                    raise RuntimeError(f"Failed to list GitHub App installations (page {page}): {err}") from err
                data = response.json()
                if not isinstance(data, list) or not data:
                    break
                all_installations.extend(data)
                if len(data) < per_page:
                    break
                page += 1

            for inst in all_installations:
                login = inst.get("account", {}).get("login", "")
                inst_id = inst.get("id")
                if login and inst_id:
                    self._org_to_installation[login.lower()] = int(inst_id)

            self._installations_cache = all_installations
            return all_installations

    def get_installation_id_for_org(self, org: str) -> Optional[int]:
        """Look up the installation ID for a specific organization login.

        Args:
            org: Organization name.

        Returns:
            Numeric installation ID if found, else None.
        """
        target_org = org.strip().lower()
        if target_org in self._org_to_installation:
            return self._org_to_installation[target_org]

        self.discover_installations(force_refresh=True)
        return self._org_to_installation.get(target_org)

    def get_installation_permissions(self, org: str) -> Dict[str, str]:
        """Return the active permissions dictionary for an organization's installation."""
        target = org.strip().lower()
        installations = self.discover_installations()
        for inst in installations:
            login = inst.get("account", {}).get("login", "").lower()
            if login == target:
                return inst.get("permissions", {})
        return {}

    def _calculate_clock_skew(self) -> float:
        """Calculate clock skew between local machine and GitHub API server.

        Prevents 401 Unauthorized errors caused by local machine clock drift.
        """
        try:
            resp = requests.head(self.api_base_url, timeout=5)
            server_date = resp.headers.get("Date")
            if server_date:
                server_timestamp = email.utils.parsedate_to_datetime(server_date).timestamp()
                skew = time.time() - server_timestamp
                logger.debug("Adjusted for local clock skew relative to GitHub: %.1f seconds", skew)
                return skew
        except Exception as err:
            logger.debug("Could not determine GitHub server clock skew: %s", err)
        return 0.0

    @staticmethod
    def _load_private_key(key_input: str) -> str:
        """Load private key from file path or raw string with safe validation.

        Args:
            key_input: File path or raw PEM/base64 string.

        Returns:
            Validated PEM private key string.
        """
        if os.path.isfile(key_input):
            try:
                with open(key_input, "r", encoding="utf-8") as key_file:
                    content = key_file.read().strip()
            except Exception as err:
                raise ValueError(f"Failed to read private key file: {err}") from err
        else:
            content = key_input.strip()

        # CI/CD secrets often supply base64-encoded PEM keys; a non-base64 value simply falls through to validation
        if not content.startswith("-----BEGIN"):
            with contextlib.suppress(ValueError):
                decoded = base64.b64decode(content, validate=False).decode("utf-8").strip()
                if decoded.startswith("-----BEGIN"):
                    content = decoded

        if "-----BEGIN" not in content or "PRIVATE KEY-----" not in content:
            raise ValueError(
                "Invalid private key format. Must be an RSA/PKCS#8 PEM private key."
            )

        return content

    def generate_jwt(self, expiration_seconds: int = 540) -> str:
        """Generate a short-lived JSON Web Token signed with RS256.

        Args:
            expiration_seconds: JWT lifetime in seconds (max 600, default 540 / 9 mins).

        Returns:
            Encoded JWT string.
        """
        now = int(time.time() - self._clock_skew)
        # GitHub allows a 60s JWT clock-drift leeway
        payload = {
            "iat": now - 60,
            "exp": now + expiration_seconds,
            "iss": str(self.app_id),
        }
        encoded_jwt = jwt.encode(
            payload,
            self._private_key_pem,
            algorithm="RS256",
        )
        return encoded_jwt

    def get_installation_token(
        self,
        installation_id: Optional[int | str] = None,
        org: Optional[str] = None,
        force_refresh: bool = False,
    ) -> str:
        """Obtain a valid installation access token, using cache if still valid.

        Supports multi-organization environments: resolves token by installation_id
        or organization name, with per-installation token caching.

        Args:
            installation_id: Specific Numeric Installation ID.
            org: Target organization login name.
            force_refresh: Whether to bypass cache and request a fresh token.

        Returns:
            Bearer token string (starts with 'ghs_').
        """
        target_id: Optional[int] = None
        if installation_id is not None:
            target_id = int(installation_id)
        elif org:
            target_id = self.get_installation_id_for_org(org)
            if not target_id:
                raise RuntimeError(
                    f"No GitHub App installation found for organization '{org}'. "
                    f"Ensure the GitHub App (ID: {self.app_id}) is installed on '{org}'."
                )
        elif self.installation_id is not None:
            target_id = self.installation_id
        else:
            installations = self.discover_installations()
            if not installations:
                raise RuntimeError(
                    f"No active installations found for GitHub App ID {self.app_id}. "
                    "Ensure an Organization Owner has installed the App on your GitHub organization."
                )
            target_id = int(installations[0]["id"])
            self.set_installation_id(target_id)

        with self._token_lock:
            now = time.time()
            if not force_refresh and target_id in self._token_cache:
                cached_token, expires_at = self._token_cache[target_id]
                if (expires_at - now) > 300:
                    return cached_token

            jwt_token = self.generate_jwt()
            endpoint = f"{self.api_base_url}/app/installations/{target_id}/access_tokens"
            headers = {
                "Authorization": f"Bearer {jwt_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": USER_AGENT,
            }

            try:
                response = _request_with_retry("POST", endpoint, headers=headers, timeout=15)
                response.raise_for_status()
                data: Dict[str, Any] = response.json()
                new_token: str = data["token"]
                expires_at = now + 3500

                self._token_cache[target_id] = (new_token, expires_at)

                if self.installation_id is None:
                    self.installation_id = target_id

                return new_token
            except requests.exceptions.RequestException as err:
                details = ""
                if getattr(err, "response", None) is not None:
                    details = f" - Details: {err.response.text[:300]}"
                logger.error(
                    "Failed to obtain installation token for App ID %s and Installation ID %s%s",
                    self.app_id,
                    target_id,
                    details,
                )
                raise RuntimeError(
                    f"GitHub App token exchange failed for installation {target_id}: {err}{details}"
                ) from err

    def get_auth_headers(
        self,
        installation_id: Optional[int | str] = None,
        org: Optional[str] = None,
        force_refresh: bool = False,
    ) -> Dict[str, str]:
        """Return standardized authorization headers for GitHub API requests.

        Args:
            installation_id: Optional target installation ID.
            org: Optional target organization name.
            force_refresh: Whether to force token renewal.
        """
        token = self.get_installation_token(
            installation_id=installation_id,
            org=org,
            force_refresh=force_refresh,
        )
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }

    def __repr__(self) -> str:
        """Safe representation redacting sensitive credentials."""
        return (
            f"<GitHubAppAuth app_id={self.app_id} "
            f"installation_id={self.installation_id} "
            f"endpoint={self.api_base_url} (credentials redacted)>"
        )
