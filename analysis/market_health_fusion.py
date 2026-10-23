"""现货、期指、两融、量化与资金的独立证据融合，不把缺失补成中性。"""
from __future__ import annotations

import datetime as dt
from statistics import mean

from analysis.market_health import number

WEIGHTS = {"trend": .25, "breadth": .15, "valuation": .10,
           "futures": .15, "leverage": .10, "quant": .10, "capital": .15}
LABELS = {"trend":"指数趋势", "breadth":"市场广度", "valuation":"估值位置",
          "futures":"期指对冲", "leverage":"融资融券", "quant":"量化交易压力", "capital":"主力资金"}


def iso(value):
    s = str(value or "")[:10]
    if len(s) == 8 and s.isdigit():
        s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    try:
        return dt.date.fromisoformat(s).isoformat()
    except ValueError:
        return None


def freshness(date, as_of, calendar, allow_lag=False):
    date = iso(date)
    if not date or not as_of:
        return "unavailable", 0
    if date == as_of:
        return "current", 1
    previous = [d for d in calendar if d < as_of]
    if allow_lag and previous and date == previous[-1]:
        return "lagged", .5
    return "stale", 0


def clip(v):
    return max(0, min(100, v))


def dimension(key, value, date, as_of, calendar, evidence, *, source, lag=False, details=None, quality=1):
    status, freshness_weight = freshness(date, as_of, calendar, lag)
    valid = value is not None and freshness_weight > 0
    if value is None and status in ("current", "lagged"):
        status = "unavailable"
    return {"key":key, "label":LABELS[key], "score":round(clip(value),1) if valid else None,
            "observed_score":round(clip(value),1) if value is not None else None,
            "as_of":iso(date), "status":status, "source":source, "evidence":evidence,
            "weight":WEIGHTS[key], "effective_weight":round(WEIGHTS[key]*freshness_weight*quality,4) if valid else 0,
            "details":details or {}}


def margin_metrics(records):
    """沪深两所当日齐全才合计；融券余量跨标的单位混合，不累加。"""
    days = {}
    for row in records or []:
        date = iso(row.get("trade_date"))
        exchange = row.get("exchange_id")
        if date and exchange in ("SSE", "SZSE"):
            days.setdefault(date, {})[exchange] = row
    result = []
    for day, exchanges in sorted(days.items()):
        if set(exchanges) != {"SSE", "SZSE"}:
            continue
        values = {}
        for field in ("rzye", "rzmre", "rzche", "rqye", "rzrqye"):
            raw = [number(r.get(field)) for r in exchanges.values()]
            values[field] = sum(raw) if all(v is not None and v >= 0 for v in raw) else None
        if values["rzye"] is None or values["rzye"] <= 0:
            continue
        values["net_buy"] = values["rzmre"]-values["rzche"] if values["rzmre"] is not None and values["rzche"] is not None else None
        result.append({"date":day, **values})
    return result


def futures_dimension(data, health, calendar):
    as_of = health["as_of"]
    products = (data.get("quotes") or {}).get("products") or []
    previous = {p.get("variety"): p for p in (data.get("previous_quotes") or {}).get("products", [])}
    basis_rows, holding_rows, factors, dates, qualities = [], [], [], [], []
    for p in products:
        main = p.get("main") or {}
        date = iso(main.get("tradedate"))
        old = (previous.get(p.get("variety")) or {}).get("main") or {}
        basis = number(main.get("verified_basis_pct"))
        delta = None
        # 同一合约、相邻收盘观测才比较，移仓不伪装成基差变化。
        old_date = iso(old.get("tradedate"))
        older = [d for d in calendar if d < as_of]
        if date == as_of and older and old_date == older[-1] and old.get("symbol") == main.get("symbol") and basis is not None and number(old.get("verified_basis_pct")) is not None:
            delta = basis-old["verified_basis_pct"]
        basis_rows.append({"variety":p.get("variety"), "contract":main.get("symbol"), "date":date,
                           "basis_pct":basis, "basis_change":delta, "previous_date":old_date,
                           "source":main.get("source") or "新浪同日行情校验",
                           "volume":main.get("volume"), "position":main.get("position")})
    for p in data.get("positions", []):
        s = (p.get("aggregate") or {}).get("summary") or {}
        date = iso(p.get("date"))
        status, reliability = freshness(date, as_of, calendar, True)
        long_, short = number(s.get("long_total")), number(s.get("short_total"))
        change = number(s.get("net_chg"))
        valid = long_ is not None and short is not None and long_+short > 0 and change is not None
        holding_rows.append({"variety":p.get("variety"), "date":date, "net":number(s.get("net")),
                             "net_change":change, "long":long_, "short":short,
                             "ls_ratio":number(s.get("ls_ratio")), "signal":s.get("signal"), "status":status})
        if valid and reliability:
            factors.append(clip(50-change/(long_+short)*5000))
            dates.append(date)
            qualities.append(reliability)
    # 至少两个品种有席位变动证据才参与，基差绝对贴水只展示。
    score = mean(factors) if len(factors) >= 2 else None
    deltas = [r["basis_change"] for r in basis_rows if r["basis_change"] is not None]
    if score is not None and len(deltas) >= 2:
        score = .75*score+.25*mean(clip(50-d*30) for d in deltas)
    date = min(dates) if dates else max((r["date"] for r in basis_rows if r["date"]), default=None)
    evidence = [f"有效前20席位变动品种 {len(factors)}/4；至少2个品种才计分。",
                "使用净持仓增量/多空持仓合计，不把绝对净空头当作裸空。",
                "基差统一为期货减现货；只用同日已核验现货。未做分红/期限调整，绝对贴水不直接计为恐慌。"]
    # 统一取最老有效日期保守打折，避免用一个新品种掩盖其他旧日期。
    return dimension("futures", score, date, as_of, calendar, evidence, source="现有股指期货服务 / CFFEX席位缓存", lag=True,
                     details={"basis":basis_rows,"positions":holding_rows,"errors":data.get("errors",[])})


def quant_eod_proxy(rows):
    """无已保存盘口事件时使用可回补的价量资金代理，明确区别于真实异动。"""
    valid = [r for r in rows if all(number(r.get(k)) is not None for k in
                                  ("pct_chg","volume_ratio","net_amount_rate","close","high","low"))
             and r["high"] > r["low"] and r["volume_ratio"] > 0]
    if len(valid) < 200:
        return None
    outflow = sum(r["net_amount_rate"] < 0 for r in valid)/len(valid)*100
    weak = sum((r["close"]-r["low"])/(r["high"]-r["low"]) <= .25 for r in valid)/len(valid)*100
    active = [r for r in valid if r["volume_ratio"] >= 1.5]
    selling = sum(r["pct_chg"] <= -2 and r["net_amount_rate"] <= -5 for r in active)
    # 放量组少于20只时不将缺少活跃样本当作低压力。
    if len(active) < 20:
        return None
    sell_ratio = selling/len(active)*100
    return {"score":round(.5*outflow+.3*sell_ratio+.2*weak,2),"sample":len(valid),
            "outflow_ratio":outflow,"weak_close_ratio":weak,"active_sample":len(active),
            "selling_count":selling,"selling_ratio":sell_ratio,
            "method":"eod_price_volume_flow",
            "formula":"净流出个股占比×50%＋放量组跌≥2%且净流出占比≥5%的比例×30%＋收盘位于日内底部25%的比例×20%"}


def build_fusion(health, cross, bars):
    as_of = health["as_of"]
    calendar = sorted({str(b.get("day") or b.get("date"))[:10] for b in bars if str(b.get("day") or b.get("date"))[:10] <= str(as_of)})
    indices = [r for r in health["indices"] if r.get("as_of") == as_of and r.get("ret20") is not None]
    trends = [clip(50-r["ret20"]*3) for r in indices]
    dims = [dimension("trend", mean(trends) if trends else None, as_of, as_of, calendar,
                      ["七指数同日20日收益：单指数压力=截断(50−收益百分点×3)。"], source="指数日线")]
    breadth = health.get("breadth", {})
    score = (100-.6*breadth["above_ma20"]-.4*breadth["above_ma250"]
             if all(number(breadth.get(k)) is not None for k in ("above_ma20","above_ma250")) else None)
    dims.append(dimension("breadth", score, breadth.get("as_of"), as_of, calendar,
                          ["压力=100−MA20广度×0.6−MA250广度×0.4。样本口径见广度页。"],source="分层广度"))
    market = health.get("market", {})
    pb = number((market.get("values") or {}).get("pb_median"))
    dims.append(dimension("valuation", (pb-1.5)/3*100 if pb is not None else None, market.get("as_of"),as_of,calendar,
                          ["正PB中位数映射估值压力；不是未来下跌概率。"],source="全A估值快照"))
    dims.append(futures_dimension(cross.get("futures", {}), health, calendar))
    leverage = cross.get("leverage", {})
    margin = [r for r in margin_metrics(leverage.get("records", [])) if r["date"] <= str(as_of)]
    last = margin[-1] if margin else {}
    change5 = ((last["rzye"]/margin[-6]["rzye"]-1)*100 if len(margin) >= 6 else None)
    net_ratio = last.get("net_buy")/last["rzye"]*100 if last.get("net_buy") is not None else None
    parts = [50-change5*8] if change5 is not None else []
    if net_ratio is not None:
        parts.append(50-net_ratio*50)
    dims.append(dimension("leverage", mean(parts) if parts else None, last.get("date"),as_of,calendar,
                          ["融资净买入=买入−偿还；余额5期变化观察加杠杆或去杠杆。只合计沪深两所完整日。",
                           "压力=净买入/余额与5期余额变化分别映射后均值；下跌与去杠杆共振时提高出清关注。",
                           "融券余额单列观察；制度、对冲用途与价格变化影响明显，不将其机械解释为看空。"],
                          source="现有TuShare客户端 / margin（沪深）",lag=True,
                          details={"series":margin[-21:],"balance_change5":change5,"net_buy_ratio":net_ratio,"error":leverage.get("error")}))
    quant = cross.get("quant", {})
    gauge = quant.get("gauge") or {}
    bull, bear = number(gauge.get("bull")), number(gauge.get("bear"))
    stocks = quant.get("stocks") or []
    smash = sum(r.get("direction") == "砸盘" for r in stocks)
    risky = sum((number(r.get("level_rank")) or 0) >= 3 for r in stocks)
    score = (bear/(bull+bear)*100 if bull is not None and bear is not None and bull+bear > 0 else None)
    if score is not None and stocks:
        score = .7*score+.3*smash/len(stocks)*100
    proxy = quant.get("proxy") if score is None else None
    if proxy:
        score = number(proxy.get("score"))
    evidence = (["未保存该日盘口事件，已从真实历史日线、量比及个股资金流回补收盘行为代理。",
                 proxy["formula"],"该代理只观察价量资金行为，不能证明交易者身份或还原盘中异动次数；以一半可靠度参与。"] if proxy else
                ["量化代理压力=空向异动占比70%＋疑似砸盘股占候选样本比30%。",
                 "高频活跃度不是涨跌方向，也不证明真实量化成交占比。无L2，所有机制判断沿用现有服务的疑似口径。"])
    dims.append(dimension("quant",score,quant.get("fallback_date") or quant.get("snapshot_date") or gauge.get("date"),as_of,calendar,
                          evidence,source="TuShare收盘行为代理（日线/量比/资金流）" if proxy else "现有量化雷达（全量按日榜单优先）",quality=.5 if proxy or len(stocks) < 200 else 1,
                          details={"bull":bull,"bear":bear,"sample":proxy["sample"] if proxy else len(stocks),"smash":smash,"risk_count":risky,"proxy":proxy,
                                   "sectors":(quant.get("sectors") or [])[:8],"error":quant.get("error")}))
    capital = cross.get("capital", {})
    flow = sorted((r for r in capital.get("records", []) if iso(r.get("trade_date")) and iso(r["trade_date"]) <= str(as_of)), key=lambda r: iso(r["trade_date"]))
    last_flow = flow[-1] if flow else {}
    rates = [number(r.get("net_amount_rate")) for r in flow[-5:]]
    rates = [r for r in rates if r is not None]
    pressure = 50-mean(rates)*3 if len(rates) >= 3 else None
    amounts = [number(r.get("net_amount")) for r in flow[-5:]]
    amounts = [r for r in amounts if r is not None]
    dims.append(dimension("capital",pressure,last_flow.get("trade_date"),as_of,calendar,
                          ["最近5期主力净流入占成交额比均值映射压力；至少3期。沿用东方财富订单分类，主力是统计代理。",
                           "使用大盘资金汇总接口（元），不累加Top榜，也不混用个股资金的万元单位。"],
                          source="现有TuShare客户端 / moneyflow_mkt_dc",lag=True,
                          details={"series":flow[-21:],"net5":sum(amounts) if len(amounts) == 5 else None,
                                   "rate_mean5":mean(rates) if len(rates) >= 3 else None,"error":capital.get("error")}))
    used = [d for d in dims if d["score"] is not None]
    cross_used = [d for d in used if d["key"] in ("futures","leverage","quant","capital")]
    coverage = sum(d["effective_weight"] for d in used)
    # 用户要求的期指、两融、量化必须实际到齐，不能只靠两个接口代替。
    sufficient = coverage >= .65 and len(used) >= 5 and {"futures","leverage","quant"}.issubset({d["key"] for d in cross_used})
    pressure = round(sum(d["score"]*d["effective_weight"] for d in used)/coverage,1) if sufficient else None
    stressed = [d["label"] for d in used if d["score"] >= 65]
    supportive = [d["label"] for d in used if d["score"] <= 35]
    missing = [d["label"] for d in dims if d["score"] is None]
    title = "综合证据未齐 · 暂不定论"
    if sufficient:
        if pressure >= 65:
            title = "多维压力共振 · 出清仍需验证"
        elif health["signals"]["top"]["score"] is not None and health["signals"]["top"]["score"] >= 40:
            title = "顶部特征增加 · 关注资金承接"
        elif health["signals"]["bottom"]["score"] is not None and health["signals"]["bottom"]["score"] >= 40 and len([d for d in cross_used if d["score"] <= 35]) >= 2:
            title = "低位承接共振 · 等待价格确认" if health["confirmation"]["bottom"]["hit"] < 3 else "低位修复共振 · 反转条件满足"
        else:
            title = "多维分歧 · 持续观察" if stressed and supportive else "压力缓和 · 观察趋势延续" if pressure < 40 else "综合状态中性 · 跟踪变化"
    reasons = [f"价格侧：{health['stage']}；温度{health['temperature']['label']}。",
               f"压力证据：{'、'.join(stressed) or '暂无强压力共振'}。",
               f"低压力证据：{'、'.join(supportive) or '暂无明显低压力共振'}。"]
    if missing:
        reasons.append(f"暂缺或过期：{'、'.join(missing)}，未补零计分。")
    conflicts = ["高压力与低压力维度并存；估值压力较低不等于资金承接，转折仍需价格验证。"] if supportive and stressed else []
    if change5 is not None and change5 >= 3:
        conflicts.append("融资余额5期增幅≥3%，新增杠杆可能放大拥挤；即使去杠杆压力低也不代表风险低。")
    return {"title":title,"pressure":pressure,"coverage":round(coverage,3),"complete":not missing,"sufficient":sufficient,
            "dimensions":dims,"reasons":reasons,"conflicts":conflicts,"missing":missing,
            "cross_count":len(cross_used),"updated_at":cross.get("updated_at"),
            "method":"七维经验压力刻度；≥65%有效权重、至少5维且期指/两融/量化都有效才输出；T−1席位/两融/资金权重减半，量化仅同日。"}
