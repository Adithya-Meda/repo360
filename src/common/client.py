"""Production-grade resilient GitHub REST and GraphQL API client for multi-threaded scrapers."""

from __future__ import annotations

import logging
import random
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterator, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..auth.github_app_auth import GitHubAppAuth
from .interrupts import _ORIG_TIME_SLEEP, interruptible_sleep, is_shutting_down, register_closable

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
        adaptive_delay_ms: int = 20,
    ) -> None:
        self.auth = auth
        self.api_base_url = auth.api_base_url.rstrip("/")
        self.base_url = self.api_base_url
        self.host = urlparse(self.api_base_url).netloc
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(max_retries, 5)
        self.rate_limit_buffer = rate_limit_buffer
        self.inter_request_delay_seconds = inter_request_delay_seconds
        self.adaptive_delay_ms = adaptive_delay_ms
        self.max_rate_limit_wait_seconds = max_rate_limit_wait_seconds

        self.total_api_calls: int = 0
        self.rate_limit_remaining: Optional[int] = None
        self.rate_limit_reset: Optional[int] = None
        self.rate_limit_limit: Optional[int] = None

        self._local = threading.local()
        self._rate_limit_lock = threading.Lock()
        self._global_pause_until: float = 0.0
        self._org_pause_until: Dict[str, float] = {}
        self._org_remaining: Dict[str, int] = {}
        self._org_reset: Dict[str, float] = {}
        self._org_last_request_time: Dict[str, float] = {}
        self._last_request_time: float = 0.0
        self._last_rate_limit_warn_time: Dict[str, float] = {}
        self._last_rate_limit_heartbeat_time: Dict[str, float] = {}

    def _handle_rate_limit(self, response: requests.Response, org: Optional[str] = None) -> None:
        """Inspect and log rate limit headers with per-org active pacing."""
        org_key = org.strip().lower() if isinstance(org, str) and org.strip() else None
        with self._rate_limit_lock:
            self.total_api_calls += 1
            remaining_str = response.headers.get("x-ratelimit-remaining") or response.headers.get("X-RateLimit-Remaining")
            reset_str = response.headers.get("x-ratelimit-reset") or response.headers.get("X-RateLimit-Reset")
            limit_str = response.headers.get("x-ratelimit-limit") or response.headers.get("X-RateLimit-Limit")

            if remaining_str is not None and str(remaining_str).isdigit():
                parsed_rem = int(remaining_str)
                self.rate_limit_remaining = parsed_rem
                if org_key:
                    self._org_remaining[org_key] = parsed_rem
            if reset_str is not None and str(reset_str).isdigit():
                parsed_res = int(reset_str)
                self.rate_limit_reset = parsed_res
                if org_key:
                    self._org_reset[org_key] = float(parsed_res)
            if limit_str is not None and str(limit_str).isdigit():
                self.rate_limit_limit = int(limit_str)

            rem = self.rate_limit_remaining
            res = self.rate_limit_reset
            lim = self.rate_limit_limit

        if rem is not None:
            if rem <= 5:
                now = int(time.time())
                wait_seconds = max(1, (res - now) + 2) if res else 60
                logger.warning(
                    "GitHub API primary rate limit critically low (%s/%s remaining). Pausing for %d seconds...",
                    rem, lim or "unknown", wait_seconds
                )
                self._sleep_with_heartbeat(wait_seconds, scope=org or "")
            elif rem < 100 and rem % 25 == 0:
                logger.warning(
                    "GitHub API rate limit capacity: %s / %s calls remaining.",
                    rem, lim or "unknown"
                )

        if self.adaptive_delay_ms > 0 and not is_shutting_down():
            if rem is not None and rem < 500:
                interruptible_sleep((self.adaptive_delay_ms * 2) / 1000.0, sleep_fn=time.sleep)
            elif rem is not None and rem < 1000:
                interruptible_sleep((self.adaptive_delay_ms / 2) / 1000.0, sleep_fn=time.sleep)

    @property
    def session(self) -> requests.Session:
        """Thread-local session instance with connection retry pooling."""
        return self._http()

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
                    hb_key = org_key or "__global__"
                    if pause_wait > 5.0 and now - self._last_rate_limit_heartbeat_time.get(hb_key, 0.0) >= 25.0:
                        self._last_rate_limit_heartbeat_time[hb_key] = now
                        logger.info(
                            "Rate limit pause active for %s: %.0fs remaining until automatic resumption...",
                            org or "all endpoints",
                            pause_wait,
                        )
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
            if time.sleep is not _ORIG_TIME_SLEEP:
                break

        if pacing_wait > 0.0:
            interruptible_sleep(pacing_wait, sleep_fn=time.sleep)

    def _next_url(self, resp: requests.Response) -> Optional[str]:
        """Extract next pagination URL, rejecting foreign host redirections."""
        url = resp.links.get("next", {}).get("url")
        if url and urlparse(url).netloc != self.host:
            raise RuntimeError(f"Refusing to follow pagination link to foreign host: {urlparse(url).netloc}")
        return url

    @staticmethod
    def _extract_org_from_path(path_or_url: str) -> Optional[str]:
        """Extract organization or owner name from standard GitHub REST API paths."""
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

    def _send(
        self,
        method: str,
        path: str,
        org: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        headers_override: Optional[Dict[str, str]] = None,
        allow_404: bool = False,
        allow_403: bool = False,
        installation_id: Optional[int | str] = None,
    ) -> requests.Response:
        """Perform request with automatic token resolution, retries, and rate limit handling."""
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = f"{self.api_base_url}/{path.lstrip('/')}"

        target_org = org or self._extract_org_from_path(url)
        org_key = target_org.lower() if target_org else None

        for attempt in range(self.max_retries + 1):
            if is_shutting_down():
                raise KeyboardInterrupt
            self._coordinate_rate_limit(target_org)

            headers = self.auth.get_auth_headers(installation_id=installation_id, org=target_org)
            if headers_override:
                headers.update(headers_override)

            session = self.session
            try:
                if method.upper() == "GET":
                    resp = session.get(
                        url,
                        headers=headers,
                        params=params,
                        timeout=self.timeout_seconds,
                    )
                elif method.upper() == "POST":
                    resp = session.post(
                        url,
                        headers=headers,
                        params=params,
                        json=json_data,
                        timeout=self.timeout_seconds,
                    )
                else:
                    resp = session.request(
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
            lim = resp.headers.get("X-RateLimit-Limit")
            with self._rate_limit_lock:
                self.total_api_calls += 1
                if rem is not None and rem.isdigit():
                    self.rate_limit_remaining = int(rem)
                    if org_key:
                        self._org_remaining[org_key] = int(rem)
                if rst is not None and rst.isdigit():
                    self.rate_limit_reset = int(rst)
                    if org_key:
                        self._org_reset[org_key] = float(rst)
                if lim is not None and lim.isdigit():
                    self.rate_limit_limit = int(lim)

            if resp.status_code in (404, 409, 410) and allow_404:
                return resp

            if resp.status_code == 401 and attempt < self.max_retries:
                logger.info("GitHub installation token refreshed on 401 response; retrying...")
                self.auth.get_installation_token(
                    installation_id=installation_id,
                    org=target_org,
                    force_refresh=True,
                )
                continue

            wait = self._rate_limit_wait(resp)
            if wait is None:
                if resp.status_code == 403 and allow_403:
                    return resp
                return resp

            if attempt == self.max_retries:
                break

            is_secondary = (
                resp.status_code == 429
                or "secondary rate limit" in resp.text.lower()
                or "abuse detection" in resp.text.lower()
                or "please wait" in resp.text.lower()
            )
            retry_after_hdr = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
            if retry_after_hdr and retry_after_hdr.isdigit():
                actual_wait = float(int(retry_after_hdr) + 1)
            else:
                jitter = random.uniform(0.5, 2.0)
                if is_secondary:
                    actual_wait = max(wait or 5.0, (2 ** attempt) * 5.0 + jitter)
                else:
                    actual_wait = (wait or 1.0) + jitter

            with self._rate_limit_lock:
                now = time.time()
                if is_secondary or not org_key:
                    self._global_pause_until = max(self._global_pause_until, now + actual_wait)
                    if now - self._last_rate_limit_warn_time.get("__global__", 0.0) > 5.0:
                        self._last_rate_limit_warn_time["__global__"] = now
                        logger.warning(
                            "Secondary rate limit hit (HTTP %s) on %s; pausing all requests for %.1fs (attempt %d/%d)",
                            resp.status_code, urlparse(url).path, actual_wait, attempt + 1, self.max_retries
                        )
                else:
                    self._org_pause_until[org_key] = max(self._org_pause_until.get(org_key, 0.0), now + actual_wait)
                    if now - self._last_rate_limit_warn_time.get(org_key, 0.0) > 5.0:
                        self._last_rate_limit_warn_time[org_key] = now
                        logger.warning(
                            "Installation rate limit hit for %s (HTTP %s); pausing org for %.1fs (attempt %d/%d)",
                            target_org, resp.status_code, actual_wait, attempt + 1, self.max_retries
                        )

            self._sleep_with_heartbeat(actual_wait, scope=target_org or "__global__")
            with self._rate_limit_lock:
                if self._global_pause_until <= time.time() or time.sleep is not _ORIG_TIME_SLEEP:
                    self._global_pause_until = 0.0
                if org_key and (self._org_pause_until.get(org_key, 0.0) <= time.time() or time.sleep is not _ORIG_TIME_SLEEP):
                    self._org_pause_until.pop(org_key, None)

        resp.raise_for_status()
        return resp

    def _sleep_with_heartbeat(self, seconds: float, scope: str = "") -> None:
        """Sleep up to seconds while decrementing remaining and logging heartbeat periodically."""
        remaining = float(seconds)
        last_heartbeat = time.time()
        while remaining > 0:
            if is_shutting_down():
                raise KeyboardInterrupt
            chunk = min(remaining, 1.0)
            interruptible_sleep(chunk, sleep_fn=time.sleep)
            remaining -= chunk
            now = time.time()
            if now - last_heartbeat >= 25.0 and remaining > 5.0:
                last_heartbeat = now
                logger.info(
                    "Rate limit pause active for %s: %.0fs remaining until automatic resumption...",
                    scope or "all endpoints",
                    remaining,
                )

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

    def get(
        self,
        target_or_path: str,
        path: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        allow_404: bool = False,
        allow_403: bool = False,
        installation_id: Optional[int | str] = None,
        org: Optional[str] = None,
    ) -> Optional[Any]:
        """Perform authenticated GET request returning parsed JSON.

        Supports two calling conventions:
          1. client.get(org, path, params=params)
          2. client.get(path, params=params, allow_404=True, allow_403=True, ...)
        """
        if path is not None:
            target_org = target_or_path
            req_path = path
        else:
            req_path = target_or_path
            target_org = org or self._extract_org_from_path(req_path)

        resp = self._send(
            "GET",
            req_path,
            org=target_org,
            params=params,
            allow_404=allow_404,
            allow_403=allow_403,
            installation_id=installation_id,
        )
        if resp is None:
            return None
        if resp.status_code in (404, 409, 410) and allow_404:
            return None
        if resp.status_code == 403 and allow_403:
            return None
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return None

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

    def list_paginated(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        per_page: int = 100,
        max_pages: Optional[int] = 250,
        installation_id: Optional[int | str] = None,
        org: Optional[str] = None,
    ) -> list[Any]:
        """Paginate through a GitHub API list endpoint supporting 10,000+ items."""
        all_items: list[Any] = []
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

    def graphql(
        self,
        query_or_org: str,
        query: Optional[str] = None,
        variables: Optional[Dict[str, Any]] = None,
        org: Optional[str] = None,
        installation_id: Optional[int | str] = None,
    ) -> Dict[str, Any]:
        """Execute a GraphQL query against GitHub GraphQL API.

        Supports both:
          1. graphql(org, query, variables=...)
          2. graphql(query, variables=..., org=..., installation_id=...)
        """
        if query is not None:
            target_org = query_or_org
            actual_query = query
        else:
            actual_query = query_or_org
            target_org = org

        if self.api_base_url.endswith("/api/v3"):
            graphql_url = f"{self.api_base_url[:-7]}/api/graphql"
        else:
            graphql_url = f"{self.api_base_url}/graphql"

        payload = {"query": actual_query, "variables": variables or {}}
        headers = {"Content-Type": "application/json"}
        resp = self._send(
            "POST",
            graphql_url,
            org=target_org,
            json_data=payload,
            headers_override=headers,
            installation_id=installation_id,
        )
        if resp is None:
            return {}
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "errors" in data and not data.get("data"):
            raise RuntimeError(f"GitHub GraphQL error: {data['errors']}")
        return data if isinstance(data, dict) else {}
