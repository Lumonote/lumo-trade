"""短线大屏的可复算观察：异动、资金、消息与日程，保留每一项关联依据。"""
from __future__ import annotations

from collections import defaultdict
import datetime as dt
import hashlib
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from analysis.market_health import number
from data_store.tushare_client import text_field

THEMES = {
    "半导体": ("半导体", "芯片", "集成电路", "光刻", "存储", "封装"),
    "人工智能": ("人工智能", "大模型", "算力", "AI", "数据中心", "软件", "互联网"),
    "机器人": ("机器人", "人形", "具身", "自动化", "减速器"),
    "商业航天": ("航天", "航空", "卫星", "军工", "航展", "低空", "无人机"),
    "新能源": ("新能源", "光伏", "锂电", "电池", "储能", "风电", "氢能", "电力"),
    "消费": ("消费", "零售", "食品", "农业", "白酒", "家电", "进博", "旅游"),
    "医药": ("医药", "医疗", "创新药", "生物", "药品"),
    "金融": ("金融", "证券", "保险", "银行", "降息", "美联储", "货币", "利率"),
    "资源": ("有色", "稀土", "金属", "黄金", "煤炭", "石油", "化工"),
}
SIGNALS = ("拉抬", "砸盘", "涨停", "跌停", "资金流入", "消息提及")
NEWS_SOURCES = {
    "jinshi": ("金十数据", "https://www.jin10.com/"),
    "ths_news": ("同花顺", "https://news.10jqka.com.cn/"),
    "sina_news": ("新浪财经", "https://finance.sina.com.cn/"),
    "eastmoney_news": ("东方财富", "https://finance.eastmoney.com/"),
}


def safe_url(value):
    value = text_field(value)
    parsed = urlsplit(value)
    return value if parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username else ""


def clean(value, length=2000):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text_field(value))).strip()[:length]


def uid(kind, value):
    return kind + ":" + hashlib.sha256(value.encode()).hexdigest()[:14]


def themes(text):
    folded = text.casefold()
    return [name for name, keys in THEMES.items() if any(
        bool(re.search(r"\bAI\b", text, re.I)) if k == "AI" else k.casefold() in folded for k in keys)]


def announced_events(news, today):
    """仅提取带明确日期和原文链接的媒体预告，不把推测当官方日程。"""
    result = []
    for item in news:
        text = item["title"] + " " + item.get("summary", "")
        if not item.get("url") or not re.search(r"大会|会议|峰会|博览会|展会|发布会|开幕|发布日", text):
            continue
        for match in re.finditer(r"(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日", text):
            if not match[1] and not item.get("date"):
                continue
            try:
                date = dt.date(int(match[1] or item["date"][:4]), int(match[2]), int(match[3])).isoformat()
            except ValueError:
                continue
            if date < today:
                continue
            result.append({"date": date, "title": item["title"], "summary": item["summary"],
                "source": item["source"], "url": item["url"], "themes": item["themes"], "verified": False,
                "category": "媒体会议预告", "verified_at": None, "timezone": "原文未明确时区，日期待官方核验",
                "watch": "该日期从媒体原文提取，需核对主办方正式议程与时间；不能把媒体预告当成已确认事件。"})
            break
    return result


def news_rows(streams, today):
    """多源去重；只有明确发布日期的消息才进入“当日核心”。时分不冒充今日日期。"""
    dedup = {}
    for key, (source, homepage) in NEWS_SOURCES.items():
        for raw in streams.get(key) or []:
            title = clean(raw.get("title") or raw.get("content"), 500)
            if not title:
                continue
            signature = re.sub(r"[^\w\u4e00-\u9fff]", "", title).casefold()
            stamp = text_field(raw.get("published_at") or raw.get("time"))
            if re.fullmatch(r"\d{10}(\.\d+)?", stamp):
                stamp = dt.datetime.fromtimestamp(float(stamp), ZoneInfo("Asia/Shanghai")).isoformat()
            date_match = re.search(r"20\d{2}[-/.]\d{2}[-/.]\d{2}", stamp)
            date = date_match.group().replace("/", "-").replace(".", "-") if date_match else ""
            row = {"id": uid("news", signature), "title": title,
                   "summary": clean(raw.get("summary") or raw.get("content") or title, 1200),
                   "published_at": stamp, "date": date, "is_today": date == today,
                   "time_basis": "源发布时间" if date else "来源未提供完整日期，未归入当日核心",
                   "source": clean(raw.get("source") or source, 100),
                   "url": safe_url(raw.get("url")), "source_url": homepage,
                   "themes": themes(title + " " + clean(raw.get("summary"))), "sources": [source],
                   "category": "政策" if re.search(r"国务院|央行|证监会|政策|财政|监管|商务部", title) else "产业" if themes(title) else "市场",
                   "sector_ids": [], "stock_codes": [], "association_basis": "仅按名称、代码或题材关键词匹配，未认定价格异动由该消息造成"}
            if signature in dedup:
                prior = dedup[signature]
                prior["sources"] = sorted(set(prior["sources"] + [source]))
                if not prior["url"] and row["url"]:
                    prior["url"] = row["url"]
                if not prior["date"] and row["date"]:
                    prior.update({k: row[k] for k in ("date", "published_at", "is_today", "time_basis")})
            else:
                dedup[signature] = row
    return list(dedup.values())


def normalize_calendar(rows, today, horizon=45):
    current = dt.date.fromisoformat(today)
    finish = (current + dt.timedelta(days=horizon)).isoformat()
    result = {}
    for raw in rows:
        start = text_field(raw.get("date"))
        end = text_field(raw.get("end_date")) or start
        try:
            start = dt.date.fromisoformat(start).isoformat()
            end = dt.date.fromisoformat(end).isoformat()
        except ValueError:
            continue
        title = clean(raw.get("title"), 300)
        if not title or end < today or start > finish or start > end:
            continue
        key = (title, start)
        row = {**raw, "id": uid("event", title + start), "date": start, "end_date": end,
               "title": title, "days_until": max(0, (dt.date.fromisoformat(start) - current).days),
               "state": "进行中" if start <= today <= end else "即将开始",
               "url": safe_url(raw.get("url")), "themes": raw.get("themes") or themes(title),
               "sector_ids": [], "stock_codes": [], "verified": bool(raw.get("verified")) and bool(safe_url(raw.get("url"))),
               "timezone": raw.get("timezone") or "时间以来源时区为准"}
        if key not in result or row["verified"]:
            result[key] = row
    return sorted(result.values(), key=lambda r: (r["date"], not r["verified"], r["title"]))


def classify_news(item):
    """重大事实与实时线索分流：核心必须有当日时间和明确事件，不用题材匹配数冒充重要性。"""
    text = item["title"]
    domestic_policy = bool(re.search(r"国务院|央行|人民银行|证监会|上交所|深交所|北交所|发改委|工信部|商务部|财政部|国家能源局", text)
                           and re.search(r"印发|发布|通过|实施|批准|审议|出台|启动|上调|下调|降准|降息|决定|通知|征求意见", text))
    global_policy = bool(re.search(r"美联储|FOMC|欧洲央行", text) and re.search(r"宣布|决定|降息|加息|维持.*利率", text))
    direct = bool(item["stock_codes"])
    company_event = direct and bool(re.search(r"公告|披露|签订|签署|中标|订单|业绩|净利润|营收|年报|季报|重组|收购|停牌|复牌|立案|退市|减持|回购", text))
    industry_event = bool(item["themes"]) and bool(re.search(r"正式投产|获批上市|首次获批|出口管制|进口限制|重大突破|量产交付|正式商用|中标.*亿元", text))
    commentary = bool(re.search(r"传闻|网传|据传|或将|有望|预计|预期|展望|盘前|复盘|周末要闻汇总|观点|分析师|机构认为|据悉", text))
    reasons = []
    category, watch = "实时线索", "核对原文与后续正式公告，再观察相关标的的成交和资金持续性。"
    if domestic_policy or global_policy:
        category = "重大政策"
        reasons.append("明确政策主体与发布/决定动作")
        watch = "核对政策原文、实施日期、适用范围及执行细则；观察相关板块的资金与量能能否持续。"
    if company_event:
        category = "公司重大事项"
        reasons.append("原文直接提及已覆盖个股，并包含业绩、订单、交易或风险披露")
        watch = "核对交易所/公司公告、金额与收入占比、执行条件和风险条款，区分意向协议与实际兑现。"
    if industry_event:
        category = "产业实质变化"
        reasons.append("涉及已识别产业，包含获批、投产、管制或商业化等明确事实")
        watch = "核对发布主体、落地时间、实际产能或业务敞口；题材相同不代表所有公司直接受益。"
    is_core = bool(item["is_today"] and reasons and not commentary)
    if not item["is_today"]:
        exclusion = "未取得明确当日发布时间"
    elif commentary:
        exclusion = "评论、预期、汇编或未证实线索，保留在快讯流"
    elif not reasons:
        exclusion = "日常快讯或行情线索，未达到重大事实筛选条件"
    else:
        exclusion = ""
    return {"is_core": is_core, "news_tier": "核心研判" if is_core else "短线快讯", "event_type": category,
            "core_reasons": reasons if is_core else [], "core_exclusion": exclusion,
            "impact_note": "；".join(reasons) if is_core else "实时观察线索，尚不能视作已确认的重要催化",
            "watch_points": watch, "classification_basis": "规则筛选，非人工确认或投资评级；原文与公告为准"}


def sector_quote(row, members, radar_date, today):
    """不同分类不强行映射：指数报价与覆盖成分汇总分别标明口径、日期和覆盖数。"""
    quote = {"pct": row.get("pct"), "net": row.get("net"), "pct_label": "板块涨跌",
             "net_label": "板块净额", "pct_at": row.get("date"), "net_at": row.get("date"),
             "source": row.get("source"), "note": ""}
    live = row.get("live") or {}
    stamp = text_field(live.get("quote_at"))
    # 同日、同代码的源报价才能替代展示值；收盘原值仍保留在板块行中。
    if stamp[:10] == today and live.get("code") == row.get("code") and row.get("code"):
        for field in ("pct", "net"):
            value = number(live.get(field))
            if value is not None:
                quote[field], quote[field + "_at"] = value, stamp
        quote["source"] = "东方财富板块报价"
    current = [s for s in members if radar_date and s.get("date") == radar_date]
    changes = [number(s.get("change_pct")) for s in current]
    flows = [number(s.get("main_net_inflow")) for s in current]
    changes, flows = [v for v in changes if v is not None], [v for v in flows if v is not None]
    coverage = {"date": radar_date, "stocks": len(current), "pct_count": len(changes), "flow_count": len(flows)}
    if quote["pct"] is None and changes:
        quote.update(pct=sum(changes)/len(changes), pct_label="成分均涨", pct_at=radar_date)
    if quote["net"] is None and flows:
        quote.update(net=sum(flows), net_label="成分净额", net_at=radar_date)
    if quote["pct_label"] == "成分均涨" or quote["net_label"] == "成分净额":
        quote["note"] = (f"雷达行业分类；同日覆盖{len(current)}只股票，涨跌幅有效{len(changes)}只、"
                         f"资金有效{len(flows)}只。成分均涨为等权平均，成分净额为已覆盖股票主力净流入合计，非板块指数或全行业资金。")
    return {**quote, "coverage": coverage}


def build_report(sources, today, generated_at):
    radar = sources.get("radar") or {}
    sector_data = sources.get("sectors") or {}
    limits = sources.get("limits") or {}
    limit_events = [dict(r) for r in limits.get("events", []) if r.get("date") == limits.get("as_of")]
    news = news_rows(sources, today)
    calendar = normalize_calendar((sources.get("calendar") or []) + announced_events(news, today), today)
    sector_map, name_to_id, stocks = {}, {}, {}

    def add_sector(name, code="", kind="行业", **fields):
        name = clean(name, 100)
        if not name:
            return None
        ident = name_to_id.get((name, kind)) or ("sector:" + code if code else uid("sector", kind + name))
        name_to_id[(name, kind)] = ident
        row = sector_map.setdefault(ident, {"id": ident, "name": name, "code": code, "type": kind,
            "pct": None, "net": None, "activity": None, "stock_codes": [], "news_ids": [], "event_ids": [],
            "themes": themes(name), "date": sector_data.get("as_of"), "source": "板块研究快照", "evidence": []})
        row.update({k: v for k, v in fields.items() if v is not None})
        return row

    for raw in sector_data.get("rows") or []:
        add_sector(raw.get("name"), raw.get("code", ""), raw.get("type") or "行业",
                   pct=number(raw.get("pct")), net=number(raw.get("net")), flow5=number(raw.get("flow5")),
                   persistence=number(raw.get("persistence")), phase=clean(raw.get("phase")),
                   history=raw.get("history") or [], live=raw.get("live"),
                   date=raw.get("date") or sector_data.get("as_of"), source="板块深度 · 收盘资金")
    for raw in limits.get("sectors") or []:
        row = add_sector(raw.get("name"), raw.get("code", ""), raw.get("type") or "行业")
        if row:
            row["pattern"] = raw.get("pattern")
    for raw in radar.get("sectors") or []:
        row = add_sector(raw.get("industry"), activity=number(raw.get("avg_activity")), collective=clean(raw.get("collective")))
        if row:
            row["radar_date"] = radar.get("fallback_date") or radar.get("snapshot_date")
    for raw in radar.get("stocks") or []:
        code = text_field(raw.get("code"))
        if not re.fullmatch(r"\d{6}", code):
            continue
        sector = add_sector(raw.get("industry"))
        row = {**raw, "code": code, "name": clean(raw.get("name") or code, 80),
               "date": raw.get("trade_date") or radar.get("fallback_date") or radar.get("snapshot_date"),
               "sector_ids": [sector["id"]] if sector else [], "news_ids": [], "event_ids": [], "limits": [],
               "membership_basis": "量化雷达记录的行业归属", "timeline": (radar.get("changes_agg", {}).get(code) or {}).get("events", [])}
        stocks[code] = row
    limit_sector_map = {r.get("code"): r for r in limits.get("sectors", [])}
    for event in limit_events:
        code = event["code"]
        row = stocks.setdefault(code, {"code": code, "name": event.get("name") or code, "date": event["date"],
            "price": event.get("close"), "change_pct": event.get("pct"), "direction": "", "activity": None,
            "changes_total": None, "sector_ids": [], "news_ids": [], "event_ids": [], "limits": [], "timeline": [],
            "membership_basis": "涨跌停记录的事件日成分"})
        row["limits"].append(event)
        for sector_code in event.get("sectors") or []:
            meta = limit_sector_map.get(sector_code)
            if meta:
                sector = add_sector(meta.get("name"), sector_code, meta.get("type") or "行业")
                if sector and sector["id"] not in row["sector_ids"]:
                    row["sector_ids"].append(sector["id"])
        if event.get("industry"):
            sector = add_sector(event["industry"])
            if sector and sector["id"] not in row["sector_ids"]:
                row["sector_ids"].append(sector["id"])
    popularity = sources.get("popularity") or {}
    for raw in popularity.get("rows", []):
        code = clean(raw.get("code"), 40)
        rank = number(raw.get("rank"))
        if not re.fullmatch(r"\d{6}", code) or rank is None or rank < 1 or int(rank) != rank:
            continue
        row = stocks.setdefault(code, {"code": code, "name": clean(raw.get("name") or code, 80),
            "date": None, "price": None, "change_pct": None, "direction": "", "activity": None,
            "changes_total": None, "sector_ids": [], "news_ids": [], "event_ids": [], "limits": [], "timeline": [],
            "membership_basis": "人气榜未提供成分归属"})
        if row.get("hot_rank") is not None and row["hot_rank"] <= rank:
            continue
        row.update(hot_rank=int(rank), hot_fetched_at=popularity.get("fetched_at"), hot_source="东方财富股吧人气榜")
        change = number(raw.get("rank_change"))
        valid_change = change is not None and int(change) == change and rank + change >= 1
        row.update(hot_rank_change=int(change) if valid_change else None,
                   hot_rank_before=int(rank + change) if valid_change else None,
                   hot_rank_period="较昨日", on_hot_list=raw.get("on_hot_list", True))
        # 简版人气榜的占位零值不作为真实价格或涨跌幅。
        if not raw.get("rank_only"):
            row["hot_price"] = number(raw.get("latest_price"))
            row["hot_change_pct"] = number(raw.get("change_pct"))
    orders=sources.get("orders") or {}
    big_buys=orders.get("big_buys") or []
    big_sells=orders.get("big_sells") or []
    for event in big_buys+big_sells:
        code=str(event.get("code") or "")
        if not re.fullmatch(r"\d{6}",code):
            continue
        row=stocks.setdefault(code,{"code":code,"name":clean(event.get("name") or code,80),"date":event.get("date"),
            "price":None,"change_pct":None,"direction":"","activity":None,"changes_total":None,
            "sector_ids":[],"news_ids":[],"event_ids":[],"limits":[],"timeline":[],
            "membership_basis":"盘口异动源未提供行业归属"})
        row.setdefault("order_events",[]).append(event)
    for row in stocks.values():
        for ident in row["sector_ids"]:
            sector_map[ident]["stock_codes"].append(row["code"])

    for item in news + calendar:
        text = item["title"] + " " + item.get("summary", "")
        topic = set(item.get("themes") or [])
        direct = [r for r in stocks.values() if r["code"] in text or (len(r["name"]) >= 3 and r["name"] in text)]
        matched = [r for r in sector_map.values() if r["name"] in text or topic.intersection(r["themes"])]
        item["sector_ids"] = [r["id"] for r in matched]
        item["stock_codes"] = [r["code"] for r in direct]
        field = "news_ids" if item["id"].startswith("news:") else "event_ids"
        for row in matched + direct:
            row[field].append(item["id"])
    for row in sector_map.values():
        members = [stocks[c] for c in row["stock_codes"]]
        row["quote"] = sector_quote(row, members, radar.get("fallback_date") or radar.get("snapshot_date"), today)
        row["signals"] = {
            "拉抬": sum(s.get("direction") == "拉抬" for s in members),
            "砸盘": sum(s.get("direction") == "砸盘" for s in members),
            "涨停": sum(any(e.get("kind") == "U" for e in s["limits"]) for s in members),
            "跌停": sum(any(e.get("kind") == "D" for e in s["limits"]) for s in members),
            "资金流入": int(row["quote"]["net"] is not None and row["quote"]["net"] > 0),
            "消息提及": len(row["news_ids"]),
        }
        row["relevance"] = (len(members) * 2 + len(row["news_ids"]) * 3 + len(row["event_ids"]) * 2
                              + (row.get("activity") or 0) / 10)
        observation = [f"覆盖{len(members)}只相关股票，拉抬{row['signals']['拉抬']}只、砸盘{row['signals']['砸盘']}只",
                       f"收盘涨停{row['signals']['涨停']}只、跌停{row['signals']['跌停']}只（限价日{limits.get('as_of') or '未取得'}）"]
        if row.get("net") is not None:
            observation.append(f"板块资金{row.get('date') or '未知日'}净额{row['net']/1e8:.2f}亿元")
        if row["quote"]["note"]:
            observation.append(row["quote"]["note"])
        row["observation"] = "；".join(observation)
        row["verification"] = "核对异动后的成交量与资金能否持续，关注消息原文、实际订单或正式发布；板块与消息关联不等于因果或买入信号。"
    sectors = sorted(sector_map.values(), key=lambda r: (-r["relevance"], -(r.get("net") or 0), r["name"]))
    stock_rows = sorted(stocks.values(), key=lambda r: (-(r.get("activity") or 0), -len(r["limits"]), r["code"]))
    hot_stocks = sorted([r for r in stock_rows if r.get("hot_rank") and r.get("on_hot_list")], key=lambda r: r["hot_rank"])
    # 当前排名越小越靠前；榜单只使用源提供的较昨日变化，不从股价或缺席榜单推算名次。
    popularity_rises = sorted([r for r in stock_rows if (r.get("hot_rank_change") or 0)>0], key=lambda r: (r["hot_rank"], -r["hot_rank_change"]))
    limit_up = sorted([r for r in limit_events if r.get("kind") == "U"], key=lambda r: (-(r.get("streak") or 0), r.get("first_time") or "99", r["code"]))
    limit_down = sorted([r for r in limit_events if r.get("kind") == "D"], key=lambda r: (-(r.get("streak") or 0), -(r.get("fall_days") or 0), r["code"]))
    for item in news:
        item.update(classify_news(item))
        item["importance"] = (20 if item["is_today"] else 0) + len(item["sector_ids"]) + len(item["stock_codes"]) * 3 + (5 if item["category"] == "政策" else 0)
    news.sort(key=lambda r: r["published_at"], reverse=True)
    news.sort(key=lambda r: -r["importance"])
    core_news = [r for r in news if r["is_core"]]
    short_news = sorted([r for r in news if not r["is_core"]], key=lambda r: r["published_at"], reverse=True)
    graph_sectors = [r for r in sectors if r["stock_codes"]][:8]
    graph_stocks = list(dict.fromkeys(c for r in graph_sectors for c in r["stock_codes"][:4]))[:24]
    edges = [{"source": r["id"], "target": "stock:" + c, "kind": "membership", "evidence": stocks[c]["membership_basis"]}
             for r in graph_sectors for c in r["stock_codes"] if c in graph_stocks]
    summary = {"stocks": len(stock_rows), "sectors": len(sectors), "news": len(news), "core_news": len(core_news), "short_news": len(short_news),
               "future_events": len(calendar), "limit_up": sum(r.get("kind") == "U" for r in limit_events),
               "limit_down": sum(r.get("kind") == "D" for r in limit_events), "broken": sum(r.get("kind") == "Z" for r in limit_events),
               "max_streak": max((r.get("streak") or 1 for r in limit_events if r.get("kind") == "U"), default=None)}
    return {"today": today, "generated_at": generated_at, "sectors": sectors, "stocks": stock_rows,
            "news": news, "core_news": core_news, "short_news": short_news, "calendar": calendar, "limits": limit_events, "summary": summary,
            "limit_up": limit_up, "limit_down": limit_down, "hot_stocks": hot_stocks, "popularity_rises": popularity_rises,
            "big_buys":big_buys,"big_sells":big_sells,"order_events":sorted(big_buys+big_sells,key=lambda r:r.get("occurred_at", ""),reverse=True),
            "order_meta":orders.get("order_meta",{}),
            "hot_fetched_at": popularity.get("fetched_at"),
            "radar_as_of": radar.get("fallback_date") or radar.get("snapshot_date"), "limit_as_of": limits.get("as_of"),
            "sector_as_of": sector_data.get("as_of"), "signals": list(SIGNALS),
            "graph": {"sector_ids": [r["id"] for r in graph_sectors], "stock_codes": graph_stocks, "edges": edges,
                      "note": "实线为已取得的行业/历史成分归属；消息题材匹配仅是观察线索，不代表资金实际流动路径"},
            "sources": sources.get("source_status", []), "notes": sources.get("notes", []),
            "method": "异动、收盘限价、板块资金、消息与未来日程各自保留数据日期；仅按明确名称、代码或题材匹配关联，不推断消息造成涨跌。"}
