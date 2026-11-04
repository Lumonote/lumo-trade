"""量化交易分析(Quant Radar)服务 —— 量化活跃识别 + 五机制收割预警 + 行为预测。

散户防御型分析:本机无 L2 逐笔/委托队列数据,全部指标为**公开数据代理指标**,
输出统一以「疑似」口径提示,不构成监管认定与投资建议。
设计 spec: docs/superpowers/specs/2026-07-13-quant-radar-tab-design.md

数据源与套路对齐 :mod:`webui.services.futures_service`(模块级函数 + TTL 缓存,
不 import ``webui.core`` 避免环;外部抓取全包 ``_with_deadline``;httpx
``trust_env`` False→True 交替对付本机 Clash 代理):

- 盘口异动: 东财 ``push2ex getAllStockChanges``,与 market_intelligence 同源但取
  **全类型码**(多/空/炸板),按股聚合成五机制评分的核心输入。仅交易时段有数据,
  每次成功抓取把聚合结果落 ``kv_repo``(namespace ``quant_radar``, key ``day:YYYYMMDD``),
  休市/盘后读取自动回退最近快照(payload 标 ``fallback_date``)。
- 全市场快照: 东财 ``push2 clist`` 按量比降序取前 N,带 f7振幅/f8换手/f10量比/
  f62主力净流入/f100行业,供活跃榜轻量评分与板块聚合。
- 龙虎榜量化席位: 本地库 ``dragon_tiger_inst``(is_quant 同步自 Tushare top_inst,
  名称含 量化/DMA/程序化/算法 启发式兜底),经 :func:`dragon_tiger_repo.get_quant_by_date`。
- 个股资金结构: 本地库 ``moneyflow_dc`` 哨兵 top_n=0 全市场快照(amount_unit 万元)。
- 期指立场: 直接读 ``futures_rank`` kv 缓存(futures_service 已入库),不再发网络请求。
- 日K: 由路由注入 ``kline_fetcher``(webui.core.STOCK_KLINE_SERVICE,复用其 60s TTL
  与实时叠加);服务自身不拉 K 线,离线单测注入假数据。
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import httpx

_KV_NAMESPACE = "quant_radar"
_OVERVIEW_TTL = 60  # 盘中自动刷新一拍
_overview_cache: Dict[str, Any] = {"ts": 0.0, "payload": None}
_overview_lock = threading.Lock()

_QUANT_CODES_TTL = 300  # 「无量化」过滤代码集缓存(秒)
_QUANT_ACTIVITY_MIN = 50  # 活跃度≥50(预警等级 中/高)判定量化参与
_quant_codes_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_quant_codes_lock = threading.Lock()

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://quote.eastmoney.com/",
}

# 盘口异动类型码。多头五码沿用 market_intelligence 生产口径,空头/炸板按东财位码
# 规则镜像。8207/8208 在 akshare 旧映射中为「竞价上涨/下跌」,两种含义同向,
# 不影响方向统计(spec §2 存疑项)。
CHANGE_TYPES: Dict[str, Dict[str, str]] = {
    "8201": {"key": "rocket", "label": "火箭发射", "direction": "bull"},
    "8202": {"key": "rebound", "label": "快速反弹", "direction": "bull"},
    "8193": {"key": "big_buy", "label": "大笔买入", "direction": "bull"},
    "8207": {"key": "buy_queue", "label": "有大买盘", "direction": "bull"},
    "4": {"key": "seal_up", "label": "封涨停板", "direction": "bull"},
    "8203": {"key": "dive", "label": "高台跳水", "direction": "bear"},
    "8204": {"key": "plunge", "label": "加速下跌", "direction": "bear"},
    "8194": {"key": "big_sell", "label": "大笔卖出", "direction": "bear"},
    "8208": {"key": "sell_queue", "label": "有大卖盘", "direction": "bear"},
    "8": {"key": "seal_down", "label": "封跌停板", "direction": "bear"},
    "16": {"key": "open_up", "label": "打开涨停板", "direction": "neutral"},
    "32": {"key": "open_down", "label": "打开跌停板", "direction": "neutral"},
}
_KEY_LABELS = {meta["key"]: meta["label"] for meta in CHANGE_TYPES.values()}
_KEY_DIRECTIONS = {meta["key"]: meta["direction"] for meta in CHANGE_TYPES.values()}
_EVENTS_CAP = 240  # 单股单日时间线事件上限(超出按时间均匀采样,计数不受影响)

MECHANISMS: List[Dict[str, str]] = [
    {"key": "spoof", "name": "虚假申报/幌骗"},
    {"key": "hft", "name": "高频速度博弈"},
    {"key": "orderbook", "name": "订单簿诱导"},
    {"key": "sentiment", "name": "情绪算法狙击"},
    {"key": "bias", "name": "行为偏差套利"},
]
_MECH_NAMES = {m["key"]: m["name"] for m in MECHANISMS}
_COMPOSITE_WEIGHTS = {"spoof": 0.20, "hft": 0.30, "orderbook": 0.20,
                      "sentiment": 0.15, "bias": 0.15}


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


def _with_deadline(seconds: float, fn: Callable, *args, **kwargs):
    """给一次外部抓取加硬预算,超时/异常返回 None(线程留后台自生自灭)。"""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(fn, *args, **kwargs).result(timeout=seconds)
    except _FutureTimeout:
        return None
    except Exception:
        return None
    finally:
        pool.shutdown(wait=False)


def _today_key() -> str:
    return _dt.date.today().strftime("%Y%m%d")


def _recent_day_keys(n: int = 8) -> List[str]:
    """近 n 个自然日 YYYYMMDD(最新在前)。快照按保存日期回看,无需交易日历。"""
    today = _dt.date.today()
    return [(today - _dt.timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]


_trade_date_memo: Dict[str, str] = {"day": "", "key": ""}


def _current_trade_date_key() -> str:
    """当前(≤今天的最近)交易日 YYYYMMDD:本地 trade_calendar 优先,缺则工作日近似。

    周末/节假日 push2ex 返回上一交易日重放数据,按日保存必须锚定真实交易日,
    否则会产生幻影日期。每个自然日内存记忆一次。
    """
    today_key = _dt.date.today().strftime("%Y%m%d")
    if _trade_date_memo["day"] == today_key and _trade_date_memo["key"]:
        return _trade_date_memo["key"]
    key = ""
    try:
        from data_store import calendar_repo

        opens = [d for d in calendar_repo.open_days() if d <= today_key]
        if opens:
            key = opens[-1]
    except Exception:
        key = ""
    if not key:
        day = _dt.date.today()
        while day.weekday() >= 5:
            day -= _dt.timedelta(days=1)
        key = day.strftime("%Y%m%d")
    _trade_date_memo.update(day=today_key, key=key)
    return key


def _current_trade_date_iso() -> str:
    return _iso(_current_trade_date_key())


def _norm_date_key(value: Any) -> str:
    """'2026-07-10' / '20260710' → 'YYYYMMDD';空/非法 → ''。"""
    s = str(value or "").strip().replace("-", "")
    return s if len(s) == 8 and s.isdigit() else ""


def _iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}" if len(yyyymmdd) == 8 else yyyymmdd


# ----------------------------- 外部抓取 -----------------------------

def _fetch_stock_changes(pagesize: int = 800) -> List[Dict[str, Any]]:
    """东财盘口异动原始行(``{c,n,t,tm,i}``)。仅交易时段有数据(周末返回上一交易日),失败 → []。

    实测该接口 ``type`` 传逗号多类型只返回第一个类型(akshare 正统用法也是每类型
    单独请求),故 12 个类型码逐个并行拉取后合并;单类型每日异动量有限,每类型
    ``pagesize`` 条已覆盖全市场。
    """
    def _one_type(type_code: str) -> List[Dict[str, Any]]:
        rows_t: List[Dict[str, Any]] = []
        for page in range(8):  # 尽量取全:翻页直到吃完(单类型日常 <3k,8 页×800 富余)
            params = {
                "type": type_code,
                "ut": "7eea3edcaed734bea9cbfc24409ed989",
                "pageindex": str(page),
                "pagesize": str(pagesize),
                "dpt": "wzchanges",
            }
            url = ("https://push2ex.eastmoney.com/getAllStockChanges?"
                   + urllib.parse.urlencode(params))
            batch: List[Dict[str, Any]] = []
            for trust_env in (False, True):  # 境内接口先直连绕系统代理
                try:
                    with httpx.Client(timeout=6, follow_redirects=True,
                                      trust_env=trust_env) as client:
                        resp = client.get(url, headers=_HEADERS)
                        resp.raise_for_status()
                        raw = ((resp.json() or {}).get("data") or {}).get("allstock") or []
                        batch = [r for r in raw if isinstance(r, dict)]
                        break
                except Exception:
                    continue
            rows_t.extend(batch)
            if len(batch) < pagesize:
                break
        return rows_t

    rows: List[Dict[str, Any]] = []
    pool = ThreadPoolExecutor(max_workers=6)
    try:
        futures = [pool.submit(_one_type, t) for t in CHANGE_TYPES]
        for future in futures:
            try:
                rows.extend(future.result(timeout=10) or [])
            except Exception:
                continue
    finally:
        pool.shutdown(wait=False)
    return rows


def _fetch_market_snapshot(limit: int = 400) -> List[Dict[str, Any]]:
    """东财全A快照按量比降序前 N(带振幅/换手/量比/主力净流入/行业)。失败 → []。"""
    params = {
        "pn": "1", "pz": str(limit), "po": "1", "np": "1", "fltt": "2", "invt": "2",
        "fid": "f10",  # 量比降序:量化高频博弈的第一信号
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f12,f14,f2,f3,f7,f8,f10,f22,f62,f100,f124",
        "_": str(int(time.time() * 1000)),
    }
    url = "https://push2.eastmoney.com/api/qt/clist/get?" + urllib.parse.urlencode(params)
    for trust_env in (False, True):
        try:
            with httpx.Client(timeout=6, follow_redirects=True, trust_env=trust_env) as client:
                resp = client.get(url, headers=_HEADERS)
                resp.raise_for_status()
                diff = ((resp.json() or {}).get("data") or {}).get("diff") or []
        except Exception:
            continue
        items = []
        for row in diff:
            code = str(row.get("f12") or "").strip()
            name = str(row.get("f14") or "").strip()
            if not code or not name:
                continue
            items.append({
                "code": code,
                "name": name,
                "price": _num(row.get("f2")),
                "change_pct": _num(row.get("f3")),
                "amplitude": _num(row.get("f7")),
                "turnover": _num(row.get("f8")),
                "volume_ratio": _num(row.get("f10")),
                "speed": _num(row.get("f22")),
                "main_net_inflow": _num(row.get("f62")),
                "quote_at": _dt.datetime.fromtimestamp(float(row["f124"]),_dt.timezone(_dt.timedelta(hours=8))).isoformat() if _num(row.get("f124")) else None,
                "flow_date": _dt.datetime.fromtimestamp(float(row["f124"]),_dt.timezone(_dt.timedelta(hours=8))).date().isoformat() if _num(row.get("f124")) else None,
                "industry": str(row.get("f100") or "").strip(),
            })
        if items:
            return items
    return []


def _flows_df_to_snapshot(df: Any) -> List[Dict[str, Any]]:
    """moneyflow_dc 全市场快照 DataFrame → clist 同形快照行(纯转换,主力净额万元→元)。

    本地兜底没有量比/振幅/换手/行业,置 None/空串,相关评分规则自动跳过。
    按主力净额绝对值降序,便于取「主力大进大出」作候选。
    """
    rows: List[Dict[str, Any]] = []
    if df is None or getattr(df, "empty", True):
        return rows
    for _, row in df.iterrows():
        code = str(row.get("ts_code") or "").split(".")[0].strip()
        if not code:
            continue
        unit_mult = 1e4 if str(row.get("amount_unit") or "万元") == "万元" else 1.0
        main_net = _num(row.get("net_amount"))
        rows.append({
            "code": code,
            "name": str(row.get("name") or "").strip(),
            "price": _num(row.get("close")),
            "change_pct": _num(row.get("pct_change")),
            "amplitude": None,
            "turnover": None,
            "volume_ratio": None,
            "speed": None,
            "main_net_inflow": main_net * unit_mult if main_net is not None else None,
            "flow_date": _iso(str(row.get("trade_date") or "").replace("-","")),
            "price_date": _iso(str(row.get("trade_date") or "").replace("-","")),
            "industry": "",
        })
    rows.sort(key=lambda r: -abs(r.get("main_net_inflow") or 0.0))
    return rows


def _snapshot_from_flows() -> List[Dict[str, Any]]:
    """clist 不可达时的本地兜底:moneyflow_dc 哨兵 top_n=0 最近一日全市场快照。"""
    try:
        from data_store import moneyflow_repo

        latest = moneyflow_repo.latest_date(top_n=0)
        if not latest:
            return []
        return _flows_df_to_snapshot(moneyflow_repo.get_top_n(latest, 0))
    except Exception:
        return []


def _tencent_secid(code: str) -> Optional[str]:
    c = str(code or "").strip()
    if len(c) != 6 or not c.isdigit():
        return None
    if c[0] in "69":
        return "sh" + c
    if c[0] in "48":
        return "bj" + c
    return "sz" + c


_TENCENT_HEADERS = {"User-Agent": _HEADERS["User-Agent"], "Referer": "https://gu.qq.com/"}


def _fetch_tencent_quotes(codes: List[str], batch: int = 80,
                          max_batches: int = 40) -> Dict[str, Dict[str, Any]]:
    """腾讯批量实时行情(qt.gtimg.cn,本机可达):量比/换手/振幅/现价/涨跌幅。失败 → {}。

    用途:clist 被掐走资金流兜底时,兜底行的 close/pct 是**历史交易日**数值,
    必须用实时报价覆盖;同时补齐兜底缺失的量比/换手/振幅。
    字段位 2026-07 实测:3现价 32涨跌% 38换手率 43振幅 49量比。
    """
    secids: List[str] = []
    seen = set()
    for code in codes or []:
        sid = _tencent_secid(code)
        if sid and sid not in seen:
            seen.add(sid)
            secids.append(sid)
    out: Dict[str, Dict[str, Any]] = {}
    for i in range(0, min(len(secids), batch * max_batches), batch):
        url = "https://qt.gtimg.cn/q=" + ",".join(secids[i:i + batch])
        text = ""
        for trust_env in (False, True):
            try:
                with httpx.Client(timeout=6, follow_redirects=True, trust_env=trust_env) as client:
                    resp = client.get(url, headers=_TENCENT_HEADERS)
                    resp.raise_for_status()
                    text = resp.content.decode("gbk", errors="ignore")
                    break
            except Exception:
                continue
        for line in text.splitlines():
            if '="' not in line:
                continue
            fields = line.split('="', 1)[1].rstrip('";').split("~")
            if len(fields) <= 49 or not fields[2].strip():
                continue
            out[fields[2].strip()] = {
                "quote_at": _dt.datetime.strptime(fields[30],"%Y%m%d%H%M%S").isoformat()+"+08:00" if re.fullmatch(r"\d{14}",fields[30]) else None,
                "price": _num(fields[3]),
                "change_pct": _num(fields[32]),
                "turnover": _num(fields[38]),
                "amplitude": _num(fields[43]),
                "volume_ratio": _num(fields[49]),
            }
    return out


def _enrich_snapshot(snapshot_rows: List[Dict[str, Any]],
                     quotes: Optional[Dict[str, Dict[str, Any]]] = None,
                     industry_map: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """实时报价覆盖快照行 + 行业映射补空(纯函数,就地修改并返回)。

    - 报价字段(价/涨跌/量比/换手/振幅)比 clist/资金流兜底都新 → 非 None 直接覆盖;
    - quotes 里有而快照没有的代码追加为新行(异动股不在兜底前列时仍有完整报价);
    - industry 仅在为空时用映射补,不覆盖 clist 自带行业。
    """
    rows = list(snapshot_rows or [])
    by_code = {str(r.get("code")): r for r in rows}
    for code, quote in (quotes or {}).items():
        row = by_code.get(code)
        if row is None:
            row = {"code": code, "name": "", "price": None, "change_pct": None,
                   "amplitude": None, "turnover": None, "volume_ratio": None,
                   "speed": None, "main_net_inflow": None, "industry": ""}
            by_code[code] = row
            rows.append(row)
        for key, value in quote.items():
            if value is not None:
                row[key] = value
    if industry_map:
        for row in rows:
            if not row.get("industry"):
                row["industry"] = industry_map.get(str(row.get("code"))) or ""
    return rows


_industry_provider: Optional[Callable[[], Dict[str, str]]] = None


def set_industry_provider(fn: Optional[Callable[[], Dict[str, str]]]) -> None:
    """宿主注入行业映射来源(market_intelligence 新浪行业分类),服务保持不 import core。"""
    global _industry_provider
    _industry_provider = fn


_tushare_industry_cache: Dict[str, Any] = {"date": "", "map": {}}


def _tushare_industry_map() -> Dict[str, str]:
    """Tushare stock_basic 全市场行业(含创业板/次新,新浪行业分类只覆盖 ~2400 只老股)。

    进程内按日 memo + kv 持久缓存(key ``industry_map``);无 token/失败时回退最近
    一次 kv 旧图(行业几乎不变,陈旧无害),再不行返回 {}。
    """
    today = _dt.date.today().isoformat()
    if _tushare_industry_cache["date"] == today and _tushare_industry_cache["map"]:
        return _tushare_industry_cache["map"]
    stale: Dict[str, str] = {}
    try:
        from data_store import kv_repo

        hit = kv_repo.get(_KV_NAMESPACE, "industry_map")
        if hit and hit[0]:
            stale = hit[0].get("map") or {}
            if hit[0].get("date") == today and stale:
                _tushare_industry_cache.update(date=today, map=stale)
                return stale
    except Exception:
        pass

    def _fetch() -> Dict[str, str]:
        from data_store import tushare_client

        pro = tushare_client.get_pro()
        if pro is None:
            return {}
        df = pro.stock_basic(exchange="", list_status="L", fields="ts_code,industry")
        out: Dict[str, str] = {}
        for _, row in df.iterrows():
            code = str(row.get("ts_code") or "").split(".")[0]
            industry = str(row.get("industry") or "").strip()
            if code and industry:
                out[code] = industry
        return out

    fetched = _with_deadline(20, _fetch) or {}
    if fetched:
        _tushare_industry_cache.update(date=today, map=fetched)
        try:
            from data_store import kv_repo

            kv_repo.set_(_KV_NAMESPACE, "industry_map", {"date": today, "map": fetched})
        except Exception:
            pass
        return fetched
    if stale:
        _tushare_industry_cache.update(date=today, map=stale)
    return stale


def _industry_map() -> Dict[str, str]:
    """合并行业映射:Tushare 全市场为底,新浪行业分类(注入)覆盖——与既有面板口径一致。"""
    try:
        provided = (_industry_provider() if _industry_provider else {}) or {}
    except Exception:
        provided = {}
    base = _tushare_industry_map()
    if not base:
        return provided
    merged = dict(base)
    merged.update(provided)
    return merged


def _futures_stance() -> Optional[str]:
    """期指前20席位多空信号(偏多/偏空/分歧/降温/中性)。只读 futures_rank kv,不发网络。"""
    try:
        from data_store import kv_repo

        for yyyymmdd in _recent_day_keys(8):
            for variety in ("IF", "IM"):
                hit = kv_repo.get("futures_rank", f"{variety}:{yyyymmdd}")
                if hit and hit[0]:
                    summary = ((hit[0].get("aggregate") or {}).get("summary")) or {}
                    signal = summary.get("signal")
                    if signal:
                        return str(signal)
    except Exception:
        pass
    return None


_VARIETY_NAMES = {"IF": "沪深300", "IH": "上证50", "IC": "中证500", "IM": "中证1000"}


def _futures_series_from_kv(max_days: int = 6) -> Dict[str, List[Dict[str, Any]]]:
    """四品种近 N 交易日前20席位净持仓序列(最新在前)。只读 futures_rank kv,不发网络。"""
    out: Dict[str, List[Dict[str, Any]]] = {}
    try:
        from data_store import kv_repo

        for variety in _VARIETY_NAMES:
            series: List[Dict[str, Any]] = []
            for yyyymmdd in _recent_day_keys(12):
                if len(series) >= max_days:
                    break
                hit = kv_repo.get("futures_rank", f"{variety}:{yyyymmdd}")
                if hit and hit[0]:
                    summary = ((hit[0].get("aggregate") or {}).get("summary")) or {}
                    if summary.get("net") is not None:
                        series.append({"date": _iso(yyyymmdd), "net": summary.get("net"),
                                       "signal": summary.get("signal")})
            if series:
                out[variety] = series
    except Exception:
        return {}
    return out


def _futures_context_from_series(per_variety: Dict[str, List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """净持仓序列 → 期指对冲环境(纯函数)。

    3日净变动合计阈值 ±8000 张与 v25 评分因子 fut_bear 同口径;另标注
    大盘(IF/IH)与中小盘(IC/IM)方向分化,供个股环境判定。
    """
    varieties: List[Dict[str, Any]] = []
    for variety, series in (per_variety or {}).items():
        rows = [r for r in series or [] if r.get("net") is not None]
        if not rows:
            continue
        net = _int(rows[0]["net"])
        ref = rows[3] if len(rows) >= 4 else rows[-1]
        varieties.append({
            "variety": variety,
            "name": _VARIETY_NAMES.get(variety, variety),
            "date": str(rows[0].get("date") or ""),
            "signal": str(rows[0].get("signal") or ""),
            "net": net,
            "net_chg_3d": net - _int(ref["net"]),
        })
    if not varieties:
        return None
    varieties.sort(key=lambda v: v["variety"])
    total_chg = sum(v["net_chg_3d"] for v in varieties)
    if total_chg <= -8000:
        pressure = "空压"
        parts = [f"期指前20席位净持仓3日净减 {abs(total_chg)} 张,对冲盘空压明显"]
    elif total_chg >= 8000:
        pressure = "回补"
        parts = [f"期指前20席位净持仓3日净增 {total_chg} 张,空头回补/多头进场"]
    else:
        pressure = "中性"
        parts = ["期指前20席位净持仓3日变化温和"]
    big = [v for v in varieties if v["variety"] in ("IF", "IH")]
    small = [v for v in varieties if v["variety"] in ("IC", "IM")]
    if big and small:
        big_chg = sum(v["net_chg_3d"] for v in big)
        small_chg = sum(v["net_chg_3d"] for v in small)
        if big_chg * small_chg < 0:
            parts.append("大盘(IF/IH)与中小盘(IC/IM)对冲方向分化,注意个股所属市值风格")
    return {"varieties": varieties, "net_chg_3d_total": total_chg,
            "pressure": pressure, "assessment": "；".join(parts)}


def _futures_context() -> Optional[Dict[str, Any]]:
    return _futures_context_from_series(_futures_series_from_kv())


def _volume_energy(bars_by_index: List[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """多指数日K成交额 → 大盘量能(纯函数):今日合计 vs 前5日均,放量/平量/缩量 + 连降天数。

    比值口径与单位无关(Sina amount 原样求和);数据不足 6 个交易日 → None。
    """
    totals: Dict[str, float] = {}
    for bars in bars_by_index or []:
        for bar in bars or []:
            date = str(bar.get("date") or "")[:10]
            amount = _num(bar.get("amount"), 4)
            if not date or amount is None:
                continue
            totals[date] = totals.get(date, 0.0) + amount
    dates = sorted(totals)
    if len(dates) < 6:
        return None
    today = totals[dates[-1]]
    base = sum(totals[d] for d in dates[-6:-1]) / 5
    ratio = round(today / base, 2) if base > 0 else None
    if ratio is None:
        label = "未知"
    elif ratio >= 1.2:
        label = "放量"
    elif ratio <= 0.8:
        label = "缩量"
    else:
        label = "平量"
    down_streak = 0
    values = [totals[d] for d in dates]
    for i in range(len(values) - 1, 0, -1):
        if values[i] < values[i - 1]:
            down_streak += 1
        else:
            break
    return {"date": dates[-1], "total_amount": today, "ratio_5d": ratio, "label": label,
            "down_streak": down_streak,
            "series": [{"date": d, "total": totals[d]} for d in dates[-10:]]}


_SINA_KLINE_URL = ("http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                   "CN_MarketData.getKLineData")
_SINA_HEADERS = {
    "User-Agent": _HEADERS["User-Agent"],
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://finance.sina.com.cn/",
}


def _fetch_index_daily(symbol: str, datalen: int = 12) -> Tuple[List[Dict[str, Any]], bool]:
    """新浪指数日K → (量能行, 是否真成交额)。指数接口实测只有 volume 无 amount,
    量能比值与单位无关,缺 amount 时用成交量代替并由调用方标 metric。失败 → ([], False)。"""
    params = {"symbol": symbol, "scale": "240", "ma": "no", "datalen": str(datalen)}
    for trust_env in (False, True):
        try:
            with httpx.Client(timeout=6, follow_redirects=True, trust_env=trust_env) as client:
                resp = client.get(_SINA_KLINE_URL, params=params, headers=_SINA_HEADERS)
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            continue
        if isinstance(data, list):
            rows: List[Dict[str, Any]] = []
            has_amount = False
            for r in data:
                if not isinstance(r, dict):
                    continue
                amount = _num(r.get("amount"), 4)
                if amount is not None:
                    has_amount = True
                else:
                    amount = _num(r.get("volume"), 4)
                rows.append({"date": str(r.get("day") or "")[:10], "amount": amount})
            return rows, has_amount
    return [], False


_volume_cache: Dict[str, Any] = {"ts": 0.0, "payload": None}


def _market_volume() -> Optional[Dict[str, Any]]:
    """沪深两市量能(上证+深成指日K合计,10min 缓存)。失败 → None。

    ``metric``:``amount``=成交额(元) / ``volume``=成交量(股,新浪指数日K无成交额时)。
    """
    now = time.time()
    if _volume_cache["payload"] and now - _volume_cache["ts"] < 600:
        return _volume_cache["payload"]
    results = [_fetch_index_daily(symbol) for symbol in ("sh000001", "sz399001")]
    payload = _volume_energy([rows for rows, _ in results if rows])
    if payload:
        payload["metric"] = "amount" if any(has for _, has in results) else "volume"
        _volume_cache.update(ts=now, payload=payload)
    return payload


def _quant_seats_window(days: int = 5) -> Dict[str, List[Dict[str, Any]]]:
    """近 N 自然日龙虎榜量化席位,按 6 位代码分组。库空/异常 → {}。"""
    try:
        from data_store import dragon_tiger_repo

        end = _dt.date.today()
        start = end - _dt.timedelta(days=days)
        df = dragon_tiger_repo.get_quant_by_date(start.isoformat(), end.isoformat())
        if df is None or getattr(df, "empty", True):
            return {}
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for _, row in df.iterrows():
            code = str(row.get("ts_code") or "").split(".")[0].strip()
            if not code:
                continue
            grouped.setdefault(code, []).append({
                "trade_date": str(row.get("trade_date") or ""),
                "inst_name": str(row.get("inst_name") or ""),
                "side": str(row.get("side") or ""),
                "buy_amount": _num(row.get("buy_amount")),
                "sell_amount": _num(row.get("sell_amount")),
                "net_amount": _num(row.get("net_amount")),
            })
        return grouped
    except Exception:
        return {}


def quant_active_codes(force: bool = False) -> Dict[str, Any]:
    """『量化资金参与』代码集(资金榜单「无量化」过滤口径),纯本地不发网络。

    并集口径: 最近有数据交易日 activity>=50(quant_radar_stock_daily)
    ∪ 近5自然日龙虎榜量化席位。available=任一源有数据;两源皆空→过滤退化 no-op。
    返回 {"codes": set[6位代码], "as_of": 活跃度数据日|None, "available": bool}。
    """
    with _quant_codes_lock:
        hit = _quant_codes_cache["data"]
        if not force and hit is not None and time.time() - _quant_codes_cache["ts"] < _QUANT_CODES_TTL:
            return hit
    codes: set = set()
    as_of = None
    activity_ok = False
    try:
        from data_store import quant_radar_repo

        dates = quant_radar_repo.list_dates(1)
        if dates:
            as_of = dates[0]
            activity_ok = True
            for item in quant_radar_repo.get_day(as_of, limit=0,
                                                 min_activity=_QUANT_ACTIVITY_MIN):
                code = str(item.get("code") or "").strip()
                if code:
                    codes.add(code)
    except Exception:
        pass
    seats = {}
    try:
        seats = _quant_seats_window(5) or {}
        codes.update(k for k in seats if k)
    except Exception:
        pass
    data = {"codes": codes, "as_of": as_of,
            "available": bool(activity_ok or seats)}
    with _quant_codes_lock:
        _quant_codes_cache["ts"] = time.time()
        _quant_codes_cache["data"] = data
    return data


def _stock_flow(code: str) -> Optional[Dict[str, Any]]:
    """个股最近一日 moneyflow_dc 全市场快照行(单位:万元)。无数据 → None。"""
    try:
        from data_store import moneyflow_repo

        end = _dt.date.today()
        start = end - _dt.timedelta(days=14)
        df = moneyflow_repo.get_stock_rows(code, start.isoformat(), end.isoformat(),
                                           snapshot_top_n=0)
        if df is None or getattr(df, "empty", True):
            return None
        row = df.iloc[0]  # 按日期倒序,首行最新
        return {
            "trade_date": str(row.get("trade_date") or ""),
            "main_net": _num(row.get("net_amount")),
            "sm_net": _num(row.get("buy_sm_amount")),
            "elg_net": _num(row.get("buy_elg_amount")),
            "unit": str(row.get("amount_unit") or "万元"),
        }
    except Exception:
        return None


# ----------------------------- 纯函数:聚合与特征 -----------------------------

def _empty_agg(code: str = "", name: str = "") -> Dict[str, Any]:
    return {"code": code, "name": name, "total": 0, "bull": 0, "bear": 0,
            "counts": {}, "last_time": "", "events": []}


def _parse_change_info(raw: Any) -> Tuple[Optional[float], Optional[float]]:
    """异动 ``i`` 字段(逗号分隔,格式随类型而异)启发式提取 (价格, 涨跌幅%)。

    与 market_intelligence._parse_change_info 同规律:涨跌幅是绝对值 <0.5 的
    比例值,价格是首个落在 [0.5, 10000) 的数;三种已知格式(封板/买卖盘/速度类)
    都满足,避免逐类型硬解析出错。
    """
    parts: List[float] = []
    for token in str(raw or "").split(","):
        try:
            parts.append(float(token))
        except (TypeError, ValueError):
            continue
    pct = next((round(v * 100, 2) for v in parts if abs(v) < 0.5), None)
    price = next((round(v, 2) for v in parts if 0.5 <= v < 10000), None)
    return price, pct


def _aggregate_changes(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """push2ex 原始行按股聚合:类型计数 + 多/空方向计数(炸板类算中性)。未知类型忽略。

    同时保留逐笔时间线 ``events``([tm, key, price, pct] 升序,供个股「拉抬过程」
    点阵图;无 tm 的行不进时间线但仍计数),随 changes_agg 一起进 kv 日快照,
    盘后/回看不丢。超过 :data:`_EVENTS_CAP` 按时间均匀采样控制快照体积。
    """
    agg: Dict[str, Dict[str, Any]] = {}
    for row in rows or []:
        meta = CHANGE_TYPES.get(str(row.get("t") or ""))
        code = str(row.get("c") or "").strip()
        if not meta or not code:
            continue
        slot = agg.setdefault(code, _empty_agg(code, str(row.get("n") or "").strip()))
        if not slot["name"]:
            slot["name"] = str(row.get("n") or "").strip()
        slot["total"] += 1
        slot["counts"][meta["key"]] = slot["counts"].get(meta["key"], 0) + 1
        if meta["direction"] == "bull":
            slot["bull"] += 1
        elif meta["direction"] == "bear":
            slot["bear"] += 1
        tm = _int(row.get("tm"))
        if tm:
            slot["last_time"] = max(slot["last_time"], f"{tm // 10000:02d}:{(tm // 100) % 100:02d}")
            price, pct = _parse_change_info(row.get("i"))
            slot["events"].append([tm, meta["key"], price, pct])
    for slot in agg.values():
        events = sorted(slot["events"], key=lambda e: e[0])
        if len(events) > _EVENTS_CAP:
            step = len(events) / _EVENTS_CAP
            events = [events[int(i * step)] for i in range(_EVENTS_CAP)]
        slot["events"] = events
    return agg


def _payload_bars(bars: List[Dict[str, Any]], limit: int = 60) -> List[Dict[str, Any]]:
    """日K原始行 → 全景图轻量bars(升序,近 limit 根,仅K线绘制所需字段)。"""
    rows = sorted((b for b in bars or [] if _num(b.get("close")) is not None),
                  key=lambda b: str(b.get("date")))
    out = []
    for b in rows[-limit:]:
        out.append({
            "date": str(b.get("date") or ""),
            "open": _num(b.get("open")),
            "high": _num(b.get("high")),
            "low": _num(b.get("low")),
            "close": _num(b.get("close")),
            "pct_chg": _num(b.get("pct_chg")),
        })
    return out


def _timeline_events(agg: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """agg['events'] → 前端时间线事件(升序,HH:MM + 标签/方向/价格/涨跌幅)。

    kv 快照回读后事件为 JSON 数组,与内存 list 同构;历史快照无 events 键 → []。
    """
    out: List[Dict[str, Any]] = []
    for ev in (agg or {}).get("events") or []:
        try:
            tm, key = int(ev[0]), str(ev[1])
        except (TypeError, ValueError, IndexError):
            continue
        out.append({
            "tm": tm,
            "time": f"{tm // 10000:02d}:{(tm // 100) % 100:02d}",
            "key": key,
            "label": _KEY_LABELS.get(key, key),
            "direction": _KEY_DIRECTIONS.get(key, "neutral"),
            "price": _num(ev[2]) if len(ev) > 2 else None,
            "pct": _num(ev[3]) if len(ev) > 3 else None,
        })
    out.sort(key=lambda e: e["tm"])
    return out


def _bar_features(bars: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """近 10 根日K → 影线率/缠斗/翻转/连涨/放量特征(升序输入;不足 2 根 → None)。"""
    rows = sorted((b for b in bars or [] if _num(b.get("close"))), key=lambda b: str(b.get("date")))
    if len(rows) < 2:
        return None
    window = rows[-10:]
    shadows: List[float] = []
    amps: List[float] = []
    for bar in window:
        o, c = float(bar["open"]), float(bar["close"])
        high = float(bar.get("high") or max(o, c))
        low = float(bar.get("low") or min(o, c))
        rng = high - low
        if rng > 0:
            shadows.append(((high - max(o, c)) + (min(o, c) - low)) / rng)
        if c > 0:
            amps.append((high - low) / c * 100)
    pcts = [float(b.get("pct_chg") or 0.0) for b in window]
    big_reversals = sum(1 for i in range(len(pcts) - 1) if pcts[i] >= 5 and pcts[i + 1] <= -3)
    up_streak = 0
    for pct in reversed(pcts):
        if pct > 0:
            up_streak += 1
        else:
            break
    vols = [float(b.get("volume") or 0.0) for b in window]
    vol_spike = False
    if len(vols) >= 6 and vols[-1] > 0:
        base = sum(vols[-6:-1]) / 5
        vol_spike = base > 0 and vols[-1] >= 1.8 * base
    amplitude_expanding = False
    if len(amps) >= 8:
        recent, earlier = amps[-3:], amps[-8:-3]
        if earlier and sum(earlier) / len(earlier) > 0:
            amplitude_expanding = (sum(recent) / 3) >= 1.5 * (sum(earlier) / len(earlier))
    day = window[-1]
    o, c = float(day["open"]), float(day["close"])
    high = float(day.get("high") or max(o, c))
    low = float(day.get("low") or min(o, c))
    rng = high - low
    return {
        "pct_chg": float(day.get("pct_chg") or 0.0),
        "upper_shadow_ratio": round((high - max(o, c)) / rng, 3) if rng > 0 else 0.0,
        "shadow_ratio_mean": round(sum(shadows) / len(shadows), 3) if shadows else 0.0,
        "amp_sum": round(sum(amps), 2),
        "net_pct": round(sum(pcts), 2),
        "big_reversals": big_reversals,
        "up_streak": up_streak,
        "vol_spike": vol_spike,
        "amplitude_expanding": amplitude_expanding,
    }


# ----------------------------- 纯函数:五机制评分 -----------------------------

def _spoof_score(agg: Dict[str, Any], pct_chg: Optional[float] = None,
                 upper_shadow_ratio: Optional[float] = None) -> Tuple[int, List[str]]:
    """机制①幌骗代理:大单异动密集但价格不跟(诱多)/砸单密集但价格不跌(压单吸筹)。"""
    counts = agg.get("counts") or {}
    buy_p = counts.get("big_buy", 0) + counts.get("buy_queue", 0)
    sell_p = counts.get("big_sell", 0) + counts.get("sell_queue", 0)
    score, reasons = 0, []
    if buy_p >= 3:
        if pct_chg is None:
            score = min(30, buy_p * 4)
            reasons.append(f"大笔买入/大买盘异动 {buy_p} 次,待收盘价确认是否兑现")
        elif pct_chg <= 0.5 or (upper_shadow_ratio or 0) >= 0.4:
            score = min(100, 40 + buy_p * 5)
            tail = "且收长上影" if (upper_shadow_ratio or 0) >= 0.4 else f"但收盘仅 {pct_chg:+.1f}%"
            reasons.append(f"大笔买入/大买盘异动 {buy_p} 次{tail},疑似托单诱多")
        else:
            score = 10
            reasons.append(f"大单买入 {buy_p} 次与涨幅匹配,幌骗嫌疑低")
    if sell_p >= 3 and pct_chg is not None and pct_chg >= -0.5:
        score = max(score, min(100, 35 + sell_p * 5))
        reasons.append(f"大笔卖出/大卖盘异动 {sell_p} 次但股价未跌({pct_chg:+.1f}%),疑似压单吸筹或诱空")
    return min(score, 100), reasons


def _hft_score(agg: Dict[str, Any],
               volume_ratio: Optional[float] = None) -> Tuple[int, List[str]]:
    """机制②高频博弈代理:异动频次 + 秒级拉砸并存 + 量比异常。"""
    counts = agg.get("counts") or {}
    total = agg.get("total") or 0
    score, reasons = 0, []
    if total >= 2:
        score += min(60, total * 8)
        reasons.append(f"当日盘口异动 {total} 次,算法参与度高")
    if counts.get("rocket") and counts.get("dive"):
        score += 30
        reasons.append("盘中拉升与跳水异动并存,疑似算法秒级拉砸")
    if volume_ratio is not None:
        if volume_ratio >= 3:
            score += 20
            reasons.append(f"量比 {volume_ratio:.1f},成交节奏远超常态")
        elif volume_ratio >= 2:
            score += 10
            reasons.append(f"量比 {volume_ratio:.1f} 偏高")
    return min(score, 100), reasons


def _orderbook_score(agg: Dict[str, Any], features: Optional[Dict[str, Any]] = None,
                     amplitude_pct: Optional[float] = None,
                     pct_chg: Optional[float] = None) -> Tuple[int, List[str]]:
    """机制③订单簿诱导代理:影线率 + 高振幅低净涨缠斗 + 炸板。"""
    counts = agg.get("counts") or {}
    score, reasons = 0, []
    if features:
        if features.get("shadow_ratio_mean", 0) >= 0.6:
            score += 40
            reasons.append(f"近10日影线占比 {features['shadow_ratio_mean']:.0%},盘中托压反复")
        if features.get("amp_sum", 0) >= 25 and abs(features.get("net_pct", 0)) <= 5:
            score += 30
            reasons.append("累计振幅大而净涨跌小,多空缠斗特征明显")
    elif amplitude_pct is not None and pct_chg is not None and amplitude_pct >= 7 and abs(pct_chg) <= 1.5:
        score += 30
        reasons.append(f"当日振幅 {amplitude_pct:.1f}% 而收盘仅 {pct_chg:+.1f}%,疑似盘口反复诱导")
    open_up = counts.get("open_up", 0)
    if open_up:
        score += min(30, open_up * 15)
        reasons.append(f"涨停开板(炸板) {open_up} 次,封单可信度存疑")
    return min(score, 100), reasons


def _sentiment_score(agg: Dict[str, Any],
                     features: Optional[Dict[str, Any]] = None) -> Tuple[int, List[str]]:
    """机制④情绪狙击代理:急涨急跌同现 + 炸板 + 大阳大阴翻转 + 振幅放大。"""
    counts = agg.get("counts") or {}
    score, reasons = 0, []
    if (counts.get("rocket") and counts.get("dive")) or (counts.get("rebound") and counts.get("plunge")):
        score += 35
        reasons.append("盘中急涨急跌反转同现,情绪面被算法反复收割的典型形态")
    if counts.get("open_up"):
        score += 25
        reasons.append("炸板放量,追涨情绪被兑现")
    if features:
        if features.get("big_reversals"):
            score += 25
            reasons.append(f"近期大阳次日大阴翻转 {features['big_reversals']} 次")
        if features.get("amplitude_expanding"):
            score += 15
            reasons.append("振幅趋势性放大,情绪博弈升级")
    return min(score, 100), reasons


def _bias_score(main_net_inflow: Optional[float] = None,
                sm_net_inflow: Optional[float] = None,
                features: Optional[Dict[str, Any]] = None,
                quant_seats: Iterable[Dict[str, Any]] = (),
                pct_chg: Optional[float] = None) -> Tuple[int, List[str]]:
    """机制⑤行为偏差套利代理:散户接盘背离 + 追高拥挤 + 量化席位站位。"""
    score, reasons = 0, []
    seats = list(quant_seats or ())
    if main_net_inflow is not None and sm_net_inflow is not None \
            and main_net_inflow < 0 < sm_net_inflow:
        score += 40
        reasons.append("主力净流出而小单(散户)净流入,散户接盘背离")
    elif main_net_inflow is not None and sm_net_inflow is None \
            and main_net_inflow < 0 and (pct_chg or 0) >= 2:
        score += 25
        reasons.append("主力净流出而股价上涨,上攻或由跟风盘推动")
    if features:
        if features.get("up_streak", 0) >= 3 and features.get("vol_spike"):
            score += 30
            reasons.append(f"连涨 {features['up_streak']} 日且尾段放量,追高盘拥挤")
        elif features.get("up_streak", 0) >= 3:
            score += 15
            reasons.append(f"连涨 {features['up_streak']} 日,追涨行为聚集")
    if seats:
        if any(str(s.get("side")) == "sell" for s in seats):
            score += 20
            reasons.append("龙虎榜量化席位现身卖方,可预测行为或正被兑现")
        else:
            score += 10
            reasons.append("龙虎榜出现量化席位")
    return min(score, 100), reasons


def _smash_score(agg: Dict[str, Any], pct_chg: Optional[float] = None,
                 volume_ratio: Optional[float] = None,
                 main_net_inflow: Optional[float] = None) -> Tuple[int, List[str]]:
    """疑似量化砸盘评分(方向维度,独立于五机制):单边杀跌异动 + 深跌 + 主力流出。

    识别「程序化集中出货」:高台跳水/加速下跌/大笔卖出/大卖盘/封跌停密集且远超
    拉升类,配合当日深跌与主力净流出。评分 ≥40 记 smash 徽章,≥60 上砸盘榜前列。
    """
    counts = agg.get("counts") or {}
    bear_p = (counts.get("dive", 0) + counts.get("plunge", 0) + counts.get("big_sell", 0)
              + counts.get("sell_queue", 0) + counts.get("seal_down", 0))
    bull_p = (counts.get("rocket", 0) + counts.get("rebound", 0) + counts.get("big_buy", 0)
              + counts.get("buy_queue", 0) + counts.get("seal_up", 0))
    score, reasons = 0, []
    if bear_p >= 3 and bear_p >= 2 * bull_p:
        score += min(50, bear_p * 6)
        reasons.append(f"杀跌类异动 {bear_p} 次(拉升类仅 {bull_p}),单边程序化抛压")
    if pct_chg is not None and pct_chg <= -5:
        if volume_ratio is not None and volume_ratio >= 1.5:
            score += 25
            reasons.append(f"放量深跌 {pct_chg:+.1f}%(量比 {volume_ratio:.1f})")
        else:
            score += 15
            reasons.append(f"深跌 {pct_chg:+.1f}%")
    elif pct_chg is not None and pct_chg <= -2:
        score += 10
        reasons.append(f"收跌 {pct_chg:+.1f}%")
    if main_net_inflow is not None and main_net_inflow < 0 \
            and pct_chg is not None and pct_chg < 0:
        score += 15
        reasons.append("主力资金净流出配合杀跌")
    if counts.get("seal_down"):
        score += 10
        reasons.append("盘中封跌停")
    if counts.get("dive", 0) >= 2:
        score += 10
        reasons.append(f"高台跳水 {counts['dive']} 次,疑似算法批量出货")
    return min(score, 100), reasons


def _direction_label(bull: int, bear: int, pct_chg: Optional[float]) -> str:
    """异动方向标签:砸盘/拉抬/拉锯;无异动 → ''。"""
    total = (bull or 0) + (bear or 0)
    if total <= 0:
        return ""
    if bear >= 2 * max(bull, 1) and (pct_chg is None or pct_chg < 0):
        return "砸盘"
    if bull >= 2 * max(bear, 1) and (pct_chg is None or pct_chg > 0):
        return "拉抬"
    return "拉锯"


def _composite(scores: Dict[str, int], has_quant_seat: bool = False) -> Dict[str, Any]:
    """五机制加权 → 量化活跃度 0-100 与预警等级(阈值 70/50/30)。"""
    activity = round(sum(_COMPOSITE_WEIGHTS[k] * (scores.get(k) or 0)
                         for k in _COMPOSITE_WEIGHTS))
    if has_quant_seat:
        activity += 10
    activity = max(0, min(100, activity))
    if activity >= 70:
        level, rank = "高危", 3
    elif activity >= 50:
        level, rank = "中度", 2
    elif activity >= 30:
        level, rank = "轻度", 1
    else:
        level, rank = "常态", 0
    return {"activity": activity, "level": level, "level_rank": rank}


# ----------------------------- 纯函数:行为预测 -----------------------------

_PREDICT_RULES: List[Dict[str, str]] = [
    {"key": "hft", "tag": "高频博弈",
     "text": "明日大概率延续高波动高频博弈,竞价与开盘半小时慎追单;任何秒级/分钟级反应你都慢算法千倍"},
    {"key": "spoof", "tag": "盘口诱导",
     "text": "盘口大单可信度低,等实际成交回报再判断,不要在大单挂出时跟风"},
    {"key": "orderbook", "tag": "托压陷阱",
     "text": "托单/压单反复,关注高撤单率盘口;影线密集区多为诱导,勿以盘口挂单判断供需"},
    {"key": "sentiment", "tag": "情绪狙击",
     "text": "情绪反转风险高,急涨急跌时先停 5 分钟再操作;你的恐慌与贪婪是算法的输入"},
    {"key": "bias", "tag": "拥挤兑现",
     "text": "追高盘拥挤,量化或反向兑现,控制仓位并预设止损;避免成为可预测的样本点"},
]


def _predict_stock(scores: Dict[str, int],
                   agg: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """规则化次日行为展望:每条对应一个 ≥60 分机制,按分数降序;全部平静给常态提示。"""
    tips = [{"tag": rule["tag"], "text": rule["text"], "mechanism": rule["key"],
             "score": scores.get(rule["key"]) or 0}
            for rule in _PREDICT_RULES if (scores.get(rule["key"]) or 0) >= 60]
    tips.sort(key=lambda t: -t["score"])
    if tips:
        return tips
    peak = max((scores.get(k) or 0) for k in _COMPOSITE_WEIGHTS) if scores else 0
    if peak >= 30:
        return [{"tag": "观察", "mechanism": "", "score": peak,
                 "text": "存在轻至中度量化活动迹象,关注盘口异动频次与量比变化,暂无需特别防御"}]
    return [{"tag": "常态", "mechanism": "", "score": peak,
             "text": "未见明显量化收割特征,按自身交易计划执行即可;保持低频、拉长持仓周期仍是最优防御"}]


def _predict_market(gauge: Dict[str, Any], futures_signal: Optional[str] = None,
                    volume: Optional[Dict[str, Any]] = None) -> str:
    """市场级展望:异动多空比 + 炸板 + 期指前20席位信号 + 大盘量能,规则透明。"""
    bull = gauge.get("bull") or 0
    bear = gauge.get("bear") or 0
    if bull > bear * 1.5:
        text = "拉升类异动占优,算法盘面偏进攻,注意尾盘获利兑现回落"
    elif bear > bull * 1.5:
        text = "杀跌类异动占优,算法盘面偏防守/出货,反弹慎追"
    else:
        text = "多空算法拉锯,方向未明,降低操作频率"
    if gauge.get("open_up"):
        text += f";今日炸板 {gauge['open_up']} 次,涨停封单可信度整体偏低"
    if futures_signal:
        text += f";期指前20席位信号「{futures_signal}」,股指对冲盘立场供参考"
    if volume and volume.get("label") and volume.get("label") != "未知":
        ratio = volume.get("ratio_5d")
        text += f";两市量能{volume['label']}" + (f"(较5日均 {ratio}x)" if ratio else "")
    return text


def _env_predictions(scores: Dict[str, int],
                     futures_ctx: Optional[Dict[str, Any]],
                     volume: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """市场环境(期指对冲 + 大盘量能)×个股机制分 → 追加的环境预测条目。"""
    tips: List[Dict[str, Any]] = []
    peak = max((scores.get(k) or 0) for k in _COMPOSITE_WEIGHTS) if scores else 0
    if futures_ctx and futures_ctx.get("pressure") == "空压" and peak >= 50:
        tips.append({"tag": "对冲压制", "mechanism": "market", "score": peak,
                     "text": "期指前20席位空头压力大,量化对冲环境下高活跃股反弹空间受限,轻仓短打不恋战"})
    if futures_ctx and futures_ctx.get("pressure") == "回补" and peak >= 50:
        tips.append({"tag": "对冲回暖", "mechanism": "market", "score": peak,
                     "text": "期指空头回补/多头进场,量化对冲环境转暖,高活跃股博弈胜率相对提升"})
    if volume and volume.get("label") == "缩量" and (scores.get("bias") or 0) >= 50:
        tips.append({"tag": "缩量拥挤", "mechanism": "market", "score": scores.get("bias") or 0,
                     "text": "大盘缩量而追高盘拥挤,跟风接力不足,谨防量化反手兑现"})
    if volume and volume.get("label") == "放量" and (scores.get("hft") or 0) >= 50:
        tips.append({"tag": "放量博弈", "mechanism": "market", "score": scores.get("hft") or 0,
                     "text": "大盘放量放大算法参与度,盘口噪音增多,信号确认周期宜拉长"})
    return tips


# ----------------------------- 纯函数:榜单组装 -----------------------------

def _light_stock_item(code: str, agg: Dict[str, Any], snap: Dict[str, Any],
                      seats: List[Dict[str, Any]]) -> Dict[str, Any]:
    """轻量五机制快评(不拉日K):盘口异动 + 全市场快照 + 量化席位。"""
    spoof, spoof_r = _spoof_score(agg, pct_chg=snap.get("change_pct"))
    hft, hft_r = _hft_score(agg, volume_ratio=snap.get("volume_ratio"))
    orderbook, ob_r = _orderbook_score(agg, amplitude_pct=snap.get("amplitude"),
                                       pct_chg=snap.get("change_pct"))
    sentiment, se_r = _sentiment_score(agg)
    bias, bias_r = _bias_score(main_net_inflow=snap.get("main_net_inflow"),
                               quant_seats=seats, pct_chg=snap.get("change_pct"))
    scores = {"spoof": spoof, "hft": hft, "orderbook": orderbook,
              "sentiment": sentiment, "bias": bias}
    comp = _composite(scores, has_quant_seat=bool(seats))
    smash, smash_r = _smash_score(agg, pct_chg=snap.get("change_pct"),
                                  volume_ratio=snap.get("volume_ratio"),
                                  main_net_inflow=snap.get("main_net_inflow"))
    direction = _direction_label(agg.get("bull") or 0, agg.get("bear") or 0,
                                 snap.get("change_pct"))
    badges = [k for k in _COMPOSITE_WEIGHTS if scores[k] >= 50]
    if smash >= 40:
        badges.append("smash")
    reasons = (smash_r if smash >= 40 else []) + spoof_r + hft_r + ob_r + se_r + bias_r
    return {
        "code": code,
        "name": agg.get("name") or snap.get("name") or "",
        "industry": snap.get("industry") or "",
        "price": snap.get("price"),
        "change_pct": snap.get("change_pct"),
        "volume_ratio": snap.get("volume_ratio"),
        "turnover": snap.get("turnover"),
        "amplitude": snap.get("amplitude"),
        "main_net_inflow": snap.get("main_net_inflow"),
        "quote_at": snap.get("quote_at"),
        "price_date": snap.get("price_date"),
        "flow_date": snap.get("flow_date"),
        "changes_total": agg.get("total") or 0,
        "changes_bull": agg.get("bull") or 0,
        "changes_bear": agg.get("bear") or 0,
        "quant_seat": bool(seats),
        "direction": direction,
        "smash": smash,
        "smash_reasons": smash_r[:3],
        "scores": scores,
        "badges": badges,
        "reasons": reasons[:4],
        **comp,
    }


def _build_stock_items(changes_agg: Dict[str, Dict[str, Any]],
                       snapshot_rows: List[Dict[str, Any]],
                       quant_seats: Dict[str, List[Dict[str, Any]]],
                       top_n: Optional[int] = None) -> List[Dict[str, Any]]:
    snap_by_code = {str(r.get("code")): r for r in snapshot_rows or []}
    # 候选 = 全部异动股(全量) ∪ 快照前 600(clist 按量比排序 / 本地兜底按主力净额绝对值排序)
    codes = list(dict.fromkeys(
        list(changes_agg) + [str(r.get("code")) for r in (snapshot_rows or [])[:600]]))
    items = [_light_stock_item(code, changes_agg.get(code) or _empty_agg(code),
                               snap_by_code.get(code) or {}, quant_seats.get(code) or [])
             for code in codes]
    items.sort(key=lambda x: (-x["activity"], -x["smash"], -x["changes_total"]))
    return items[:top_n] if top_n else items


def _aggregate_sectors(items: List[Dict[str, Any]],
                       top_n: Optional[int] = None) -> List[Dict[str, Any]]:
    """全部评分项按行业聚合:活跃度 + 方向识别(集体砸盘/集体拉抬,无行业信息跳过)。

    集体砸盘 = 板块内疑似砸盘股(direction==砸盘 或 smash≥40) ≥3 只且平均涨跌 ≤-2%;
    集体拉抬对称(≥3 只拉抬且平均涨跌 ≥2%)。集体砸盘板块置顶,其余按平均活跃度。
    默认返回全部板块(前端分页展示),``top_n`` 仅供需要截断的调用方使用。
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in items or []:
        industry = str(item.get("industry") or "").strip()
        if industry:
            grouped.setdefault(industry, []).append(item)
    sectors = []
    for industry, members in grouped.items():
        members.sort(key=lambda m: -(m.get("activity") or 0))
        avg = sum(m.get("activity") or 0 for m in members) / len(members)
        pcts = [m.get("change_pct") for m in members if m.get("change_pct") is not None]
        avg_pct = round(sum(pcts) / len(pcts), 2) if pcts else None
        smashed = [m for m in members
                   if m.get("direction") == "砸盘" or (m.get("smash") or 0) >= 40]
        pumped = [m for m in members if m.get("direction") == "拉抬"]
        collective = ""
        if len(smashed) >= 3 and avg_pct is not None and avg_pct <= -2:
            collective = "集体砸盘"
        elif len(pumped) >= 3 and avg_pct is not None and avg_pct >= 2:
            collective = "集体拉抬"
        top_member = (smashed[0] if collective == "集体砸盘" and smashed else members[0])
        sectors.append({
            "industry": industry,
            "count": len(members),
            "avg_activity": round(avg, 1),
            "avg_change_pct": avg_pct,
            "smashed_count": len(smashed),
            "collective": collective,
            "top": {"code": top_member["code"], "name": top_member["name"],
                    "activity": top_member["activity"]},
        })
    sectors.sort(key=lambda s: (0 if s["collective"] == "集体砸盘" else 1,
                                -s["smashed_count"], -s["avg_activity"], -s["count"]))
    return sectors[:top_n] if top_n else sectors


def _build_gauge(changes_agg: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    gauge = {"date": _current_trade_date_iso(), "total": 0, "bull": 0, "bear": 0,
             "open_up": 0, "active_stocks": len(changes_agg), "top_types": []}
    type_counts: Dict[str, int] = {}
    for agg in changes_agg.values():
        gauge["total"] += agg.get("total") or 0
        gauge["bull"] += agg.get("bull") or 0
        gauge["bear"] += agg.get("bear") or 0
        for key, n in (agg.get("counts") or {}).items():
            type_counts[key] = type_counts.get(key, 0) + n
    gauge["open_up"] = type_counts.get("open_up", 0)
    gauge["top_types"] = [{"key": k, "label": _KEY_LABELS.get(k, k), "count": n}
                          for k, n in sorted(type_counts.items(), key=lambda kv: -kv[1])[:6]]
    return gauge


# ----------------------------- 知识卡(静态结构化内容) -----------------------------

def knowledge_payload() -> Dict[str, Any]:
    """五机制知识卡 + 监管时间线 + 散户防御五条。内容取自需求文章,前端直接渲染。"""
    return {
        "mechanisms": [
            {"key": "spoof", "name": "虚假申报操纵(幌骗)",
             "method": "在关键价位挂大额买/卖单制造供需假象,诱导跟风后毫秒级撤单反向成交",
             "signals": ["盘口大单反复挂撤", "大买单密集但股价不涨", "大卖单密集但股价不跌"],
             "defense": ["不追逐盘口大单——真正的大资金不会让你看到意图", "等大单实际成交后再判断"]},
            {"key": "hft", "name": "高频闪电抢单(速度套利)",
             "method": "利用微秒级速度优势抢先看到订单流并成交,赚散户几十毫秒延迟的速度差",
             "signals": ["异动频次极高", "量比异常放大", "秒级拉升与跳水并存"],
             "defense": ["不做超短线——你已输了千倍速度", "持仓周期放到日线以上,速度优势边际递减"]},
            {"key": "orderbook", "name": "订单簿诱导(托单/压单陷阱)",
             "method": "买五档堆托单造支撑假象、卖五档堆压单造抛压恐慌,真实意图与挂单方向相反",
             "signals": ["某价位反复挂单又撤单", "高振幅低净涨", "影线密集", "涨停反复开板"],
             "defense": ["看 Level-2 撤单率,高撤单率盘口是陷阱", "低撤单率才是真实供需"]},
            {"key": "sentiment", "name": "情绪算法狙击",
             "method": "NLP 实时扫描社媒/股吧情绪,检测到集体恐慌率先砸盘、集体贪婪率先拉升,永远快你一步",
             "signals": ["急涨急跌反转", "炸板", "大阳次日大阴", "振幅趋势放大"],
             "defense": ["恐慌/贪婪时先停 5 分钟再操作", "你情绪化的每个决定,对面都有算法在等"]},
            {"key": "bias", "name": "行为偏差统计套利",
             "method": "对追涨杀跌/处置效应/过度交易/锚定成本等散户系统性偏差建概率模型,统计意义上持续收割",
             "signals": ["连涨后放量追高", "主力流出散户流入背离", "量化席位站卖方"],
             "defense": ["记录每笔交易的原因", "若九成理由是『感觉要涨』,你就是模型里可预测的样本点"]},
        ],
        "timeline": [
            {"date": "2024-10", "event": "《证券市场程序化交易管理规定》实施,程序化交易首次纳入系统监管"},
            {"date": "2025-04", "event": "沪深北交易所发布《程序化交易管理实施细则》,首次以量化标准界定高频交易"},
            {"date": "2025-07", "event": "《实施细则》正式实施,高频交易差异化监管(提费/限频)落地"},
            {"date": "2026-04", "event": "高频认定标准从每秒300笔骤降至15笔(收紧20倍),剑指扰乱市场公平行为"},
        ],
        "defense_rules": [
            "降低交易频率——交易越少,被收割的采样点越少",
            "拉长持仓周期至日线以上——速度优势对长周期边际递减",
            "不在恐慌/贪婪时立刻操作——急跌 3% 时先停 5 分钟",
            "不追逐盘口大单与封单——等成交回报,不信挂单",
            "记录交易日志,反向审视自己是否成为『可预测样本』",
        ],
        "disclaimer": ("本页全部指标为公开数据构造的启发式代理指标,「疑似」判定不构成对任何主体"
                       "违规行为的认定,亦不构成投资建议;量化交易同时具有提供流动性、缩小价差等积极作用。"),
    }


# ----------------------------- kv 快照 -----------------------------

def _save_day_snapshot(date_key: str, payload: Dict[str, Any]) -> None:
    try:
        from data_store import kv_repo

        kv_repo.set_(_KV_NAMESPACE, f"day:{date_key}", payload)
    except Exception:
        pass


def _load_recent_snapshot(date_keys: List[str]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    try:
        from data_store import kv_repo

        for date_key in date_keys:
            hit = kv_repo.get(_KV_NAMESPACE, f"day:{date_key}")
            if hit and hit[0]:
                return date_key, hit[0]
    except Exception:
        pass
    return None, None


# ----------------------------- 对外入口 -----------------------------

# ----------------------------- 吸筹埋伏榜(买入侧;spec 2026-07-16) -----------------------------

_ACCUM_TTL = 600
_accum_cache: Dict[str, Dict[str, Any]] = {}
_accum_lock = threading.Lock()


def _accum_series_from_df(df: Any) -> Dict[str, List[Dict[str, Any]]]:
    """get_market_window DataFrame → ``{6位代码: 升序逐日行}``(纯转换)。"""
    out: Dict[str, List[Dict[str, Any]]] = {}
    if df is None or getattr(df, "empty", True):
        return out
    for row in df.to_dict("records"):
        code = str(row.get("ts_code") or "").split(".")[0].strip()
        if len(code) == 6 and code.isdigit():
            out.setdefault(code, []).append(row)
    return out


def _accum_bonus(agg: Optional[Dict[str, Any]],
                 seats: Iterable[Dict[str, Any]],
                 change_pct: Optional[float]) -> Tuple[int, List[str]]:
    """当日盘口/席位对吸筹判定的佐证加分(spec §2.6):压单吸筹或大买单 +5,量化席位买方 +5。"""
    counts = (agg or {}).get("counts") or {}
    bonus, reasons = 0, []
    sell_wall = counts.get("big_sell", 0) + counts.get("sell_queue", 0)
    absorbing = sell_wall >= 3 and (change_pct is None or change_pct >= -0.5)
    if absorbing or counts.get("big_buy", 0) >= 3:
        bonus += 5
        reasons.append("当日盘口大单异动佐证(压单吸筹/大笔买入)")
    if any(str(s.get("side")) == "buy" for s in seats or ()):
        bonus += 5
        reasons.append("近30日龙虎榜量化席位现身买方")
    return bonus, reasons


def _apply_no_quant(payload: Dict[str, Any], no_quant: bool) -> Dict[str, Any]:
    """「无量化」读时过滤:浅拷贝 payload 剔除量化参与股;缓存/kv 快照存全量不动。"""
    if not no_quant or not isinstance(payload, dict):
        return payload
    info = quant_active_codes()
    codes = info.get("codes") or set()
    stocks = list(payload.get("stocks") or [])
    kept = [s for s in stocks if str(s.get("code") or "") not in codes]
    out = dict(payload)
    out["stocks"] = kept
    out["count"] = len(kept)
    out["no_quant"] = True
    out["quant_filtered"] = len(stocks) - len(kept)
    out["quant_criteria_available"] = bool(info.get("available"))
    return out


def accumulation_payload(window: int = 40, date: str = "", force: bool = False,
                         market_rows_fn: Optional[Callable[[str, int], Any]] = None,
                         changes_agg_fn: Optional[Callable[[], Dict[str, Any]]] = None,
                         seats_fn: Optional[Callable[[], Dict[str, List[Dict[str, Any]]]]] = None,
                         fetch_quotes: Optional[Callable[[List[str]], Dict[str, Dict[str, Any]]]] = None,
                         no_quant: bool = False,
                         ) -> Dict[str, Any]:
    """吸筹埋伏榜:全市场扫描主力持续净流入且股价横盘的疑似吸筹股(买入埋伏侧)。

    数据源 ``moneyflow_dc`` 本地库(不发网络取历史);当日增强 = 腾讯实时报价覆盖 +
    行业映射 + 盘口/席位佐证加分。传 ``date`` 且非当前交易日 → kv 快照优先,缺则
    as-of 重算(纯本地)。``*_fn`` 参数供离线测试注入(注入即视为离线,不发网络)。
    """
    from analysis import accumulation_detector as det

    try:
        window = int(window)
    except (TypeError, ValueError):
        window = 40
    if window not in det.WINDOWS:
        window = 40
    requested = _norm_date_key(date)
    current_key = _current_trade_date_key()
    historical = bool(requested and requested != current_key)
    injected = any((market_rows_fn, changes_agg_fn, seats_fn, fetch_quotes))
    cache_key = f"{window}:{requested or current_key}"
    if not injected and not force:
        with _accum_lock:
            hit = _accum_cache.get(cache_key)
            if hit and time.time() - hit["ts"] < _ACCUM_TTL:
                return _apply_no_quant(hit["payload"], no_quant)
    if historical and not injected:
        try:
            from data_store import kv_repo

            snap = kv_repo.get(_KV_NAMESPACE, f"accum:{requested}:{window}")
            if snap and snap[0]:
                return _apply_no_quant(snap[0], no_quant)
        except Exception:
            pass

    end_iso = _iso(requested) if requested else _dt.date.today().isoformat()
    if market_rows_fn is not None:
        df = market_rows_fn(end_iso, window)
    else:
        from data_store import moneyflow_repo

        df = moneyflow_repo.get_market_window(end_iso, window)
    series = _accum_series_from_df(df)
    data_date = max((rows[-1].get("trade_date") for rows in series.values()),
                    default="") if series else ""

    items: List[Dict[str, Any]] = []
    for code, rows in series.items():
        result = det.detect(rows, window=window)
        if not result or not result.get("qualified"):
            continue
        last = rows[-1]
        items.append({"code": code, "name": str(last.get("name") or ""), "industry": "",
                      "price": _num(last.get("close")), "change_pct": _num(last.get("pct_change")),
                      **{k: v for k, v in result.items() if k != "daily"}})

    if items and not historical:  # 当日增强:实时报价 + 行业 + 佐证加分
        codes = [i["code"] for i in items]
        quotes = (fetch_quotes(codes) if fetch_quotes
                  else ({} if injected
                        else _with_deadline(15, _fetch_tencent_quotes, codes) or {}))
        industry = {} if injected else _industry_map()
        if changes_agg_fn is not None:
            changes_agg = changes_agg_fn() or {}
        else:
            _d, snap = _load_recent_snapshot([current_key])
            changes_agg = (snap or {}).get("changes_agg") or {}
        seats_map = (seats_fn() if seats_fn is not None
                     else ({} if injected else _quant_seats_window(30)))
        for item in items:
            quote = quotes.get(item["code"]) or {}
            for key in ("price", "change_pct"):
                if quote.get(key) is not None:
                    item[key] = quote[key]
            if not item["industry"]:
                item["industry"] = industry.get(item["code"]) or ""
            bonus, extra = _accum_bonus(changes_agg.get(item["code"]),
                                        seats_map.get(item["code"]) or [],
                                        item.get("change_pct"))
            if bonus:
                item["score"] = min(100, (item.get("score") or 0) + bonus)
                item["reasons"] = list(item.get("reasons") or []) + extra

    items.sort(key=lambda x: (-(x.get("score") or 0), -(x.get("accum_ratio") or 0)))
    payload = {
        "ok": True, "window": window, "data_date": str(data_date or ""),
        "updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "count": len(items), "stocks": items,
        "note": "" if items else "窗口内无满足吸筹判定的股票(或本地资金流历史不足,可先运行资金榜单回填)",
        "disclaimer": knowledge_payload()["disclaimer"],
    }
    if not injected:
        if not historical and items:
            try:
                from data_store import kv_repo

                kv_repo.set_(_KV_NAMESPACE, f"accum:{current_key}:{window}", payload)
            except Exception:
                pass
        with _accum_lock:
            _accum_cache[cache_key] = {"ts": time.time(), "payload": payload}
    return _apply_no_quant(payload, no_quant)


def _smashed_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """疑似砸盘榜:smash≥40,按砸盘分降序、跌幅深者靠前。"""
    return sorted(
        (r for r in rows or [] if (r.get("smash") or 0) >= 40),
        key=lambda x: (-(x.get("smash") or 0),
                       x.get("change_pct") if x.get("change_pct") is not None else 0),
    )


def _expand_with_day_rows(date_iso: str,
                          stocks: List[Dict[str, Any]],
                          smashed: List[Dict[str, Any]],
                          sectors: List[Dict[str, Any]]) -> Tuple[
        List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """kv 快照只存前列;按日表(全量落库)行数更多时用它补全股票榜/砸盘榜/板块榜。"""
    try:
        from data_store import quant_radar_repo

        rows = quant_radar_repo.get_day(date_iso, limit=0)
    except Exception:
        rows = []
    if len(rows) > len(stocks or []):
        stocks = rows
        smashed = _smashed_rows(rows)
        sectors = _aggregate_sectors(rows) or sectors
    return stocks, smashed, sectors


def _historical_overview(date_key: str) -> Dict[str, Any]:
    """按日期回看(不发网络):优先 kv 完整快照(顺带懒回填按日表),缺则按日表重建,再缺则空态。"""
    date_iso = _iso(date_key)
    base = {
        "updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "live": False, "fallback_date": None, "snapshot_date": date_iso,
        "futures_signal": None, "volume_energy": None,
        "knowledge": knowledge_payload(),
    }
    _date, snap = _load_recent_snapshot([date_key])
    if snap:
        gauge = snap.get("gauge") or _build_gauge({})
        stocks = snap.get("stocks") or []
        try:
            from data_store import quant_radar_repo

            if stocks and not quant_radar_repo.get_day(date_iso, limit=1):
                quant_radar_repo.upsert_day(date_iso, stocks)  # 懒回填按日表
        except Exception:
            pass
        stocks, smashed, sectors = _expand_with_day_rows(
            date_iso, stocks, snap.get("smashed") or [], snap.get("sectors") or [])
        return {**base, "gauge": gauge, "stocks": stocks,
                "smashed": smashed,
                "sectors": sectors,
                "alerts": snap.get("alerts") or [],
                "market_prediction": _predict_market(gauge),
                "note": f"{date_iso} 历史快照"}
    rows: List[Dict[str, Any]] = []
    try:
        from data_store import quant_radar_repo

        rows = quant_radar_repo.get_day(date_iso, limit=0)
    except Exception:
        rows = []
    if rows:
        gauge = {"date": date_iso,
                 "total": sum(r.get("changes_total") or 0 for r in rows),
                 "bull": sum(r.get("changes_bull") or 0 for r in rows),
                 "bear": sum(r.get("changes_bear") or 0 for r in rows),
                 "open_up": 0, "active_stocks": len(rows), "top_types": []}
        alerts = [{"code": r["code"], "name": r.get("name") or "",
                   "industry": r.get("industry") or "",
                   "activity": r.get("activity") or 0, "level": r.get("level") or "",
                   "badges": r.get("badges") or [],
                   "reason": "；".join((r.get("reasons") or [])[:2]) or "多机制并发"}
                  for r in rows if (r.get("level_rank") or 0) >= 3]
        return {**base, "gauge": gauge, "stocks": rows,
                "smashed": _smashed_rows(rows),
                "sectors": _aggregate_sectors(rows), "alerts": alerts,
                "market_prediction": _predict_market(gauge),
                "note": f"{date_iso} 由按日榜单表重建(当日完整盘口快照缺失,温度计为近似值)"}
    gauge = _build_gauge({})
    gauge["date"] = date_iso
    return {**base, "gauge": gauge, "stocks": [], "smashed": [], "sectors": [], "alerts": [],
            "market_prediction": "",
            "note": f"{date_iso} 无按日数据(该日未运行量化雷达或非交易日)"}


def overview(force: bool = False,
             fetch_changes: Optional[Callable[[], List[Dict[str, Any]]]] = None,
             fetch_snapshot: Optional[Callable[[], List[Dict[str, Any]]]] = None,
             futures_signal_fn: Optional[Callable[[], Optional[str]]] = None,
             snapshot_dates: Optional[List[str]] = None,
             volume_fn: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
             fetch_quotes: Optional[Callable[[List[str]], Dict[str, Dict[str, Any]]]] = None,
             date: str = "") -> Dict[str, Any]:
    """量化雷达总览:市场温度计(含大盘量能) + 活跃股票榜 + 板块榜 + 高危预警 + 知识卡。

    交易时段实时抓取,按**交易日**落 kv 完整快照 + quant_radar_stock_daily 按日榜单表;
    休市/盘后自动回退最近 ≤8 天快照(标 ``fallback_date``)。传 ``date``(YYYY-MM-DD /
    YYYYMMDD)且非当前交易日 → 纯本地历史回看(kv 快照优先,按日表重建兜底,不发网络)。
    fetch_* 参数供离线测试注入。
    """
    requested = _norm_date_key(date)
    if requested and requested != _current_trade_date_key():
        return _historical_overview(requested)
    injected = any((fetch_changes, fetch_snapshot, futures_signal_fn, volume_fn, fetch_quotes))
    if not injected:
        with _overview_lock:
            cached = _overview_cache["payload"]
            if cached and not force and (time.time() - _overview_cache["ts"]) < _OVERVIEW_TTL:
                return cached
    changes_rows = (fetch_changes() if fetch_changes
                    else _with_deadline(15, _fetch_stock_changes)) or []
    snapshot_rows = (fetch_snapshot() if fetch_snapshot
                     else _with_deadline(12, _fetch_market_snapshot)) or []
    snapshot_note = ""
    if not snapshot_rows and fetch_snapshot is None:
        snapshot_rows = _snapshot_from_flows()
        if snapshot_rows:
            snapshot_note = "东财全市场快照不可达,已用本地资金流缓存兜底(量比/振幅/行业暂缺)"
    futures_signal = (futures_signal_fn() if futures_signal_fn else _futures_stance())
    volume = volume_fn() if volume_fn else (None if injected else _with_deadline(10, _market_volume))
    changes_agg = _aggregate_changes(changes_rows)
    knowledge = knowledge_payload()
    now_iso = _dt.datetime.now().isoformat(timespec="seconds")

    if not changes_agg:  # 休市/盘后:回退最近快照
        date_key, snap = _load_recent_snapshot(snapshot_dates or _recent_day_keys(8))
        if snap:
            gauge = snap.get("gauge") or _build_gauge({})
            fb_stocks, fb_smashed, fb_sectors = _expand_with_day_rows(
                _iso(date_key), snap.get("stocks") or [],
                snap.get("smashed") or [], snap.get("sectors") or [])
            payload = {
                "updated_at": now_iso, "live": False, "fallback_date": _iso(date_key),
                "gauge": gauge,
                "stocks": fb_stocks,
                "smashed": fb_smashed,
                "sectors": fb_sectors,
                "alerts": snap.get("alerts") or [],
                "market_prediction": _predict_market(gauge, futures_signal, volume),
                "futures_signal": futures_signal,
                "volume_energy": volume,
                "knowledge": knowledge,
                "note": f"非交易时段,展示 {_iso(date_key)} 快照",
            }
        else:
            gauge = _build_gauge({})
            payload = {
                "updated_at": now_iso, "live": False, "fallback_date": None,
                "gauge": gauge, "stocks": [], "smashed": [], "sectors": [], "alerts": [],
                "market_prediction": _predict_market(gauge, futures_signal, volume),
                "futures_signal": futures_signal,
                "volume_energy": volume,
                "knowledge": knowledge,
                "note": "非交易时段且暂无历史快照,待交易时段自动积累数据",
            }
        if not injected:
            with _overview_lock:
                _overview_cache.update(ts=time.time(), payload=payload)
        return payload

    quant_seats = _quant_seats_window(5)
    # 腾讯实时报价覆盖(修正兜底快照的陈旧价,补量比/换手/振幅) + 行业映射补空。
    # 全量口径:报价覆盖全部异动股(而非前 N),行业映射覆盖全部候选。
    quote_codes = list(changes_agg) + [str(r.get("code")) for r in snapshot_rows[:600]]
    quotes = (fetch_quotes(quote_codes) if fetch_quotes
              else ({} if injected
                    else _with_deadline(20, _fetch_tencent_quotes, quote_codes) or {}))
    snapshot_rows = _enrich_snapshot(snapshot_rows, quotes,
                                     {} if injected else _industry_map())
    items = _build_stock_items(changes_agg, snapshot_rows, quant_seats)  # 全量评分
    stocks = items  # 全量返回,前端分页展示
    smashed = _smashed_rows(items)
    sectors = _aggregate_sectors(items)
    gauge = _build_gauge(changes_agg)
    gauge["smashed_stocks"] = sum(1 for i in items if i.get("direction") == "砸盘")
    alerts = [{
        "code": s["code"], "name": s["name"], "industry": s["industry"],
        "activity": s["activity"], "level": s["level"],
        "badges": s["badges"], "reason": "；".join(s["reasons"][:2]) or "多机制并发",
    } for s in items if s["level_rank"] >= 3]
    market_prediction = _predict_market(gauge, futures_signal, volume)
    collective = [s["industry"] for s in sectors if s.get("collective") == "集体砸盘"][:3]
    if collective:
        market_prediction += f";{'、'.join(collective)} 疑似遭程序化集中抛售,相关板块防御为先"
    trade_key = _current_trade_date_key()
    payload = {
        "updated_at": now_iso, "live": True, "fallback_date": None,
        "snapshot_date": _iso(trade_key),
        "gauge": gauge, "stocks": stocks, "smashed": smashed,
        "sectors": sectors, "alerts": alerts,
        "market_prediction": market_prediction,
        "futures_signal": futures_signal,
        "volume_energy": volume,
        "knowledge": knowledge,
        "note": snapshot_note,
    }
    # 按交易日双写:kv 快照(温度计/板块/changes_agg + 股票/砸盘前列,控制单条体积) +
    # 按日榜单表(全部异动股 + 高活跃/砸盘项,全量,供按天搜索/回看补全/个股历史);
    # 回看时 _expand_with_day_rows 会用按日表把前列补全,周末重放锚定真实交易日。
    _save_day_snapshot(trade_key, {
        "gauge": gauge, "stocks": stocks[:300], "smashed": smashed[:100],
        "sectors": sectors, "alerts": alerts, "changes_agg": changes_agg,
    })
    try:
        from data_store import quant_radar_repo

        persist = [i for i in items
                   if (i.get("changes_total") or 0) > 0
                   or (i.get("activity") or 0) >= 30 or (i.get("smash") or 0) >= 40]
        quant_radar_repo.upsert_day(_iso(trade_key), persist)
    except Exception:
        pass
    if not injected:
        with _overview_lock:
            _overview_cache.update(ts=time.time(), payload=payload)
    return payload


def stock_analysis(code: str,
                   kline_fetcher: Optional[Callable[[str, int], List[Dict[str, Any]]]] = None,
                   fetch_changes: Optional[Callable[[], List[Dict[str, Any]]]] = None,
                   snapshot_dates: Optional[List[str]] = None,
                   futures_ctx_fn: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
                   volume_fn: Optional[Callable[[], Optional[Dict[str, Any]]]] = None) -> Dict[str, Any]:
    """个股量化行为深评:五机制评分明细 + 异动统计 + 量化席位 + 资金结构 + 市场环境 + 行为预测。

    ``kline_fetcher(code, limit) -> [日K记录]`` 由路由注入(STOCK_KLINE_SERVICE);
    异动实时为空时回退当日/最近 kv 快照里的 ``changes_agg``。
    市场环境=期指对冲(futures_rank kv,本地) + 大盘量能(新浪指数日K);注入
    ``fetch_changes`` 视为离线模式,未显式注入的量能源不再发网络。
    """
    code = str(code or "").strip().split(".")[0]
    if not code.isdigit() or len(code) != 6:
        return {"ok": False, "error": f"无效股票代码: {code!r}"}
    notes: List[str] = []
    changes_rows = (fetch_changes() if fetch_changes
                    else _with_deadline(15, _fetch_stock_changes)) or []
    agg = _aggregate_changes(changes_rows).get(code)
    changes_date = _current_trade_date_iso()
    if agg is None:
        date_key, snap = _load_recent_snapshot(snapshot_dates or _recent_day_keys(8))
        cached = ((snap or {}).get("changes_agg") or {}).get(code)
        if cached:
            agg = cached
            changes_date = _iso(date_key)
            notes.append(f"盘口异动为 {changes_date} 快照")
        else:
            agg = _empty_agg(code)
            notes.append("当前无盘口异动数据(非交易时段或该股无异动)")

    bars: List[Dict[str, Any]] = []
    if kline_fetcher is not None:
        try:
            bars = kline_fetcher(code, 120) or []
        except Exception:
            bars = []
    features = _bar_features(bars)
    if features is None:
        notes.append("无日K数据,影线/趋势类规则未参与评分")
    day_pct = features["pct_chg"] if features else None
    upper_shadow = features["upper_shadow_ratio"] if features else None

    flow = _stock_flow(code)
    seats_map = _quant_seats_window(30)
    seats = seats_map.get(code) or []
    if flow is None or not seats:
        # 本地资金流 / 龙虎榜席位缺失(moneyflow_dc / dragon_tiger 未回填或滞后)→
        # 同步按需补齐后再取一次,让「过了日期需手动补数据」在个股查看时自动完成
        # (个股完全没有数据时响应本来就缺这块,值得等一次补齐)
        try:
            from webui.services.capital_rankings_service import auto_backfill_if_stale
            kinds = tuple(k for k, missing in (("moneyflow", flow is None),
                                               ("dragon_tiger", not seats)) if missing)
            auto_backfill_if_stale(kinds=kinds)
            if flow is None:
                flow = _stock_flow(code)
            if not seats:
                seats = (_quant_seats_window(30) or {}).get(code) or []
        except Exception:  # noqa: BLE001
            pass
    else:
        # 个股数据齐了,但整库可能滞后(例如 App 几天没开,资金流停在几天前):
        # 后台按需补齐,冷却与补数上限由 auto_backfill 内部兜住,本次响应不等待。
        try:
            from webui.services.capital_rankings_service import auto_backfill_background
            auto_backfill_background(("moneyflow", "dragon_tiger"))
        except Exception:  # noqa: BLE001
            pass
    if flow is None:
        notes.append("无个股资金流数据(moneyflow_dc 未覆盖)")
    quote = None
    if fetch_changes is None:  # 在线模式:腾讯实时报价(量比进高频评分,亦作无日K时的涨跌兜底)
        quote = (_with_deadline(8, _fetch_tencent_quotes, [code]) or {}).get(code)
    if day_pct is None and quote:
        day_pct = quote.get("change_pct")

    # 市场环境:期指对冲(kv 本地读) + 大盘量能(新浪指数日K,10min 缓存)
    if futures_ctx_fn is not None:
        futures_ctx = futures_ctx_fn()
    else:
        futures_ctx = _futures_context() if fetch_changes is None else None
    if volume_fn is not None:
        volume = volume_fn()
    else:
        volume = (_with_deadline(10, _market_volume)) if fetch_changes is None else None
    if futures_ctx is None:
        notes.append("期指持仓上下文暂缺(打开股指期货页可积累缓存)")
    if volume is None:
        notes.append("大盘量能数据暂缺")

    spoof, spoof_r = _spoof_score(agg, pct_chg=day_pct, upper_shadow_ratio=upper_shadow)
    hft, hft_r = _hft_score(agg, volume_ratio=(quote or {}).get("volume_ratio"))
    orderbook, ob_r = _orderbook_score(agg, features=features)
    sentiment, se_r = _sentiment_score(agg, features=features)
    bias, bias_r = _bias_score(
        main_net_inflow=(flow or {}).get("main_net"),
        sm_net_inflow=(flow or {}).get("sm_net"),
        features=features, quant_seats=seats, pct_chg=day_pct)
    scores = {"spoof": spoof, "hft": hft, "orderbook": orderbook,
              "sentiment": sentiment, "bias": bias}
    reasons_map = {"spoof": spoof_r, "hft": hft_r, "orderbook": ob_r,
                   "sentiment": se_r, "bias": bias_r}
    comp = _composite(scores, has_quant_seat=bool(seats))
    smash, smash_r = _smash_score(agg, pct_chg=day_pct,
                                  volume_ratio=(quote or {}).get("volume_ratio"),
                                  main_net_inflow=(flow or {}).get("main_net"))
    direction = _direction_label(agg.get("bull") or 0, agg.get("bear") or 0, day_pct)
    predictions = _predict_stock(scores, agg) + _env_predictions(scores, futures_ctx, volume)
    if smash >= 60:
        predictions.insert(0, {
            "tag": "程序化出货", "mechanism": "smash", "score": smash,
            "text": "单边杀跌异动密集,疑似量化程序化集中出货;反弹接飞刀风险高,等杀跌异动频次明显回落再考虑",
        })
    accumulation: Optional[Dict[str, Any]] = None
    try:
        from analysis import accumulation_detector as _accum_det
        from data_store import moneyflow_repo

        _end = _dt.date.today()
        _start = _end - _dt.timedelta(days=120)   # 40交易日 ≈ 58自然日,留双倍余量
        _df = moneyflow_repo.get_stock_rows(code, _start.isoformat(), _end.isoformat(),
                                            snapshot_top_n=0)
        if _df is not None and not getattr(_df, "empty", True):
            accumulation = _accum_det.detect(_df.to_dict("records"), window=40)
    except Exception:
        accumulation = None
    if accumulation is None:
        notes.append("吸筹分析暂缺(本地资金流历史不足 12 个交易日)")
    history: List[Dict[str, Any]] = []
    try:
        from data_store import quant_radar_repo

        history = quant_radar_repo.get_stock_history(code, 60)
    except Exception:
        history = []
    knowledge = knowledge_payload()
    return {
        "ok": True,
        "code": code,
        "name": agg.get("name") or "",
        "updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "mechanisms": [{"key": m["key"], "name": m["name"],
                        "score": scores[m["key"]], "reasons": reasons_map[m["key"]]}
                       for m in MECHANISMS],
        **comp,
        "direction": direction,
        "smash": smash,
        "smash_reasons": smash_r,
        "predictions": predictions,
        "market_context": {"futures": futures_ctx, "volume": volume},
        "defense": knowledge["defense_rules"],
        "changes": {"date": changes_date, "total": agg.get("total") or 0,
                    "bull": agg.get("bull") or 0, "bear": agg.get("bear") or 0,
                    "counts": {k: v for k, v in (agg.get("counts") or {}).items()},
                    "labels": _KEY_LABELS,
                    "events": _timeline_events(agg)},
        "seats": seats,
        "flow": flow,
        "accumulation": accumulation,
        "history": history,
        "bars": _payload_bars(bars),
        "bars_used": len(bars),
        "notes": notes,
        "disclaimer": knowledge["disclaimer"],
    }


# ----------------------------- 收盘后自动落库守护 -----------------------------

_autosave_thread: Optional[threading.Thread] = None
_autosave_lock = threading.Lock()


def _day_closed(date_key: str) -> bool:
    """当日量化雷达是否已保存过收盘态快照(kv 标记 ``closed:YYYYMMDD``)。"""
    try:
        from data_store import kv_repo

        return kv_repo.get("quant_radar", f"closed:{date_key}") is not None
    except Exception:
        return False


def _mark_day_closed(date_key: str) -> None:
    try:
        from data_store import kv_repo

        kv_repo.set_("quant_radar", f"closed:{date_key}", 1)
    except Exception:
        pass


def _autosave_once(now: Optional[_dt.datetime] = None) -> bool:
    """收盘后把当日量化雷达榜单落库一次;当日已存过收盘态则跳过。

    判据必须是「收盘态标记」而不是「当日按日表是否为空」:盘中打开过雷达页
    就会留下盘中态的按日行,若把盘中态当成已保存,收盘态将永远缺席,个股
    「量化行为历史」里的当日数据会停在打开页面的那个时刻。返回是否执行了
    落库(没拿到当日 live 数据不打标,留待下次轮询重试)。
    """
    now = now or _dt.datetime.now()
    if now.weekday() >= 5:
        return False
    today_key = now.strftime("%Y%m%d")
    if _current_trade_date_key() != today_key or _day_closed(today_key):
        return False
    try:
        payload = overview(force=True)  # live 路径内部完成 kv + 按日表双写
        if payload.get("live"):
            _mark_day_closed(today_key)
            return True
    except Exception:
        pass
    return False


def _autosave_loop(after_hhmm: str, interval: float) -> None:
    while True:
        try:
            now = _dt.datetime.now()
            if now.weekday() < 5 and now.strftime("%H:%M") >= after_hhmm:
                _autosave_once(now)
        except Exception:
            pass
        time.sleep(interval)


def start_autosave() -> Optional[threading.Thread]:
    """启动『收盘后自动保存当日量化雷达榜单』守护线程(进程内只启一次)。

    保证页面当天没被打开(或盘中打开过、只有盘中态)也能把当日收盘态按天落库
    (收盘态标记 closed:YYYYMMDD 不存在才补跑一次)。环境变量:
    - KRONOS_DISABLE_QUANT_RADAR_AUTOSAVE=1  关闭
    - KRONOS_QUANT_RADAR_SAVE_AFTER=15:05    收盘保存时刻(HH:MM)
    - KRONOS_QUANT_RADAR_SAVE_INTERVAL=1800  轮询间隔秒(最低 300)
    """
    global _autosave_thread
    flag = os.environ.get("KRONOS_DISABLE_QUANT_RADAR_AUTOSAVE", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        print("[quant-radar-autosave] 已被 KRONOS_DISABLE_QUANT_RADAR_AUTOSAVE 关闭")
        return None
    with _autosave_lock:
        if _autosave_thread and _autosave_thread.is_alive():
            return _autosave_thread
        after = os.environ.get("KRONOS_QUANT_RADAR_SAVE_AFTER", "15:05").strip() or "15:05"
        try:
            interval = float(os.environ.get("KRONOS_QUANT_RADAR_SAVE_INTERVAL", "1800"))
        except (TypeError, ValueError):
            interval = 1800.0
        interval = max(300.0, interval)
        _autosave_thread = threading.Thread(
            target=_autosave_loop, args=(after, interval), daemon=True)
        _autosave_thread.start()
        print(f"[quant-radar-autosave] 已启动:交易日 {after} 后自动保存当日榜单,轮询 {int(interval)}s")
        return _autosave_thread
