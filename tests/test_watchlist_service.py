"""watchlist_service 代码校验 + 行情源市场前缀（含北交所新代码段 920xxx）。"""
from webui.services.watchlist_service import (
    WatchlistService,
    _VALID_CODE,
    _eastmoney_secid,
    _tencent_symbol,
)


def test_valid_code_accepts_beijing_920():
    """北交所 920xxx 应通过自选代码校验。"""
    assert _VALID_CODE.match("920161")


def test_eastmoney_quote_retains_exchange_date(tmp_path, monkeypatch):
    import datetime
    from zoneinfo import ZoneInfo
    import webui.services.watchlist_service as module
    timestamp = datetime.datetime(2026, 9, 30, 15, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    monkeypatch.setattr(module, "request_json", lambda *a, **k: {"data": {"diff": [
        {"f12": "000001", "f2": 10.5, "f124": timestamp}]}})
    quote = WatchlistService(tmp_path / "watch.json")._quotes_eastmoney(["000001"])["000001"]
    assert quote["quote_date"] == "2026-09-30"


def test_tencent_quote_retains_exchange_date(tmp_path, monkeypatch):
    import webui.services.watchlist_service as module
    fields = [""] * 33
    fields[2], fields[3], fields[30] = "000001", "10.5", "20260930145900"
    monkeypatch.setattr(module, "request_text", lambda *a, **k: 'v_sz000001="' + "~".join(fields) + '";')
    quote = WatchlistService(tmp_path / "watch.json")._quotes_tencent(["000001"])["000001"]
    assert quote["quote_date"] == "2026-09-30"


def test_valid_code_still_accepts_known_segments():
    for code in ("600519", "000001", "300750", "830799", "430047"):
        assert _VALID_CODE.match(code), code


def test_eastmoney_secid_beijing_is_market_0():
    """北交所 920xxx 在东财 secid 中归市场 0（不可因 9 开头被当沪市 1）。"""
    assert _eastmoney_secid("920161") == "0.920161"


def test_eastmoney_secid_shanghai_b_share_is_market_1():
    assert _eastmoney_secid("900001") == "1.900001"
    assert _eastmoney_secid("600519") == "1.600519"


def test_tencent_symbol_beijing_is_bj():
    """北交所 920xxx 腾讯行情前缀须为 bj（不可因 9 开头被当 sh）。"""
    assert _tencent_symbol("920161") == "bj920161"


def test_tencent_symbol_shanghai_b_share_is_sh():
    assert _tencent_symbol("900001") == "sh900001"
    assert _tencent_symbol("600519") == "sh600519"


def test_list_with_quotes_includes_sector_and_return_summary(tmp_path, monkeypatch):
    svc = WatchlistService(tmp_path / "watchlist.json")
    svc.add("600519", "贵州茅台")
    svc.add("000001", "平安银行")
    monkeypatch.setattr(svc, "quotes", lambda codes: {
        "600519": {"name": "贵州茅台", "price": 1800.0, "change_pct": 2.5, "main_net_inflow": 100000000.0},
        "000001": {"name": "平安银行", "price": 12.0, "change_pct": -1.0, "main_net_inflow": -20000000.0},
    })
    monkeypatch.setattr(svc, "_sector_info", lambda code: {
        "600519": {"sector": "白酒", "boards": ["白酒概念"]},
        "000001": {"sector": "银行", "boards": ["银行"]},
    }[code])

    out = svc.list_with_quotes()

    assert out["items"][0]["sector"] == "银行"
    assert out["items"][1]["sector"] == "白酒"
    ret = out["summary"]["return_summary"]
    assert ret["avg_change_pct"] == 0.75
    assert ret["up_count"] == 1
    assert ret["down_count"] == 1
    assert ret["main_net_inflow"] == 80000000.0
    sectors = out["summary"]["sector_summary"]["top_sectors"]
    assert {s["name"] for s in sectors} == {"白酒", "银行"}


def test_new_add_goes_after_pinned_items(tmp_path):
    """置顶后再添加新股票：新股票应排在置顶之后（不压过置顶）。"""
    svc = WatchlistService(tmp_path / "watchlist.json")
    svc.add("600519", "贵州茅台")
    svc.add("000001", "平安银行")
    # 置顶 600519
    svc.pin("600519", True)
    # 添加新股票
    svc.add("300750", "宁德时代")
    codes = [it["code"] for it in svc.list_items()]
    assert codes[0] == "600519", "置顶股票应保持在最上"
    assert codes[1] == "300750", "新添加股票应紧跟置顶之后"
    assert codes[2] == "000001"


def test_pin_then_unpin_restores_order(tmp_path):
    """取消置顶后回到普通排序（非置顶按加入时间倒序）。"""
    svc = WatchlistService(tmp_path / "watchlist.json")
    svc.add("600519", "贵州茅台")
    svc.add("000001", "平安银行")
    svc.pin("600519", True)
    svc.pin("600519", False)  # 取消置顶
    codes = [it["code"] for it in svc.list_items()]
    assert codes == ["000001", "600519"], "取消置顶后按加入时间倒序（后加入在前）"


def test_multiple_pinned_stay_on_top_in_add_order(tmp_path):
    """多个置顶股票保持在最上，新添加股票始终排在其后。"""
    svc = WatchlistService(tmp_path / "watchlist.json")
    svc.add("600519", "贵州茅台")
    svc.add("000001", "平安银行")
    svc.pin("600519", True)
    svc.pin("000001", True)
    svc.add("300750", "宁德时代")
    svc.add("002594", "比亚迪")
    codes = [it["code"] for it in svc.list_items()]
    assert codes[:2] == ["600519", "000001"], "置顶组保持在最上"
    assert codes[2:] == ["002594", "300750"], "新添加的按加入时间倒序排在置顶之后"
