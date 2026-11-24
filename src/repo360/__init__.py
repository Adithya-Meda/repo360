"""Repo360 Platform Governance & Intelligence package."""

from __future__ import annotations

from .common.interrupts import install_signal_handlers

install_signal_handlers()

import warnings

# Silence known-noisy third-party SDK warnings (google-genai) without masking warnings from other libraries
warnings.filterwarnings("ignore", message=".*invalid escape sequence.*", category=SyntaxWarning)
warnings.filterwarnings("ignore", message=".*automatic function calling.*")
warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"google(\..*)?$")
warnings.filterwarnings("ignore", category=UserWarning, module=r"google(\..*)?$")

__version__ = "1.3.0"
__author__ = "Adithya Meda"

