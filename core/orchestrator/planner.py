"""Planner (v1) : découpage de la demande en tâches.

Le prototype 1 ne découpe pas la demande : une tâche unique, exécutée
synchrone par le scheduler. Le découpage multi-étapes viendra avec Hermes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Task:
    request: str


def plan(request: str) -> list[Task]:
    return [Task(request=request)]
