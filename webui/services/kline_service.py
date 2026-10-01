"""Stock K-line data loading for WebUI routes."""

from __future__ import annotations

import datetime
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Optional

from analysis.limit_up_patterns import bars_from_records, detect_kline_patterns
from analysis.operation_models import analyze_operations
from webui.services.analysis_jobs import normalize_stock_codes, safe_int
from webui.services.http_client import request_json


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, '', '—', 'N/A'):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


class StockKlineService:
    # Sina日K短缓存：日K盘中只靠报价叠加更新当根bar，60s内重复请求(点开弹窗、
    # 30s盘中自刷、列表多处入口)复用同一份记录，避免每次都同步占住后端worker打Sina(超时8s)。
    SINA_CACHE_TTL = 60.0

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        # 实时报价源（如 WatchlistService.quotes）。日K是隔日/盘中按天一根的快照，
        # 叠加当日最新价后，盘中那根bar与「实时价」徽章可跟随最新成交价。
        self._quote_provider: Optional[Callable[[list], dict]] = None
        self._sina_cache: dict[tuple[str, str, int], tuple[float, list[dict[str, Any]]]] = {}
        self._sina_cache_lock = threading.Lock()

    def set_quote_provider(self, provider: Optional[Callable[[list], dict]]) -> None:
        """注入/更换实时报价源（如 WatchlistService.quotes）。与自选行情共用同一行情口径。"""
        self._quote_provider = provider

    @staticmethod
    def sina_symbol_for_code(stock_code: str) -> str:
        code = str(stock_code or '').strip().zfill(6)
        if code.startswith(('43', '83', '87', '92')):
            return f'bj{code}'
        if code.startswith(('6', '9')):
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
            '1h': '60',
            'daily': '240',
            '1d': '240',
            'day': '240',
        }
        return period_map.get(str(period or 'daily'), '240')

    def local_kline_candidates(self, stock_code: str, period: str) -> list[Path]:
        if not self.data_dir.exists():
            return []
        prefixes = []
        if str(period) in ('5m', '5'):
            prefixes.extend(['5m', '5min'])
        prefixes.extend(['1d', 'daily', 'day'])
        patterns = [f'{prefix}_{stock_code}.*' for prefix in prefixes]
        candidates = []
        for pattern in patterns:
            candidates.extend(self.data_dir.glob(pattern))
        return [path for path in candidates if path.suffix.lower() in ('.csv', '.feather')]

    @staticmethod
    def frame_to_kline_records(df: Any, limit: int) -> list[dict[str, Any]]:
        import pandas as pd

        if df is None or df.empty:
            return []

        work = df.copy()
        timestamp_col = next(
            (col for col in ['timestamps', 'timestamp', 'date', 'datetime', 'time'] if col in work.columns),
            None,
        )
        if timestamp_col:
            work[timestamp_col] = pd.to_datetime(work[timestamp_col], errors='coerce')
            work = work.dropna(subset=[timestamp_col])
            work = work.sort_values(timestamp_col)
        else:
            work['_timestamp'] = pd.RangeIndex(start=1, stop=len(work) + 1)
            timestamp_col = '_timestamp'

        for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
            if col in work.columns:
                work[col] = pd.to_numeric(work[col], errors='coerce')
        work = work.dropna(subset=[col for col in ['open', 'high', 'low', 'close'] if col in work.columns])

        records = []
        for _, row in work.tail(limit).iterrows():
            timestamp = row[timestamp_col]
            if isinstance(timestamp, pd.Timestamp):
                date_text = timestamp.strftime('%Y-%m-%d')
            else:
                date_text = str(timestamp)
            open_price = float(row['open'])
            close_price = float(row['close'])
            pct_chg = ((close_price - open_price) / open_price * 100) if open_price else 0.0
            records.append({
                'date': date_text,
                'open': open_price,
                'close': close_price,
                'high': float(row['high']),
                'low': float(row['low']),
                'volume': float(row['volume']) if 'volume' in row and pd.notna(row.get('volume')) else 0.0,
                'amount': float(row['amount']) if 'amount' in row and pd.notna(row.get('amount')) else 0.0,
                'pct_chg': round(pct_chg, 2),
            })
        return records

    def load_local_kline(self, stock_code: str, period: str, limit: int) -> tuple[list[dict[str, Any]], str | None]:
        import pandas as pd

        for path in self.local_kline_candidates(stock_code, period):
            try:
                if path.suffix.lower() == '.csv':
                    df = pd.read_csv(path)
                else:
                    df = pd.read_feather(path)
                records = self.frame_to_kline_records(df, limit)
                if records:
                    return records, path.name
            except Exception as exc:
                print(f"Failed to load local kline {path}: {exc}")
        return [], None

    @staticmethod
    def parse_sina_klines(klines: list[Any]) -> list[dict[str, Any]]:
        records = []
        prev_close = None
        for item in klines or []:
            if not isinstance(item, dict):
                continue
            open_price = _safe_float(item.get('open'))
            close_price = _safe_float(item.get('close'))
            if open_price <= 0 or close_price <= 0:
                continue
            high_price = _safe_float(item.get('high'), max(open_price, close_price))
            low_price = _safe_float(item.get('low'), min(open_price, close_price))
            pct_chg = 0.0
            if prev_close and prev_close > 0:
                pct_chg = (close_price / prev_close - 1) * 100
            records.append({
                'date': str(item.get('day') or item.get('date') or ''),
                'open': open_price,
                'close': close_price,
                'high': high_price,
                'low': low_price,
                'volume': _safe_float(item.get('volume')),
                'amount': _safe_float(item.get('amount')),
                'pct_chg': round(pct_chg, 2),
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
            'http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'
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
            timeout=8,
        )
        records = self.parse_sina_klines(payload if isinstance(payload, list) else [])
        return records, ''

    def _fetch_sina_kline_cached(self, stock_code: str, period: str, limit: int) -> list[dict[str, Any]]:
        """TTL内复用Sina结果；命中返回逐条浅拷贝(实时叠加会就地改写当日bar)。失败不缓存。"""
        key = (str(stock_code), self.period_to_sina_scale(period), int(limit))
        now = time.monotonic()
        with self._sina_cache_lock:
            cached = self._sina_cache.get(key)
            if cached and now - cached[0] < self.SINA_CACHE_TTL:
                return [dict(record) for record in cached[1]]
        records, _name = self.fetch_sina_kline(stock_code, period, limit)
        if records:
            with self._sina_cache_lock:
                self._sina_cache[key] = (now, [dict(record) for record in records])
        return records

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
        last['high'] = max(_safe_float(last.get('high'), price), price)
        low_val = _safe_float(last.get('low'), price)
        last['low'] = min(low_val if low_val > 0 else price, price)
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
            quote_map = provider([code]) or {}
        except Exception:  # noqa: BLE001 — 行情源不可用时降级，不应阻断K线
            return None
        quote = quote_map.get(str(code).strip()) if isinstance(quote_map, dict) else None
        if not isinstance(quote, dict):
            return None
        price = _safe_float(quote.get('price'))
        if price <= 0:
            return None
        today = datetime.date.today().strftime('%Y-%m-%d')
        applied = self._apply_quote_to_records(records, quote, today)
        return {
            'name': str(quote.get('name') or '').strip(),
            'price': price,
            'change_pct': round(_safe_float(quote.get('change_pct')), 2),
            'change_amount': _safe_float(quote.get('change_amount')),
            'updated_at': datetime.datetime.now().isoformat(timespec='seconds'),
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

    def get_payload(
        self,
        stock_code: str,
        period: str = 'daily',
        limit: int | str = 120,
    ) -> tuple[dict[str, Any] | None, str | None]:
        codes = normalize_stock_codes([stock_code])
        if not codes:
            return None, 'Invalid stock code'
        code = codes[0]
        normalized_limit = safe_int(limit, 120, minimum=30, maximum=500) or 120
        normalized_period = str(period or 'daily')

        sina_records: list[dict[str, Any]] = []
        sina_error: str | None = None
        try:
            sina_records = self._fetch_sina_kline_cached(code, normalized_period, normalized_limit)
        except Exception as exc:
            sina_error = str(exc)

        if sina_records:
            quote_block = self._overlay_realtime(sina_records, code)
            name = (quote_block or {}).get('name') or ''
            return {
                'code': code,
                'name': name,
                'period': normalized_period,
                'source': 'sina',
                'available': True,
                'records': sina_records,
                'patterns': self._pattern_payload(sina_records, code, name),
                'operation': analyze_operations(sina_records, period=normalized_period),
                'quote': quote_block,
                'quoted': bool(quote_block),
                'quote_updated_at': quote_block['updated_at'] if quote_block else None,
            }, None

        local_records, local_source = self.load_local_kline(code, normalized_period, normalized_limit)
        if local_records:
            quote_block = self._overlay_realtime(local_records, code)
            name = (quote_block or {}).get('name') or ''
            return {
                'code': code,
                'name': name,
                'period': normalized_period,
                'source': f'local:{local_source}',
                'available': True,
                'records': local_records,
                'patterns': self._pattern_payload(local_records, code, name),
                'operation': analyze_operations(local_records, period=normalized_period),
                'quote': quote_block,
                'quoted': bool(quote_block),
                'quote_updated_at': quote_block['updated_at'] if quote_block else None,
                'message': 'Sina 行情接口暂不可用，已使用本地缓存数据（可能不是最新）。' if sina_error else None,
            }, None

        if sina_error:
            return {
                'code': code,
                'name': '',
                'period': normalized_period,
                'source': 'sina',
                'available': False,
                'records': [],
                'message': '暂无K线数据：本地没有该股票数据，Sina 行情接口暂不可用。',
                'detail': sina_error,
            }, None

        return {
            'code': code,
            'name': '',
            'period': normalized_period,
            'source': 'sina',
            'available': False,
            'records': [],
            'message': '暂无K线数据：本地没有该股票数据，Sina 行情接口未返回记录。',
        }, None
