"""Planner (v1): breaks the request down into tasks.

Prototype 1 doesn't actually break the request down: a single task,
executed synchronously by the scheduler. Multi-step breakdown will come
with Hermes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Task:
    request: str


def plan(request: str) -> list[Task]:
    return [Task(request=request)]
