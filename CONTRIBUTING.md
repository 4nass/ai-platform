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

`core/orchestrator/test_runner.py` runs the same suite (`uv run pytest -q`) against a task's branch after an agent makes changes, so keeping it green is what Hermes itself checks before reporting a task as passing.
