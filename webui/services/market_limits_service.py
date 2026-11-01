"""大盘体检涨跌停研究：只读查询、后台回补、逐日缓存与选股形态标签。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import re
import threading
import time

from analysis.market_health import number
from analysis.market_limits import build_report, normalize_day, pattern_tags
from data_store import kv_repo
from data_store.connection import close_conn
from data_store.tushare_client import text_field, yyyymmdd_to_iso as iso
from webui.services.market_health_service import now
from webui.services.sector_depth_service import _records as _tushare_records

NAMESPACE = "market_limits_v1"
_lock = threading.Lock()
_request_lock = threading.Lock()
_next_request = {}


def _records(method, **params):
    # 按接口分开限速，避免数百个板块的日线同时撞上每分钟200次限制。
    with _request_lock:
        current = time.monotonic()
        scheduled = max(current, _next_request.get(method, current))
        _next_request[method] = scheduled + .35
    if scheduled > current:
        time.sleep(scheduled - current)
    try:
        return _tushare_records(method, **params)
    except ValueError as exc:
        if "现有TuShare配置" in str(exc):
            raise ValueError("涨跌停历史需要现有TuShare配置，请在设置中检查Token") from None
        raise


def _read(key):
    hit = kv_repo.get(NAMESPACE, key)
    return hit[0] if hit else None


def date_range(start="", end=""):
    today = now().date()
    try:
        finish = dt.date.fromisoformat(str(end)) if end else today
        begin = dt.date.fromisoformat(str(start)) if start else finish - dt.timedelta(days=30)
    except (TypeError, ValueError):
        raise ValueError("日期必须为YYYY-MM-DD") from None
    if begin > finish:
        raise ValueError("开始日期不能晚于结束日期")
    if finish > today:
        raise ValueError("结束日期不能晚于今天")
    if (finish - begin).days > 366:
        raise ValueError("单次最多查询366天，可分段查询并导出")
    return begin.isoformat(), finish.isoformat()


def _key(start, end):
    return f"report:{start}:{end}"


def query(jobs=None, start="", end=""):
    if not start and not end:
        last = _read("latest_range") or {}
        start, end = last.get("start", ""), last.get("end", "")
    start, end = date_range(start, end)
    report = _read(_key(start, end))
    job_id = _read("latest_job")
    job = jobs.get(job_id) if jobs and job_id else None
    return {**(report or build_report([], [], start, end)), "available": report is not None,
            "job": job, "message": "" if report else "尚无此区间数据，点击更新区间数据回补真实历史"}


def detail(kind, code, jobs=None, start="", end=""):
    if kind not in ("stock", "sector"):
        raise ValueError("明细类型必须为stock或sector")
    if not re.fullmatch(r"\d{6}" if kind == "stock" else r"BK\d{4,6}", str(code)):
        raise ValueError("股票或板块代码无效")
    report = query(jobs, start, end)
    row = next((r for r in report["stocks" if kind == "stock" else "sectors"] if r["code"] == code), None)
    if row is None:
        raise ValueError("所选区间没有该股票或板块的涨跌停记录")
    events = [r for r in report["events"] if r["code"] == code] if kind == "stock" else [r for r in report["events"] if code in r["sectors"]]
    stock_codes = {r["code"] for r in events}
    sector_codes = {v for r in events for v in r["sectors"]}
    return {"kind": kind, "row": row, "events": events, "start": report["start"], "end": report["end"],
            "stocks": [r for r in report["stocks"] if r["code"] in stock_codes],
            "sectors": [r for r in report["sectors"] if r["code"] in sector_codes],
            "notes": report.get("notes", []), "updated_at": report.get("updated_at")}


def _daily(date, basics):
    try:
        cached = _read("day:" + date)
        # 最近两日重新核对迟到披露；更早完整历史直接命中。
        if cached and date < (now().date() - dt.timedelta(days=2)).isoformat() and not cached.get("unverified"):
            return cached
        compact = date.replace("-", "")
        quotes = _records("daily", cap=6000, trade_date=compact)
        limits = _records("stk_limit", cap=5800, trade_date=compact, fields="ts_code,trade_date,pre_close,up_limit,down_limit")
        if not quotes or not limits:
            if cached:
                return {**cached, "warning": "本轮未取得日线或限价，保留上次真实日数据"}
            raise ValueError("日线或实际限价尚未披露")
        extras, extra_ok = [], True
        try:
            for kind in ("U", "D", "Z"):
                extras.extend(_records("limit_list_d", cap=2500, trade_date=compact, limit_type=kind))
        except Exception:
            extra_ok = False
        day = normalize_day(date, quotes, limits, extras, basics)
        if not day["covered"] or (cached and day["covered"] < cached["covered"]):
            if cached:
                return {**cached, "warning": "本轮限价覆盖减少，保留上次真实日数据"}
            raise ValueError("未取得可核验的A股收盘价和实际限价")
        day["extra_available"] = extra_ok
        kv_repo.set_(NAMESPACE, "day:" + date, day)
        return day
    finally:
        close_conn()


def _members(code, start, end, boards):
    try:
        key = f"members:{code}:{start}:{end}"
        cached = _read(key)
        if cached is not None:
            return cached
        raw = _records("dc_member", cap=5000, con_code=code, start_date=start.replace("-", ""), end_date=end.replace("-", ""))
        result = {}
        for r in raw:
            date, board = iso(r.get("trade_date")), text_field(r.get("ts_code")).split(".")[0]
            if text_field(r.get("con_code")) != code or not start <= date <= end or board not in boards:
                continue
            result.setdefault(f"{date}:{code[:6]}", []).append({"code": board})
        # 空结果可能是尚未披露，不持久化成永久的“无板块”。
        if result:
            kv_repo.set_(NAMESPACE, key, result)
        return result
    finally:
        close_conn()


def _patterns(code, end, sector=False):
    try:
        key = f"pattern:{code}:{end}"
        cached = _read(key)
        if cached:
            return cached
        start = (dt.date.fromisoformat(end) - dt.timedelta(days=400)).strftime("%Y%m%d")
        raw = _records("dc_daily" if sector else "daily", cap=2000 if sector else 6000,
                       ts_code=code, start_date=start, end_date=end.replace("-", ""))
        raw = sorted({iso(r.get("trade_date")): r for r in raw if iso(r.get("trade_date")) <= end}.values(),
                     key=lambda r: str(r.get("trade_date")))[-120:]
        factors = {}
        if raw and not sector:
            adjusted = _records("adj_factor", ts_code=code, start_date=start, end_date=end.replace("-", ""))
            factors = {iso(r.get("trade_date")): number(r.get("adj_factor")) for r in adjusted}
            if any(not factors.get(iso(r.get("trade_date"))) for r in raw):
                return {"tags": [], "as_of": None, "status": "复权因子缺失，待补形态"}
        bars = []
        anchor = factors.get(iso(raw[-1].get("trade_date"))) if raw and factors else 1
        for r in raw:
            date = iso(r.get("trade_date"))
            scale = factors.get(date, 1) / anchor
            bar = {"date": date, "volume": number(r.get("vol")), "amount": number(r.get("amount"))}
            for field in ("open", "high", "low", "close"):
                bar[field] = number(r.get(field)) * scale if number(r.get(field)) is not None else None
            if not sector and bar["amount"] is not None:
                bar["amount"] *= 1000
            bars.append(bar)
        result = pattern_tags(bars, "" if sector else code[:6], end)
        result["price_basis"] = "板块指数日线" if sector else "前复权日线（锚定截止日）"
        if result["as_of"] == end:
            kv_repo.set_(NAMESPACE, key, result)
        return result
    finally:
        close_conn()


def _batch(tasks, worker, progress):
    results, failed = {}, []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(worker, item): item for item in tasks}
        for i, future in enumerate(as_completed(futures), 1):
            item = futures[future]
            try:
                results[item] = future.result()
            except Exception:
                failed.append(item)
            progress(i, len(tasks))
    return results, failed


def _board_catalog(start, end):
    boards = {}
    begin, finish = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    while begin <= finish:
        next_month = (begin.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        until = min(finish, next_month - dt.timedelta(days=1))
        for kind in ("行业板块", "概念板块"):
            raw = _records("dc_index", idx_type=kind, start_date=begin.strftime("%Y%m%d"), end_date=until.strftime("%Y%m%d"))
            for r in sorted(raw, key=lambda r: str(r.get("trade_date"))):
                code = text_field(r.get("ts_code")).split(".")[0]
                if re.fullmatch(r"BK\d{4,6}", code):
                    boards[code] = {"name": text_field(r.get("name")), "type": "行业" if kind == "行业板块" else "概念"}
        begin = next_month
    return boards


def collect(start, end, progress=lambda value, message: None):
    start, end = date_range(start, end)
    current = now()
    # 额外60个交易日承接区间前的连板；左端仍连续时保留下界标识。
    lookback = (dt.date.fromisoformat(start) - dt.timedelta(days=150)).strftime("%Y%m%d")
    raw = _records("trade_cal", exchange="SSE", is_open="1", start_date=lookback, end_date=end.replace("-", ""))
    calendar = sorted({iso(r.get("cal_date")) for r in raw if str(r.get("is_open")) in ("1", "1.0")})
    calendar = [d for d in calendar if d and d <= end and (d < current.date().isoformat() or current.time() >= dt.time(16, 10))]
    if not calendar:
        raise ValueError("此区间未取得已收盘交易日历")
    calendar = [d for d in calendar if d < start][-60:] + [d for d in calendar if d >= start]
    basics = {r["ts_code"]: r for r in _records("stock_basic", exchange="", list_status="L", fields="ts_code,name")}
    days, failures = _batch(calendar, lambda d: _daily(d, basics),
                            lambda i, n: progress(int(i / n * 40), f"核验日线与限价 {i}/{n}日"))
    selected = [v for d, v in days.items() if start <= d <= end]
    if not selected:
        raise ValueError("所选区间没有取得可核验日数据，原成功快照保留")
    codes = sorted({r["ts_code"] for d in selected for r in d["events"]})
    boards, notes = {}, []
    try:
        boards = _board_catalog(start, end)
    except Exception:
        notes.append("板块分类接口不可用，保留股票事件；板块统计待补齐")
    memberships = {}
    if boards:
        mapped, failed_members = _batch(codes, lambda c: _members(c, start, end, boards),
                                       lambda i, n: progress(40 + int(i / n * 20), f"关联事件当天板块 {i}/{n}只"))
        for value in mapped.values():
            memberships.update(value)
        if failed_members:
            notes.append(f"{len(failed_members)}只股票的历史板块成分读取失败，股票统计仍保留")
    touched_boards = sorted({r["code"] for values in memberships.values() for r in values})
    as_of = max(d["date"] for d in selected)
    tasks = [(False, c) for c in codes] + [(True, c + ".DC") for c in touched_boards]
    calculated, failed_patterns = _batch(tasks, lambda t: _patterns(t[1], as_of, sector=t[0]),
                                         lambda i, n: progress(60 + int(i / n * 37), f"计算条件选股形态 {i}/{n}项"))
    patterns = {("sector:" + code.split(".")[0] if sector else "stock:" + code[:6]): value
                for (sector, code), value in calculated.items()}
    if failed_patterns:
        notes.append(f"{len(failed_patterns)}项形态日K读取失败，标签标记待补，不影响事件统计")
    report = build_report(list(days.values()), calendar, start, end, memberships, patterns, boards)
    report.update(updated_at=now().isoformat(), notes=notes, source="TuShare daily × stk_limit；limit_list_d补充封板信息",
                  name_basis="名称优先采用事件日榜单；缺失时采用当前上市名称，退市股票仍按代码保留",
                  membership_basis="dc_member事件当天成分；同股可属于多个板块，板块数量不能相加当作全市场数量",
                  pattern_basis="条件选股默认参数；标签截至最后有效收盘日，非每次涨跌停发生日；个股前复权，板块指数原值")
    if failures:
        notes.append(f"{len(failures)}个交易日读取失败，缺日不计为零；相邻连续数标为至少")
    unverified = sum(d.get("unverified", 0) for d in selected)
    if unverified:
        notes.append(f"{unverified}条股票日记录缺少有效实际限价，未纳入统计（可能包含无涨跌幅限制日）")
    if any(not d.get("extra_available") for d in selected):
        notes.append("部分日期封板明细不可用；实际限价统计保留，封单/时间等缺值不补零。ST不在limit_list_d覆盖范围")
    if any(d.get("warning") for d in selected):
        notes.append("部分日期本轮返回不足，已保留该日上次成功核验数据")
    report["complete"] = not report["missing_dates"] and not unverified and not report["unmapped_events"]
    pattern_rows = report["stocks"] + report["sectors"]
    report["pattern_coverage"] = {"computed": sum(r["pattern"].get("status") == "已计算" for r in pattern_rows),
                                  "total": len(pattern_rows), "failed": len(failed_patterns)}
    old = _read(_key(start, end))
    if old and (len(report["missing_dates"]) > len(old.get("missing_dates", [])) or (old.get("as_of") or "") > (report["as_of"] or "")):
        raise ValueError("本轮日数据完整度下降，原成功区间快照保留，请重试更新")
    kv_repo.set_(NAMESPACE, _key(start, end), report)
    kv_repo.set_(NAMESPACE, "latest_range", {"start": start, "end": end})
    return report


def start_refresh(jobs, start="", end=""):
    start, end = date_range(start, end)
    with _lock:
        job_id = _read("latest_job")
        active = jobs.get(job_id) if job_id else None
        if active and active.get("status") in ("queued", "running"):
            return active

        def worker(job_id, params):
            jobs.update(job_id, status="running", started_at=now().isoformat(), progress=1)
            try:
                def progress(value, message):
                    jobs.update(job_id, progress=value)
                    jobs.append_log(job_id, message)
                report = collect(params["start"], params["end"], progress)
                jobs.update(job_id, status="finished", finished_at=now().isoformat(), progress=100,
                            result={"start": start, "end": end, "as_of": report["as_of"], "notes": report["notes"]})
            except Exception as exc:
                message = str(exc) if isinstance(exc, ValueError) else f"涨跌停数据更新失败（{type(exc).__name__}），请检查数据权限或稍后重试"
                jobs.update(job_id, status="failed", finished_at=now().isoformat(), error=message)
            finally:
                close_conn()
        job = jobs.start("market_limits", {"start": start, "end": end}, worker)
        kv_repo.set_(NAMESPACE, "latest_job", job["id"])
        return job
