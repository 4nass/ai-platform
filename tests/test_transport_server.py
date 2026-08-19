from __future__ import annotations

from pathlib import Path
from socketserver import ThreadingMixIn

from core.transport import server


class _Server:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def serve_forever(self):
        return None


def test_development_server_is_threaded_for_sse(monkeypatch, tmp_path: Path):
    captured = {}
    monkeypatch.setattr(server, "authenticator_from_env", lambda root: object())

    def make_server(host, port, app, *, server_class):
        captured.update(host=host, port=port, app=app, server_class=server_class)
        return _Server()

    monkeypatch.setattr(server, "make_server", make_server)

    server.serve(tmp_path, host="127.0.0.1", port=9911)

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9911
    assert issubclass(captured["server_class"], ThreadingMixIn)
    assert captured["server_class"].daemon_threads is True
