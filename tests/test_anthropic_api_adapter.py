"""Tests for providers.anthropic_api.adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.errors import ConfigError
from providers.anthropic_api import adapter
from providers.base import AgentTask


def _write_configs(tmp_path: Path, models_yaml: str, token_budget_yaml: str = "backend: 1000\n") -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text(models_yaml, encoding="utf-8")
    (tmp_path / "config" / "token_budget.yaml").write_text(token_budget_yaml, encoding="utf-8")


def test_load_yaml_empty_file_defaults_to_empty_dict(tmp_path: Path) -> None:
    (tmp_path / "empty.yaml").write_text("", encoding="utf-8")

    assert adapter._load_yaml(tmp_path, Path("empty.yaml")) == {}


def test_write_files_creates_parent_dirs(tmp_path: Path) -> None:
    files = [adapter.FileChange(path="a/b/c.py", action="create", content="x = 1\n")]

    written = adapter._write_files(tmp_path, files)

    assert written == ["a/b/c.py"]
    assert (tmp_path / "a" / "b" / "c.py").read_text(encoding="utf-8") == "x = 1\n"


def test_write_files_rejects_parent_traversal(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.txt"
    files = [adapter.FileChange(path="../outside.txt", action="create", content="pwned\n")]

    with pytest.raises(ValueError, match="outside the repo"):
        adapter._write_files(repo, files)

    assert not outside.exists()


def test_write_files_rejects_absolute_path(tmp_path: Path) -> None:
    """`Path("/repo") / "/etc/passwd"` is `/etc/passwd` — an absolute path
    replaces the base entirely, so this escapes without any `..` at all."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "absolute.txt"
    files = [adapter.FileChange(path=str(outside), action="create", content="pwned\n")]

    with pytest.raises(ValueError, match="outside the repo"):
        adapter._write_files(repo, files)

    assert not outside.exists()


def test_write_files_rejects_the_whole_plan_not_just_the_bad_entry(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    files = [
        adapter.FileChange(path="legit.py", action="create", content="x = 1\n"),
        adapter.FileChange(path="../escape.txt", action="create", content="pwned\n"),
    ]

    with pytest.raises(ValueError):
        adapter._write_files(repo, files)

    assert not (repo / "legit.py").exists()  # nothing half-applied


def test_run_returns_failure_instead_of_raising_on_escape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_configs(tmp_path, "models:\n  claude:\n    model: claude-sonnet-5\n")

    plan = adapter.CodeChangePlan(
        summary="sneaky",
        files=[adapter.FileChange(path="../../escape.txt", action="create", content="pwned\n")],
    )

    class FakeResponse:
        parsed_output = plan

    class FakeMessages:
        def parse(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(adapter.anthropic, "Anthropic", lambda: FakeClient())

    result = adapter.run(AgentTask(agent="backend", description="x", repo_root=tmp_path))

    assert result.success is False
    assert "outside the repo" in result.summary


def test_model_id_extracts_configured_model() -> None:
    config = {"models": {"claude": {"model": "claude-sonnet-5"}}}

    assert adapter._model_id(config) == "claude-sonnet-5"


def test_model_id_missing_key_raises_config_error() -> None:
    with pytest.raises(ConfigError):
        adapter._model_id({"models": {}})


def test_run_raises_config_error_on_malformed_models_yaml(tmp_path: Path) -> None:
    _write_configs(tmp_path, "models: {}\n")

    task = AgentTask(agent="backend", description="do x", repo_root=tmp_path)

    with pytest.raises(ConfigError):
        adapter.run(task)


def test_run_writes_files_and_returns_summary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_configs(tmp_path, "models:\n  claude:\n    model: claude-sonnet-5\n")

    plan = adapter.CodeChangePlan(
        summary="added a helper",
        files=[adapter.FileChange(path="helper.py", action="create", content="def helper(): pass\n")],
    )

    class FakeResponse:
        parsed_output = plan

    class FakeMessages:
        def parse(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(adapter.anthropic, "Anthropic", lambda: FakeClient())

    task = AgentTask(agent="backend", description="add a helper", repo_root=tmp_path)
    result = adapter.run(task)

    assert result.success is True
    assert result.summary == "added a helper"
    assert (tmp_path / "helper.py").exists()


def test_run_returns_failure_when_parsed_output_is_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_configs(tmp_path, "models:\n  claude:\n    model: claude-sonnet-5\n")

    class FakeResponse:
        parsed_output = None

    class FakeMessages:
        def parse(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(adapter.anthropic, "Anthropic", lambda: FakeClient())

    task = AgentTask(agent="backend", description="add a helper", repo_root=tmp_path)
    result = adapter.run(task)

    assert result.success is False
