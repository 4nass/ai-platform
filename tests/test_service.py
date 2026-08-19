from pathlib import Path
import threading
from core import service

def _config(tmp_path):
    return service.ServiceConfig(tmp_path, required_paths=(tmp_path/"mount",))

def test_health_reports_mount_and_provider_state(monkeypatch, tmp_path):
    (tmp_path/"mount").mkdir()
    monkeypatch.setattr(service.doctor, "_provider_checks", lambda _: [service.doctor.Check("provider", "WARN", "optional")])
    report = service.health(_config(tmp_path))
    assert report.ready
    assert any(c.name.startswith("Mount") and c.status=="PASS" for c in report.readiness)

def test_health_fails_missing_mount(monkeypatch, tmp_path):
    monkeypatch.setattr(service.doctor, "_provider_checks", lambda _: [])
    report = service.health(_config(tmp_path))
    assert not report.ready
    assert any(c.status=="FAIL" for c in report.readiness)

def test_run_once_reconciles_and_drains(tmp_path):
    calls=[]
    config=service.ServiceConfig(tmp_path)
    def healthy(_): return service.HealthReport((service.HealthCheck("p","PASS","ok"),), ())
    code=service.run_forever(config, once=True, health_fn=healthy,
        reconcile_fn=lambda root: calls.append("reconcile"),
        drain_fn=lambda root, limit=1: calls.append("drain") or 0)
    assert code == 0
    assert calls == ["reconcile","drain"]

def test_env_file_is_explicit_and_does_not_execute(tmp_path, monkeypatch):
    env=tmp_path/"service.env"; env.write_text("AI_PLATFORM_SERVICE_IDLE=2\n# comment\n")
    service.load_env_file(env)
    assert monkeypatch
    assert service.ServiceConfig.from_env(tmp_path).idle_seconds == 2.0

def test_env_file_overrides_an_inherited_setting(tmp_path, monkeypatch):
    env = tmp_path / "service.env"
    env.write_text("AI_PLATFORM_SERVICE_IDLE=2\n", encoding="utf-8")
    monkeypatch.setenv("AI_PLATFORM_SERVICE_IDLE", "99")

    service.load_env_file(env)

    assert service.ServiceConfig.from_env(tmp_path).idle_seconds == 2.0
