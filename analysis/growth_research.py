"""全市场成长研究的确定性计算。财报、预测和技术证据分别保留口径。"""
from __future__ import annotations

from collections import Counter, defaultdict
import math
import re
from statistics import mean, median
from typing import Any

VERSION = 1
GROUPS = {
    "A": "实际与预期双高 · 低位/支撑", "B": "实际高增长 · 低位/支撑",
    "C": "高盈利预期 · 低位/支撑", "D": "成长候选 · 等待价格回落",
    "E": "其他行业成长 · 低位/支撑", "F": "其他成长候选",
    "G": "扭亏观察", "other": "未命中或数据不足",
}
INDUSTRY_THEMES = {
    "半导体": "半导体与国产替代", "电子化学品Ⅱ": "半导体与国产替代",
    "元件": "AI硬件与电子元件", "通信设备": "算力与通信设备", "计算机设备": "算力与通信设备",
    "软件开发": "软件与AI应用", "IT服务Ⅱ": "软件与AI应用", "自动化设备": "机器人与工业自动化",
    "电机Ⅱ": "机器人与工业自动化", "电网设备": "电网与电力装备", "其他电源设备Ⅱ": "电网与电力装备",
    "电池": "新能源与储能", "光伏设备": "新能源与储能", "风电设备": "新能源与储能",
    "金属新材料": "先进材料", "非金属材料Ⅱ": "先进材料", "航空装备Ⅱ": "航空航天与高端装备",
    "航天装备Ⅱ": "航空航天与高端装备", "军工电子Ⅱ": "航空航天与高端装备", "轨交设备Ⅱ": "航空航天与高端装备",
    "消费电子": "智能终端与消费电子", "光学光电子": "智能终端与消费电子",
    "生物制品": "创新药与生物科技", "医疗器械": "高端医疗器械",
}
CONCEPT_THEMES = {
    "半导体与国产替代": ("半导体", "存储芯片", "光刻", "先进封装", "第三代半导体", "Chiplet", "HBM"),
    "算力与通信设备": ("CPO", "光模块", "液冷服务器", "算力", "数据中心", "6G概念"),
    "机器人与工业自动化": ("人形机器人", "机器人概念", "减速器", "工业母机", "具身智能", "智能制造"),
    "创新药与生物科技": ("创新药", "细胞免疫治疗", "基因测序", "合成生物", "生物制造", "脑机接口"),
    "先进材料": ("新材料", "碳纤维", "PEEK材料", "稀土永磁"),
    "智能汽车": ("汽车芯片", "无人驾驶", "激光雷达", "车路云", "智能驾驶"),
    "航空航天与高端装备": ("商业航天", "低空经济", "大飞机", "卫星互联网"),
    "新能源与储能": ("固态电池", "钠离子电池", "储能", "氢能源", "可控核聚变"),
    "软件与AI应用": ("人工智能", "AI应用", "国产软件", "工业软件", "信创", "量子科技"),
}
CRITERIA = [
    "实际增长：营收同比≥20%，归母同比≥30%，归母≥0.5亿元，扣非EPS>0，去年同期归母>0。",
    "盈利预期：近6月评级机构≥3，下一年EPS增长≥25%，第三年增长≥20%，未来两年EPS CAGR≥25%，三年EPS均为正。",
    "阶段低位：120个观察日价格区间位置≤25%，距区间最高价回撤≥25%。不代表底部已确认。",
    "参考支撑：60日重复局部低点（间隔≥5日、价差≤2%、其后反弹≥3%）上方0—5%，或向上MA60上方0—3%。",
    "机构数是近6月评级机构数，不是各预测年度的EPS贡献机构数。EPS增长不等同于公司总净利润增长。",
    "行业为新质方向代理，概念命中需核实主营；负基数扭亏另列。收盘价推算只能初筛，支撑可能失效。",
]


def number(value: Any) -> float | None:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def code6(value: Any) -> str:
    value = re.sub(r"^(?:SH|SZ|BJ)", "", str(value or "").upper()).split(".")[0]
    return value if re.fullmatch(r"(?:00|30|60|68|43|83|87|88|92)\d{4}", value) else ""


def theme_evidence(industry: str, concepts: str) -> tuple[list[str], list[str], str]:
    themes, evidence = [], []
    if industry in INDUSTRY_THEMES:
        themes.append(INDUSTRY_THEMES[industry]); evidence.append("行业：" + industry)
    for theme, words in CONCEPT_THEMES.items():
        hits = [word for word in words if word in concepts]
        if hits:
            if theme not in themes:
                themes.append(theme)
            evidence.extend("概念：" + hit for hit in hits)
    return themes, evidence, "industry" if industry in INDUSTRY_THEMES else "concept" if themes else "none"


def technical_position(history: list[dict], price: float, as_of: str, bars: list[dict] | None = None) -> dict:
    """只使用截至基准日的数据；过期或价格不符的复权K线不升级证据。"""
    rows = sorted((r for r in history if r.get("date", "") <= as_of and (number(r.get("close")) or 0) > 0), key=lambda r: r["date"])[-140:]
    valid_bars = sorted((r for r in bars or [] if r.get("date", "") <= as_of and (number(r.get("close")) or 0)>0), key=lambda r:r["date"])[-140:]
    verified = bool(valid_bars and valid_bars[-1]["date"] == as_of
                    and abs(valid_bars[-1]["close"] / price - 1) <= .01
                    and all(number(r.get("high")) is not None and number(r.get("low")) is not None
                            and 0<r["low"]<=r["close"]<=r["high"] for r in valid_bars))
    if verified:
        scale = price / valid_bars[-1]["close"]
        rows = [{**r, **{k:r[k]*scale for k in ("close", "high", "low")}} for r in valid_bars]
        closes = [r["close"] for r in rows]
    else:
        closes = [r["close"] for r in rows]
        if rows:
            closes[-1] = rows[-1]["close"]
            for i in range(len(rows)-2,-1,-1):
                change = number(rows[i+1].get("pct_change"))
                closes[i] = closes[i+1] / (1+change/100) if change is not None and change>-100 else closes[i+1]*rows[i]["close"]/rows[i+1]["close"]
    out = {"history_days":len(rows), "history_date":rows[-1]["date"] if rows else None,
           "technical_basis":"qfq" if verified else "close", "bottom":False,"support":None,
           "near_position":False,"position_status":"历史不足60日", "position120":None,
           "drawdown120":None,"return5d":None,"invalidation_reference":None,"support_distance":None}
    if len(rows)<60 or rows[-1]["date"] != as_of:
        if rows and rows[-1]["date"] != as_of:
            out["position_status"] = "价格历史滞后"
        return out
    lows = [r["low"] for r in rows] if verified else closes
    highs = [r["high"] for r in rows] if verified else closes
    low, high = min(lows[-120:]), max(highs[-120:])
    position = (price-low)/(high-low) if high>low else None
    drawdown = price/high-1
    bottom = len(rows)>=120 and position is not None and position<=.25 and drawdown<=-.25
    recent, rebound = lows[-60:], closes[-60:]
    pivots = [i for i in range(2,57) if recent[i]<=min(recent[i-2:i+3]) and max(rebound[i+1:min(i+11,60)])/recent[i]>=1.03]
    levels = []
    for i in pivots:
        touches=[]
        for j in pivots:
            if abs(recent[j]/recent[i]-1)<=.02 and (not touches or j-touches[-1]>=5):touches.append(j)
        if len(touches)>=2:
            level=median(recent[j] for j in touches)
            if 0<=price/level-1<=.05:levels.append((level,len(touches)))
    ma60=mean(closes[-60:])
    ma_support=len(rows)>=65 and ma60>mean(closes[-65:-5]) and 0<=price/ma60-1<=.03
    level,touches=max(levels,default=(None,0),key=lambda x:x[0] or 0)
    kind="多次前低" if level else "MA60回踩" if ma_support else ""
    level=level or (ma60 if ma_support else None)
    atr=mean(max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1])) for i in range(len(rows)-14,len(rows))) if verified else None
    invalid=level-max(level*.03,atr or 0) if level else low*.97 if bottom else None
    out.update(low120=low,high120=high,position120=position,drawdown120=drawdown,bottom=bottom,
               support=level,support_type=kind,support_touches=touches,support_distance=price/level-1 if level else None,
               invalidation_reference=invalid,atr14=atr,ma60=ma60,ma20=mean(closes[-20:]),
               resistance20=max(highs[-20:]),return5d=price/closes[-6]-1,
               near_position=bool(bottom or level),position_status="低位且近支撑" if bottom and level else "阶段低位" if bottom else "接近参考支撑" if level else "未到低位/支撑")
    return out


def build_snapshot(market: list[dict], histories: dict, financials: dict, qfq: dict,
                   concepts: dict, as_of: str, generated_at: str) -> dict:
    reports = financials.get("reports") or {}
    # 使用覆盖较完整的最新报告期，避免16家早披露季报替换数千家中报。
    usable = [(d,rows) for d,rows in reports.items() if d<=as_of and len(rows)>=min(2000,max(1,len(market)*.5))]
    report_date, actual = max(usable,default=("",{}),key=lambda x:x[0])
    prior_date = str(int(report_date[:4])-1)+report_date[4:] if report_date else ""
    prior = reports.get(prior_date,{})
    q1 = reports.get(report_date[:4]+"-03-31",{}) if report_date and not report_date.endswith("03-31") else {}
    forecasts = financials.get("forecasts") or {}
    year_sets=Counter(tuple(sorted(int(r[f"YEAR{i}"]) for i in range(1,5) if r.get(f"YEAR_MARK{i}")=="E" and r.get(f"YEAR{i}") and int(r[f"YEAR{i}"])>=int(generated_at[:4]))) for r in forecasts.values())
    years=list(year_sets.most_common(1)[0][0][:3]) if year_sets else []
    stocks=[]
    for raw in market:
        code=code6(raw.get("ts_code") or raw.get("code"));price=number(raw.get("close") or raw.get("price"))
        if not code or price is None or price<=0:continue
        a,p,f=actual.get(code,{}),prior.get(code,{}),forecasts.get(code,{})
        industry=a.get("PUBLISHNAME") or f.get("INDUSTRY_BOARD") or concepts.get(code,{}).get("industry") or "未分类"
        tags=f.get("CONCEPTINDEX_BOARD") or concepts.get(code,{}).get("concepts","")
        themes,evidence,strength=theme_evidence(industry,tags)
        eps={int(f[f"YEAR{i}"]):number(f.get(f"EPS{i}")) for i in range(1,5) if f.get(f"YEAR{i}")}
        base,nxt,last=[eps.get(y) for y in years] if len(years)==3 else [None]*3
        growth_next=nxt/base-1 if base is not None and base>0 and nxt is not None else None
        growth_last=last/nxt-1 if nxt is not None and nxt>0 and last is not None else None
        cagr=math.sqrt(last/base)-1 if base is not None and last is not None and min(base,last)>0 else None
        profit,prior_profit,rev_growth,profit_growth,ded,aeps,cf=[number(r.get(k)) for r,k in [(a,"PARENT_NETPROFIT"),(p,"PARENT_NETPROFIT"),(a,"YSTZ"),(a,"SJLTZ"),(a,"DEDUCT_BASIC_EPS"),(a,"BASIC_EPS"),(a,"MGJYXJJE")]]
        orgs=int(number(f.get("RATING_ORG_NUM")) or 0)
        high_actual=all(v is not None for v in (profit,prior_profit,rev_growth,profit_growth,ded)) and profit>=5e7 and prior_profit>0 and rev_growth>=20 and profit_growth>=30 and ded>0
        high_future=orgs>=3 and all(v is not None for v in (growth_next,growth_last,cagr)) and growth_next>=.25 and growth_last>=.2 and cagr>=.25 and last>0
        turnaround=profit is not None and prior_profit is not None and profit>0 and prior_profit<0
        future_turnaround=orgs>=3 and base is not None and last is not None and base<=0 and last>0
        tech=technical_position(histories.get(code,[]),price,as_of,qfq.get(code,[]))
        name=str(raw.get("name") or a.get("SECURITY_NAME_ABBR") or f.get("SECURITY_NAME_ABBR") or code)
        warning=bool(re.search("ST|PT|退",name,re.I));near=tech["near_position"]
        group="other"
        if not warning and (high_actual or high_future):
            group=("A" if high_actual and high_future else "B" if high_actual else "C") if themes and near else "D" if themes else "E" if near else "F"
        elif not warning and themes and (turnaround or future_turnaround):group="G"
        pe=price/base if base is not None and base>0 else None
        risks=[]
        for condition,label in [
            (warning,"风险警示/退市名称"),(not a,"缺少可比报告"),(a and prior_profit is None,"缺少去年同期利润"),
            (profit is not None and profit<0,"报告期亏损"),(profit is not None and 0<profit<5e7,"利润低于0.5亿元"),
            (cf is not None and cf<=0,"经营现金流非正"),(aeps is not None and aeps>0 and ded is not None and ded/aeps<.8,"扣非EPS占比低于80%"),
            (not f,"无盈利预测覆盖"),(f and orgs<3,"评级机构不足3家"),(turnaround,"实际扭亏，负基数不可比"),
            (future_turnaround,"预期扭亏，不计算正常CAGR"),(pe is not None and pe>60,"预期PE高于60倍"),
            (tech.get("return5d") is not None and tech["return5d"]<-.03,"近5日跌幅超过3%"),
            (strength=="concept","概念命中待核主营"),(not tech["history_date"] or tech["history_date"]<as_of,"价格历史不足或滞后")]:
            if condition:risks.append(label)
        q=q1.get(code,{})
        sustained=high_actual and (number(q.get("YSTZ")) or 0)>=10 and (number(q.get("SJLTZ")) or 0)>=20
        stocks.append({"code":code,"name":name,"industry":industry,"themes":themes,"theme_evidence":evidence,"theme_strength":strength,
                       "price":price,"price_date":as_of,"report_date":report_date or None,"notice_date":(a.get("NOTICE_DATE") or "")[:10] or None,
                       "revenue_yi":number(a.get("TOTAL_OPERATE_INCOME"))/1e8 if number(a.get("TOTAL_OPERATE_INCOME")) is not None else None,
                       "profit_yi":profit/1e8 if profit is not None else None,"prior_profit_yi":prior_profit/1e8 if prior_profit is not None else None,
                       "revenue_growth":rev_growth/100 if rev_growth is not None else None,"profit_growth":profit_growth/100 if profit_growth is not None else None,
                       "actual_eps":aeps,"deduct_eps":ded,"operating_cf_ps":cf,"eps_base":base,"eps_next":nxt,"eps_last":last,
                       "growth_next":growth_next,"growth_last":growth_last,"cagr":cagr,"forecast_orgs":orgs,"pe":pe,
                       "high_actual":bool(high_actual),"high_future":bool(high_future),"sustained_growth":bool(sustained),
                       "actual_turnaround":turnaround,"future_turnaround":future_turnaround,"risk_warning":warning,
                       "group":group,"group_label":GROUPS[group],"risks":risks,**tech})
    stocks.sort(key=lambda r:(r["group"],not r["sustained_growth"],-r["forecast_orgs"],r["code"]))
    sectors=[];grouped=defaultdict(list)
    for r in stocks:grouped[r["industry"]].append(r)
    for industry,rows in grouped.items():
        comparable=[r for r in rows if r["forecast_orgs"]>=3 and all(r.get(k) is not None and r[k]>0 for k in ("eps_base","eps_next","eps_last"))]
        sectors.append({"industry":industry,"stocks":len(rows),"forecast_covered":len(comparable),
                        "actual_high":sum(r["high_actual"] for r in rows),"future_high":sum(r["high_future"] for r in rows),
                        "growth_next":median(r["growth_next"] for r in comparable) if comparable else None,
                        "cagr":median(r["cagr"] for r in comparable) if comparable else None,
                        "pe":median(r["pe"] for r in comparable) if comparable else None,
                        "near_growth":sum(r["group"] in ("A","B","C","E") for r in rows),
                        "examples":[{"code":r["code"],"name":r["name"]} for r in rows if r["high_future"] and not r["risk_warning"]][:8]})
    sectors.sort(key=lambda r:(r["forecast_covered"]<5,-(r["cagr"] if r["cagr"] is not None else -99)))
    counts=Counter(r["group"] for r in stocks);near=[r for r in stocks if r["group"] in ("A","B","C")]
    notes=[]
    if not usable:notes.append("尚无覆盖较完整的财报。点击“更新财报与预期”，补齐后重算。")
    if len(forecasts)<len(stocks)*.5:notes.append("盈利预测覆盖不足全市场一半；未覆盖公司不能视为没有增长。")
    if len(near)>sum(r["technical_basis"]=="qfq" for r in near):notes.append("部分价格位置来自收盘初筛，可用“校验候选支撑”补齐前复权证据。")
    return {"version":VERSION,"as_of":as_of,"generated_at":generated_at,"report_date":report_date or None,
            "financial_updated_at":financials.get("report_updated_at",{}).get(report_date,financials.get("updated_at")),
            "forecasts_updated_at":financials.get("forecasts_updated_at",financials.get("updated_at")),
            "forecast_years":years,"stocks":stocks,"sectors":sectors,
            "summary":{"total":len(stocks),"financial_covered":sum(r["profit_yi"] is not None for r in stocks),
                       "forecast_covered":sum(r["eps_base"] is not None for r in stocks),"near_growth":len(near),
                       "actual_high":sum(r["high_actual"] for r in stocks),"future_high":sum(r["high_future"] for r in stocks),
                       "verified_near":sum(r["technical_basis"]=="qfq" for r in near),"groups":{k:counts[k] for k in GROUPS}},
            "criteria":CRITERIA,"groups":GROUPS,"notes":notes}
