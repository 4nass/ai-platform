# Contributing

## Running the test suite

Install dependencies (including the `dev` group, which provides `pytest`) and run:

```bash
uv sync
uv run pytest
```

To run a single file or test:

```bash
uv run pytest tests/test_scheduler.py
uv run pytest tests/test_scheduler.py::test_resolve_provider_known_agent
```

`core/orchestrator/test_runner.py` runs a target repo's own declared test command against a task's branch after an agent makes changes, so keeping it green is what the engine itself checks before reporting a task as passing. For this repo, that command is declared in [`.ai-platform.yml`](.ai-platform.yml) at the root (`uv run pytest -q`) — the engine never assumes `pytest`, since a `--repo` target on a different stack needs its own command (or none: absent, tests are skipped rather than run against a command that doesn't apply).
