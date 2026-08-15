"""Development server wiring for the transport API."""
from __future__ import annotations
import json
import os
from pathlib import Path
from wsgiref.simple_server import make_server
from core.transport.auth import Authenticator, ReplayStore, credential_from_mapping
from core.transport.http import create_app

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

def _loopback(host: str) -> bool:
    return host in {"127.0.0.1", "::1", "localhost"}


def _remote_allowed(host: str) -> None:
    if _loopback(host):
        return
    if os.environ.get("AI_PLATFORM_REMOTE_ENABLED", "").lower() not in {"1", "true", "yes", "on"}:
        raise RuntimeError("remote exposure is disabled; set AI_PLATFORM_REMOTE_ENABLED=true explicitly")
    if os.environ.get("AI_PLATFORM_TLS_TERMINATED", "").lower() not in {"1", "true", "yes", "on"}:
        raise RuntimeError("remote exposure requires AI_PLATFORM_TLS_TERMINATED=true")
    if os.environ.get("AI_PLATFORM_RATE_LIMIT", "").lower() not in {"1", "true", "yes", "on"}:
        raise RuntimeError("remote exposure requires AI_PLATFORM_RATE_LIMIT=true")


def serve(engine_root: Path, *, host: str = "127.0.0.1", port: int = 8787) -> None:
    _remote_allowed(host)
    auth = authenticator_from_env(engine_root)
    with make_server(host, port, create_app(engine_root, auth)) as server:
        server.serve_forever()
