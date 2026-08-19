"""Provider-neutral authenticated ephemeral preview lifecycle (#34)."""
from __future__ import annotations
import hashlib, json, secrets, sqlite3, uuid
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from core.actions.executor import ActionContext, ActionError, ActionResult, CleanupResult, PreviewDeployPlan
from core.jobs import approvals, store
from core.orchestrator import registry

REQUESTED="requested"; DEPLOYING="deploying"; READY="ready"; FAILED="failed"
EXPIRED="expired"; SUPERSEDED="superseded"; CLEANING="cleaning"; CLEANED="cleaned"
CLEANUP_FAILED="cleanup_failed"; CANCELLED="cancelled"
TERMINAL=frozenset({FAILED,EXPIRED,SUPERSEDED,CLEANED,CLEANUP_FAILED,CANCELLED})
SCHEMA="""CREATE TABLE IF NOT EXISTS previews (
id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, job_id INTEGER, run_id INTEGER,
project_id TEXT NOT NULL, principal TEXT NOT NULL, service TEXT NOT NULL, environment TEXT NOT NULL,
commit_sha TEXT NOT NULL, config_sha256 TEXT NOT NULL DEFAULT '', data_mode TEXT NOT NULL,
fingerprint TEXT NOT NULL, provider TEXT NOT NULL DEFAULT '', external_id TEXT NOT NULL DEFAULT '',
url TEXT NOT NULL DEFAULT '', logs_url TEXT NOT NULL DEFAULT '', auth_mode TEXT NOT NULL DEFAULT '',
capability_hash TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, ttl_seconds INTEGER NOT NULL,
requested_at TEXT NOT NULL, started_at TEXT, expires_at TEXT, finished_at TEXT,
error_code TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_previews_job ON previews(job_id);
CREATE INDEX IF NOT EXISTS idx_previews_run ON previews(project_id,run_id);
CREATE TABLE IF NOT EXISTS preview_events (
id INTEGER PRIMARY KEY, preview_id TEXT NOT NULL, event TEXT NOT NULL, at TEXT NOT NULL,
actor TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL DEFAULT '{}');"""

class PreviewError(Exception): pass
class PreviewReplayError(PreviewError): pass

@dataclass(frozen=True)
class PreviewDeployment:
    provider: str; external_id: str; url: str; source_commit: str; auth_mode: str
    logs_url: str = ""; status: str = READY

@dataclass(frozen=True)
class PreviewCleanup:
    ok: bool; summary: str = ""

@dataclass(frozen=True)
class PreviewContext:
    engine_root: Path; project: registry.Project; principal: str; credentials: object
    capability_token: str; preview_id: str; job_id: int | None; run_id: int | None

class PreviewProvider(Protocol):
    def deploy(self, plan: PreviewDeployPlan, context: PreviewContext) -> PreviewDeployment: ...
    def cleanup(self, preview: "PreviewRecord", context: PreviewContext) -> PreviewCleanup: ...

@dataclass(frozen=True)
class PreviewRecord:
    id: str; request_id: str; job_id: int | None; run_id: int | None; project_id: str
    principal: str; service: str; environment: str; commit_sha: str; config_sha256: str
    data_mode: str; fingerprint: str; provider: str; external_id: str; url: str; logs_url: str
    auth_mode: str; status: str; ttl_seconds: int; requested_at: str; started_at: str | None
    expires_at: str | None; finished_at: str | None; error_code: str
    def safe_dict(self):
        return {"preview_id":self.id, **{k:getattr(self,k) for k in (
            "request_id","job_id","run_id","project_id","service","environment","commit_sha",
            "config_sha256","data_mode","provider","external_id","url","logs_url","auth_mode",
            "status","ttl_seconds","requested_at","started_at","expires_at","finished_at","error_code")}}

@contextmanager
def _connect(root):
    with store.connect(Path(root)) as con:
        con.executescript(SCHEMA)
        yield con
def _row(row): return PreviewRecord(**{k:row[k] for k in PreviewRecord.__dataclass_fields__})
def get(root,pid):
    with _connect(root) as con: row=con.execute("SELECT * FROM previews WHERE id=?",(pid,)).fetchone()
    if row is None: raise PreviewError("preview not found")
    return _row(row)
def get_for_job(root,job_id):
    with _connect(root) as con: row=con.execute(
        "SELECT * FROM previews WHERE job_id=? ORDER BY requested_at DESC LIMIT 1",(job_id,)).fetchone()
    return _row(row) if row else None
def events(root,pid):
    get(root,pid)
    with _connect(root) as con: rows=con.execute(
        "SELECT event,at,actor,payload FROM preview_events WHERE preview_id=? ORDER BY id",(pid,)).fetchall()
    return [{**dict(r),"payload":json.loads(r["payload"] or "{}")} for r in rows]

class PreviewManager:
    def __init__(self,engine_root,provider,*,allowed_hosts=("preview.example.com",),
                 credential_provider=None,clock=None):
        self.engine_root=Path(engine_root); self.provider=provider
        self.allowed_hosts=tuple(h.lower().rstrip(".") for h in allowed_hosts if h)
        if not self.allowed_hosts: raise ValueError("at least one preview host is required")
        self.credential_provider=credential_provider
        self.clock=clock or (lambda:datetime.now(timezone.utc).isoformat())
        with _connect(self.engine_root): pass
    def _now(self):
        value=self.clock()
        if isinstance(value,datetime): return value.astimezone(timezone.utc).isoformat()
        if isinstance(value,(int,float)): return datetime.fromtimestamp(value,timezone.utc).isoformat()
        return str(value)
    def deploy(self,plan,*,project,principal,request_id,job_id=None,run_id=None,credentials=None,cancel_event=None):
        self._validate(plan,project,request_id)
        fp=approvals.fingerprint(plan.action,plan.target,plan.detail()); old=self._find(request_id)
        if old:
            if old.fingerprint!=fp:
                self._audit(old.id,"refused.replay",principal,{"reason":"fingerprint mismatch"})
                raise PreviewReplayError("request id was already used for different preview inputs")
            return old
        if run_id is not None: self._supersede(project,principal,run_id,credentials)
        pid=str(uuid.uuid4()); now=self._now(); token=secrets.token_urlsafe(32)
        digest=hashlib.sha256(token.encode()).hexdigest()
        with _connect(self.engine_root) as con:
            con.execute("INSERT INTO previews(id,request_id,job_id,run_id,project_id,principal,service,environment,"
                        "commit_sha,config_sha256,data_mode,fingerprint,status,capability_hash,ttl_seconds,requested_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (pid,request_id,job_id,run_id,project.id,principal,plan.service,plan.environment,
                         plan.commit_sha,plan.config_sha256,plan.data_mode,fp,REQUESTED,digest,plan.ttl_seconds,now))
            self._event(con,pid,"requested",principal,{"commit_sha":plan.commit_sha,"job_id":job_id,"run_id":run_id})
        self._emit(job_id,"preview.requested",{"preview_id":pid,"commit_sha":plan.commit_sha})
        if cancel_event is not None and cancel_event.is_set():
            self._transition(pid,CANCELLED,principal,"cancelled",{"reason":"cancelled before deploy"})
            return get(self.engine_root,pid)
        self._transition(pid,DEPLOYING,principal,"deploying",{"commit_sha":plan.commit_sha})
        self._emit(job_id,"preview.deploying",{"preview_id":pid})
        deployment = None
        try:
            creds=credentials
            if creds is None and self.credential_provider: creds=self.credential_provider.get(project.id,plan.action)
            ctx=PreviewContext(self.engine_root,project,principal,creds,token,pid,job_id,run_id)
            deployment=self.provider.deploy(plan,ctx); self._validate_deployment(deployment,plan)
            expires=(datetime.fromisoformat(now)+timedelta(seconds=plan.ttl_seconds)).isoformat()
            access=self._access_url(deployment.url,deployment.auth_mode,token)
            status=READY if deployment.status==READY else DEPLOYING
            with _connect(self.engine_root) as con:
                con.execute("UPDATE previews SET provider=?,external_id=?,url=?,logs_url=?,auth_mode=?,status=?,"
                            "started_at=?,expires_at=?,finished_at=?,error_code='' WHERE id=?",
                            (deployment.provider,deployment.external_id,access,deployment.logs_url,deployment.auth_mode,
                             status,now,expires,now if status==READY else None,pid))
                self._event(con,pid,"ready" if status==READY else "provider.accepted",principal,
                            {"provider":deployment.provider,"commit_sha":plan.commit_sha})
            self._emit(job_id,"preview.ready" if status==READY else "preview.accepted",
                       {"preview_id":pid,"url":access,"expires_at":expires,"commit_sha":plan.commit_sha})
        except Exception as exc:
            if deployment is not None:
                try:
                    cleanup = self.provider.cleanup(
                        get(self.engine_root,pid),
                        PreviewContext(self.engine_root,project,principal,creds,"",pid,job_id,run_id),
                    )
                    self._audit(pid,"cleanup.result",principal,{"ok":cleanup.ok,"reason":"failed deployment"})
                except Exception as cleanup_exc:
                    self._audit(pid,"cleanup.failure",principal,
                                {"error":type(cleanup_exc).__name__,"reason":"failed deployment"})
            self._transition(pid,FAILED,principal,"provider.failure",{"error":type(exc).__name__},error_code=type(exc).__name__)
            self._emit(job_id,"preview.failed",{"preview_id":pid,"error":type(exc).__name__})
        return get(self.engine_root,pid)
    def cleanup(self,pid,*,principal,project,credentials=None,reason="requested"):
        p=get(self.engine_root,pid)
        if p.status in {CLEANED,SUPERSEDED,CLEANUP_FAILED}: return p
        self._transition(pid,CLEANING,principal,"cleanup.requested",{"reason":reason})
        try:
            creds=credentials
            if creds is None and self.credential_provider: creds=self.credential_provider.get(project.id,"preview_deploy")
            out=self.provider.cleanup(p,PreviewContext(self.engine_root,project,principal,creds,"",p.id,p.job_id,p.run_id))
            target=CLEANED if out.ok else CLEANUP_FAILED
            self._transition(pid,target,principal,"cleanup.result",{"ok":out.ok},error_code="" if out.ok else "cleanup_failed")
            self._emit(p.job_id,"preview.cleaned" if out.ok else "preview.cleanup_failed",{"preview_id":pid,"reason":reason})
        except Exception as exc:
            self._transition(pid,CLEANUP_FAILED,principal,"cleanup.failure",{"error":type(exc).__name__},error_code=type(exc).__name__)
            self._emit(p.job_id,"preview.cleanup_failed",{"preview_id":pid,"reason":reason})
        return get(self.engine_root,pid)
    def cleanup_for_request(self,request_id,*,principal,project,credentials=None):
        p=self._find(request_id)
        return self.cleanup(p.id,principal=principal,project=project,credentials=credentials,reason="action failure") if p else None
    def reconcile(self,*,project_resolver=None,principal="system:preview-reconciler"):
        now=self._now()
        with _connect(self.engine_root) as con: rows=con.execute(
            "SELECT * FROM previews WHERE status IN (?,?) AND expires_at<=? ORDER BY id",(READY,DEPLOYING,now)).fetchall()
        out=[]
        for row in rows:
            p=_row(row); self._transition(p.id,EXPIRED,principal,"expired",{"expires_at":p.expires_at})
            self._emit(p.job_id,"preview.expired",{"preview_id":p.id,"expires_at":p.expires_at})
            out.append(self.cleanup(p.id,principal=principal,project=project_resolver(p.project_id),reason="ttl expired")
                       if project_resolver else get(self.engine_root,p.id))
        return out
    def authorize_capability(self,token):
        if not token or len(token)>512: raise PreviewError("invalid preview capability")
        digest=hashlib.sha256(token.encode()).hexdigest(); now=self._now()
        with _connect(self.engine_root) as con: row=con.execute(
            "SELECT * FROM previews WHERE capability_hash=? AND status IN (?,?) AND expires_at>?",
            (digest,DEPLOYING,READY,now)).fetchone()
        if not row: raise PreviewError("preview capability is invalid or expired")
        return _row(row)
    def _validate(self,plan,project,request_id):
        if plan.project_id!=project.id: raise PreviewError("preview project does not match registry")
        if not request_id or len(request_id)>200 or not all(c.isprintable() for c in request_id): raise PreviewError("preview request id is invalid")
        if len(plan.commit_sha)!=40 or any(c not in "0123456789abcdefABCDEF" for c in plan.commit_sha): raise PreviewError("preview must pin a 40-character hexadecimal commit SHA")
        if not project.remote: raise PreviewError("preview project has no configured remote")
    def _validate_deployment(self,d,plan):
        if d.source_commit!=plan.commit_sha: raise PreviewError("provider deployed a different commit")
        if d.auth_mode not in {"provider","capability"}: raise PreviewError("provider auth or capability auth is required")
        self._validate_url(d.url,"preview URL")
        if d.logs_url: self._validate_url(d.logs_url,"preview logs URL")
        if d.status not in {READY,DEPLOYING}: raise PreviewError("unsupported preview status")
    def _validate_url(self,value,label):
        p=urlsplit(value)
        if p.scheme!="https" or p.username or p.password or p.fragment or p.query or not p.hostname: raise PreviewError(f"{label} must be an HTTPS URL without credentials or query parameters")
        host=p.hostname.lower().rstrip(".")
        if not any(host==h or host.endswith("."+h) for h in self.allowed_hosts): raise PreviewError(f"{label} is outside the configured preview domain")
    @staticmethod
    def _access_url(url, mode, token):
        """Return only an URL that remains safe to place in an artifact.

        A bearer in a query string leaks through browser history, Referer and
        proxy logs. The manager cannot set a cross-origin secure cookie or
        header, so capability mode stays fail-closed until a provider-specific
        edge exchange is implemented.
        """
        if mode == "capability":
            raise PreviewError(
                "capability previews require a secure provider edge token exchange"
            )
        return url
    def _find(self,request_id):
        with _connect(self.engine_root) as con: row=con.execute("SELECT * FROM previews WHERE request_id=?",(request_id,)).fetchone()
        return _row(row) if row else None
    def _supersede(self,project,principal,run_id,credentials):
        with _connect(self.engine_root) as con: rows=con.execute(
            "SELECT * FROM previews WHERE project_id=? AND run_id=? AND status IN (?,?,?)",
            (project.id,run_id,REQUESTED,DEPLOYING,READY)).fetchall()
        for row in rows:
            p=_row(row); self._transition(p.id,SUPERSEDED,principal,"superseded",{"run_id":run_id})
            try:
                creds=credentials
                if creds is None and self.credential_provider: creds=self.credential_provider.get(project.id,"preview_deploy")
                out=self.provider.cleanup(p,PreviewContext(self.engine_root,project,principal,creds,"",p.id,p.job_id,p.run_id))
                self._audit(p.id,"cleanup.result",principal,{"ok":out.ok,"reason":"superseded"})
                self._emit(p.job_id,"preview.cleaned" if out.ok else "preview.cleanup_failed",{"preview_id":p.id,"reason":"superseded"})
            except Exception as exc:
                self._audit(p.id,"cleanup.failure",principal,{"error":type(exc).__name__,"reason":"superseded"})
                self._emit(p.job_id,"preview.cleanup_failed",{"preview_id":p.id,"reason":"superseded"})
    def _transition(self,pid,status,actor,event,payload,*,error_code=""):
        current=get(self.engine_root,pid)
        if current.status==status or (current.status in TERMINAL and status not in {CLEANING,CLEANED}): return
        now=self._now()
        with _connect(self.engine_root) as con:
            con.execute("UPDATE previews SET status=?,finished_at=?,error_code=? WHERE id=?",
                        (status,now if status in TERMINAL or status==READY else current.finished_at,error_code,pid))
            self._event(con,pid,event,actor,payload)
    def _audit(self,pid,event,actor,payload):
        with _connect(self.engine_root) as con: self._event(con,pid,event,actor,payload)
    @staticmethod
    def _event(con,pid,event,actor,payload):
        con.execute("INSERT INTO preview_events(preview_id,event,at,actor,payload) VALUES(?,?,?,?,?)",
                    (pid,event,datetime.now(timezone.utc).isoformat(),actor,json.dumps(payload or {},sort_keys=True)))
    def _emit(self,job_id,event,payload):
        if job_id is None: return
        try: store.emit_event(self.engine_root,job_id,event,payload=payload)
        except Exception: pass

class PreviewActionHandler:
    def __init__(self,manager): self.manager=manager
    def execute(self,plan,context:ActionContext):
        if not isinstance(plan,PreviewDeployPlan): raise ActionError("preview handler received a different action")
        r=self.manager.deploy(plan,project=context.project,principal=context.principal,request_id=context.request_id,
                              job_id=context.job_id,run_id=context.run_id,credentials=context.credentials,cancel_event=context.cancel_event)
        return ActionResult(r.status in {READY,DEPLOYING},"preview",f"preview {r.status}",r.id)
    def cleanup(self,plan,context:ActionContext):
        r=self.manager.cleanup_for_request(context.request_id,principal=context.principal,project=context.project,credentials=context.credentials)
        return CleanupResult(True,"no preview to clean") if r is None else CleanupResult(r.status==CLEANED,f"preview cleanup {r.status}")
