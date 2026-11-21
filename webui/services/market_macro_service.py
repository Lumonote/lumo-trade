"""宏观研究：公开来源采集、带日期快照与后台更新。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import calendar
import csv
import datetime as dt
from functools import lru_cache, partial
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import threading
from urllib.parse import urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
import httpx

from analysis.market_health import number
from analysis.market_cycle import cycle_report, flow_concentration
from analysis.market_health_fusion import iso
from analysis.market_macro import (CN10Y_SOURCE, FRED, INDEXES, SPECS, SPREAD, build_events, build_research,
                                   clean_series, equity_bond_spread, scenario)
from data_store import kv_repo, tushare_client
from data_store.connection import close_conn, get_conn
from webui.services.daily_history import parse_day
from webui.services.http_client import request_text
from webui.services.market_health_service import now
from webui.services.market_cycle_sources import AXES, fetch_releases

NAMESPACE = "market_macro"
BASELINE = Path(__file__).resolve().parents[2] / "config/market_macro_baseline.json.gz"
CYCLE_BASELINE = BASELINE.with_name('market_cycle_releases.json')
# Seed keys are versioned to let a new distribution import its updated archive.
BASELINE_KEY = 'baseline:20261031'
CYCLE_BASELINE_KEY = 'cycle-releases:20261031'
BOND_BASELINE_KEY = 'cn10y:20261031'
FISCAL_URL = SPECS["spending"]["url"]
DEBT_URL = SPECS["local_new"]["url"]
FED_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
_lock = threading.Lock()
_HISTORY_NAMESPACE = 'market_macro_history_v1'
_TU_DAILY_HISTORY = {'gold': ('sge_daily', 'close', 'trade_date', {'ts_code': 'Au99.99'}),
                     'shibor3m': ('shibor', '3m', 'date', {}),
                     'lpr1y': ('shibor_lpr', '1y', 'date', {}), 'lpr5y': ('shibor_lpr', '5y', 'date', {})}
_TU_MONTHLY_HISTORY = {'m1': ('cn_m', 'm1_yoy'), 'm2': ('cn_m', 'm2_yoy'),
                       'cpi': ('cn_cpi', 'nt_yoy'), 'ppi': ('cn_ppi', 'ppi_yoy'),
                       'pmi': ('cn_pmi', 'pmi010000'), 'social': ('sf_month', 'inc_month'),
                       'social_stock': ('sf_month', 'stk_endval')}
_ON_DEMAND_HISTORY = FRED.keys() | _TU_DAILY_HISTORY.keys() | _TU_MONTHLY_HISTORY.keys() | {SPREAD}
_history_locks = {key: threading.Lock() for key in _ON_DEMAND_HISTORY}
_BOND_HISTORY_START = '2006-03-01'  # 财政部-中国国债收益率曲线的官方查询起点。

POLICIES = [
    {"date": "2024-09-24", "category": "monetary", "title": "金融支持经济高质量发展政策组合发布", "summary": "降准、利率、住房金融与资本市场工具的政策组合；分别跟踪落地与信用需求。", "tags": "降准 降息 地产 资本市场", "source": "国新办 / 证监会", "url": "https://www.csrc.gov.cn/csrc/c106311/c7508374/content.shtml"},
    {"date": "2024-09-27", "category": "monetary", "title": "降准0.5个百分点及7天逆回购利率下调落地", "summary": "区分政策生效与此前公布日；不将银行体系流动性等同于股市流入。", "tags": "降准 降息", "source": "中国政府网", "url": "https://app.www.gov.cn/govdata/gov/202409/27/519960/article.html"},
    {"date": "2024-11-08", "category": "fiscal", "title": "增加地方政府债务限额置换存量隐性债务", "summary": "债务限额增加6万亿元，分三年安排；核心是置换和化债，不能全部视作新增需求。原文次日刊载，采用发布会日期。", "tags": "化债 地方债", "source": "财政部", "url": "https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/202411/t20241109_3947230.htm"},
    {"date": "2026-03-16", "category": "fiscal", "title": "2026年中央和地方预算报告刊载", "summary": "观察预算安排、发行、拨付与实际支出的传导。此日为原文刊载日，不冒充所有预算措施首次公告日。", "tags": "预算 财政 消费 产业", "source": "财政部", "url": "https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/202603/t20260316_3985331.htm"},
    {"date": "2026-10-09", "category": "fiscal", "title": "上半年预算执行情况报告刊载", "summary": "跟踪扩内需与以旧换新等执行信息；这是执行报告刊载，不是所有措施的新公告。", "tags": "消费 以旧换新 财政 执行", "source": "财政部", "url": "https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/202610/t20261009_4002434.htm"},
    {"date": "2026-10-23", "category": "fiscal", "title": "前三季度财政收支新闻发布会", "summary": "核对财政支出和支持内需的执行情况，需与资金拨付、销量和利润验证。", "tags": "财政 支出 消费", "source": "财政部", "url": "https://m.mof.gov.cn/czxw/202610/t20261023_4003429.htm"},
]


def _read(key):
    hit = kv_repo.get(NAMESPACE, key)
    return hit[0] if hit else None


def _baseline_stamp():
    try:
        return BASELINE.stat().st_mtime_ns
    except OSError:
        return None


@lru_cache(maxsize=1)
def _load_baseline(stamp):
    try:
        with gzip.open(BASELINE, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, EOFError):
        return {}


def baseline():
    return _load_baseline(_baseline_stamp())


def _load_cycle_baseline():
    try:
        return json.loads(CYCLE_BASELINE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _stored_json(raw):
    try:
        data = json.loads(raw) if raw else None
    except (ValueError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _with_bond_history(saved):
    if not saved:
        return saved
    series = saved.get("series") or {}
    end = iso(saved.get("as_of"))
    if series.get("cn10y") or not end:
        return saved
    # 老版本用户的其他通道可能比分发包更新，只补国债且不越过用户快照截止日。
    raw = kv_repo.seed_raw(NAMESPACE, BOND_BASELINE_KEY,
                           lambda: {'cn10y': baseline().get('series', {}).get('cn10y', [])})
    rows = clean_series(_stored_json(raw).get('cn10y'), end)
    if not rows:
        return saved
    sources = [s for s in saved.get("sources", []) if s.get("name") != CN10Y_SOURCE["name"]]
    return {**saved, "series": {**series, "cn10y": rows}, "sources": [*sources, {
        **CN10Y_SOURCE, "rows": len(rows), "date": rows[-1]["date"], "status": "分发基线"}]}


def _with_cycle_history(data, archive):
    cutoff = data.get('as_of') or ''
    releases = {}
    for key in AXES:
        merged = {(r['period'], r['published_on'], r['url']): r for source in (archive, data.get('cycle_releases') or {})
                  for r in source.get(key, []) if r.get('published_on') and r['published_on'] <= cutoff}
        releases[key] = [merged[k] for k in sorted(merged)]
    return {**data, 'cycle_releases': releases} if data else data


class _Snapshot:
    def __init__(self, raw, archive_raw):
        saved = _stored_json(raw)
        if not saved and raw:
            raw = kv_repo.seed_raw(NAMESPACE, BASELINE_KEY, baseline)
            saved = _stored_json(raw)
        self.data = _with_cycle_history(_with_bond_history(saved), _stored_json(archive_raw))
        self.version = f'{hashlib.sha256(raw or b"").hexdigest()}:{hashlib.sha256(archive_raw or b"").hexdigest()}'
        self.research = lru_cache(maxsize=8)(partial(build_research, self.data))
        self.cycle = lru_cache(maxsize=12)(partial(cycle_report, self.data))

    def events(self, equity, category, keyword, limit=1000, offset=0):
        return build_events(self.data, equity=equity, category=category, keyword=str(keyword or "")[:100],
                            limit=int(limit), offset=int(offset))


# Key on the actual bytes: no per-query JSON decode, and revisions made in the
# same second (even by another process) cannot hit the previous report cache.
_snapshot_for = lru_cache(maxsize=1)(_Snapshot)


def _snapshot():
    raw = kv_repo.get_raw(NAMESPACE, "bundle")
    if raw is None:
        raw = kv_repo.seed_raw(NAMESPACE, BASELINE_KEY, baseline)
    archive = kv_repo.seed_raw(NAMESPACE, CYCLE_BASELINE_KEY, _load_cycle_baseline)
    return _snapshot_for(raw, archive)


def bundle():
    return _snapshot().data


def status(jobs=None):
    job_id = _read("job")
    return {"job": jobs.get(job_id) if jobs and job_id else None}


def events(*, equity="csi300", category="all", keyword="", limit=1000, offset=0):
    snap = _snapshot()
    return {"equity": equity, "as_of": snap.data.get("as_of"), "snapshot_version": snap.version,
            "events": snap.events(equity, category, keyword, limit, offset)}


def cycle(equity, cutoff):
    if equity not in INDEXES or not cutoff:
        raise ValueError('请选择研究指数和回放截止日')
    snap = _snapshot()
    return {**deepcopy(snap.cycle(equity, cutoff)), 'snapshot_version': snap.version}


def query(jobs=None, *, equity="csi300", factor="real10y", window=252, lag=0, frequency="daily",
          category="all", keyword="", earnings=0, pe=0, discount_bp=-25, duration=10):
    assumptions = scenario(float(earnings), float(pe), float(discount_bp), float(duration))
    snap = _snapshot()
    event_data = snap.events(equity, category, keyword)
    research = snap.research(equity=equity, factor=factor, window=int(window), lag=int(lag), frequency=frequency)
    research = deepcopy(research)
    if factor in _ON_DEMAND_HISTORY:
        saved, _ = _history_cache(_history_key(factor, equity))
        research['chart']['factor_history'] = clean_series([*saved, *research['chart']['factor_history']])
    return {**research, **status(jobs), "events": event_data, "scenario": assumptions, "cycle": deepcopy(snap.cycle(equity)),
            "snapshot_version": snap.version, "today": now().date().isoformat(), "available": bool(snap.data),
            "indexes": [{"key": k, "code": v[0], "name": v[1]} for k, v in INDEXES.items()],
            "factors": [{"key": k, **v} for k, v in SPECS.items()]}


def _text(url):
    # 官方站点直连，避免继承桌面代理配置中的可选SOCKS依赖。
    with httpx.Client(timeout=httpx.Timeout(55, connect=15), follow_redirects=True, trust_env=False) as client:
        for attempt in range(2):
            try:
                response = client.get(url)
                response.raise_for_status()
                charset = re.search(rb'charset\s*=\s*["\']?([A-Za-z0-9_-]+)', response.content[:4096], re.I)
                response.encoding = "gb18030" if charset and charset[1].lower() in (b"gb2312", b"gbk", b"gb18030") else "utf-8"
                return response.text
            except (httpx.TimeoutException, httpx.ConnectError):
                if attempt:
                    raise


def _records(method, cap=2000, **params):
    pro = tushare_client.get_pro()
    if pro is None:
        raise ValueError("未配置TuShare，国内行情与宏观序列保留原快照")
    rows, seen = [], set()
    for offset in range(0, 100000, cap):
        frame = getattr(pro, method)(**params, limit=cap, offset=offset)
        batch = [] if frame is None else frame.to_dict("records")
        if not batch:
            return rows
        fingerprints = {json.dumps(r, sort_keys=True, default=str) for r in batch}
        if fingerprints & seen:
            raise ValueError(f"{method}分页重复，未采用截断数据")
        seen.update(fingerprints)
        rows.extend(batch)
        if len(batch) < cap:
            return rows
    raise ValueError(f"{method}超过采集上限")


def _series(rows, field, date_field="trade_date", frequency="daily"):
    result = []
    for r in rows:
        date = str(r.get(date_field) or "")
        period = None
        if frequency == "monthly" and re.fullmatch(r"\d{6}", date):
            year, month = int(date[:4]), int(date[4:])
            period, date = f"{year:04}-{month:02}", dt.date(year, month, calendar.monthrange(year, month)[1]).isoformat()
        elif frequency == "quarterly" and re.fullmatch(r"\d{4}Q[1-4]", date):
            year, month = int(date[:4]), int(date[-1]) * 3
            period, date = date, dt.date(year, month, calendar.monthrange(year, month)[1]).isoformat()
        result.append({"date": date, "value": number(r.get(field)), **({"period": period} if period else {})})
    return clean_series(result, now().date().isoformat())


def fred_series(code, end, *, start='2014-01-01', timeout=None):
    params = urlencode({'id': code, 'cosd': start, 'coed': end})
    url = f'https://fred.stlouisfed.org/graph/fredgraph.csv?{params}'
    raw = _text(url) if timeout is None else request_text(url, timeout=timeout, trust_env=False)
    rows = csv.DictReader(io.StringIO(raw.lstrip('\ufeff')))
    if not rows.fieldnames or code not in rows.fieldnames or not {'observation_date', 'DATE'}.intersection(rows.fieldnames):
        raise ValueError('FRED 返回的列与研究指标不匹配')
    values = clean_series([{'date': r.get('observation_date') or r.get('DATE'), 'value': r.get(code)} for r in rows], end)
    return [r for r in values if r['date'] >= start]


def _history_key(factor, equity):
    return f'{factor}:{equity}' if factor == SPREAD else factor


def _history_cache(key):
    cached = kv_repo.get(_HISTORY_NAMESPACE, key)
    data = cached[0] if cached else {}
    return data.get('rows', []), data.get('covered', [])


def _missing_days(start, end, covered):
    day = dt.timedelta(days=1)
    for left, right in sorted((parse_day(a), parse_day(b)) for a, b in covered):
        if right < start:
            continue
        if left > end:
            break
        if left > start:
            yield start, left-day
        if right >= end:
            return
        start = max(start, right+day)
    if start <= end:
        yield start, end


def _fetch_factor(factor, equity, left, right):
    if factor in FRED:
        return fred_series(FRED[factor][0], right, start=left, timeout=12)
    compact = {'start_date': left.replace('-', ''), 'end_date': right.replace('-', '')}
    if factor == SPREAD:
        rows = _records('index_dailybasic', **compact, ts_code=INDEXES[equity][0], fields='trade_date,pe_ttm')
        bonds = {r['date']: r['value'] for r in cn10y_series(left, right)} if rows else {}
        return equity_bond_spread([{'date': r['trade_date'], 'pe_ttm': r.get('pe_ttm')} for r in rows], bonds, right)
    if factor in _TU_DAILY_HISTORY:
        method, field, date_field, params = _TU_DAILY_HISTORY[factor]
        return _series(_records(method, **compact, **params), field, date_field)
    method, field = _TU_MONTHLY_HISTORY[factor]
    start_month = compact['start_date'][:6]
    if factor == 'social_stock':
        start_month = f'{max(1, int(start_month[:4])-1):04}{start_month[4:]}'
    rows = _series(_records(method, start_m=start_month, end_m=compact['end_date'][:6]), field, 'month', 'monthly')
    if factor == 'social_stock':
        monthly = {r['period']: r['value'] for r in rows}
        result = []
        for row in rows:
            year, month = row['period'].split('-')
            prior = monthly.get(f'{int(year)-1:04}-{month}')
            if prior and prior > 0:
                result.append({**row, 'value': (row['value']/prior-1)*100})
        return result
    return rows


def history(*, factor, equity, start, end):
    """Query observations only; independent from the disclosure-aware replay."""
    if equity not in INDEXES or factor not in SPECS:
        raise ValueError('未知指数或宏观指标')
    begin, finish = parse_day(start), parse_day(end)
    if begin > finish:
        raise ValueError('开始日期不能晚于结束日期')
    if finish > now().date():
        raise ValueError('结束日期不能晚于今天')

    snap = _snapshot()
    source_series = snap.data.get('series') or {}
    cutoff = snap.data.get('as_of')
    if factor == SPREAD:
        bonds = {r['date']: r['value'] for r in clean_series(source_series.get('cn10y'), cutoff)}
        base = equity_bond_spread((snap.data.get('valuations') or {}).get(equity), bonds, cutoff)
    else:
        base = clean_series(source_series.get(factor), cutoff)
    key = _history_key(factor, equity)
    saved, checked = _history_cache(key) if factor in _ON_DEMAND_HISTORY else ([], [])
    failed = False
    earliest = max(begin, parse_day(_BOND_HISTORY_START)) if factor == SPREAD else begin
    if factor in _ON_DEMAND_HISTORY and earliest <= finish:
        baseline_range = [(base[0]['date'], base[-1]['date'])] if base else []
        if list(_missing_days(earliest, finish, [*checked, *baseline_range])):
            # Re-read in the lock so overlapping queries in this process share network work.
            with _history_locks[factor]:
                saved, checked = _history_cache(key)
                missing = list(_missing_days(earliest, finish, [*checked, *baseline_range]))
                if missing:
                    left, right = missing[0][0].isoformat(), missing[-1][1].isoformat()
                    try:
                        incoming = [r for r in _fetch_factor(factor, equity, left, right) if left <= r['date'] <= right]
                    except Exception:  # Provider auth/network/format failures are retryable.
                        failed = True
                    else:
                        with get_conn() as conn:
                            conn.execute('BEGIN IMMEDIATE')
                            saved, checked = _history_cache(key)
                            settled = min(right, (now().date()-dt.timedelta(days=7)).isoformat())
                            if left <= settled:
                                checked = [*checked, [left, settled]]
                            kv_repo.set_(_HISTORY_NAMESPACE, key, {
                                'rows': clean_series([*saved, *incoming]),
                                'covered': checked,
                            })
                        saved, _ = _history_cache(key)

    all_rows = clean_series([*saved, *base], now().date().isoformat())
    rows = [r for r in all_rows if start <= r['date'] <= end]
    available_start = all_rows[0]['date'] if all_rows else None
    available_end = all_rows[-1]['date'] if all_rows else None
    message = None
    if failed:
        message = '历史数据源暂不可用，已显示 SQLite 中此区间的观测。'
    elif not rows:
        message = '所选区间没有实际观测。'
    elif start < available_start or end > available_end:
        message = '所选区间部分超出实际观测覆盖。'
    return {'start': start, 'end': end, 'rows': [{'date': r['date'], 'value': r['value']} for r in rows],
            'available_start': available_start, 'available_end': available_end, 'message': message}


def cn10y_series(start, end):
    """财政部历史查询每段起止日期差须小于365天；tenYear原单位为%。"""
    cursor, stop = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    rows = []
    while cursor <= stop:
        last = min(stop, cursor + dt.timedelta(days=364))
        payload = json.loads(_text("https://yield.chinabond.com.cn/cbweb-czb-web/czb/historyQuery"
                                   f"?startDate={cursor}&endDate={last}&gjqx=10&locale=cn_ZH&qxmc=1"))
        if str(payload.get("flag")) != "0" or not isinstance(payload.get("heList"), list):
            raise ValueError("国债收益率区间查询失败")
        batch = clean_series([{"date": r.get("workTime"), "value": r.get("tenYear")} for r in payload["heList"]], last.isoformat())
        batch = [r for r in batch if r["date"] >= cursor.isoformat()]
        if not batch and (last - cursor).days >= 15:
            raise ValueError("国债收益率历史区间缺失")
        rows.extend(batch)
        cursor = last + dt.timedelta(days=1)
    return rows


def refresh_cn10y(old_rows, end):
    prior = clean_series(old_rows, end)
    start = (dt.date.fromisoformat(prior[-1]["date"]) - dt.timedelta(days=30)).isoformat() if prior else "2014-01-01"
    rows = cn10y_series(start, end)
    if not rows or prior and rows[-1]["date"] < prior[-1]["date"]:
        raise ValueError("国债收益率缺失或最新观测日倒退")
    return clean_series([*prior, *rows], end)


def _document(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup, re.sub(r"\s+", "", soup.get_text(" "))


def listing(url, pages=2):
    found = {}
    for page in range(pages):
        html = _text(url if page == 0 else urljoin(url, f"index_{page}.html"))
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            title, href = a.get("title") or a.get_text("", strip=True), urljoin(url, a["href"])
            if href.startswith("http://"):
                href = "https://" + href[7:]
            if not re.search(r"财政收支情况|地方政府债券发行和债务余额情况", title):
                continue
            if not urlparse(href).hostname.endswith("mof.gov.cn"):
                continue
            surrounding = a.parent.get_text(" ", strip=True)
            dates = re.findall(r"20\d{2}[-/]\d{2}[-/]\d{2}", surrounding)
            found[href] = {"title": title, "url": href, "published_on": dates[-1].replace("/", "-") if dates else None}
    return list(found.values())


def _published(soup, fallback):
    dates = [fallback] if fallback else []
    for tag in soup.find_all("meta"):
        if str(tag.get("name", "")).lower() in ("pubdate", "publishdate", "firstpublishedtime"):
            match = re.search(r"20\d{2}-\d{2}-\d{2}", tag.get("content", ""))
            if match:
                dates.append(match[0])
    return max(dates) if dates else None


def parse_fiscal(html, item):
    soup, text = _document(html)
    title = item["title"]
    match = re.search(r"(20\d{2})年", title)
    if not match:
        return None
    year = int(match[1])
    month_match = re.search(r"(?:1[-—－至]|前|年)(\d{1,2})月", title)
    month = int(month_match[1]) if month_match else 9 if "三季度" in title else 6 if "上半年" in title else 3 if "一季度" in title else 12 if "月" not in title else None
    if not month:
        return None
    def amount(label):
        hit = re.search(label + r"[，,：:]?([\d,.]+)亿元[^。；]{0,45}?同比(增长|下降|持平)([\d.]+)?%?", text)
        if not hit:
            return None, None
        return float(hit[1].replace(",", "")), (float(hit[3] or 0) * (-1 if hit[2] == "下降" else 1))
    spending, spending_yoy = amount("全国一般公共预算支出")
    income, income_yoy = amount("全国一般公共预算收入")
    fund, fund_yoy = amount("全国政府性基金预算支出")
    land, land_yoy = amount("国有土地使用权出让收入")
    if spending is None:
        return None
    return {**item, "period": f"{year:04}-{month:02}", "date": dt.date(year, month, calendar.monthrange(year, month)[1]).isoformat(),
            "published_on": _published(soup, item.get("published_on")), "spending": spending, "spending_yoy": spending_yoy,
            "income": income, "income_yoy": income_yoy, "fund_spending": fund, "fund_spending_yoy": fund_yoy,
            "land_income": land, "land_income_yoy": land_yoy, "unit": "亿元 / 累计同比%"}


def parse_debt(html, item):
    soup, text = _document(html)
    match = re.search(r"(20\d{2})年(\d{1,2})月", item["title"])
    if not match:
        return None
    year, month = int(match[1]), int(match[2])
    # 只读取“当月”章节，避免把1-X月累计金额当作当月发行。
    section = re.split(r"（二）|\(二\)", text.split("当月发行情况", 1)[-1], maxsplit=1)[0]
    def amount(label):
        hit = re.search(label + r"([\d,.]+)亿元", section)
        return float(hit[1].replace(",", "")) if hit else None
    new, refinance = amount("发行新增(?:地方政府)?债券"), amount("发行再融资(?:地方政府)?债券")
    if new is None or refinance is None:
        return None
    return {**item, "period": f"{year:04}-{month:02}", "date": dt.date(year, month, calendar.monthrange(year, month)[1]).isoformat(),
            "published_on": _published(soup, item.get("published_on")), "new": new, "refinance": refinance,
            "total": new + refinance, "unit": "亿元 / 当月"}


def fomc_calendar(html):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    months = {dt.date(2020, m, 1).strftime("%B"): m for m in range(1, 13)}
    for panel in soup.select(".panel"):
        heading = panel.select_one(".panel-heading")
        match = re.search(r"20\d{2}", heading.get_text()) if heading else None
        if not match:
            continue
        year = int(match[0])
        for meeting in panel.select(".fomc-meeting"):
            month_tag, days_tag = meeting.select_one(".fomc-meeting__month"), meeting.select_one(".fomc-meeting__date")
            if not month_tag or not days_tag:
                continue
            month_name = month_tag.get_text(strip=True).split("/")[-1]
            month, days = months.get(month_name), re.findall(r"\d+", days_tag.get_text())
            if not month or not days:
                continue
            date = dt.date(year, month, int(days[-1])).isoformat()
            statement = next((urljoin(FED_URL, a["href"]) for a in meeting.find_all("a", href=True) if "pressreleases/monetary" in a["href"]), None)
            rows.append({"date": date, "title": "FOMC议息会议", "days": days_tag.get_text(strip=True), "sep": "*" in days_tag.get_text(), "url": statement or FED_URL,
                         "status": "已举行" if date <= now().date().isoformat() else "计划日期 / 可能调整"})
    return sorted(rows, key=lambda r: r["date"])


def collect(progress=lambda message: None, old=None):
    old = old if old is not None else bundle()
    end = now().date().isoformat()
    result = {"as_of": end, "series": dict(old.get("series") or {}), "equities": dict(old.get("equities") or {}),
              "valuations": dict(old.get("valuations") or {}), "sectors": dict(old.get("sectors") or {}),
              "fiscal": old.get("fiscal", []), "debt": old.get("debt", []), "calendar": old.get("calendar", []),
              "events": POLICIES, "errors": [], "sources": [], "cycle_flows": list(old.get('cycle_flows') or []),
              "cycle_releases": old.get('cycle_releases') or {}}
    tasks = {}
    start, compact_end = "20140101", end.replace("-", "")
    for key, (code, *_) in FRED.items():
        tasks[("series", key)] = lambda code=code: fred_series(code, end)
    tasks[("series", "cn10y")] = lambda: refresh_cn10y(old.get("series", {}).get("cn10y"), end)
    for key, (code, _) in INDEXES.items():
        tasks[("equities", key)] = lambda code=code: _series(_records("index_daily", ts_code=code, start_date=start, end_date=compact_end, fields="ts_code,trade_date,close"), "close")
        tasks[("valuations", key)] = lambda code=code: [{"date": r["date"], "pe_ttm": r["value"]} for r in _series(_records("index_dailybasic", ts_code=code, start_date=start, end_date=compact_end, fields="ts_code,trade_date,pe_ttm"), "pe_ttm")]
    tasks[("series", "gold")] = lambda: _series(_records("sge_daily", ts_code="Au99.99", start_date=start, end_date=compact_end, fields="ts_code,trade_date,close"), "close")
    def monetary():
        return _records("cn_m", start_m="201301", end_m=compact_end[:6])
    tasks[("raw", "money")] = monetary
    for key, method, field in (("cpi", "cn_cpi", "nt_yoy"), ("ppi", "cn_ppi", "ppi_yoy"), ("pmi", "cn_pmi", "pmi010000")):
        tasks[("series", key)] = lambda method=method, field=field: _series(_records(method, start_m="201401", end_m=compact_end[:6], fields="month," + field), field, "month", "monthly")
    tasks[("raw", "social")] = lambda: _records("sf_month", start_m="201301", end_m=compact_end[:6])
    tasks[("raw", "lpr")] = lambda: _records("shibor_lpr", start_date=start, end_date=compact_end)
    tasks[("series", "shibor3m")] = lambda: _series(_records("shibor", start_date=start, end_date=compact_end), "3m", "date")
    tasks[("series", "gdp")] = lambda: _series(_records("cn_gdp", start_q="2014Q1", end_q=f"{end[:4]}Q4"), "gdp_yoy", "quarter", "quarterly")
    tasks[("calendar", "fomc")] = lambda: fomc_calendar(_text(FED_URL))
    tasks[("raw", "industries")] = lambda: _records("index_classify", level="L1", src="SW2021")
    tasks[("raw", "fiscal_list")] = lambda: listing(FISCAL_URL, pages=3)
    tasks[("raw", "debt_list")] = lambda: listing(DEBT_URL, pages=3)
    tasks[('raw', 'nbs')] = lambda: fetch_releases(_text, end, result['cycle_releases'])
    raw, fresh = {}, set()
    def run(task_map):
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(fn): key for key, fn in task_map.items()}
            for future in as_completed(futures):
                group, key = futures[future]
                try:
                    value = future.result()
                    if not value:
                        raise ValueError("接口未返回有效记录")
                    if group == "raw":
                        raw[key] = value
                    elif group == "calendar":
                        result["calendar"] = value
                    else:
                        existing = result[group].get(key)
                        # 单通道日期倒退时保留旧版本，刷新失败也不改原观测日。
                        existing_rows = existing.get("rows", []) if group == "sectors" and existing else existing
                        new_rows = value.get("rows", []) if group == "sectors" else value
                        if existing_rows and new_rows and existing_rows[-1]["date"] > new_rows[-1]["date"]:
                            raise ValueError("最新观测日期倒退，已保留原序列")
                        result[group][key] = value
                    fresh.add((group, key))
                    progress(f"已读取 {SPECS.get(key, {}).get('name', INDEXES.get(key, ('', key))[1])}")
                except Exception as exc:
                    result["errors"].append(f"{group}/{key}读取未完成（{type(exc).__name__}），保留原日期与原快照")
        close_conn()
    run(tasks)
    if 'nbs' in raw:
        result['cycle_releases'], failures = raw['nbs']
        if failures:
            result['errors'].append(f'库存周期有{failures}页未能读取或解析，保留归档披露')
        if not any(result['cycle_releases'].values()) or result['cycle_releases'] == old.get('cycle_releases'):
            fresh.discard(('raw', 'nbs'))
    # Classify a single observed cross-section, not overlapping sector leaderboards.
    prices = result['equities'].get('shcomp') or []
    if prices and ('equities', 'shcomp') in fresh:
        day = prices[-1]['date'].replace('-', '')
        try:
            point = flow_concentration(_records('daily', trade_date=day, fields='ts_code,trade_date,amount'),
                                       _records('stock_basic', list_status='L', fields='ts_code,industry'),
                                       _records('moneyflow', trade_date=day,
                                                fields='ts_code,trade_date,buy_lg_amount,buy_elg_amount,sell_lg_amount,sell_elg_amount'), day)
            if point['universe']:
                point['captured_on'] = end
                # Keep older vintages available to point-in-time replay.
                points = {(r['date'], r.get('captured_on')): r for r in result['cycle_flows']}
                key = point['date'], end
                if key not in points or points[key].get('value') is None or point['value'] is not None:
                    points[key] = point
                result['cycle_flows'] = list(points.values())
        except Exception:
            result['errors'].append('大单行业集中度未能读取，未推算数值')
    def assign_series(key, rows):
        existing = result["series"].get(key, [])
        if not rows or existing and rows[-1]["date"] < existing[-1]["date"]:
            result["errors"].append(f"{key}有效观测缺失或日期倒退，保留原序列与日期")
            return
        result["series"][key] = rows
        fresh.add(("series", key))
    if "money" in raw:
        for key in ("m1", "m2"):
            assign_series(key, _series(raw["money"], key + "_yoy", "month", "monthly"))
    if "lpr" in raw:
        for key, field in (("lpr1y", "1y"), ("lpr5y", "5y")):
            assign_series(key, _series(raw["lpr"], field, "date"))
    if "social" in raw:
        assign_series("social", _series(raw["social"], "inc_month", "month", "monthly"))
        stocks = _series(raw["social"], "stk_endval", "month", "monthly")
        by_period = {r["period"]: r["value"] for r in stocks}
        assign_series("social_stock", [{**r, "value": (r["value"] / by_period[f"{int(r['period'][:4])-1}{r['period'][4:]}"] - 1) * 100}
            for r in stocks if by_period.get(f"{int(r['period'][:4])-1}{r['period'][4:]}", 0) > 0])
    sector_tasks = {}
    for item in raw.get("industries", []):
        code, name = item.get("index_code"), item.get("industry_name")
        if code and name:
            def sector(code=code, name=name):
                rows = _series(_records("sw_daily", cap=4000, ts_code=code, start_date=start, end_date=compact_end, fields="ts_code,trade_date,close"), "close")
                if not rows:
                    raise ValueError("板块序列为空")
                return {"name": name, "rows": rows}
            sector_tasks[("sectors", code)] = sector
    run(sector_tasks)
    for group, parser in (("fiscal", parse_fiscal), ("debt", parse_debt)):
        articles = {r["url"].replace("http://", "https://", 1): {**r, "url": r["url"].replace("http://", "https://", 1)} for r in result[group]}
        # 已归档原文不重复请求；最新一期仍重取，以覆盖更正。
        pending = sorted(raw.get(group + "_list", []), key=lambda r: r.get("published_on") or r["url"], reverse=True)[:60]
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_text, r["url"]): r for i, r in enumerate(pending) if i == 0 or r["url"] not in articles}
            failures = 0
            for future in as_completed(futures):
                item = futures[future]
                try:
                    parsed = parser(future.result(), item)
                    if parsed and parsed.get("published_on") and parsed["published_on"] <= end and parsed["date"] <= end:
                        articles[item["url"]] = parsed
                    else:
                        failures += 1
                except Exception:
                    failures += 1
        result[group] = sorted({r["period"]: r for r in sorted(articles.values(), key=lambda r: r.get("published_on") or "")}.values(), key=lambda r: r["date"])
        if failures:
            result["errors"].append(f"{group}有{failures}篇原文未能解析，未填零或覆盖原记录")
        progress(f"财政原文：{group} {len(result[group])}期")
    for key, group, field in (("spending", "fiscal", "spending_yoy"), ("local_new", "debt", "new"), ("local_refinance", "debt", "refinance")):
        if result[group]:
            result["series"][key] = clean_series([{k: r.get(k) for k in ("date", "period", "published_on", "url")} | {"value": r.get(field)} for r in result[group]], end)
    result["events"] = POLICIES + [{"date": r["published_on"], "category": "fiscal", "title": r["title"], "summary": f"统计期{r['period']}；数据披露事件，未视作新政策公告。", "url": r["url"], "source": "财政部", "date_kind": "原文/列表刊载日（取较晚值）"} for r in result["fiscal"] + result["debt"]]
    for key, spec in {**SPECS, "cn10y": CN10Y_SOURCE}.items():
        if key == SPREAD:  # 按研究指数动态计算，不是独立采集通道。
            continue
        rows = result["series"].get(key, [])
        result["sources"].append({"name": spec["name"], "source": spec["source"], "url": spec["url"], "rows": len(rows), "date": rows[-1]["date"] if rows else None,
                                  "status": "本次读取" if ("series", key) in fresh else "保留原快照" if rows else "未取得"})
    for group in ("equities", "valuations", "sectors"):
        for key, item in result[group].items():
            rows = item["rows"] if group == "sectors" else item
            result["sources"].append({"name": (item["name"] if group == "sectors" else INDEXES[key][1]) + {"equities": "日线", "valuations": "PE_TTM", "sectors": "行业日线"}[group],
                                      "source": "TuShare / 指数提供方", "url": "https://tushare.pro/document/2?doc_id=" + ("327" if group == "sectors" else "128" if group == "valuations" else "95"),
                                      "date": rows[-1]["date"] if rows else None, "rows": len(rows), "status": "本次读取" if (group, key) in fresh else "保留原快照"})
    if not fresh:
        raise ValueError("公开来源均未读取成功，保留原研究")
    result["updated_at"] = now().isoformat()
    return result


def start_refresh(jobs):
    with _lock:
        job_id = _read("job")
        active = jobs.get(job_id) if job_id else None
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
                result = collect(progress)
                kv_repo.set_(NAMESPACE, "bundle", result)
                jobs.update(job_id, status="finished", progress=100, finished_at=now().isoformat(), result={"as_of": result["as_of"], "notes": result["errors"]})
            except Exception as exc:
                jobs.update(job_id, status="failed", finished_at=now().isoformat(), error=str(exc) if isinstance(exc, ValueError) else f"宏观公开来源读取失败（{type(exc).__name__}），原研究保留")
            finally:
                close_conn()
        job = jobs.start("market_macro", {}, worker)
        kv_repo.set_(NAMESPACE, "job", job["id"])
        return job
