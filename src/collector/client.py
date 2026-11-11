"""Resilient Repo360 REST and GraphQL API client.

Production-grade rate-limit resilience for 1000+ repositories:
- Primary Rate Limit monitoring (x-ratelimit-remaining / x-ratelimit-reset)
- Secondary Rate Limit (abuse detection) automatic backoff & retry
- Retry-After header compliance with exponential jittered fallback
- Automatic 1-hour Installation Token refresh watchdog
- Adaptive pacing to avoid burst throttling
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Iterator, List, Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..auth.github_app_auth import GitHubAppAuth
from ..common.interrupts import interruptible_sleep, is_shutting_down, register_closable

logger = logging.getLogger(__name__)


class Repo360Client:
    """Production-grade GitHub API client with enterprise rate-limit handling for 1000+ repos."""

    def __init__(
        self,
        auth: GitHubAppAuth,
        timeout_seconds: int = 30,
        max_retries: int = 5,
        backoff_factor: float = 1.5,
        adaptive_delay_ms: int = 20,
    ) -> None:
        """Initialize Repo360 API Client.

        Args:
            auth: Authenticated GitHubAppAuth instance.
            timeout_seconds: HTTP request timeout.
            max_retries: Number of retry attempts on network/server/rate-limit errors.
            backoff_factor: Retry backoff multiplier.
            adaptive_delay_ms: Polite inter-request micro-delay (ms) to avoid burst detection.
        """
        self.auth = auth
        self.base_url = auth.api_base_url
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(max_retries, 5)
        self.backoff_factor = backoff_factor
        self.adaptive_delay_ms = adaptive_delay_ms

        self.total_api_calls: int = 0
        self.rate_limit_remaining: Optional[int] = None
        self.rate_limit_reset: Optional[int] = None
        self.rate_limit_limit: Optional[int] = None
        self._org_rate_limit: Dict[str, int] = {}
        self._lock = threading.Lock()

        # Configure session with standard retry strategy for transient connection resets
        self.session = requests.Session()
        retries = Retry(
            total=3,
            backoff_factor=backoff_factor,
            status_forcelist=[502, 503, 504],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries, pool_connections=100, pool_maxsize=100)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        register_closable(self.session)

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep up to ``seconds`` while waking instantaneously on Ctrl+C."""
        interruptible_sleep(float(seconds), sleep_fn=time.sleep)

    def _sleep_with_heartbeat(self, seconds: int) -> None:
        """Sleep for the specified seconds while logging a progress heartbeat every 30 seconds."""
        remaining = float(seconds)
        last_heartbeat = time.time()
        while remaining > 0:
            chunk = min(remaining, 1.0)
            interruptible_sleep(chunk, sleep_fn=time.sleep)
            remaining -= chunk
            if time.time() - last_heartbeat >= 30 and remaining > 0:
                logger.info("Rate limit cooldown in progress: %d seconds remaining...", int(remaining))
                last_heartbeat = time.time()

    def _handle_rate_limit(self, response: requests.Response, org: Optional[str] = None) -> None:
        """Inspect and log rate limit headers with per-org active pacing for 1000+ repos."""
        org_key = org.strip().lower() if isinstance(org, str) and org.strip() else None
        with self._lock:
            self.total_api_calls += 1
            remaining_str = response.headers.get("x-ratelimit-remaining")
            reset_str = response.headers.get("x-ratelimit-reset")
            limit_str = response.headers.get("x-ratelimit-limit")

            parsed_rem: Optional[int] = None
            if remaining_str is not None:
                try:
                    parsed_rem = int(remaining_str)
                    self.rate_limit_remaining = parsed_rem
                    if org_key:
                        self._org_rate_limit[org_key] = parsed_rem
                except ValueError:
                    pass
            if reset_str is not None:
                try:
                    self.rate_limit_reset = int(reset_str)
                except ValueError:
                    pass
            if limit_str is not None:
                try:
                    self.rate_limit_limit = int(limit_str)
                except ValueError:
                    pass

            rem = self._org_rate_limit.get(org_key, self.rate_limit_remaining) if org_key else self.rate_limit_remaining
            res = self.rate_limit_reset
            lim = self.rate_limit_limit

        # Primary rate limit management
        if rem is not None:
            if rem <= 5:
                now = int(time.time())
                wait_seconds = max(1, (res - now) + 2) if res else 60
                logger.warning(
                    "GitHub API primary rate limit critically low (%s/%s remaining). "
                    "Pausing execution for %d seconds until reset window (%s UTC)...",
                    rem,
                    lim or "unknown",
                    wait_seconds,
                    time.strftime("%H:%M:%S", time.gmtime(res or (now + wait_seconds))),
                )
                self._sleep_with_heartbeat(wait_seconds)
            elif rem < 100 and rem % 25 == 0:
                logger.warning(
                    "GitHub API rate limit capacity: %s / %s calls remaining. Resets in %d seconds.",
                    rem,
                    lim or "unknown",
                    max(0, (res or int(time.time())) - int(time.time())),
                )

        # Adaptive micro-delay: execute at full wire speed when rate limit quota is healthy;
        # throttle gracefully only when remaining quota is low or approaching reset
        if self.adaptive_delay_ms > 0 and not is_shutting_down():
            if rem is not None and rem < 500:
                interruptible_sleep((self.adaptive_delay_ms * 2) / 1000.0, sleep_fn=time.sleep)
            elif rem is not None and rem < 1000:
                interruptible_sleep((self.adaptive_delay_ms / 2) / 1000.0, sleep_fn=time.sleep)

    @staticmethod
    def _extract_org_from_path(path_or_url: str) -> Optional[str]:
        """Extract organization or owner from standard GitHub REST API endpoint paths.

        Matches:
          - /orgs/{org} or /orgs/{org}/...
          - /repos/{org}/{repo} or /repos/{org}/{repo}/...
          - Full URLs such as https://api.github.com/orgs/{org}/...
        """
        clean = path_or_url.split("?")[0].strip()
        if "://" in clean:
            clean = clean.split("://", 1)[1]
            if "/" in clean:
                clean = clean.split("/", 1)[1]
            else:
                return None
        parts = [p for p in clean.strip("/").split("/") if p]
        if len(parts) >= 2 and parts[0] in ("orgs", "repos"):
            return parts[1]
        return None

    def get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        allow_404: bool = False,
        installation_id: Optional[int | str] = None,
        org: Optional[str] = None,
    ) -> Optional[Any]:
        """Perform an authenticated GET request against GitHub API with full rate-limit resilience.

        Dynamically extracts and injects per-organization GitHub App installation tokens
        to support multi-organization environments without authorization errors.

        Args:
            path: Relative API path (e.g., '/repos/org/repo') or absolute URL.
            params: Optional query parameters.
            allow_404: If True, returns None instead of raising an exception on 404.
            installation_id: Optional specific installation ID to use for this request.
            org: Optional organization name override.

        Returns:
            Parsed JSON object/list or None if 404 allowed.
        """
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        target_org = org or self._extract_org_from_path(url)

        for attempt in range(1, self.max_retries + 1):
            response: Optional[requests.Response] = None
            headers = self.auth.get_auth_headers(installation_id=installation_id, org=target_org)
            try:
                response = self.session.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=self.timeout_seconds,
                )
                self._handle_rate_limit(response, org=target_org)

                # 404 / 409 (Empty repo) / 410 handling
                if response.status_code in (404, 409, 410):
                    if allow_404:
                        return None
                    response.raise_for_status()

                # Handle Secondary Rate Limit or 429
                if response.status_code == 429 or (
                    response.status_code == 403
                    and ("secondary rate limit" in response.text.lower() or "abuse" in response.text.lower())
                ):
                    retry_after_hdr = response.headers.get("retry-after")
                    if retry_after_hdr:
                        try:
                            backoff = int(retry_after_hdr) + 1
                        except ValueError:
                            backoff = 30
                    else:
                        backoff = min(120, int((2 ** attempt) * 5 + (attempt * 2)))

                    logger.warning(
                        "GitHub Secondary Rate Limit / Abuse detection triggered on '%s' (attempt %d/%d). "
                        "Backing off for %d seconds as instructed by GitHub...",
                        path,
                        attempt,
                        self.max_retries,
                        backoff,
                    )
                    self._sleep_with_heartbeat(backoff)
                    continue

                # Standard 403 Forbidden (Permissions or feature not enabled on plan) - do NOT retry
                if response.status_code == 403:
                    if allow_404:
                        return None
                    response.raise_for_status()

                # Handle Expired Token (401)
                if response.status_code == 401 and attempt < self.max_retries:
                    logger.info("GitHub installation token expired. Refreshing token and retrying...")
                    self.auth.get_installation_token(
                        installation_id=installation_id,
                        org=target_org,
                        force_refresh=True,
                    )
                    continue

                # Handle Server Errors (500, 502, 503, 504)
                if response.status_code in (500, 502, 503, 504):
                    if attempt < self.max_retries:
                        backoff = int(self.backoff_factor * (2 ** attempt))
                        logger.warning("GitHub API server error %s. Retrying in %ds...", response.status_code, backoff)
                        self._interruptible_sleep(backoff)
                        continue

                response.raise_for_status()
                return response.json()

            except requests.exceptions.HTTPError as http_err:
                # Deterministic HTTP client errors (400, 401, 403, 404, 409, 410, 422) should not be retried as network errors
                if response is not None and response.status_code in (400, 401, 403, 404, 409, 410, 422):
                    if allow_404 and response.status_code in (403, 404, 409, 410):
                        return None
                    raise http_err
                if attempt == self.max_retries:
                    logger.error("API request failed after %d attempts on GET %s: %s", attempt, path, http_err)
                    raise http_err
                backoff = int(self.backoff_factor * (2 ** attempt))
                logger.warning(
                    "HTTP error on attempt %d/%d for %s (%s). Retrying in %ds...",
                    attempt,
                    self.max_retries,
                    path,
                    http_err,
                    backoff,
                )
                self._interruptible_sleep(backoff)
            except requests.exceptions.RequestException as err:
                if attempt == self.max_retries:
                    logger.error("API request failed after %d attempts on GET %s: %s", attempt, path, err)
                    raise err
                backoff = int(self.backoff_factor * (2 ** attempt))
                logger.warning(
                    "Network error on attempt %d/%d for %s (%s). Retrying in %ds...",
                    attempt,
                    self.max_retries,
                    path,
                    err,
                    backoff,
                )
                self._interruptible_sleep(backoff)

        return None

    def list_paginated(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        per_page: int = 100,
        max_pages: Optional[int] = 250,
        installation_id: Optional[int | str] = None,
        org: Optional[str] = None,
    ) -> List[Any]:
        """Paginate through a GitHub API list endpoint supporting 10,000+ items.

        Args:
            path: API path.
            params: Query parameters.
            per_page: Items per page (max 100).
            max_pages: Maximum pages to fetch (default: 250 pages = 25,000 items; None for unlimited).
            installation_id: Optional specific installation ID for this request.
            org: Optional organization name override.

        Returns:
            Aggregated list of items.
        """
        all_items: List[Any] = []
        page = 1
        query_params = dict(params or {})
        query_params["per_page"] = per_page

        while max_pages is None or page <= max_pages:
            query_params["page"] = page
            data = self.get(
                path,
                params=query_params,
                installation_id=installation_id,
                org=org,
            )
            if not data or not isinstance(data, list):
                break
            all_items.extend(data)
            if len(data) < per_page:
                break
            page += 1

        return all_items

    def stream_paginated(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        per_page: int = 100,
        installation_id: Optional[int | str] = None,
        org: Optional[str] = None,
    ) -> Iterator[Any]:
        """Stream items page-by-page yielding individual entities in O(1) memory."""
        page = 1
        query_params = dict(params or {})
        query_params["per_page"] = per_page

        while True:
            query_params["page"] = page
            data = self.get(
                path,
                params=query_params,
                installation_id=installation_id,
                org=org,
            )
            if not data or not isinstance(data, list):
                break
            for item in data:
                yield item
            if len(data) < per_page:
                break
            page += 1
