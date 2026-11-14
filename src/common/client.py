"""Production-grade resilient GitHub REST and GraphQL API client for multi-threaded scrapers."""

from __future__ import annotations

import logging
import random
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..auth.github_app_auth import GitHubAppAuth
from .interrupts import interruptible_sleep, is_shutting_down, register_closable

logger = logging.getLogger("repo360.client")
EMPTY_RESOURCE_STATUSES = frozenset({409, 410})


class ResilientGitHubClient:
    """Thread-safe REST & GraphQL client with proactive cross-thread rate-limit coordination."""

    def __init__(
        self,
        auth: GitHubAppAuth,
        timeout_seconds: int = 30,
        max_retries: int = 5,
        rate_limit_buffer: int = 25,
        inter_request_delay_seconds: float = 0.0,
        max_rate_limit_wait_seconds: int = 3600,
    ) -> None:
        self.auth = auth
        self.api_base_url = auth.api_base_url.rstrip("/")
        self.host = urlparse(self.api_base_url).netloc
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.rate_limit_buffer = rate_limit_buffer
        self.inter_request_delay_seconds = inter_request_delay_seconds
        self.max_rate_limit_wait_seconds = max_rate_limit_wait_seconds

        self._local = threading.local()
        self._rate_limit_lock = threading.Lock()
        self._global_pause_until: float = 0.0
        self._org_pause_until: Dict[str, float] = {}
        self._org_remaining: Dict[str, int] = {}
        self._org_reset: Dict[str, float] = {}
        self._org_last_request_time: Dict[str, float] = {}
        self._last_request_time: float = 0.0
        self._last_rate_limit_warn_time: Dict[str, float] = {}

    def _http(self) -> requests.Session:
        """Thread-local session instance with connection retry pooling."""
        session = getattr(self._local, "session", None)
        if session is None:
            retry = Retry(
                total=self.max_retries,
                backoff_factor=1.5,
                status_forcelist=(500, 502, 503, 504),
                allowed_methods=frozenset({"GET", "POST", "HEAD"}),
                raise_on_status=False,
            )
            session = requests.Session()
            adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            session.headers.update({
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Repo360-Platform/1.0",
            })
            register_closable(session)
            self._local.session = session
        return session

    def _coordinate_rate_limit(self, org: Optional[str]) -> None:
        """Coordinate rate-limit delays and pacing across all concurrent worker threads."""
        org_key = org.lower() if org else None
        pacing_wait = 0.0

        while True:
            if is_shutting_down():
                raise KeyboardInterrupt
            with self._rate_limit_lock:
                now = time.time()
                pause_wait = 0.0

                if self._global_pause_until > now:
                    pause_wait = max(pause_wait, self._global_pause_until - now)

                if org_key:
                    rst = self._org_reset.get(org_key)
                    if rst is not None and now >= rst:
                        self._org_remaining.pop(org_key, None)
                        self._org_reset.pop(org_key, None)
                        self._org_pause_until.pop(org_key, None)

                    if self._org_pause_until.get(org_key, 0.0) > now:
                        pause_wait = max(pause_wait, self._org_pause_until[org_key] - now)
                    elif pause_wait == 0.0:
                        remaining = self._org_remaining.get(org_key)
                        if remaining is not None and remaining <= self.rate_limit_buffer:
                            reset_at = self._org_reset.get(org_key, now + 60.0)
                            if reset_at > now:
                                needed = (reset_at - now) + 2.0
                                if needed <= self.max_rate_limit_wait_seconds:
                                    self._org_pause_until[org_key] = now + needed
                                    pause_wait = needed
                                    if now - self._last_rate_limit_warn_time.get(org_key, 0.0) > 5.0:
                                        self._last_rate_limit_warn_time[org_key] = now
                                        logger.warning(
                                            "Proactive rate limit pause for %s: %d calls remaining; waiting %.1fs until reset",
                                            org, remaining, needed
                                        )
                                else:
                                    logger.error(
                                        "Rate limit reset for %s is %.0fs away, exceeding max limit %ds",
                                        org, needed, self.max_rate_limit_wait_seconds
                                    )
                            else:
                                self._org_remaining.pop(org_key, None)
                                self._org_reset.pop(org_key, None)

                if pause_wait > 0.0:
                    pass
                else:
                    scope_key = org_key or "__global__"
                    if self.inter_request_delay_seconds > 0.0:
                        delay = self.inter_request_delay_seconds
                    elif org_key and org_key in self._org_remaining:
                        rem_quota = self._org_remaining[org_key]
                        if rem_quota < 300:
                            delay = 0.40
                        elif rem_quota < 1000:
                            delay = 0.15
                        elif rem_quota < 2500:
                            delay = 0.05
                        else:
                            delay = 0.0
                    else:
                        delay = 0.0

                    if delay > 0.0:
                        last_ts = self._org_last_request_time.get(scope_key, 0.0)
                        if now < last_ts + delay:
                            pacing_wait = min((last_ts + delay) - now, delay)
                            self._org_last_request_time[scope_key] = now + pacing_wait
                        else:
                            pacing_wait = 0.0
                            self._org_last_request_time[scope_key] = now
                        self._last_request_time = self._org_last_request_time[scope_key]
                    else:
                        pacing_wait = 0.0
                    break

            interruptible_sleep(min(pause_wait, 5.0), sleep_fn=time.sleep)

        if pacing_wait > 0.0:
            interruptible_sleep(pacing_wait, sleep_fn=time.sleep)

    def _next_url(self, resp: requests.Response) -> Optional[str]:
        """Extract next pagination URL, rejecting foreign host redirections."""
        url = resp.links.get("next", {}).get("url")
        if url and urlparse(url).netloc != self.host:
            raise RuntimeError(f"Refusing to follow pagination link to foreign host: {urlparse(url).netloc}")
        return url

    def _send(
        self,
        method: str,
        path: str,
        org: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        headers_override: Optional[Dict[str, str]] = None,
    ) -> requests.Response:
        """Perform request with automatic token resolution, retries, and rate limit handling."""
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = f"{self.api_base_url}/{path.lstrip('/')}"

        org_key = org.lower() if org else None

        for attempt in range(self.max_retries + 1):
            if is_shutting_down():
                raise KeyboardInterrupt
            self._coordinate_rate_limit(org)

            headers = self.auth.get_auth_headers(org=org)
            if headers_override:
                headers.update(headers_override)

            try:
                resp = self._http().request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_data,
                    timeout=self.timeout_seconds,
                )
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError) as net_err:
                if attempt == self.max_retries:
                    logger.error("Network request failed after %d retries for %s: %s", self.max_retries, url, net_err)
                    raise
                backoff = min(30.0, (1.5 ** attempt) + random.uniform(0.1, 0.5))
                logger.warning(
                    "Transient network drop (%s) for %s; retrying in %.2fs (attempt %d/%d)",
                    type(net_err).__name__, url, backoff, attempt + 1, self.max_retries
                )
                interruptible_sleep(backoff, sleep_fn=time.sleep)
                continue

            rem = resp.headers.get("X-RateLimit-Remaining")
            rst = resp.headers.get("X-RateLimit-Reset")
            with self._rate_limit_lock:
                if org_key:
                    if rem is not None and rem.isdigit():
                        self._org_remaining[org_key] = int(rem)
                    if rst is not None and rst.isdigit():
                        self._org_reset[org_key] = float(rst)

            wait = self._rate_limit_wait(resp)
            if wait is None:
                return resp

            if attempt == self.max_retries:
                break

            is_secondary = (
                resp.status_code == 429
                or "secondary rate limit" in resp.text.lower()
                or "abuse detection" in resp.text.lower()
                or "please wait" in resp.text.lower()
            )
            jitter = random.uniform(0.5, 2.0)
            actual_wait = wait + jitter

            with self._rate_limit_lock:
                now = time.time()
                if is_secondary or not org_key:
                    backoff_wait = max(actual_wait, (2 ** attempt) * 15.0 + jitter)
                    self._global_pause_until = max(self._global_pause_until, now + backoff_wait)
                    if now - self._last_rate_limit_warn_time.get("__global__", 0.0) > 5.0:
                        self._last_rate_limit_warn_time["__global__"] = now
                        logger.warning(
                            "Secondary rate limit hit (HTTP %s) on %s; pausing all requests for %.1fs (attempt %d/%d)",
                            resp.status_code, urlparse(url).path, backoff_wait, attempt + 1, self.max_retries
                        )
                    actual_wait = backoff_wait
                else:
                    self._org_pause_until[org_key] = max(self._org_pause_until.get(org_key, 0.0), now + actual_wait)
                    if now - self._last_rate_limit_warn_time.get(org_key, 0.0) > 5.0:
                        self._last_rate_limit_warn_time[org_key] = now
                        logger.warning(
                            "Installation rate limit hit for %s (HTTP %s); pausing org for %.1fs (attempt %d/%d)",
                            org, resp.status_code, actual_wait, attempt + 1, self.max_retries
                        )

            interruptible_sleep(actual_wait, sleep_fn=time.sleep)

        resp.raise_for_status()
        return resp

    @staticmethod
    def _rate_limit_wait(resp: requests.Response) -> Optional[int]:
        if resp.status_code not in (403, 429):
            return None
        retry_after = resp.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return int(retry_after) + 1
        text = resp.text.lower()
        if "secondary rate limit" in text or "abuse detection" in text or "please wait" in text:
            return 60
        if resp.headers.get("X-RateLimit-Remaining") == "0":
            reset = int(resp.headers.get("X-RateLimit-Reset", "0"))
            try:
                server_date = parsedate_to_datetime(resp.headers["Date"])
                now = server_date.timestamp()
            except (KeyError, TypeError, ValueError, OverflowError):
                now = time.time()
            wait = reset - int(now)
            return wait + 2 if wait > 0 else 60
        return None

    def get(self, org: str, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Perform authenticated GET request returning parsed JSON."""
        resp = self._send("GET", path, org=org, params=params)
        resp.raise_for_status()
        return resp.json()

    def get_response(self, org: str, path: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
        """Perform authenticated GET request returning raw Response."""
        return self._send("GET", path, org=org, params=params)

    def paginate(self, org: str, path: str, params: Optional[Dict[str, Any]] = None) -> Iterator[Dict[str, Any]]:
        """Paginate through a standard GitHub list endpoint."""
        url: Optional[str] = path
        query: Optional[Dict[str, Any]] = {**(params or {}), "per_page": 100}
        while url:
            resp = self._send("GET", url, org=org, params=query)
            if resp.status_code in EMPTY_RESOURCE_STATUSES:
                return
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                yield from data
            elif isinstance(data, dict):
                logger.warning("paginate received dict response from %s instead of list: keys=%s", url, list(data.keys()))
            url, query = self._next_url(resp), None

    def paginate_enveloped(
        self, org: str, path: str, envelope_key: str, params: Optional[Dict[str, Any]] = None
    ) -> Iterator[Dict[str, Any]]:
        """Paginate through an enveloped GitHub endpoint (e.g. runner-groups, workflow_runs)."""
        url: Optional[str] = path
        query: Optional[Dict[str, Any]] = {**(params or {}), "per_page": 100}
        while url:
            resp = self._send("GET", url, org=org, params=query)
            if resp.status_code in EMPTY_RESOURCE_STATUSES:
                return
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                items = data.get(envelope_key, [])
                if isinstance(items, list):
                    yield from items
            elif isinstance(data, list):
                yield from data
            url, query = self._next_url(resp), None

    def graphql(self, org: str, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Execute a GraphQL query using the organization's installation token."""
        if self.api_base_url.endswith("/api/v3"):
            graphql_url = f"{self.api_base_url[:-7]}/api/graphql"
        else:
            graphql_url = f"{self.api_base_url}/graphql"

        payload = {"query": query, "variables": variables or {}}
        headers = {"Content-Type": "application/json"}
        resp = self._send("POST", graphql_url, org=org, json_data=payload, headers_override=headers)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "errors" in data and not data.get("data"):
            raise RuntimeError(f"GitHub GraphQL error: {data['errors']}")
        return data
