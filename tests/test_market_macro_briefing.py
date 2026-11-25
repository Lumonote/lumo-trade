"""宏观简报保留日期、单位、缺口和发布边界，不让缓存冻结时效。"""
from copy import deepcopy
import datetime as dt

import pytest

from analysis.market_macro import SPECS, build_research, clean_series
from analysis.market_macro_briefing import build_briefing, reassess_briefing


def row(date, value, **extra):
    return {"date": date, "value": value, **extra}


def indicator(data, key):
    return next(r for r in data["indicators"] if r["key"] == key)


def dimension(data, key):
    return next(r for r in data["dimensions"] if r["key"] == key)


def test_empty_bundle_keeps_every_indicator_and_group_without_fabricating_values():
    report = build_briefing({}, SPECS, "2026-11-25")
    assert len(report["indicators"]) == len(SPECS)
    assert {r["key"] for r in report["groups"]} == {"growth", "liquidity", "external", "fiscal", "valuation"}
    assert report["coverage"] == {"total": len(SPECS), "available": 0, "fresh": 0, "stale": 0, "missing": len(SPECS)}
    assert all(r["value"] is None and r["change"] is None for r in report["indicators"])
    assert all(r["tone"] == "unavailable" for r in report["dimensions"])


def test_invalid_and_future_observations_or_releases_are_excluded():
    rows = [row("invalid", 100), row("2026-11-20", 1), row("2026-11-21", 2, published_on="2026-11-26"),
            row("2026-11-22", 3, published_on="invalid"), row("2026-11-23", float("nan")),
            row("2026-11-24", float("inf")), row("2026-11-25", True), row("2026-11-26", 9)]
    metric = indicator(build_briefing({"shibor3m": rows}, SPECS, "2026-11-25"), "shibor3m")
    assert metric["value"] == 1 and metric["date"] == "2026-11-20"
    assert metric["published_on"] is None and metric["previous_value"] is None


def test_zero_and_negative_rates_are_valid_and_daily_changes_use_basis_points():
    report = build_briefing({"real10y": [row("2026-11-24", -.25), row("2026-11-25", 0)]}, SPECS, "2026-11-25")
    metric = indicator(report, "real10y")
    assert metric["value"] == 0 and metric["previous_value"] == -.25
    assert metric["change"] == 25 and metric["change_unit"] == "bp"
    assert dimension(report, "external")["label"] == "实际利率上行"


def test_price_changes_are_ordinary_returns_and_nonpositive_prices_are_invalid():
    report = build_briefing({"gold": [row("2026-11-23", 100), row("2026-11-24", 110), row("2026-11-25", 0)]}, SPECS, "2026-11-25")
    metric = indicator(report, "gold")
    assert metric["value"] == 110 and metric["change"] == pytest.approx(10)
    assert metric["change_unit"] == "%"


@pytest.mark.parametrize("key,values,unit,expected", [
    ("m2", [7, 7.5], "百分点", .5), ("pmi", [49, 50], "百分点", 1),
    ("social", [100, 120], "亿元", 20), ("gdp", [4, 4.5], "百分点", .5),
])
def test_monthly_and_quarterly_change_units(key, values, unit, expected):
    dates = ["2026-06-30", "2026-09-30"] if key == "gdp" else ["2026-09-30", "2026-10-31"]
    report = build_briefing({key: [row(date, value) for date, value in zip(dates, values)]}, SPECS, "2026-11-25")
    metric = indicator(report, key)
    assert metric["change"] == expected and metric["change_unit"] == unit


@pytest.mark.parametrize("key,dates", [
    ("m2", ["2026-08-31", "2026-10-31"]),
    ("gdp", ["2026-03-31", "2026-09-30"]),
    ("gold", ["2026-11-01", "2026-11-25"]),
    ("lpr1y", ["2026-09-20", "2026-11-20"]),
])
def test_missing_period_or_long_gap_does_not_fabricate_previous_period_change(key, dates):
    report = build_briefing({key: [row(date, value) for date, value in zip(dates, [2, 3])]}, SPECS, "2026-11-25")
    metric = indicator(report, key)
    assert metric["change"] is None and metric["previous_value"] == 2
    assert any(r["key"] == f"comparison:{key}" for r in report["watchlist"])


def test_month_revisions_use_latest_known_release_without_comparing_same_period():
    series = {"m2": [row("2026-09-30", 8, published_on="2026-10-10"),
                      row("2026-10-31", 8.3, published_on="2026-11-10"),
                      row("2026-10-31", 8.4, published_on="2026-11-20"),
                      row("2026-10-31", 9, published_on="2026-12-01")]}
    metric = indicator(build_briefing(series, SPECS, "2026-11-25"), "m2")
    assert metric["value"] == 8.4 and metric["change"] == .4
    assert metric["published_on"] == "2026-11-20" and metric["previous_date"] == "2026-09-30"
    research = build_research({"as_of": "2026-11-25", "series": series})
    assert indicator(research["briefing"], "m2")["value"] == 8.4
    assert next(r for r in research["metrics"] if r["key"] == "m2")["value"] == 8.4


def test_research_cutoff_filters_known_future_publications_before_deduplication():
    rows = [row("2026-10-31", 49), row("2026-10-31", 51, published_on="2026-11-20"),
            row("2026-10-31", 55, published_on="2026-12-01"), row("2026-10-31", 60, published_on="invalid")]
    assert clean_series(rows, "2026-11-25") == [rows[1]]
    report = build_research({"as_of": "2026-11-25", "series": {"pmi": rows}}, factor="pmi", frequency="monthly", window=60)
    assert next(r for r in report["metrics"] if r["key"] == "pmi")["value"] == 51
    assert indicator(report["briefing"], "pmi")["value"] == 51
    assert report["chart"]["factor_history"] == [{"date": "2026-10-31", "value": 51}]
    assert any("为51" in note for note in report["insights"])


def test_m1_definition_and_fiscal_year_boundaries_block_changes():
    data = {key: [row("2024-12-31", 5), row("2025-01-31", 8)] for key in ("m1", "spending", "m2")}
    report = build_briefing(data, SPECS, "2025-02-20")
    assert indicator(report, "m1")["change"] is None
    assert "统计口径" in indicator(report, "m1")["change_label"]
    assert indicator(report, "spending")["change"] is None
    assert "跨年" in indicator(report, "spending")["change_label"]
    assert indicator(report, "m2")["change"] == 3


def test_current_reading_reassesses_old_snapshot_without_mutating_it():
    source = {"pmi": [row("2026-07-31", 49), row("2026-08-31", 51)],
              "real10y": [row("2026-09-01", 2), row("2026-09-02", 1.9)]}
    original = deepcopy(source)
    report = build_briefing(source, SPECS, "2026-09-02")
    cached = deepcopy(report)
    assert dimension(report, "growth")["label"] == "制造业扩张区间"
    current = reassess_briefing(report, "2026-11-25")
    assert current["as_of"] == "2026-09-02" and current["assessed_on"] == "2026-11-25"
    assert current["coverage"]["fresh"] == 0 and current["coverage"]["stale"] == 2
    assert indicator(current, "pmi")["value"] == 51 and indicator(current, "pmi")["date"] == "2026-08-31"
    assert dimension(current, "growth")["tone"] == "unavailable"
    assert source == original and report == cached


def test_freshness_has_independent_product_thresholds_not_pair_alignment_limits():
    report = build_briefing({"equity_bond_spread": [row("2026-11-20", 3)],
                            "lpr1y": [row("2026-10-20", 3)]}, SPECS, "2026-11-22")
    assert indicator(report, "equity_bond_spread")["status"] == "fresh"
    assert indicator(report, "equity_bond_spread")["max_age_days"] == 10
    assert indicator(report, "lpr1y")["status"] == "fresh"


def test_different_months_and_unknown_publication_are_visible_not_combined():
    report = build_briefing({"pmi": [row("2026-10-31", 51)], "cpi": [row("2026-09-30", .1)]}, SPECS, "2026-11-25")
    assert any(r["key"] == "period:growth" for r in report["watchlist"])
    assert any(r["key"] == "publication" for r in report["watchlist"])
    assert indicator(report, "pmi")["published_on"] is None


def test_briefing_never_reads_after_earlier_assessment_date():
    source = {"pmi": [row("2026-09-30", 49), row("2026-10-31", 51, published_on="2026-11-01")]}
    report = build_briefing(source, SPECS, "2026-11-25", assessed_on="2026-10-31")
    assert indicator(report, "pmi")["date"] == "2026-09-30"
    later = build_briefing(source, SPECS, "2026-11-25")
    assert indicator(reassess_briefing(later, "2026-10-31"), "pmi")["value"] is None


@pytest.mark.parametrize("value,label,tone", [(49, "制造业收缩区间", "negative"), (50, "制造业临界点", "neutral"), (51, "制造业扩张区间", "positive")])
def test_pmi_level_and_change_are_not_confused(value, label, tone):
    report = build_briefing({"pmi": [row("2026-09-30", value+1), row("2026-10-31", value)]}, SPECS, "2026-11-25")
    assert dimension(report, "growth")["label"] == label and dimension(report, "growth")["tone"] == tone
    assert indicator(report, "pmi")["change"] == -1


def test_research_includes_selected_index_spread_in_briefing():
    data = {"as_of": "2026-11-25", "series": {"cn10y": [row("2026-11-25", 2)]},
            "valuations": {"csi300": [{"date": "2026-11-25", "pe_ttm": 10}],
                           "shcomp": [{"date": "2026-11-25", "pe_ttm": 20}]}}
    assert indicator(build_research(data)["briefing"], "equity_bond_spread")["value"] == 8
    assert indicator(build_research(data, equity="shcomp")["briefing"], "equity_bond_spread")["value"] == 3


def test_query_reassesses_cached_briefing_when_clock_advances(isolated_price_db, monkeypatch):
    from webui.services import market_macro_service as service
    source = {"as_of": "2026-10-31", "series": {"real10y": [row("2026-10-30", 2), row("2026-10-31", 1.9)]},
              "calendar": [{"date": day, "status": "计划日期 / 可能调整"} for day in ("2026-11-10", "2026-11-25", "2026-12-10")]}
    service.kv_repo.set_(service.NAMESPACE, "bundle", source)
    monkeypatch.setattr(service, "now", lambda: dt.datetime(2026, 11, 1))
    first = service.query()
    cache = service._snapshot().research
    hits = cache.cache_info().hits
    monkeypatch.setattr(service, "now", lambda: dt.datetime(2026, 11, 25))
    second = service.query()
    assert cache.cache_info().hits > hits
    assert indicator(first["briefing"], "real10y")["status"] == "fresh"
    assert indicator(second["briefing"], "real10y")["status"] == "stale"
    assert second["briefing"]["assessed_on"] == second["today"] == "2026-11-25"
    assert indicator(cache()["briefing"], "real10y")["status"] == "fresh"
    assert first["calendar"][0]["status"] == "计划日期 / 可能调整"
    assert [r["status"] for r in second["calendar"]] == ["日期已过 / 不确认实际举行", "今日计划 / 以官方公布为准", "计划日期 / 可能调整"]
    assert service.bundle()["calendar"] == source["calendar"]


def test_dimension_primary_observation_is_first_and_always_visible():
    source = {key: [row("2026-09-30", 7), row("2026-10-31", 8)] for key in ("m1", "m2", "social", "social_stock")}
    source.update({key: [row("2026-11-24", 2), row("2026-11-25", 1.9)] for key in ("shibor3m", "lpr1y", "lpr5y")})
    liquidity = dimension(build_briefing(source, SPECS, "2026-11-25"), "liquidity")
    assert liquidity["label"] == "信用增速回升"
    assert liquidity["evidence"][0].startswith(SPECS["social_stock"]["name"])
    assert len(liquidity["evidence"]) == 4


def test_evidence_rounds_display_without_changing_precise_observations():
    source = {"social_stock": [row("2026-09-30", 7.1840198), row("2026-10-31", 7.11311)],
              "real10y": [row("2026-11-24", 2.000001), row("2026-11-25", 2)],
              "usdcny": [row("2026-11-24", 7.12345), row("2026-11-25", 7.123456)]}
    report = build_briefing(source, SPECS, "2026-11-25")
    metric = indicator(report, "social_stock")
    assert metric["value"] == 7.11311 and metric["change"] == -.0709098
    evidence = dimension(report, "liquidity")["evidence"][0]
    assert "7.11%" in evidence and "-0.07百分点" in evidence and "7.11311" not in evidence
    external = dimension(report, "external")["evidence"]
    assert "+0bp" in external[0] and "-0" not in external[0]
    assert any("7.1235人民币/美元" in text for text in external)


@pytest.mark.parametrize("mode", ["success", "failed", "superseded"])
def test_macro_refresh_reports_only_successfully_adopted_fiscal_channels(isolated_price_db, monkeypatch, mode):
    from webui.services import market_macro_service as service
    monkeypatch.setattr(service, "now", lambda: dt.datetime(2026, 11, 25))
    monkeypatch.setattr(service, "_records", lambda *a, **kw: [])
    monkeypatch.setattr(service, "fred_series", lambda *a, **kw: [])
    monkeypatch.setattr(service, "refresh_cn10y", lambda *a, **kw: [])
    monkeypatch.setattr(service, "fetch_releases", lambda *a, **kw: ({}, []))
    monkeypatch.setattr(service, "fomc_calendar", lambda *a, **kw: [])
    monkeypatch.setattr(service, "_text", lambda *a, **kw: "offline")
    fiscal = {"date": "2026-10-31", "period": "2026-10", "published_on": "2026-11-20",
              "url": "https://www.mof.gov.cn/old", "title": "财政", "spending_yoy": 3}
    debt = {**fiscal, "url": "https://zwgls.mof.gov.cn/old", "title": "地方债", "new": 100, "refinance": 200}
    old = {"as_of": "2026-11-20", "fiscal": [fiscal], "debt": [debt],
           "series": {"spending": [row("2026-10-31", 3)], "local_new": [row("2026-10-31", 100)],
                      "local_refinance": [row("2026-10-31", 200)]}}
    new_fiscal = {**fiscal, "url": "https://www.mof.gov.cn/new", "published_on": "2026-11-24", "spending_yoy": 4}
    new_debt = {**debt, "url": "https://zwgls.mof.gov.cn/new", "published_on": "2026-11-24", "new": 110}
    if mode == "superseded":
        new_fiscal["published_on"] = new_debt["published_on"] = "2026-11-10"
    monkeypatch.setattr(service, "listing", lambda url, **kw: [new_fiscal if url == service.FISCAL_URL else new_debt])
    monkeypatch.setattr(service, "parse_fiscal", lambda *args: None if mode == "failed" else new_fiscal)
    monkeypatch.setattr(service, "parse_debt", lambda *args: None if mode == "failed" else new_debt)
    report = service.collect(old=old)
    sources = {r["name"]: r for r in report["sources"]}
    for key in ("spending", "local_new", "local_refinance"):
        assert sources[SPECS[key]["name"]]["status"] == ("本次读取" if mode == "success" else "保留原快照")
    assert report["series"]["spending"][-1]["value"] == (4 if mode == "success" else 3)
