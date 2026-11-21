"""Date-bounded unadjusted public daily bars (independent of Sina's trailing window)."""
import datetime as dt
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import islice
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from data_store import kv_repo, price_bars_repo
from data_store.connection import get_conn
from webui.services.http_client import request_json

_FIRST_SESSION_YEAR = dt.date(1990, 1, 1)
_WINDOW = dt.timedelta(days=500)
_DAY = dt.timedelta(days=1)
_LOCKS = [threading.Lock() for _ in range(64)]
_COVERAGE = 'tencent_daily_coverage_v1'


def parse_day(value):
    try:
        day = dt.date.fromisoformat(value)
        if day.isoformat() == value:
            return day
    except (TypeError, ValueError):
        pass
    raise ValueError('日期必须为 YYYY-MM-DD')


def last_closed_day(now=None):
    now = now or dt.datetime.now(ZoneInfo('Asia/Shanghai'))
    return now.date() - _DAY if now.time() < dt.time(15, 30) else now.date()


def _chunks(start, end):
    while start <= end:
        stop = min(start + _WINDOW, end)
        yield start, stop
        start = stop + _DAY


def fetch_tencent(symbol, start, end):
    # Split long requests: the server caps response length even with explicit bounds.
    records = []
    for first, last in _chunks(parse_day(start), parse_day(end)):
        query = urlencode({'param': f'{symbol},day,{first},{last},600'})
        data = request_json('https://web.ifzq.gtimg.cn/appstock/app/kline/kline?'+query,
                            headers={'Referer': 'https://gu.qq.com/', 'User-Agent': 'Mozilla/5.0'},
                            timeout=8, retries=1, trust_env=False)
        if not isinstance(data, dict) or str(data.get('code')) != '0':
            raise ValueError('腾讯日线接口未返回有效响应')
        payload = data.get('data')
        if payload == []:
            continue
        if not isinstance(payload, dict) or not isinstance(block := payload.get(symbol), dict):
            raise ValueError('腾讯日线响应结构异常')
        if 'day' not in block and ('qfqday' in block or 'hfqday' in block):
            raise ValueError('腾讯未返回未复权日线')
        rows = block.get('day') or []
        if not isinstance(rows, list):
            raise ValueError('腾讯日线记录格式异常')
        matches = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                raise ValueError('腾讯日线字段不完整')
            if first <= parse_day(row[0]) <= last:
                matches.append(dict(zip(('date', 'open', 'close', 'high', 'low', 'volume'), row)))
        if rows and not matches:
            raise ValueError('腾讯日线返回了区间外记录')
        records.extend(matches)
    return records


def _missing(start, end, intervals):
    for left, right in sorted(intervals):
        if right < start:
            continue
        if left > end:
            break
        if left > start:
            yield start, left - _DAY
        start = max(start, right + _DAY)
        if start > end:
            return
    if start <= end:
        yield start, end


def _coverage_for(symbol):
    data = kv_repo.get(_COVERAGE, symbol)
    return [(parse_day(a), parse_day(b), expires) for a, b, expires in (data[0] if data else [])
            if expires is None or expires > time.time()]


def _save_coverage(symbol, receipts):
    merged = []
    for start, end, expires in sorted(receipts, key=lambda item: item[:2]):
        if merged and expires == merged[-1][2] and start <= merged[-1][1] + _DAY:
            merged[-1] = merged[-1][0], max(merged[-1][1], end), expires
        else:
            merged.append((start, end, expires))
    kv_repo.set_(_COVERAGE, symbol, [(a.isoformat(), b.isoformat(), expires) for a, b, expires in merged])


def _known_sessions(start, end):
    conn = get_conn()
    first, last = conn.execute('SELECT min(cal_date),max(cal_date) FROM trade_calendar').fetchone()
    lo, hi = start.strftime('%Y%m%d'), end.strftime('%Y%m%d')
    if not first or first > lo or last < hi:
        return set()
    return {dt.date.fromisoformat(r[0]).isoformat() for r in conn.execute('''SELECT cal_date FROM trade_calendar
            WHERE is_open=1 AND cal_date BETWEEN ? AND ?''', (lo, hi))}


def _download(symbol, start, end):
    try:
        return fetch_tencent(symbol, start.isoformat(), end.isoformat())
    except (httpx.HTTPError, OSError, ValueError):
        return None


def _fill(symbol, intervals, checks):
    chunks = (chunk for left, right in intervals for chunk in _chunks(left, right))
    deadline = time.monotonic() + 18
    with ThreadPoolExecutor(max_workers=4) as pool:
        while time.monotonic() < deadline and (batch := list(islice(chunks, 4))):
            downloads = [(a, b, pool.submit(_download, symbol, a, b)) for a, b in batch]
            for a, b, future in downloads:
                rows = future.result()
                if rows is None:
                    continue
                written = price_bars_repo.store('tencent', symbol, '1d', rows, start=a.isoformat(), end=b.isoformat())
                if written == len(rows):
                    checks.append((a, b, None if written else int(time.time()) + 86400))
            _save_coverage(symbol, checks)
            if all(future.result() is None for _, _, future in downloads):
                break


def load_range(symbol, start, end, *, warmup=0, now=None, local_rows=None):
    """Read a single daily series from SQLite, downloading only unchecked intervals."""
    if not re.fullmatch(r'(sh|sz|bj)\d{6}', symbol):
        raise ValueError('无效的交易所行情代码')
    first, last = parse_day(start), parse_day(end)
    if first > last:
        raise ValueError('开始日期不能晚于结束日期')
    lo, hi = max(first, _FIRST_SESSION_YEAR), min(last, last_closed_day(now))
    if lo > hi:
        return [], 'sqlite', '该日期区间没有已收盘的 A 股日线。'
    padded = max(_FIRST_SESSION_YEAR, lo - dt.timedelta(days=warmup * 2 + 35 if warmup else 0))
    lower, upper = padded.isoformat(), hi.isoformat()
    start_day = lo.isoformat()

    def read():
        found = [('sqlite:'+source, price_bars_repo.read(source, symbol, start=lower, end=upper))
                 for source in ('tencent', 'sina')]
        found.append(('sqlite:market_daily', price_bars_repo.market_history(symbol, start=lower, end=upper)))
        if local_rows:
            found.append(('sqlite:ohlcv', local_rows(lower, upper)))
        return found

    def split(rows):
        visible = [r for r in rows if r['date'] >= start_day]
        prior = [r for r in rows if r['date'] < start_day]
        return visible, prior[-warmup:] if warmup else []

    def covered(rows, sessions):
        return bool(sessions) and sessions.issubset(r['date'] for r in rows)

    with _LOCKS[hash(symbol) % len(_LOCKS)]:
        choices = read()
        sessions = _known_sessions(lo, hi)
        best_source, best = max(choices, key=lambda item: tuple(map(len, split(item[1]))))
        visible, prior = split(best)
        checks = _coverage_for(symbol)
        unverified = list(_missing(padded, hi, [(a, b) for a, b, _ in checks]))
        if unverified and not (covered(visible, sessions) and len(prior) >= warmup):
            _fill(symbol, unverified, checks)
            choices = read()

        best_source, best = max(choices, key=lambda item: tuple(map(len, split(item[1]))))
        visible, prior = split(best)
        pending = any(_missing(lo, hi, [(a, b) for a, b, _ in checks])) and not covered(visible, sessions)
        if not visible:
            msg = '该区间的日线尚未补齐，请稍后重试。' if pending else '该区间未取得该标的的日线，请核对实际上市或编制时间。'
            return [], 'sqlite', msg
        msg = f'展示已入库日线 {visible[0]["date"]} 至 {visible[-1]["date"]}。'
        if pending:
            return prior + visible, best_source, '该区间仍有数据待补。' + msg
        if parse_day(visible[0]['date']) - lo > dt.timedelta(days=7) or hi - parse_day(visible[-1]['date']) > dt.timedelta(days=7):
            return prior + visible, best_source, msg
        return prior + visible, best_source, None
