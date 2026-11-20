"""大盘体检：两融全景、公开资金证据。只读查询与后台采集分开。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
from collections import defaultdict
from functools import lru_cache
import gzip
import json
import logging
import re
from pathlib import Path
import sqlite3
import threading

from analysis.market_capital import capital_report, etf_predecessors, etf_rows, margin_report, participant_history, security_code, text
from analysis.market_health import number
from analysis.market_health_fusion import iso, margin_metrics
from data_store import calendar_repo, kv_repo, quant_radar_repo, tushare_client
from data_store.connection import close_conn, get_conn
from webui.services.market_health_service import now, read_baseline

NAMESPACE = "market_capital"
logger = logging.getLogger(__name__)
BASELINE = Path(__file__).resolve().parents[2] / "config/market_capital_baseline.json.gz"
_locks = {key: threading.Lock() for key in ("financing", "participants")}
PARTICIPANTS = {"main", "elg", "lg", "md", "sm", "institution", "etf", "national", "quant"}
DAILY_SOURCES = {"flows": "moneyflow_dc", "institutions": "top_inst"}
PARTICIPANTS_SEED = "seed:participants:v1"
NATIONAL_NAMES = ("中央汇金投资有限责任公司", "中央汇金资产管理有限责任公司", "中国证券金融股份有限公司", "中国国新控股有限责任公司", "中国诚通控股集团有限公司")


class RefreshConflict(ValueError):
    pass


def _records(method, cap=5000, **params):
    cap = min(cap, {"fund_share": 2000, "fund_nav": 2000, "top10_floatholders": 2000}.get(method, cap))
    pro = tushare_client.get_pro()
    if pro is None:
        raise ValueError("两融和资金研究需要现有TuShare配置，请在后台配置检查Token")
    result, seen = [], set()
    for offset in range(0, 200000, cap):
        frame = getattr(pro, method)(**params, limit=cap, offset=offset)
        rows = [] if frame is None else frame.to_dict("records")
        if not rows:
            return result
        keys = {json.dumps(r, sort_keys=True, default=str) for r in rows}
        if keys & seen:
            raise ValueError(f"{method}分页重复，未将截断结果作为全量")
        seen.update(keys)
        result.extend(rows)
        if len(rows) < cap:
            return result
    raise ValueError(f"{method}分页超出上限，原研究保留")


def _read(key):
    hit = kv_repo.get(NAMESPACE, key)
    return hit[0] if hit else None


@lru_cache(maxsize=1)
def _baseline():
    try:
        with gzip.open(BASELINE, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, EOFError):
        return {}


def _group_days(rows):
    days = defaultdict(list)
    for row in rows:
        day = iso(row.get("trade_date"))
        if day:
            days[day].append(row)
    return days


def _seed_participants():
    if _read(PARTICIPANTS_SEED) is not None:
        return
    data = _baseline().get("participants")
    if not data:
        return
    # Keep the shipped partitions below both live partitions and old full snapshots.
    # A failed first install must be retryable without publishing a partial catalog.
    conn = get_conn()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if _read(PARTICIPANTS_SEED) is not None:
            return
        for key, method in DAILY_SOURCES.items():
            days = _group_days(data.get(key, []))
            for day in data.get(f"{key}_days", []):
                if day and iso(day) == day:
                    days.setdefault(day, [])
            for day, rows in days.items():
                kv_repo.set_(NAMESPACE, f"seed:{method}:{day}", rows)
        partitioned = {*DAILY_SOURCES, *(f"{key}_days" for key in DAILY_SOURCES)}
        kv_repo.seed_raw(NAMESPACE, "participants", lambda: {k: v for k, v in data.items() if k not in partitioned})
        kv_repo.set_(NAMESPACE, PARTICIPANTS_SEED, True)


def bundle(kind, *, include_daily=False):
    if kind == "participants":
        _seed_participants()
    saved = _read(kind)
    if saved is None and kind == "financing" and (seed := _baseline().get(kind)):
        raw = kv_repo.seed_raw(NAMESPACE, kind, lambda: seed)
        saved = json.loads(raw) if raw else None
    if saved and kind == "participants" and include_daily:
        return _range_evidence(saved, "0000", "9999", DAILY_SOURCES)
    return saved or {}


def _daily_keys(method, provenance="raw"):
    prefix = f"{provenance}:{method}:"
    return {key[len(prefix):] for key in kv_repo.range_keys(NAMESPACE, prefix + '0000', prefix + '9999')}


def _archive_legacy(data):
    # A pre-partition snapshot may be the only surviving copy of an entire day.
    for key, method in DAILY_SOURCES.items():
        if not data.get(key):
            continue
        known = _daily_keys(method)
        for day, rows in _group_days(data[key]).items():
            if day not in known:
                kv_repo.set_(NAMESPACE, f"raw:{method}:{day}", rows)


def _range_evidence(data, start, end, keys):
    """Priority per whole market day: raw partition > legacy snapshot > bundled baseline."""
    result = dict(data)
    dates = set(data.get("calendar", [])) | set(data.get("evidence_calendar", []))
    dates.update(iso(d) for d in calendar_repo.open_days())
    bounds = {}
    for key in keys:
        method = DAILY_SOURCES[key]
        prefix = f"raw:{method}:"
        seed_prefix = f"seed:{method}:"
        base = _daily_keys(method, "seed")
        legacy = _group_days(data.get(key, []))
        known = _daily_keys(method) | base | legacy.keys()
        dates.update(known)
        bounds[key] = (min(known, default=None), max(known, default=None))
        days = {}
        if start and end:
            days.update((k[len(seed_prefix):], rows) for k, rows in kv_repo.range_items(NAMESPACE, seed_prefix + start, seed_prefix + end))
            days.update((d, rows) for d, rows in legacy.items() if start <= d <= end)
            days.update((k[len(prefix):], rows) for k, rows in kv_repo.range_items(NAMESPACE, prefix + start, prefix + end))
        result[key] = [row for d in sorted(days) for row in days[d]]
        result[key + '_days'] = sorted(days)
    # Legacy ETF snapshots infer their predecessors from their *own* calendar.
    result['etf_predecessors'] = etf_predecessors(data)
    result.update(calendar=sorted(d for d in dates if d), evidence_bounds=bounds)
    return result


def _status(kind, jobs):
    job_id = _read("job:" + kind)
    return jobs.get(job_id) if jobs and job_id else None


def financing(jobs=None, **params):
    window = int(params.get("window", 20))
    deposit = float(params.get("deposit", 80))
    warning, closeout = float(params.get("warning", 150)), float(params.get("closeout", 130))
    if window not in (5, 10, 20) or not 20 <= deposit <= 300 or not 100 <= closeout < warning <= 300:
        raise ValueError("成本窗口为5/10/20日，保证金20–300%，情景线须满足100%≤平仓线<预警线≤300%")
    data = bundle("financing")
    if not data:
        records = (read_baseline().get("cross", {}).get("leverage") or {}).get("records", [])
        history = margin_metrics(records)
        data = {"margin": records, "as_of": history[-1]["date"] if history else None, "sources": [], "errors": ["个股明细尚未采集；现有两融汇总保留原数据日，请更新两融研究"]}
    report = margin_report(data, window=window, deposit=deposit, warning=warning, closeout=closeout)
    report.update(job=_status("financing", jobs), updated_at=data.get("updated_at"), sources=data.get("sources", []), errors=data.get("errors", []),
                  today=now().date().isoformat(), disclosure="交易所通常次日披露；深市周五数据可能在下周一更新。沪深汇总须两所同日完整。个股与ETF口径分别展示，融券数量不跨标的相加。行业为本次采集的当前股票分类。")
    return report


def participants(jobs=None, **params):
    participant = params.get("participant", "main")
    if participant not in PARTICIPANTS:
        raise ValueError("未知资金类型")
    start, end = params.get("start") or None, params.get("end") or None
    if any(value and iso(value) != value for value in (start, end)) or start and end and start > end:
        raise ValueError("日期须为YYYY-MM-DD，开始日期不能晚于结束日期")
    if end and end > now().date().isoformat():
        raise ValueError("结束日期不能晚于今日")
    data = bundle("participants")
    source_key = {"institution": "institutions", "etf": "etf", "national": "national", "quant": "quant"}.get(participant, "flows")
    source = next((r for r in data.get("sources", []) if r["key"] == source_key), {})
    if not end:
        end = source.get("as_of") or data.get("flow_as_of") or data.get("as_of")
        if start:
            end = max(start, end or start)
    if not start and participant == "national":
        start = min((r["as_of"] for r in data.get("national", []) if r.get("as_of") and r["as_of"] <= (end or "")), default=end)
    if participant != 'etf':
        data = _range_evidence(data, start or end, end, DAILY_SOURCES)
    report = capital_report(data, start=start, end=end, participant=participant,
                            keyword=str(params.get("keyword") or "")[:100], sector=str(params.get("sector") or "")[:100])
    report["data_as_of"] = source.get("as_of") or max((iso(r.get("trade_date") or r.get("as_of")) or "" for r in data.get(source_key, [])), default=None)
    if source_key in DAILY_SOURCES:
        report['data_as_of'] = data['evidence_bounds'][source_key][1] or report['data_as_of']
    etf = participant == "etf"
    report.update(job=_status("participants", jobs), updated_at=(data.get("etf_updated_at") if etf else None) or data.get("updated_at"),
                  errors=data.get("etf_errors", data.get("errors", [])) if etf else data.get("errors", []), today=now().date().isoformat(),
                  disclosure="资金类型可能重叠，不可加总为市场增量资金。大单/小单为订单规模；ETF为份额变化×同日净值的申赎估算；机构仅限龙虎榜公开单日席位；国家队为已公告持仓；量化为公开行为代理。")
    return report


def history(*, participant="main", code="", start="", end=""):
    """只读当前资金证据；图表日期独立于榜单，不触发采集。"""
    code = security_code(code)
    if participant not in PARTICIPANTS:
        raise ValueError("未知资金类型")
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
        raise ValueError("请输入有效证券代码")
    if any(value and iso(value) != value for value in (start, end)) or start and end and start > end:
        raise ValueError("日期须为YYYY-MM-DD，开始日期不能晚于结束日期")
    if end and end > now().date().isoformat():
        raise ValueError("结束日期不能晚于今日")
    data = bundle("participants")
    key = {"institution": "institutions", "etf": "etf", "national": "national", "quant": "quant"}.get(participant, "flows")
    source = next((r for r in data.get("sources", []) if r["key"] == key), {})
    end = end or source.get("as_of") or data.get("flow_as_of") or data.get("as_of")
    start = start or end
    if not start or not end:
        raise ValueError("尚无日期可查询，请先更新资金研究")
    if start > end or (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days > 3660:
        raise ValueError("开始日期不能晚于结束日期，单次查询最长十年")
    if key in DAILY_SOURCES:
        data = _range_evidence(data, start, end, [key])
        calendar = data['calendar']
    else:
        calendar = sorted({d for d in [*(data.get("calendar") or []), *(iso(d) for d in calendar_repo.open_days())] if d})
    quant = quant_radar_repo.get_stock_range(code, start, end, calendar) if participant == "quant" else []
    report = participant_history(data, participant=participant, code=code, start=start, end=end, calendar=calendar, quant=quant)
    if key in DAILY_SOURCES:
        report['available_start'], report['available_end'] = data['evidence_bounds'][key]
    if participant == "quant":
        first, last = quant_radar_repo.date_bounds()
        report["available_start"] = min(filter(None, (first, report["available_start"])), default=None)
        report["available_end"] = max(filter(None, (last, report["available_end"])), default=None)
    report.update(today=now().date().isoformat(), source=source,
                  updated_at=(data.get("etf_updated_at") if participant == "etf" else None) or data.get("updated_at"))
    return report


def _day(method, date, fields=None):
    key = f"raw:{method}:{date}"
    cached = _read(key)
    # 最新三个自然日重试披露修订；更早成功记录直接复用。
    if cached is not None and date < (now().date() - dt.timedelta(days=3)).isoformat():
        return cached
    try:
        params = {"trade_date": date.replace("-", "")}
        if fields:
            params["fields"] = fields
        rows = [r for r in _records(method, **params) if iso(r.get("trade_date")) == date]
        if rows:
            # 丢弃pandas/numpy非原生值，NaN统一缺失。
            rows = [{k: (v if isinstance(v, str) else number(v)) for k, v in r.items()} for r in rows]
            kv_repo.set_(NAMESPACE, key, rows)
            return rows
        if method == 'top_inst' and cached is None and date < now().date().isoformat():
            kv_repo.set_(NAMESPACE, key, [])  # Only a completed disclosure day can establish an empty partition.
        return cached or []
    finally:
        close_conn()


def _merge_etf(*batches):
    """同日同基金逐字段补全；有效修订覆盖旧值，空值不覆盖，份额 0 有效。"""
    merged = {}
    for rows in batches:
        for r in rows:
            date, code = iso(r.get("trade_date")), text(r.get("ts_code"))
            if not date or not code:
                continue
            row = merged.setdefault((date, code), {"trade_date": date, "ts_code": code, "total_share": None, "nav": None, "total_size": None})
            for key in ("total_share", "nav", "close"):
                value = number(r.get(key))
                if value is not None and (value > 0 if key == "nav" else value >= 0):
                    row[key] = value
            size = number(r.get("total_size"))
            estimated = r.get("size_estimated", text(r.get("source")).startswith("fund_share + fund_nav"))
            if size is not None and size >= 0 and (not estimated or row["total_size"] is None or row.get("size_estimated")):
                row["total_size"], row["size_estimated"] = size, bool(estimated)
            for key in ("etf_name", "source", "exchange"):
                if text(r.get(key)):
                    row[key] = text(r[key])
    for row in merged.values():
        if row["total_share"] is not None and row["nav"] is not None and (row["total_size"] is None or row.get("size_estimated")):
            row["total_size"] = row["total_share"] * row["nav"]
            row["size_estimated"] = True
    return list(merged.values())


def _etf_day(date, metadata):
    """份额和净值独立采集；任一接口失败仍保留另一项同日观测。"""
    def day(method, fields):
        try:
            return _day(method, date, fields)
        except Exception:
            return _read(f"raw:{method}:{date}") or []

    try:
        primary = _merge_etf(day("etf_share_size", "trade_date,ts_code,etf_name,total_share,total_size,nav,close,exchange"))
        if primary and all(r["total_share"] is not None and r["nav"] is not None for r in primary):
            return primary
        shares = {r["ts_code"]: r.get("fd_share") for r in day("fund_share", "trade_date,ts_code,fd_share") if r.get("ts_code") in metadata}
        key = f"raw:fund_nav:{date}"
        cached = _read(key) or []
        today = now().date()
        fresh = []
        if not cached or date >= (today - dt.timedelta(days=3)).isoformat():
            try:
                fresh = _records("fund_nav", nav_date=date.replace("-", ""), market="E", fields="ts_code,nav_date,ann_date,unit_nav")
            except Exception:
                pass  # 已缓存的同日净值与刚取得的份额仍有效。
        navs = {}
        for r in sorted([*cached, *fresh], key=lambda r: str(r.get("ann_date") or "")):
            if iso(r.get("nav_date")) == date and (iso(r.get("ann_date")) or "9999") <= today.isoformat() and (number(r.get("unit_nav")) or 0) > 0:
                navs[r["ts_code"]] = r
        if fresh and navs:
            kv_repo.set_(NAMESPACE, key, list(navs.values()))
        fallback = [{"trade_date": date, "ts_code": code, "etf_name": metadata.get(code, {}).get("extname"), "total_share": shares.get(code),
                     "nav": navs.get(code, {}).get("unit_nav"), "source": "fund_share + fund_nav"}
                    for code in (shares.keys() | (navs.keys() & metadata.keys()))]
        return _merge_etf(fallback, primary)
    finally:
        close_conn()


def _quarter(day):
    month = ((day.month - 1) // 3) * 3
    return dt.date(day.year, month + 1, 1) - dt.timedelta(days=1)


def _sse_open_days(start, end):
    if start > end:
        return []
    rows = _records("trade_cal", exchange="SSE", start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"), is_open="1")
    first, last = start.isoformat(), end.isoformat()
    return sorted({d for r in rows if str(r.get("is_open")) in ("1", "1.0")
                   and (d := iso(r.get("cal_date"))) and first <= d <= last})


def _publishable_day(current):
    return current.date() - dt.timedelta(days=current.time() < dt.time(15, 30))


def _collect_etf(result, dates, previous, through, progress):
    predecessors = etf_predecessors(result)
    previous = previous or predecessors.get(dates[0])
    result["etf_predecessors"] = {**predecessors, **dict(zip(dates, [previous, *dates]))}
    prior_ends = result.get("etf_calendar_ends", {})
    result["etf_calendar_ends"] = {**prior_ends, dates[-1]: max(through, prior_ends.get(dates[-1], dates[-1]))}
    errors = []
    metadata = {r["ts_code"]: r for r in result.get("etf_meta", [])}
    try:
        for row in _records("etf_basic", list_status="L", fields="ts_code,extname,index_code,index_name"):
            if code := text(row.get("ts_code")):
                metadata[code] = {**metadata.get(code, {}), **{k: v for k, v in row.items() if text(v)}}
    except Exception:
        errors.append("ETF跟踪指数分类读取失败，保留已有分类")
    result["etf_meta"] = list(metadata.values())

    prior = {date: [] for date in dates}
    if previous:
        prior[previous] = []
    merged = []
    for row in result.get("etf", []):
        date = iso(row.get("trade_date"))
        if date in prior:
            prior[date].append(row)
        else:
            merged.append(row)

    def fetch(date):
        return [r for r in _etf_day(date, metadata) if iso(r.get("trade_date")) == date]

    def fetched(future):
        try:
            return future.result()
        except Exception:
            return []  # 当日采集失败时保留已缓存的独立字段。

    failures = 0
    total = len(dates)
    with ThreadPoolExecutor(max_workers=4) as pool:
        baseline = pool.submit(fetch, previous) if previous else None
        work = {pool.submit(fetch, date): date for date in dates}
        if baseline:
            progress(f"ETF份额/净值：读取前一交易日 {previous} 的份额基准", 0, total)
            before = _merge_etf(prior[previous], fetched(baseline))
            merged.extend(before)
        else:
            before = []
        if not any(r["total_share"] is not None for r in before):
            errors.append("ETF首日前一交易日份额基准未取得，无法估算缺基准标的的首日净额")
        for processed, future in enumerate(as_completed(work), 1):
            date = work[future]
            rows = fetched(future)
            failures += not any(number(r.get("total_share")) is not None or number(r.get("nav")) is not None for r in rows)
            merged.extend(_merge_etf(prior[date], rows))
            progress(f"ETF份额/净值：已处理 {processed}/{total} 个区间交易日", processed, total)

    if failures:
        errors.append(f"ETF有{failures}个区间交易日读取失败或未披露；缺失不补零，保留已有记录")
    result["etf"] = merged
    result["etf_errors"] = errors
    result["etf_updated_at"] = now().isoformat()
    source = {"key": "etf", "as_of": max((date for r in merged if (date := iso(r.get("trade_date"))) in result["etf_predecessors"]
                                          and (number(r.get("total_share")) is not None or number(r.get("nav")) is not None)), default=None),
              "rows": len(merged), "status": "部分缺失 / 保留已有记录" if errors else "已读取", "failures": failures,
              "note": "etf_share_size优先；不可用时使用fund_share+fund_nav同日ETF白名单，缺净值不估金额"}
    result["sources"] = [s for s in result.get("sources", []) if s["key"] != "etf"] + [source]
    return source


def collect_etf_range(start, end, progress):
    result = dict(bundle("participants"))
    first = dt.date.fromisoformat(start)
    requested_end = dt.date.fromisoformat(end)
    publishable = _publishable_day(now()).isoformat()
    lookback = dt.date.fromordinal(max(1, first.toordinal() - 35))
    calendar = _sse_open_days(lookback, requested_end)
    through = min(end, publishable) if end in calendar else end
    dates = [d for d in calendar if start <= d <= through]
    if not dates:
        raise ValueError("所选区间没有已收盘的交易日可刷新；当日数据在北京时间15:30后采集")
    previous = max((d for d in calendar if d < start), default=None)
    _collect_etf(result, dates, previous, through, progress)
    amount_dates = {r["date"] for r in etf_rows(result, start=start, end=through) if r["net"] is not None}
    if through < end:
        result["etf_errors"].append(f"{end} 未到北京时间15:30，未计入采集交易日")
    _archive_legacy(result)
    kv_repo.set_(NAMESPACE, "participants", {k: v for k, v in result.items() if k not in DAILY_SOURCES})
    return {"start": start, "end": end, "as_of": max(amount_dates, default=None), "notes": result["etf_errors"],
            "processed_days": len(dates), "amount_days": len(amount_dates)}


def collect(kind, progress=lambda message: None):
    if kind not in _locks:
        raise ValueError("研究类型须为financing或participants")
    current = now()
    old = bundle(kind)
    result = dict(old)
    result["sources"], result["errors"] = [], []
    through = _publishable_day(current)
    dates = _sse_open_days(current.date() - dt.timedelta(days=90), through)
    etf_previous = dates[-23] if len(dates) > 22 else None
    dates = dates[-22:]
    if len(dates) < 21:
        raise ValueError("交易日历不足21日，保留原研究")
    stocks = _records("stock_basic", exchange="", list_status="L", fields="ts_code,name,industry,market")
    if not stocks:
        raise ValueError("股票分类未返回，保留原研究")
    result["stocks"] = stocks
    result["classification_at"] = current.date().isoformat()

    def record(key, rows, failures=0, confirmed=()):
        prior = old.get(key, [])
        last = max([*(iso(r.get("trade_date")) or "" for r in rows), *confirmed], default="")
        prior_date = max((iso(r.get("trade_date")) or "" for r in prior), default="")
        if (not rows and not confirmed) or last < prior_date:
            rows, last = prior, prior_date
            failures = max(1, failures)
        result[key] = rows
        result["sources"].append({"key": key, "as_of": last or None, "rows": len(rows), "status": "部分失败 / 保留已有日期" if failures else "已读取", "failures": failures})
        if failures:
            result["errors"].append(f"{key}有{failures}项读取失败或未披露；缺失不补零，已有数据保留原日期")

    def batch(key, method, wanted, fields=None):
        rows, failures, confirmed = [], 0, []
        prior = {}
        for r in old.get(key, []):
            prior.setdefault(iso(r.get("trade_date")), []).append(r)
        with ThreadPoolExecutor(max_workers=4) as pool:
            work = {pool.submit(_day, method, day, fields): day for day in wanted}
            for i, future in enumerate(as_completed(work), 1):
                date = work[future]
                try:
                    batch_rows = future.result()
                except Exception:
                    failures += 1
                    rows.extend(_read(f"raw:{method}:{date}") or prior.get(date, []))
                    continue
                if not batch_rows and method == 'top_inst' and _read(f'raw:{method}:{date}') == []:
                    confirmed.append(date)
                elif not batch_rows:
                    failures += 1
                    batch_rows = prior.get(date, [])
                rows.extend(batch_rows)
                progress(f"{method}：{i}/{len(wanted)}个交易日")
        record(key, rows, failures, confirmed)

    if kind == "financing":
        batch("margin", "margin", dates)
        summary = margin_metrics(result["margin"])
        if not summary:
            raise ValueError("未取得沪深两所同日完整融资汇总，保留原研究")
        result["as_of"] = summary[-1]["date"]
        wanted = [d for d in dates if d <= result["as_of"]][-21:]
        batch("details", "margin_detail", wanted)
        batch("prices", "daily", wanted, "ts_code,trade_date,close,pct_chg,vol,amount")
        batch("factors", "adj_factor", wanted, "ts_code,trade_date,adj_factor")
        batch("basics", "daily_basic", [result["as_of"]], "ts_code,trade_date,circ_mv,turnover_rate")
        if not any(iso(r.get("trade_date")) == result["as_of"] for r in result["details"]):
            raise ValueError("汇总日未取得个股明细，保留原研究")
    else:
        result["as_of"] = dates[-1]
        _archive_legacy(old)
        batch("flows", "moneyflow_dc", dates[-21:])
        result["flow_as_of"] = max((iso(r.get("trade_date")) or "" for r in result["flows"]), default=None)
        batch("institutions", "top_inst", dates[-21:])
        _collect_etf(result, dates, etf_previous, through.isoformat(), lambda message, *_: progress(message))
        national, failures = [], 0
        meta = {r["ts_code"]: r for r in stocks}
        period = _quarter(current.date()).strftime("%Y%m%d")
        try:
            holdings = _records("top10_floatholders", period=period)
            for row in holdings:
                holder = str(row.get("holder_name") or "")
                announced, end = iso(row.get("ann_date")), iso(row.get("end_date"))
                if holder not in NATIONAL_NAMES or not announced or not end or announced > current.date().isoformat() or end > dates[-1]:
                    continue
                code = row.get("ts_code")
                national.append({"code": code, "name": meta.get(code, {}).get("name") or code, "sector": meta.get(code, {}).get("industry") or "未分类",
                                 "holder": holder, "as_of": announced, "period": end, "shares": number(row.get("hold_amount")),
                                 "hold_ratio": number(row.get("hold_ratio")), "change_shares": number(row.get("hold_change")), "net": None})
        except Exception:
            failures = 1
        disclosures = {(r["code"], r["holder"], r["as_of"], r["period"]): r for r in [*old.get("national", []), *national]}
        result["national"] = list(disclosures.values())
        result["sources"].append({"key": "national", "as_of": max((r["as_of"] for r in result["national"]), default=None), "rows": len(result["national"]),
                                  "status": "披露持仓 / 非每日交易" if national else "本期尚无可用披露 / 旧证据保留", "failures": failures})
        quant_dates = [d for d in quant_radar_repo.list_dates(limit=120) if iso(d) and iso(d) <= dates[-1]]
        quant = quant_radar_repo.get_day(quant_dates[0], limit=0) if quant_dates else []
        if not quant:
            baseline_quant = read_baseline().get("cross", {}).get("quant") or {}
            quant = baseline_quant.get("stocks") or []
            quant_dates = [baseline_quant.get("fallback_date") or baseline_quant.get("snapshot_date")] if quant else []
        result["quant"] = [{"code": str(r.get("code") or ""), "name": r.get("name") or r.get("code"), "sector": r.get("sector") or r.get("industry") or "未分类",
                            "as_of": iso(quant_dates[0]), "activity": number(r.get("activity")), "direction": r.get("direction"),
                            "bull": number(r.get("changes_bull")), "bear": number(r.get("changes_bear")), "net": None} for r in quant]
        result["sources"].append({"key": "quant", "as_of": iso(quant_dates[0]) if quant_dates else None, "rows": len(quant), "status": "现有雷达或研究基线行为代理 / 不推算资金金额"})
        if not any(result.get(key) for key in ("flows", "institutions", "etf", "national", "quant")) and not any(
                s['key'] == 'institutions' and s['as_of'] in dates and not s['failures'] for s in result['sources']):
            raise ValueError("资金接口未返回可用数据，请检查现有TuShare权限；原研究保留")
    result["calendar"] = dates
    result["updated_at"] = now().isoformat()
    if old.get("as_of") and result["as_of"] < old["as_of"]:
        raise ValueError("接口数据日期倒退，原研究保留")
    if kind == 'participants':
        _archive_legacy(result)
        result['evidence_calendar'] = sorted(set(old.get('evidence_calendar', [])) | set(old.get('calendar', [])) | set(dates))
        # The header remains bounded; historical rows live in the original per-day raw keys.
        header = {k: v for k, v in result.items() if k not in DAILY_SOURCES}
        for source in header['sources']:
            if source['key'] in DAILY_SOURCES:
                method = DAILY_SOURCES[source['key']]
                source['as_of'] = max(_daily_keys(method) | _daily_keys(method, "seed"), default=source['as_of'])
                if source['key'] == 'flows':
                    header['flow_as_of'] = source['as_of']
        kv_repo.set_(NAMESPACE, kind, header)
    else:
        kv_repo.set_(NAMESPACE, kind, result)
    return result


def start_refresh(kind, jobs, *, participant="", start="", end=""):
    if kind not in _locks:
        raise ValueError("研究类型须为financing或participants")
    if not all(isinstance(v, str) for v in (participant, start, end)) or participant and participant not in PARTICIPANTS:
        raise ValueError("无效的资金类型或日期")
    params = {}
    if start or end:
        if kind != "participants" or participant != "etf":
            raise ValueError("区间刷新仅适用于ETF资金研究")
        if not start or not end or iso(start) != start or iso(end) != end or start > end:
            raise ValueError("请选择完整的起止日期（YYYY-MM-DD），开始日期不能晚于结束日期")
        if end > now().date().isoformat():
            raise ValueError("结束日期不能晚于北京时间今日")
        if (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days > 3660:
            raise ValueError("单次刷新区间最长十年")
        params = {"participant": "etf", "start": start, "end": end}
    with _locks[kind]:
        active = _status(kind, jobs)
        if active and active.get("status") in ("queued", "running"):
            if (active.get("params") or {}) != params:
                raise RefreshConflict("另一个资金研究区间或默认刷新正在执行，请在完成后重试")
            return active
        def worker(job_id, params):
            started_at = now().isoformat()
            count = 0
            warned = False
            def progress(message=None, *, percent=None):
                nonlocal count, warned
                count += 1
                try:
                    jobs.update(job_id, status="running", started_at=started_at,
                                progress=min(95, count) if percent is None else percent)
                    if message is not None:
                        jobs.append_log(job_id, message)
                except sqlite3.Error:
                    if not warned:
                        logger.warning("研究任务 %s 未能写入进度，继续采集", job_id, exc_info=True)
                        warned = True
            try:
                progress()
                if params:
                    result = collect_etf_range(params["start"], params["end"],
                                               lambda message, done, total: progress(message, percent=1 + 94 * done // total))
                else:
                    data = collect(kind, progress)
                    result = {"as_of": data["as_of"], "notes": data["errors"] + data.get("etf_errors", [])}
                jobs.update(job_id, status="finished", finished_at=now().isoformat(), progress=100, result=result)
            except Exception as exc:
                # 外部异常可能包含请求体，不把Token或连接信息回显到界面。
                if isinstance(exc, sqlite3.Error):
                    message = "本地数据库读写失败，请检查磁盘空间和数据目录权限后重试"
                elif isinstance(exc, ValueError):
                    message = str(exc)
                else:
                    message = f"公开数据读取失败（{type(exc).__name__}），请检查网络/接口权限；原研究保留"
                jobs.update(job_id, status="failed", finished_at=now().isoformat(), error=message)
            finally:
                close_conn()
        job = jobs.start("market_capital_" + kind, params, worker)
        kv_repo.set_(NAMESPACE, "job:" + kind, job["id"])
        return job
