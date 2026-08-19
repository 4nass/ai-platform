"""Development server wiring for the transport API."""
from __future__ import annotations
import json
import os
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server
from core.transport.auth import Authenticator, ReplayStore, credential_from_mapping
from core.transport.http import create_app


class ThreadedWSGIServer(ThreadingMixIn, WSGIServer):
    """Keep a long-lived SSE connection from serializing every API request.

    This is deliberately a small development-server improvement, not a claim
    that wsgiref is a production-grade HTTP deployment. Production still
    needs a managed WSGI/ASGI host and a reverse proxy.
    """

    daemon_threads = True


def authenticator_from_env(engine_root: Path, *, variable: str = "AI_PLATFORM_TRANSPORT_CREDENTIALS") -> Authenticator:
    raw = os.environ.get(variable)
    if not raw:
        raise RuntimeError(f"{variable} is required; provide credentials through a secret manager")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{variable} must contain valid JSON") from exc
    if isinstance(data, dict):
        data = list(data.values())
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"{variable} must be a non-empty JSON list")
    credentials = {}
    for item in data:
        credential = credential_from_mapping(item)
        credentials[credential.key_id] = credential
    return Authenticator(credentials, ReplayStore(Path(engine_root) / "transport.sqlite"))

def serve(engine_root: Path, *, host: str = "127.0.0.1", port: int = 8787) -> None:
    auth = authenticator_from_env(engine_root)
    with make_server(
        host, port, create_app(engine_root, auth), server_class=ThreadedWSGIServer
    ) as server:
        server.serve_forever()
