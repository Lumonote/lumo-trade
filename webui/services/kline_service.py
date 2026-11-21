"""Stock K-line data loading for WebUI routes."""

from __future__ import annotations

import datetime
import math
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from analysis.limit_up_patterns import bars_from_records, detect_kline_patterns
from analysis.operation_models import analyze_operations
from analysis.panic_indicator import panic_from_bars, panic_indicator
from data_store import kv_repo, ohlcv_repo, price_bars_repo
from data_store.connection import get_conn
from webui.services.analysis_jobs import normalize_stock_codes, safe_int
from webui.services.daily_history import fetch_tencent, last_closed_day, load_range, parse_day
from webui.services.http_client import request_json


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, '', '—', 'N/A'):
            return default
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


class StockKlineService:
    SINA_WINDOW = 1023  # Request window of this provider, not a local-history retention limit.
    FREQUENCIES = {'daily': '1d', 'day': '1d', '1d': '1d', '1h': '60m',
                   **{p: f'{n}m' for n in (5, 15, 30, 60) for p in (str(n), f'{n}m')}}
    SINA_CACHE_TTL = 60.0
    _locks = [threading.Lock() for _ in range(128)]

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        # 实时报价源（如 WatchlistService.quotes）。日K是隔日/盘中按天一根的快照，
        # 叠加当日最新价后，盘中那根bar与「实时价」徽章可跟随最新成交价。
        self._quote_provider: Optional[Callable[[list], dict]] = None

    def set_quote_provider(self, provider: Optional[Callable[[list], dict]]) -> None:
        """注入/更换实时报价源（如 WatchlistService.quotes）。与自选行情共用同一行情口径。"""
        self._quote_provider = provider

    @staticmethod
    def sina_symbol_for_code(stock_code: str) -> str:
        code = str(stock_code or '').strip().zfill(6)
        if code.startswith(('43', '83', '87', '92')):
            return f'bj{code}'
        if code.startswith(('5', '6', '9')):
            return f'sh{code}'
        return f'sz{code}'

    @staticmethod
    def period_to_sina_scale(period: str) -> str:
        period_map = {
            '5m': '5',
            '5': '5',
            '15m': '15',
            '15': '15',
            '30m': '30',
            '30': '30',
            '60m': '60',
            '60': '60',
            '1h': '60',
            'daily': '240',
            '1d': '240',
            'day': '240',
        }
        return period_map.get(str(period or 'daily'), '240')

    @staticmethod
    def frame_to_kline_records(df: Any, limit: int | None, period='daily', end=None) -> list[dict[str, Any]]:
        import pandas as pd

        if df is None or df.empty:
            return []

        work = df.copy()
        timestamp_col = next(
            (col for col in ['timestamps', 'timestamp', 'date', 'datetime', 'time', 'trade_date'] if col in work.columns),
            None,
        )
        if not timestamp_col:
            return []
        stamp = pd.to_datetime(work[timestamp_col].astype(str), errors='coerce')
        daily = StockKlineService.FREQUENCIES[period] == '1d'
        work['date'] = stamp.dt.strftime('%Y-%m-%d' if daily else '%Y-%m-%d %H:%M:%S')
        work = work[stamp.notna() & (True if daily else stamp.ne(stamp.dt.normalize()))]
        if end:
            work = work[work['date'].str[:10] <= end]
        work = work.sort_values('date', kind='stable').drop_duplicates('date', keep='last')
        return StockKlineService.parse_sina_klines((work.tail(limit) if limit else work).to_dict('records'))

    @staticmethod
    def parse_sina_klines(klines: list[Any]) -> list[dict[str, Any]]:
        records = []
        prev_close = None
        for item in klines or []:
            if not isinstance(item, dict):
                continue
            open_price = _safe_float(item.get('open'), None)
            close_price = _safe_float(item.get('close'))
            if close_price <= 0:
                continue
            high_price = _safe_float(item.get('high'), None)
            low_price = _safe_float(item.get('low'), None)
            pct_chg = 0.0
            if prev_close and prev_close > 0:
                pct_chg = (close_price / prev_close - 1) * 100
            records.append({
                'date': str(item.get('day') or item.get('date') or ''),
                'open': open_price,
                'close': close_price,
                'high': high_price,
                'low': low_price,
                'volume': _safe_float(item.get('volume'), None),
                'amount': _safe_float(item.get('amount'), None),
                'pct_chg': round(pct_chg, 2),
                **({'provisional': True} if item.get('provisional') else {}),
            })
            prev_close = close_price
        return records

    def fetch_sina_kline(self, stock_code: str, period: str, limit: int) -> tuple[list[dict[str, Any]], str]:
        params = {
            'symbol': self.sina_symbol_for_code(stock_code),
            'scale': self.period_to_sina_scale(period),
            'ma': 'no',
            'datalen': str(limit),
        }
        url = (
            'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'
            'CN_MarketData.getKLineData?' + urllib.parse.urlencode(params)
        )
        payload = request_json(
            url,
            headers={
                'User-Agent': (
                    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
                ),
                'Accept': 'application/json,text/plain,*/*',
                'Referer': 'https://finance.sina.com.cn/',
            },
            timeout=8, retries=1, trust_env=False,
        )
        records = self.parse_sina_klines(payload if isinstance(payload, list) else [])
        return records, ''

    @staticmethod
    def _clock():
        return datetime.datetime.now(ZoneInfo('Asia/Shanghai'))

    def history(self, code, period, limit=None, *, end=None):
        """Return one SQLite series, optionally filling missing bars before reading it again."""
        freq, symbol = self.FREQUENCIES[period], self.sina_symbol_for_code(code)
        now = self._clock()
        today = now.date().isoformat()
        stop = min(end or today, today)
        provisional = today if now.time() < datetime.time(15, 30) else None
        target = datetime.date.fromisoformat(stop)
        if target == now.date() and (now.time() < datetime.time(9, 30) or (end and provisional)):
            target -= datetime.timedelta(days=1)
        cal = get_conn().execute('SELECT max(cal_date),max(CASE WHEN is_open=1 AND cal_date<=? THEN cal_date END) FROM trade_calendar',
                                 (target.strftime('%Y%m%d'),)).fetchone()
        while target.weekday() >= 5:
            target -= datetime.timedelta(days=1)
        expected = target.isoformat()
        if cal[0] and cal[1] and cal[0] >= expected.replace('-', ''):
            expected = f'{cal[1][:4]}-{cal[1][4:6]}-{cal[1][6:]}'
        need = limit or self.SINA_WINDOW
        live_day = provisional if not end else None

        def read():
            choices = [(price_bars_repo.read(s, symbol, freq, end=stop, limit=limit, live_day=live_day), s)
                       for s in (('sina', 'tencent') if freq == '1d' else ('sina',))]
            if freq == '1d':
                choices.append((price_bars_repo.market_history(symbol, end=stop, limit=limit), 'market_daily'))
            frame = ohlcv_repo.load_dataframe(code, freq, limit, end=stop)
            choices.append((self.frame_to_kline_records(frame, limit, period, stop), 'ohlcv'))
            def rank(item):
                bars = item[0]
                last = bars[-1]['date'] if bars else ''
                recent = last[:10] >= expected
                return (recent, len(bars) if recent else 0, last, len(bars))
            return max(choices, key=rank)

        with self._locks[hash((code, freq)) % len(self._locks)]:
            bars, source = read()
            recent = bool(bars) and bars[-1]['date'][:10] >= expected
            updating = not end and provisional and expected == today and now.weekday() < 5
            key = f'{symbol}:{freq}:{end or "live"}'
            attempt = kv_repo.get('kline_fill', key)
            cooling = (attempt and attempt[0].get('need', 0) >= need and attempt[0].get('phase') == (provisional or 'closed')
                       and time.time()-attempt[1] < self.SINA_CACHE_TTL)
            detail = attempt[0].get('error') if cooling else None
            if not cooling and (not recent or len(bars) < need or updating):
                try:
                    incoming, _ = self.fetch_sina_kline(code, period, self.SINA_WINDOW if end else min(need, self.SINA_WINDOW))
                    price_bars_repo.store('sina', symbol, freq, incoming, provisional_day=provisional, end=today)
                except Exception as exc:
                    detail = str(exc)
                bars, source = read()
                if freq == '1d' and (not bars or bars[-1]['date'][:10] < expected or (end and len(bars) < need)):
                    start = (target-datetime.timedelta(days=need*2+30)).isoformat()
                    try:
                        incoming = fetch_tencent(symbol, start, stop)
                        price_bars_repo.store('tencent', symbol, freq, incoming, start=start, end=stop, provisional_day=provisional)
                    except Exception as exc:
                        detail = detail or str(exc)
                    bars, source = read()
                try:
                    kv_repo.set_('kline_fill', key, {'need': need, 'phase': provisional or 'closed', 'error': detail})
                except Exception:
                    pass  # The bars themselves were read back independently.
            if bars and bars[-1]['date'][:10] < expected:
                detail = detail or f'截至 {expected} 的交易日行情仍待补充'
            return self.parse_sina_klines(bars), 'sqlite:'+source, detail

    @staticmethod
    def _apply_quote_to_records(records: list[dict[str, Any]], quote: Any, today: str) -> bool:
        """把实时报价就地叠加到「当日」那根日K上(close/high/low/pct_chg)。

        仅当最后一根的日期 == today 才改写——盘前/隔日(当日bar尚未生成)时不伪造，
        避免凭空造出一根没有真实 OHLC 的当日K。返回是否发生了改写。
        """
        if not records or not isinstance(quote, dict):
            return False
        price = _safe_float(quote.get('price'))
        if price <= 0:  # 0 价 = 停牌/无效，按未取到处理
            return False
        last = records[-1]
        if str(last.get('date') or '') != str(today):
            return False
        last['close'] = price
        if _safe_float(last.get('high')) > 0:
            last['high'] = max(float(last['high']), price)
        if _safe_float(last.get('low')) > 0:
            last['low'] = min(float(last['low']), price)
        change_pct = quote.get('change_pct')
        if change_pct is not None:  # 报价的涨跌幅是对昨收，正是日K当日涨跌口径
            last['pct_chg'] = round(_safe_float(change_pct), 2)
        return True

    def _overlay_realtime(self, records: list[dict[str, Any]], code: str) -> Optional[dict[str, Any]]:
        """取实时报价并叠加到当日bar；返回报价块(供前端徽章)或 None。

        报价源缺失/异常/无效价时静默降级，绝不阻断K线本身。
        """
        provider = self._quote_provider
        if not provider or not records:
            return None
        try:
            cached = kv_repo.get('kline_quote', code)
            if not cached or time.time()-cached[1] >= self.SINA_CACHE_TTL:
                incoming = (provider([code]) or {}).get(code)
                if isinstance(incoming, dict) and _safe_float(incoming.get('price')) > 0:
                    kv_repo.set_('kline_quote', code, incoming)
                    cached = kv_repo.get('kline_quote', code)
            if not cached or time.time()-cached[1] >= self.SINA_CACHE_TTL:
                return None
            quote = cached[0]
        except Exception:
            return None
        if not isinstance(quote, dict):
            return None
        price = _safe_float(quote.get('price'))
        if price <= 0:
            return None
        today = self._clock().date().isoformat()
        quote_day = quote.get('quote_date')
        applied = (not quote_day or quote_day == today) and bool(records[-1].get('provisional')) and self._apply_quote_to_records(records, quote, today)
        return {
            'name': str(quote.get('name') or '').strip(),
            'price': price,
            'change_pct': round(_safe_float(quote.get('change_pct')), 2),
            'change_amount': _safe_float(quote.get('change_amount')),
            'updated_at': datetime.datetime.fromtimestamp(cached[1]).isoformat(timespec='seconds'),
            'as_of': quote_day,
            'applied': applied,
        }

    def _pattern_payload(self, records: list[dict[str, Any]], code: str, name: str = "") -> list[dict[str, Any]]:
        try:
            bars = bars_from_records(records)
            matches = detect_kline_patterns(bars, code=code, name=name)
        except Exception:  # noqa: BLE001
            return []
        return [{
            "pattern": match.get("pattern"),
            "name": match.get("name"),
            "date": match.get("trigger_date"),
            "anchor_date": match.get("anchor_date"),
            "mark_dates": match.get("mark_dates") or [],
            "strength": match.get("strength"),
            "tone": match.get("tone"),
            "rationale": match.get("rationale"),
            "days_ago": match.get("days_ago"),
            "direction": match.get("direction"),
        } for match in matches[:12]]

    def get_bars(
        self,
        stock_code: str,
        period: str = 'daily',
        limit: int = 120,
        *,
        use_cache: bool = True,
        cache_write: bool = True,
    ) -> list[dict[str, Any]]:
        """与图表共用落库行情，但不叠加盘中报价、不运行形态检测。"""
        codes = normalize_stock_codes([stock_code])
        if not codes or period not in self.FREQUENCIES:
            return []
        return self.history(codes[0], period, safe_int(limit, 120, minimum=1, maximum=1023) or 120)[0]

    def _range_history(self, code, period, start, end):
        freq = self.FREQUENCIES[period]
        symbol = self.sina_symbol_for_code(code)

        def local_rows(lower, upper):
            frame = ohlcv_repo.load_dataframe(code, freq, start=lower, end=upper)
            return self.frame_to_kline_records(frame, None, period, upper)

        if freq == '1d':
            rows, source, message = load_range(symbol, start, end, warmup=249, now=self._clock(), local_rows=local_rows)
            return self.parse_sina_klines(rows), source, message

        upper = min(end, last_closed_day(self._clock()).isoformat())
        options = [(price_bars_repo.read('sina', symbol, freq, start=start, end=upper), 'sqlite:sina'),
                   (local_rows(start, upper), 'sqlite:ohlcv')]
        rows, source = max(options, key=lambda item: len(item[0]))
        message = ('历史分钟线使用已入库数据。' if rows else '该区间没有已入库的分钟线，可切换到日线查询。')
        return self.parse_sina_klines(rows), source, message

    def get_payload(
        self,
        stock_code: str,
        period: str = 'daily',
        limit: int | str = 120,
        *, start: str | None = None, end: str | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        codes = normalize_stock_codes([stock_code])
        if not codes:
            return None, 'Invalid stock code'
        code = codes[0]
        full = str(limit) == 'all'
        normalized_limit = None if full else safe_int(limit, 120, minimum=1, maximum=self.SINA_WINDOW) or 120
        normalized_period = str(period or 'daily')
        if normalized_period not in self.FREQUENCIES:
            return None, 'Unsupported K-line period'
        try:
            if end:
                parse_day(end)
            if start:
                last = end or self._clock().date().isoformat()
                if parse_day(start) > parse_day(last):
                    return None, '开始日期不能晚于结束日期'
        except ValueError as exc:
            return None, str(exc)

        if start:
            records, source, detail = self._range_history(code, normalized_period, start, last)
        else:
            records, source, detail = self.history(code, normalized_period, normalized_limit, end=end)
        daily = self.FREQUENCIES[normalized_period] == '1d'
        if records:
            message = detail if start else (f'补数未完整，展示已入库的 {len(records)} 根，截至 {records[-1]["date"]}。' if detail or (normalized_limit and len(records)<normalized_limit) else None)
            quote_block = self._overlay_realtime(records, code) if daily and not start and not end and source == 'sqlite:sina' else None
            name = (quote_block or {}).get('name') or ''
            return {
                'code': code,
                'name': name,
                'period': normalized_period,
                'source': source,
                'available': True,
                'records': records,
                'panic': panic_from_bars(records, quote=quote_block, period=normalized_period),
                'patterns': self._pattern_payload(records, code, name) if daily else [],
                'operation': analyze_operations(records, period=normalized_period),
                'quote': quote_block,
                'quoted': bool(quote_block),
                'quote_updated_at': quote_block['updated_at'] if quote_block else None,
                'message': message,
            }, None

        return {
            'code': code,
            'name': '',
            'period': normalized_period,
            'source': 'sqlite',
            'available': False,
            'records': [],
            'panic': panic_indicator(),
            'message': detail if start else 'SQLite 尚无该区间的同频行情；本次未补到数据，稍后会重试。',
            'detail': detail,
        }, None
