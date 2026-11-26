"""宏观关系图谱：真实样本、频率边界及条件传导说明。"""
import calendar
import datetime as dt
import json
import math

import pytest

from analysis.market_macro import SPECS, build_research, pairs_for


def monthly_rows(count, start_year=2022, start_month=1, value=lambda i: 100 + i):
    result = []
    for i in range(count):
        year, month = divmod(start_year * 12 + start_month - 1 + i, 12)
        month += 1
        result.append({"date": f"{year}-{month:02}-{calendar.monthrange(year, month)[1]}", "value": value(i)})
    return result


def report_rows(bundle, **kwargs):
    return {row["key"]: row for row in build_research(bundle, **kwargs)["macro_relations"]["rows"]}


def test_all_categories_remain_visible_without_observations():
    report = build_research({"as_of": "2026-11-26"})
    atlas = report["macro_relations"]
    assert {row["key"] for row in atlas["rows"]} == set(SPECS)
    assert {row["group"] for row in atlas["rows"]} == {group["key"] for group in atlas["groups"]}
    for row in atlas["rows"]:
        assert all(row[key] for key in ("mechanism", "up", "down", "sectors", "watch", "caveat", "sources"))
        assert row["evidence"]["rho"] is None
        assert row["evidence"]["n"] == 0
        assert "pairs" not in row["evidence"]
        assert all(source["url"].startswith("https://") for source in row["sources"])
    json.dumps(report, allow_nan=False)


def test_quarterly_gdp_has_no_made_up_monthly_statistic():
    rows = report_rows({"as_of": "2026-11-26", "series": {"gdp": monthly_rows(30)}})
    evidence = rows["gdp"]["evidence"]
    assert evidence["frequency"] == "quarterly"
    assert evidence["window"] is None
    assert evidence["rho"] is None and evidence["n"] == 0
    assert "季频" in evidence["status"]


def test_daily_report_still_excludes_partial_month_in_monthly_evidence():
    prices = monthly_rows(30)
    factors = monthly_rows(30, value=lambda i: 5 + i * i / 100)
    # The final June observation is a midmonth reading, not a completed June return.
    prices[-1]["date"] = factors[-1]["date"] = "2024-06-15"
    bundle = {"as_of": "2024-06-15", "equities": {"csi300": prices}, "series": {"m2": factors}}
    mid = report_rows(bundle)["m2"]["evidence"]
    assert mid["frequency"] == "monthly" and mid["window"] == 60
    assert mid["n"] == 28 and mid["end"] == "2024-05-31"
    end = report_rows({**bundle, "as_of": "2024-06-30"})["m2"]["evidence"]
    assert end["n"] == 29 and end["end"] == "2024-06-15"


def test_monthly_gaps_do_not_become_samples_and_no_variation_is_null():
    prices = monthly_rows(36)
    factors = monthly_rows(36, value=lambda i: 0)
    del factors[10]
    bundle = {"as_of": "2024-12-31", "equities": {"csi300": prices}, "series": {"cpi": factors}}
    evidence = report_rows(bundle)["cpi"]["evidence"]
    assert evidence["n"] == 33
    assert evidence["rho"] is None and evidence["spearman"] is None
    assert evidence["status"] == "因子无有效变化"
    # Zero CPI is an actual observation, so adjacent observations still count.
    assert evidence["start"] == "2022-02-28"


def test_methodology_breaks_are_excluded_from_new_evidence():
    prices = monthly_rows(36, start_year=2023)
    factors = monthly_rows(36, start_year=2023, value=lambda i: i * i / 100)
    rows = report_rows({"as_of": "2025-12-31", "equities": {"csi300": prices},
                        "series": {"m1": factors, "spending": factors}})
    m1, spending = rows["m1"]["evidence"], rows["spending"]["evidence"]
    assert m1["n"] == 11 and m1["start"] == "2025-02-28"
    assert m1["rho"] is None and "新口径" in m1["note"]
    assert spending["n"] == 33
    assert "跨年" in spending["note"]


@pytest.mark.parametrize("key", ["m1", "spending"])
def test_atlas_and_detail_share_methodology_and_sample_counts(key):
    prices = monthly_rows(60, start_year=2023, value=lambda i: 100 * math.exp(i * i / 10000))
    factors = monthly_rows(60, start_year=2023, value=lambda i: 5 + i * i / 100)
    bundle = {"as_of": "2027-12-31", "equities": {"csi300": prices}, "series": {key: factors}}
    report = build_research(bundle, factor=key, frequency="monthly", window=60)
    evidence = next(row["evidence"] for row in report["macro_relations"]["rows"] if row["key"] == key)
    for field in ("n", "rho", "spearman", "start", "end"):
        assert evidence[field] == report["relationship"][field]
    assert evidence["rho"] == pytest.approx(1)
    lagged = pairs_for(prices, factors, SPECS[key], frequency="monthly", window=60, lag=3)
    assert lagged
    if key == "m1":
        assert min(row["factor_date"] for row in lagged) == "2025-02-28"
    else:
        assert all(row["factor_date"][5:7] != "01" for row in lagged)
        # A valid September→October change must survive when it leads a January return.
        assert any(row["date"][5:7] == "01" for row in lagged)


def test_index_selection_changes_evidence_but_detail_window_does_not():
    start = dt.date(2024, 1, 1)
    dates = [(start + dt.timedelta(days=i)).isoformat() for i in range(100)]
    positive = [{"date": date, "value": 100 * math.exp(i * i / 10000)} for i, date in enumerate(dates)]
    negative = [{"date": date, "value": 100 * math.exp(-i * i / 10000)} for i, date in enumerate(dates)]
    factors = [{"date": date, "value": i * i / 100} for i, date in enumerate(dates)]
    bundle = {"as_of": dates[-1], "equities": {"csi300": positive, "csi500": negative}, "series": {"shibor3m": factors}}
    first = report_rows(bundle, factor="shibor3m", window=60)["shibor3m"]["evidence"]
    assert first["n"] == 99 and first["rho"] == pytest.approx(1)
    assert first["window"] == 252 and first["lag"] == 0
    assert first == report_rows(bundle, factor="shibor3m", window=120, lag=5)["shibor3m"]["evidence"]
    second = report_rows(bundle, equity="csi500")["shibor3m"]["evidence"]
    assert second["rho"] == pytest.approx(-1)


def test_snapshot_cutoff_excludes_future_and_unpublished_observations():
    prices = monthly_rows(30)
    factors = monthly_rows(30, value=lambda i: i * i / 100)
    factors[-2]["published_on"] = "2024-07-20"
    rows = report_rows({"as_of": "2024-05-31", "equities": {"csi300": prices}, "series": {"pmi": factors}})
    evidence = rows["pmi"]["evidence"]
    assert evidence["n"] == 27 and evidence["end"] == "2024-04-30"
