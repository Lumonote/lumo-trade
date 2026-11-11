import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from webui.services import market_intelligence as market_intelligence_module
from webui.services.market_intelligence import MarketIntelligenceService


class StubMarketIntelligenceService(MarketIntelligenceService):
    def __init__(self):
        super().__init__(ttl_seconds=180)
        self.calls = 0

    def _request_json(self, url, headers=None, timeout=5, trust_env=True):
        self.calls += 1
        if "jin10" in url:
            return {
                "data": [
                    {
                        "id": 1,
                        "time": "10:00:00",
                        "important": True,
                        "data": {
                            "title": "<b>央行发布政策</b>",
                            "source": " 金十 ",
                            "link": "https://example.com/news",
                        },
                    }
                ]
            }
        if "getAllStockChanges" in url:
            # 盘口异动（push2ex）。i 为速度类格式 [幅, 价, 幅]，tm 为 HHMMSS。
            return {
                "data": {
                    "allstock": [
                        {"c": "300001", "n": "测试股", "t": 8201, "i": "0.0589,12.34,0.0589", "tm": 143000},
                    ]
                }
            }
        return {
            "data": {
                "diff": [
                    {
                        "f12": "BK0420",
                        "f14": "银行",
                        "f2": "10.5",
                        "f3": "1.23",
                        "f62": "123456789",
                    }
                ]
            }
        }


def test_load_fetches_and_caches_payload():
    service = StubMarketIntelligenceService()

    first = service.load()
    second = service.load()

    assert first is second
    # 1 金十快讯 + 5 东财 clist（行业 / 概念 / 资金 / 热门股 / 涨幅榜）+ 1 东财盘口异动 = 7 次上游抓取；
    # 第二次 load 命中缓存，不再抓取。
    assert service.calls == 7
    assert first["jinshi"][0]["title"] == "央行发布政策"
    assert first["eastmoney"]["industry_boards"][0]["main_net_inflow_text"] == "1.23亿"
    assert first["eastmoney"]["top_gainers"][0]["change_pct"] == 1.23
    # 盘口异动：类型码映射为中文标签，i 字段启发式解析出价格与涨跌幅，tm 解析为 HH:MM。
    change = first["eastmoney"]["changes"][0]
    assert change["type"] == "火箭发射"
    assert change["change_pct"] == 5.89
    assert change["price"] == 12.34
    assert change["time"] == "14:30"


def test_load_force_refresh_bypasses_cache():
    service = StubMarketIntelligenceService()

    first = service.load()
    second = service.load(force_refresh=True)

    assert first is not second
    assert service.calls == 14


def test_load_captures_source_errors():
    service = MarketIntelligenceService()
    service.fetch_jinshi_flash = lambda limit=12: (_ for _ in ()).throw(RuntimeError("offline"))
    service.fetch_eastmoney_clist = lambda *args, **kwargs: []
    # clist 为空会触发热点回退（人气榜+腾讯）；回退也失败时应捕获错误而非抛出/联网。
    service.fetch_hot_rank = lambda limit=12: (_ for _ in ()).throw(RuntimeError("rank offline"))
    # 板块的腾讯兜底同样不发网(联网机器上不桩会拿到真实板块,断言必挂)
    service.fetch_tencent_boards = lambda board_type, limit=8: []

    payload = service.load()

    assert payload["jinshi"] == []
    assert payload["errors"]["jinshi"] == "offline"
    assert payload["eastmoney"]["industry_boards"] == []
    assert payload["eastmoney"]["hot_stocks"] == []
    assert payload["errors"]["eastmoney_hot_stocks"] == "rank offline"


def test_hot_stocks_falls_back_to_rank_when_clist_empty():
    """clist 返回空（本机 push2 被掐）时，热点应回退到人气榜+腾讯行情。"""
    service = MarketIntelligenceService()
    service.fetch_jinshi_flash = lambda limit=12: []
    service.fetch_xueqiu_hot = lambda limit=12: []
    service.fetch_eastmoney_changes = lambda limit=15: []
    service.fetch_eastmoney_clist = lambda *args, **kwargs: []
    service.fetch_hot_rank = lambda limit=12: [
        {"code": "601991", "name": "大唐发电", "price": 9.18, "change_pct": 9.68,
         "main_net_inflow": None, "main_net_inflow_text": "—", "source": "eastmoney_rank"}
    ]

    payload = service.load()

    assert "eastmoney_hot_stocks" not in payload["errors"]
    hot = payload["eastmoney"]["hot_stocks"]
    assert len(hot) == 1
    assert hot[0]["code"] == "601991"
    assert hot[0]["source"] == "eastmoney_rank"


def test_fetch_eastmoney_clist_bypasses_http_cache():
    """东财热点榜请求要带防缓存参数/头，避免工作台热点模块拿到旧响应。"""
    service = MarketIntelligenceService()
    captured = {}

    def fake_request(url, headers=None, timeout=5, trust_env=True):
        captured["url"] = url
        captured["headers"] = headers or {}
        captured["trust_env"] = trust_env
        return {"data": {"diff": []}}

    service._request_json = fake_request
    service.fetch_eastmoney_clist("m:90+t:3", fid="f3", limit=10)

    assert "&_=" in captured["url"]
    assert captured["headers"].get("Cache-Control") == "no-cache"
    assert captured["headers"].get("Pragma") == "no-cache"
    assert captured["trust_env"] is False


def test_load_fetches_ten_concept_boards():
    """工作台「板块热点 / 东财概念」应拉东财概念 Top10。"""
    service = MarketIntelligenceService()
    service.fetch_jinshi_flash = lambda limit=12: []
    service.fetch_ths_flash = lambda limit=15: []
    service.fetch_sina_flash = lambda limit=12: []
    service.fetch_eastmoney_news = lambda limit=15: []
    service.fetch_tencent_boards = lambda board_type, limit=8: []
    service.fetch_hot_rank = lambda limit=12: []
    service.fetch_eastmoney_changes = lambda limit=15: []
    calls = []

    def fake_clist(fs, fid="f3", limit=10):
        calls.append((fs, fid, limit))
        return []

    service.fetch_eastmoney_clist = fake_clist
    service.load()

    assert ("m:90+t:3", "f3", 10) in calls


def test_market_cloud_prefers_tushare_and_converts_units(monkeypatch):
    """大盘云图优先使用 TuShare，并把 TuShare 的千元/万元字段统一成元口径。"""

    class FakePro:
        def trade_cal(self, **kwargs):  # noqa: D401 - 测试桩
            return pd.DataFrame([
                {"cal_date": "20260617", "is_open": 1},
                {"cal_date": "20260618", "is_open": 1},
            ])

        def stock_basic(self, **kwargs):  # noqa: D401 - 测试桩
            return pd.DataFrame([
                {"ts_code": "600519.SH", "symbol": "600519", "name": "贵州茅台", "industry": "白酒", "market": "主板"},
                {"ts_code": "300750.SZ", "symbol": "300750", "name": "宁德时代", "industry": "电池", "market": "创业板"},
                {"ts_code": "900901.SH", "symbol": "900901", "name": "B股样本", "industry": "其他", "market": "主板"},
            ])

        def daily(self, trade_date=None, fields=""):  # noqa: D401 - 测试桩
            assert trade_date == "20260618"
            return pd.DataFrame([
                {"ts_code": "600519.SH", "trade_date": trade_date, "close": 1688.8, "pct_chg": 2.345, "amount": 211619.6},
                {"ts_code": "300750.SZ", "trade_date": trade_date, "close": 388.1, "pct_chg": -1.2, "amount": 12345.0},
                {"ts_code": "900901.SH", "trade_date": trade_date, "close": 1.2, "pct_chg": 9.9, "amount": 999999.0},
            ])

        def daily_basic(self, trade_date=None, fields=""):  # noqa: D401 - 测试桩
            return pd.DataFrame([
                {"ts_code": "600519.SH", "trade_date": trade_date, "turnover_rate": 0.7, "total_mv": 200000000.0, "circ_mv": 190000000.0},
                {"ts_code": "300750.SZ", "trade_date": trade_date, "turnover_rate": 1.5, "total_mv": 80000000.0, "circ_mv": 75000000.0},
            ])

        def moneyflow_dc(self, trade_date=None):  # noqa: D401 - 测试桩
            return pd.DataFrame([
                {"ts_code": "600519.SH", "net_amount": 21161.96},
                {"ts_code": "300750.SZ", "net_amount": -444.4},
            ])

    fake_pro = FakePro()
    monkeypatch.setenv("TUSHARE_TOKEN", "fake-token")
    monkeypatch.setitem(sys.modules, "tushare", SimpleNamespace(pro_api=lambda token: fake_pro))
    # 桩数据只有 2 行,压低"全市场覆盖"门槛以命中提前返回分支
    monkeypatch.setattr(market_intelligence_module, "MARKET_CLOUD_FULL_COVERAGE", 1)

    service = MarketIntelligenceService()
    service._request_json = lambda *args, **kwargs: pytest.fail("Eastmoney should not be called when TuShare has rows")
    service.fetch_sina_market_cloud_stocks = lambda *args, **kwargs: pytest.fail("Sina fallback should not be called")
    service._fetch_tencent_cloud_quotes = lambda codes, **kwargs: {}  # 单位换算测试不联网,报价叠加空转

    rows = service.fetch_market_cloud_stocks(limit=100, force_refresh=False)

    assert [row["code"] for row in rows] == ["600519", "300750"]
    first = rows[0]
    assert first["source"] == "tushare_market_cloud"
    assert first["industry"] == "白酒"
    assert first["trade_date"] == "20260618"
    assert first["amount"] == pytest.approx(211619600.0)          # 千元 → 元
    assert first["market_cap"] == pytest.approx(2000000000000.0)  # 万元 → 元
    assert first["main_net_inflow"] == pytest.approx(211619600.0) # 万元 → 元
    assert first["main_net_inflow_text"] == "2.12亿"


def test_market_cloud_force_refresh_prefers_eastmoney_realtime(monkeypatch):
    """手动刷新大盘云图时优先使用东财实时行情，避免继续展示 TuShare 日线收盘数据。"""

    # 东财桩只有 1 行,压低覆盖门槛使其视为"全市场达标"
    monkeypatch.setattr(market_intelligence_module, "MARKET_CLOUD_FULL_COVERAGE", 1)
    service = MarketIntelligenceService()
    service.fetch_tushare_market_cloud_stocks = lambda *args, **kwargs: pytest.fail(
        "manual refresh should prefer realtime Eastmoney rows"
    )
    service.fetch_sina_market_cloud_stocks = lambda *args, **kwargs: pytest.fail(
        "Sina fallback should not be called when Eastmoney has rows"
    )

    def fake_request_json(url, headers=None, timeout=5, trust_env=True):
        assert "push2.eastmoney.com/api/qt/clist/get" in url
        assert trust_env is False
        return {
            "data": {
                "diff": [
                    {
                        "f12": "600519",
                        "f14": "贵州茅台",
                        "f2": 1689.01,
                        "f3": 1.23,
                        "f6": 211619600.0,
                        "f8": 0.72,
                        "f20": 2000000000000.0,
                        "f62": 123456789.0,
                        "f100": "白酒",
                    }
                ]
            }
        }

    service._request_json = fake_request_json

    rows = service.fetch_market_cloud_stocks(limit=100, force_refresh=True)

    assert len(rows) == 1
    assert rows[0]["source"] == "eastmoney_market_cloud"
    assert rows[0]["code"] == "600519"
    assert rows[0]["price"] == 1689.01
    assert rows[0]["change_pct"] == 1.23
    assert rows[0]["industry"] == "白酒"


def test_market_cloud_refresh_falls_through_when_realtime_partial(monkeypatch):
    """东财不可达/部分覆盖时(如仅新浪 ~2400 只老股),继续尝试 TuShare 全市场并采用更全的结果。"""

    monkeypatch.setattr(market_intelligence_module, "MARKET_CLOUD_FULL_COVERAGE", 3)
    service = MarketIntelligenceService()
    partial = [{"code": "600519", "name": "贵州茅台", "source": "eastmoney_market_cloud"}]
    full = [{"code": f"60000{i}", "name": f"股{i}", "source": "tushare_market_cloud"}
            for i in range(3)]
    service.fetch_eastmoney_market_cloud_stocks = lambda *args, **kwargs: list(partial)
    service.fetch_tushare_market_cloud_stocks = lambda *args, **kwargs: list(full)
    service.fetch_sina_market_cloud_stocks = lambda *args, **kwargs: pytest.fail(
        "TuShare 覆盖已达标,不应再落到新浪部分覆盖")

    rows = service.fetch_market_cloud_stocks(limit=100, force_refresh=True)

    assert len(rows) == 3
    assert rows[0]["source"] == "tushare_market_cloud"


def test_market_cloud_keeps_largest_result_when_all_sources_partial(monkeypatch):
    """所有源都低于覆盖门槛时,返回行数最多的一组而非首个非空结果。"""

    monkeypatch.setattr(market_intelligence_module, "MARKET_CLOUD_FULL_COVERAGE", 100)
    service = MarketIntelligenceService()
    service.fetch_eastmoney_market_cloud_stocks = lambda *args, **kwargs: [
        {"code": "600519", "source": "eastmoney_market_cloud"}]
    service.fetch_tushare_market_cloud_stocks = lambda *args, **kwargs: []
    service.fetch_sina_market_cloud_stocks = lambda *args, **kwargs: [
        {"code": "600519", "source": "sina_market_cloud"},
        {"code": "000001", "source": "sina_market_cloud"}]

    rows = service.fetch_market_cloud_stocks(limit=100, force_refresh=True)

    assert len(rows) == 2
    assert rows[0]["source"] == "sina_market_cloud"


def test_market_cloud_cache_is_shared_across_limits(monkeypatch):
    """云图页(市值前 5000)与条件选股(全市场)共用一份缓存:窄请求取宽缓存的前缀,取数按请求过的最宽 limit。"""

    monkeypatch.setattr(market_intelligence_module, "MARKET_CLOUD_FULL_COVERAGE", 1)
    service = MarketIntelligenceService()
    universe = [{"code": f"{600000 + i}", "source": "tushare_market_cloud", "trade_date": "20260618"}
                for i in range(5600)]
    fetched = []

    def fake_tushare(limit=5000, trade_date=None):
        fetched.append(limit)
        return universe[:limit]

    service.fetch_tushare_market_cloud_stocks = fake_tushare
    service.fetch_eastmoney_market_cloud_stocks = lambda *args, **kwargs: []
    service.fetch_sina_market_cloud_stocks = lambda *args, **kwargs: []
    service._fetch_tencent_cloud_quotes = lambda codes, **kwargs: {}

    assert len(service.fetch_market_cloud_stocks(limit=5000)) == 5000     # 云图页先到:按 5000 取
    assert len(service.fetch_market_cloud_stocks(limit=6000)) == 5600     # 选股要全市场:缓存不够宽,重取一次
    assert fetched == [5000, 6000]
    narrow = service.fetch_market_cloud_stocks(limit=5000)                # 云图页再来:直接取前缀,不重取
    assert [row["code"] for row in narrow] == [row["code"] for row in universe[:5000]]
    assert fetched == [5000, 6000]
    assert len(service.fetch_market_cloud_stocks(limit=5000, force_refresh=True)) == 5000
    assert fetched == [5000, 6000, 6000]                                  # 此后刷新始终按最宽取,两页不再互相挤掉
    assert len(service.fetch_market_cloud_stocks(limit=6000)) == 5600
    assert fetched == [5000, 6000, 6000]


def test_market_cloud_trade_date_uses_tushare_history_only(monkeypatch):
    """指定交易日查询应精确读取该日 TuShare 日线，且不混入实时行情回退。"""

    calls = {"daily": [], "trade_cal": 0}

    class FakePro:
        def trade_cal(self, **kwargs):  # noqa: D401 - 测试桩
            calls["trade_cal"] += 1
            pytest.fail("explicit trade_date should not scan recent trade calendar")

        def stock_basic(self, **kwargs):  # noqa: D401 - 测试桩
            return pd.DataFrame([
                {"ts_code": "600519.SH", "symbol": "600519", "name": "贵州茅台", "industry": "白酒", "market": "主板"},
                {"ts_code": "300750.SZ", "symbol": "300750", "name": "宁德时代", "industry": "电池", "market": "创业板"},
            ])

        def daily(self, trade_date=None, fields=""):  # noqa: D401 - 测试桩
            calls["daily"].append(trade_date)
            assert trade_date == "20260617"
            return pd.DataFrame([
                {"ts_code": "600519.SH", "trade_date": trade_date, "close": 1680.0, "pct_chg": -0.56, "amount": 1000.0},
                {"ts_code": "300750.SZ", "trade_date": trade_date, "close": 390.0, "pct_chg": 2.15, "amount": 800.0},
            ])

        def daily_basic(self, trade_date=None, fields=""):  # noqa: D401 - 测试桩
            assert trade_date == "20260617"
            return pd.DataFrame([
                {"ts_code": "600519.SH", "trade_date": trade_date, "turnover_rate": 0.6, "total_mv": 200000000.0},
                {"ts_code": "300750.SZ", "trade_date": trade_date, "turnover_rate": 1.3, "total_mv": 80000000.0},
            ])

        def moneyflow_dc(self, trade_date=None):  # noqa: D401 - 测试桩
            assert trade_date == "20260617"
            return pd.DataFrame([
                {"ts_code": "600519.SH", "net_amount": -1200.0},
                {"ts_code": "300750.SZ", "net_amount": 900.0},
            ])

    fake_pro = FakePro()
    monkeypatch.setenv("TUSHARE_TOKEN", "fake-token")
    monkeypatch.setitem(sys.modules, "tushare", SimpleNamespace(pro_api=lambda token: fake_pro))

    service = MarketIntelligenceService()
    service.fetch_eastmoney_market_cloud_stocks = lambda *args, **kwargs: pytest.fail(
        "date query should not call realtime Eastmoney rows"
    )
    service.fetch_sina_market_cloud_stocks = lambda *args, **kwargs: pytest.fail(
        "date query should not call realtime Sina fallback"
    )

    rows = service.fetch_market_cloud_stocks(limit=100, force_refresh=True, trade_date="2026-06-17")

    assert calls["daily"] == ["20260617"]
    assert calls["trade_cal"] == 0
    assert [row["trade_date"] for row in rows] == ["20260617", "20260617"]
    assert {row["source"] for row in rows} == {"tushare_market_cloud"}
    assert {row["code"] for row in rows} == {"600519", "300750"}


def _stale_tushare_cloud_rows():
    return [
        {
            "code": "600519", "name": "贵州茅台", "price": 1680.0, "change_pct": -0.56,
            "amount": 211619600.0, "turnover_rate": 0.6, "market_cap": 2000000000000.0,
            "main_net_inflow": 21161960.0, "main_net_inflow_text": "2116.20万",
            "industry": "白酒", "trade_date": "20260727", "source": "tushare_market_cloud",
        },
        {
            "code": "300750", "name": "宁德时代", "price": 388.1, "change_pct": -1.2,
            "amount": 123450000.0, "turnover_rate": 1.5, "market_cap": 800000000000.0,
            "main_net_inflow": -4444000.0, "main_net_inflow_text": "-444.40万",
            "industry": "电池", "trade_date": "20260727", "source": "tushare_market_cloud",
        },
    ]


def test_market_cloud_overlays_realtime_quotes_on_stale_tushare(monkeypatch):
    """交易日盘中 TuShare 仅有上一交易日日线时,应叠加腾讯实时报价,而不是整天停在昨天。"""

    monkeypatch.setattr(market_intelligence_module, "MARKET_CLOUD_FULL_COVERAGE", 1)
    service = MarketIntelligenceService()
    service.fetch_eastmoney_market_cloud_stocks = lambda *args, **kwargs: []
    service.fetch_tushare_market_cloud_stocks = lambda *args, **kwargs: _stale_tushare_cloud_rows()
    service.fetch_sina_market_cloud_stocks = lambda *args, **kwargs: []
    service._cloud_quotes_overlay_due = lambda rows, today=None: bool(rows)  # 测试不依赖真实日期
    requested = {}

    def fake_quotes(codes, **kwargs):
        requested["codes"] = list(codes)
        return {
            "600519": {
                "price": 1701.5, "change_pct": 3.21, "amount": 1076340000.0,
                "turnover_rate": 0.9, "quote_date": "20260728",
            }
        }

    service._fetch_tencent_cloud_quotes = fake_quotes

    rows = service.fetch_market_cloud_stocks(limit=100, force_refresh=True)

    assert requested["codes"] == ["600519", "300750"]
    quoted = rows[0]
    assert quoted["price"] == 1701.5
    assert quoted["change_pct"] == 3.21
    assert quoted["amount"] == 1076340000.0
    assert quoted["turnover_rate"] == 0.9
    assert quoted["trade_date"] == "20260728"  # 数据日推进到报价日
    assert quoted["quoted"] is True
    assert "quote_date" not in quoted
    assert quoted["market_cap"] == 2000000000000.0  # 市值/主力净流入保持日线口径
    assert quoted["main_net_inflow"] == 21161960.0
    untouched = rows[1]
    assert untouched["price"] == 388.1
    assert untouched["change_pct"] == -1.2
    assert untouched["trade_date"] == "20260727"
    assert "quoted" not in untouched


def test_market_cloud_overlay_keeps_stale_rows_when_quotes_unavailable(monkeypatch):
    """腾讯报价拉取失败时原样返回上一交易日日线(兜底不能整块变空)。"""

    monkeypatch.setattr(market_intelligence_module, "MARKET_CLOUD_FULL_COVERAGE", 1)
    service = MarketIntelligenceService()
    service.fetch_eastmoney_market_cloud_stocks = lambda *args, **kwargs: []
    service.fetch_tushare_market_cloud_stocks = lambda *args, **kwargs: _stale_tushare_cloud_rows()
    service.fetch_sina_market_cloud_stocks = lambda *args, **kwargs: []
    service._cloud_quotes_overlay_due = lambda rows, today=None: bool(rows)
    service._fetch_tencent_cloud_quotes = lambda codes, **kwargs: {}

    rows = service.fetch_market_cloud_stocks(limit=100, force_refresh=True)

    assert len(rows) == 2
    assert [row["trade_date"] for row in rows] == ["20260727", "20260727"]
    assert all("quoted" not in row for row in rows)


def test_cloud_quotes_overlay_due_rules():
    """仅在「Tushare 日线源 + 数据日≠今日 + 今日为工作日」时才叠加实时报价。"""
    import datetime as _dt

    due = MarketIntelligenceService._cloud_quotes_overlay_due
    stale = _stale_tushare_cloud_rows()
    tuesday = _dt.date(2026, 7, 28)
    saturday = _dt.date(2026, 7, 25)

    assert due(stale, today=tuesday) is True
    assert due([], today=tuesday) is False
    assert due(stale, today=saturday) is False  # 周末休市:昨日=最新完整交易日,无需覆盖
    assert due([dict(stale[0], trade_date="20260728")], today=tuesday) is False  # 当日日线已发布
    assert due([dict(stale[0], source="sina_market_cloud")], today=tuesday) is False  # 实时源本身即当日
    assert due([dict(stale[0], trade_date="")], today=tuesday) is False


def test_fetch_tencent_cloud_quotes_parses_batches(monkeypatch):
    """腾讯批量报价解析:sh/sz/bj(含920)前缀分流、字段位 3价/32涨跌/37成交额(万元→元)/38换手/30报价时间。"""

    urls = []

    def fake_request_text(url, headers=None, timeout=5, encoding="gbk", errors="ignore", retries=1):
        urls.append(url)

        def line(prefix, code, price, pct, amount_wan, turnover, quote_time):
            fields = [""] * 50
            fields[0] = "1"
            fields[1] = "样本"
            fields[2] = code
            fields[3] = str(price)
            fields[30] = quote_time
            fields[32] = str(pct)
            fields[37] = str(amount_wan)
            fields[38] = str(turnover)
            return f'v_{prefix}{code}="' + "~".join(fields) + '";'

        return "\n".join([
            line("sh", "600519", 1701.5, 3.21, 107634, 0.9, "20260728143000"),
            line("sz", "000001", 11.2, 0.81, 107634, 0.5, "20260728143000"),
            line("bj", "920002", 50.37, 4.42, 123, 1.1, "20260728143000"),
        ])

    monkeypatch.setattr(market_intelligence_module, "request_text", fake_request_text)

    service = MarketIntelligenceService()
    quotes = service._fetch_tencent_cloud_quotes(["600519", "000001", "920002", "430047", "bad"], batch=3)

    # 其他测试遗留的行业映射守护线程也走 request_text,只统计腾讯报价请求
    gtimg_urls = [url for url in urls if "qt.gtimg.cn" in url]
    assert len(gtimg_urls) == 2  # 4 个有效代码按 batch=3 分两批
    assert any("sh600519,sz000001,bj920002" in url for url in gtimg_urls)
    assert any("bj430047" in url for url in gtimg_urls)
    quote = quotes["600519"]
    assert quote["price"] == 1701.5
    assert quote["change_pct"] == 3.21
    assert quote["amount"] == pytest.approx(1076340000.0)  # 万元 → 元
    assert quote["turnover_rate"] == 0.9
    assert quote["quote_date"] == "20260728"
    assert "bad" not in quotes
