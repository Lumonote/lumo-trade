"""宏观研究的单位、对齐、事件窗口和数据缺口边界。"""
import datetime as dt
import json
import math

import pytest

from analysis.market_macro import (SPECS, build_report, clean_series, event_study, pairs_for,
                                   pearson, relationship, scenario, valuation_decomposition)
from webui.services import market_macro_service as service


def series(count=320, start="2024-01-01", value=lambda i: 100 + i):
    start = dt.date.fromisoformat(start)
    return [{"date": (start + dt.timedelta(days=i)).isoformat(), "value": value(i)} for i in range(count)]


def test_clean_values_and_future_dates():
    rows = [{"date": "20261001", "value": "1.2"}, {"date": "2026-10-02", "value": float("nan")},
            {"date": "2026-10-03", "value": float("inf")}, {"date": "2026-10-04", "value": "--"},
            {"date": "2026-10-01", "value": 2}, {"date": "2026-11-01", "value": 9}]
    assert clean_series(rows, "2026-10-31") == [{"date": "2026-10-01", "value": 2}]


def test_foreign_data_uses_strictly_prior_observation_and_rate_bp():
    prices = series(8)
    factors = series(8, value=lambda i: 2 + i / 100)
    rows = pairs_for(prices, factors, SPECS["real10y"])
    assert len(rows) == 6
    assert rows[0]["date"] == "2024-01-03" and rows[0]["factor_date"] == "2024-01-02"
    assert rows[0]["x"] == pytest.approx(1)
    assert rows[0]["y"] == pytest.approx(math.log(102 / 101) * 100)
    assert all(r["factor_date"] < r["date"] for r in rows)


def test_same_observation_not_duplicated_and_stale_not_extended():
    rows = pairs_for(series(50), [{"date": "2024-01-01", "value": 2}, {"date": "2024-01-03", "value": 3}], SPECS["real10y"])
    assert len(rows) == 1
    assert rows[0]["date"] == "2024-01-04"
    # 不能为凑满N个样本，把很早的利率报价拉入最近N个交易日窗口。
    assert not pairs_for(series(500), series(8, value=lambda i: i / 100 + 1), SPECS["lpr1y"], window=60)


def test_leading_factor_is_shifted_in_asset_sessions():
    prices, factors = series(20), series(20, value=lambda i: i * i + 1)
    current = pairs_for(prices, factors, SPECS["real10y"])
    lagged = pairs_for(prices, factors, SPECS["real10y"], lag=5)
    assert lagged[0]["date"] == current[5]["date"]
    assert lagged[0]["factor_date"] == current[0]["factor_date"]
    assert lagged[0]["x"] == current[0]["x"]


def test_monthly_no_missing_month_bridge_and_units():
    prices = [{"date": f"2024-{m:02}-28", "value": 100 + m} for m in (1, 2, 4, 5)]
    factors = [{"date": f"2024-{m:02}-28", "value": 5 + m / 10} for m in (1, 2, 4, 5)]
    rows = pairs_for(prices, factors, SPECS["m2"], frequency="monthly")
    assert len(rows) == 2
    assert all(r["x"] == pytest.approx(.1) for r in rows)


def test_correlations_require_samples_and_actual_variation():
    prices = series(100, value=lambda i: 100 * math.exp(i * i / 10000))
    factors = series(100, value=lambda i: i * i / 100)
    result = relationship(prices, factors, SPECS["shibor3m"])
    assert result["n"] == 99 and result["rho"] == pytest.approx(1)
    assert result["spearman"] == pytest.approx(1)
    assert relationship(prices[:30], factors[:30], SPECS["shibor3m"])["rho"] is None
    assert pearson([1, 1, 1], [1, 2, 3]) is None


def test_event_anchor_after_announcement_incomplete_and_overlap():
    prices = series(30)
    events = [{"date": "2024-01-10", "title": "降息", "category": "fed"}, {"date": "2024-01-10", "title": "同日另一政策", "category": "fiscal"}, {"date": "2024-02-01", "title": "未开始", "category": "fed"}]
    report = event_study(events, prices)
    assert report["rows"][0]["returns"]["1"] is None
    row = report["rows"][1]
    assert row["anchor"] == "2024-01-11"
    assert row["returns"]["1"] == pytest.approx((110 / 109 - 1) * 100)
    assert row["returns"]["60"] is None and row["overlap"]
    filtered = event_study(events, prices, category="fed", keyword="降息")
    assert filtered["total"] == 1
    assert filtered["summary"][0]["n"] == 1


def test_valuation_identity_and_no_bridge_across_missing_pe():
    prices = series(300, value=lambda i: 100 * math.exp(i / 1000))
    valuations = [{"date": r["date"], "pe_ttm": 10 * math.exp(i / 2000)} for i, r in enumerate(prices)]
    report = valuation_decomposition(prices, valuations)
    assert report["available"] and len(report["rows"]) == 251
    assert report["price_log"] == pytest.approx(25)
    assert report["earnings_log"] == pytest.approx(12.5)
    assert report["valuation_log"] == pytest.approx(12.5)
    assert report["residual"] == pytest.approx(0, abs=1e-10)
    valuations.pop(-20)
    assert not valuation_decomposition(prices, valuations)["available"]


def test_scenario_distinguishes_identity_from_discount_assumption():
    report = scenario(10, -10, -100, 10)
    assert report["identity_pct"] == pytest.approx(-1)
    assert report["duration_pct"] == pytest.approx((1.1 * math.exp(.1) - 1) * 100)
    with pytest.raises(ValueError): scenario(float("nan"))
    with pytest.raises(ValueError): scenario(discount_bp=301)


@pytest.mark.parametrize("params", [{"equity": "bogus"}, {"frequency": "daily", "factor": "m2"}, {"frequency": "monthly", "lag": 20}, {"window": 3}, {"factor": "gdp"}])
def test_invalid_research_combinations(params):
    with pytest.raises(ValueError): build_report({}, **params)


def test_empty_report_is_json_serializable_no_fabricated_zero():
    report = build_report({"as_of": "2026-10-31"})
    assert report["relationship"]["rho"] is None
    assert all(r["value"] is None for r in report["metrics"])
    json.dumps(report, allow_nan=False)


def test_fiscal_parser_keeps_period_publication_and_cumulative_units():
    html = '<meta name="PubDate" content="2026-10-23"><p>全国一般公共预算收入172292亿元，同比增长5.1%。全国一般公共预算支出210199亿元，同比增长1%。全国政府性基金预算支出62754亿元，同比下降16.2%。国有土地使用权出让收入16087亿元，同比下降27.9%。</p>'
    row = service.parse_fiscal(html, {"title": "2026年前三季度财政收支情况", "url": "https://www.mof.gov.cn/report", "published_on": "2026-10-24"})
    assert row["period"] == "2026-09" and row["date"] == "2026-09-30"
    assert row["published_on"] == "2026-10-24"  # conflicting timestamps use conservative later date
    assert row["spending"] == 210199 and row["fund_spending_yoy"] == -16.2
    assert row["land_income_yoy"] == -27.9


@pytest.mark.parametrize("title,period", [("2024年10月财政收支情况", "2024-10"), ("2024年1—10月财政收支情况", "2024-10"), ("2024年财政收支情况", "2024-12"), ("2024年上半年财政收支情况", "2024-06")])
def test_fiscal_month_title_can_describe_cumulative_data(title, period):
    row = service.parse_fiscal("全国一般公共预算支出10000亿元，同比增长1%。", {"title": title, "url": "https://www.mof.gov.cn/report", "published_on": "2025-01-01"})
    assert row["period"] == period and row["spending"] == 10000


def test_debt_parser_never_confuses_monthly_and_cumulative():
    html = '<meta name="PubDate" content="2026-10-23"><p>（一）当月发行情况。2026年9月，全国发行新增债券7335亿元。全国发行再融资债券2713亿元。（二）1-9月发行情况。全国发行新增债券42293亿元。全国发行再融资债券45789亿元。</p>'
    row = service.parse_debt(html, {"title": "2026年9月地方政府债券发行和债务余额情况", "url": "https://zwgls.mof.gov.cn/report", "published_on": "2026-10-28"})
    assert row["new"] == 7335 and row["refinance"] == 2713 and row["total"] == 10048
    assert row["published_on"] == "2026-10-28" and row["period"] == "2026-09"


def test_fomc_calendar_schedule_has_no_implied_rate():
    html = '<div class="panel"><h4 class="panel-heading">2026 FOMC Meetings</h4><div class="fomc-meeting"><div class="fomc-meeting__month">December</div><div class="fomc-meeting__date">8-9*</div></div></div>'
    rows = service.fomc_calendar(html)
    assert rows[0]["date"] == "2026-12-09" and rows[0]["sep"]
    assert "rate" not in rows[0] and "probability" not in rows[0]


def test_distributable_baseline_data_coverage_and_complete_report():
    data = service.baseline()
    assert len(data["equities"]["csi300"]) > 1000
    assert len(data["sectors"]) == 31
    report = build_report(data, factor="gold")
    assert report["relationship"]["n"] == 252
    assert report["valuation"]["available"]
    assert len(report["sector_sensitivity"]) == 31
    assert all(r["name"] for r in report["sector_sensitivity"])
    assert report["metrics"][6]["date"] <= data["as_of"]
    json.dumps(report, allow_nan=False)


def test_refresh_reuses_active_job_and_failure_keeps_previous_bundle(monkeypatch):
    saved = {"bundle": {"as_of": "2026-10-20", "series": {"gold": [{"date": "2026-10-19", "value": 900}]}}}
    monkeypatch.setattr(service, "_read", lambda key: saved.get(key))
    monkeypatch.setattr(service.kv_repo, "set_", lambda namespace, key, value: saved.update({key: value}))
    monkeypatch.setattr(service, "collect", lambda progress: (_ for _ in ()).throw(RuntimeError("SECRET request body")))
    class Jobs:
        def __init__(self): self.jobs = {}; self.starts = 0
        def get(self, key): return self.jobs.get(key)
        def start(self, kind, params, worker):
            self.starts += 1
            self.worker = worker
            self.jobs["one"] = {"id": "one", "status": "queued"}
            return self.jobs["one"]
        def update(self, key, **changes): self.jobs[key].update(changes)
        def append_log(self, key, text): pass
    jobs = Jobs()
    first = service.start_refresh(jobs)
    assert service.start_refresh(jobs) == first and jobs.starts == 1
    jobs.worker("one", {})
    assert jobs.get("one")["status"] == "failed"
    assert "SECRET" not in jobs.get("one")["error"]
    assert saved["bundle"]["as_of"] == "2026-10-20"
    assert saved["bundle"]["series"]["gold"][0]["date"] == "2026-10-19"


def test_midmonth_excludes_unfinished_month_from_relationship_only():
    rows = series(700, "2023-01-01", lambda i: 100 + i)
    bundle = {"as_of": "2024-10-15", "equities": {"csi300": rows}, "series": {"m2": rows}, "events": []}
    report = build_report(bundle, factor="m2", frequency="monthly", window=24)
    assert report["chart"]["asset"][-1]["date"] == "2024-09-30"
    assert all(r["date"] < "2024-10-01" for r in report["relationship"]["pairs"])


def test_partial_refresh_does_not_replace_newer_domestic_series(monkeypatch):
    old = {"series": {k: [{"date": "2026-09-30", "value": 8}] for k in ("m1", "m2", "lpr1y", "lpr5y", "social", "social_stock")}}
    monkeypatch.setattr(service, "fred_series", lambda code, end: [{"date": "2026-10-29", "value": 4}])
    monkeypatch.setattr(service, "listing", lambda *args, **kwargs: [])
    monkeypatch.setattr(service, "_text", lambda url: "<html></html>")
    def records(method, **params):
        return {"cn_m": [{"month": "202001", "m1_yoy": 2, "m2_yoy": 3}],
                "shibor_lpr": [{"date": "20200120", "1y": 4, "5y": 5}],
                "sf_month": [{"month": "201901", "inc_month": 10, "stk_endval": 100}, {"month": "202001", "inc_month": 20, "stk_endval": 110}]}.get(method, [])
    monkeypatch.setattr(service, "_records", records)
    result = service.collect(old=old)
    for key in old["series"]:
        assert result["series"][key] == old["series"][key]
        source = next(r for r in result["sources"] if r["name"] == SPECS[key]["name"])
        assert source["status"] == "保留原快照"
    assert any("日期倒退" in e for e in result["errors"])
