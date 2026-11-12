"""Enterprise AI Synthesis & Remediation Engine.

Supports multiple LLM providers:
1. Google GenAI (Gemini) - native SDK integration
2. OpenAI / Azure OpenAI - standard REST Chat Completions
3. Anthropic (Claude) - standard REST Messages API

Capabilities:
- Synthesize Executive Risk Briefings from multi-repository audit findings.
- Tailor remediation snippets (Copilot instructions, DevContainers, Dependabot)
  specifically to each repository's detected language and framework stack.
- Sanitize all prompt inputs to prevent credential or proprietary code leakage.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
import warnings
from typing import Any, Callable, Dict, List, Optional, Tuple

warnings.filterwarnings("ignore", category=SyntaxWarning)
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*invalid escape sequence.*")
warnings.filterwarnings("ignore", message=".*automatic function calling.*")

from ..checks.base import CheckResult, CheckStatus

logger = logging.getLogger(__name__)


class AIEngine:
    """Safe, resilient multi-provider LLM integration for governance analysis and remediation."""

    SENSITIVE_PATTERN = re.compile(
        r"(ghp_[a-zA-Z0-9]{36}|ghs_[a-zA-Z0-9]{36}|-----BEGIN[^\-]+-----|bearer\s+[a-zA-Z0-9_\-\.]+)",
        re.IGNORECASE,
    )

    FALLBACK_MODELS_BY_PROVIDER: Dict[str, Tuple[str, ...]] = {
        "gemini": (
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.8-flash",
            "gemini-flash-latest",
        ),
        "openai": (
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4-turbo",
        ),
        "anthropic": (
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
            "claude-3-haiku-20240307",
        ),
    }

    FALLBACK_MODELS = FALLBACK_MODELS_BY_PROVIDER["gemini"]

    SUPPORTED_REMEDIATION_CHECKS: frozenset[str] = frozenset({
        "copilot_instructions",
        "devcontainers",
        "dependabot_configuration",
        "workflow_sha_pinning",
        "workflow_concurrency_groups",
        "codeowners_validation",
        "pull_request_templates",
        "issue_templates",
        "pre_commit_hooks",
    })

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: str = "gemini-3.5-flash",
        provider: str = "gemini",
        enabled: bool = True,
        base_url: str = "",
    ) -> None:
        """Initialize AI Engine with multi-provider routing.

        Args:
            api_key: API Key for the target provider (optional).
            model_name: Target model identifier (e.g. gemini-3.5-flash, gpt-4o, claude-3-5-sonnet-20241022).
            provider: LLM provider name ("gemini", "openai", "azure", "anthropic").
            enabled: Master toggle for AI capabilities.
            base_url: Custom API endpoint base URL (optional, for Azure / proxies).
        """
        self.api_key = (api_key or "").strip()
        if self.api_key.startswith("${") and self.api_key.endswith("}"):
            self.api_key = ""

        prov = (provider or "").strip().lower()
        model_lower = (model_name or "").lower()
        if not prov or prov == "auto":
            if model_lower.startswith(("gpt-", "o1", "o3", "chatgpt-")):
                prov = "openai"
            elif model_lower.startswith("claude-"):
                prov = "anthropic"
            else:
                prov = "gemini"
        self.provider = prov

        if not model_name:
            if self.provider in ("openai", "azure"):
                self.model_name = "gpt-4o"
            elif self.provider == "anthropic":
                self.model_name = "claude-3-5-sonnet-20241022"
            else:
                self.model_name = "gemini-3.5-flash"
        else:
            self.model_name = model_name

        self.enabled = enabled and bool(self.api_key)
        self.base_url = (base_url or "").strip()
        self._client: Optional[Any] = None
        self._gen_config: Optional[Any] = None
        self._remediation_cache: Dict[str, str] = {}
        self._cache_lock = threading.Lock()
        self._cache_cond = threading.Condition(self._cache_lock)
        self._in_flight_keys: set[str] = set()
        self._prompt_cache: Dict[str, str] = {}
        self._prompt_cache_max_entries: int = 512
        self._prompt_in_flight: set[str] = set()
        self._prompt_cond = threading.Condition(self._cache_lock)
        self._rate_lock = threading.Lock()
        self._min_request_interval_s: float = self._parse_non_negative_float_env(
            "LLM_MIN_INTERVAL_SECONDS", 0.0
        )
        self._retry_attempts: int = self._parse_bounded_int_env(
            "LLM_RETRY_ATTEMPTS", default=2, low=1, high=4
        )
        self._next_request_at: float = 0.0
        self._model_cooldown_until: Dict[str, float] = {}
        self.init_error: Optional[str] = None
        self._circuit_cooldown_until: float = 0.0
        self._circuit_notified: bool = False

        if self.enabled:
            if self.provider == "gemini":
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", SyntaxWarning)
                        warnings.simplefilter("ignore", UserWarning)
                        warnings.simplefilter("ignore", DeprecationWarning)
                        from google import genai
                        from google.genai import types

                    self._client = genai.Client(api_key=self.api_key)
                    self._gen_config = types.GenerateContentConfig(
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
                    )
                    logger.info("AI Engine initialized using Gemini (model: %s)", self.model_name)
                except Exception as err:
                    logger.warning("Could not initialize Google GenAI client: %s. Falling back to deterministic mode.", err)
                    self.init_error = f"Package initialization failed: {err}"
                    self.enabled = False
            elif self.provider in ("openai", "azure"):
                logger.info("AI Engine initialized using OpenAI / Azure (model: %s)", self.model_name)
            elif self.provider == "anthropic":
                logger.info("AI Engine initialized using Anthropic (model: %s)", self.model_name)
            else:
                logger.warning("Unsupported LLM provider '%s'. Falling back to deterministic mode.", self.provider)
                self.enabled = False

    def is_available(self) -> bool:
        """Return True if AI engine is configured and ready."""
        if not self.enabled or not self.api_key:
            return False
        if self.provider == "gemini":
            return self._client is not None
        return True

    def is_circuit_active(self) -> bool:
        """Check if AI circuit breaker is temporarily tripped due to upstream rate limits."""
        return time.time() < self._circuit_cooldown_until

    def is_eligible_for_remediation(self, result: CheckResult) -> bool:
        """Check if a CheckResult qualifies for AI remediation synthesis."""
        return (
            self.is_available()
            and not self.is_circuit_active()
            and result.status in (CheckStatus.WARN, CheckStatus.FAIL)
            and result.check_id in self.SUPPORTED_REMEDIATION_CHECKS
        )

    @staticmethod
    def _parse_non_negative_float_env(name: str, default: float) -> float:
        """Parse a non-negative float environment variable safely."""
        raw = os.getenv(name, "").strip()
        if not raw:
            return default
        try:
            return max(0.0, float(raw))
        except ValueError:
            return default

    @staticmethod
    def _parse_bounded_int_env(name: str, default: int, low: int, high: int) -> int:
        """Parse and clamp an integer environment variable safely."""
        raw = os.getenv(name, "").strip()
        if not raw:
            return default
        try:
            return max(low, min(high, int(raw)))
        except ValueError:
            return default

    @staticmethod
    def _extract_retry_after(
        headers: Optional[Any] = None,
        err_msg: str = "",
    ) -> Optional[float]:
        """Extract bounded Retry-After seconds from response headers or provider error text."""
        if headers is not None and hasattr(headers, "get"):
            raw_hdr = headers.get("Retry-After") or headers.get("retry-after")
            if raw_hdr is not None:
                try:
                    return max(0.5, min(120.0, float(str(raw_hdr).strip())))
                except ValueError:
                    pass

        if err_msg:
            match = re.search(
                r"retry\s+(?:in|after)\s+([0-9]+(?:\.[0-9]+)?)\s*(ms|milliseconds?|s|sec|seconds?)?",
                err_msg,
                re.IGNORECASE,
            )
            if match:
                val = float(match.group(1))
                unit = (match.group(2) or "s").lower()
                if unit.startswith("m"):
                    val /= 1000.0
                return max(0.5, min(120.0, val))
        return None

    def recommended_max_workers(self, requested: int = 8) -> int:
        """Return safe LLM worker concurrency based on provider limits and active rate-limit pacing."""
        requested_safe = max(1, int(requested))
        if self.is_circuit_active():
            return 1
        env_cap = os.getenv("LLM_MAX_WORKERS", "").strip()
        if env_cap:
            try:
                return max(1, min(requested_safe, int(env_cap)))
            except ValueError:
                pass
        with self._rate_lock:
            if self._min_request_interval_s > 0.0:
                return 1
        provider_cap = 3 if self.provider == "gemini" else 4
        return min(requested_safe, provider_cap)

    def _wait_for_rate_slot(self) -> None:
        """Reserve the next paced dispatch slot across threads without sleeping under lock."""
        with self._rate_lock:
            interval = self._min_request_interval_s
            if interval <= 0.0:
                return
            now = time.monotonic()
            wait_s = max(0.0, self._next_request_at - now)
            self._next_request_at = max(now, self._next_request_at) + interval
        if wait_s > 0.0:
            time.sleep(wait_s)

    def _bump_adaptive_pacing(self) -> None:
        """Adaptively increase minimum inter-request spacing after a provider 429/503."""
        with self._rate_lock:
            if self._min_request_interval_s <= 0.0:
                self._min_request_interval_s = 1.5
            else:
                self._min_request_interval_s = min(4.0, self._min_request_interval_s + 0.75)

    def _mark_model_rate_limited(self, model: str, cooldown_seconds: float) -> None:
        """Record temporary per-model cooldown so subsequent calls route directly to healthy fallbacks."""
        bounded = max(15.0, min(300.0, float(cooldown_seconds)))
        with self._rate_lock:
            self._model_cooldown_until[model] = time.monotonic() + bounded

    def _candidate_models(self, provider_key: str) -> List[str]:
        """Return deduplicated candidate models ordered with active model first."""
        fallbacks = self.FALLBACK_MODELS_BY_PROVIDER.get(provider_key, ())
        ordered: List[str] = []
        for candidate in (self.model_name, *fallbacks):
            if candidate and candidate not in ordered:
                ordered.append(candidate)
        return ordered

    def trip_circuit_breaker(self, cooldown_seconds: float = 120.0, reason: str = "Rate limit") -> None:
        """Temporarily suspend live LLM remediation requests to keep audit pipeline moving fast."""
        self._circuit_cooldown_until = time.time() + cooldown_seconds
        if not self._circuit_notified:
            logger.warning(
                "[AI Remediation] %s reached on provider '%s'. "
                "Circuit breaker active for %ds — seamlessly using built-in deterministic "
                "remediation templates to maintain maximum audit velocity.",
                reason,
                self.provider.upper(),
                int(cooldown_seconds),
            )
            self._circuit_notified = True

    def verify_connection(self) -> tuple[bool, str]:
        """Perform an active pre-flight connectivity and credential check with model fallback."""
        if not self.enabled or not self.api_key:
            key_name = (
                "OPENAI_API_KEY" if self.provider in ("openai", "azure")
                else "ANTHROPIC_API_KEY" if self.provider == "anthropic"
                else "GEMINI_API_KEY"
            )
            return False, f"{key_name} is not configured"

        if self.provider in ("openai", "azure"):
            return self._verify_openai()
        elif self.provider == "anthropic":
            return self._verify_anthropic()
        else:
            return self._verify_gemini()

    def _verify_gemini(self) -> tuple[bool, str]:
        """Verify Google Gemini connection and active model quota."""
        if not self._client:
            return False, "Google GenAI client not initialized"

        candidate_models = [self.model_name] + [
            m for m in self.FALLBACK_MODELS_BY_PROVIDER["gemini"] if m != self.model_name
        ]
        last_error = ""

        for candidate in candidate_models:
            try:
                kwargs: Dict[str, Any] = {
                    "model": candidate,
                    "contents": "ping",
                }
                if self._gen_config is not None:
                    kwargs["config"] = self._gen_config

                self._client.models.generate_content(**kwargs)
                if candidate != self.model_name:
                    logger.info(
                        "Primary model '%s' unavailable. Seamlessly fell back to '%s'.",
                        self.model_name,
                        candidate,
                    )
                    self.model_name = candidate
                return True, "OK"
            except Exception as err:
                err_msg = str(err)
                lower_msg = err_msg.lower()
                last_error = err_msg
                if "API_KEY_INVALID" in err_msg or "UNAUTHENTICATED" in err_msg:
                    self.enabled = False
                    self.init_error = "API Key Invalid or Expired"
                    return False, self.init_error
                if (
                    "RESOURCE_EXHAUSTED" in err_msg
                    or "429" in err_msg
                    or "503" in err_msg
                    or "rate limit" in lower_msg
                    or "quota exceeded" in lower_msg
                ):
                    retry_after = self._extract_retry_after(err_msg=err_msg)
                    self._mark_model_rate_limited(candidate, retry_after or 60.0)
                continue

        if "429" in last_error or "RESOURCE_EXHAUSTED" in last_error:
            reason = "Quota exhausted / Rate limit reached on candidate models"
        elif "404" in last_error or "NOT_FOUND" in last_error:
            reason = f"Models {candidate_models} not found or inaccessible"
        else:
            reason = f"Connection error ({last_error[:60]})"
        self.init_error = reason
        self.enabled = False
        return False, reason

    def _verify_openai(self) -> tuple[bool, str]:
        """Verify OpenAI connection and active model quota via REST API."""
        import requests

        base_url = self.base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "api-key": self.api_key,
            "Content-Type": "application/json",
        }
        candidate_models = self._candidate_models("openai")
        last_error = ""

        for candidate in candidate_models:
            try:
                payload = {
                    "model": candidate,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                }
                resp = requests.post(url, headers=headers, json=payload, timeout=10)
                if resp.status_code == 200:
                    if candidate != self.model_name:
                        logger.info(
                            "Primary model '%s' unavailable. Seamlessly fell back to '%s'.",
                            self.model_name,
                            candidate,
                        )
                        self.model_name = candidate
                    return True, "OK"
                elif resp.status_code in (401, 403):
                    self.enabled = False
                    self.init_error = "API Key Invalid or Expired"
                    return False, self.init_error
                elif resp.status_code in (429, 503):
                    retry_after = self._extract_retry_after(
                        getattr(resp, "headers", None),
                        getattr(resp, "text", "") or "",
                    )
                    self._mark_model_rate_limited(candidate, retry_after or 60.0)
                    last_error = "Quota exhausted / Rate limit reached on candidate models"
                    continue
                elif resp.status_code == 404:
                    last_error = f"Model {candidate} not found"
                    continue
                else:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:50]}"
            except Exception as err:
                last_error = str(err)
                continue

        reason = last_error or "OpenAI connection failed"
        self.init_error = reason
        self.enabled = False
        return False, reason

    def _verify_anthropic(self) -> tuple[bool, str]:
        """Verify Anthropic connection and active model quota via REST API."""
        import requests

        base_url = self.base_url or os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1")
        url = f"{base_url.rstrip('/')}/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        candidate_models = self._candidate_models("anthropic")
        last_error = ""

        for candidate in candidate_models:
            try:
                payload = {
                    "model": candidate,
                    "max_tokens": 5,
                    "messages": [{"role": "user", "content": "ping"}],
                }
                resp = requests.post(url, headers=headers, json=payload, timeout=10)
                if resp.status_code == 200:
                    if candidate != self.model_name:
                        logger.info(
                            "Primary model '%s' unavailable. Seamlessly fell back to '%s'.",
                            self.model_name,
                            candidate,
                        )
                        self.model_name = candidate
                    return True, "OK"
                elif resp.status_code in (401, 403):
                    self.enabled = False
                    self.init_error = "API Key Invalid or Expired"
                    return False, self.init_error
                elif resp.status_code in (429, 503, 529):
                    retry_after = self._extract_retry_after(
                        getattr(resp, "headers", None),
                        getattr(resp, "text", "") or "",
                    )
                    self._mark_model_rate_limited(candidate, retry_after or 60.0)
                    last_error = "Quota exhausted / Rate limit reached on candidate models"
                    continue
                elif resp.status_code == 404:
                    last_error = f"Model {candidate} not found"
                    continue
                else:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:50]}"
            except Exception as err:
                last_error = str(err)
                continue

        reason = last_error or "Anthropic connection failed"
        self.init_error = reason
        self.enabled = False
        return False, reason

    def sanitize_text(self, text: str) -> str:
        """Redact sensitive tokens and key patterns before sending to LLM."""
        return self.SENSITIVE_PATTERN.sub("[REDACTED_CREDENTIAL]", text)

    def _execute_with_model_fallback(
        self,
        provider_key: str,
        invoke_model: Callable[[str], Tuple[str, Optional[str], Optional[float]]],
        retry_on_rate_limit: bool = True,
    ) -> Optional[str]:
        """Run a provider call with model failover, per-model cooldowns, pacing, and circuit breaker."""
        if not self.is_available() or self.is_circuit_active():
            return None

        all_candidates = self._candidate_models(provider_key)
        now = time.monotonic()
        with self._rate_lock:
            active_candidates = [
                m for m in all_candidates if self._model_cooldown_until.get(m, 0.0) <= now
            ]
            if not active_candidates and all_candidates:
                soonest_expiry = min(
                    self._model_cooldown_until.get(m, now + 60.0) for m in all_candidates
                )
                remaining = max(5.0, min(120.0, soonest_expiry - now))
            else:
                remaining = 0.0

        if not active_candidates:
            if remaining > 0.0:
                self.trip_circuit_breaker(
                    remaining,
                    reason=f"{provider_key.upper()} Rate limit (all candidate models cooling down)",
                )
            return None

        max_attempts = self._retry_attempts if retry_on_rate_limit else 1
        saw_rate_limit = False
        max_retry_after: Optional[float] = None

        for idx, model in enumerate(active_candidates):
            for attempt in range(max_attempts):
                if retry_on_rate_limit or idx == 0:
                    self._wait_for_rate_slot()
                if not self.is_available() or self.is_circuit_active():
                    return None

                status, text, retry_after_s = invoke_model(model)
                if status == "ok":
                    with self._rate_lock:
                        self._model_cooldown_until.pop(model, None)
                    if model != self.model_name:
                        self.model_name = model
                    return text
                if status == "empty":
                    return None
                if status == "auth_error":
                    self.enabled = False
                    return None
                if status == "rate_limit":
                    saw_rate_limit = True
                    if retry_after_s is not None:
                        max_retry_after = max(max_retry_after or 0.0, retry_after_s)
                    self._bump_adaptive_pacing()
                    if attempt + 1 < max_attempts and (
                        retry_after_s is None or retry_after_s <= 12.0
                    ):
                        jitter = (time.monotonic_ns() % 400) / 1000.0
                        backoff_s = (
                            retry_after_s
                            if retry_after_s is not None
                            else min(8.0, 1.5 * (2 ** attempt))
                        )
                        time.sleep(backoff_s + jitter)
                        continue
                    cooldown_s = retry_after_s if retry_after_s is not None else 60.0
                    self._mark_model_rate_limited(model, cooldown_s)
                    break
                # Non-rate-limit errors try the next candidate model
                break

        if saw_rate_limit:
            cb_cooldown = max(60.0, min(300.0, max_retry_after or 120.0))
            self.trip_circuit_breaker(
                cb_cooldown,
                reason=f"{provider_key.upper()} Rate limit (429/Quota exceeded)",
            )
        return None

    def _classify_http_error(
        self,
        resp: Any,
        model: str,
        provider_label: str,
    ) -> Tuple[str, Optional[str], Optional[float]]:
        """Map HTTP non-200 error responses into normalized failover statuses."""
        status_code = getattr(resp, "status_code", 0)
        if status_code in (401, 403):
            logger.warning(
                "%s API Key Invalid or Unauthenticated. Falling back to deterministic mode.",
                provider_label,
            )
            return ("auth_error", None, None)
        if status_code in (429, 503, 529):
            retry_after = self._extract_retry_after(
                getattr(resp, "headers", None),
                getattr(resp, "text", "") or "",
            )
            return ("rate_limit", None, retry_after)
        if status_code == 404:
            return ("not_found", None, None)
        logger.debug("%s API returned status %s for %s", provider_label, status_code, model)
        return ("error", None, None)

    def _call_gemini(self, prompt: str, retry_on_rate_limit: bool = True) -> Optional[str]:
        """Execute Gemini call with resilient rate-limit handling and safe model fallback."""
        clean_prompt = self.sanitize_text(prompt)

        def _invoke(model: str) -> Tuple[str, Optional[str], Optional[float]]:
            try:
                kwargs: Dict[str, Any] = {
                    "model": model,
                    "contents": clean_prompt,
                }
                if self._gen_config is not None:
                    kwargs["config"] = self._gen_config

                response = self._client.models.generate_content(**kwargs)
                if response and response.text:
                    return ("ok", response.text.strip(), None)
                return ("empty", None, None)
            except Exception as err:
                err_msg = str(err)
                lower_msg = err_msg.lower()
                if "API_KEY_INVALID" in err_msg or "UNAUTHENTICATED" in err_msg:
                    logger.warning(
                        "AI engine suspended (API Key Invalid). Falling back to deterministic templates."
                    )
                    return ("auth_error", None, None)
                if (
                    "RESOURCE_EXHAUSTED" in err_msg
                    or "429" in err_msg
                    or "503" in err_msg
                    or "rate limit" in lower_msg
                    or "quota exceeded" in lower_msg
                ):
                    return ("rate_limit", None, self._extract_retry_after(err_msg=err_msg))
                if "NOT_FOUND" in err_msg or "404" in err_msg:
                    return ("not_found", None, None)
                logger.debug("AI generation skipped on %s for check: %s", model, err)
                return ("error", None, None)

        return self._execute_with_model_fallback(
            "gemini",
            _invoke,
            retry_on_rate_limit=retry_on_rate_limit,
        )

    def _call_openai(self, prompt: str, retry_on_rate_limit: bool = True) -> Optional[str]:
        """Execute OpenAI Chat Completion API call with model fallback and rate-limit resilience."""
        if not self.is_available() or self.is_circuit_active():
            return None

        import requests

        base_url = self.base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "api-key": self.api_key,
            "Content-Type": "application/json",
        }
        clean_prompt = self.sanitize_text(prompt)

        def _invoke(model: str) -> Tuple[str, Optional[str], Optional[float]]:
            try:
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": clean_prompt}],
                    "temperature": 0.2,
                }
                resp = requests.post(url, headers=headers, json=payload, timeout=20)
                if resp.status_code == 200:
                    data = resp.json()
                    choices = data.get("choices", [])
                    if choices:
                        content = choices[0].get("message", {}).get("content", "")
                        return ("ok", content.strip(), None) if content else ("empty", None, None)
                    return ("empty", None, None)
                return self._classify_http_error(resp, model, "OpenAI")
            except Exception as err:
                logger.debug("OpenAI API call failed on model %s: %s", model, err)
                return ("error", None, None)

        return self._execute_with_model_fallback(
            "openai",
            _invoke,
            retry_on_rate_limit=retry_on_rate_limit,
        )

    def _call_anthropic(self, prompt: str, retry_on_rate_limit: bool = True) -> Optional[str]:
        """Execute Anthropic Messages API call with model fallback and rate-limit resilience."""
        if not self.is_available() or self.is_circuit_active():
            return None

        import requests

        base_url = self.base_url or os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1")
        url = f"{base_url.rstrip('/')}/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        clean_prompt = self.sanitize_text(prompt)

        def _invoke(model: str) -> Tuple[str, Optional[str], Optional[float]]:
            try:
                payload = {
                    "model": model,
                    "max_tokens": 2048,
                    "messages": [{"role": "user", "content": clean_prompt}],
                    "temperature": 0.2,
                }
                resp = requests.post(url, headers=headers, json=payload, timeout=20)
                if resp.status_code == 200:
                    data = resp.json()
                    contents = data.get("content", [])
                    if contents:
                        text = contents[0].get("text", "")
                        return ("ok", text.strip(), None) if text else ("empty", None, None)
                    return ("empty", None, None)
                return self._classify_http_error(resp, model, "Anthropic")
            except Exception as err:
                logger.debug("Anthropic API call failed on model %s: %s", model, err)
                return ("error", None, None)

        return self._execute_with_model_fallback(
            "anthropic",
            _invoke,
            retry_on_rate_limit=retry_on_rate_limit,
        )

    def _generate_text(self, prompt: str, retry_on_rate_limit: bool = True) -> Optional[str]:
        """Route text generation to the active LLM provider with thread-safe prompt caching."""
        if not self.is_available() or self.is_circuit_active():
            return None

        clean_prompt = self.sanitize_text(prompt)
        digest = hashlib.sha256(clean_prompt.encode("utf-8", errors="ignore")).hexdigest()
        cache_key = f"{self.provider}:{digest}"

        with self._prompt_cond:
            while True:
                if cache_key in self._prompt_cache:
                    return self._prompt_cache[cache_key]
                if cache_key not in self._prompt_in_flight:
                    self._prompt_in_flight.add(cache_key)
                    break
                if self.is_circuit_active() or not self.is_available():
                    return None
                self._prompt_cond.wait(timeout=1.0)

        result: Optional[str] = None
        try:
            if self.provider in ("openai", "azure"):
                result = self._call_openai(clean_prompt, retry_on_rate_limit=retry_on_rate_limit)
            elif self.provider == "anthropic":
                result = self._call_anthropic(clean_prompt, retry_on_rate_limit=retry_on_rate_limit)
            else:
                result = self._call_gemini(clean_prompt, retry_on_rate_limit=retry_on_rate_limit)
        finally:
            with self._prompt_cond:
                self._prompt_in_flight.discard(cache_key)
                if result:
                    if len(self._prompt_cache) >= self._prompt_cache_max_entries:
                        oldest_key = next(iter(self._prompt_cache))
                        del self._prompt_cache[oldest_key]
                    self._prompt_cache[cache_key] = result
                self._prompt_cond.notify_all()
        return result

    @staticmethod
    def build_deterministic_executive_summary(results: List[CheckResult], org_name: str) -> str:
        """Build a concise, grounded executive governance summary without live LLM calls."""
        if not results:
            return ""

        total = len(results)
        passed = sum(1 for r in results if r.status == CheckStatus.PASS)
        warned = sum(1 for r in results if r.status == CheckStatus.WARN)
        failed = sum(1 for r in results if r.status == CheckStatus.FAIL)
        skipped = sum(1 for r in results if r.status == CheckStatus.SKIPPED)
        effective = total - skipped
        score_str = f"{(passed / effective) * 100:.1f}%" if effective > 0 else "N/A (All Skipped)"

        critical_high_fails = [
            r
            for r in results
            if r.status == CheckStatus.FAIL and str(getattr(r.severity, "value", r.severity)).upper() in ("CRITICAL", "HIGH")
        ]
        top_controls: List[str] = []
        seen_checks: set[str] = set()
        for r in critical_high_fails:
            if r.check_name not in seen_checks:
                seen_checks.add(r.check_name)
                top_controls.append(r.check_name)
            if len(top_controls) >= 3:
                break

        scope_label = org_name or "audited organizations"
        p1 = (
            f"1. Executive Posture Summary: Across {scope_label}, {total} governance checks were evaluated "
            f"({passed} passed, {failed} failed, {warned} warnings, {skipped} skipped; compliance score {score_str})."
        )
        if top_controls:
            p2 = (
                f"2. Priority Remediation Focus: {len(critical_high_fails)} high/critical control gaps were identified, "
                f"led by {', '.join(top_controls)}. Standardize branch rulesets, push protection, and pinned workflow dependencies across affected repositories."
            )
        elif failed > 0 or warned > 0:
            p2 = (
                f"2. Priority Remediation Focus: {failed} failed and {warned} warning controls require platform engineering follow-up; "
                f"consult the Fleet_Playbook and AI_Recommendations sheets for verified configuration baselines."
            )
        else:
            p2 = "2. Priority Remediation Focus: All evaluated controls passed baseline governance standards with zero critical gaps detected."
        return f"{p1}\n\n{p2}"

    def generate_executive_summary(self, results: List[CheckResult], org_name: str) -> str:
        """Synthesize a high-level executive risk and governance narrative."""
        if not self.is_available() or self.is_circuit_active():
            return ""

        total = len(results)
        passed = sum(1 for r in results if r.status == CheckStatus.PASS)
        warned = sum(1 for r in results if r.status == CheckStatus.WARN)
        failed = sum(1 for r in results if r.status == CheckStatus.FAIL)

        failed_checks = [f"- [{r.category}] {r.repo_name}: {r.check_name} ({r.current_state})" for r in results if r.status == CheckStatus.FAIL][:12]
        warned_checks = [f"- [{r.category}] {r.repo_name}: {r.check_name}" for r in results if r.status == CheckStatus.WARN][:8]

        prompt = f"""You are a Principal Cloud Security and DevOps Architect auditing GitHub Enterprise Cloud.
Analyze these audit findings for organization '{self.sanitize_text(org_name)}' and synthesize a concise, high-impact Executive Risk Briefing.

METRICS:
- Total Checks: {total}
- Passing: {passed}
- Warnings: {warned}
- Critical Failures / Action Required: {failed}

HIGH RISK / FAILED CHECKS:
{chr(10).join(failed_checks) if failed_checks else 'None'}

WARNINGS:
{chr(10).join(warned_checks) if warned_checks else 'None'}

FORMAT YOUR RESPONSE AS 3 CONCISE PLAIN-TEXT PARAGRAPHS (NO Markdown **bold**, NO ### headers, NO backticks):
1. Executive Risk Summary: Overall posture, score evaluation, and primary governance gaps.
2. Supply-Chain & Critical Vulnerability Vectors: Specifically calling out risks from unpinned actions, missing rulesets, or exposed secrets.
3. Strategic 30-Day Remediation Plan: High-priority immediate fixes for the platform team.
Keep it factual, authoritative, professional, and actionable. Do not use filler or platitudes."""

        summary = self._generate_text(prompt)
        return self._to_plain_narrative(summary) if summary else ""

    def _apply_remediation(self, result: CheckResult, snippet: str, detected_stack: str) -> None:
        """Apply synthesized remediation and metadata to CheckResult, preserving canonical baseline."""
        if result.remediation_snippet_after and not result.extra_details.get("ai_enriched"):
            result.extra_details.setdefault("baseline_remediation", result.remediation_snippet_after)
        result.remediation_snippet_after = snippet
        result.extra_details["ai_enriched"] = True
        result.extra_details["detected_stack"] = detected_stack
        result.extra_details["ai_model"] = self.model_name
        result.extra_details["ai_provider"] = self.provider

    def enrich_remediation(self, result: CheckResult, tech_stack: Dict[str, Any]) -> None:
        """Generate tailored remediation snippets based on repository tech stack."""
        if not self.is_eligible_for_remediation(result):
            return

        raw_langs = [str(l).strip() for l in tech_stack.get("languages", []) if str(l).strip()]
        languages = ", ".join(dict.fromkeys(raw_langs)) or "General Software"
        frameworks = ", ".join(dict.fromkeys(str(f).strip() for f in tech_stack.get("frameworks", []) if str(f).strip())) or "Standard"
        manifests = ", ".join(dict.fromkeys(str(m).strip() for m in tech_stack.get("manifests", []) if str(m).strip())) or "None"

        cache_key = f"{result.check_id}:{languages}:{frameworks}:{manifests}"

        # Wait for any in-flight request with the same cache key
        with self._cache_lock:
            while cache_key in self._in_flight_keys:
                self._cache_cond.wait(timeout=10.0)
                if cache_key in self._remediation_cache:
                    cached_text = self._remediation_cache[cache_key]
                    if cached_text:
                        self._apply_remediation(result, cached_text, languages)
                    return

            if cache_key in self._remediation_cache:
                cached_text = self._remediation_cache[cache_key]
                if cached_text:
                    self._apply_remediation(result, cached_text, languages)
                return

            self._in_flight_keys.add(cache_key)

        cleaned_text = ""
        try:
            if self.is_circuit_active():
                return

            prompt = f"""You are a Senior DevOps Engineer. Generate a production-ready, custom-tailored configuration snippet to remediate this finding:

TECH STACK CONTEXT (Reusable across repositories sharing this stack):
- Detected Languages: {self.sanitize_text(languages)}
- Detected Frameworks/Libraries: {self.sanitize_text(frameworks)}
- Build/Config Manifests Found: {self.sanitize_text(manifests)}

AUDIT FINDING:
- Check: {result.check_name}
- Problem: {result.current_state}
- Recommended Goal: {result.recommended_state}

TASK:
Provide ONLY the exact, tailored configuration file content to resolve this for this specific tech stack. Do not hardcode individual repository names in comments or titles.
- For 'copilot_instructions': Write a customized .github/copilot-instructions.md with team style rules, testing conventions, and safety guidelines specific to {languages}.
- For 'devcontainers': Provide a complete, valid .devcontainer/devcontainer.json configured with the right runtime container image, port forwards, and VS Code extensions for {languages} and {frameworks}.
- For 'dependabot_configuration': Provide a valid .github/dependabot.yml covering all detected package ecosystems ({manifests}).
- For 'workflow_concurrency_groups': Provide the exact GitHub Actions YAML 'concurrency' block configured with cancel-in-progress for PR branches.
- For 'codeowners_validation': Provide a complete, production-ready .github/CODEOWNERS defining granular team ownership for this tech stack.
- For 'pull_request_templates': Provide a comprehensive .github/PULL_REQUEST_TEMPLATE.md checklist (tests, docs, security) tailored to {languages}.
- For 'pre_commit_hooks': Provide a valid .pre-commit-config.yaml with modern linting and formatting hooks for {languages}.
- For 'issue_templates': Provide a YAML issue template (.github/ISSUE_TEMPLATE/bug_report.yml) configured for {languages}.

Output ONLY the code/configuration snippet inside code fences. Do not add introductory or concluding chat."""

            text = self._generate_text(prompt, retry_on_rate_limit=False)
            if text:
                cleaned = text.strip()
                if cleaned.startswith("```"):
                    lines = cleaned.splitlines()
                    if len(lines) >= 2 and lines[-1].startswith("```"):
                        cleaned = "\n".join(lines[1:-1])
                cleaned_text = cleaned
        finally:
            with self._cache_lock:
                self._remediation_cache[cache_key] = cleaned_text
                self._in_flight_keys.discard(cache_key)
                self._cache_cond.notify_all()

        if cleaned_text:
            self._apply_remediation(result, cleaned_text, languages)

    def analyze_push_protection_bypass(
        self,
        secret_type: str,
        comment: str,
        actor: str = "",
        repo: str = "",
    ) -> Tuple[str, str]:
        """Classify push protection bypass risk and generate an actionable security advisory.

        Returns:
            Tuple of (risk_level, advisory_text)
            e.g. ("High Risk", "Generic comment 'test'. Verify if live credential was pushed and rotate immediately.")
        """
        comment_clean = (comment or "").strip()
        comment_lower = comment_clean.lower()
        secret_clean = secret_type or "Secret"

        test_indicators = ("test", "mock", "dummy", "sample", "example", "fixture", "unit test", "doc", "placeholder", "fake")
        urgency_indicators = ("urgent", "temp", "bypass", "asap", "prod", "fix", "deploy", "quick", "skip", "override")

        default_risk = "Moderate Risk"
        default_advisory = f"Push protection was bypassed by {actor or 'developer'}. Review token entropy and commit history."

        if not comment_clean or comment_clean in ("-", "--", "N/A", "None", "unspecified"):
            default_risk = "Critical Risk"
            default_advisory = "Push protection was bypassed with zero justification. Immediately inspect commit SHA and rotate credential if active."
        elif any(w in comment_lower for w in urgency_indicators) and not any(w in comment_lower for w in test_indicators):
            default_risk = "High Risk"
            default_advisory = f"High-risk bypass justification ('{comment_clean}'). Verify whether live {secret_clean} was exposed and revoke if verified."
        elif any(w in comment_lower for w in test_indicators):
            default_risk = "Low Risk"
            default_advisory = f"Developer noted test/fixture credential. Confirm pattern is not used in production environments."

        if not self.is_available():
            return default_risk, default_advisory

        prompt = f"""You are a Principal Application Security Engineer. Evaluate this GitHub Secret Scanning Push Protection bypass:
Secret Type: {self.sanitize_text(secret_clean)}
Actor: {self.sanitize_text(actor or 'Unknown')}
Repository: {self.sanitize_text(repo or 'Target')}
Developer Justification: "{self.sanitize_text(comment_clean)}"

Analyze whether this justification indicates a genuine test fixture, false positive, or an accidental leak of a production secret.
Format your answer EXACTLY as:
RISK: <Critical Risk | High Risk | Moderate Risk | Low Risk>
ADVISORY: <1 concise sentence specifying verification action for SecOps>"""

        response = self._generate_text(prompt)
        if not response:
            return default_risk, default_advisory

        try:
            risk = default_risk
            advisory = default_advisory
            for line in response.splitlines():
                line_str = line.strip()
                if line_str.upper().startswith("RISK:"):
                    raw_risk = line_str.split(":", 1)[1].strip()
                    for valid in ("Critical Risk", "High Risk", "Moderate Risk", "Low Risk"):
                        if valid.lower() in raw_risk.lower():
                            risk = valid
                            break
                elif line_str.upper().startswith("ADVISORY:"):
                    raw_adv = line_str.split(":", 1)[1].strip()
                    cleaned_adv = self._to_plain_advisory(raw_adv)
                    if cleaned_adv:
                        advisory = cleaned_adv
            return risk, advisory
        except Exception:
            return default_risk, default_advisory

    @staticmethod
    def _strip_inline_markdown(text: str) -> str:
        """Remove Markdown code ticks, bold/italic markers, and leading heading hashes."""
        cleaned = (text or "").strip()
        cleaned = re.sub(r"```[\s\S]*?```", " ", cleaned)
        cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
        cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
        cleaned = re.sub(r"__([^_]+)__", r"\1", cleaned)
        cleaned = re.sub(r"(?<!\w)\*([^*]+)\*(?!\w)", r"\1", cleaned)
        cleaned = re.sub(r"(?m)^\s*#{1,6}\s+", "", cleaned)
        return cleaned

    @classmethod
    def _to_plain_advisory(cls, text: str) -> str:
        """Strip Markdown emphasis, inline code ticks, and list prefixes for single-cell Excel advisories."""
        cleaned = cls._strip_inline_markdown(text)
        cleaned = re.sub(r"(?m)^\s*(?:[-*•]+|\d+\.)\s+", "", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()

    @classmethod
    def _to_plain_narrative(cls, text: str) -> str:
        """Strip Markdown tokens while preserving paragraph line breaks for multi-paragraph briefings."""
        cleaned = cls._strip_inline_markdown(text)
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in cleaned.splitlines()]
        normalized = "\n".join(lines)
        return re.sub(r"\n{3,}", "\n\n", normalized).strip()

    _NO_PIPELINE_HALLUCINATION_RE = re.compile(
        r"\b(?:complete\s+absence\s+of|absence\s+of|zero|no|lack(?:ing)?)\s+(?:automated\s+)?(?:deployment\s+)?(?:pipelines?|deployment\s+automation|ci/cd)\b",
        re.IGNORECASE,
    )

    def diagnose_dora_flow(
        self,
        repo: str = "",
        df_weekly: float = 0.0,
        df_tier: str = "Low",
        lead_time_hours: Optional[float] = None,
        lttc_tier: str = "Low",
        cfr_pct: float = 0.0,
        cfr_tier: str = "Low",
        mttr_hours: Optional[float] = None,
        mttr_tier: str = "Low",
        composite_tier: str = "Low",
        rework_pct: Optional[float] = None,
        rework_tier: str = "N/A",
        open_incidents: int = 0,
        signal_mode: str = "",
        code_change_deploys: Optional[int] = None,
        no_op_deploys: int = 0,
        stalled_deploys: int = 0,
        collapsed_retries: int = 0,
        total_deploys: Optional[int] = None,
    ) -> str:
        """Diagnose delivery flow bottlenecks and prescribe actionable engineering improvements."""
        lt_str = f"{lead_time_hours:.2f}h" if lead_time_hours is not None else "N/A"
        mttr_str = f"{mttr_hours:.2f}h" if mttr_hours is not None else "N/A"
        rework_str = f"{rework_pct:.1f}%" if rework_pct is not None else "N/A"

        stalled_addressed = False
        if cfr_pct > 15.0 or cfr_tier in ("Low", "Medium"):
            retry_note = f" ({collapsed_retries} duplicate retry run(s) collapsed)" if collapsed_retries > 0 else ""
            default_rec = (
                f"Elevated change failure rate ({cfr_pct:.1f}%{retry_note}). "
                "Strengthen automated regression gates, pre-deploy checks, and rollback verification."
            )
        elif stalled_deploys > 0 and (total_deploys == 0 or (code_change_deploys == 0 and no_op_deploys == 0)):
            stalled_addressed = True
            default_rec = (
                f"{stalled_deploys} deployment(s) remained IN_PROGRESS >24h without finalizing status. "
                "Configure workflow timeout-minutes and explicit GitHub Deployment completion/failure callbacks."
            )
        elif (rework_pct is not None and rework_pct > 8.0) or rework_tier in ("Low", "Medium"):
            default_rec = (
                f"High deployment rework rate ({rework_str}). Enforce pre-merge integration verification "
                "and staged rollouts to curb hotfixes and reverts."
            )
        elif open_incidents > 0:
            default_rec = (
                f"{open_incidents} open recovery incident(s) pending resolution. "
                "Prioritize automated rollback verification and incident triage."
            )
        elif code_change_deploys == 0 and no_op_deploys > 0:
            default_rec = (
                f"All {no_op_deploys} successful pipeline run(s) were same-SHA rebuilds with zero new code changes. "
                "Trigger production releases from code commits or release tags rather than scheduled rebuilds alone."
            )
        elif (lead_time_hours is not None and lead_time_hours > 168.0) or lttc_tier in ("Low", "Medium"):
            default_rec = (
                f"Lead time bottleneck ({lt_str}). Reduce PR batch size, enforce smaller reviews, "
                "and parallelize CI build/test steps."
            )
        elif df_weekly < 1.0 or df_tier in ("Low", "Medium"):
            noop_note = (
                f" (excluding {no_op_deploys} same-SHA no-op rebuild(s))"
                if no_op_deploys > 0
                else ""
            )
            default_rec = (
                f"Code-change deployment cadence is low ({df_weekly:.2f}/week{noop_note}). "
                "Decouple manual release gates and ship smaller, automated merge-to-production increments."
            )
        elif composite_tier == "Elite":
            default_rec = "Elite DORA delivery performance. Maintain automated testing coverage and current trunk-based release cadence."
        else:
            default_rec = "Balanced engineering flow. Continue monitoring pull request review turnaround and test pipeline duration."

        if stalled_deploys > 0 and not stalled_addressed:
            default_rec = f"{default_rec} Also resolve {stalled_deploys} stalled IN_PROGRESS deployment(s) stuck >24h."

        if not self.is_available():
            return default_rec

        safe_repo = self.sanitize_text(repo)
        safe_signal = self.sanitize_text(signal_mode) or "GitHub Native Signals"
        code_change_str = str(code_change_deploys) if code_change_deploys is not None else "Unknown"
        total_str = str(total_deploys) if total_deploys is not None else "Unknown"
        has_active_pipeline = (
            no_op_deploys > 0
            or (total_deploys is not None and total_deploys > 0)
            or (bool(signal_mode) and "no production activity" not in signal_mode.lower() and "permission gap" not in signal_mode.lower())
        )

        prompt = f"""You are a Principal DevOps and Delivery Flow Architect. Diagnose delivery bottlenecks for repository '{safe_repo}':
- Composite Performance Tier: {composite_tier}
- Delivery Signal Mode: {safe_signal}
- Total Completed Deployments: {total_str} (Unique Code-Change Deploys: {code_change_str}, No-Op Same-SHA Rebuilds: {no_op_deploys})
- Collapsed Same-SHA Failure Retries: {collapsed_retries} | Stalled Deploys (>24h IN_PROGRESS): {stalled_deploys}
- Code-Change Deployment Frequency: {df_weekly:.2f} per week ({df_tier})
- Lead Time for Changes: {lt_str} ({lttc_tier})
- Change Failure Rate: {cfr_pct:.1f}% ({cfr_tier})
- Failed Deployment Recovery Time (MTTR): {mttr_str} ({mttr_tier})
- Deployment Rework Rate: {rework_str} ({rework_tier})
- Open Incidents: {open_incidents}

Strict Grounding & Output Rules:
1. Only cite facts consistent with the telemetry above. Never claim deployment pipelines or automation are absent when Delivery Signal Mode shows active workflows/deployments/releases or Total/No-Op Deploys > 0.
2. If No-Op Same-SHA Rebuilds dominate and Unique Code-Change Deploys is 0, explain that pipelines run on schedule/rebuilds without shipping new code changes.
3. If Stalled Deploys > 0, explicitly call out cleaning up hung IN_PROGRESS deployment statuses and workflow timeouts.
4. Keep remediation grounded in native GitHub capabilities (GitHub Actions, Deployments, Releases, Pages, PR/environment protection gates) unless another tool is evidenced.
5. Output PLAIN TEXT ONLY: NO Markdown bold/italic (** or *), NO backticks, NO bullet lists. Provide ONLY 1-2 concise sentences."""

        res = self._generate_text(prompt)
        if not res:
            return default_rec
        cleaned = self._to_plain_advisory(res)
        if not cleaned:
            return default_rec
        if has_active_pipeline and self._NO_PIPELINE_HALLUCINATION_RE.search(cleaned):
            return default_rec
        return cleaned

    def generate_finops_advisory(
        self,
        repo: str,
        disk_size_mb: float,
        artifact_mb: float,
        default_90d_count: int,
        stale_cache_count: int,
        optimization_status: str,
    ) -> str:
        """Generate tailored storage quota and workflow artifact retention recommendations."""
        if default_90d_count > 0 and stale_cache_count > 0:
            default_rec = f"Override default 90-day retention with 'retention-days: 7' in workflow files ({default_90d_count} artifacts) and purge {stale_cache_count} stale caches."
        elif default_90d_count > 0:
            default_rec = f"Configure 'retention-days: 7' on {default_90d_count} workflow artifacts to eliminate default 90-day storage waste."
        elif stale_cache_count > 0:
            default_rec = f"Purge {stale_cache_count} stale Actions caches unaccessed for 30+ days via GitHub Actions Cache API."
        elif disk_size_mb > 1000:
            default_rec = f"Large Git repository footprint ({disk_size_mb:.1f} MB). Run git-filter-repo or migrate heavy binaries to Git LFS."
        elif optimization_status == "Optimized":
            default_rec = "Storage profile is well-optimized with efficient artifact retention and active cache usage."
        else:
            default_rec = "Review artifact retention policies and purge unneeded build dependencies."

        if not self.is_available():
            return default_rec

        prompt = f"""You are a Cloud FinOps and CI/CD Infrastructure Architect. Analyze repository '{self.sanitize_text(repo)}':
- Git Disk Footprint: {disk_size_mb:.1f} MB
- Workflow Artifact Storage: {artifact_mb:.1f} MB
- Artifacts using Default 90-day Retention: {default_90d_count}
- Stale Actions Caches (>30d unaccessed): {stale_cache_count}
- Optimization Status: {optimization_status}

Provide ONLY 1-2 concise sentences prescribing precise configuration fixes (.github/workflows YAML retention settings or cache purging). No conversational filler."""

        res = self._generate_text(prompt)
        return (self._to_plain_advisory(res) or default_rec) if res else default_rec

    def generate_runner_fleet_advisory(
        self,
        total_runners: int,
        availability_pct: float,
        busy_count: int,
        avg_queue_wait_s: float,
        p95_queue_wait_s: float,
        sla_breaches: int,
        ephemeral_count: int,
    ) -> str:
        """Generate autoscaling and runner fleet sizing advisory."""
        if sla_breaches > 0 or p95_queue_wait_s > 60:
            default_rec = f"Queue latency breaches 60s SLA ({sla_breaches} jobs delayed, P95: {p95_queue_wait_s:.0f}s). Adopt Actions Runner Controller (ARC) with HorizontalRunnerAutoscaler to dynamically expand runner capacity during peak build windows."
        elif ephemeral_count < (total_runners / 2) and total_runners > 0:
            default_rec = f"Persistent runner sprawl detected ({total_runners - ephemeral_count} non-ephemeral). Migrate to ephemeral container runner scale sets to ensure reproducible CI environments and prevent security cross-contamination."
        else:
            default_rec = "Runner fleet capacity is well-provisioned. Availability and queue latency remain within target SLAs."

        if not self.is_available() or self.is_circuit_active():
            return default_rec

        prompt = f"""You are a Principal CI/CD Infrastructure Architect. Analyze this GitHub Actions self-hosted runner fleet:
- Total Runners: {total_runners}
- Availability: {availability_pct:.1f}%
- Currently Busy: {busy_count}
- P95 Queue Latency: {p95_queue_wait_s:.1f}s
- Jobs Breaching 60s Queue SLA: {sla_breaches}
- Ephemeral Runners: {ephemeral_count} / {total_runners}

Provide ONLY 2 concise plain-text sentences with clear architectural recommendations on autoscaling policies (ARC, KEDA) and runner pool sizing."""

        res = self._generate_text(prompt)
        return (self._to_plain_advisory(res) or default_rec) if res else default_rec

    def generate_activity_insights(
        self,
        org_name: str,
        total_contributors: int,
        active_contributors: int,
        total_activities: int,
        total_commits: int,
        prs_opened: int,
        prs_merged: int,
    ) -> str:
        """Synthesize engineering activity trends and contributor cadence."""
        default_rec = f"Engineering activity across {org_name}: {active_contributors} active contributors ({total_activities} actions, {prs_merged} PRs merged). Maintain balanced code review assignments to prevent reviewer fatigue."

        if not self.is_available() or self.is_circuit_active():
            return default_rec

        prompt = f"""You are an Engineering Operations Director. Analyze contributor activity for '{self.sanitize_text(org_name)}':
- Total Contributors Tracked: {total_contributors}
- Active Contributors: {active_contributors}
- Total Activities: {total_activities}
- Commits: {total_commits}
- PRs Opened / Merged: {prs_opened} / {prs_merged}

Provide ONLY 2 concise plain-text sentences summarizing development cadence and workload distribution. No chat, Markdown, or filler."""

        res = self._generate_text(prompt)
        return (self._to_plain_advisory(res) or default_rec) if res else default_rec


def init_ai_engine(
    require_ai: bool = False,
    no_ai: bool = False,
    enable_ai: Optional[bool] = None,
    verify: bool = True,
    creds: Optional[Dict[str, Any]] = None,
    log: Optional[logging.Logger] = None,
) -> AIEngine:
    """Initialize AIEngine with credentials, pre-flight healthcheck, and CLI overrides."""
    from ..credentials import resolve_runtime_credentials

    active_log = log or logger
    resolved_creds = creds if creds is not None else resolve_runtime_credentials()
    provider = resolved_creds.get("llm_provider", "gemini")
    ai_model = resolved_creds.get("llm_model", "gemini-3.5-flash")
    llm_api_key = resolved_creds.get("llm_api_key", "")

    if no_ai or enable_ai is False:
        llm_enabled = False
    elif enable_ai is True:
        llm_enabled = True
    else:
        llm_enabled = bool(resolved_creds.get("llm_enabled", True))

    ai_engine = AIEngine(
        api_key=llm_api_key,
        model_name=ai_model,
        provider=provider,
        enabled=llm_enabled,
    )
    ai_engine.disabled_by_cli = bool(no_ai or enable_ai is False)

    if not llm_enabled:
        if require_ai:
            raise RuntimeError("--require-ai was specified but AI engine is disabled or not configured.")
        if ai_engine.disabled_by_cli and verify:
            active_log.info(
                "AI Remediation Engine: NOT ENABLED (--require-ai no). "
                "AI remediation suggestions will be omitted."
            )
        return ai_engine

    key_hint = (
        "OPENAI_API_KEY"
        if ai_engine.provider in ("openai", "azure")
        else "ANTHROPIC_API_KEY"
        if ai_engine.provider == "anthropic"
        else "GEMINI_API_KEY"
    )

    if not ai_engine.is_available():
        offline_reason = ai_engine.init_error or f"{key_hint} not configured"
        if require_ai:
            raise RuntimeError("--require-ai was specified but AI engine is disabled or not configured.")
        if verify:
            active_log.warning(
                "AI Remediation Engine: NOT ENABLED / OFFLINE (%s). "
                "AI remediation suggestions will be omitted. "
                "To enable AI-tailored outputs, run 'repo360 setup' or configure %s and pass '--require-ai yes'.",
                offline_reason,
                key_hint,
            )
        return ai_engine

    if verify:
        active_log.info(
            "Performing pre-flight healthcheck for AI Remediation Engine (%s: %s)...",
            ai_engine.provider.upper(),
            ai_engine.model_name,
        )
        is_ok, reason = ai_engine.verify_connection()
        if is_ok:
            active_log.info(
                "AI Remediation Engine: ONLINE (%s: %s - Authenticated)",
                ai_engine.provider.upper(),
                ai_engine.model_name,
            )
        else:
            ai_engine.enabled = False
            active_log.warning(
                "AI Remediation Engine: OFFLINE (%s). "
                "AI remediation suggestions will be omitted.",
                reason,
            )
            if require_ai:
                raise RuntimeError(f"--require-ai was specified but AI engine is offline: {reason}")

    return ai_engine
