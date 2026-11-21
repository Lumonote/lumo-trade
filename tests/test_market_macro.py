"""宏观研究的单位、对齐、事件窗口和数据缺口边界。"""
import datetime as dt
import json
import math

import pytest

from analysis.market_macro import (SPECS, SPREAD, build_report, clean_series, event_study, pairs_for,
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


def test_fred_history_backfills_old_range_to_sqlite_and_reuses_overlap(isolated_price_db, monkeypatch):
    calls = []
    monkeypatch.setattr(service, 'baseline', lambda: {'as_of': '2026-10-31', 'series': {
        'us10y': [{'date': '2014-01-02', 'value': 3}]}})

    def fetch(url, **kwargs):
        calls.append(url)
        return 'observation_date,DGS10\n1998-09-22,4.73\n1998-09-23,.\n1998-09-24,4.69\n1999-05-01,99\n'

    monkeypatch.setattr(service, 'request_text', fetch)
    params = {'factor': 'us10y', 'equity': 'shcomp', 'start': '1998-09-21', 'end': '1998-09-25'}
    first = service.history(**params)
    assert first['rows'] == [{'date': '1998-09-22', 'value': 4.73}, {'date': '1998-09-24', 'value': 4.69}]
    assert 'cosd=1998-09-21&coed=1998-09-25' in calls[0]
    assert service.history(**{**params, 'start': '1998-09-22', 'end': '1998-09-24'})['rows'] == first['rows']
    assert len(calls) == 1
    assert service._history_cache('us10y')[0] == first['rows']


def test_invalid_fred_response_is_retryable_and_displays_only_real_local_data(isolated_price_db, monkeypatch):
    calls = []
    monkeypatch.setattr(service, 'baseline', lambda: {'as_of': '2026-10-31', 'series': {
        'us10y': [{'date': '2014-01-02', 'value': 3}]}})
    monkeypatch.setattr(service, 'request_text', lambda url, **kw: calls.append(url) or '<html>failed</html>')
    params = {'factor': 'us10y', 'equity': 'shcomp', 'start': '2008-01-02', 'end': '2014-01-02'}
    result = service.history(**params)
    assert result['rows'] == [{'date': '2014-01-02', 'value': 3}]
    assert '暂不可用' in result['message']
    service.history(**params)
    assert len(calls) == 2


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


def test_events_search_full_history_and_summarize_before_paging():
    prices = series(1200, start="2021-01-01")
    events = [{"id": str(i), "date": row["date"], "title": f"event {i}", "category": "fed"}
              for i, row in enumerate(prices[:1102])]
    # The earlier version of this event matched; the correction does not.
    events.append({**events[0], "title": "corrected", "category": "fiscal"})
    first = event_study(events, prices, limit=1000)
    rest = event_study(events, prices, offset=1000)
    assert first["total"] == rest["total"] == 1102
    assert len(first["rows"]) == 1000 and len(rest["rows"]) == 102
    assert rest["rows"][-1]["title"] == "corrected"
    assert first["summary"] == rest["summary"] and all(r["n"] == 1102 for r in rest["summary"])
    assert not event_study(events, prices, category="fed", keyword="event 0")["total"]
    late_match = event_study(events, prices, keyword="corrected")
    assert late_match["total"] == 1 and not late_match["rows"][0]["overlap"]
    # Neither of these two windows is visible on the other's page.
    for offset in (0, 1):
        page = event_study(events[1:3], prices, offset=offset, limit=1)
        assert page["total"] == 2 and page["rows"][0]["overlap"]


def test_research_cache_uses_snapshot_bytes_and_only_research_parameters(tmp_path, monkeypatch):
    from data_store.connection import get_conn, reset_for_testing
    monkeypatch.setenv("KRONOS_USER_DIR", str(tmp_path))
    monkeypatch.setenv("KRONOS_SQLITE_PATH", str(tmp_path / "macro.sqlite"))
    reset_for_testing()
    original, calls = service.build_research, []

    def counted(bundle, **params):
        calls.append(params)
        return original(bundle, **params)

    monkeypatch.setattr(service, "build_research", counted)
    prices = series(30)
    data = {"as_of": prices[-1]["date"], "series": {"gold": series(2), "cn10y": series(2)},
            "equities": {"csi300": prices}, "events": [{"date": prices[0]["date"], "title": "policy A", "category": "fed"}]}

    def save():
        service.kv_repo.set_(service.NAMESPACE, "bundle", data)
        get_conn().execute("UPDATE kv_cache SET updated_at=? WHERE namespace=? AND key='bundle'",
                           ("2024-01-30T00:00:00", service.NAMESPACE))
        return service.kv_repo.get_raw(service.NAMESPACE, "bundle")

    try:
        raw = save()
        query = lambda **kw: service.query(factor="gold", **kw)
        first = query()
        first["chart"]["factor_history"].clear()
        second = query(keyword="absent", pe=1)
        assert second["chart"]["factor_history"][-1]["value"] == 101
        assert second["events"]["total"] == 0 and second["scenario"]["pe"] == 1
        assert len(calls) == 1 and service.events(keyword="policy A")["events"]["total"] == 1
        with monkeypatch.context() as m:
            m.setattr(service, "_snapshot", lambda: pytest.fail("status must not load the research snapshot"))
            assert service.status() == {"job": None}
        query(lag=1)
        query(lag=1)
        assert len(calls) == 2
        for window in (60, 120, 252):
            for lag in (0, 1, 5, 20):
                query(window=window, lag=lag)
        assert service._snapshot().research.cache_info().currsize == 8
        # Same timestamp, length, and as_of, different values and event title.
        data["series"]["gold"][-1]["value"] = 109
        data["events"][0]["title"] = "policy B"
        assert len(save()) == len(raw)
        revised = query()
        assert revised["snapshot_version"] != first["snapshot_version"]
        assert revised["chart"]["factor_history"][-1]["value"] == 109
        events = service.events(keyword="policy B")
        assert events["snapshot_version"] == revised["snapshot_version"] and events["events"]["total"] == 1
        assert not service.events(keyword="policy A")["events"]["total"]
    finally:
        service._snapshot_for.cache_clear()
        reset_for_testing()


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


def test_spread_uses_index_specific_same_day_positive_pe_and_preserves_history():
    data = service.baseline()
    report = build_report(data, factor=SPREAD, window=60)
    recent = report["chart"]["factor_history"][-1]
    assert recent["date"] == "2026-10-30"
    assert recent["value"] == pytest.approx(100 / 12.9122 - 1.65)
    assert len(report["chart"]["factor_history"]) > len(report["chart"]["factor"]) > 60
    report500 = build_report(data, factor=SPREAD, equity="csi500", frequency="monthly", window=24)
    assert report500["chart"]["factor_history"][-1]["value"] == pytest.approx(100 / 26.7601 - 1.65)
    assert report500["valuation_comparison"] == report["valuation_comparison"]
    assert build_report(data, factor="lpr5y", window=60)["chart"]["factor_history"][0]["date"] < "2020-01-01"

    report = build_report({"as_of": "2024-01-03", "series": {"cn10y": series(2, value=lambda i: 2)},
                           "valuations": {"csi300": [{"date": "2024-01-01", "pe_ttm": 10},
                                                      {"date": "2024-01-02", "pe_ttm": 0},
                                                      {"date": "2024-01-03", "pe_ttm": 5}]}}, factor=SPREAD)
    assert report["chart"]["factor_history"] == [{"date": "2024-01-01", "value": 8}]
    comparison = report["valuation_comparison"]
    assert comparison["comparison_date"] is None and not comparison["current_low"]
    assert all(r["low"] is None and not r["comparable"] for r in comparison["rows"])
    assert next(r for r in comparison["rows"] if r["key"] == "csi300")["date"] == "2024-01-01"


def test_valuation_comparison_requires_both_percentiles_and_excludes_current_day():
    bonds = series(253, value=lambda i: 1 if i == 252 else 2)
    data = {"as_of": bonds[-1]["date"], "equities": {"csi300": bonds}, "series": {"cn10y": bonds}, "valuations": {
        "csi300": [{"date": r["date"], "pe_ttm": 10 if i == 252 else 20} for i, r in enumerate(bonds)],
        "csi500": [{"date": r["date"], "pe_ttm": 20} for r in bonds],
    }}
    rows = {r["key"]: r for r in build_report(data)["valuation_comparison"]["rows"]}
    assert rows["csi300"]["low"] is True and rows["csi300"]["samples"] == 252
    assert rows["csi300"]["pe_percentile"] == 0 and rows["csi300"]["spread_percentile"] == 100
    assert rows["csi500"]["low"] is False
    assert rows["csi500"]["pe_percentile"] == 50 and rows["csi500"]["spread_percentile"] == 100


def test_stale_low_is_historical_only_even_when_report_day_is_closed():
    older, recent, future = "2025-12-22", "2026-08-24", "2026-09-01"
    past = series(252, "2025-01-01")
    dates = ["2019-01-04", *(r['date'] for r in past), older, recent, future]
    data = {"as_of": "2026-08-30", "calendar": [{"date": "2026-08-28"}],
            "equities": {"csi300": [{"date": recent, "value": 4000}, {"date": future, "value": 4500}],
                         "chinext": [{"date": older, "value": 2000}]},
            "series": {"cn10y": [{"date": d, "value": 2} for d in dates]},
            "valuations": {k: [{"date": d, "pe_ttm": 10 if d == last else 20} for d in [*dates[:-3], last, future]]
                           for k, last in [("chinext", older), ("csi300", recent)]}}
    board = build_report(data)["valuation_comparison"]
    rows = {r['key']: r for r in board['rows']}
    assert board['comparison_date'] == recent and board['current_low'] == ['csi300']
    assert rows['chinext']['historical_low'] and rows['chinext']['low'] is None
    assert rows['chinext']['date'] == older and rows['chinext']['lag_sessions'] == 1
    assert rows['csi300']['samples'] == rows['chinext']['samples'] == 252  # 不含五年前那条
    data['valuations']['csi300'].pop(1)
    short = next(r for r in build_report(data)['valuation_comparison']['rows'] if r['key'] == 'csi300')
    assert short['comparable'] and short['samples'] == 251 and short['low'] is None


def test_older_user_snapshot_only_backfills_bonds_through_its_own_cutoff(tmp_path, monkeypatch):
    from data_store.connection import reset_for_testing

    saved = {"as_of": "2024-01-02", "updated_at": "original", "series": {"gold": series(1)}}
    monkeypatch.setenv('KRONOS_SQLITE_PATH', str(tmp_path / 'macro.sqlite'))
    monkeypatch.setattr(service, "baseline", lambda: {"series": {"cn10y": series(3)}})
    reset_for_testing()
    try:
        service.kv_repo.set_(service.NAMESPACE, 'bundle', saved)
        data = service.bundle()
        assert data["updated_at"] == "original" and data["series"]["gold"] == saved["series"]["gold"]
        assert data["series"]["cn10y"] == series(2)
        assert data["sources"][0]["date"] == "2024-01-02"
        assert service.kv_repo.get(service.NAMESPACE, service.BOND_BASELINE_KEY)[0]['cn10y'] == series(3)
        assert service.kv_repo.get(service.NAMESPACE, 'bundle')[0] == saved
    finally:
        service._snapshot_for.cache_clear()
        reset_for_testing()


def test_cn10y_history_splits_queries_and_refresh_revises_only_overlap(monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    calls = []

    def read(url):
        params = parse_qs(urlsplit(url).query)
        start, end = params["startDate"][0], params["endDate"][0]
        assert (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days <= 364
        calls.append((start, end))
        return json.dumps({"flag": "0", "heList": [{"workTime": end, "tenYear": "1.65"}]})

    monkeypatch.setattr(service, "_text", read)
    rows = service.cn10y_series("2024-01-01", "2025-01-04")
    assert calls == [("2024-01-01", "2024-12-30"), ("2024-12-31", "2025-01-04")]
    prior = [{"date": "2023-01-01", "value": 2}, {"date": "2025-01-04", "value": 1.7}]
    assert service.refresh_cn10y(prior, "2025-01-04") == [prior[0], rows[-1]]
    assert calls[-1] == ("2024-12-05", "2025-01-04")


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
