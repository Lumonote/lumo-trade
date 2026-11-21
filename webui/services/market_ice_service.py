"""Fill missing *closed* MA20 breadth observations without blocking the page.

The queue is implicit: benchmark sessions minus valid SQLite observations. Failed
dates cool down independently; a suspension or a provider outage cannot pin the cursor.
"""
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import json
import logging
import re
import threading
import time

import httpx

from analysis.market_health import ICE_MINIMUM, ice_history, merge_ice_history
from data_store import calendar_repo, kv_repo, market_daily_fetch, price_bars_repo, tushare_client
from data_store.connection import close_conn, get_conn
from data_store.opportunity_prices import STOCK_TS_CODE_FILTER
from webui.services.daily_history import fetch_tencent

NS = 'market_health_ice_v1'
BLOCK = 250
_lock = threading.Lock()
_log = logging.getLogger(__name__)


def history():
    return [p for _, p in kv_repo.range_items(NS, 'day:0000', 'day:9999')]


def _closed_days(calendar, observations):
    have = {r['as_of'] for r in merge_ice_history(observations) if not r.get('provisional')}
    return [d for d in calendar if d not in have]


def status(calendar, observations):
    missing = _closed_days(calendar, observations)
    hit = kv_repo.get(NS, 'status')
    retry = kv_repo.get(NS, 'queue')
    deadlines = (retry[0] if retry else {})
    next_retry = min((deadlines.get(d, 0) for d in missing), default=0)
    return {**(hit[0] if hit else {}), 'total': len(calendar), 'missing': len(missing),
            'covered': len(calendar)-len(missing), 'first_missing': missing[0] if missing else None,
            'retry_at': next_retry if next_retry > time.time() else None, 'running': _lock.locked()}


def _window(calendar, gaps):
    last = calendar.index(gaps[-1])
    first = max(0, last-BLOCK+1)
    wanted = [d for d in gaps if d >= calendar[first]]
    begin = calendar.index(wanted[0])
    start = calendar[begin-19] if begin >= 19 else (dt.date.fromisoformat(calendar[0])-dt.timedelta(days=65)).isoformat()
    end = wanted[-1]
    sessions = {d for d in calendar[:last+1] if d >= start}
    sessions.update(f'{d[:4]}-{d[4:6]}-{d[6:]}' for d in calendar_repo.open_days()
                    if start.replace('-', '') <= d <= end.replace('-', ''))
    if begin < 19:
        earlier = (dt.date.fromisoformat(calendar[0])-dt.timedelta(days=1)).isoformat()
        for rows in (price_bars_repo.market_history('sh000001', start, earlier),
                     price_bars_repo.read('tencent', 'sh000001', start=start, end=earlier)):
            sessions.update(r['date'] for r in rows)
        if sum(d < wanted[0] for d in sessions) < 19:
            # A benchmark bar, not a weekday or another stock's trading history,
            # establishes that this was an actual exchange session.
            try:
                bars = fetch_tencent('sh000001', start, earlier)
            except (httpx.HTTPError, OSError, ValueError):
                bars = []
            if bars:
                price_bars_repo.store('tencent', 'sh000001', '1d', bars, start=start, end=earlier)
                sessions.update(r['date'] for r in price_bars_repo.read('tencent', 'sh000001', start=start, end=earlier))
    days = sorted(d for d in sessions if d < wanted[0])[-19:] + sorted(d for d in sessions if d >= wanted[0])
    return wanted, days


def _from_saved(symbols, calendar, wanted, selected_on, stamp):
    end = wanted[-1]
    points = {}
    for source, name in [('sina', '新浪'), ('market_daily', 'TuShare'), ('tencent', '腾讯')]:
        data = price_bars_repo.closes(source, symbols, calendar[0], end)
        if len(data) < ICE_MINIMUM:
            continue
        rows = ice_history(data, wanted, calendar=calendar, sample_as_of=end, calculated_at=stamp,
                           method=source+'_unadjusted_daily_ma20')
        for row in rows:
            d = row['as_of']
            if d not in points or row['denominators']['above_ma20'] > points[d]['denominators']['above_ma20']:
                points[d] = {**row, 'source': name+'未复权日线 · SQLite', 'sample_as_of': selected_on, 'backfilled': True}
    return points


def _whole_market(days, wanted, stamp):
    """Alternate point-in-time universe when the fixed 400 are too young/suspended.

    Only adjacent 20-session closes of the same stock qualify. Never mix these
    counts with the stratified observations or the five-day confirmation cohort.
    """
    if len(days)<20 or not wanted:
        return {}
    query = f'''WITH sessions AS (SELECT key AS i,value AS d FROM json_each(?)),
        bars AS (SELECT ts_code,i,d,close,lag(i,19) OVER w AS first,
                 avg(close) OVER (w ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma
                 FROM sessions JOIN market_daily ON trade_date=d WHERE close>0 AND {STOCK_TS_CODE_FILTER}
                 WINDOW w AS (PARTITION BY ts_code ORDER BY i))
        SELECT d,ts_code,close>ma+abs(close)*1e-12 FROM bars WHERE i-first=19 ORDER BY d,ts_code'''
    wanted = set(wanted); counts = {}
    for day, code, above in get_conn().execute(query, (json.dumps([d.replace('-', '') for d in days]),)):
        day = f'{day[:4]}-{day[4:6]}-{day[6:]}'
        if day not in wanted:
            continue
        group = counts.setdefault(day, [0, 0, hashlib.sha256()]); group[0] += 1; group[1] += above; group[2].update(code.encode())
    return {d: {'as_of': d, 'above_ma20': 100*n/total, 'above_count': n, 'denominators': {'above_ma20': total},
                'cohort': h.hexdigest()[:16], 'method': 'market_daily_all_a_ma20', 'source': 'TuShare 全A有效样本 · SQLite',
                'sample_as_of': d, 'calculated_at': stamp, 'provisional': False, 'backfilled': True}
            for d,(total,n,h) in counts.items() if total >= ICE_MINIMUM}


def saved_market_observations(days, target_days, observations):
    """Use actual point-in-time market partitions for deep history without today's sample cohort."""
    pending = set(target_days) & set(_closed_days(days, observations))
    if not pending or len(days) < 20:
        return []
    points = {}
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    for i in range(19, len(days), BLOCK):
        window = days[i-19:i+BLOCK]
        filled = market_daily_fetch.filled_dates(window)
        eligible = [d for j, d in enumerate(window[19:], 19) if d in pending and
                    all(p.replace('-', '') in filled for p in window[j-19:j+1])]
        if eligible:
            points.update(_whole_market(window, eligible, stamp))
    for day, row in points.items():
        kv_repo.set_(NS, 'day:'+day, row)
    return list(points.values())


def _public_fill(symbols, days, needed):
    cached = price_bars_repo.closes('tencent', symbols, min(needed), max(needed))
    targets = []
    for s in symbols:
        missing = needed - {r['date'] for r in cached.get(s, ())}
        if missing:
            # Split non-adjacent holes; a single missing bar costs a one-day request.
            ranges = []
            for i, d in enumerate(days):
                if d in missing:
                    if i and days[i-1] in missing:
                        ranges[-1][1] = d
                    else:
                        ranges.append([d, d])
            targets.append((s, ranges))
    def fetch(target):
        symbol, ranges = target
        wrote = 0
        for start, end in ranges:
            try:
                rows = fetch_tencent(symbol, start, end)
            except (httpx.HTTPError, OSError):
                return None if not wrote else wrote
            except ValueError:
                continue
            wrote += price_bars_repo.store('tencent', symbol, '1d', rows, start=start, end=end)
        return wrote

    # Only transport failures trip the circuit breaker, not valid empty listings.
    cooldown = kv_repo.get(NS, 'public_retry_at')
    if cooldown and cooldown[0] > time.time():
        return
    with ThreadPoolExecutor(max_workers=4) as pool:
        for i in range(0, len(targets), 12):
            batch = list(pool.map(fetch, targets[i:i+12]))
            if len(batch) >= 4 and all(n is None for n in batch):
                kv_repo.set_(NS, 'public_retry_at', time.time()+600)
                break


def fill_block(calendar, gaps, symbols, selected_on):
    """One bounded unit of work. Recheck every input from SQLite after fetching."""
    wanted, days = _window(calendar, gaps)
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    ix = {d: i for i, d in enumerate(days)}
    windows = {d: days[ix[d]-19:ix[d]+1] for d in wanted if ix[d] >= 19}
    if not windows:
        return wanted, {}, 1800

    def calculate():
        p = _from_saved(symbols, days, sorted(windows), selected_on, stamp)
        # A partial daily import is not an all-A universe. Keep the fallback
        # independently identified and require populated market partitions.
        alternate = [d for d, w in windows.items() if d not in p and all(v in filled for v in w)]
        p.update(_whole_market(days, alternate, stamp))
        return p

    def dependencies(missing):
        return {prior for d in missing for prior in windows[d]}

    def filled_sessions():
        return {f'{d[:4]}-{d[4:6]}-{d[6:]}' for d in market_daily_fetch.filled_dates(days)}

    filled = filled_sessions()
    points = calculate()
    missing = windows.keys()-points.keys()
    delay = 1800
    if missing:
        # Sparse single-day holes in a mostly populated market_daily need one request per day, not one per stock.
        needed = dependencies(missing)
        absent = needed-filled
        tried_market = bool(needed & filled and 0 < len(absent) <= 24 and tushare_client.available())
        if tried_market:
            market_daily_fetch.ensure_dates(sorted(absent))
            filled = filled_sessions()
            points = calculate()
            missing = windows.keys()-points.keys()
        if missing and len(symbols) >= ICE_MINIMUM:
            _public_fill(symbols, days, dependencies(missing))
            points = calculate()
            missing = windows.keys()-points.keys()
        absent = dependencies(missing)-filled
        if absent and not tried_market and tushare_client.available():
            result = market_daily_fetch.ensure_dates(sorted(absent), max_fetch=24)
            if result['fetched']:
                filled = filled_sessions()
                points = calculate()
                if result['remaining']:
                    delay = 30  # Bounded TuShare batch; continue on the next page poll.
    for d, p in points.items():
        kv_repo.set_(NS, 'day:'+d, p)
    return wanted, points, delay


def ensure(calendar, observations, breadth):
    """Nonblocking, resumable on the next page poll or restart. No duplicate workers."""
    gaps = _closed_days(calendar, observations)
    if not gaps or not _lock.acquire(blocking=False):
        return
    try:
        prior = kv_repo.get(NS, 'queue'); retry = (prior[0] if prior else {})
        pending = [d for d in gaps if retry.get(d, 0) <= time.time()]
        if not pending:
            _lock.release(); return
        symbols = sorted({s for s in breadth.get('sample_symbols', []) if isinstance(s, str) and re.fullmatch(r'(sh|sz)\d{6}', s)})
        def worker(retry):
            try:
                remaining = pending
                while remaining:
                    batch, points, delay = fill_block(calendar, remaining, symbols, breadth.get('sample_as_of') or breadth.get('as_of'))
                    done = set(batch)
                    retry = {d:t for d,t in retry.items() if d not in done and t>time.time()}
                    retry.update(dict.fromkeys(done-points.keys(), time.time()+delay))
                    remaining = [d for d in remaining if d not in done]
                    kv_repo.set_(NS, 'queue', retry)
                    kv_repo.set_(NS, 'status', {'last_batch': len(batch), 'recovered': len(points), 'updated_at': time.time(),
                                 'note': '样本不足的日期将分批重试' if len(points)<len(batch) else '本批缺口已补齐'})
            except Exception:
                _log.exception('Ice history fill interrupted')
                kv_repo.set_(NS, 'queue', {**retry, **dict.fromkeys(remaining, time.time()+600)})
                kv_repo.set_(NS, 'status', {'updated_at':time.time(), 'note':'本轮补数失败，稍后自动重试'})
            finally:
                close_conn(); _lock.release()
        threading.Thread(target=worker, args=(retry,), name='market-ice-fill', daemon=True).start()
    except BaseException:
        _lock.release(); raise
