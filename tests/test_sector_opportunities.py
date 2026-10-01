from datetime import date, timedelta
import pytest

from analysis.sector_opportunities import sector_landscape
from analysis.trading_plan import sector_plan


def series(n=40, end=date(2026,9,30)):
    return [{"trade_date":(end-timedelta(days=n-i-1)).isoformat(), "pct_chg_mean":.4,
        "excess_vs_market":.2 if i<n-10 else .3, "breadth":.6, "net_amount":100,
        "member_count":20, "provisional":0} for i in range(n)]


def test_continuous_sector_status_exists_without_turning_events():
    rows=sector_landscape({("银行","行业"):series()},reference_date="2026-10-01")
    row=rows[0]
    assert row["state"]=="leading" and row["eligible"]
    assert row["return20"]==pytest.approx((1.004**20-1)*100)
    assert len(row["curve"])==40
    pulse={"sectors":{"landscape":rows,"fired":[],"watch":[]}}
    assert sector_plan({"sector":"银行"},pulse,today=date(2026,10,1))["entry_allowed"]


def test_stale_provisional_or_incomplete_data_remains_visible_but_not_eligible():
    old=series(end=date(2026,9,10))
    live=series();live[-1]["provisional"]=1
    short=series(10)
    result=sector_landscape({("旧板块","行业"):old,("盘中板块","行业"):live,("新板块","行业"):short},reference_date="2026-10-01")
    assert len(result)==3
    assert all(not r["eligible"] for r in result)
    assert next(r for r in result if r["sector"]=="旧板块")["stale"]
    assert next(r for r in result if r["sector"]=="新板块")["state"]=="unknown"


def test_missing_flow_or_breadth_is_not_treated_as_confirmation():
    rows=series();rows[-1]["net_amount"]=None;rows[-1]["breadth"]=None
    result=sector_landscape({("银行","行业"):rows})[0]
    assert result["flow5"] is None
    assert not result["eligible"]


def test_missing_daily_return_breaks_curve_instead_of_zero_filling():
    rows=series();rows[-12]["pct_chg_mean"]=None
    result=sector_landscape({("银行","行业"):rows})[0]
    assert result["relative20"] is None
    assert result["curve"][-12]["nav"] is None
    assert not result["eligible"]


def test_unchanged_negative_relative_strength_is_not_improvement():
    rows=series()
    for r in rows: r["excess_vs_market"]=-.3
    result=sector_landscape({("弱板块","行业"):rows})[0]
    assert result["state"]=="lagging"
    assert not result["eligible"]
    assert not sector_landscape({("未来数据","行业"):series(end=date(2026,10,2))},reference_date="2026-10-01")[0]["eligible"]
