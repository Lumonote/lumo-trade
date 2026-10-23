"""板块资金持续性研究。只使用完整交易日窗口，分数为描述刻度而非概率。"""
from __future__ import annotations

from collections import defaultdict
from statistics import median

from analysis.market_health import number
from analysis.market_health_fusion import iso


def normalize(rows):
    result = {}
    for raw in rows:
        kind = raw.get("content_type")
        code = str(raw.get("ts_code") or "").split(".")[0]
        date = iso(raw.get("trade_date"))
        if kind not in ("行业", "概念") or not code.startswith("BK") or not date:
            continue
        row = {"date":date,"code":code,"name":raw.get("name") or code,"type":kind,
               "leader":raw.get("buy_sm_amount_stock")}
        for key, field in (("net","net_amount"),("rate","net_amount_rate"),("pct","pct_change"),
                           ("close","close"),("elg","buy_elg_amount"),("lg","buy_lg_amount"),
                           ("md","buy_md_amount"),("sm","buy_sm_amount")):
            row[key] = number(raw.get(field))
        if row["net"] is not None:
            result[(date,code)] = row
    return list(result.values())


def _window(history, dates, n):
    wanted = dates[-n:]
    by_date = {r["date"]:r for r in history}
    return [by_date[d] for d in wanted] if len(wanted) == n and all(d in by_date for d in wanted) else []


def _compound(rows):
    result = 1
    for row in rows:
        value = number(row.get("pct"))
        if value is None:
            return None
        result *= 1+value/100
    return (result-1)*100 if rows else None


def _rank(rows, key, target):
    valid = sorted((r for r in rows if number(r.get(key)) is not None),key=lambda r:r[key],reverse=True)
    previous, rank = None, 0
    for i, row in enumerate(valid,1):
        # Ties receive the same rank instead of creating arbitrary leadership.
        if previous is None or row[key] != previous:rank = i
        previous = row[key]
        row[target] = rank
        row[target+"_pct"] = 100*(len(valid)-rank)/max(1,len(valid)-1)


def build_report(rows, calendar, benchmark):
    """rows 金额已经是元；benchmark 为 {ISO日:涨跌幅百分数}。"""
    dedup = {(r["date"],r["code"]):dict(r) for r in rows}
    if not dedup:
        return {"as_of":None,"rows":[],"rotation":[],"calendar":calendar}
    latest = max(r["date"] for r in dedup.values())
    dates = sorted({iso(d) for d in calendar if iso(d) and iso(d) <= latest})
    if not dates or dates[-1] != latest:
        dates = []
    by_day = defaultdict(list)
    by_code = defaultdict(list)
    for row in dedup.values():
        by_day[(row["date"],row["type"])].append(row)
        by_code[row["code"]].append(row)
    for group in by_day.values():
        _rank(group,"net","rank")
        _rank(group,"rate","rate_rank")
        for row in group:
            row["universe"] = len(group)
    output = []
    for history in by_code.values():
        history.sort(key=lambda r:r["date"])
        if history[-1]["date"] != latest:
            continue
        item = {**history[-1],"days":len(history),"history":history,"risks":[]}
        for n in (3,5,10,20):
            window = _window(history,dates,n)
            item[f"flow{n}"] = sum(r["net"] for r in window) if window else None
            item[f"positive{n}"] = sum(r["net"] > 0 for r in window) if window else None
            item[f"ret{n}"] = _compound(window)
            market = _compound([{"pct":benchmark[d]} for d in dates[-n:]]) if window and all(d in benchmark for d in dates[-n:]) else None
            # Relative wealth return, not the sum of daily percentage point differences.
            item[f"excess{n}"] = ((1+item[f"ret{n}"]/100)/(1+market/100)-1)*100 if market is not None and item[f"ret{n}"] is not None and market > -100 else None
        streak = 0
        lookup = {r["date"]:r for r in history}
        for date in reversed(dates):
            if date not in lookup or lookup[date]["net"] <= 0:
                break
            streak += 1
        item["streak"] = streak
        five, ten = (_window(history,dates,n) for n in (5,10))
        gross5 = sum(abs(r["net"]) for r in five)
        item["retention5"] = max(0,item["flow5"])/gross5*100 if gross5 else 0 if five else None
        item["top20_days5"] = sum(r["net"] > 0 and r["rank"] <= max(1,r["universe"]*.2) for r in five) if five else None
        item["rank_change5"] = five[0]["rank"]-item["rank"] if five else None
        item["acceleration"] = item["flow5"]-sum(r["net"] for r in ten[:5]) if ten else None
        window21 = _window(history,dates,21)
        prior = window21[:-1] if window21 else []
        base = median([r["net"] for r in prior]) if prior else None
        mad = median([abs(r["net"]-base) for r in prior]) if prior else None
        item["flow_z"] = (item["net"]-base)/(1.4826*mad) if mad else None
        item["persistence"] = round(item["positive5"]/5*40 + item["retention5"]*.2 + item["top20_days5"]/5*20 + (20 if item["excess5"] > 0 else 0),1) if five and item["excess5"] is not None else None
        strength = (item["rank_pct"]+item["rate_rank_pct"])/2 if item["net"] > 0 and item.get("rate_rank_pct") is not None else 0 if item["net"] <= 0 else None
        item["strength"] = round(strength,1) if strength is not None else None
        item["score"] = round(strength*.5+item["persistence"]*.5,1) if item["persistence"] is not None and strength is not None else None
        if item["net"] <= 0:
            item["phase"] = "承接转弱" if (item["flow5"] or 0)>0 else "资金撤退"
        elif item["flow5"] is None:
            item["phase"] = "历史窗口不足"
        elif item["flow5"] <= 0:
            item["phase"] = "单日修复"
        elif item["excess5"] is None or item["ret5"] is None:
            item["phase"] = "价格证据不足"
        elif item["positive5"] >= 4 and (item["excess5"] or 0)>0:
            item["phase"] = "持续共振"
        elif (item["ret5"] or 0) <= 1:
            item["phase"] = "资金先行观察"
        else:
            item["phase"] = "流入扩散观察"
        if item["pct"] is not None and item["pct"] > 0 and item["net"] < 0:
            item["risks"].append("价涨资金流出，价格与资金背离")
        if item["net"] > 0 and item["flow5"] is not None and item["flow5"] <= 0:
            item["risks"].append("当日流入尚未扭转五日净流出")
        if item["acceleration"] is not None and item["acceleration"] < 0:
            item["risks"].append("最近五日净额低于此前五日，承接减速")
        if five and item["flow5"] > 0 and item["net"]/sum(max(0,r["net"]) for r in five) > .65:
            item["risks"].append("近五日正流入超过65%集中在今天，需后续交易日验证")
        if (item["ret20"] or 0) > 20:
            item["risks"].append("二十日涨幅超过20%，观察拥挤与获利回吐")
        item["missing_windows"] = [n for n in (3,5,10,20) if item[f"flow{n}"] is None]
        output.append(item)
    output.sort(key=lambda r:r["net"],reverse=True)
    rotation = []
    for date in dates[-10:]:
        for kind in ("行业","概念"):
            group = sorted(by_day.get((date,kind),[]),key=lambda r:r["net"],reverse=True)
            rotation.append({"date":date,"type":kind,"leaders":[{"code":r["code"],"name":r["name"],"net":r["net"]} for r in group[:5] if r["net"]>0]})
    return {"as_of":latest,"rows":output,"rotation":rotation,"calendar":dates,
            "stale_boards":sum(h[-1]["date"] != latest for h in by_code.values())}


def analyze_members(members, flows, date, member_date):
    unique = {str(r.get("con_code") or "").split(".")[0]:r for r in members}
    by_code = {str(r.get("ts_code") or "").split(".")[0]:r for r in flows if iso(r.get("trade_date")) == date}
    stocks = []
    for code, member in unique.items():
        raw = by_code.get(code)
        if not raw or not code:
            continue
        net = number(raw.get("net_amount"))
        stocks.append({"code":code,"name":member.get("name") or raw.get("name"),"net":net*10000 if net is not None else None,
                       "pct":number(raw.get("pct_change")),"rate":number(raw.get("net_amount_rate")),"date":date})
    stocks.sort(key=lambda r:r["net"] if r["net"] is not None else -float("inf"),reverse=True)
    valid = [r for r in stocks if r["net"] is not None]
    positive = [r for r in valid if r["net"]>0]
    priced = [r for r in stocks if r["pct"] is not None]
    inflow = sum(r["net"] for r in positive)
    coverage = len(valid)/len(unique)*100 if unique else 0
    risks = []
    top3 = sum(r["net"] for r in positive[:3])/inflow*100 if inflow else None
    breadth = len(positive)/len(valid)*100 if valid else None
    if coverage<90:risks.append("成分股资金覆盖低于90%，扩散与集中度结论仅供样本观察")
    if member_date != date:risks.append("成分日期与资金日期不一致，不能视为同日完整板块")
    if len(valid)>=10 and top3 is not None and top3>60:risks.append("前三只占正流入超过60%，龙头集中承接")
    if breadth is not None and breadth<40:risks.append("不足四成有效成分净流入，资金扩散较弱")
    return {"as_of":date,"member_as_of":member_date,"members":len(unique),"covered":len(valid),"coverage":coverage,
            "flow_breadth":breadth,"price_breadth":sum(r["pct"]>0 for r in priced)/len(priced)*100 if priced else None,
            "median_change":median(r["pct"] for r in priced) if priced else None,"top3_share":top3,
            "hhi":sum((r["net"]/inflow)**2 for r in positive) if inflow else None,
            "member_net":sum(r["net"] for r in valid) if valid else None,"stocks":stocks,"risks":risks,
            "source":"TuShare dc_member 当日成分 + moneyflow_dc 同日个股资金（万元转元）"}
