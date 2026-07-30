"""Tests for core.orchestrator.planner."""

from __future__ import annotations

from core.orchestrator.planner import Task, plan


def test_plan_returns_a_single_task_with_the_request() -> None:
    tasks = plan("Add OAuth2 authentication")

    assert tasks == [Task(request="Add OAuth2 authentication")]
