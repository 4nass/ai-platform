"""WAL-aware backups and explicit restores for the managed service (#40)."""
from __future__ import annotations
import hashlib, json, os, shutil, sqlite3, tempfile, uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DATABASES=("jobs.sqlite","telemetry.sqlite","transport.sqlite")

class BackupError(Exception): pass
@dataclass(frozen=True)
class BackupResult:
    path: Path
    files: tuple[str,...]
    skipped: tuple[str,...]
    created_at: str

def _sha256(path):
    digest=hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b""): digest.update(chunk)
    return digest.hexdigest()

def _mode(path,mode):
    path.chmod(mode)
    return path

def _atomic_json(path,data):
    tmp=path.with_name(path.name+".tmp-"+uuid.uuid4().hex)
    tmp.write_text(json.dumps(data,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    with tmp.open("rb") as stream: os.fsync(stream.fileno())
    os.replace(tmp,path)
    _mode(path,0o600)

def _backup_sqlite(source,destination):
    source_conn=sqlite3.connect(f"file:{source}?mode=ro",uri=True,timeout=10)
    try:
        destination_conn=sqlite3.connect(destination,timeout=10)
        try:
            source_conn.backup(destination_conn)
            result=destination_conn.execute("PRAGMA integrity_check").fetchone()[0]
            if result!="ok": raise BackupError(f"integrity check failed for {source.name}: {result}")
            destination_conn.commit()
        finally: destination_conn.close()
    finally: source_conn.close()

def create(engine_root:Path, destination:Path|None=None, *, keep:int=7, now=None)->BackupResult:
    engine_root=Path(engine_root); root=Path(destination) if destination else engine_root/"backups"
    if keep<1: raise BackupError("backup retention must keep at least one snapshot")
    root.mkdir(parents=True,exist_ok=True); _mode(root,0o700)
    stamp=(now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    final=root/stamp; temp=root/(".tmp-"+stamp+"-"+uuid.uuid4().hex)
    temp.mkdir(mode=0o700)
    files=[]; skipped=[]; metadata={}
    try:
        for name in DATABASES:
            source=engine_root/name
            if not source.is_file():
                skipped.append(name); continue
            target=temp/name; _backup_sqlite(source,target); _mode(target,0o600)
            files.append(name); metadata[name]={"size":target.stat().st_size,"sha256":_sha256(target)}
        created=(now or datetime.now(timezone.utc)).isoformat()
        _atomic_json(temp/"manifest.json",{"version":1,"created_at":created,"files":metadata})
        os.replace(temp,final); _mode(final,0o700)
        snapshots=sorted((p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".tmp-")),reverse=True)
        for old in snapshots[keep:]:
            shutil.rmtree(old)
        return BackupResult(final,tuple(files),tuple(skipped),created)
    except Exception:
        shutil.rmtree(temp,ignore_errors=True)
        raise

def _load_manifest(path):
    try: data=json.loads((path/"manifest.json").read_text(encoding="utf-8"))
    except (OSError,ValueError) as exc: raise BackupError("backup manifest is missing or invalid") from exc
    if data.get("version")!=1 or not isinstance(data.get("files"),dict): raise BackupError("unsupported backup manifest")
    return data

def restore(engine_root:Path, backup_path:Path, *, force=False)->tuple[str,...]:
    engine_root=Path(engine_root); backup_path=Path(backup_path)
    manifest=_load_manifest(backup_path)
    from core.jobs import store
    active=store.recent(engine_root,limit=1000)
    active=[job for job in active if job.state in store.ACTIVE_STATES]
    if active and not force: raise BackupError("active jobs exist; stop the service or pass force explicitly")
    staged=[]
    try:
        for name,info in manifest["files"].items():
            if name not in DATABASES or Path(name).name!=name: raise BackupError("manifest contains an invalid database name")
            source=backup_path/name
            if not source.is_file() or _sha256(source)!=info.get("sha256"): raise BackupError(f"backup file failed checksum: {name}")
            fd,tmp=tempfile.mkstemp(prefix=".restore-",dir=engine_root)
            os.close(fd)
            shutil.copy2(source,tmp); _mode(Path(tmp),0o600)
            check=sqlite3.connect(tmp)
            try:
                if check.execute("PRAGMA integrity_check").fetchone()[0]!="ok": raise BackupError(f"integrity check failed: {name}")
            finally: check.close()
            staged.append((name,Path(tmp)))
        for name,tmp in staged: os.replace(tmp,engine_root/name)
        return tuple(name for name,_ in staged)
    finally:
        for _,tmp in staged:
            if tmp.exists(): tmp.unlink()
