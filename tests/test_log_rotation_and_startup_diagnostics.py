import os
import tempfile
from pathlib import Path

from utils.db_safety import collect_sqlite_runtime_diagnostics
from utils.logger import clear_log_file, describe_runtime_log_config
from utils import logger


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


class TestStartupLoggingDiagnostics:
    """启动时把「日志配置」交代清楚。

    为什么值得一条用例：日志配置是**出问题之后才想起来问**的东西。事后翻日志的人
    看得见「日志里有什么」，看不见「本来该写进来却没写的是什么」——少开了一类日志、
    热路径被采样成了计数，这些事实不在启动时说一句，只能靠猜。实测踩过：有人开了
    `LOG_GIT=false` 之后找了半天「为什么没有 git 日志」。
    """

    def test_it_reports_the_categories_that_are_off(self, monkeypatch):
        monkeypatch.setitem(logger.LOG_LEVEL, "GIT_VERBOSE", False)
        monkeypatch.setitem(logger.LOG_LEVEL, "DETAIL_VERBOSE", False)

        line = describe_runtime_log_config()

        assert line.startswith("📋 日志配置:"), line
        assert "GIT" in line, f"关掉的 GIT 没被报出来：{line}"
        assert "DETAIL" in line, f"关掉的 DETAIL 没被报出来：{line}"

    def test_it_says_so_when_nothing_is_off(self, monkeypatch):
        for category in logger._LOG_CATEGORIES:
            monkeypatch.setitem(logger.LOG_LEVEL, f"{category}_VERBOSE", True)

        line = describe_runtime_log_config()

        assert "关闭的类目=无" in line, line

    def test_it_reports_the_hot_path_sampling_mode(self, monkeypatch):
        for mode in ("aggregate", "all", "off"):
            monkeypatch.setenv("LOG_SAMPLE_MODE", mode)
            assert f"热路径采样={mode}" in describe_runtime_log_config()
        monkeypatch.delenv("LOG_SAMPLE_MODE", raising=False)
        assert "热路径采样=aggregate" in describe_runtime_log_config(), (
            "默认口径必须与 services/log_sampling.py 一致（聚合），否则这句会骗人"
        )

    def test_it_is_printed_at_startup_and_the_log_file_still_starts_empty(self, monkeypatch):
        """诊断行必须打出来，而且**不能写进 runlog.log**。

        `runlog.log` 从空开始是启动契约（上一版用例钉着它）；诊断行走
        `_original_print` 只到控制台，不能顺手把这条契约破坏掉。
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            monkeypatch.setenv("LOG_DIR", tmp_dir)
            printed: list[str] = []
            monkeypatch.setattr(logger, "_original_print", printed.append)

            clear_log_file()

            assert any("日志文件已轮转并初始化" in line for line in printed), printed
            assert any("📋 日志配置:" in line for line in printed), (
                f"启动时没有报告日志配置：{printed}"
            )
            runlog = Path(tmp_dir) / "runlog.log"
            assert runlog.read_text(encoding="utf-8") == "", (
                "诊断行被写进 runlog.log 了，启动契约（日志从空开始）被破坏"
            )


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
