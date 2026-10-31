"""宏观与A股的描述性研究：变化量相关、事件窗口、同日盈利估值恒等式。"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
import datetime as dt
import math
from statistics import mean, median

from analysis.market_health import number
from analysis.market_health_fusion import iso

INDEXES = {"shcomp": ("000001.SH", "上证指数"), "csi300": ("000300.SH", "沪深300"),
           "chinext": ("399006.SZ", "创业板指"), "csi500": ("000905.SH", "中证500"), "csi1000": ("000852.SH", "中证1000")}
FRED = {"fed_upper": ("DFEDTARU", "联邦基金目标上限", "%", "rate"),
        "fed_lower": ("DFEDTARL", "联邦基金目标下限", "%", "rate"),
        "us10y": ("DGS10", "美国10年国债收益率", "%", "rate"),
        "real10y": ("DFII10", "美国10年实际利率", "%", "rate"),
        "usd": ("DTWEXBGS", "美元广义贸易加权指数", "点", "price"),
        "usdcny": ("DEXCHUS", "美元兑人民币", "人民币/美元", "price")}
SPECS = {key: {"name": label, "unit": unit, "kind": kind, "frequency": "daily", "foreign": True,
               "source": f"FRED / {code}", "url": f"https://fred.stlouisfed.org/series/{code}"}
         for key, (code, label, unit, kind) in FRED.items()}
SPECS.update({
    "gold": {"name": "上金所Au99.99", "unit": "元/克", "kind": "price", "frequency": "daily", "source": "上金所 / TuShare sge_daily", "url": "https://www.sge.com.cn/sjzx/quotation_daily_new"},
    "lpr1y": {"name": "1年期LPR", "unit": "%", "kind": "rate", "frequency": "daily", "max_age": 45, "source": "全国银行间同业拆借中心 / TuShare", "url": "https://tushare.pro/document/2?doc_id=151"},
    "lpr5y": {"name": "5年期LPR", "unit": "%", "kind": "rate", "frequency": "daily", "max_age": 45, "source": "全国银行间同业拆借中心 / TuShare", "url": "https://tushare.pro/document/2?doc_id=151"},
    "shibor3m": {"name": "3个月Shibor", "unit": "%", "kind": "rate", "frequency": "daily", "source": "全国银行间同业拆借中心 / TuShare", "url": "https://tushare.pro/document/2?doc_id=149"},
    "m2": {"name": "M2同比", "unit": "%", "kind": "rate", "frequency": "monthly", "source": "人民银行 / TuShare cn_m", "url": "https://tushare.pro/document/2?doc_id=242"},
    "m1": {"name": "M1同比", "unit": "%", "kind": "rate", "frequency": "monthly", "source": "人民银行 / TuShare cn_m", "url": "https://tushare.pro/document/2?doc_id=242"},
    "cpi": {"name": "CPI同比", "unit": "%", "kind": "rate", "frequency": "monthly", "source": "国家统计局 / TuShare cn_cpi", "url": "https://tushare.pro/document/2?doc_id=228"},
    "ppi": {"name": "PPI同比", "unit": "%", "kind": "rate", "frequency": "monthly", "source": "国家统计局 / TuShare cn_ppi", "url": "https://tushare.pro/document/2?doc_id=245"},
    "pmi": {"name": "制造业PMI", "unit": "%", "kind": "level", "frequency": "monthly", "source": "国家统计局 / TuShare cn_pmi", "url": "https://tushare.pro/document/2?doc_id=325"},
    "social": {"name": "社融当月增量", "unit": "亿元", "kind": "level", "frequency": "monthly", "source": "人民银行 / TuShare sf_month", "url": "https://tushare.pro/document/2?doc_id=310"},
    "social_stock": {"name": "社融存量同比（计算）", "unit": "%", "kind": "rate", "frequency": "monthly", "source": "同月社融存量同比计算", "url": "https://tushare.pro/document/2?doc_id=310"},
    "gdp": {"name": "GDP当季同比", "unit": "%", "kind": "rate", "frequency": "quarterly", "source": "国家统计局 / TuShare cn_gdp", "url": "https://tushare.pro/document/2?doc_id=227"},
    "spending": {"name": "一般公共预算累计支出同比", "unit": "%", "kind": "rate", "frequency": "monthly", "source": "财政部原文 / 累计口径", "url": "https://www.mof.gov.cn/zhengwuxinxi/redianzhuanti/quanguocaizhengshouzhiqingkuang/"},
    "local_new": {"name": "新增地方政府债券发行", "unit": "亿元", "kind": "level", "frequency": "monthly", "source": "财政部债务管理司 / 当月新增", "url": "https://zwgls.mof.gov.cn/tjsj/"},
    "local_refinance": {"name": "地方政府再融资债券发行", "unit": "亿元", "kind": "level", "frequency": "monthly", "source": "财政部债务管理司 / 当月再融资", "url": "https://zwgls.mof.gov.cn/tjsj/"},
})

TRANSMISSIONS = [
    {"key": "fed", "name": "美联储加息 / 降息", "path": "政策利率 → 美债与实际利率 → 美元、全球折现率与风险偏好 → A股估值与盈利预期",
     "benefit": "通胀回落、增长稳定时的降息，可能缓解成长资产估值压力；需汇率与资金流确认。", "risk": "衰退式降息可能伴随盈利下修与风险溢价上升；加息也可能反映需求强，不能只按利率方向判断。", "watch": "目标区间、实际利率、美元、人民币与企业订单", "sectors": "电子、计算机、医药、非银金融、出口产业"},
    {"key": "gold", "name": "黄金与实际利率", "path": "实际利率与持有机会成本 / 美元与避险需求 → 国际黄金 → 汇率与境内溢价 → 人民币黄金与黄金产业",
     "benefit": "实际利率回落、美元走弱或避险需求增加，可能支持黄金；矿企盈利还取决于成本、产量和套保。", "risk": "金价与实际利率可以阶段性同涨。人民币升值、国内溢价回落可能抵消国际金价上涨。", "watch": "实际利率、美元兑人民币、金价与矿企成本", "sectors": "有色金属、黄金产业；珠宝消费与矿企影响不同"},
    {"key": "currency", "name": "美元与人民币", "path": "中美利差与风险偏好 → 汇率 → 资本流动、进口成本、出口结算及外币负债",
     "benefit": "人民币升值可能减轻进口成本与外币负债压力；贬值可能改善部分出口商人民币收入。", "risk": "汇率与资金流可双向反馈；企业套保、成本币种与定价权不同，行业影响并不一致。", "watch": "美元广义指数（非DXY）、美元兑人民币、进出口与套保", "sectors": "航空、造纸、出口制造、电子、资源进口"},
    {"key": "monetary", "name": "国内降准与降息", "path": "准备金 / 政策利率 → 银行负债与贷款利率 → 信用需求与企业融资 → 估值和盈利",
     "benefit": "降准提供银行体系流动性；LPR下降可能降低企业和居民融资成本。", "risk": "信用需求不足时流动性未必进入实体或股市；银行息差可能承压，不把降准金额等同股市流入。", "watch": "LPR、Shibor、社融存量、M1/M2、银行净息差", "sectors": "银行、房地产、非银金融、成长行业"},
    {"key": "fiscal", "name": "财政发力与地方债", "path": "预算安排 → 新增债券发行 → 资金拨付与项目执行 → 企业订单、现金流和需求",
     "benefit": "新增专项债、设备更新与财政支出，可能通过订单和需求支持相关行业。", "risk": "额度、发行、拨付与实际支出是不同阶段；再融资和化债主要改善债务结构，不等同新增需求。", "watch": "新增 / 再融资分列、一般预算与基金支出、项目落地", "sectors": "建筑、建材、机械、电力设备、消费"},
    {"key": "property", "name": "地产支持与化债", "path": "融资与购房政策 → 销售 / 库存 / 交付 → 地产现金流、地方财政和银行资产质量",
     "benefit": "销售和回款改善后，可能沿产业链传导至家居、建材及银行资产质量。", "risk": "政策公布不等于销售和利润已经修复；存量债务置换不应重复计作新增投资。", "watch": "销售、库存、交付、土地收入、信用利差", "sectors": "房地产、建筑材料、家用电器、银行"},
    {"key": "consumption", "name": "促消费与产业支持", "path": "补贴 / 贴息 / 投资支持 → 需求前移与资本开支 → 收入、毛利及现金流",
     "benefit": "以旧换新、设备更新、科技支持可能改善受支持产品订单。", "risk": "替代原有消费、提前透支需求或价格竞争，可能使收入改善未能转化为利润。", "watch": "预算到位、销量、利润率、补贴结算", "sectors": "汽车、家电、机械、电子、计算机"},
    {"key": "capital", "name": "资本市场制度政策", "path": "发行、交易、分红与中长期资金政策 → 市场供需和风险溢价 → 估值与行业分化",
     "benefit": "改善长期资金参与和股东回报预期，可能降低部分资产的风险溢价。", "risk": "制度作用有时滞，交易活跃不等于实体盈利改善；需结合估值和实际资金证据。", "watch": "政策执行、股东回报、ETF申赎、机构披露与成交", "sectors": "非银金融、银行、稳定分红与不同融资需求企业"},
]


def clean_series(rows, end=None):
    values = {}
    for row in rows or []:
        date, value = iso(row.get("date")), number(row.get("value"))
        if date and value is not None and (not end or date <= end):
            values[date] = {**row, "date": date, "value": value}
    return [values[d] for d in sorted(values)]


def pearson(x, y):
    if len(x) < 3 or len(x) != len(y):
        return None
    mx, my = mean(x), mean(y)
    xx, yy = sum((v - mx) ** 2 for v in x), sum((v - my) ** 2 for v in y)
    if xx <= 1e-18 or yy <= 1e-18:
        return None
    return max(-1, min(1, sum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(xx * yy)))


def ranks(values):
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    result, i = [0.0] * len(values), 0
    while i < len(ordered):
        j = i + 1
        while j < len(ordered) and ordered[j][1] == ordered[i][1]:
            j += 1
        for index, value in ordered[i:j]:
            result[index] = (i + j - 1) / 2 + 1
        i = j
    return result


def statistics(pairs, minimum=60):
    x, y = [r["x"] for r in pairs], [r["y"] for r in pairs]
    rho = pearson(x, y) if len(pairs) >= minimum else None
    mx, my = (mean(x), mean(y)) if x else (0, 0)
    denominator = sum((v - mx) ** 2 for v in x)
    beta = sum((a - mx) * (b - my) for a, b in zip(x, y)) / denominator if rho is not None and denominator > 0 else None
    return {"n": len(pairs), "rho": rho, "spearman": pearson(ranks(x), ranks(y)) if rho is not None else None,
            "beta": beta, "r2": rho ** 2 if rho is not None else None, "start": pairs[0]["date"] if pairs else None,
            "end": pairs[-1]["date"] if pairs else None, "status": "样本不足" if len(pairs) < minimum else "因子无有效变化" if rho is None else "描述性相关 / 非因果"}


def monthly(rows):
    result = {}
    for row in clean_series(rows):
        result[row["date"][:7]] = row
    return list(result.values())


def pairs_for(asset, factor, spec, *, window=252, lag=0, frequency="daily"):
    asset, factor = clean_series(asset), clean_series(factor)
    if frequency == "monthly":
        asset, factor = monthly(asset), monthly(factor)
        by_month = {r["date"][:7]: r for r in factor}
        result = []
        for i in range(max(1, lag + 1), len(asset)):
            current, previous = asset[i], asset[i - 1]
            target, prior = asset[i - lag], asset[i - lag - 1]
            # 不用跨缺月的变化冒充一个月；月度关系按统计期描述，不作为实时回测。
            def serial(row):
                return int(row["date"][:4]) * 12 + int(row["date"][5:7])
            if serial(current) - serial(previous) != 1 or serial(target) - serial(prior) != 1:
                continue
            f, p = by_month.get(target["date"][:7]), by_month.get(prior["date"][:7])
            if f and p and previous["value"] > 0 and current["value"] > 0:
                x = change(f["value"], p["value"], spec["kind"], daily=False)
                if x is not None:
                    result.append({"date": current["date"], "factor_date": f["date"], "x": x, "y": math.log(current["value"] / previous["value"]) * 100})
        cutoff = asset[-window]["date"] if len(asset) >= window else ""
        return [r for r in result if r["date"] >= cutoff]
    if spec["frequency"] != "daily":
        return []
    dates = [r["date"] for r in factor]
    aligned = []
    for row in asset:
        pos = (bisect_left(dates, row["date"]) if spec.get("foreign") else bisect_right(dates, row["date"])) - 1
        value = factor[pos] if pos >= 0 else None
        age = (dt.date.fromisoformat(row["date"]) - dt.date.fromisoformat(value["date"])).days if value else None
        aligned.append(value if age is not None and age <= spec.get("max_age", 10) else None)
    result = []
    for i in range(max(1, lag + 1), len(asset)):
        current, previous = asset[i], asset[i - 1]
        f, p = aligned[i - lag], aligned[i - lag - 1]
        if not f or not p or f["date"] == p["date"] or previous["value"] <= 0 or current["value"] <= 0:
            continue
        if (dt.date.fromisoformat(current["date"]) - dt.date.fromisoformat(previous["date"])).days > 15:
            continue
        x = change(f["value"], p["value"], spec["kind"])
        if x is not None:
            result.append({"date": current["date"], "factor_date": f["date"], "x": x, "y": math.log(current["value"] / previous["value"]) * 100})
    cutoff = asset[-window]["date"] if len(asset) >= window else ""
    return [r for r in result if r["date"] >= cutoff]


def change(current, previous, kind, daily=True):
    if kind == "price":
        return math.log(current / previous) * 100 if current > 0 and previous > 0 else None
    return (current - previous) * (100 if kind == "rate" and daily else 1)


def relationship(asset, factor, spec, *, window=252, lag=0, frequency="daily"):
    pairs = pairs_for(asset, factor, spec, window=window, lag=lag, frequency=frequency)
    stats = statistics(pairs, 24 if frequency == "monthly" else 60)
    stats["x_unit"] = "对数变化%" if spec["kind"] == "price" else "bp" if spec["kind"] == "rate" and frequency == "daily" else "百分点" if spec["kind"] == "rate" else spec["unit"] + "变化"
    stats["y_unit"] = "指数对数收益%"
    stats["pairs"] = pairs
    timeline = monthly(asset) if frequency == "monthly" else clean_series(asset)
    stats["stability"] = [{"window": n, **statistics([r for r in pairs if r["date"] >= (timeline[-n]["date"] if len(timeline) >= n else "")], 24 if frequency == "monthly" else 60)}
                          for n in ((24, 36, 60) if frequency == "monthly" else (60, 120, 252)) if n <= window]
    return stats


def event_study(events, asset, *, category="all", keyword="", limit=1000):
    rows = clean_series(asset)
    dates = [r["date"] for r in rows]
    result = []
    for event in sorted(events, key=lambda r: r.get("date") or "", reverse=True):
        date = iso(event.get("date"))
        if not date or category != "all" and event.get("category") != category:
            continue
        if keyword and keyword.casefold() not in " ".join(str(event.get(k) or "") for k in ("title", "summary", "category", "tags")).casefold():
            continue
        anchor = bisect_right(dates, date)
        row = {**event, "anchor": dates[anchor] if anchor < len(dates) else None, "returns": {}, "overlap": False}
        for horizon in (1, 5, 20, 60):
            end = anchor + horizon - 1
            valid = anchor > 0 and end < len(rows) and rows[anchor - 1]["value"] > 0
            row["returns"][str(horizon)] = (rows[end]["value"] / rows[anchor - 1]["value"] - 1) * 100 if valid else None
        if anchor > 0 and anchor < len(rows) and rows[anchor - 1]["value"] > 0:
            path = [r["value"] for r in rows[anchor:min(len(rows), anchor + 20)]]
            row["drawdown20"] = min((value / rows[anchor - 1]["value"] - 1) * 100 for value in path) if len(path) == 20 else None
        else:
            row["drawdown20"] = None
        result.append(row)
    for row in result:
        if row["anchor"]:
            position = bisect_left(dates, row["anchor"])
            row["overlap"] = any(other is not row and other["anchor"] and abs(bisect_left(dates, other["anchor"]) - position) < 20 for other in result)
    summary = []
    for horizon in (1, 5, 20, 60):
        values = [r["returns"][str(horizon)] for r in result if r["returns"][str(horizon)] is not None]
        summary.append({"horizon": horizon, "n": len(values), "mean": mean(values) if values else None, "median": median(values) if values else None,
                        "positive": sum(v > 0 for v in values), "positive_share": sum(v > 0 for v in values) / len(values) * 100 if values else None})
    return {"rows": result[:limit], "total": len(result), "summary": summary,
            "note": "仅按日级日期观察，事件锚点保守取公告/生效日期之后的首个A股交易日，以此前收盘为基准。无精确公告时刻，不声称捕捉公告当日反应；窗口重叠、其他政策及行情会影响结果，频率不是未来上涨概率。"}


def valuation_decomposition(prices, valuations, *, sessions=250):
    price = {r["date"]: r["value"] for r in clean_series(prices)}
    rows = []
    for raw in valuations or []:
        date, pe = iso(raw.get("date")), number(raw.get("pe_ttm"))
        if date in price and price[date] > 0 and pe is not None and pe > 0:
            rows.append({"date": date, "price": price[date], "pe": pe, "earnings_proxy": price[date] / pe})
    rows = sorted({r["date"]: r for r in rows}.values(), key=lambda r: r["date"])
    if len(rows) <= sessions:
        return {"available": False, "note": "同日价格与正PE_TTM交集不足完整窗口；不以其他指数或日期替代。", "rows": []}
    trading_dates = sorted(price)
    last = rows[-1]
    position = bisect_left(trading_dates, last["date"])
    expected = trading_dates[max(0, position - sessions):position + 1]
    selected = {r["date"]: r for r in rows}
    if len(expected) != sessions + 1 or any(date not in selected for date in expected):
        return {"available": False, "note": "完整交易窗口存在同日估值缺失；不跨缺日延长窗口或补值。", "rows": []}
    window_rows = [selected[date] for date in expected]
    first = window_rows[0]
    p = math.log(last["price"] / first["price"]) * 100
    e = math.log(last["earnings_proxy"] / first["earnings_proxy"]) * 100
    v = math.log(last["pe"] / first["pe"]) * 100
    history = [{"date": r["date"], "price": r["price"] / first["price"] * 100, "earnings": r["earnings_proxy"] / first["earnings_proxy"] * 100,
                "valuation": r["pe"] / first["pe"] * 100} for r in window_rows]
    return {"available": True, "as_of": last["date"], "start": first["date"], "observations": len(rows), "sessions": sessions,
            "pe": last["pe"], "pe_percentile": sum(r["pe"] <= last["pe"] for r in rows[-1250:]) / len(rows[-1250:]) * 100,
            "price_pct": (last["price"] / first["price"] - 1) * 100, "price_log": p, "earnings_log": e, "valuation_log": v, "residual": p - e - v,
            "rows": history, "note": "价格=隐含盈利代理×PE_TTM；对数价格变化=盈利代理变化+估值变化。盈利代理是指数价格/同日PE_TTM，不是上市公司实际利润增速，受成分与统计口径变化影响；这是恒等式分解，不是拟合预测。PE历史分位使用最多1250条同日有效观测。"}


def scenario(earnings=0, pe=0, discount_bp=-25, duration=10):
    values = [number(v) for v in (earnings, pe, discount_bp, duration)]
    if any(v is None for v in values) or not -80 <= values[0] <= 100 or not -80 <= values[1] <= 100 or not -300 <= values[2] <= 300 or not 0 <= values[3] <= 40:
        raise ValueError("情景参数无效：盈利/PE变化-80%至100%，折现率-300至300bp，久期0至40年")
    e, p, rate, years = values
    return {"earnings": e, "pe": p, "discount_bp": rate, "duration": years,
            "identity_pct": ((1 + e / 100) * (1 + p / 100) - 1) * 100,
            "duration_pct": ((1 + e / 100) * math.exp(-years * rate / 10000) - 1) * 100,
            "note": "独立教学情景：盈利×PE恒等式与恒定久期折现近似分开计算。折现率变化由你设定，不代表美联储政策利率一比一传导；久期是假设，不是估计出的A股久期。"}


def latest_metrics(series, end):
    result = []
    for key, spec in SPECS.items():
        rows = clean_series(series.get(key, []), end)
        last = rows[-1] if rows else {}
        result.append({"key": key, **spec, "date": last.get("date"), "period": last.get("period"), "published_on": last.get("published_on"),
                       "value": last.get("value"), "previous": rows[-2]["value"] if len(rows) > 1 else None,
                       "age_days": (dt.date.fromisoformat(end) - dt.date.fromisoformat(last["date"])).days if last else None,
                       "url": last.get("url") or spec["url"], "rows": len(rows), "status": "已取得" if last else "未取得"})
    return result


def build_report(bundle, *, equity="csi300", factor="real10y", window=252, lag=0, frequency="daily", category="all", keyword="", earnings=0, pe=0, discount_bp=-25, duration=10):
    if equity not in INDEXES or factor not in SPECS or frequency not in ("daily", "monthly"):
        raise ValueError("未知指数、宏观因子或频率")
    if SPECS[factor]["frequency"] == "quarterly" or frequency == "daily" and SPECS[factor]["frequency"] != "daily":
        raise ValueError("月度宏观因子请选择月频；GDP季频仅作为环境指标")
    windows, lags = ((60, 120, 252, 756), (0, 1, 5, 20)) if frequency == "daily" else ((24, 36, 60, 120), (0, 1, 3, 6))
    if window not in windows or lag not in lags:
        raise ValueError("窗口或领先期不在支持范围内")
    end = iso(bundle.get("as_of")) or dt.date.today().isoformat()
    series = {key: clean_series(value, end) for key, value in (bundle.get("series") or {}).items()}
    all_prices = clean_series((bundle.get("equities") or {}).get(equity, []), end)
    prices = all_prices
    complete_end = end
    if frequency == "monthly":
        # 月中刷新不把未结束的当月指数收益当作完整月度样本。
        day = dt.date.fromisoformat(end)
        next_month = (day.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        if day != next_month - dt.timedelta(days=1):
            prices = [r for r in prices if r["date"][:7] < end[:7]]
            complete_end = (day.replace(day=1) - dt.timedelta(days=1)).isoformat()
    chosen = relationship(prices, series.get(factor, []), SPECS[factor], window=window, lag=lag, frequency=frequency)
    overview = []
    for key, spec in SPECS.items():
        if frequency == "daily" and spec["frequency"] != "daily" or spec["frequency"] == "quarterly":
            continue
        row = relationship(prices, series.get(key, []), spec, window=window, lag=lag, frequency=frequency)
        overview.append({k: v for k, v in {"key": key, "name": spec["name"], **row}.items() if k not in ("pairs", "stability")})
    sectors = []
    for key, raw in (bundle.get("sectors") or {}).items():
        row = relationship(clean_series(raw.get("rows"), complete_end), series.get(factor, []), SPECS[factor], window=window, lag=lag, frequency=frequency)
        sectors.append({"code": key, "name": raw.get("name") or key, **{k: v for k, v in row.items() if k not in ("pairs", "stability")}})
    events = [r for r in bundle.get("events", []) if iso(r.get("date")) and r["date"] <= end]
    for key, category_key in (("fed_upper", "fed"), ("lpr1y", "monetary")):
        rows = series.get(key, [])
        for previous, current in zip(rows, rows[1:]):
            delta = current["value"] - previous["value"]
            if abs(delta) > 1e-8:
                events.append({"id": f"{key}:{current['date']}", "date": current["date"], "category": category_key,
                               "title": f"{SPECS[key]['name']}{'上调' if delta > 0 else '下调'}{abs(delta) * 100:g}bp",
                               "summary": "公开利率序列的生效/报价日变动，非精确政策公告时刻。", "date_kind": "利率生效/报价日", "url": SPECS[key]["url"], "source": SPECS[key]["source"]})
    unique = {r.get("id") or r.get("url") or r["date"] + r.get("title", ""): r for r in events}
    metrics = latest_metrics(series, end)
    lookup = {r["key"]: r for r in metrics}
    gold_links = []
    for key in ("real10y", "usd", "usdcny"):
        row = relationship(clean_series(series.get("gold", []), complete_end), series.get(key, []), SPECS[key], window=window, lag=lag, frequency=frequency)
        gold_links.append({"key": key, "name": SPECS[key]["name"], **{k: v for k, v in row.items() if k not in ("pairs", "stability")}})
    fed_rows = series.get("fed_upper", [])
    fed_changes = [{"date": b["date"], "bp": (b["value"] - a["value"]) * 100} for a, b in zip(fed_rows, fed_rows[1:]) if abs(b["value"] - a["value"]) > 1e-8]
    notes = []
    if fed_changes:
        change = fed_changes[-1]
        notes.append(f"联邦基金目标上限最近一次变化：{change['date']}{'上调' if change['bp'] > 0 else '下调'}{abs(change['bp']):g}bp。该变化为生效日记录；需结合实际利率、汇率与增长环境，不能单独认定利好或利空。")
    if chosen["rho"] is not None:
        notes.append(f"{INDEXES[equity][1]}与{SPECS[factor]['name']}变化量的样本相关为{chosen['rho']:.2f}，有效观测{chosen['n']}个；仅描述所选窗口，不能据此认定因果。")
    else:
        notes.append(f"所选联动暂不可判定：{chosen['status']}。缺失不填零，也不用价格水平相关代替收益相关。")
    if lookup["pmi"]["value"] is not None:
        notes.append(f"制造业PMI统计期{lookup['pmi']['period'] or lookup['pmi']['date']}为{lookup['pmi']['value']:g}，{'高于' if lookup['pmi']['value'] > 50 else '低于' if lookup['pmi']['value'] < 50 else '等于'}50；需结合价格与信用验证需求环境。")
    if lookup["spending"]["value"] is not None:
        notes.append(f"财政一般预算累计支出同比{lookup['spending']['value']:g}%，统计期{lookup['spending']['period']}；累计同比不等于当月同比。新增债与再融资分列，不能将全部发行额视作新增刺激。")
    return {"as_of": end, "equity": equity, "equity_name": INDEXES[equity][1], "factor": factor, "factor_name": SPECS[factor]["name"],
            "frequency": frequency, "window": window, "lag": lag, "metrics": metrics, "relationship": chosen, "correlations": overview,
            "gold_links": gold_links,
            "sector_sensitivity": sorted(sectors, key=lambda r: abs(r["rho"]) if r["rho"] is not None else -1, reverse=True),
            "events": event_study(list(unique.values()), all_prices, category=category, keyword=keyword),
            "valuation": valuation_decomposition(all_prices, (bundle.get("valuations") or {}).get(equity, [])),
            "scenario": scenario(earnings, pe, discount_bp, duration), "transmissions": TRANSMISSIONS,
            "chart": {"asset": (monthly(prices) if frequency == "monthly" else prices)[-window - 1:],
                      "factor": (monthly(series.get(factor, [])) if frequency == "monthly" else series.get(factor, []))[-window * 2 - 40:]},
            "fiscal": bundle.get("fiscal", []), "debt": bundle.get("debt", []), "calendar": bundle.get("calendar", []),
            "insights": notes, "sources": bundle.get("sources", []), "errors": bundle.get("errors", []), "updated_at": bundle.get("updated_at"),
            "alignment_note": "日频：美国观测仅匹配严格早于A股日期的数值；额外领先期按A股交易日计算。重复沿用同一因子观测日不产生新样本；超过10天未更新（日频LPR允许45天）不匹配。月频：按完整统计月份描述关系，不冒充宏观数据公布前即可交易的回测。",
            "limits": ["统计相关不等于因果，政策也可能是对市场与经济状况的回应。", "FRED与国内宏观历史可能修订；无完整实时发布版本库，不能用于无前视偏差的交易回测。", "未接入可验证的联邦基金期货预期与概率，不把当前利率或会议日历当作隐含降息预期。", "M1统计口径自2025年调整，跨口径比较需谨慎；国内金价为人民币/克，含汇率和境内溢价，非美元现货金价。"]}
