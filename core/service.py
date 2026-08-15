"""Managed local worker service, health probes and bounded restart backoff (#40)."""
from __future__ import annotations
import json, logging, os, socket, signal, sys, threading, time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable
from core import doctor
from core.jobs import store, worker

@dataclass(frozen=True)
class ServiceConfig:
    engine_root: Path
    required_paths: tuple[Path,...] = ()
    network: tuple[str,int] | None = None
    idle_seconds: float = 1.0
    max_backoff: float = 60.0
    log_path: Path | None = None
    log_bytes: int = 10*1024*1024
    log_backups: int = 5
    @classmethod
    def from_env(cls,engine_root:Path):
        paths=tuple(Path(p).expanduser() for p in os.environ.get("AI_PLATFORM_SERVICE_REQUIRED_PATHS","").split(os.pathsep) if p)
        network=None; raw=os.environ.get("AI_PLATFORM_SERVICE_NETWORK","")
        if raw:
            host,sep,port=raw.rpartition(":")
            if not sep or not host or not port.isdigit(): raise ValueError("AI_PLATFORM_SERVICE_NETWORK must be host:port")
            network=(host,int(port))
        log=os.environ.get("AI_PLATFORM_SERVICE_LOG","")
        return cls(Path(engine_root),paths,network,float(os.environ.get("AI_PLATFORM_SERVICE_IDLE","1")),float(os.environ.get("AI_PLATFORM_SERVICE_MAX_BACKOFF","60")),Path(log).expanduser() if log else None)

def load_env_file(path:Path):
    path=Path(path)
    for number,line in enumerate(path.read_text(encoding="utf-8").splitlines(),1):
        stripped=line.strip()
        if not stripped or stripped.startswith("#"): continue
        key,sep,value=stripped.partition("=")
        if not sep or not key.isidentifier(): raise ValueError(f"invalid service env line {number}")
        os.environ.setdefault(key, value.strip().strip("\"'"))
    return path

@dataclass(frozen=True)
class HealthCheck:
    name: str
    status: str
    detail: str
    def as_dict(self): return {"name":self.name,"status":self.status,"detail":self.detail}

@dataclass(frozen=True)
class HealthReport:
    liveness: tuple[HealthCheck,...]
    readiness: tuple[HealthCheck,...]
    @property
    def ready(self): return not any(c.status=="FAIL" for c in self.readiness)
    def as_dict(self): return {"liveness":[c.as_dict() for c in self.liveness],"readiness":[c.as_dict() for c in self.readiness],"ready":self.ready}

def _sqlite_check(root):
    try:
        with store.connect(root) as con:
            result=con.execute("PRAGMA integrity_check").fetchone()[0]
            if result!="ok": return HealthCheck("SQLite","FAIL",str(result))
            mode=con.execute("PRAGMA journal_mode").fetchone()[0]
        return HealthCheck("SQLite","PASS",f"integrity ok; journal_mode={mode}")
    except Exception as exc:
        return HealthCheck("SQLite","FAIL",f"database unavailable ({type(exc).__name__})")

def _mount_checks(config):
    return tuple(HealthCheck(f"Mount {path}","PASS","available") if path.exists() else HealthCheck(f"Mount {path}","FAIL","required path is not mounted or does not exist") for path in config.required_paths)

def _network_check(config):
    if config.network is None: return HealthCheck("Network prerequisite","PASS","not required")
    host,port=config.network
    try:
        with socket.create_connection((host,port),timeout=2): pass
        return HealthCheck("Network prerequisite","PASS",f"{host}:{port} reachable")
    except OSError:
        return HealthCheck("Network prerequisite","FAIL",f"{host}:{port} is unavailable")

def health(config:ServiceConfig)->HealthReport:
    live=(HealthCheck("Process","PASS",f"pid {os.getpid()}"),)
    ready=[HealthCheck("Engine root","PASS","available") if config.engine_root.is_dir() else HealthCheck("Engine root","FAIL","engine root is missing")]
    ready.extend(_mount_checks(config))
    if config.engine_root.is_dir(): ready.append(_sqlite_check(config.engine_root))
    else: ready.append(HealthCheck("SQLite","FAIL","engine root is unavailable"))
    try:
        callable(worker.drain)
        ready.append(HealthCheck("Worker","PASS","worker loop is importable"))
    except Exception as exc: ready.append(HealthCheck("Worker","FAIL",f"worker unavailable ({type(exc).__name__})"))
    ready.append(_network_check(config))
    try:
        provider_checks=doctor._provider_checks(config.engine_root)
        ready.extend(HealthCheck(c.name,c.status,c.detail) for c in provider_checks)
    except Exception as exc:
        ready.append(HealthCheck("Provider readiness","FAIL",f"cannot inspect providers ({type(exc).__name__})"))
    return HealthReport(live,tuple(ready))

def _logger(config):
    logger=logging.getLogger("ai-platform.service"); logger.setLevel(logging.INFO)
    if logger.handlers: return logger
    if config.log_path:
        config.log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(config.log_path, maxBytes=config.log_bytes, backupCount=config.log_backups)
        config.log_path.chmod(0o600)
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler); logger.propagate=False
    return logger

def run_forever(config:ServiceConfig,*,stop_event=None,once=False,health_fn=health,drain_fn=worker.drain,reconcile_fn=worker.reconcile,sleep_fn=None):
    stop=stop_event or threading.Event(); log=_logger(config); sleeper=sleep_fn or stop.wait
    installed=[]
    def stop_signal(signum,frame):
        log.info("shutdown requested by signal %s",signum); stop.set()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM,signal.SIGINT):
            installed.append((sig,signal.getsignal(sig))); signal.signal(sig,stop_signal)
    backoff=1.0
    try:
        while not stop.is_set():
            report=health_fn(config)
            if not report.ready:
                failed=sum(c.status=="FAIL" for c in report.readiness)
                log.warning("readiness failed: %s check(s)",failed)
                if once: return 1
                sleeper(backoff); backoff=min(config.max_backoff,backoff*2); continue
            try:
                reconcile_fn(config.engine_root)
                ran=drain_fn(config.engine_root,limit=1)
                backoff=1.0
                if once: return 0
                if not ran: sleeper(config.idle_seconds)
            except Exception as exc:
                log.error("worker cycle failed: %s",type(exc).__name__)
                if once: return 1
                sleeper(backoff); backoff=min(config.max_backoff,backoff*2)
        log.info("service stopped after current worker cycle")
        return 0
    finally:
        for sig,old in installed: signal.signal(sig,old)

def health_json(config): return json.dumps(health(config).as_dict(),sort_keys=True)
