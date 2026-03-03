import os
import tempfile
from pathlib import Path

from utils.db_safety import collect_sqlite_runtime_diagnostics
from utils.logger import clear_log_file


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_clear_log_file_rotates_and_keeps_at_most_10_backups(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        monkeypatch.setenv("LOG_DIR", tmp_dir)

        log_dir = Path(tmp_dir)
        runlog = log_dir / "runlog.log"
        _write(runlog, "old log content")

        # 预置 12 个历史备份，验证会自动清理最早项
        for idx in range(12):
            ts = f"2026010210{idx:04d}"  # 20260102100000, ...
            _write(log_dir / f"runlog.log.bak.{ts}", f"bak-{idx}")

        clear_log_file()

        assert runlog.exists()
        assert runlog.read_text(encoding="utf-8") == ""

        backups = sorted(log_dir.glob("runlog.log.bak.*"))
        assert len(backups) == 10
        # 最早的 3 个应被清理（12 个旧备份 + 1 个新备份，保留最新 10）
        assert not (log_dir / "runlog.log.bak.20260102100000").exists()
        assert not (log_dir / "runlog.log.bak.20260102100001").exists()
        assert not (log_dir / "runlog.log.bak.20260102100002").exists()


def test_collect_sqlite_runtime_diagnostics_reports_basic_fields():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    try:
        import sqlite3

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE t1(id INTEGER PRIMARY KEY, v TEXT)")
        cur.executemany("INSERT INTO t1(v) VALUES (?)", [(f"v{i}",) for i in range(100)])
        conn.commit()
        cur.execute("DELETE FROM t1")
        conn.commit()
        conn.close()

        uri = f"sqlite:///{db_path}"
        diag = collect_sqlite_runtime_diagnostics(uri)

        assert diag["backend"] == "sqlite"
        assert diag["exists"] is True
        assert diag["sqlite_path"] is not None
        assert diag["db_size_bytes"] >= 0
        assert diag["page_count"] >= 1
        assert diag["freelist_count"] >= 0
        assert 0.0 <= float(diag["free_ratio"]) <= 1.0
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{db_path}{suffix}")
            except OSError:
                pass


def test_collect_sqlite_runtime_diagnostics_for_non_sqlite_uri():
    diag = collect_sqlite_runtime_diagnostics("mysql+pymysql://u:p@127.0.0.1:3306/demo")
    assert diag["backend"] == "mysql"
    assert diag["exists"] is False
