"""大盘体检：两融全景、公开资金证据。只读查询与后台采集分开。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
from functools import lru_cache
import gzip
import json
from pathlib import Path
import threading

from analysis.market_capital import capital_report, margin_report
from analysis.market_health import number
from analysis.market_health_fusion import iso, margin_metrics
from data_store import kv_repo, quant_radar_repo, tushare_client
from data_store.connection import close_conn
from webui.services.market_health_service import now, read_baseline

NAMESPACE = "market_capital"
BASELINE = Path(__file__).resolve().parents[2] / "config/market_capital_baseline.json.gz"
_locks = {key: threading.Lock() for key in ("financing", "participants")}
PARTICIPANTS = {"main", "elg", "lg", "md", "sm", "institution", "etf", "national", "quant"}
NATIONAL_NAMES = ("中央汇金投资有限责任公司", "中央汇金资产管理有限责任公司", "中国证券金融股份有限公司", "中国国新控股有限责任公司", "中国诚通控股集团有限公司")


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


def bundle(kind):
    saved = _read(kind)
    if saved:
        return saved
    return _baseline().get(kind) or {}


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
    if not start and participant == "national":
        start = min((r["as_of"] for r in data.get("national", []) if r.get("as_of") and r["as_of"] <= (end or "")), default=end)
    report = capital_report(data, start=start, end=end, participant=participant,
                            keyword=str(params.get("keyword") or "")[:100], sector=str(params.get("sector") or "")[:100])
    report.update(job=_status("participants", jobs), updated_at=data.get("updated_at"), errors=data.get("errors", []), today=now().date().isoformat(),
                  disclosure="资金类型可能重叠，不可加总为市场增量资金。大单/小单为订单规模；ETF为份额变化×同日净值的申赎估算；机构仅限龙虎榜公开单日席位；国家队为已公告持仓；量化为公开行为代理。")
    return report


def _day(method, date, fields=None):
    key = f"raw:{method}:{date}"
    cached = _read(key)
    # 最新三个自然日重试披露修订；更早成功记录直接复用。
    if cached and date < (now().date() - dt.timedelta(days=3)).isoformat():
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
        return cached or []
    finally:
        close_conn()


def _etf_day(date, metadata):
    """新版接口不可用时用同日基金份额+净值；不以价格或规模变化代替申赎。"""
    try:
        try:
            rows = _day("etf_share_size", date, "trade_date,ts_code,etf_name,total_share,total_size,nav,close,exchange")
        except Exception:
            rows = []
        if rows:
            return rows
        shares = _day("fund_share", date, "trade_date,ts_code,fd_share")
        cached = _read(f"raw:fund_nav:{date}")
        if not cached or date >= (now().date() - dt.timedelta(days=3)).isoformat():
            navs = _records("fund_nav", nav_date=date.replace("-", ""), market="E", fields="ts_code,nav_date,ann_date,unit_nav")
            navs = [r for r in navs if iso(r.get("nav_date")) == date and iso(r.get("ann_date")) and iso(r["ann_date"]) <= now().date().isoformat()]
            if navs:
                kv_repo.set_(NAMESPACE, f"raw:fund_nav:{date}", navs)
            cached = navs or cached
        navs = {r["ts_code"]: number(r.get("unit_nav")) for r in cached or []}
        result = []
        for r in shares:
            code = r.get("ts_code")
            if code not in metadata:
                continue
            share, nav = number(r.get("fd_share")), navs.get(code)
            result.append({"trade_date": date, "ts_code": code, "etf_name": metadata[code].get("extname"), "total_share": share,
                           "nav": nav, "total_size": share * nav if share is not None and nav is not None else None,
                           "source": "fund_share + fund_nav（ETF白名单，同日净值）"})
        return result
    finally:
        close_conn()


def _quarter(day):
    month = ((day.month - 1) // 3) * 3
    return dt.date(day.year, month + 1, 1) - dt.timedelta(days=1)


def collect(kind, progress=lambda message: None):
    if kind not in _locks:
        raise ValueError("研究类型须为financing或participants")
    current = now()
    old = bundle(kind)
    result = dict(old)
    result["sources"], result["errors"] = [], []
    raw_calendar = _records("trade_cal", exchange="SSE", start_date=(current.date() - dt.timedelta(days=90)).strftime("%Y%m%d"), end_date=current.strftime("%Y%m%d"), is_open="1")
    dates = sorted({iso(r.get("cal_date")) for r in raw_calendar if str(r.get("is_open")) in ("1", "1.0") and iso(r.get("cal_date"))})
    dates = [d for d in dates if d < current.date().isoformat() or current.time() >= dt.time(15, 30)][-22:]
    if len(dates) < 21:
        raise ValueError("交易日历不足21日，保留原研究")
    result["calendar"] = dates
    stocks = _records("stock_basic", exchange="", list_status="L", fields="ts_code,name,industry,market")
    if not stocks:
        raise ValueError("股票分类未返回，保留原研究")
    result["stocks"] = stocks
    result["classification_at"] = current.date().isoformat()

    def record(key, rows, failures=0):
        prior = old.get(key, [])
        last = max((iso(r.get("trade_date")) or "" for r in rows), default="")
        prior_date = max((iso(r.get("trade_date")) or "" for r in prior), default="")
        if not rows or last < prior_date:
            rows, last = prior, prior_date
            failures = max(1, failures)
        result[key] = rows
        result["sources"].append({"key": key, "as_of": last or None, "rows": len(rows), "status": "部分失败 / 保留已有日期" if failures else "已读取", "failures": failures})
        if failures:
            result["errors"].append(f"{key}有{failures}项读取失败或未披露；缺失不补零，已有数据保留原日期")

    def batch(key, method, wanted, fields=None, fetcher=None):
        rows, failures = [], 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            work = {pool.submit(fetcher, day) if fetcher else pool.submit(_day, method, day, fields): day for day in wanted}
            for i, future in enumerate(as_completed(work), 1):
                try:
                    batch_rows = future.result()
                    if not batch_rows:
                        failures += 1
                    rows.extend(batch_rows)
                except Exception:
                    failures += 1
                    rows.extend(_read(f"raw:{method}:{work[future]}") or [])
                progress(f"{method}：{i}/{len(wanted)}个交易日")
        record(key, rows, failures)

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
        batch("flows", "moneyflow_dc", dates[-21:])
        result["flow_as_of"] = max((iso(r.get("trade_date")) or "" for r in result["flows"]), default=None)
        batch("institutions", "top_inst", dates[-21:])
        try:
            result["etf_meta"] = _records("etf_basic", list_status="L", fields="ts_code,extname,index_code,index_name")
        except Exception:
            result["errors"].append("ETF跟踪指数分类读取失败，保留已有分类")
        etf_meta = {r["ts_code"]: r for r in result.get("etf_meta", [])}
        batch("etf", "ETF份额/净值", dates, fetcher=lambda day: _etf_day(day, etf_meta))
        for source in result["sources"]:
            if source["key"] == "etf":
                source["note"] = "etf_share_size优先；不可用时使用fund_share+fund_nav同日ETF白名单，缺净值不估金额"
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
        result["national"] = national or old.get("national", [])
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
        if not result["flows"] and not result["institutions"] and not result["etf"] and not national and not quant:
            raise ValueError("资金接口未返回可用数据，请检查现有TuShare权限；原研究保留")
    result["updated_at"] = now().isoformat()
    if old.get("as_of") and result["as_of"] < old["as_of"]:
        raise ValueError("接口数据日期倒退，原研究保留")
    kv_repo.set_(NAMESPACE, kind, result)
    return result


def start_refresh(kind, jobs):
    if kind not in _locks:
        raise ValueError("研究类型须为financing或participants")
    with _locks[kind]:
        active = _status(kind, jobs)
        if active and active.get("status") in ("queued", "running"):
            return active
        def worker(job_id, params):
            jobs.update(job_id, status="running", started_at=now().isoformat(), progress=1)
            count = [0]
            def progress(message):
                count[0] += 1
                jobs.update(job_id, progress=min(95, count[0]))
                jobs.append_log(job_id, message)
            try:
                result = collect(kind, progress)
                jobs.update(job_id, status="finished", finished_at=now().isoformat(), progress=100,
                            result={"as_of": result["as_of"], "notes": result["errors"]})
            except Exception as exc:
                # 外部异常可能包含请求体，不把Token或连接信息回显到界面。
                message = str(exc) if isinstance(exc, ValueError) else f"公开数据读取失败（{type(exc).__name__}），请检查网络/接口权限；原研究保留"
                jobs.update(job_id, status="failed", finished_at=now().isoformat(), error=message)
            finally:
                close_conn()
        job = jobs.start("market_capital_" + kind, {}, worker)
        kv_repo.set_(NAMESPACE, "job:" + kind, job["id"])
        return job
