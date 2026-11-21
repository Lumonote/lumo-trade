"""股指期货服务 —— 行情(新浪) + 中金所前20席位多空持仓排名 + 持仓趋势。

数据源与套路对齐 :mod:`webui.services.star_orbit_service`（模块级函数 + TTL 缓存，
不 import ``webui.core`` 避免环）:

- 实时行情: 新浪 ``Market_Center.getHQFuturesData``(与 akshare ``futures_zh_realtime``
  同源)按品种 node 码直连拉全部挂牌合约。不走 akshare 是因为它每次都要先抓品种注册
  表 JS 再 demjson 解析,该步夜间/限流常拿到空响应报 "No value to decode";四个股指
  品种的 node 码固定(IF=qz_qh/IH=szgz_qh/IC=zzgz_qh/IM=im_qh),直连一步到位。
- 现货指数: 新浪 ``hq.sinajs.cn`` 短格式(``s_sh000300`` 等)直连,算主力合约基差/贴水率。
  与东财套路一致,httpx ``trust_env`` False→True 交替(本机 Clash 常掐外部行情源)。
- 多空持仓: akshare ``get_cffex_rank_table``(中金所官方 CSV)。每合约一张表,列含
  rank(1-20,**999=官方合计行**)、``long/short/vol`` 三组 ``*_open_interest/*_chg/*_party_name``。
  数据盘后发布,盘中查当日自动回退最近有数据的交易日(payload 标 ``fallback_from``)。
  历史日期数据不可变 → 以 ``kv_repo``(namespace ``futures_rank``)永久缓存,趋势查询逐日命中。
- Tushare 兜底(与仓库「东财→Tushare」惯例一致): 新浪行情挂 → ``fut_daily`` EOD(标
  stale);指数现货挂 → ``index_daily``;中金所 CSV 挂 → ``fut_holding`` 重建三榜。
  token 经 ``data_store.tushare_client``(服务进程内 ConfigurationService 已把用户配置
  目录写入 ``KRONOS_CONFIG_DIR``,token 从用户目录取)。
"""
from __future__ import annotations

import datetime as _dt
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout
from typing import Any, Callable, Dict, List, Optional

import httpx

PRODUCTS: List[Dict[str, str]] = [
    {"variety": "IF", "name": "沪深300", "node": "qz_qh", "index_code": "sh000300", "ts_index": "000300.SH"},
    {"variety": "IH", "name": "上证50", "node": "szgz_qh", "index_code": "sh000016", "ts_index": "000016.SH"},
    {"variety": "IC", "name": "中证500", "node": "zzgz_qh", "index_code": "sh000905", "ts_index": "000905.SH"},
    {"variety": "IM", "name": "中证1000", "node": "im_qh", "index_code": "sh000852", "ts_index": "000852.SH"},
]
VARIETIES = {p["variety"]: p for p in PRODUCTS}
# Product launch dates published by CFFEX in its PFMI disclosure.
_FIRST_TRADE = {"IF": "20100416", "IH": "20150416", "IC": "20150416", "IM": "20220722"}

_KV_NAMESPACE = "futures_rank"
_OVERVIEW_TTL = 30  # 行情缓存秒数(盘中自动刷新即 30s 一拍)
_overview_cache: Dict[str, Any] = {"ts": 0.0, "payload": None}
_overview_lock = threading.Lock()

_SINA_HQ_URL = "https://hq.sinajs.cn/list="
_SINA_NODE_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
                  "json_v2.php/Market_Center.getHQFuturesData")
_SINA_HEADERS = {
    "Referer": "https://vip.stock.finance.sina.com.cn/",
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
}


def _num(v: Any, ndigits: int = 2) -> Optional[float]:
    try:
        if v in (None, "", "-"):
            return None
        return round(float(v), ndigits)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return 0


def _get_pro():
    """Tushare pro 句柄(无 token / 未安装 → None,调用方自行降级)。"""
    try:
        from data_store import tushare_client

        return tushare_client.get_pro()
    except Exception:
        return None


def _with_deadline(seconds: float, fn: Callable, *args, **kwargs):
    """给一次外部抓取加硬预算,超时返回 None(线程留后台自生自灭)。

    httpx 的 timeout 不覆盖 DNS 解析(getaddrinfo 阻塞),akshare 内部 requests 更无
    超时;夜间代理/DNS 黑洞会让单次抓取挂几分钟,把整个 handler 拖死。这里用独立
    线程 + ``result(timeout)`` 兜住:超时立刻走降级路径,不等挂死的线程。
    """
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(fn, *args, **kwargs).result(timeout=seconds)
    except _FutureTimeout:
        return None
    except Exception:
        return None
    finally:
        pool.shutdown(wait=False)


# ----------------------------- 实时行情 + 基差 -----------------------------

def _index_spot(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """新浪短格式指数现货 ``{sh000300: {name, price, change, change_pct}}``。失败 → {}。"""
    if not codes:
        return {}
    url = _SINA_HQ_URL + ",".join(f"s_{c}" for c in codes)
    for trust_env in (False, True):
        try:
            with httpx.Client(timeout=6, follow_redirects=True, trust_env=trust_env) as client:
                resp = client.get(url, headers=_SINA_HEADERS)
                resp.raise_for_status()
                text = resp.content.decode("gbk", errors="ignore")
        except Exception:
            continue
        out: Dict[str, Dict[str, Any]] = {}
        for line in text.splitlines():
            if "=" not in line:
                continue
            head, body = line.split("=", 1)
            code = head.strip().rsplit("_", 1)[-1]
            fields = body.strip().rstrip(";").strip('"').split(",")
            if len(fields) < 4 or not fields[0]:
                continue
            out[code] = {
                "name": fields[0],
                "price": _num(fields[1]),
                "change": _num(fields[2]),
                "change_pct": _num(fields[3]),
            }
        if out:
            return out
    return {}


def _product_quotes(product: Dict[str, str]) -> Dict[str, Any]:
    """单品种全部挂牌合约实时行情(新浪 node 接口直连)。失败 → degraded。"""
    variety = product["variety"]
    params = {"page": "1", "sort": "position", "asc": "0",
              "node": product["node"], "base": "futures"}
    rows: List[Dict[str, Any]] = []
    note = "行情获取失败"
    for trust_env in (False, True):  # 先直连绕过系统代理,再走默认路由
        try:
            with httpx.Client(timeout=8, follow_redirects=True, trust_env=trust_env) as client:
                resp = client.get(_SINA_NODE_URL, params=params, headers=_SINA_HEADERS)
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list) and data:
                    rows = [r for r in data if isinstance(r, dict)]
                    break
        except Exception as exc:  # noqa: BLE001
            note = f"行情获取失败: {exc}"
            continue
    contracts: List[Dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol or symbol == f"{variety}0":  # 连续合约与主力重复,不单列
            continue
        trade = _num(row.get("trade"))
        pre = _num(row.get("presettlement")) or _num(row.get("prevsettlement"))
        change_pct = None
        if trade is not None and pre:
            change_pct = round((trade - pre) / pre * 100, 2)
        contracts.append({
            "symbol": symbol,
            "price": trade,
            "presettlement": pre,
            "change_pct": change_pct,
            "open": _num(row.get("open")),
            "high": _num(row.get("high")),
            "low": _num(row.get("low")),
            "volume": _int(row.get("volume")),
            "position": _int(row.get("position")),
            "ticktime": str(row.get("ticktime") or ""),
            "tradedate": str(row.get("tradedate") or ""),
        })
    contracts.sort(key=lambda c: c["symbol"])
    return {"variety": variety, "name": product["name"], "contracts": contracts,
            "degraded": not contracts, "note": "" if contracts else note}


_INDEX_FALLBACK_TTL = 600  # Tushare 指数 EOD 兜底缓存秒数
_index_fallback_cache: Dict[str, Any] = {"ts": 0.0, "by_code": {}}


def _tushare_index_spot(pairs: List[tuple]) -> Dict[str, Dict[str, Any]]:
    """新浪指数现货挂时的 Tushare ``index_daily`` EOD 兜底。``pairs=[(sina码, ts码)]``。"""
    now = time.time()
    cached = _index_fallback_cache["by_code"]
    if cached and (now - _index_fallback_cache["ts"]) < _INDEX_FALLBACK_TTL:
        return cached
    pro = _get_pro()
    if pro is None:
        return cached
    dates = _recent_trade_dates(5)
    out: Dict[str, Dict[str, Any]] = {}
    for sina_code, ts_code in pairs:
        try:
            df = pro.index_daily(ts_code=ts_code, start_date=dates[-1], end_date=dates[0])
        except Exception:
            continue
        if df is None or getattr(df, "empty", True):
            continue
        row = df.iloc[0]  # Tushare 按日期降序,首行即最新
        out[sina_code] = {
            "name": "", "price": _num(row.get("close")),
            "change": _num(row.get("change")), "change_pct": _num(row.get("pct_chg")),
            "stale": True, "date": _iso(str(row.get("trade_date") or "")),
        }
    if out:
        _index_fallback_cache.update(ts=now, by_code=out)
        return out
    return cached


_EOD_QUOTES_TTL = 600  # Tushare 期货 EOD 兜底缓存秒数
_eod_quotes_cache: Dict[str, Any] = {"ts": 0.0, "by_variety": {}}


def _tushare_eod_quotes() -> Dict[str, List[Dict[str, Any]]]:
    """新浪实时挂时的 Tushare ``fut_daily`` EOD 兜底 ``{品种: [合约行情]}``(标 stale)。"""
    now = time.time()
    cached = _eod_quotes_cache["by_variety"]
    if cached and (now - _eod_quotes_cache["ts"]) < _EOD_QUOTES_TTL:
        return cached
    pro = _get_pro()
    if pro is None:
        return cached
    for yyyymmdd in _recent_trade_dates(4):
        try:
            df = pro.fut_daily(trade_date=yyyymmdd, exchange="CFFEX")
        except Exception:
            continue
        if df is None or getattr(df, "empty", True):
            continue
        by_variety: Dict[str, List[Dict[str, Any]]] = {}
        for _, row in df.iterrows():
            symbol = str(row.get("ts_code") or "").split(".")[0].strip().upper()
            match = re.fullmatch(r"(IF|IH|IC|IM)\d{4}", symbol)
            if not match:  # 跳过 IF.CFX / IFL.CFX 等连续合约
                continue
            close = _num(row.get("close"))
            pre = _num(row.get("pre_settle"))
            change_pct = None
            if close is not None and pre:
                change_pct = round((close - pre) / pre * 100, 2)
            by_variety.setdefault(match.group(1), []).append({
                "symbol": symbol,
                "price": close,
                "presettlement": pre,
                "change_pct": change_pct,
                "open": _num(row.get("open")),
                "high": _num(row.get("high")),
                "low": _num(row.get("low")),
                "volume": _int(row.get("vol")),
                "position": _int(row.get("oi")),
                "ticktime": "收盘",
                "tradedate": _iso(yyyymmdd),
            })
        if by_variety:
            for contracts in by_variety.values():
                contracts.sort(key=lambda c: c["symbol"])
            _eod_quotes_cache.update(ts=now, by_variety=by_variety)
            return by_variety
    return cached


def overview(force: bool = False) -> Dict[str, Any]:
    """四大股指期货全合约行情 + 现货指数 + 基差。30s TTL,4 品种并行拉取。

    新浪实时挂掉的品种自动落 Tushare EOD 收盘兜底(``stale:true``);指数现货同理。
    """
    with _overview_lock:
        cached = _overview_cache["payload"]
        if cached and not force and (time.time() - _overview_cache["ts"]) < _OVERVIEW_TTL:
            return cached
    # 手动管理线程池: with 语句的 __exit__ 会 join 全部线程,一个挂死的抓取
    # (夜间 DNS/代理黑洞)会把整个 handler 拖过 WKWebView 的超时;这里带
    # deadline 收集结果,超时的品种直接按 degraded 走 Tushare 兜底。
    pool = ThreadPoolExecutor(max_workers=len(PRODUCTS) + 1)
    try:
        spot_future = pool.submit(_index_spot, [p["index_code"] for p in PRODUCTS])
        quote_futures = [pool.submit(_product_quotes, p) for p in PRODUCTS]
        try:
            spots = spot_future.result(timeout=10) or {}
        except Exception:
            spots = {}
        products = []
        for meta, future in zip(PRODUCTS, quote_futures):
            try:
                products.append(future.result(timeout=14))
            except Exception:
                products.append({"variety": meta["variety"], "name": meta["name"],
                                 "contracts": [], "degraded": True, "note": "行情获取超时"})
    finally:
        pool.shutdown(wait=False)
    missing_spots = [(m["index_code"], m["ts_index"]) for m in PRODUCTS
                     if not (spots.get(m["index_code"]) or {}).get("price")]
    if missing_spots:
        fallback_spots = _with_deadline(20, _tushare_index_spot, missing_spots) or {}
        spots = {**fallback_spots, **spots}
    eod_fallback: Optional[Dict[str, List[Dict[str, Any]]]] = None
    for product, meta in zip(products, PRODUCTS):
        product["stale"] = False
        if product["degraded"]:
            if eod_fallback is None:
                eod_fallback = _with_deadline(25, _tushare_eod_quotes) or {}
            contracts = eod_fallback.get(meta["variety"]) or []
            if contracts:
                product["contracts"] = [dict(c) for c in contracts]
                product["degraded"] = False
                product["stale"] = True
                product["note"] = "新浪实时不可用,已用 Tushare 收盘数据兜底"
        spot = spots.get(meta["index_code"]) or {}
        if spot and not spot.get("name"):
            spot["name"] = meta["name"]
        product["index"] = {"code": meta["index_code"], **spot} if spot.get("price") else None
        spot_price = spot.get("price")
        for contract in product["contracts"]:
            basis = basis_pct = None
            if spot_price and contract["price"] is not None:
                basis = round(contract["price"] - spot_price, 2)
                basis_pct = round(basis / spot_price * 100, 2)
            contract["basis"] = basis
            contract["basis_pct"] = basis_pct
        main = max(product["contracts"], key=lambda c: c["volume"], default=None)
        product["main"] = dict(main) if main else None
    payload = {
        "updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "live": any(not p["degraded"] and not p["stale"] for p in products),
        "products": products,
    }
    with _overview_lock:
        _overview_cache.update(ts=time.time(), payload=payload)
    return payload


# ----------------------------- 多空持仓排名 -----------------------------

def _recent_trade_dates(n: int) -> List[str]:
    """近 n 个交易日 ``YYYYMMDD``(最新在前)。Tushare 日历优先,失败回退工作日近似。"""
    try:
        from data_store import tushare_client

        dates = [d.replace("-", "") for d in tushare_client.recent_trade_dates(n)]
        if dates:
            return dates
    except Exception:
        pass
    out: List[str] = []
    day = _dt.date.today()
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.strftime("%Y%m%d"))
        day -= _dt.timedelta(days=1)
    return out


def _normalize_rank_df(df: Any) -> Dict[str, Any]:
    """中金所单合约排名表 → ``{long_rows, short_rows, vol_rows, totals}``(纯转换)。

    rank 1-20 为三个独立榜单按名次并排；rank==999 是官方合计行,缺失时按行求和兜底。
    numpy 数值全部转 Python int,避免 JSON 序列化被 ``default=str`` 变字符串。
    """
    long_rows: List[Dict[str, Any]] = []
    short_rows: List[Dict[str, Any]] = []
    vol_rows: List[Dict[str, Any]] = []
    totals = {"long": 0, "long_chg": 0, "short": 0, "short_chg": 0, "vol": 0, "vol_chg": 0}
    official_total = None
    for _, row in df.iterrows():
        rank = _int(row.get("rank"))
        entry = {
            "long_party": str(row.get("long_party_name") or "").strip(),
            "long": _int(row.get("long_open_interest")),
            "long_chg": _int(row.get("long_open_interest_chg")),
            "short_party": str(row.get("short_party_name") or "").strip(),
            "short": _int(row.get("short_open_interest")),
            "short_chg": _int(row.get("short_open_interest_chg")),
            "vol_party": str(row.get("vol_party_name") or "").strip(),
            "vol": _int(row.get("vol")),
            "vol_chg": _int(row.get("vol_chg")),
        }
        if rank == 999:
            official_total = entry
            continue
        if not 1 <= rank <= 20:
            continue
        if entry["long_party"]:
            long_rows.append({"rank": rank, "party": entry["long_party"],
                              "oi": entry["long"], "chg": entry["long_chg"]})
        if entry["short_party"]:
            short_rows.append({"rank": rank, "party": entry["short_party"],
                               "oi": entry["short"], "chg": entry["short_chg"]})
        if entry["vol_party"]:
            vol_rows.append({"rank": rank, "party": entry["vol_party"],
                             "oi": entry["vol"], "chg": entry["vol_chg"]})
    if official_total:
        totals = {"long": official_total["long"], "long_chg": official_total["long_chg"],
                  "short": official_total["short"], "short_chg": official_total["short_chg"],
                  "vol": official_total["vol"], "vol_chg": official_total["vol_chg"]}
    else:
        totals = {"long": sum(r["oi"] for r in long_rows),
                  "long_chg": sum(r["chg"] for r in long_rows),
                  "short": sum(r["oi"] for r in short_rows),
                  "short_chg": sum(r["chg"] for r in short_rows),
                  "vol": sum(r["oi"] for r in vol_rows),
                  "vol_chg": sum(r["chg"] for r in vol_rows)}
    for rows in (long_rows, short_rows, vol_rows):
        rows.sort(key=lambda r: r["rank"])
    return {"long_rows": long_rows, "short_rows": short_rows,
            "vol_rows": vol_rows, "totals": totals}


def _merge_rows(rows_lists: List[List[Dict[str, Any]]], top_n: int = 20) -> List[Dict[str, Any]]:
    """多合约同名会员求和后重排名,取前 top_n(用于「全部合约」聚合榜)。"""
    merged: Dict[str, Dict[str, Any]] = {}
    for rows in rows_lists:
        for row in rows:
            slot = merged.setdefault(row["party"], {"party": row["party"], "oi": 0, "chg": 0})
            slot["oi"] += row["oi"]
            slot["chg"] += row["chg"]
    ranked = sorted(merged.values(), key=lambda r: -r["oi"])[:top_n]
    for idx, row in enumerate(ranked, start=1):
        row["rank"] = idx
    return ranked


def _member_net_rows(long_rows: List[Dict[str, Any]],
                     short_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """多/空两榜按会员轧差 → 净持仓榜(净多在前,净空在后)。"""
    members: Dict[str, Dict[str, Any]] = {}
    for row in long_rows:
        slot = members.setdefault(row["party"], {"party": row["party"], "long": 0,
                                                 "long_chg": 0, "short": 0, "short_chg": 0})
        slot["long"] += row["oi"]
        slot["long_chg"] += row["chg"]
    for row in short_rows:
        slot = members.setdefault(row["party"], {"party": row["party"], "long": 0,
                                                 "long_chg": 0, "short": 0, "short_chg": 0})
        slot["short"] += row["oi"]
        slot["short_chg"] += row["chg"]
    rows = []
    for slot in members.values():
        slot["net"] = slot["long"] - slot["short"]
        slot["net_chg"] = slot["long_chg"] - slot["short_chg"]
        rows.append(slot)
    rows.sort(key=lambda r: -r["net"])
    return rows


def _summarize(totals: Dict[str, Any]) -> Dict[str, Any]:
    """前20合计 → 多空比/净持仓/信号。规则透明:仅看多空增减方向组合。"""
    long_total = totals.get("long") or 0
    short_total = totals.get("short") or 0
    long_chg = totals.get("long_chg") or 0
    short_chg = totals.get("short_chg") or 0
    net = long_total - short_total
    ls_ratio = round(long_total / short_total, 3) if short_total else None
    if long_chg > 0 and short_chg < 0:
        signal = {"tag": "偏多", "reason": "多单增仓、空单减仓"}
    elif long_chg < 0 and short_chg > 0:
        signal = {"tag": "偏空", "reason": "空单增仓、多单减仓"}
    elif long_chg > 0 and short_chg > 0:
        signal = {"tag": "分歧", "reason": "多空同步增仓,分歧加大,波动或放大"}
    elif long_chg < 0 and short_chg < 0:
        signal = {"tag": "降温", "reason": "多空同步减仓,资金离场观望"}
    else:
        signal = {"tag": "中性", "reason": "多空增减仓变化不明显"}
    stance = "前20席位整体净空头" if net < 0 else "前20席位整体净多头"
    return {
        "long_total": long_total, "long_chg": long_chg,
        "short_total": short_total, "short_chg": short_chg,
        "net": net, "net_chg": long_chg - short_chg,
        "ls_ratio": ls_ratio,
        "vol_total": totals.get("vol") or 0, "vol_chg": totals.get("vol_chg") or 0,
        "signal": signal["tag"],
        "signal_reason": f"{signal['reason']}；{stance}",
    }


def _build_day_payload(tables: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """各合约标准化表 → 单日完整 payload(聚合 + 分合约,均带净持仓榜与摘要)。"""
    contract_list = sorted(tables)
    by_contract: Dict[str, Any] = {}
    for contract, table in tables.items():
        by_contract[contract] = {
            **table,
            "net_rows": _member_net_rows(table["long_rows"], table["short_rows"]),
            "summary": _summarize(table["totals"]),
        }
    agg_totals = {key: sum(t["totals"].get(key) or 0 for t in tables.values())
                  for key in ("long", "long_chg", "short", "short_chg", "vol", "vol_chg")}
    agg_long = _merge_rows([t["long_rows"] for t in tables.values()])
    agg_short = _merge_rows([t["short_rows"] for t in tables.values()])
    aggregate = {
        "long_rows": agg_long,
        "short_rows": agg_short,
        "vol_rows": _merge_rows([t["vol_rows"] for t in tables.values()]),
        "net_rows": _member_net_rows(agg_long, agg_short),
        "totals": agg_totals,
        "summary": _summarize(agg_totals),
    }
    return {"contract_list": contract_list, "by_contract": by_contract, "aggregate": aggregate}


def _holding_tables(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Tushare ``fut_holding`` 记录(单品种多合约) → 与 :func:`_normalize_rank_df` 同形结构。

    fut_holding 每行=会员×合约,带 vol/long_hld/short_hld 三组值(不在某榜时为 NaN);
    没有名次列 → 按各指标降序重建前20排名;合计与 CFFEX「前20合计」同口径,由榜内求和。
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        symbol = str(record.get("symbol") or "").strip().upper()
        if symbol:
            grouped.setdefault(symbol, []).append(record)
    tables: Dict[str, Dict[str, Any]] = {}
    for symbol, rows in grouped.items():
        def ranked(field: str, chg_field: str) -> List[Dict[str, Any]]:
            candidates = []
            for row in rows:
                value = _num(row.get(field))
                if value is None or value <= 0:
                    continue
                candidates.append({"party": str(row.get("broker") or "").strip(),
                                   "oi": _int(value), "chg": _int(row.get(chg_field))})
            candidates.sort(key=lambda r: -r["oi"])
            for rank, row in enumerate(candidates[:20], start=1):
                row["rank"] = rank
            return candidates[:20]

        long_rows = ranked("long_hld", "long_chg")
        short_rows = ranked("short_hld", "short_chg")
        vol_rows = ranked("vol", "vol_chg")
        tables[symbol] = {
            "long_rows": long_rows, "short_rows": short_rows, "vol_rows": vol_rows,
            "totals": {
                "long": sum(r["oi"] for r in long_rows),
                "long_chg": sum(r["chg"] for r in long_rows),
                "short": sum(r["oi"] for r in short_rows),
                "short_chg": sum(r["chg"] for r in short_rows),
                "vol": sum(r["oi"] for r in vol_rows),
                "vol_chg": sum(r["chg"] for r in vol_rows),
            },
        }
    return tables


def _tushare_day_rank(variety: str, yyyymmdd: str) -> Optional[Dict[str, Any]]:
    """中金所 CSV 挂时的 Tushare ``fut_holding`` 兜底。无数据/无权限 → None。"""
    pro = _get_pro()
    if pro is None:
        return None
    try:
        df = pro.fut_holding(trade_date=yyyymmdd, exchange="CFFEX")
    except Exception:
        return None
    if df is None or getattr(df, "empty", True):
        return None
    records = [row for _, row in df.iterrows()
               if re.fullmatch(rf"{variety}\d{{4}}", str(row.get("symbol") or "").strip().upper())]
    if not records:
        return None
    tables = _holding_tables([dict(r) for r in records])
    if not tables:
        return None
    payload = _build_day_payload(tables)
    payload["source"] = "tushare"
    return payload


def _fetch_cffex_tables(variety: str, yyyymmdd: str):
    """中金所官方排名 CSV(akshare)。独立函数以便加 deadline(akshare 内部无超时)。"""
    import akshare as ak

    return ak.get_cffex_rank_table(date=yyyymmdd, vars_list=[variety])


def _load_day_rank(variety: str, yyyymmdd: str, force: bool = False) -> Optional[Dict[str, Any]]:
    """单品种单日排名(kv 永久缓存;中金所 CSV → Tushare fut_holding 兜底)。

    无数据(节假日/未发布) → None。
    """
    key = f"{variety}:{yyyymmdd}"
    if not force:
        try:
            from data_store import kv_repo

            hit = kv_repo.get(_KV_NAMESPACE, key)
            if hit and hit[0]:
                return hit[0]
        except Exception:
            pass
    payload: Optional[Dict[str, Any]] = None
    tables_raw = _with_deadline(15, _fetch_cffex_tables, variety, yyyymmdd)
    if tables_raw:
        tables = {str(contract): _normalize_rank_df(df) for contract, df in tables_raw.items()
                  if df is not None and len(df)}
        if tables:
            payload = _build_day_payload(tables)
            payload["source"] = "cffex"
    if payload is None:
        payload = _with_deadline(20, _tushare_day_rank, variety, yyyymmdd)
    if payload is None:
        return None
    try:
        from data_store import kv_repo

        kv_repo.set_(_KV_NAMESPACE, key, payload)
    except Exception:
        pass
    return payload


def _iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}" if len(yyyymmdd) == 8 else yyyymmdd


def position_rank(variety: str, date: str = "", force: bool = False) -> Dict[str, Any]:
    """品种多空持仓排名。``date`` 空 → 最新已发布交易日(盘中自动回退,标 fallback_from)。"""
    variety = str(variety or "IF").strip().upper()
    meta = VARIETIES.get(variety)
    if not meta:
        return {"ok": False, "error": f"未知品种 {variety},支持 {'/'.join(VARIETIES)}"}
    requested = str(date or "").replace("-", "").strip()
    first_trade = _FIRST_TRADE[variety]
    if requested:
        try:
            _dt.date.fromisoformat(requested)
        except ValueError:
            return {"ok": False, "error": "日期须为 YYYY-MM-DD"}
        if requested < first_trade:
            return {"ok": False, "variety": variety, "date": _iso(requested), "available_start": _iso(first_trade),
                    "error": f"{meta['name']}股指期货于 {_iso(first_trade)} 上市，所选日期无席位持仓。"}
    candidates = [requested] if requested else _recent_trade_dates(8)
    for yyyymmdd in candidates:
        payload = _load_day_rank(variety, yyyymmdd, force=force)
        if payload:
            fallback = None
            if not requested and yyyymmdd != candidates[0]:
                fallback = _iso(candidates[0])
            return {"ok": True, "variety": variety, "name": meta["name"],
                    "date": _iso(yyyymmdd), "available_start": _iso(first_trade), "fallback_from": fallback, **payload}
    which = _iso(requested) if requested else "最近交易日"
    return {"ok": False, "variety": variety, "name": meta["name"], "available_start": _iso(first_trade),
            "error": f"{which}无持仓排名数据(节假日或中金所尚未发布,盘后约16:00更新)"}


def position_trend(variety: str, days: int = 10) -> Dict[str, Any]:
    """近 N 交易日前20席位多/空/净持仓走势(逐日命中 kv 缓存,缺的现拉)。"""
    variety = str(variety or "IF").strip().upper()
    meta = VARIETIES.get(variety)
    if not meta:
        return {"ok": False, "error": f"未知品种 {variety}"}
    days = max(2, min(int(days or 10), 30))
    # 多取 40% 交易日兜底:当日未发布/节假日抓空时仍能凑满 days 个点
    dates = _recent_trade_dates(days + max(3, days * 2 // 5))
    series: List[Dict[str, Any]] = []
    missing: List[str] = []
    deadline = time.time() + 100  # WKWebView 前端 fetch 上限 120s,留余量返回已拿到的部分
    for yyyymmdd in dates:
        if len(series) >= days:
            break
        if time.time() > deadline:
            missing.append(f"{_iso(yyyymmdd)}(未拉取,本次超时)")
            continue
        payload = _load_day_rank(variety, yyyymmdd)
        if not payload:
            missing.append(_iso(yyyymmdd))
            continue
        summary = payload["aggregate"]["summary"]
        series.append({
            "date": _iso(yyyymmdd),
            "long": summary["long_total"], "short": summary["short_total"],
            "net": summary["net"], "ls_ratio": summary["ls_ratio"],
            "vol": summary["vol_total"],
        })
    series.reverse()  # 时间升序,直接喂折线图
    return {"ok": True, "variety": variety, "name": meta["name"],
            "days": days, "series": series, "missing": missing}
