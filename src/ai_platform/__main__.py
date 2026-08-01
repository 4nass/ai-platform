"""Lets the CLI be started as `python -m ai_platform`.

The `ai-platform` console script is the way a person runs this. A detached
worker (core.jobs.worker.spawn_detached) needs something it can name without
depending on that script being on the spawning process's PATH — under a
managed service, a cron entry or a gateway, it often isn't — so it spawns
`sys.executable -m ai_platform`, which resolves through the same interpreter
that is already running the engine.
"""

from __future__ import annotations

from ai_platform import main

if __name__ == "__main__":
    main()
