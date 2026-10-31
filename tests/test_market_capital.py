"""两融风险与资金归因：单位、时点、模型边界、席位去重与缺失。"""
from copy import deepcopy

import pytest

from analysis.market_capital import capital_report, etf_rows, institution_rows, margin_report


@pytest.fixture
def sample():
    dates = [f"2026-10-{day:02d}" for day in range(1, 22)]
    code = "600000.SH"
    return {"as_of": dates[-1], "flow_as_of": dates[-1], "calendar": dates,
            "stocks": [{"ts_code": code, "name": "样本股票", "industry": "银行", "market": "主板"}],
            "details": [{"trade_date": d, "ts_code": code, "rzye": 1e8, "rzmre": 1e7, "rzche": 8e6, "rqye": 1e6, "rqyl": 1e5, "rqmcl": 200, "rqchl": 100, "rzrqye": 1.01e8} for d in dates],
            "prices": [{"trade_date": d, "ts_code": code, "close": 8, "pct_chg": -2, "vol": 10000, "amount": 10000} for d in dates],
            "factors": [{"trade_date": d, "ts_code": code, "adj_factor": 1} for d in dates],
            "basics": [{"trade_date": dates[-1], "ts_code": code, "circ_mv": 100000, "turnover_rate": 1}],
            "margin": [{"trade_date": d, "exchange_id": exchange, "rzye": 1e9, "rzmre": 1e8, "rzche": 8e7, "rqye": 1e6, "rzrqye": 1.001e9} for d in dates for exchange in ("SSE", "SZSE")],
            "flows": [{"trade_date": d, "ts_code": code, "net_amount": 3, "buy_elg_amount": 2, "buy_lg_amount": 1, "buy_md_amount": -1, "buy_sm_amount": -2} for d in dates]}


def test_margin_units_cost_and_scenario(sample):
    report = margin_report(sample)
    row = report["rows"][0]
    assert row["cost"] == pytest.approx(10)  # 千元、手转换后VWAP=10元
    assert row["model_ratio"] == pytest.approx(144)
    assert row["float_ratio"] == 10
    assert row["buy_ratio"] == 100
    assert row["net5"] == 1e7 and row["net20"] == 4e7
    assert report["summary"]["rzye"] == 2e9
    assert report["stress"][0]["closeout_count"] == 0
    assert report["stress"][-1]["closeout_count"] == 1
    assert sum(r["balance"] for r in report["distribution"]) == 1e8
    assert row["buffer"] == pytest.approx((1 - 130 / 144) * 100)


@pytest.mark.parametrize("fault", ["missing_day", "missing_factor", "split", "suspended", "no_buy", "missing_buy"])
def test_model_does_not_invent_cost_for_incomplete_or_incomparable_data(sample, fault):
    if fault == "missing_day": sample["prices"].pop(5)
    if fault == "missing_factor": sample["factors"].pop(5)
    if fault == "split": sample["factors"][-1]["adj_factor"] = 2
    if fault == "suspended": sample["prices"][5]["vol"] = 0
    if fault == "no_buy":
        for row in sample["details"]: row["rzmre"] = 0
    if fault == "missing_buy": sample["details"][5]["rzmre"] = None
    report = margin_report(sample)
    assert report["rows"][0]["model_ratio"] is None
    assert report["coverage"]["model"] == 0
    assert report["stress"][0]["closeout_share"] is None


def test_shorter_window_and_same_day_basic_required(sample):
    sample["prices"].pop(2)
    sample["basics"][0]["trade_date"] = "2026-10-20"
    row = margin_report(sample, window=5)["rows"][0]
    assert row["cost"] == pytest.approx(10)
    assert row["float_ratio"] is None


def test_stock_whitelist_summary_requires_both_exchanges_and_no_future_price(sample):
    sample["details"].append({**sample["details"][-1], "ts_code": "510300.SH", "rzye": 9e9})
    sample["margin"] = [r for r in sample["margin"] if not (r["trade_date"] == sample["as_of"] and r["exchange_id"] == "SZSE")]
    sample["prices"][-1]["trade_date"] = "2026-10-22"
    report = margin_report(sample)
    assert len(report["rows"]) == 1
    assert report["summary"] == {}
    assert report["rows"][0]["model_ratio"] is None


def test_missing_repayment_is_not_zero_and_duplicate_stock_not_counted_twice(sample):
    sample["details"][-1]["rzche"] = None
    sample["details"].append(deepcopy(sample["details"][-1]))
    report = margin_report(sample)
    assert len(report["rows"]) == 1
    assert report["rows"][0]["net"] is None
    assert report["rows"][0]["net5"] is None


def test_finance_parameters_change_model_not_observed_balance(sample):
    first = margin_report(sample)
    second = margin_report(sample, deposit=100, closeout=140)
    assert second["rows"][0]["model_ratio"] == pytest.approx(160)
    assert first["summary"] == second["summary"]
    assert second["parameters"]["closeout"] == 140


def test_institution_dedup_buy_sell_sides_and_exclude_cumulative():
    base = {"trade_date": "20261021", "ts_code": "600000.SH", "exalter": "机构专用", "buy": 120, "sell": 20, "reason": "日涨幅偏离值", "side": "0"}
    raw = [base, {**base, "side": "1"}, {**base, "reason": "连续三个交易日累计偏离"}, {**base, "exalter": "普通营业部"}]
    rows = institution_rows(raw)
    assert len(rows) == 1 and rows[0]["net"] == 100


def test_etf_adjacent_calendar_and_nav_units(sample):
    sample["etf"] = [{"trade_date": "20261020", "ts_code": "510300.SH", "total_share": 100},
                     {"trade_date": "20261021", "ts_code": "510300.SH", "total_share": 110, "nav": 2, "total_size": 220}]
    row = etf_rows(sample)[-1]
    assert row["net"] == 200000
    assert row["size"] == 2200000
    sample["etf"][0]["trade_date"] = "20261019"
    assert etf_rows(sample)[-1]["net"] is None
    sample["etf"][0]["trade_date"] = "20261020"
    sample["etf"][-1]["nav"] = None
    assert etf_rows(sample)[-1]["net"] is None


def test_flow_units_filter_and_window_coverage(sample):
    sample["flows"].pop(17)
    report = capital_report(sample, start="2026-10-17", end="2026-10-21", participant="sm", keyword="银行")
    row = report["rows"][0]
    assert row["net"] == -80000 and row["main"] == 120000
    assert row["days"] == 4 and row["expected_days"] == 5 and row["complete"] is False
    assert report["sectors"][0]["net"] == -80000
    assert len(report["history"]) == 4
    assert capital_report(sample, keyword="不存在")["rows"] == []


def test_identity_observations_do_not_generate_money(sample):
    sample["national"] = [{"code": "600000.SH", "name": "样本股票", "holder": "中央汇金投资有限责任公司", "sector": "银行", "as_of": "2026-10-20", "period": "2026-09-30", "shares": 100, "net": None}]
    sample["quant"] = [{"code": "600000.SH", "name": "样本股票", "sector": "银行", "as_of": "2026-10-20", "activity": 50, "net": None}]
    for kind in ("national", "quant"):
        report = capital_report(sample, participant=kind, start="2026-10-19", end="2026-10-20")
        assert report["totals"]["net"] is None and report["history"] == []
        assert report["totals"]["count"] == 1
        assert capital_report(sample, participant=kind, start="2026-10-21", end="2026-10-21")["rows"] == []


def test_matrix_keeps_sources_separate_and_normalizes_quant_codes(sample):
    sample["quant"] = [{"code": "600000", "name": "样本股票", "sector": "银行", "as_of": sample["as_of"], "activity": 50, "net": None}]
    sample["national"] = [{"code": "600000.SH", "sector": "银行", "as_of": "2026-10-22", "net": None}]
    sample["institutions"] = [{"trade_date": "20261021", "ts_code": "600000.SH", "exalter": "机构专用", "buy": 120, "sell": 20, "reason": "日涨幅偏离"}]
    report = capital_report(sample)
    matrix = report["matrix"][0]
    assert matrix["main"] == 30000 and matrix["institution"] == 100
    assert matrix["quant_count"] == 1 and matrix["national_count"] == 0
    assert matrix["relation"] == "大单承接 / 小单流出"
    quant = capital_report(sample, participant="quant")
    assert quant["matrix"][0]["institution"] == 100
    assert quant["totals"]["observed_net"] is None


def test_partial_amount_coverage_and_requested_dates_are_explicit(sample):
    sample["flows"][-1]["net_amount"] = None
    report = capital_report(sample)
    assert report["totals"]["net"] is None and report["totals"]["amount_covered"] == 0
    report = capital_report(sample, start="2026-09-01")
    assert report["range_note"] and not report["rows"][0]["complete"]


@pytest.mark.parametrize("missing", [float("nan"), float("inf"), 3.0, "NaN", None, " "])
@pytest.mark.parametrize("participant", ["main", "sm", "etf"])
def test_search_with_missing_classification_in_existing_cache(sample, missing, participant):
    sample["stocks"].append({"ts_code": "600001.SH", "name": "样本新股", "industry": missing})
    sample["flows"].append({**sample["flows"][-1], "ts_code": "600001.SH"})
    sample["etf_meta"] = [{"ts_code": "510300.SH", "index_name": "银行"}, {"ts_code": "510301.SH", "index_name": missing}]
    sample["etf"] = [{"ts_code": code, "trade_date": date, "etf_name": "样本ETF", "total_share": shares, "nav": 2}
                     for code in ("510300.SH", "510301.SH") for date, shares in (("20261020", 100), ("20261021", 110))]
    report = capital_report(sample, participant=participant, keyword="样本")
    assert len(report["rows"]) == 2
    assert all(isinstance(r["name"], str) for r in report["sectors"])
    assert {r["sector"] for r in report["rows"]} == {"银行", "跟踪指数未取得" if participant == "etf" else "未分类"}
    assert report["totals"]["net"] == (400000 if participant == "etf" else 60000 if participant == "main" else -40000)
    assert len(capital_report(sample, participant=participant, keyword="不存在")["rows"]) == 0


def test_financing_and_identity_classifications_handle_nan_without_mutating_cache(sample):
    sample["stocks"][0]["industry"] = float("nan")
    assert margin_report(sample)["sectors"][0]["name"] == "未分类"
    sample["quant"] = [{"code": "600000", "name": float("nan"), "sector": float("nan"), "as_of": sample["as_of"], "net": None}]
    report = capital_report(sample, participant="quant")
    assert report["rows"][0]["name"] == "样本股票" and report["sectors"][0]["name"] == "未分类"
    assert isinstance(sample["quant"][0]["sector"], float)


def test_metadata_resolves_bare_codes_and_excludes_non_stock_instruments(sample):
    sample["quant"] = [{"code": "600000", "name": "600000", "sector": None, "as_of": sample["as_of"], "net": None},
                       {"code": "200029", "name": "B股样本", "as_of": sample["as_of"], "net": None}]
    sample["national"] = [{"code": "600000.SH", "name": "600000.SH", "sector": float("nan"), "as_of": sample["as_of"], "net": None},
                          {"code": "360001.SZ", "name": "360001.SZ", "sector": None, "as_of": sample["as_of"], "net": None}]
    sample["institutions"] = [{"trade_date": sample["as_of"], "ts_code": code, "exalter": "机构专用", "reason": "日涨幅偏离", "buy": 100, "sell": 20}
                              for code in ("600000.SH", "123267.SZ")]
    for kind in ("quant", "national", "institution"):
        report = capital_report(sample, participant=kind)
        assert len(report["rows"]) == 1
        assert report["rows"][0]["name"] == "样本股票" and report["rows"][0]["sector"] == "银行"
        assert report["rows"][0]["code"] == "600000.SH"
        assert report["excluded_nonstock"] == 1
        assert report["metadata_coverage"] == {"missing_names": 0, "missing_sectors": 0}


def test_missing_master_name_and_industry_can_use_existing_observations(sample):
    sample["stocks"][0].update(name=None, industry=float("nan"))
    sample["flows"][-1]["name"] = "已披露名称"
    sample["quant"] = [{"code": "600000", "name": "600000", "sector": "银行", "as_of": sample["as_of"], "net": None}]
    row = capital_report(sample, participant="quant")["rows"][0]
    assert row["name"] == "已披露名称" and row["sector"] == "银行"


def test_etf_name_and_index_can_use_other_verified_metadata(sample):
    sample["etf_meta"] = [{"ts_code": "510300.SH", "extname": "样本ETF", "index_code": "000300.SH", "index_name": float("nan")},
                          {"ts_code": "510301.SH", "index_code": "000300.SH", "index_name": "沪深300"}]
    sample["etf"] = [{"ts_code": "510300.SH", "trade_date": date, "etf_name": "510300.SH", "total_share": shares, "nav": 2}
                     for date, shares in (("20261020", 100), ("20261021", 110))]
    row = capital_report(sample, participant="etf")["rows"][0]
    assert row["name"] == "样本ETF" and row["sector"] == "沪深300"
