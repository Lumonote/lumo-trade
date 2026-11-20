"""Disclosure-aware inventory cycle and independent valuation cross-checks."""
from bisect import bisect_right
import datetime as dt
from statistics import median

from analysis.market_health import number
from analysis.market_health_fusion import iso

AXES = ('new_orders', 'finished_inventory')
PHASES = {(1, 1): '主动补库', (-1, 1): '被动补库', (-1, -1): '主动去库', (1, -1): '被动去库'}


def month_offset(period, n):
    year, month = map(int, period.split('-'))
    y, m = divmod(year * 12 + month - 1 + n, 12)
    return f'{y:04}-{m + 1:02}'


def cycle_rows(releases, end):
    axes = []
    for key in AXES:
        months = {}
        for r in sorted(releases.get(key, []), key=lambda x: x.get('published_on') or ''):
            pub, period = iso(r.get('published_on')), r.get('period')
            if pub and pub <= end and period and iso(period + '-01') and number(r.get('value')) is not None:
                months.setdefault(period, []).append(r)
        axes.append(months)
    periods = sorted(set(axes[0]) | set(axes[1]))
    rows = []
    for p in periods:
        row = {'period': p}
        versions, diffs = [], []
        for key, months in zip(AXES, axes):
            current, prior = months.get(p, []), months.get(month_offset(p, -1), [])
            delta = round(current[-1]['value'] - prior[-1]['value'], 6) if current and prior else None
            row[key] = current[-1] if current else None
            row[key + '_delta'] = delta
            diffs.append(delta)
            versions.extend([current, prior])
        row['phase'] = '待披露 / 缺月' if None in diffs else PHASES.get(tuple((d > 0) - (d < 0) for d in diffs), '过渡 / 方向不明')
        row['known_on'] = max(v[-1]['published_on'] for v in versions) if all(versions) else None
        # Do not backdate a corrected reading as if it had been the original signal.
        row['unrevised'] = all(len(v) == 1 for v in versions)
        rows.append(row)
    return rows


def forward_sample(prices, i, horizon):
    if i + horizon >= len(prices):
        return None
    window = prices[i:i + horizon + 1]
    peak, drawdown = window[0]['value'], 0.
    for r in window:
        peak = max(peak, r['value'])
        drawdown = min(drawdown, r['value'] / peak - 1)
    return (window[-1]['value'] / window[0]['value'] - 1) * 100, drawdown * 100


def historical_check(rows, prices):
    dates = [r['date'] for r in prices]
    signals = [(r['phase'], bisect_right(dates, r['known_on'])) for r in rows
               if r['phase'] in PHASES.values() and r['unrevised'] and dates and r['known_on'] >= dates[0]]
    first = min((i for _, i in signals), default=len(prices))
    result = []
    for horizon in (20, 60):
        outcomes = [forward_sample(prices, i, horizon) for i in range(first, max(first, len(prices) - horizon))]
        for label in ['同期无条件', *PHASES.values()]:
            starts = range(first, first + len(outcomes)) if label == '同期无条件' else sorted(i for phase, i in signals if phase == label and i + horizon < len(prices))
            sample = [outcomes[i - first] for i in starts]
            result.append({'phase': label, 'days': horizon, 'n': len(sample),
                           'overlaps': sum(b < a + horizon for a, b in zip(starts, starts[1:])),
                           'return_pct': median(r[0] for r in sample) if sample else None,
                           'drawdown_pct': median(r[1] for r in sample) if sample else None})
    return result


def credit_change(rows, cutoff, replay=False):
    by_month = {r['date'][:7]: r for r in rows if iso(r.get('date')) and r['date'] <= cutoff and number(r.get('value')) is not None
                and (not r.get('published_on') or r['published_on'] <= cutoff) and (not replay or iso(r.get('published_on')))}
    period = max(by_month, default=None)
    required = [by_month.get(month_offset(period, n)) for n in (0, -1, -2, -12, -13, -14)] if period else []
    delta = (sum(r['value'] for r in required[:3]) - sum(r['value'] for r in required[3:])) if required and all(required) else None
    dated = bool(required and all(r and iso(r.get('published_on')) for r in required))
    return {'period': period, 'value': delta, 'published_on': max(r['published_on'] for r in required) if dated else None,
            'status': '待确认' if delta is None else '同比多增' if delta > 0 else '同比少增' if delta < 0 else '同比持平'}


def industry_breadth(sectors, dates):
    def window(days):
        up = n = 0
        if len(days) != 21:
            return up, n, None
        for sector in sectors.values():
            edge = {r['date']: number(r.get('value')) for r in sector.get('rows', []) if r['date'] in (days[0], days[-1])}
            if all((edge.get(d) or 0) > 0 for d in (days[0], days[-1])):
                n += 1
                up += edge[days[-1]] > edge[days[0]]
        return up, n, up / n * 100 if n == 31 else None

    up, n, current = window(dates[-21:])
    _, _, previous = window(dates[-41:-20])
    return {'as_of': dates[-1] if dates else None, 'up': up, 'n': n, 'value': current,
            'previous_value': previous, 'change_pp': current - previous if current is not None and previous is not None else None}


def flow_concentration(quotes, classifications, flows, day):
    """One stock, one industry; never sum overlapping sector leaderboards."""
    members = {r['ts_code'] for r in quotes if r.get('trade_date') == day and r['ts_code'].endswith(('.SH', '.SZ')) and (number(r.get('amount')) or 0) > 0}
    names = {r['ts_code']: r.get('industry') for r in classifications}
    by_code = {r['ts_code']: r for r in flows if r.get('trade_date') == day}
    totals, covered = {}, 0
    for code in members:
        values = [number(by_code.get(code, {}).get(k)) for k in ('buy_lg_amount', 'buy_elg_amount', 'sell_lg_amount', 'sell_elg_amount')]
        if not names.get(code) or None in values:
            continue
        covered += 1
        totals[names[code]] = totals.get(names[code], 0) + sum(values[:2]) - sum(values[2:])
    positive = sorted((v for v in totals.values() if v > 0), reverse=True)
    return {'date': iso(day), 'covered': covered, 'universe': len(members), 'industries': len(totals),
            'value': sum(positive[:3]) / sum(positive) * 100 if members and covered == len(members) and positive else None}


def cycle_report(bundle, equity, cutoff=None):
    end = iso(bundle.get('as_of')) or dt.date.today().isoformat()
    if cutoff and (iso(cutoff) != cutoff or cutoff > end):
        raise ValueError('回放截止日无效或晚于快照')
    end = cutoff or end
    rows = cycle_rows(bundle.get('cycle_releases', {}), end)
    prices = sorted((r for r in bundle.get('equities', {}).get(equity, []) if r['date'] <= end and (number(r.get('value')) or 0) > 0), key=lambda r: r['date'])
    dates = [r['date'] for r in prices]
    matching_flows = (r for r in bundle.get('cycle_flows', [])
                      if dates and r['date'] == dates[-1] and (not cutoff or r.get('captured_on', '9999') <= cutoff))
    flow = max(matching_flows, key=lambda r: (r.get('value') is not None, r.get('captured_on') or ''), default=None)
    return {'cutoff': end, 'equity': equity, 'rows': rows, 'study': historical_check(rows, prices),
            'credit': credit_change(bundle.get('series', {}).get('social', []), end, bool(cutoff)),
            'breadth': industry_breadth(bundle.get('sectors', {}), dates), 'flow': flow}
