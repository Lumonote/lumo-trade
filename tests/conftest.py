import sqlite3
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_kronos_env(monkeypatch):
    """清掉 RuntimeConfigurationService.apply_environment() 导出的进程级环境变量。

    任何模块级裸 `import webui.core`(pytest 收集期、无 KRONOS_USER_DIR)都会把
    KRONOS_CONFIG_DIR=真实用户配置目录 写进 os.environ 并常驻整个 pytest 进程;
    后续用 KRONOS_USER_DIR=tmp_path「隔离」重载的 fixture(如 robyn_module)在
    configuration_service.__init__ 里会优先读到这个泄漏值,把测试写入(含
    /api/settings/tushare 的占位 token 12345678901234567890)打到真实配置上,
    清掉用户的真 Tushare Token(2026-07-23 实际发生)。这里在每个测试前统一
    delenv,让隔离 fixture 的路径解析回落到 KRONOS_USER_DIR;需要这些变量的
    测试仍可在自身 monkeypatch.setenv(在本 fixture 之后生效,互不影响)。
    """
    for var in (
        "KRONOS_CONFIG_DIR",
        "KRONOS_DATA_DIR",
        "KRONOS_RESULTS_DIR",
        "KRONOS_LOGS_DIR",
        "KRONOS_MODELS_DIR",
        "TUSHARE_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """提供临时 SQLite 数据库路径，测试结束自动清理。"""
    return tmp_path / "test_fingerprints.db"


@pytest.fixture
def isolated_price_db(tmp_path, monkeypatch):
    from data_store import connection

    monkeypatch.setenv('KRONOS_SQLITE_PATH', str(tmp_path / 'prices.sqlite'))
    monkeypatch.setattr(connection._local, 'conn', None, raising=False)
    monkeypatch.setattr(connection, '_schema_applied', False)
    try:
        yield connection.get_conn()
    finally:
        connection.close_conn()


@pytest.fixture
def isolated_kline(isolated_price_db, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from webui.services import kline_service

    clock = datetime(2026, 6, 3, 11, 0, tzinfo=ZoneInfo('Asia/Shanghai'))
    monkeypatch.setattr(kline_service.StockKlineService, '_clock', staticmethod(lambda: clock))
    monkeypatch.setattr(kline_service, 'fetch_tencent', lambda *args: [])
    return clock
