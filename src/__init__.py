"""Repo360 Platform Governance & Intelligence package."""

from __future__ import annotations

from .common.interrupts import install_signal_handlers

install_signal_handlers()

import warnings

# Silence third-party SDK syntax/deprecation warnings (e.g. google-genai) on Python 3.12+
warnings.filterwarnings("ignore", category=SyntaxWarning)
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*invalid escape sequence.*")
warnings.filterwarnings("ignore", message=".*automatic function calling.*")

__version__ = "1.0.0"
__author__ = "Enterprise Platform Engineering Team"

