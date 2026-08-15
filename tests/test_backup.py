from pathlib import Path
import sqlite3
import pytest
from core import backup
from core.jobs import store

def test_create_backup_is_wal_aware_and_retains_snapshots(tmp_path: Path):
    engine = tmp_path / "engine"; engine.mkdir()
    store.submit(engine, project="demo", request="one")
    result = backup.create(engine, now=__import__("datetime").datetime(2026,1,1,tzinfo=__import__("datetime").timezone.utc))
    assert result.files == ("jobs.sqlite",)
    assert (result.path/"manifest.json").is_file()
    assert (result.path/"jobs.sqlite").stat().st_mode & 0o777 == 0o600
    second = backup.create(engine, now=__import__("datetime").datetime(2026,1,2,tzinfo=__import__("datetime").timezone.utc), keep=1)
    assert second.path.is_dir()
    assert len([p for p in (engine/"backups").iterdir() if p.is_dir()]) == 1

def test_restore_rejects_corruption_and_active_jobs(tmp_path: Path):
    engine = tmp_path/"engine"; engine.mkdir()
    store.submit(engine, project="demo", request="one")
    snap = backup.create(engine).path
    with (snap/"jobs.sqlite").open("ab") as stream: stream.write(b"corrupt")
    with pytest.raises(backup.BackupError, match="checksum"):
        backup.restore(engine, snap, force=True)
    clean = backup.create(engine, destination=tmp_path/"snapshots").path
    with pytest.raises(backup.BackupError, match="active jobs"):
        backup.restore(engine, clean)

def test_restore_replaces_valid_database(tmp_path: Path):
    engine = tmp_path/"engine"; engine.mkdir()
    job = store.submit(engine, project="demo", request="one")
    store.cancel(engine, job.id)
    snap = backup.create(engine).path
    with store.connect(engine) as con:
        con.execute("DELETE FROM job_events")
        con.execute("DELETE FROM jobs")
    assert store.recent(engine) == []
    restored = backup.restore(engine, snap)
    assert restored == ("jobs.sqlite",)
    assert len(store.recent(engine)) == 1
