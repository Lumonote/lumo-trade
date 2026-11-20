from datetime import datetime

import pandas as pd
import pytest

from data_store import connection as conn_mod
from data_store import ohlcv_repo


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("KRONOS_SQLITE_PATH", str(tmp_path / "k.sqlite"))
    conn_mod.reset_for_testing()
    yield
    conn_mod.reset_for_testing()


def _sample_df(start="2026-01-02 09:30:00", n=5):
    ts = pd.date_range(start, periods=n, freq="D")
    return pd.DataFrame({
        "timestamps": ts,
        "open": [1.0 + i for i in range(n)],
        "high": [1.5 + i for i in range(n)],
        "low":  [0.5 + i for i in range(n)],
        "close": [1.2 + i for i in range(n)],
        "volume": [100.0 + i for i in range(n)],
        "amount": [1000.0 + i for i in range(n)],
    })


def test_upsert_and_load_roundtrip(tmp_db):
    df_in = _sample_df()
    n = ohlcv_repo.upsert_df("000001", "1d", df_in)
    assert n == len(df_in)
    df_out = ohlcv_repo.load_dataframe("000001", "1d")
    assert len(df_out) == len(df_in)
    assert list(df_out.columns) == ["timestamps", "open", "high", "low", "close", "volume", "amount"]
    assert df_out["close"].iloc[-1] == pytest.approx(df_in["close"].iloc[-1])


def test_upsert_is_idempotent_and_updates(tmp_db):
    df = _sample_df()
    ohlcv_repo.upsert_df("000001", "1d", df)
    df.loc[df.index[-1], "close"] = 999.0
    ohlcv_repo.upsert_df("000001", "1d", df)
    out = ohlcv_repo.load_dataframe("000001", "1d")
    assert out["close"].iloc[-1] == pytest.approx(999.0)
    assert ohlcv_repo.row_count("000001", "1d") == len(df)


def test_load_returns_empty_when_no_data(tmp_db):
    out = ohlcv_repo.load_dataframe("999999", "1d")
    assert len(out) == 0
    assert list(out.columns) == ["timestamps", "open", "high", "low", "close", "volume", "amount"]


def test_load_limit_returns_tail_ordered_ascending(tmp_db):
    ohlcv_repo.upsert_df("000001", "1d", _sample_df(n=10))
    out = ohlcv_repo.load_dataframe("000001", "1d", limit=3)
    assert len(out) == 3
    assert out["close"].iloc[0] < out["close"].iloc[-1]


def test_codes_isolated_by_frequency(tmp_db):
    ohlcv_repo.upsert_df("000001", "1d", _sample_df(n=3))
    ohlcv_repo.upsert_df("000001", "5m", _sample_df(n=7))
    assert ohlcv_repo.row_count("000001", "1d") == 3
    assert ohlcv_repo.row_count("000001", "5m") == 7


def test_end_date_includes_intraday_before_applying_limit(tmp_db):
    ohlcv_repo.upsert_df("000001", "1d", _sample_df(start="2026-06-08 15:00:00"))
    rows = ohlcv_repo.load_dataframe("000001", "1d", end="2026-06-10", limit=2)
    assert rows.timestamps.dt.strftime("%F %H:%M").tolist() == ["2026-06-09 15:00", "2026-06-10 15:00"]
