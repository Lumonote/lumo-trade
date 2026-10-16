"""板块动量改用东财真实行业板块（中文名 + 热度排序），不写死板块。"""
from __future__ import annotations

import webui.core as core
import pytest
from analysis.panic_indicator import panic_from_indices


@pytest.fixture(autouse=True)
def _offline_indices(monkeypatch):
    monkeypatch.setattr(core.MARKET_PULSE_SERVICE, "indices_payload",
                        lambda: {"indices": [], "panic": panic_from_indices([])})


def _intel(boards):
    return {"eastmoney": {"industry_boards": boards}}


def test_sectors_use_real_industry_boards(monkeypatch):
    boards = [
        {"code": "BK1036", "name": "半导体", "change_pct": 3.21, "main_net_inflow_text": "12.34亿"},
        {"code": "BK0473", "name": "证券", "change_pct": 1.05, "main_net_inflow_text": "3.21亿"},
    ]
    monkeypatch.setattr(core, "_load_market_intelligence", lambda: _intel(boards))

    sectors = core._build_market_dashboard()["sectors"]

    # 中文真实板块名，按抓取的热度顺序透传
    assert [s["name"] for s in sectors] == ["半导体", "证券"]
    assert sectors[0]["avg_change"] == 3.21
    assert sectors[0]["key"] == "BK1036"
    assert sectors[0]["code"] == "BK1036"
    assert sectors[0]["board_code"] == "BK1036"
    assert sectors[0]["main_net_inflow_text"] == "12.34亿"
    # 不再出现合成英文板块名
    assert "AGI" not in {s["name"] for s in sectors}


def test_sectors_empty_when_boards_unavailable(monkeypatch):
    """拉取失败/为空时返回空板块——不回退到写死的合成板块。"""
    monkeypatch.setattr(core, "_load_market_intelligence", lambda: _intel([]))
    assert core._build_market_dashboard()["sectors"] == []
