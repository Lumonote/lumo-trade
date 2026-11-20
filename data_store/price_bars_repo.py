"""SQLite series for public quotes. Never splice providers or volume units."""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict

from data_store.connection import get_conn


def _num(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def store(source, symbol, frequency, records, *, provisional_day=None, start=None, end=None):
    rows = []
    for r in records:
        try:
            stamp = dt.datetime.fromisoformat(str(r.get('date') or r.get('day')))
        except (AttributeError, ValueError):
            continue
        day = stamp.date().isoformat()
        if (start and day < start) or (end and day > end) or (frequency != '1d' and stamp.time() == dt.time()):
            continue
        prices = [_num(r.get(k)) for k in ('open', 'high', 'low', 'close', 'volume', 'amount')]
        if not prices[3] or prices[3] <= 0:
            continue
        ts = day if frequency == '1d' else stamp.isoformat(sep=' ', timespec='seconds')
        rows.append((source, symbol, frequency, ts, *prices, int(day == provisional_day or bool(r.get('provisional')))))
    if not rows:
        return 0
    conn = get_conn()
    with conn:
        conn.execute('SAVEPOINT store_bars')
        conn.executemany('''INSERT INTO provider_bar VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source,symbol,frequency,ts) DO UPDATE SET open=coalesce(excluded.open,open),
            high=coalesce(excluded.high,high), low=coalesce(excluded.low,low), close=excluded.close,
            volume=coalesce(excluded.volume,volume), amount=coalesce(excluded.amount,amount), provisional=excluded.provisional
            WHERE excluded.provisional <= provisional''', rows)
        conn.execute('RELEASE store_bars')
    return len(rows)


def read(source, symbol, frequency='1d', *, start=None, end=None, limit=None, live_day=None):
    params = [source, symbol, frequency]
    query = 'SELECT ts AS date,open,high,low,close,volume,amount,provisional FROM provider_bar WHERE source=? AND symbol=? AND frequency=?'
    if start:
        query += ' AND ts>=?'; params.append(start)
    if end:
        query += ' AND ts<=?'; params.append(end+' 23:59:59')
    query += ' AND (provisional=0 OR substr(ts,1,10)=?)'; params.append(live_day or '')
    query += ' ORDER BY ts DESC'
    if limit:
        query += ' LIMIT ?'; params.append(limit)
    return [dict(r) for r in reversed(get_conn().execute(query, params).fetchall())]


def market_history(symbol, start=None, end=None, limit=None):
    query = '''SELECT trade_date,open,high,low,close,vol AS volume,amount FROM market_daily
               WHERE ts_code=? AND close>0'''
    params = [symbol[2:]+'.'+symbol[:2].upper()]
    for op, value in [('>=', start), ('<=', end)]:
        if value:
            query += ' AND trade_date'+op+'?'; params.append(value.replace('-', ''))
    query += ' ORDER BY trade_date DESC'
    if limit:
        query += ' LIMIT ?'; params.append(limit)
    return [{**dict(r), 'date': f"{r[0][:4]}-{r[0][4:6]}-{r[0][6:8]}"} for r in reversed(get_conn().execute(query, params).fetchall())]


def closes(source, symbols, start, end):
    """One indexed range per member; never load full-market frames to compute a sample."""
    histories = defaultdict(list)
    if not symbols:
        return histories
    marks = ','.join('?' for _ in symbols)
    if source == 'market_daily':
        names = {s[2:]+'.'+s[:2].upper(): s for s in symbols}
        query = f'SELECT ts_code,trade_date,close FROM market_daily WHERE ts_code IN ({marks}) AND trade_date BETWEEN ? AND ? AND close>0 ORDER BY ts_code,trade_date'
        for code, date, close in get_conn().execute(query, (*names, start.replace('-', ''), end.replace('-', ''))):
            histories[names[code]].append({'date': f'{date[:4]}-{date[4:6]}-{date[6:]}', 'close': close})
    else:
        query = f"SELECT symbol,ts,close FROM provider_bar WHERE source=? AND symbol IN ({marks}) AND frequency='1d' AND ts BETWEEN ? AND ? AND provisional=0 ORDER BY symbol,ts"
        for symbol, day, close in get_conn().execute(query, (source, *symbols, start, end)):
            histories[symbol].append({'date': day, 'close': close})
    return histories


def datasets():
    valid = 'open>0 AND low>0 AND low<=min(open,close) AND high>=max(open,close)'
    rows = get_conn().execute(f'''SELECT 'ohlcv' AS source,code AS symbol,frequency,count(*) FROM ohlcv
          WHERE {valid} GROUP BY code,frequency
        UNION ALL SELECT source,symbol,frequency,count(*) FROM provider_bar
          WHERE provisional=0 AND {valid} GROUP BY source,symbol,frequency
        UNION ALL SELECT 'market_daily',ts_code,'1d',count(*) FROM market_daily
          WHERE {valid} GROUP BY ts_code
        ORDER BY symbol,frequency,source''')
    labels = {'ohlcv': '本地已导入', 'market_daily': '市场日线库', 'sina': '新浪', 'tencent': '腾讯'}
    return [{'name': f'{symbol} · {freq} · {labels.get(source, source)}',
             'path': f'sqlite:{source}:{symbol}:{freq}', 'size': f'{n} 根'}
            for source, symbol, freq, n in rows]
