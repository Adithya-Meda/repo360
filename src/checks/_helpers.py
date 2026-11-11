"""Shared helper utilities for governance check evaluation and permission handling."""

from __future__ import annotations

from typing import Any, Optional
import requests

from .base import BaseCheck, CheckResult, CheckSeverity, CheckStatus


def get_permission_gap(
    inspector: Any,
    org: str,
    control: str,
    repo: Optional[str] = None,
) -> Optional[str]:
    """Return recorded string permission error if available, ignoring unconfigured mocks."""
    getter = getattr(inspector, "get_permission_error", None)
    if callable(getter):
        val = getter(org, control, repo=repo)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def is_http_forbidden(err: Exception) -> bool:
    """Return True if an exception represents an HTTP 403 Forbidden response."""
    return (
        isinstance(err, requests.exceptions.HTTPError)
        and getattr(err, "response", None) is not None
        and err.response.status_code == 403
    )


def make_permission_skipped_result(
    check: BaseCheck,
    org: str,
    reason: str,
    documentation_url: str,
    repo: str = "Organization-Wide",
) -> CheckResult:
    """Build a consistent CheckStatus.SKIPPED result for API permission gaps."""
    return CheckResult(
        check_id=check.check_id,
        check_name=check.check_name,
        persona=check.persona,
        category=check.category,
        status=CheckStatus.SKIPPED,
        severity=CheckSeverity.INFO,
        org_name=org,
        repo_name=repo,
        current_state=f"Unable to verify (insufficient GitHub App/token permissions): {reason}",
        recommended_state="Grant the required read permission in GitHub App settings ('repo360 setup') and re-run.",
        explanation="Check was marked SKIPPED rather than assuming PASS or FAIL because GitHub denied access to inspect this control.",
        documentation_url=documentation_url,
    )
