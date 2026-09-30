# -*- coding: utf-8 -*-
"""总览页「指数风向 + 板块机会与拐点」编排层。

纯编排:每个数据源都是注入的 callable,服务本身零 I/O、可离线测试。
每源独立降级(见 ``_safe``),单源失败只置对应 ``degraded`` 标志,其余面板照常。

⚠️ 未经历史校验的判据不得上线(spec §8):``rule_stats`` 缺失或没有 ``enabled``
时,``enabled`` 传空列表,「已触发 / 临界观察」两列都为空,而不是全量放行。

设计 spec: docs/superpowers/specs/2026-08-21-market-pulse-index-sector-turning-design.md
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

from analysis import index_pulse as ip
from analysis.market_regime import position_advice

DISCLAIMER = ("指数状态与板块信号基于公开数据的统计描述,不构成投资建议;"
              "盘中信号随当日数据变化,收盘后方为定稿。")

# 指数状态 → market_regime 仓位建议的分级映射
_PULSE_TO_LEVEL = {
    "strong": "risk_on",
    "mild_up": "neutral",
    "range": "neutral",
    "pullback": "caution",
    "risk": "risk_off",
    "unknown": "unknown",
}
_LEVEL_SEVERITY = ("risk_off", "caution", "neutral", "risk_on")


class MarketPulseService:
    def __init__(self, *, index_bars: Callable, index_quotes: Callable,
                 sector_series: Callable, turning_rules: Callable,
                 rule_stats: Optional[Callable] = None, ttl: int = 30,
                 datalen: int = 160, series_limit: int = 60):
        self._index_bars = index_bars
        self._index_quotes = index_quotes
        self._sector_series = sector_series
        self._turning_rules = turning_rules
        self._rule_stats = rule_stats or (lambda: {})
        self._ttl = int(ttl)
        self._datalen = int(datalen)
        self._series_limit = int(series_limit)
        self._lock = threading.Lock()
        self._cache: Dict[str, Any] = {"ts": 0.0, "key": None, "payload": None}

    @staticmethod
    def _safe(fn, default):
        try:
            return fn(), False
        except Exception:  # noqa: BLE001 — 逐源降级是本服务的核心契约
            return default, True

    # ------------------------------------------------------------ 指数
    def _build_indices(self) -> Dict[str, Any]:
        symbols = [s for s, _, _ in ip.DISPLAY_INDEXES + ip.STYLE_INDEXES]
        bars_by_symbol, deg_index = self._safe(
            lambda: self._index_bars(symbols, datalen=self._datalen) or {}, {})
        quotes, deg_quote = self._safe(
            lambda: self._index_quotes(symbols) or {}, {})

        metrics_by_symbol: Dict[str, Dict[str, Any]] = {}
        realtime_by_symbol: Dict[str, bool] = {}
        dates_by_symbol: Dict[str, str] = {}
        for symbol in symbols:
            bars = bars_by_symbol.get(symbol) or []
            quote = quotes.get(symbol)
            if quote and not quote.get("date"):
                quote = None
            merged, live = ip.overlay_realtime(bars, quote) if bars else (bars, False)
            metrics_by_symbol[symbol] = ip.index_metrics(merged)
            realtime_by_symbol[symbol] = bool(live)
            dates_by_symbol[symbol] = str((merged[-1] if merged else {}).get("day") or "")[:10]

        cards: List[Dict[str, Any]] = []
        for symbol, ts_code, name in ip.DISPLAY_INDEXES:
            if not bars_by_symbol.get(symbol):
                continue
            metrics = metrics_by_symbol[symbol]
            pulse = ip.classify_pulse(metrics)
            cards.append({
                "symbol": symbol, "ts_code": ts_code, "name": name,
                "close": metrics["close"],
                "chg_5d": metrics["chg_5d"], "chg_20d": metrics["chg_20d"],
                "vol_ratio": metrics["vol_ratio"],
                "percentile_60d": metrics["percentile_60d"],
                "above_ma20": metrics["above_ma20"],
                "ma20_slope": metrics["ma20_slope"],
                "pulse": pulse, "pulse_label": ip.PULSE_LABELS[pulse],
                "realtime": realtime_by_symbol[symbol],
                "as_of": dates_by_symbol[symbol],
            })

        style = ip.style_axis(metrics_by_symbol.get("sh000300"),
                              metrics_by_symbol.get("sh000852"))
        levels = [_PULSE_TO_LEVEL.get(c["pulse"], "unknown") for c in cards]
        known = [lv for lv in levels if lv in _LEVEL_SEVERITY]
        level = (_LEVEL_SEVERITY[min(_LEVEL_SEVERITY.index(lv) for lv in known)]
                 if known else "unknown")
        as_of = None
        for symbol, _, _ in ip.DISPLAY_INDEXES:
            bars = bars_by_symbol.get(symbol) or []
            if bars:
                as_of = dates_by_symbol[symbol]
                break
        return {"indices": cards, "style": style, "level": level,
                "position_advice": position_advice(level), "as_of": as_of,
                "benchmark": metrics_by_symbol.get("sh000300") or {},
                "degraded_index": deg_index, "degraded_quote": deg_quote or not quotes}

    # ------------------------------------------------------------ 板块
    def _build_sectors(self, as_of: Optional[str]) -> Dict[str, Any]:
        stats, _ = self._safe(lambda: self._rule_stats() or {}, {})
        enabled = list(stats.get("enabled") or [])
        weights = stats.get("weights") or None

        series_by_sector, degraded = self._safe(
            lambda: self._sector_series(end_date=as_of, limit=self._series_limit) or {}, {})
        if not enabled or not series_by_sector:
            evaluated = {"fired": [], "watch": []}
            deg_rules = degraded
        else:
            evaluated, deg_rules = self._safe(
                lambda: self._turning_rules(series_by_sector, weights=weights,
                                            enabled=enabled),
                {"fired": [], "watch": []})
            deg_rules = deg_rules or degraded

        latest_date = ""
        provisional = False
        for series in series_by_sector.values():
            if series:
                last = series[-1]
                latest_date = max(latest_date, str(last.get("trade_date") or ""))
                provisional = provisional or bool(last.get("provisional"))
        return {
            "fired": evaluated.get("fired", []),
            "watch": evaluated.get("watch", []),
            "enabled": enabled,
            "rule_stats": stats.get("rules") or {},
            "as_of": latest_date or None,
            "provisional": provisional,
            "degraded": bool(deg_rules),
        }

    # ------------------------------------------------------------ 对外
    def payload(self, *, as_of: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
        key = as_of or ""
        now = time.time()
        with self._lock:
            cached = self._cache
            if (not force and cached["payload"] is not None and cached["key"] == key
                    and now - cached["ts"] < self._ttl):
                return cached["payload"]

        index_part = self._build_indices()
        sector_part = self._build_sectors(as_of)
        result = {
            "as_of": {"index": index_part["as_of"], "sector": sector_part["as_of"],
                      "provisional": sector_part["provisional"]},
            "indices": index_part["indices"],
            "style": index_part["style"],
            "level": index_part["level"],
            "position_advice": index_part["position_advice"],
            "benchmark": index_part["benchmark"],
            "sectors": {
                "fired": sector_part["fired"],
                "watch": sector_part["watch"],
                "enabled": sector_part["enabled"],
                "rule_stats": sector_part["rule_stats"],
            },
            "degraded": {
                "index": index_part["degraded_index"],
                "index_quote": index_part["degraded_quote"],
                "sector": sector_part["degraded"],
            },
            "disclaimer": DISCLAIMER,
        }
        with self._lock:
            self._cache = {"ts": now, "key": key, "payload": result}
        return result
