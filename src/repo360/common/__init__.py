"""Common utilities and inventory models for Repo360 platform."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .ai_sheet import AI_REC_HEADERS, PRIORITY_STYLES, render_ai_recommendations_sheet

__all__ = [
    "AI_REC_HEADERS",
    "PRIORITY_STYLES",
    "render_ai_recommendations_sheet",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from . import ai_sheet

        return getattr(ai_sheet, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
