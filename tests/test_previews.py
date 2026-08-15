from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from core.actions.executor import ActionExecutor, PreviewDeployPlan, PREVIEW_DEPLOY, SUCCEEDED, WAITING_APPROVAL
from core.jobs import approvals
from core.orchestrator.registry import Project
from core.previews.manager import CLEANED, FAILED, READY, PreviewActionHandler, PreviewCleanup, PreviewDeployment, PreviewManager, PreviewReplayError, events, get
COMMIT="a"*40
OTHER_COMMIT="b"*40
class Clock:
    def __init__(self): self.value=datetime(2026,8,15,tzinfo=timezone.utc)
    def __call__(self): return self.value.isoformat()
    def advance(self,seconds): self.value += timedelta(seconds=seconds)
class Provider:
    def __init__(self,*,commit=None,auth_mode="capability",status=READY,raises=False):
        self.commit=commit; self.auth_mode=auth_mode; self.status=status; self.raises=raises
        self.deployments=0; self.cleanups=0; self.context=None
    def deploy(self,plan,context):
        self.deployments += 1; self.context=context
        if self.raises: raise RuntimeError("provider secret=must-not-be-persisted")
        return PreviewDeployment("fake-ci",f"deploy-{self.deployments}",f"https://run-{self.deployments}.preview.example.com/",self.commit or plan.commit_sha,self.auth_mode,"https://logs.preview.example.com/deploy",self.status)
    def cleanup(self,preview,context):
        self.cleanups += 1; return PreviewCleanup(True,"deleted")
def project(tmp_path,*,approvals_required=()):
    return Project(id="demo",path=tmp_path,remote="https://git.example.com/demo.git",base_branch="main",allowed_actions=(PREVIEW_DEPLOY,),approval_required=tuple(approvals_required))
def plan(commit=COMMIT,*,ttl=60,data_mode="readonly"):
    return PreviewDeployPlan(project_id="demo",service="web",environment="pr-123",commit_sha=commit,ttl_seconds=ttl,data_mode=data_mode)
def manager(tmp_path,provider,clock=None):
    return PreviewManager(tmp_path,provider,allowed_hosts=("preview.example.com",),clock=clock or Clock())
def test_deploy_is_pinned_authenticated_and_audited(tmp_path):
    provider=Provider(); clock=Clock(); m=manager(tmp_path,provider,clock)
    record=m.deploy(plan(),project=project(tmp_path),principal="owner",request_id="preview-1",job_id=7,run_id=11,credentials="opaque-secret")
    assert record.status==READY and record.commit_sha==COMMIT and record.auth_mode=="capability"
    token=record.url.split("=",1)[1]
    assert m.authorize_capability(token).id==record.id
    assert provider.context.credentials=="opaque-secret"
    assert "opaque-secret" not in str(record.safe_dict())
    assert [e["event"] for e in events(tmp_path,record.id)]==["requested","deploying","ready"]
def test_request_replay_with_changed_commit_is_rejected(tmp_path):
    m=manager(tmp_path,Provider()); m.deploy(plan(),project=project(tmp_path),principal="owner",request_id="same")
    with pytest.raises(PreviewReplayError): m.deploy(plan(OTHER_COMMIT),project=project(tmp_path),principal="owner",request_id="same")
def test_provider_cannot_deploy_unapproved_commit_or_public_url(tmp_path):
    wrong=manager(tmp_path,Provider(commit=OTHER_COMMIT)).deploy(plan(),project=project(tmp_path),principal="owner",request_id="wrong")
    assert wrong.status==FAILED
    class PublicProvider(Provider):
        def deploy(self,plan,context): return PreviewDeployment("public","x","https://evil.example.net/",plan.commit_sha,"provider")
    bad=manager(tmp_path,PublicProvider()).deploy(plan(),project=project(tmp_path),principal="owner",request_id="bad")
    assert bad.status==FAILED
def test_ttl_reconciliation_cleans_up_preview(tmp_path):
    clock=Clock(); provider=Provider(); m=manager(tmp_path,provider,clock)
    record=m.deploy(plan(),project=project(tmp_path),principal="owner",request_id="ttl")
    clock.advance(61); result=m.reconcile(project_resolver=lambda _: project(tmp_path))
    assert result[0].id==record.id and result[0].status==CLEANED and provider.cleanups==1
def test_same_run_supersedes_and_cleans_previous_preview(tmp_path):
    provider=Provider(); m=manager(tmp_path,provider)
    first=m.deploy(plan(),project=project(tmp_path),principal="owner",request_id="first",run_id=9)
    second=m.deploy(plan(OTHER_COMMIT),project=project(tmp_path),principal="owner",request_id="second",run_id=9)
    assert get(tmp_path,first.id).status=="superseded" and provider.cleanups==1 and second.status==READY
def test_action_executor_consumes_exact_preview_approval(tmp_path):
    provider=Provider(); m=manager(tmp_path,provider)
    executor=ActionExecutor(tmp_path,{PREVIEW_DEPLOY:PreviewActionHandler(m)})
    waiting=executor.execute(plan(),project=project(tmp_path,approvals_required=(PREVIEW_DEPLOY,)),principal="owner",request_id="action-preview",job_id=9,run_id=12)
    assert waiting.state==WAITING_APPROVAL
    approval=approvals.get(tmp_path,waiting.approval_id); approvals.decide(tmp_path,approval.id,approved=True,principal="owner")
    done=executor.execute(plan(),project=project(tmp_path,approvals_required=(PREVIEW_DEPLOY,)),principal="owner",request_id="action-preview",approval_id=approval.id,job_id=9,run_id=12)
    assert done.state==SUCCEEDED and provider.deployments==1 and get(tmp_path,provider.context.preview_id).run_id==12
def test_invalid_data_mode_is_rejected():
    with pytest.raises(Exception,match="data mode"): plan(data_mode="shared")
def test_provider_failure_does_not_persist_raw_error(tmp_path):
    record=manager(tmp_path,Provider(raises=True)).deploy(plan(),project=project(tmp_path),principal="owner",request_id="failed")
    assert record.status==FAILED and "must-not-be-persisted" not in str(get(tmp_path,record.id))
