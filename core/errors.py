"""Shared exception types."""

from __future__ import annotations


class ConfigError(Exception):
    """Raised when config/*.yaml is missing, incomplete, or refers to something unknown."""
