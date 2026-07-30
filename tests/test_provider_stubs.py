"""Tests for the unimplemented provider stubs (codex_cli, openai_api)."""

from __future__ import annotations

from pathlib import Path

import pytest

from providers.base import AgentTask
from providers.codex_cli import adapter as codex_cli
from providers.openai_api import adapter as openai_api


def _task() -> AgentTask:
    return AgentTask(agent="backend", description="do x", repo_root=Path("."))


def test_codex_cli_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        codex_cli.run(_task())


def test_openai_api_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        openai_api.run(_task())
