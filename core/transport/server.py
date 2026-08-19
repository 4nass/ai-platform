"""Development server wiring for the transport API."""
from __future__ import annotations
import json
import logging
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

def _configure_access_logging() -> None:
    """Give the built-in development server useful, redacted access logs.

    A production WSGI host owns its logging configuration; this only makes the
    CLI server observable without changing an embedding application's handlers.
    """
    logger = logging.getLogger("ai_platform.transport.access")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False


def _loopback(host: str) -> bool:
    return host in {"127.0.0.1", "::1", "localhost"}


def _remote_allowed(engine_root: Path, host: str) -> None:
    """Re-run the gate at the moment of binding, and refuse on any blocker.

    A readiness report is produced ahead of deployment, against the
    configuration of that moment. Between then and now the environment can have
    changed — an attestation expired, a credential rotated away, a bind address
    moved. So the report is not evidence a server may rely on; it is recomputed
    here, where the consequence actually lands.

    The environment switch below is kept as a second line rather than replaced
    by the gate. It is cruder and it is independent, which is exactly what a
    last defence should be: a bug in the readiness logic must not be enough on
    its own to open a socket.
    """
    if _loopback(host):
        return

    from core import attestations, security_readiness

    if os.environ.get("AI_PLATFORM_REMOTE_ENABLED", "").lower() not in {"1", "true", "yes", "on"}:
        raise RuntimeError("remote exposure is disabled; set AI_PLATFORM_REMOTE_ENABLED=true explicitly")

    env = dict(os.environ)
    env.setdefault("AI_PLATFORM_BIND_HOST", host)
    report = security_readiness.evaluate(engine_root, env=env)
    attestations.record_decision(
        engine_root,
        decision=report.decision,
        fingerprint=report.fingerprint,
        remote_ready=report.remote_ready,
        actor="server:bind",
        context=f"bind {host}",
        report=security_readiness.report_json(report),
    )
    if not report.remote_ready:
        blockers = "; ".join(f"{check.name}: {check.detail}" for check in report.failures)
        raise RuntimeError(
            f"refusing a non-loopback bind on {host}: readiness is {report.decision}. {blockers}"
        )


def serve(engine_root: Path, *, host: str = "127.0.0.1", port: int = 8787) -> None:
    engine_root = Path(engine_root)
    _remote_allowed(engine_root, host)
    _configure_access_logging()
    auth = authenticator_from_env(engine_root)
    with make_server(
        host, port, create_app(engine_root, auth), server_class=ThreadedWSGIServer
    ) as server:
        server.serve_forever()
