"""资金榜单服务:主力净流入榜(moneyflow)+ 龙虎榜(dragon_tiger_list)。

提供单日 / 多日聚合查询、可选叠加实时报价、回填 N 天。报价(quote_provider)
与取数(fetcher / dates)均可注入,便于离线测试与数据源切换。

资金榜全市场快照统一落 moneyflow_dc 的哨兵 top_n=0,隔离 opportunity
discovery 写入的正 top_n 快照。
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Optional

import pandas as pd

from data_store import calendar_repo, dragon_tiger_list_repo, dragon_tiger_repo, market_daily_fetch, moneyflow_repo

logger = logging.getLogger(__name__)

SNAPSHOT_TOP_N = 0  # 资金榜全市场快照哨兵
MONEYFLOW_AMOUNT_UNIT = "万元"
_TS_INST_SIDE = {"0": "buy", "1": "sell", "buy": "buy", "sell": "sell"}
_QUANT_KEYWORDS = ("量化", "DMA", "程序化", "算法")

# ---------------------------------------------------------------------------
# 按需自动补齐：查询路径上发现最近 max_days 个交易日有缺失(含中间被跳过的交易日、
# 以及上次抓取被截断的残缺日)就自动回填,免得用户手动去点「补偿数据」。
# 冷却按 kind 分桶——资金榜单页会同时打 moneyflow 与 dragon_tiger 两个接口,
# 共用一个时间戳会让先到的那个独占补数机会、另一个永远补不上。
# ---------------------------------------------------------------------------
_auto_backfill_lock = threading.Lock()
_auto_backfill_last_ts: dict[str, float] = {}   # kind -> 上次触发时刻
AUTO_BACKFILL_COOLDOWN_SECS = 600.0
AUTO_BACKFILL_MAX_DAYS = 10   # 检查最近 10 个交易日
AUTO_BACKFILL_MAX_FETCH = 3   # 单次最多补几天(剩下的留给下次刷新,避免请求被整月回填拖死)
_history_attempts: dict[tuple[str, str], float] = {}
_PROVIDER_START = {"moneyflow": "2023-09-11", "dragon_tiger": "2005-01-01"}
_PROVIDER_NAME = {"moneyflow": "东财个股资金流向", "dragon_tiger": "龙虎榜每日明细"}

# 当日资金流/龙虎榜要收盘后才由 Tushare 发布。该时刻之前把「今天」算成缺口的话,
# 每次请求都会去拉一个注定拉不到的今天,还会吃掉冷却窗口,真正缺的历史交易日
# 反而永远排不上队。
AUTO_BACKFILL_PUBLISH_AFTER = "17:30"

# 全市场资金流正常约 5500~6000 行/天。判「某天已补齐」只看有没有行是不够的：
# 抓取被截断留下的残缺日(实测有过 550 行、1050 行的日子)会被永久当成已完成,
# 榜单从此在残缺的全集上排名。改为与近期各日行数的中位数比,低于一半即判残缺；
# 用相对值而非写死的行数,universe 增减不会让判据失准。
MONEYFLOW_COVERAGE_RATIO = 0.5


def _publish_cutoff_passed() -> bool:
    """当日资金流/龙虎榜是否已过 Tushare 发布时刻(可用环境变量覆盖)。"""
    cutoff = (os.environ.get("KRONOS_CAPITAL_PUBLISH_AFTER") or "").strip() \
        or AUTO_BACKFILL_PUBLISH_AFTER
    return _dt.datetime.now().strftime("%H:%M") >= cutoff


def _covered_dates(kind: str, dates: list[str]) -> set[str]:
    """``dates`` 中已经补齐的 ISO 日期。

    moneyflow 是全市场快照,按行数判完整(见 MONEYFLOW_COVERAGE_RATIO);
    龙虎榜每天上榜数天然只有几十条且逐日波动,套行数阈值会把正常日误判成
    残缺,故有行即算已补。
    """
    if kind != "moneyflow":
        return dragon_tiger_list_repo.existing_dates(dates)
    counts = moneyflow_repo.date_counts(dates, SNAPSHOT_TOP_N)
    if not counts:
        return set()
    ordered = sorted(counts.values())
    median = ordered[len(ordered) // 2]
    floor = max(1, int(median * MONEYFLOW_COVERAGE_RATIO))
    return {d for d, n in counts.items() if n >= floor}



def _bare_code(ts_code: str) -> str:
    return str(ts_code or "").split(".")[0].strip()


def _to_iso(d) -> str:
    """'YYYYMMDD' / Timestamp / 'YYYY-MM-DD' → 'YYYY-MM-DD'。"""
    if d is None:
        return ""
    s = str(d).strip()
    if re.fullmatch(r"\d{8}", s):
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    try:
        return pd.to_datetime(s).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return s


def _num(v):
    try:
        if v is None or pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _str(v) -> str:
    try:
        if v is None or pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v)


def _json_value(v):
    try:
        if v is None or pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(v, "item"):
        try:
            return v.item()
        except (ValueError, TypeError):
            pass
    return v if isinstance(v, (str, int, float, bool)) else str(v)


def _sum_nums(*values):
    nums = [_num(v) for v in values]
    nums = [v for v in nums if v is not None]
    return sum(nums) if nums else None


def _inst_amount_total(row: dict[str, Any]) -> float:
    buy = _num(row.get("buy_amount")) or 0.0
    sell = _num(row.get("sell_amount")) or 0.0
    return buy + sell


def _inst_name_is_generic(name: str) -> bool:
    return bool(re.fullmatch(r"机构(买入|卖出)\(\d+家\)", str(name or "").strip()))


def _classify_quant_name(name: str) -> tuple[int, float]:
    is_quant = int(any(k in (name or "") for k in _QUANT_KEYWORDS))
    return is_quant, 0.6 if is_quant else 0.0


def _inst_side(value) -> str:
    text = _str(value).strip()
    mapped = _TS_INST_SIDE.get(text)
    if mapped:
        return mapped
    n = _num(value)
    if n == 0:
        return "buy"
    if n == 1:
        return "sell"
    return text or "buy"


def _moneyflow_like(cols: set[str]) -> bool:
    return bool(
        {"buy_elg_amount", "buy_lg_amount", "buy_md_amount", "buy_sm_amount", "amount_unit"} & cols
    )


def _raw_payload(raw_json) -> dict[str, Any]:
    raw = _str(raw_json).strip()
    if not raw:
        return {}
    merged: dict[str, Any] = {}
    for payload in _raw_payload_rows(raw):
        for key, value in payload.items():
            if key not in merged or merged.get(key) in (None, ""):
                merged[key] = value
    return merged


def _raw_payload_rows(raw_json) -> list[dict[str, Any]]:
    raw = _str(raw_json).strip()
    if not raw:
        return []
    rows: list[dict[str, Any]] = []
    for part in raw.splitlines():
        part = part.strip()
        if not part:
            continue
        try:
            payload = json.loads(part)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            rows.append({str(k): _json_value(v) for k, v in payload.items()})
    return rows


def backfill_outcome(summary: dict) -> dict:
    """把各 kind 的回填结果汇总成 job 级判定。

    返回 ``{"rows": 总入库行数, "errors": 去重后的错误列表, "ok": bool}``。
    全部 0 行且有错误 → ``ok=False``(让无效 Token / 取数失败在「任务」里显形,
    不再静默成「回填完成 0 行」);只要有任意行入库即视为成功(部分日期失败仅记日志)。
    """
    total = 0
    errors: list[str] = []
    for kind, s in (summary or {}).items():
        s = s or {}
        total += int(s.get("rows") or 0)
        total += int(s.get("inst_rows") or 0)
        for item in s.get("errors") or []:
            msg = item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else str(item)
            label = f"{kind}: {msg}" if msg else str(kind)
            if label not in errors:
                errors.append(label)
        for item in s.get("inst_errors") or []:
            msg = item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else str(item)
            label = f"{kind}机构席位: {msg}" if msg else f"{kind}机构席位"
            if label not in errors:
                errors.append(label)
    return {"rows": total, "errors": errors, "ok": not (total == 0 and bool(errors))}


def auto_backfill_if_stale(kinds: tuple[str, ...] = ("moneyflow", "dragon_tiger"),
                           max_days: int = AUTO_BACKFILL_MAX_DAYS,
                           cooldown_secs: float = AUTO_BACKFILL_COOLDOWN_SECS,
                           max_fetch: int = AUTO_BACKFILL_MAX_FETCH) -> dict:
    """按需自动补齐：最近 ``max_days`` 个交易日里本地缺失的日期自动回填。

    「缺失」包含三种：从没抓过的日期、中间被跳过的交易日、以及上次抓取被截断
    只入库了零星几行的残缺日。每次最多补 ``max_fetch`` 天(最近的优先),剩下的
    留给下次刷新,免得一次查询被整月回填拖死。

    个股查看(stock_capital_summary / quant_radar stock_analysis)与榜单查询路径
    调用,让「过了日期要手动补数据」自动完成。冷却按 kind 分桶 + 进程锁,避免
    并发/频繁触发 Tushare 全市场拉取；Tushare 不可用 / 无缺失 / 拉取失败都静默
    降级返回 ``{"triggered": False, ...}``,绝不阻断查询。

    返回 ``{"triggered", "kinds", "missing", "remaining", "errors", "reason"}``。
    """
    idle = {"triggered": False, "kinds": [], "missing": {}, "remaining": 0, "errors": []}
    from data_store import tushare_client
    if not tushare_client.available():
        return {**idle, "reason": "tushare_unavailable"}

    now = time.time()
    with _auto_backfill_lock:
        due = [k for k in kinds
               if (now - _auto_backfill_last_ts.get(k, 0.0)) >= cooldown_secs]
    if not due:
        return {**idle, "reason": "cooldown"}

    trade_dates = tushare_client.recent_trade_dates(max_days) or []
    if not trade_dates:
        return {**idle, "reason": "no_cal"}
    if not _publish_cutoff_passed():
        today = _dt.date.today().strftime("%Y%m%d")
        trade_dates = [d for d in trade_dates if str(d) != today]
    if not trade_dates:
        return {**idle, "reason": "pending_publish"}

    # 逐个交易日检查缺口(YYYYMMDD 本地 ↔ ISO 入库)：不只比较「最新日期」,
    # 中间被跳过的交易日与残缺日同样会被发现。
    missing_by_kind: dict[str, list[str]] = {}
    for kind in due:
        covered = _covered_dates(kind, trade_dates)
        missing = [d for d in trade_dates if _to_iso(d) not in covered]
        if missing:
            missing_by_kind[kind] = missing
    if not missing_by_kind:
        return {**idle, "reason": "fresh"}

    budget = max(1, int(max_fetch))
    with _auto_backfill_lock:
        # 冷却在锁内再验一次(并发窗口保护),并按 kind 各自记账。
        planned = {k: v[:budget] for k, v in missing_by_kind.items()
                   if (now - _auto_backfill_last_ts.get(k, 0.0)) >= cooldown_secs}
        for kind in planned:
            _auto_backfill_last_ts[kind] = now
    if not planned:
        return {**idle, "reason": "cooldown"}

    remaining = sum(len(missing_by_kind[k]) - len(v) for k, v in planned.items())
    errors: list[str] = []
    for kind, dates in planned.items():
        try:
            summary = CapitalRankingsService().backfill(kinds=(kind,), dates=dates)
        except Exception as exc:  # noqa: BLE001 — 单个 kind 失败不阻断其余
            logger.warning("auto backfill %s failed: %s", kind, exc)
            errors.append(f"{kind}: {exc}")
            continue
        for item in (summary or {}).get(kind, {}).get("errors") or []:
            msg = item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else str(item)
            if msg:
                errors.append(f"{kind}: {msg}")
    return {"triggered": True, "kinds": list(planned), "missing": planned,
            "remaining": remaining, "errors": errors, "reason": "backfilled"}


_auto_backfill_bg_lock = threading.Lock()
_auto_backfill_bg_running: set[str] = set()


def auto_backfill_background(kinds: tuple[str, ...] = ("moneyflow", "dragon_tiger")) -> bool:
    """在后台线程里做按需补齐,不阻塞当前请求。

    榜单页用这个:补数是整批全市场拉取,同步做会把 HTTP 响应挂住十几秒;
    本次请求照常返回本地已有的数据,补上的部分下次刷新可见。
    返回是否真的起了线程(已有同类在跑 / Tushare 不可用时为 False)。
    """
    from data_store import tushare_client
    if not tushare_client.available():
        return False
    with _auto_backfill_bg_lock:
        todo = tuple(k for k in kinds if k not in _auto_backfill_bg_running)
        if not todo:
            return False
        _auto_backfill_bg_running.update(todo)

    def _work() -> None:
        try:
            auto_backfill_if_stale(kinds=todo)
        except Exception as exc:  # noqa: BLE001 — 后台补数失败只影响「数据还是旧的」
            logger.warning("background auto backfill failed: %s", exc)
        finally:
            with _auto_backfill_bg_lock:
                _auto_backfill_bg_running.difference_update(todo)

    threading.Thread(target=_work, name="capital-auto-backfill", daemon=True).start()
    return True



class CapitalRankingsService:
    def __init__(self, quote_provider: Optional[Callable[[list], dict]] = None,
                 quant_codes_fn: Optional[Callable[[], dict]] = None):
        # quote_provider(codes:list[str]) -> {bare_code: {price, change_pct, main_net_inflow,...}}
        self._quote_provider = quote_provider
        # quant_codes_fn() -> {"codes": set[6位代码], "as_of": str|None, "available": bool}
        # 「无量化」过滤口径,缺省 lazy import quant_radar_service.quant_active_codes
        self._quant_codes_fn = quant_codes_fn

    # ---------------- 查询 ----------------

    def moneyflow_ranking(
        self,
        date=None,
        days=1,
        top_n=50,
        mode="single",
        with_quotes=True,
        start_date=None,
        end_date=None,
        no_quant=False,
    ) -> dict:
        coverage = self._ranking_coverage("moneyflow", date, days, mode, start_date, end_date)
        fetch_n = self._fetch_limit(top_n, no_quant)
        if mode == "aggregate" and start_date and end_date:
            as_of = end_date
            df = moneyflow_repo.get_range_aggregated(
                start_date,
                end_date,
                limit=fetch_n,
                snapshot_top_n=SNAPSHOT_TOP_N,
                sort_by="net_amount",
            )
            return self._envelope("moneyflow", mode, as_of, days, top_n, df, with_quotes,
                                  start_date, end_date, no_quant=no_quant, coverage=coverage)
        if mode == "aggregate":
            as_of = date or moneyflow_repo.latest_date(SNAPSHOT_TOP_N)
            df = (moneyflow_repo.get_aggregated(as_of, days=days, limit=fetch_n,
                                                snapshot_top_n=SNAPSHOT_TOP_N)
                  if as_of else None)
        else:
            as_of = date or moneyflow_repo.latest_date(SNAPSHOT_TOP_N)
            df = (moneyflow_repo.get_ranking(as_of, limit=fetch_n, snapshot_top_n=SNAPSHOT_TOP_N)
                  if as_of else None)
        return self._envelope("moneyflow", mode, as_of, days, top_n, df, with_quotes,
                              no_quant=no_quant, coverage=coverage)

    def dragon_tiger_ranking(
        self,
        date=None,
        days=1,
        top_n=50,
        mode="single",
        with_quotes=True,
        start_date=None,
        end_date=None,
        no_quant=False,
    ) -> dict:
        coverage = self._ranking_coverage("dragon_tiger", date, days, mode, start_date, end_date)
        fetch_n = self._fetch_limit(top_n, no_quant)
        if mode == "aggregate" and start_date and end_date:
            as_of = end_date
            df = dragon_tiger_list_repo.get_range_aggregated(
                start_date,
                end_date,
                top_n=fetch_n,
                sort_by="l_buy",
            )
            return self._envelope("dragon_tiger", mode, as_of, days, top_n, df, with_quotes,
                                  start_date, end_date, no_quant=no_quant, coverage=coverage)
        if mode == "aggregate":
            as_of = date or dragon_tiger_list_repo.latest_date()
            df = (dragon_tiger_list_repo.get_aggregated(as_of, days=days, top_n=fetch_n)
                  if as_of else None)
        else:
            as_of = date or dragon_tiger_list_repo.latest_date()
            df = dragon_tiger_list_repo.get_top_n(as_of, fetch_n) if as_of else None
        return self._envelope("dragon_tiger", mode, as_of, days, top_n, df, with_quotes,
                              no_quant=no_quant, coverage=coverage)

    def _ranking_coverage(self, kind, date, days, mode, start_date, end_date):
        try:
            if mode == "aggregate" and start_date and end_date:
                return self._backfill_history(kind, start_date=start_date, end_date=end_date)
            if date:
                if mode == "aggregate" and days > 1:
                    return self._backfill_history(kind, date=date, days=days)
                return self._backfill_history(kind, start_date=date, end_date=date)
            auto_backfill_background((kind,))
        except Exception:  # noqa: BLE001 — 历史补数失败时依然读取原数据
            logger.warning("capital %s backfill skipped", kind, exc_info=True)
        return None

    def stock_capital_summary(
        self,
        ts_code: str,
        date=None,
        days=5,
        start_date=None,
        end_date=None,
        with_quotes=False,
    ) -> dict:
        """个股资金榜单摘要:主力净流入榜 + 龙虎榜。

        默认按 date/end_date 之前最近 days 个有数据交易日聚合;传入
        start_date/end_date 时改用显式日期区间。rank 保留全市场同窗口名次。

        查询前先按需自动补齐缺失交易日（auto_backfill_if_stale），让
        「过日期后需手动补数据」自动完成；补齐失败不影响本次查询降级返回。
        """
        try:
            days = max(1, min(120, int(days or 5)))
        except (TypeError, ValueError):
            days = 5
        code = _bare_code(ts_code)
        use_range = bool(start_date and end_date)
        mode = "range" if use_range else "aggregate"
        # 默认（近 N 日）视图：先按需补齐最近交易日；显式日期/区间由下面窗口补齐处理
        if not use_range and not date:
            try:
                auto_backfill_if_stale()
            except Exception:  # noqa: BLE001 — 自动补齐失败不阻断个股查询
                logger.warning("stock_capital_summary auto backfill skipped: %s", code)
        coverage = {}
        if use_range or date or end_date:
            for kind in _PROVIDER_START:
                coverage[kind] = self._ranking_coverage(kind, date or end_date, days, "aggregate", start_date, end_date)
        moneyflow = self._stock_moneyflow_section(
            ts_code,
            date=date,
            days=days,
            start_date=start_date,
            end_date=end_date,
            with_quotes=with_quotes,
        )
        dragon_tiger = self._stock_dragon_tiger_section(
            ts_code,
            date=date,
            days=days,
            start_date=start_date,
            end_date=end_date,
            with_quotes=with_quotes,
        )
        for section in (moneyflow, dragon_tiger):
            if status := coverage.get(section["kind"]):
                section["coverage"] = status
                section["reason"] = status["reason"] or section["reason"]
        has_rows = bool(moneyflow.get("row") or dragon_tiger.get("row"))
        latest_dates = [
            d for d in (moneyflow.get("latest_date"), dragon_tiger.get("latest_date")) if d
        ]
        return {
            "kind": "stock_capital_rankings",
            "code": code,
            "mode": mode,
            "days": days,
            "date": date,
            "start_date": start_date,
            "end_date": end_date,
            "as_of": end_date or date or (max(latest_dates) if latest_dates else None),
            "data_status": "fresh" if has_rows else "unavailable",
            "reason": None if has_rows else "所选区间暂无该股资金榜单记录。",
            "moneyflow": moneyflow,
            "dragon_tiger": dragon_tiger,
        }

    def _backfill_history(self, kind, *, date=None, days=1, start_date=None, end_date=None):
        from data_store import tushare_client

        first_available = _PROVIDER_START[kind]
        source = _PROVIDER_NAME[kind]
        start, end = _to_iso(start_date), _to_iso(end_date or date)
        status = {"provider": source, "provider_start": first_available, "requested_days": 0,
                  "stored_days": 0, "reason": None}
        if end and end < first_available:
            status["reason"] = f"{source}的来源覆盖从 {first_available} 起。所选日期早于该来源的覆盖。"
            return status

        online = tushare_client.available()
        lower = max(start, first_available) if start_date and end_date else start_date
        window = self._request_window(days, date, lower, end_date, allow_network=online)
        today = _dt.date.today().strftime("%Y%m%d")
        floor = first_available.replace("-", "")
        window = [d for d in window if d >= floor and (d < today or d == today and _publish_cutoff_passed())]
        if not window:
            status["reason"] = ("所选区间暂无可确认的已披露交易日。" if online else
                                "所选区间暂无本地交易日历。配置 TuShare 后可补历史榜单。")
            return status

        covered = _covered_dates(kind, window)
        missing = [d for d in window if _to_iso(d) not in covered]
        if missing and online:
            clock = time.monotonic()
            with _auto_backfill_lock:
                for key, expires in list(_history_attempts.items()):
                    if expires <= clock:
                        del _history_attempts[key]
                planned = [d for d in missing if (kind, d) not in _history_attempts][:AUTO_BACKFILL_MAX_FETCH]
                for d in planned:
                    _history_attempts[kind, d] = clock + AUTO_BACKFILL_COOLDOWN_SECS
            if planned:
                self.backfill(kinds=(kind,), dates=planned)
                covered = _covered_dates(kind, window)

        status["requested_days"], status["stored_days"] = len(window), len(covered)
        notes = []
        if start and start < first_available:
            notes.append(f"{source}的来源覆盖从 {first_available} 起。")
        if len(covered) < len(window):
            notes.append(f"所选区间有 {len(covered)}/{len(window)} 个交易日已入库。")
            if not online:
                notes.append("配置 TuShare 后可继续补历史榜单。")
        status["reason"] = "".join(notes) or None
        return status

    @staticmethod
    def _request_window(days: int, date=None, start_date=None, end_date=None, *, allow_network=True) -> list[str]:
        """Return requested sessions in YYYYMMDD, newest first."""
        full_range = bool(start_date and end_date)
        try:
            last = min(_dt.date.fromisoformat(_to_iso(end_date or date)), _dt.date.today())
            first = (_dt.date.fromisoformat(_to_iso(start_date)) if full_range else
                     last - _dt.timedelta(days=max(days * 2 + 8, 35)))
        except (ValueError, OverflowError):
            return []
        if first > last:
            return []
        start, end = first.strftime("%Y%m%d"), last.strftime("%Y%m%d")
        cached = calendar_repo.open_days()
        window = {d for d in cached if start <= d <= end}
        if allow_network:
            if not cached:
                window.update(market_daily_fetch.open_days(start, end))
            else:
                if start < cached[0] and (full_range or len(window) < days):
                    previous = (_dt.date.fromisoformat(_to_iso(cached[0])) - _dt.timedelta(days=1)).strftime("%Y%m%d")
                    window.update(market_daily_fetch.open_days(start, min(end, previous)))
                if end > cached[-1]:
                    following = (_dt.date.fromisoformat(_to_iso(cached[-1])) + _dt.timedelta(days=1)).strftime("%Y%m%d")
                    window.update(market_daily_fetch.open_days(max(start, following), end))
        if not window and first == last and first.weekday() < 5 and not (cached and cached[0] <= start <= cached[-1]):
            window.add(start)  # The requested single day's feed can verify it directly.
        result = sorted(window, reverse=True)
        return result if full_range else result[:days]

    @staticmethod
    def _fetch_limit(top_n, no_quant) -> int:
        """no_quant 时 over-fetch ×3(上限500),过滤后截回 top_n,避免榜单缩水。"""
        return min(int(top_n) * 3, 500) if no_quant else int(top_n)

    def _quant_code_info(self) -> dict:
        if self._quant_codes_fn is not None:
            return self._quant_codes_fn()
        try:
            from webui.services import quant_radar_service
            return quant_radar_service.quant_active_codes()
        except Exception:  # noqa: BLE001 — 量化口径缺失不能阻断榜单
            return {"codes": set(), "as_of": None, "available": False}

    def _envelope(self, kind, mode, as_of, days, top_n, df, with_quotes,
                  start_date=None, end_date=None, no_quant=False, coverage=None) -> dict:
        quant_info = self._quant_code_info() if no_quant else None
        quant_codes = (quant_info or {}).get("codes") or set()
        rows = self._normalize(df)
        quant_filtered = 0
        if quant_codes:
            before = len(rows)
            rows = [r for r in rows if r.get("code") not in quant_codes]
            quant_filtered = before - len(rows)
        rows = rows[:top_n]
        if with_quotes:
            rows = self._attach_quotes(rows)
        windows = (self._window_rankings(kind, as_of, top_n, with_quotes,
                                         quant_info=quant_info)
                   if as_of else {})
        return {
            "kind": kind,
            "mode": mode,
            "as_of": as_of,
            "days": days,
            "start_date": start_date,
            "end_date": end_date,
            "top_n": top_n,
            "count": len(rows),
            "coverage": coverage,
            "reason": (coverage or {}).get("reason") or ("所选区间暂无榜单记录。" if coverage and not rows else None),
            "rows": rows,
            "windows": windows,
            "no_quant": bool(no_quant),
            "quant_filtered": quant_filtered,
            "quant_criteria_available": (quant_info or {}).get("available") if no_quant else None,
            "quant_as_of": (quant_info or {}).get("as_of") if no_quant else None,
        }

    def _stock_moneyflow_section(
        self,
        ts_code: str,
        *,
        date=None,
        days=5,
        start_date=None,
        end_date=None,
        with_quotes=False,
    ) -> dict:
        latest = moneyflow_repo.latest_date(SNAPSHOT_TOP_N)
        if start_date and end_date:
            df = moneyflow_repo.get_stock_range_aggregated(
                ts_code,
                start_date,
                end_date,
                snapshot_top_n=SNAPSHOT_TOP_N,
            )
            section_start, section_end = start_date, end_date
        else:
            as_of = date or end_date or latest
            df = (
                moneyflow_repo.get_stock_aggregated(
                    ts_code,
                    end_date=as_of,
                    days=days,
                    snapshot_top_n=SNAPSHOT_TOP_N,
                )
                if as_of else None
            )
            section_start = None
            section_end = as_of
        rows = self._normalize(df)
        if with_quotes:
            rows = self._attach_quotes(rows)
        row = rows[0] if rows else None
        return {
            "kind": "moneyflow",
            "mode": "range" if start_date and end_date else "aggregate",
            "days": days,
            "start_date": section_start or (row or {}).get("first_date"),
            "end_date": section_end or (row or {}).get("last_date"),
            "latest_date": latest,
            "data_status": "fresh" if row else "unavailable",
            "reason": None if row else "该股未进入主力净流入榜数据窗口",
            "count": len(rows),
            "row": row,
            "rows": rows,
        }

    def _stock_dragon_tiger_section(
        self,
        ts_code: str,
        *,
        date=None,
        days=5,
        start_date=None,
        end_date=None,
        with_quotes=False,
    ) -> dict:
        latest = dragon_tiger_list_repo.latest_date()
        if start_date and end_date:
            df = dragon_tiger_list_repo.get_stock_range_aggregated(ts_code, start_date, end_date)
            section_start, section_end = start_date, end_date
        else:
            as_of = date or end_date or latest
            df = (
                dragon_tiger_list_repo.get_stock_aggregated(ts_code, end_date=as_of, days=days)
                if as_of else None
            )
            section_start = None
            section_end = as_of
        rows = self._normalize(df)
        if with_quotes:
            rows = self._attach_quotes(rows)
        row = rows[0] if rows else None
        return {
            "kind": "dragon_tiger",
            "mode": "range" if start_date and end_date else "aggregate",
            "days": days,
            "start_date": section_start or (row or {}).get("first_date"),
            "end_date": section_end or (row or {}).get("last_date"),
            "latest_date": latest,
            "data_status": "fresh" if row else "unavailable",
            "reason": None if row else "该股未进入龙虎榜数据窗口",
            "count": len(rows),
            "row": row,
            "rows": rows,
        }

    def _window_rankings(self, kind, as_of, top_n, with_quotes, quant_info=None) -> dict:
        quant_codes = (quant_info or {}).get("codes") or set()
        fetch_n = self._fetch_limit(top_n, bool(quant_codes))
        windows: dict[str, dict[str, Any]] = {}
        for days in (5, 30):
            if kind == "moneyflow":
                df = moneyflow_repo.get_aggregated(
                    as_of,
                    days=days,
                    limit=fetch_n,
                    snapshot_top_n=SNAPSHOT_TOP_N,
                    sort_by="net_amount",
                )
            else:
                df = dragon_tiger_list_repo.get_aggregated(
                    as_of,
                    days=days,
                    top_n=fetch_n,
                    sort_by="l_buy",
                )
            rows = self._normalize(df)
            quant_filtered = 0
            if quant_codes:
                before = len(rows)
                rows = [r for r in rows if r.get("code") not in quant_codes]
                quant_filtered = before - len(rows)
            rows = rows[:top_n]
            if with_quotes:
                rows = self._attach_quotes(rows)
            windows[str(days)] = {"days": days, "count": len(rows), "rows": rows,
                                  "quant_filtered": quant_filtered}
        return windows

    def _normalize(self, df) -> list:
        rows: list[dict[str, Any]] = []
        if df is None or getattr(df, "empty", True):
            return rows
        cols = set(df.columns)
        has_lc = "list_count" in cols
        institution_cache: dict[str, pd.DataFrame] = {}
        for i, (_, r) in enumerate(df.iterrows(), start=1):
            ts_code = _str(r.get("ts_code"))
            raw = {c: _json_value(r.get(c)) for c in df.columns}
            detail_rows = _raw_payload_rows(r.get("raw_json"))
            raw.update({k: _json_value(v) for k, v in _raw_payload(r.get("raw_json")).items()})
            moneyflow_row = _moneyflow_like(cols)
            amount_unit = _str(r.get("amount_unit")) if "amount_unit" in cols else ""
            if not amount_unit and moneyflow_row:
                amount_unit = MONEYFLOW_AMOUNT_UNIT
            if amount_unit and not raw.get("amount_unit"):
                raw["amount_unit"] = amount_unit
            if moneyflow_row:
                for detail in detail_rows:
                    if not detail.get("amount_unit") and not detail.get("_amount_unit"):
                        detail["amount_unit"] = amount_unit or MONEYFLOW_AMOUNT_UNIT
            main_buy_amount = (
                _num(r.get("main_buy_amount")) if "main_buy_amount" in cols
                else _sum_nums(r.get("buy_elg_amount"), r.get("buy_lg_amount"))
            )
            retail_buy_amount = (
                _num(r.get("retail_buy_amount")) if "retail_buy_amount" in cols
                else _sum_nums(r.get("buy_md_amount"), r.get("buy_sm_amount"))
            )
            rank_value = _num(r.get("market_rank")) if "market_rank" in cols else None
            row = {
                "rank": int(rank_value) if rank_value is not None else i,
                "code": _bare_code(ts_code),
                "ts_code": ts_code,
                "name": _str(r.get("name")),
                "trade_date": _str(r.get("trade_date")) if "trade_date" in cols else None,
                "net_amount": _num(r.get("net_amount")),
                "net_amount_rate": _num(r.get("net_amount_rate")) if "net_amount_rate" in cols else None,
                "buy_elg_amount": _num(r.get("buy_elg_amount")) if "buy_elg_amount" in cols else None,
                "buy_elg_amount_rate": _num(r.get("buy_elg_amount_rate")) if "buy_elg_amount_rate" in cols else None,
                "buy_lg_amount": _num(r.get("buy_lg_amount")) if "buy_lg_amount" in cols else None,
                "buy_lg_amount_rate": _num(r.get("buy_lg_amount_rate")) if "buy_lg_amount_rate" in cols else None,
                "buy_md_amount": _num(r.get("buy_md_amount")) if "buy_md_amount" in cols else None,
                "buy_md_amount_rate": _num(r.get("buy_md_amount_rate")) if "buy_md_amount_rate" in cols else None,
                "buy_sm_amount": _num(r.get("buy_sm_amount")) if "buy_sm_amount" in cols else None,
                "buy_sm_amount_rate": _num(r.get("buy_sm_amount_rate")) if "buy_sm_amount_rate" in cols else None,
                "main_buy_amount": main_buy_amount,
                "retail_buy_amount": retail_buy_amount,
                "amount_unit": amount_unit,
                "close": _num(r.get("close")) if "close" in cols else None,
                "pct_change": _num(r.get("pct_change")) if "pct_change" in cols else None,
                "list_count": (int(r["list_count"]) if has_lc and not pd.isna(r["list_count"]) else None),
                "first_date": _str(r.get("first_date")) if "first_date" in cols else None,
                "last_date": _str(r.get("last_date")) if "last_date" in cols else None,
                "turnover_rate": _num(r.get("turnover_rate")) if "turnover_rate" in cols else None,
                "amount": _num(r.get("amount")) if "amount" in cols else None,
                "l_buy": _num(r.get("l_buy")) if "l_buy" in cols else None,
                "l_sell": _num(r.get("l_sell")) if "l_sell" in cols else None,
                "l_amount": _num(r.get("l_amount")) if "l_amount" in cols else None,
                "net_rate": _num(r.get("net_rate")) if "net_rate" in cols else None,
                "amount_rate": _num(r.get("amount_rate")) if "amount_rate" in cols else None,
                "reason_count": (int(r["reason_count"]) if "reason_count" in cols and not pd.isna(r["reason_count"]) else None),
                "reason": _str(r.get("reason")) if "reason" in cols else "",
                "last_price": None,
                "change_pct": None,
                "quoted": False,
                "detail_rows": detail_rows,
                "raw": raw,
            }
            row.update(self._institution_summary(row, institution_cache))
            rows.append(row)
        return rows

    def _institution_summary(self, row: dict[str, Any], cache: dict[str, pd.DataFrame]) -> dict[str, Any]:
        ts_code = row.get("ts_code") or row.get("code")
        if not ts_code:
            return {"institution_rows": []}
        start = _to_iso(row.get("trade_date") or row.get("first_date") or "")
        end = _to_iso(row.get("trade_date") or row.get("last_date") or row.get("first_date") or "")
        if not start and not end:
            return {"institution_rows": []}
        try:
            df = cache.get(str(ts_code))
            if df is None:
                df = dragon_tiger_repo.get_by_code(str(ts_code))
                cache[str(ts_code)] = df
        except Exception as exc:  # noqa: BLE001 - 机构席位缺失不能阻断榜单
            logger.warning("capital rankings institution attach failed %s: %s", ts_code, exc)
            return {"institution_rows": []}
        if df is None or getattr(df, "empty", True):
            return {"institution_rows": []}

        work = df.copy()
        if "trade_date" in work.columns:
            work["trade_date"] = work["trade_date"].map(_to_iso)
        if start and end:
            work = work[(work["trade_date"] >= start) & (work["trade_date"] <= end)]
        elif start:
            work = work[work["trade_date"] >= start]
        elif end:
            work = work[work["trade_date"] <= end]
        if work.empty:
            return {"institution_rows": []}

        records: list[dict[str, Any]] = []
        for _, inst in work.sort_values(
            by=["trade_date", "buy_amount", "net_amount"],
            ascending=[False, False, False],
            na_position="last",
        ).iterrows():
            buy = _num(inst.get("buy_amount"))
            sell = _num(inst.get("sell_amount"))
            net = _num(inst.get("net_amount"))
            quant_confidence = _num(inst.get("quant_confidence"))
            if quant_confidence is None:
                quant_text = _str(inst.get("quant_confidence")).strip()
                quant_confidence = quant_text or None
            if net is None and (buy is not None or sell is not None):
                net = (buy or 0.0) - (sell or 0.0)
            records.append({
                "trade_date": _to_iso(inst.get("trade_date")),
                "inst_name": _str(inst.get("inst_name")),
                "side": _str(inst.get("side")),
                "buy_amount": buy,
                "sell_amount": sell,
                "net_amount": net,
                "is_quant": int(_num(inst.get("is_quant")) or 0),
                "quant_confidence": quant_confidence,
                "reason": _str(inst.get("reason")),
            })
        if not records:
            return {"institution_rows": []}

        buy_total = sum((_num(r.get("buy_amount")) or 0.0) for r in records)
        sell_total = sum((_num(r.get("sell_amount")) or 0.0) for r in records)
        net_total = sum((_num(r.get("net_amount")) or 0.0) for r in records)
        amount_total = buy_total + sell_total
        daily_rows = self._institution_daily_summary(records)

        by_name: dict[str, dict[str, Any]] = {}
        for inst in records:
            name = _str(inst.get("inst_name")).strip()
            if not name:
                continue
            item = by_name.setdefault(name, {"name": name, "buy": 0.0, "net": 0.0, "amount": 0.0})
            item["buy"] += _num(inst.get("buy_amount")) or 0.0
            item["net"] += _num(inst.get("net_amount")) or 0.0
            item["amount"] += _inst_amount_total(inst)
        name_items = list(by_name.values())
        specific_names = [item for item in name_items if not _inst_name_is_generic(item["name"])]
        if specific_names:
            name_items = specific_names
        name_items.sort(key=lambda x: (x["buy"], x["net"], x["amount"], x["name"]), reverse=True)
        display_names = [item["name"] for item in name_items[:8]]
        if len(name_items) > 8:
            display_names.append(f"等{len(name_items)}家")

        return {
            "institution_rows": records,
            "institution_daily_rows": daily_rows,
            "institution_buy_amount": buy_total,
            "institution_sell_amount": sell_total,
            "institution_net_amount": net_total,
            "institution_amount": amount_total,
            "institution_count": len(by_name),
            "main_institution_names": " / ".join(display_names),
        }

    @staticmethod
    def _institution_daily_summary(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_date: dict[str, dict[str, Any]] = {}
        names_by_date: dict[str, set[str]] = {}
        buy_names_by_date: dict[str, set[str]] = {}
        for inst in records:
            trade_date = _str(inst.get("trade_date")).strip()
            if not trade_date:
                continue
            item = by_date.setdefault(trade_date, {
                "trade_date": trade_date,
                "institution_buy_amount": 0.0,
                "institution_sell_amount": 0.0,
                "institution_net_amount": 0.0,
                "institution_amount": 0.0,
                "institution_count": 0,
                "buy_institution_count": 0,
            })
            buy = _num(inst.get("buy_amount")) or 0.0
            sell = _num(inst.get("sell_amount")) or 0.0
            net = _num(inst.get("net_amount")) or (buy - sell)
            item["institution_buy_amount"] += buy
            item["institution_sell_amount"] += sell
            item["institution_net_amount"] += net
            item["institution_amount"] += buy + sell
            name = _str(inst.get("inst_name")).strip()
            if name:
                names_by_date.setdefault(trade_date, set()).add(name)
                if buy > 0:
                    buy_names_by_date.setdefault(trade_date, set()).add(name)
        for trade_date, item in by_date.items():
            item["institution_count"] = len(names_by_date.get(trade_date, set()))
            item["buy_institution_count"] = len(buy_names_by_date.get(trade_date, set()))
        return sorted(by_date.values(), key=lambda x: x["trade_date"], reverse=True)

    def _attach_quotes(self, rows: list) -> list:
        if not self._quote_provider or not rows:
            return rows
        codes = [r["code"] for r in rows if r["code"]]
        try:
            qmap = self._quote_provider(codes) or {}
        except Exception as exc:  # noqa: BLE001 — 报价失败降级,不阻断榜单
            logger.warning("capital rankings quote attach failed: %s", exc)
            return rows
        for r in rows:
            q = qmap.get(r["code"])
            if not q:
                continue
            r["last_price"] = _num(q.get("price"))
            r["change_pct"] = _num(q.get("change_pct"))
            mni = _num(q.get("main_net_inflow"))
            if mni is not None:
                r["live_main_net_inflow"] = mni
            r["quoted"] = True
        return rows

    # ---------------- 回填 ----------------

    def backfill(self, kinds=("moneyflow", "dragon_tiger"), days=30, *, dates=None,
                 moneyflow_fetcher=None, dragon_tiger_fetcher=None,
                 dragon_tiger_inst_fetcher=None, pro=None) -> dict:
        explicit_dates = dates is not None
        if dates is None:
            from data_store import tushare_client
            dates = tushare_client.recent_trade_dates(days) or []
        if not dates:
            # 拿不到任何交易日:多半是 Token 无效(trade_cal 被拒)或没配 Token。
            # 显式报错而非静默 0 行,否则 UI 只会看到「回填完成」却「暂无数据」。
            from data_store import tushare_client
            reason = (
                "Tushare Token 无效或 trade_cal 调用失败,无法获取交易日历。请在「设置」中检查 Tushare Token。"
                if tushare_client.available()
                else "Tushare 未配置或不可用(缺少 Token)。请在「设置」中配置有效的 Tushare Token。"
            )
            return {k: {"dates": 0, "ok_dates": 0, "rows": 0, "errors": [("", reason)]}
                    for k in kinds}
        summary: dict[str, Any] = {}
        if "moneyflow" in kinds:
            mf_dates = dates
            mf_skipped: list[str] = []
            if not explicit_dates:
                existing = moneyflow_repo.existing_dates(dates, SNAPSHOT_TOP_N)
                mf_dates = [d for d in dates if _to_iso(d) not in existing]
                mf_skipped = [d for d in dates if _to_iso(d) in existing]
            summary["moneyflow"] = self._backfill_moneyflow(mf_dates, moneyflow_fetcher, pro)
            summary["moneyflow"]["requested_dates"] = len(dates)
            summary["moneyflow"]["skipped_dates"] = len(mf_skipped)
        if "dragon_tiger" in kinds:
            from analysis.institutional import dragon_tiger_list_provider
            summary["dragon_tiger"] = dragon_tiger_list_provider.backfill_recent(
                dates=dates, fetcher=dragon_tiger_fetcher, pro=pro, skip_existing=not explicit_dates,
            )
            inst_summary = self._backfill_dragon_tiger_inst(
                dates,
                fetcher=dragon_tiger_inst_fetcher,
                pro=pro,
                skip_when_list_fetcher_injected=dragon_tiger_fetcher is not None,
            )
            if inst_summary:
                summary["dragon_tiger"]["inst_rows"] = inst_summary.get("rows", 0)
                summary["dragon_tiger"]["inst_ok_dates"] = inst_summary.get("ok_dates", 0)
                summary["dragon_tiger"]["inst_errors"] = inst_summary.get("errors", [])
        return summary

    def _backfill_moneyflow(self, dates, fetcher, pro) -> dict:
        if fetcher is None:
            if pro is None:
                from data_store import tushare_client
                pro = tushare_client.get_pro()
            if pro is None:
                return {"dates": 0, "ok_dates": 0, "rows": 0,
                        "errors": [("", "tushare pro unavailable")]}

            def fetcher(d):  # noqa: E306
                if hasattr(pro, "moneyflow_dc"):
                    return pro.moneyflow_dc(trade_date=d)
                return pro.moneyflow_ths(trade_date=d)

        total_rows = 0
        ok = 0
        errors: list[tuple[str, str]] = []
        for d in dates:
            try:
                df = fetcher(d)
            except Exception as exc:  # noqa: BLE001
                errors.append((str(d), str(exc)))
                logger.warning("moneyflow_dc fetch failed %s: %s", d, exc)
                continue
            ok += 1
            if df is None or len(df) == 0:
                continue
            work = df.copy()
            if "trade_date" in work.columns:
                work["trade_date"] = work["trade_date"].map(_to_iso)
            else:
                work["trade_date"] = _to_iso(d)
            if "_amount_unit" not in work.columns and "amount_unit" not in work.columns:
                work["_amount_unit"] = MONEYFLOW_AMOUNT_UNIT
            total_rows += moneyflow_repo.upsert_df(work, top_n=SNAPSHOT_TOP_N)

        from data_store import sync_log_repo
        status = "ok" if not errors else ("partial" if ok else "failed")
        sync_log_repo.append("capital_moneyflow", "", status=status, rows=total_rows,
                             error=("; ".join(f"{dt}:{m}" for dt, m in errors) or None))
        return {"dates": len(dates), "ok_dates": ok, "rows": total_rows, "errors": errors}

    def _backfill_dragon_tiger_inst(
        self,
        dates,
        *,
        fetcher=None,
        pro=None,
        skip_when_list_fetcher_injected: bool = False,
    ) -> dict | None:
        """同步 Tushare top_inst 机构席位,让榜单明细能列出机构名称和买卖额。"""
        if fetcher is None:
            if pro is None:
                if skip_when_list_fetcher_injected:
                    return None
                from data_store import tushare_client
                pro = tushare_client.get_pro()
            if pro is None or not hasattr(pro, "top_inst"):
                return None

            def fetcher(d):  # noqa: E306
                return pro.top_inst(trade_date=d)

        total_rows = 0
        ok = 0
        errors: list[tuple[str, str]] = []
        for d in dates:
            try:
                df = fetcher(d)
            except Exception as exc:  # noqa: BLE001
                errors.append((str(d), str(exc)))
                logger.warning("top_inst fetch failed %s: %s", d, exc)
                continue
            ok += 1
            if df is None or len(df) == 0:
                continue
            rows: list[dict[str, Any]] = []
            for _, r in df.iterrows():
                code = _str(r.get("ts_code")).strip()
                inst_name = (_str(r.get("exalter")).strip() or _str(r.get("inst_name")).strip())
                if not code or not inst_name:
                    continue
                is_quant = _num(r.get("is_quant"))
                quant_confidence = _num(r.get("quant_confidence"))
                if is_quant is None:
                    is_quant, fallback_conf = _classify_quant_name(inst_name)
                    if quant_confidence is None:
                        quant_confidence = fallback_conf
                rows.append({
                    "ts_code": code,
                    "trade_date": _to_iso(r.get("trade_date") or d),
                    "inst_name": inst_name,
                    "side": _inst_side(r.get("side")),
                    "net_amount": _num(r.get("net_buy")) if "net_buy" in df.columns else _num(r.get("net_amount")),
                    "buy_amount": _num(r.get("buy")) if "buy" in df.columns else _num(r.get("buy_amount")),
                    "sell_amount": _num(r.get("sell")) if "sell" in df.columns else _num(r.get("sell_amount")),
                    "is_quant": int(is_quant or 0),
                    "quant_confidence": quant_confidence,
                    "reason": _str(r.get("reason")),
                })
            total_rows += dragon_tiger_repo.upsert_rows(rows)
        return {"dates": len(dates), "ok_dates": ok, "rows": total_rows, "errors": errors}
