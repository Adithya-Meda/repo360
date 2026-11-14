"""Unified resilient GitHub REST and GraphQL API client re-export.

Provides Repo360Client alias pointing directly to the unified ResilientGitHubClient.
"""

from __future__ import annotations

from ..common.client import ResilientGitHubClient

# Production-grade unified client re-export
Repo360Client = ResilientGitHubClient

__all__ = ["Repo360Client", "ResilientGitHubClient"]
