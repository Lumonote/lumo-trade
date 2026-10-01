"""Framework-agnostic WebUI core layer.

Holds helpers, service singletons, and module-level constants that
were originally defined in webui/app.py. Both webui/app.py (Flask
fallback during migration) and webui/robyn_app.py (Robyn runtime)
import from here.
"""

from __future__ import annotations

import os
import sys
import json
import math
import random
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import warnings
import webbrowser
import datetime
import logging
from collections import defaultdict
from html import unescape
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.utils

warnings.filterwarnings("ignore")

PROJECT_ROOT_PATH = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT_PATH) not in sys.path:
    sys.path.append(str(PROJECT_ROOT_PATH))

if os.environ.get("KRONOS_DISABLE_TORCH", "0").lower() in {"1", "true", "yes"}:
    MODEL_AVAILABLE = False
    Kronos = KronosTokenizer = KronosPredictor = None
    print("Kronos model import disabled by KRONOS_DISABLE_TORCH")
else:
    try:
        from model import Kronos, KronosTokenizer, KronosPredictor

        MODEL_AVAILABLE = True
    except ImportError:
        MODEL_AVAILABLE = False
        Kronos = KronosTokenizer = KronosPredictor = None
        print("Warning: Kronos model cannot be imported, will use simulated data for demonstration")
        print(f"Model import error: {sys.exc_info()[1]}")

from webui.services.background_jobs import BackgroundJobService
from webui.services.analysis_jobs import AnalysisJobRequestParser, normalize_stock_codes, safe_int
from webui.services.configuration_service import RuntimeConfigurationService
from webui.services.http_client import request_text
from webui.services.job_store import JobStore
from webui.services.kline_service import StockKlineService
from webui.services.market_intelligence import MarketIntelligenceService
from webui.services.model_runtime import (
    ModelRuntimeContext,
    load_model_payload,
    loaded_model_info,
    run_prediction_payload,
)
from webui.services.paths import ensure_user_subdirs, project_root, results_dir, user_root
from webui.services.pattern_search_service import PatternSearchService
from webui.services.stock_suite_service import STOCK_SUITE_SERVICE
from webui.services.trading_client_service import TradingClientService
from webui.services.watchlist_service import WatchlistService
from webui.services.capital_rankings_service import CapitalRankingsService
from webui.services.paper_trading_service import PaperTradingService
from webui.services.paper_auto_follow_service import PaperAutoFollowService
from webui.services.notification_events import NotificationEventService
from webui.services.scoring_health_service import ScoringHealthService
from webui.services.db_backup_service import DbBackupService
from webui.services.command_center_service import CommandCenterService
from webui.services.market_pulse_service import MarketPulseService
from webui.services.trading_plan_service import TradingPlanService
from data_store import opportunity_repo

logger = logging.getLogger(__name__)

PROJECT_ROOT = project_root()
USER_ROOT = user_root()
ensure_user_subdirs(USER_ROOT)
# WebUI与桌面使用相同持久数据目录；显式数据库/数据目录设置仍优先。
os.environ.setdefault("KRONOS_DATA_DIR", str(USER_ROOT / "data"))
RESULTS_DIR = results_dir()
REPORT_DIRS = {
    "results": RESULTS_DIR,
    "reports": USER_ROOT / "reports",
    "integrated_results": USER_ROOT / "integrated_results",
}
PRIMARY_OPPORTUNITY_REPORT_RE = re.compile(r"^opportunity_top10_\d{8}_\d{6}\.md$")

CONFIGURATION_SERVICE = RuntimeConfigurationService(PROJECT_ROOT, USER_ROOT)
JOB_STORE = JobStore(USER_ROOT / "data" / "webui_jobs.sqlite")
ANALYSIS_JOB_PARSER = AnalysisJobRequestParser()
STOCK_KLINE_SERVICE = StockKlineService(USER_ROOT / "data")
MARKET_INTELLIGENCE_SERVICE = MarketIntelligenceService()
PATTERN_SEARCH_SERVICE = PatternSearchService(USER_ROOT / "data" / "pattern_fingerprints.db")
TRADING_CLIENT_SERVICE = TradingClientService(PROJECT_ROOT / "config" / "trading_client_adapters.json")
WATCHLIST_SERVICE = WatchlistService(USER_ROOT / "config" / "watchlist.json")
# 资金榜单(主力净流入榜+龙虎榜):注入自选服务的实时报价以叠加最新价/涨跌幅
CAPITAL_RANKINGS_SERVICE = CapitalRankingsService(quote_provider=WATCHLIST_SERVICE.quotes)
# 模拟盘台账(起始100W,含简化费用):盯市价复用自选实时报价
PAPER_TRADING_SERVICE = PaperTradingService(quote_provider=WATCHLIST_SERVICE.quotes)
# 整库备份(导出一致性快照 / 导入校验→自动备份→灌库→migrate)
DB_BACKUP_SERVICE = DbBackupService()
# 应用内通知事件(EOD 复盘 / 机会挖掘 / 自动跟单完成时入队,前端通知条「系统」分类消费)
NOTIFICATION_EVENTS = NotificationEventService()
# 评分算法健康度:优先读 results_dir()(桌面/CLI 统一目录),回退仓内 results/(历史回测CSV所在)
SCORING_HEALTH_SERVICE = ScoringHealthService([RESULTS_DIR, PROJECT_ROOT / "results"])
# 模拟盘自动跟单(机会报告达档自动建仓,EOD 持有到期平仓;默认关闭,配置见后台设置页)
AUTO_FOLLOW_SERVICE = PaperAutoFollowService(
    PAPER_TRADING_SERVICE,
    CONFIGURATION_SERVICE.load_auto_follow_config,
    USER_ROOT / "data" / "paper_auto_follow.json",
    events=NOTIFICATION_EVENTS,
)


# ----------------------------- 风险·机遇 作战大屏 -----------------------------
def _cc_market_env():
    """复用风向服务中真实沪深300收益，缺失字段保持未知。"""
    benchmark = MARKET_PULSE_SERVICE.payload().get("benchmark") or {}
    return {key: value for key, value in {
        "hs300_ret_5d": benchmark.get("chg_5d"),
        "hs300_ret_20d": benchmark.get("chg_20d"),
    }.items() if value is not None}


def _cc_holdings():
    """Paper-trading account + positions + max drawdown,供组合风险层。"""
    account = PAPER_TRADING_SERVICE.account_summary()
    positions = PAPER_TRADING_SERVICE.positions()
    # stats().max_drawdown 为负值(峰值回撤比例);引擎期望正的回撤幅度
    try:
        mdd = abs(float(PAPER_TRADING_SERVICE.stats().get("max_drawdown") or 0.0))
    except Exception:
        mdd = 0.0
    return {"account": account, "positions": positions, "max_drawdown": mdd}


def _command_center_available_dates(limit=60):
    """有机会挖掘 run 的日期(新→旧),用于大屏选日。数据源:SQLite ``opportunity_run``。"""
    try:
        from data_store import opportunity_repo
        rows = opportunity_repo.runs_by_day(limit=limit)
    except Exception as exc:
        logger.debug(f"读取机会挖掘按天列表失败: {exc}")
        return []
    dates = []
    for row in rows or []:
        d = str((row or {}).get('run_date') or '')[:10]
        if d and d not in dates:
            dates.append(d)
    return dates[:limit]


# 大屏撮合矩阵取当日 run 的前 N 名(与报告「综合排名 TOP20」同口径)。
# 一次 run 入库的是全量候选(数百只),全铺进矩阵会被低分噪声淹没。
COMMAND_CENTER_TOP_N = 20


def _command_center_item_from_row(row):
    """``opportunity_item`` 行 → 大屏矩阵 item(自带风险信号,供撮合直接消费)。"""
    row = dict(row or {})
    scores = _json_obj(row.get('scores_json'), {})
    signals = _json_obj(row.get('signals_json'), {})
    sig = dict(signals) if isinstance(signals, dict) else {}
    # 板块拥挤度用七维里的板块分(sidecar 时代由 sector_score 字段承载)
    sector_score = _safe_float(scores.get('sector'), None) if isinstance(scores, dict) else None
    if sector_score is not None and 'sector_score' not in sig:
        sig['sector_score'] = sector_score
    code = _stock_code_key(row.get('code'))
    return {
        'code': code,
        'stock_code': code,
        'name': row.get('name'),
        'stock_name': row.get('name'),
        'score': _safe_float(row.get('total_score'), 0.0) or 0.0,
        'rating': row.get('rating'),
        'sector': row.get('sector'),
        'sector_code': row.get('sector_code'),
        'change_pct': _safe_float(row.get('change_pct'), None),
        'degraded': bool(row.get('degraded')),
        'signals': sig,
        'scores': scores if isinstance(scores, dict) else {},
    }


def _command_center_report(date=None):
    """当日机会挖掘结果(数据源:**SQLite** ``opportunity_run``/``opportunity_item``)。

    同日多次 run 按 code 去重保留最高综合分,按分降序取前 ``COMMAND_CENTER_TOP_N`` 名。
    ``date`` 为空 → 最近一个有 run 的日子;指定日无 run → 回退最近一天。
    每条 item 自带 ``signals``(风险信号 + 板块分),大屏不再依赖 markdown 报告与
    ``.signals.json`` 旁挂文件。
    """
    empty = {"items": [], "file": None, "date": None, "report_count": 0, "run_ids": []}
    try:
        from data_store import opportunity_repo
    except Exception as exc:
        logger.debug(f"opportunity_repo 不可用: {exc}")
        return empty

    try:
        target = str(date or "").strip()[:10]
        runs = opportunity_repo.list_runs(run_date=target, limit=50) if target else []
        if not runs:  # 未指定日 / 该日无 run → 回退最近一次 run 所在的那天
            latest = opportunity_repo.latest_run()
            if not latest:
                return empty
            target = str(latest.get('run_date') or latest.get('run_at') or '')[:10]
            runs = opportunity_repo.list_runs(run_date=target, limit=50) or [latest]
    except Exception as exc:
        logger.debug(f"读取当日机会挖掘 run 失败: {exc}")
        return empty

    # list_runs 已按时间倒序:最新一次 run 的报告名用于 as_of 展示
    newest_file = None
    merged = {}
    run_ids = []
    for run in runs:
        rid = run.get('id')
        if rid is None:
            continue
        run_ids.append(int(rid))
        if newest_file is None:
            newest_file = Path(str(run.get('report_file') or '')).name or None
        try:
            rows = opportunity_repo.items_for_run(int(rid))
        except Exception as exc:
            logger.debug(f"读取 run {rid} 明细失败: {exc}")
            continue
        for row in rows:
            item = _command_center_item_from_row(row)
            code = item['code']
            if not code:
                continue
            prev = merged.get(code)
            if prev is None or item['score'] > prev['score']:
                merged[code] = item

    items = sorted(merged.values(), key=lambda i: i['score'], reverse=True)
    return {
        "items": items[:COMMAND_CENTER_TOP_N],
        "file": newest_file,
        "date": target or None,
        "report_count": len(run_ids),
        "run_ids": run_ids,
    }


def _market_pulse_index_bars(symbols, datalen=160):
    from analysis import market_regime

    return market_regime.get_index_bars(symbols, datalen=datalen)


def _market_pulse_index_quotes(symbols):
    from data_store.index_quote_fetch import fetch_index_quotes

    quotes = fetch_index_quotes(symbols)
    # ⚠️ core.py 用的是 `import datetime`(模块),不是 `from datetime import datetime`,
    # 因此这里必须写 datetime.datetime.now(),与本文件其余调用一致。
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    return {sym: {**q, "date": today} for sym, q in quotes.items()}


def _market_pulse_series(end_date=None, limit=60):
    from analysis import sector_series

    return sector_series.load_all_series(end_date=end_date, limit=limit, include_stale=True)


def _market_pulse_rules(series_by_sector, weights=None, enabled=None):
    from analysis import sector_turning

    return sector_turning.evaluate_universe(series_by_sector, weights=weights,
                                            enabled=enabled)


def _market_pulse_rule_stats():
    """判据校验结果(scripts/validate_turning_rules.py 写入)。缺失 → 空,不放行任何判据。"""
    from data_store import kv_repo

    hit = kv_repo.get("market_pulse", "rule_stats")
    return (hit[0] or {}) if hit else {}


MARKET_PULSE_SERVICE = MarketPulseService(
    index_bars=_market_pulse_index_bars,
    index_quotes=_market_pulse_index_quotes,
    sector_series=_market_pulse_series,
    turning_rules=_market_pulse_rules,
    rule_stats=_market_pulse_rule_stats,
)


def market_pulse_payload(as_of=None):
    """总览页「指数风向 + 板块机会与拐点」payload(见 MarketPulseService.payload)。"""
    return MARKET_PULSE_SERVICE.payload(as_of=as_of)


def _trading_plan_kline(code):
    payload, error = STOCK_KLINE_SERVICE.get_payload(code, period="daily", limit=240)
    if error:
        raise ValueError(error)
    return payload or {}


TRADING_PLAN_SERVICE = TradingPlanService(
    store_path=USER_ROOT / "config" / "trading_plans.json",
    watchlist=WATCHLIST_SERVICE.list_items,
    opportunities=lambda: _command_center_report(),
    market_pulse=lambda: MARKET_PULSE_SERVICE.payload(),
    holdings=_cc_holdings,
    quotes=WATCHLIST_SERVICE.quotes,
    kline=_trading_plan_kline,
    metadata=WATCHLIST_SERVICE._sector_info,
)


def start_sector_refresh():
    """板块日序列盘中重算 / 收盘定稿守护线程(见 sector_series.start_finalize_daemon)。"""
    try:
        from analysis import sector_series

        return sector_series.start_finalize_daemon()
    except Exception as exc:  # noqa: BLE001 — 守护线程启动失败不阻塞服务
        logger.debug(f"启动板块序列守护线程失败: {exc}")
        return None


COMMAND_CENTER_SERVICE = CommandCenterService(
    load_report=lambda: _command_center_report(),
    capital_rankings=lambda: CAPITAL_RANKINGS_SERVICE.moneyflow_ranking(top_n=20),
    market_env=_cc_market_env,
    holdings=_cc_holdings,
    quotes=WATCHLIST_SERVICE.quotes,
    hot_membership=lambda date=None: _hot_sector_stock_membership_index(
        _hot_sector_snapshot_for_date(date)),
    news_index=lambda positions: _holdings_news_index(positions, _load_market_intelligence()),
    hot_news=lambda date=None, report_file=None: opportunity_repo.latest_hot_news(
        date, limit=10, report_file=report_file, latest_run_only=True),
    market_pulse=lambda: MARKET_PULSE_SERVICE.payload(),
)


def command_center_overview(date=None, quotes_only=False):
    """Whole-screen payload for the 风险·机遇 大屏(见 CommandCenterService.overview)。

    ``date`` 指定历史日(YYYY-MM-DD),聚合该日全部报告;为空取最近一天。
    """
    report = _command_center_report(date)
    available = _command_center_available_dates()
    return COMMAND_CENTER_SERVICE.overview(
        quotes_only=bool(quotes_only),
        report=report,
        available_dates=available,
    )


def start_command_center_recompute():
    """复用机会挖掘后台 job(全市场重扫)刷新大屏数据,返回 job 快照。"""
    params = {"source": "multi", "limit": 100, "workers": 10, "stock_codes": []}
    job = JOB_SERVICE.start("opportunity_discovery", params, _run_opportunity_job)
    return {"success": True, "job_id": job["id"], "job": _get_job_snapshot(job["id"])}


# 形态指纹是隔日快照；给命中结果叠加自选同款实时报价（东财 ulist.np→腾讯回退），形态页可见当日最新价。
PATTERN_SEARCH_SERVICE.set_quote_provider(WATCHLIST_SERVICE.quotes)
# K线默认是新浪日K（隔日/盘中按天一根）；叠加自选同款实时报价，让当日那根bar与「实时价」徽章跟随盘中最新价。
STOCK_KLINE_SERVICE.set_quote_provider(WATCHLIST_SERVICE.quotes)
# 量化雷达活跃榜的行业列复用 market_intelligence 的新浪行业分类映射(按天后台构建),
# 避免 clist 被掐走本地兜底时行业列全空;服务保持不 import core。
try:
    from webui.services import quant_radar_service as _quant_radar_service

    _quant_radar_service.set_industry_provider(MARKET_INTELLIGENCE_SERVICE.industry_map)
except Exception:  # noqa: BLE001 — 行业列是装饰,注入失败不影响主流程
    pass

tokenizer = None
model = None
predictor = None

AVAILABLE_MODELS = {
    "kronos-mini": {
        "name": "Kronos-mini",
        "model_id": "northwind9898/Kronos-mini",
        "tokenizer_id": "northwind9898/Kronos-Tokenizer-2k",
        "context_length": 2048,
        "params": "4.1M",
        "description": "Lightweight model, suitable for fast prediction",
    },
    "kronos-small": {
        "name": "Kronos-small",
        "model_id": "northwind9898/Kronos-small",
        "tokenizer_id": "northwind9898/Kronos-Tokenizer-base",
        "context_length": 512,
        "params": "24.7M",
        "description": "Small model, balanced performance and speed",
    },
    "kronos-base": {
        "name": "Kronos-base",
        "model_id": "northwind9898/Kronos-base",
        "tokenizer_id": "northwind9898/Kronos-Tokenizer-base",
        "context_length": 512,
        "params": "102.3M",
        "description": "Base model, provides better prediction quality",
    },
}


# --- Market Data Configuration & Logic ---

# Real Stock Mapping (A-Share Codes)
REAL_CODES_MAP = {
    'ai': ['sz002230', 'sh688256', 'sh601360', 'sz000977', 'sh603019', 'sh601138', 'sh688111', 'sz300418'],
    'robot': ['sz300124', 'sz002747', 'sh603728', 'sh688017', 'sz002050', 'sh601689'],
    'quantum': ['sh688027', 'sz000555', 'sz300520', 'sh600120'],
    'fusion': ['sz000969', 'sh600105', 'sh688122', 'sh600363'],
    'space': ['sh600118', 'sh600879', 'sh601698', 'sh688568']
}

SECTORS = [
    { "id": 0, "key": 'ai', "name": '人工智能', "color": '#ff0055', "angle": 0, "count": 800, "hot": True },
    { "id": 1, "key": 'robot', "name": '人形机器人', "color": '#00ff88', "angle": 1.2, "count": 600, "hot": True },
    { "id": 2, "key": 'quantum', "name": '量子计算', "color": '#00ccff', "angle": 2.4, "count": 500, "hot": False },
    { "id": 3, "key": 'fusion', "name": '可控核聚变', "color": '#ffff00', "angle": 3.6, "count": 400, "hot": True },
    { "id": 4, "key": 'space', "name": '深空探测', "color": '#ff8800', "angle": 4.8, "count": 450, "hot": False },
]

# Flatten codes for API fetching
ALL_REAL_CODES = []
for k, v in REAL_CODES_MAP.items():
    ALL_REAL_CODES.extend(v)

REAL_INDEX_CODES = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000985": "中证全指",
    "sh000016": "上证50",
    "sh000300": "沪深300",
    "sh000905": "中证500",
}

# Market Global State
market_state = {
    "stocks": [],
    "indices": {},
    "trades": [],
    "news": [],
    "sectors": SECTORS,
    "last_update": time.time(),
    "index_last_update": None,
    "real_data_cache": {} # code -> {price, change, name}
}
market_monitor_thread = None
market_monitor_lock = threading.Lock()

# Initial News Data
INITIAL_NEWS = [
    { "tag": "Breaking", "title": "Dec 7, 2025: A-Share stands at 3800, AI sector explodes" },
    { "tag": "Policy", "title": "AGI Development Plan (2025-2030) released, trillion-dollar market opens" },
    { "tag": "Flash", "title": "First consumer humanoid robot 'Optimus-C' sold out in seconds" },
    { "tag": "Tech", "title": "CAS announces major breakthrough in quantum computer 'Jiuzhang 4'" },
    { "tag": "Market", "title": "Northbound capital net inflow exceeds 15B, focusing on hard tech" },
    { "tag": "Company", "title": "Huawei releases 6G prototype, communication industry sees new revolution" },
    { "tag": "Warning", "title": "Abnormal capital inflow detected in 'Nuclear Fusion' sector, beware of chasing highs" }
]

def generate_market_news():
    """Generate dynamic market news based on current market state"""
    try:
        # Find top performing sector
        top_sector = None
        max_change = -100

        for stock in market_state["stocks"]:
            if stock.get("real_api_code") and stock["change"] > max_change:
                max_change = stock["change"]
                # Find sector name
                for s in SECTORS:
                    if s["id"] == stock["sectorId"]:
                        top_sector = s["name"]
                        break

        if top_sector and max_change > 3.0:
            return {
                "tag": "Market",
                "title": f"Sector Alert: {top_sector} leads the rally with top gainers up {max_change:.1f}%"
            }

        # Random generic news
        templates = [
            ("Tech", "Global AI computing power demand surges, semiconductor sector benefits"),
            ("Policy", "Central Bank: Maintain reasonable and sufficient liquidity"),
            ("Market", "Main board turnover exceeds 1 trillion in morning session"),
            ("Flash", "New battery technology achieves energy density breakthrough")
        ]
        t = random.choice(templates)
        return { "tag": t[0], "title": t[1] }

    except Exception:
        return None

def init_market():
    global_index = 0
    # Clear existing stocks to prevent duplication on re-init
    market_state["stocks"] = []
    market_state["news"] = list(INITIAL_NEWS)
    market_state["sectors"] = SECTORS
    market_state["indices"] = {}
    market_state["index_last_update"] = None

    for sector in SECTORS:
        real_codes = REAL_CODES_MAP.get(sector["key"], [])

        for i in range(sector["count"]):
            # Position (Spiral Galaxy Logic)
            arm_offset = sector["angle"]
            distance = random.random() * 40 + 10
            angle = distance * 0.1 + arm_offset
            x = math.cos(angle) * distance + (random.random() - 0.5) * 5
            y = (random.random() - 0.5) * (distance * 0.2)
            z = math.sin(angle) * distance + (random.random() - 0.5) * 5

            # Determine if this is a "Real" monitored stock or a "Background" particle
            is_real_monitored = i < len(real_codes)

            stock_code = "UNKNOWN"
            stock_name = "Unknown"

            if is_real_monitored:
                # Map to a real stock code
                full_code = real_codes[i] # e.g. sz002230
                stock_code = full_code[2:] # 002230
                stock_name = "Loading..."
            else:
                # Background particle
                stock_code = ('60' if random.random() > 0.5 else '00') + str(random.randint(0, 9999)).zfill(4)
                stock_name = sector["name"] + " Stock"

            stock = {
                "index": global_index,
                "id": f"{sector['key']}-{i}",
                "sectorId": sector["id"],
                "real_api_code": real_codes[i] if is_real_monitored else None,
                "name": stock_name,
                "code": stock_code,
                "price": 0.0,
                "change": 0.0,
                "volume": 0,
                "isLimitUp": False,
                "position": [x, y, z],
                "tags": [sector["name"]]
            }
            market_state["stocks"].append(stock)
            global_index += 1

    print(f"Market initialized with {len(market_state['stocks'])} stocks.")


def _parse_tencent_quote_line(line):
    if '="' not in line:
        return None, None
    parts = line.split('="', 1)
    api_code = parts[0].strip().replace('v_', '')
    fields = parts[1].strip().strip('"').split('~')
    if len(fields) <= 4:
        return api_code, None

    name = fields[1] or api_code
    price = _safe_float(fields[3], None)
    prev_close = _safe_float(fields[4], None)
    change = _safe_float(fields[31], None) if len(fields) > 31 else None
    change_pct = _safe_float(fields[32], None) if len(fields) > 32 else None

    if change_pct is None and price is not None and prev_close:
        change_pct = (price - prev_close) / prev_close * 100
    if change is None and price is not None and prev_close is not None:
        change = price - prev_close

    return api_code, {
        "code": api_code,
        "name": name,
        "price": price,
        "change": change,
        "change_pct": change_pct,
        "available": price is not None,
        "source": "tencent",
        "updated_at": _format_datetime(),
    }


def fetch_real_index_data():
    """Fetch real index quotes. Never derive index values from monitored stocks."""
    try:
        url = f"http://qt.gtimg.cn/q={','.join(REAL_INDEX_CODES.keys())}"
        content = request_text(
            url,
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=5,
            encoding='gbk',
            errors='ignore',
        )

        indices = {}
        for line in content.strip().split(';'):
            api_code, quote = _parse_tencent_quote_line(line)
            if api_code and quote and quote.get("available"):
                quote["display_name"] = REAL_INDEX_CODES.get(api_code, quote["name"])
                indices[api_code] = quote

        if indices:
            market_state["indices"] = indices
            market_state["index_last_update"] = time.time()
            return True
    except Exception as e:
        print(f"Error fetching real index data: {e}")
    return False


def fetch_real_market_data():
    try:
        url = f"http://qt.gtimg.cn/q={','.join(ALL_REAL_CODES)}"
        content = request_text(
            url,
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=5,
            encoding='gbk',
        )

        lines = content.strip().split(';')
        for line in lines:
            if '="' in line:
                parts = line.split('="')
                if len(parts) < 2: continue

                api_code = parts[0].strip().replace('v_', '')
                data_str = parts[1].strip('"')
                fields = data_str.split('~')

                if len(fields) > 30:
                    name = fields[1]
                    current_price = float(fields[3])
                    prev_close = float(fields[4])

                    change_pct = 0.0
                    if prev_close > 0:
                        change_pct = (current_price - prev_close) / prev_close * 100

                    volume = float(fields[6]) # Hand/Lot

                    market_state["real_data_cache"][api_code] = {
                        "name": name,
                        "price": current_price,
                        "change": change_pct,
                        "volume": volume
                    }
                    market_state["last_update"] = time.time()

    except Exception as e:
        print(f"Error fetching real data: {e}")

def monitor_loop():
    print("Market data monitor started...")
    while True:
        try:
            # 1. Fetch Real Data
            fetch_real_index_data()
            fetch_real_market_data()

            # 2. Update Stocks
            for stock in market_state["stocks"]:
                if stock["real_api_code"]:
                    # Update Real Stocks
                    data = market_state["real_data_cache"].get(stock["real_api_code"])
                    if data:
                        stock["name"] = data["name"]
                        stock["price"] = data["price"]
                        stock["change"] = data["change"]
                        stock["volume"] = data["volume"]
                        stock["isLimitUp"] = data["change"] > 9.5

                        # Update tags
                        stock["tags"] = [SECTORS[stock["sectorId"]]["name"]]
                        if data["change"] > 5: stock["tags"].append("Inflow")
                else:
                    # Update Background Particles
                    sector_key = SECTORS[stock["sectorId"]]["key"]
                    leaders = REAL_CODES_MAP.get(sector_key, [])

                    base_change = 0
                    if leaders:
                        leader_code = leaders[0]
                        leader_data = market_state["real_data_cache"].get(leader_code)
                        if leader_data:
                            base_change = leader_data["change"]

                    noise = (random.random() - 0.5) * 2
                    stock["change"] = base_change + noise
                    stock["price"] = max(2.0, stock["price"] * (1 + stock["change"]/1000))

            # 3. Generate "Real" Trades
            current_time = time.time()
            if len(market_state["trades"]) < 15:
                for stock in market_state["stocks"]:
                    if stock["real_api_code"] and abs(stock["change"]) > 2.0:
                        if random.random() < 0.1:
                            amount = round(random.random() * 10 + 1, 1)
                            trade = {
                                "targetId": stock["id"],
                                "targetName": stock["name"],
                                "targetPos": stock["position"],
                                "amount": amount,
                                "timestamp": current_time
                            }
                            if not any(t["targetId"] == stock["id"] for t in market_state["trades"]):
                                market_state["trades"].append(trade)

            # Cleanup trades
            market_state["trades"] = [t for t in market_state["trades"] if current_time - t["timestamp"] < 5]

            # 4. Update Dynamic News
            if random.random() < 0.1: # 10% chance per loop (approx every 20s)
                new_news = generate_market_news()
                if new_news:
                    # Check duplication
                    if not market_state["news"] or market_state["news"][0]["title"] != new_news["title"]:
                        market_state["news"].insert(0, new_news)
                        if len(market_state["news"]) > 20:
                            market_state["news"] = market_state["news"][:20]

        except Exception as e:
            print(f"Error in monitor loop: {e}")

        time.sleep(2.0)


def start_market_monitor():
    """Initialize market state and start the monitor thread once."""
    global market_monitor_thread
    with market_monitor_lock:
        if market_monitor_thread and market_monitor_thread.is_alive():
            return market_monitor_thread

        print("Initializing market data...")
        init_market()
        market_monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        market_monitor_thread.start()
        return market_monitor_thread


def load_data_files():
    """Scan data directory and return available data files"""
    data_dir = USER_ROOT / 'data'
    data_files = []

    if data_dir.exists():
        for path in data_dir.iterdir():
            if path.suffix.lower() in ('.csv', '.feather'):
                file_size = path.stat().st_size
                data_files.append({
                    'name': path.name,
                    'path': str(path),
                    'size': f"{file_size / 1024:.1f} KB" if file_size < 1024 * 1024 else f"{file_size / (1024 * 1024):.1f} MB"
                })

    return data_files


def load_data_file(file_path):
    """Load data file"""
    try:
        if file_path.endswith('.csv'):
            df = pd.read_csv(file_path)
        elif file_path.endswith('.feather'):
            df = pd.read_feather(file_path)
        else:
            return None, "Unsupported file format"

        # Check required columns
        required_cols = ['open', 'high', 'low', 'close']
        if not all(col in df.columns for col in required_cols):
            return None, f"Missing required columns: {required_cols}"

        # Process timestamp column
        if 'timestamps' in df.columns:
            df['timestamps'] = pd.to_datetime(df['timestamps'])
        elif 'timestamp' in df.columns:
            df['timestamps'] = pd.to_datetime(df['timestamp'])
        elif 'date' in df.columns:
            # If column name is 'date', rename it to 'timestamps'
            df['timestamps'] = pd.to_datetime(df['date'])
        else:
            # If no timestamp column exists, create one
            df['timestamps'] = pd.date_range(start='2024-01-01', periods=len(df), freq='1H')

        # Ensure numeric columns are numeric type
        for col in ['open', 'high', 'low', 'close']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        # Process volume column (optional)
        if 'volume' in df.columns:
            df['volume'] = pd.to_numeric(df['volume'], errors='coerce')

        # Process amount column (optional, but not used for prediction)
        if 'amount' in df.columns:
            df['amount'] = pd.to_numeric(df['amount'], errors='coerce')

        # Remove rows containing NaN values
        df = df.dropna()

        return df, None

    except Exception as e:
        return None, f"Failed to load file: {str(e)}"


def save_prediction_results(file_path, prediction_type, prediction_results, actual_data, input_data, prediction_params):
    """Save prediction results to file"""
    try:
        # Runtime prediction output must stay outside packaged read-only resources.
        results_dir = USER_ROOT / 'prediction_results'
        results_dir.mkdir(parents=True, exist_ok=True)

        # Generate filename
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'prediction_{timestamp}.json'
        filepath = results_dir / filename

        # Prepare data for saving
        save_data = {
            'timestamp': datetime.datetime.now().isoformat(),
            'file_path': file_path,
            'prediction_type': prediction_type,
            'prediction_params': prediction_params,
            'input_data_summary': {
                'rows': len(input_data),
                'columns': list(input_data.columns),
                'price_range': {
                    'open': {'min': float(input_data['open'].min()), 'max': float(input_data['open'].max())},
                    'high': {'min': float(input_data['high'].min()), 'max': float(input_data['high'].max())},
                    'low': {'min': float(input_data['low'].min()), 'max': float(input_data['low'].max())},
                    'close': {'min': float(input_data['close'].min()), 'max': float(input_data['close'].max())}
                },
                'last_values': {
                    'open': float(input_data['open'].iloc[-1]),
                    'high': float(input_data['high'].iloc[-1]),
                    'low': float(input_data['low'].iloc[-1]),
                    'close': float(input_data['close'].iloc[-1])
                }
            },
            'prediction_results': prediction_results,
            'actual_data': actual_data,
            'analysis': {}
        }

        # If actual data exists, perform comparison analysis
        if actual_data and len(actual_data) > 0:
            # Calculate continuity analysis
            if len(prediction_results) > 0 and len(actual_data) > 0:
                last_pred = prediction_results[0]  # First prediction point
            first_actual = actual_data[0]  # First actual point

            save_data['analysis']['continuity'] = {
                'last_prediction': {
                    'open': last_pred['open'],
                    'high': last_pred['high'],
                    'low': last_pred['low'],
                    'close': last_pred['close']
                },
                'first_actual': {
                    'open': first_actual['open'],
                    'high': first_actual['high'],
                    'low': first_actual['low'],
                    'close': first_actual['close']
                },
                'gaps': {
                    'open_gap': abs(last_pred['open'] - first_actual['open']),
                    'high_gap': abs(last_pred['high'] - first_actual['high']),
                    'low_gap': abs(last_pred['low'] - first_actual['low']),
                    'close_gap': abs(last_pred['close'] - first_actual['close'])
                },
                'gap_percentages': {
                    'open_gap_pct': (abs(last_pred['open'] - first_actual['open']) / first_actual['open']) * 100,
                    'high_gap_pct': (abs(last_pred['high'] - first_actual['high']) / first_actual['high']) * 100,
                    'low_gap_pct': (abs(last_pred['low'] - first_actual['low']) / first_actual['low']) * 100,
                    'close_gap_pct': (abs(last_pred['close'] - first_actual['close']) / first_actual['close']) * 100
                }
            }

        # Save to file
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(save_data, f, indent=2, ensure_ascii=False)

        print(f"Prediction results saved to: {filepath}")
        return str(filepath)

    except Exception as e:
        print(f"Failed to save prediction results: {e}")
        return None


def create_prediction_chart(df, pred_df, lookback, pred_len, actual_df=None, historical_start_idx=0):
    """Create prediction chart"""
    # Use specified historical data start position, not always from the beginning of df
    if historical_start_idx + lookback + pred_len <= len(df):
        # Display lookback historical points + pred_len prediction points starting from specified position
        historical_df = df.iloc[historical_start_idx:historical_start_idx + lookback]
        prediction_range = range(historical_start_idx + lookback, historical_start_idx + lookback + pred_len)
    else:
        # If data is insufficient, adjust to maximum available range
        available_lookback = min(lookback, len(df) - historical_start_idx)
        available_pred_len = min(pred_len, max(0, len(df) - historical_start_idx - available_lookback))
        historical_df = df.iloc[historical_start_idx:historical_start_idx + available_lookback]
        prediction_range = range(historical_start_idx + available_lookback,
                                 historical_start_idx + available_lookback + available_pred_len)

    # Create chart
    fig = go.Figure()

    # Add historical data (candlestick chart)
    fig.add_trace(go.Candlestick(
        x=historical_df['timestamps'] if 'timestamps' in historical_df.columns else historical_df.index,
        open=historical_df['open'],
        high=historical_df['high'],
        low=historical_df['low'],
        close=historical_df['close'],
        name='历史数据 (400个数据点)',
        increasing_line_color='#26A69A',
        decreasing_line_color='#EF5350'
    ))

    # Add prediction data (candlestick chart)
    if pred_df is not None and len(pred_df) > 0:
        # Calculate prediction data timestamps - ensure continuity with historical data
        if 'timestamps' in df.columns and len(historical_df) > 0:
            # Start from the last timestamp of historical data, create prediction timestamps with the same time interval
            last_timestamp = historical_df['timestamps'].iloc[-1]
            time_diff = df['timestamps'].iloc[1] - df['timestamps'].iloc[0] if len(df) > 1 else pd.Timedelta(hours=1)

            pred_timestamps = pd.date_range(
                start=last_timestamp + time_diff,
                periods=len(pred_df),
                freq=time_diff
            )
        else:
            # If no timestamps, use index
            pred_timestamps = range(len(historical_df), len(historical_df) + len(pred_df))

        fig.add_trace(go.Candlestick(
            x=pred_timestamps,
            open=pred_df['open'],
            high=pred_df['high'],
            low=pred_df['low'],
            close=pred_df['close'],
            name='预测数据 (120个数据点)',
            increasing_line_color='#66BB6A',
            decreasing_line_color='#FF7043'
        ))

    # Add actual data for comparison (if exists)
    if actual_df is not None and len(actual_df) > 0:
        # Actual data should be in the same time period as prediction data
        if 'timestamps' in df.columns:
            # Actual data should use the same timestamps as prediction data to ensure time alignment
            if 'pred_timestamps' in locals():
                actual_timestamps = pred_timestamps
            else:
                # If no prediction timestamps, calculate from the last timestamp of historical data
                if len(historical_df) > 0:
                    last_timestamp = historical_df['timestamps'].iloc[-1]
                    time_diff = df['timestamps'].iloc[1] - df['timestamps'].iloc[0] if len(df) > 1 else pd.Timedelta(
                        hours=1)
                    actual_timestamps = pd.date_range(
                        start=last_timestamp + time_diff,
                        periods=len(actual_df),
                        freq=time_diff
                    )
                else:
                    actual_timestamps = range(len(historical_df), len(historical_df) + len(actual_df))
        else:
            actual_timestamps = range(len(historical_df), len(historical_df) + len(actual_df))

        fig.add_trace(go.Candlestick(
            x=actual_timestamps,
            open=actual_df['open'],
            high=actual_df['high'],
            low=actual_df['low'],
            close=actual_df['close'],
            name='实际数据 (120个数据点)',
            increasing_line_color='#FF9800',
            decreasing_line_color='#F44336'
        ))

    # Update layout
    fig.update_layout(
        title='Kronos金融预测结果 - 400个历史数据点 + 120个预测数据点 vs 120个实际数据点',
        xaxis_title='时间',
        yaxis_title='价格',
        template='plotly_white',
        height=600,
        showlegend=True
    )

    # Ensure x-axis time continuity
    if 'timestamps' in historical_df.columns:
        # Get all timestamps and sort them
        all_timestamps = []
        if len(historical_df) > 0:
            all_timestamps.extend(historical_df['timestamps'])
        if 'pred_timestamps' in locals():
            all_timestamps.extend(pred_timestamps)
        if 'actual_timestamps' in locals():
            all_timestamps.extend(actual_timestamps)

        if all_timestamps:
            all_timestamps = sorted(all_timestamps)
            fig.update_xaxes(
                range=[all_timestamps[0], all_timestamps[-1]],
                rangeslider_visible=False,
                type='date'
            )

    return json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)


def _safe_int(value, default, minimum=None, maximum=None):
    """Parse an integer with optional bounds."""
    return safe_int(value, default, minimum=minimum, maximum=maximum)


def _safe_float(value, default=0.0):
    try:
        if value in (None, '', '—', 'N/A'):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _format_datetime(ts=None):
    if ts is None:
        dt = datetime.datetime.now()
    elif isinstance(ts, (int, float)):
        dt = datetime.datetime.fromtimestamp(ts)
    elif isinstance(ts, datetime.datetime):
        dt = ts
    else:
        return str(ts)
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def _load_market_intelligence(force_refresh=False):
    return MARKET_INTELLIGENCE_SERVICE.load(force_refresh=bool(force_refresh))


def _normalize_stock_codes(raw_codes):
    return normalize_stock_codes(raw_codes)


def _stock_code_key(value):
    codes = normalize_stock_codes([value])
    return codes[0] if codes else str(value or '').strip()


def _holdings_news_index(positions, intelligence=None):
    """逐只持仓扫描市场情报,返回 ``{code6: [{platform, title}, ...]}``(每只去重+封顶)。

    名称/代码匹配只在此处做(纯函数 ``score_holdings_relevance`` 不做模糊匹配)。
    名称子串匹配加最小长度门限(≥3)以压假阳性,代码命中始终可信;``platform``
    名须与 ``risk_opportunity_engine.NEWS_PLATFORM_WEIGHTS`` 的键一致。
    """
    intelligence = intelligence if intelligence is not None else _load_market_intelligence()
    cap = 5

    def _matches(code6, name, text):
        if not text:
            return False
        if code6 and code6 in text:
            return True
        return bool(name) and len(name) >= 3 and name in text

    jinshi = intelligence.get('jinshi') or []
    hot_stocks = (intelligence.get('eastmoney') or {}).get('hot_stocks') or []
    em_news = intelligence.get('eastmoney_news') or []
    sina_news = intelligence.get('sina_news') or []
    ths_news = intelligence.get('ths_news') or []
    flash_sources = (
        ('东财快讯', em_news),
        ('新浪快讯', sina_news),
        ('同花顺快讯', ths_news),
    )

    rows = {}
    for pos in positions or []:
        code6 = _stock_code_key(pos.get('ts_code') or pos.get('code'))
        name = str(pos.get('name') or pos.get('stock_name') or '').strip()
        hits = []
        seen = set()

        def add(platform, title):
            title = str(title or '').strip()
            key = (platform, title)
            if title and key not in seen and len(hits) < cap:
                seen.add(key)
                hits.append({'platform': platform, 'title': title})

        for it in jinshi:
            text = f"{it.get('title') or ''} {it.get('source') or ''}"
            if _matches(code6, name, text):
                add('金十快讯', it.get('title'))
        for it in hot_stocks:
            row_code = str(it.get('code') or '').zfill(6)
            if (code6 and row_code == code6) or (name and name == str(it.get('name') or '')):
                add('东财人气热度', f"{name or code6} 资金热度")
        for platform, source in flash_sources:
            for it in source:
                if _matches(code6, name, str(it.get('title') or '')):
                    add(platform, it.get('title'))

        if hits:
            rows[code6] = hits
    return rows



def _json_obj(value, default=None):
    if default is None:
        default = {}
    if value in (None, ''):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if pd.isna(value) if not isinstance(value, (dict, list, tuple, set, str, bytes)) else False:
        return None
    return value


JOB_SERVICE = BackgroundJobService(JOB_STORE, sanitizer=lambda value: _json_safe(value))


def _latest_files(directory, pattern, limit=10):
    if not directory.exists():
        return []
    files = [path for path in directory.glob(pattern) if path.is_file()]
    return sorted(files, key=lambda item: item.stat().st_mtime, reverse=True)[:limit]


def _latest_primary_opportunity_reports(limit=1):
    """Return dashboard-ready Top榜 reports, excluding source-specific long-form files."""
    candidates = _latest_files(RESULTS_DIR, 'opportunity_top10_*.md', limit=80)
    reports = [
        path for path in candidates
        if PRIMARY_OPPORTUNITY_REPORT_RE.match(path.name)
    ]
    return reports[:limit]


def _latest_primary_opportunity_report_since(since_ts, tolerance=2.0):
    """Return the newest dashboard-ready Top榜 report produced after a job started."""
    for path in _latest_primary_opportunity_reports(limit=20):
        try:
            if path.stat().st_mtime + tolerance >= since_ts:
                return path
        except OSError:
            continue
    return None


def _report_url(path):
    if not path:
        return None
    report_path = Path(path)
    if not report_path.is_absolute():
        report_path = PROJECT_ROOT / report_path
    try:
        resolved = report_path.resolve()
    except OSError:
        return None

    for key, directory in REPORT_DIRS.items():
        try:
            relative = resolved.relative_to(directory.resolve())
            return f"/analysis-reports/{key}/{relative.as_posix()}"
        except ValueError:
            continue
    return None


_OPPORTUNITY_TERMINAL_LOG_MARKERS = (
    '自动参数优化默认关闭',
    'KRONOS_SKIP_AUTO_OPTIMIZE',
    '自动优化已生效',
    '自动优化未生效',
    '自动回测失败',
)


def _log_path_after_label(logs, label):
    prefix = f"{label}:"
    for line in reversed(logs or []):
        text = str(line)
        if prefix not in text:
            continue
        value = text.split(prefix, 1)[1].strip()
        return value or ''
    return ''


def _opportunity_completion_result_from_logs(job):
    logs = [str(line) for line in (job.get('logs') or [])]
    joined = '\n'.join(logs)
    if '投资机会挖掘完成' not in joined or '报表路径:' not in joined:
        return None
    if '挖掘结果已入库' not in joined:
        return None
    if not any(marker in joined for marker in _OPPORTUNITY_TERMINAL_LOG_MARKERS):
        return None

    report_path = _log_path_after_label(logs, '报表路径')
    top_report_raw = _log_path_after_label(logs, 'Top榜路径')
    top_report_path = Path(top_report_raw) if top_report_raw else None
    params = job.get('params') or {}
    source = str(params.get('source') or 'multi').strip() or 'multi'
    source_label = {
        'multi': '多源综合',
        'heat': '仅热度榜',
        'moneyflow_dc': '资金流向榜单',
        'sector_hot': '热门板块成分股',
    }.get(source, source)
    stock_codes = params.get('stock_codes') or []
    mode = 'specified_pool' if stock_codes else 'market_scan'
    mode_label = '指定股票池' if stock_codes else '全市场扫描'
    return {
        'report_path': report_path,
        'report_file': Path(report_path).name if report_path else '',
        'report_url': _report_url(report_path) if report_path else None,
        'top_report_path': str(top_report_path) if top_report_path else '',
        'top_report_file': top_report_path.name if top_report_path else '',
        'top_report_url': _report_url(top_report_path) if top_report_path else None,
        'mode': mode,
        'mode_label': mode_label,
        'source': source,
        'source_label': source_label,
        'params': {
            'limit': params.get('limit'),
            'workers': params.get('workers'),
            'source': source,
            'stock_codes': stock_codes,
        },
    }


def _reconcile_completed_opportunity_job(job):
    if not job or job.get('type') != 'opportunity_discovery':
        return job
    if job.get('status') not in ('queued', 'running'):
        return job
    result = _opportunity_completion_result_from_logs(job)
    if not result:
        return job

    logs = list(job.get('logs') or [])
    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if not any('任务完成状态已根据已生成报告自动校正' in str(line) for line in logs):
        logs.append(f"{stamp} 任务完成状态已根据已生成报告自动校正")
    return JOB_SERVICE.update(
        job['id'],
        status='finished',
        finished_at=datetime.datetime.now().isoformat(),
        result=result,
        logs=logs,
    ) or job


def _reconcile_completed_opportunity_jobs_on_startup():
    try:
        active_jobs = JOB_STORE.list_by_status(('queued', 'running'), limit=200)
    except Exception:  # noqa: BLE001
        active_jobs = []
    for job in active_jobs:
        _reconcile_completed_opportunity_job(job)


_reconcile_completed_opportunity_jobs_on_startup()


def mark_interrupted_jobs_on_boot() -> int:
    """把共享任务库里 running/queued 的任务标记「后台进程已重启,任务已中断」。

    只能在真正作为后端服务启动时调用(robyn_app 的 startup 钩子)。过去挂在模块
    import 期:任何 import webui.core 的进程(pytest 收集、CLI 脚本、并行的 dev
    实例)都会误杀打包 App 正在跑的任务——2026-07-24 实际误杀过一次机会挖掘。
    KRONOS_SKIP_INTERRUPT_MARK=1 供 dev 服与打包 App 并行时跳过标记。
    """
    if str(os.environ.get('KRONOS_SKIP_INTERRUPT_MARK', '')).strip().lower() in ('1', 'true', 'yes', 'on'):
        return 0
    return JOB_SERVICE.mark_interrupted_jobs()


def reveal_in_file_manager(path) -> bool:
    """在系统文件管理器(Finder/资源管理器)里定位已导出的文件,best-effort。

    打包 App 的 WKWebView 不会触发 ``Content-Disposition: attachment`` 下载,而主窗口
    导航到本机后端的 http 源(127.0.0.1:7070)之后又拿不到 Tauri IPC —— 浏览器侧任何
    下载方式(``<a download>``/隐藏 iframe/``location.href``)都静默失效,这正是「点击
    导出 Excel 无反应」的真因。导出文件本身已写到结果目录,故改由本机后端直接把它在
    文件管理器里选中:既绕过 WKWebView 的下载限制,也不依赖 Tauri IPC,dev 与打包态
    行为一致。

    安全护栏:只允许定位由我们自己生成、位于 :data:`REPORT_DIRS` 之内的文件,避免本
    函数沦为任意文件打开器。返回是否成功唤起文件管理器。
    """
    try:
        resolved = Path(path).expanduser().resolve()
    except (TypeError, ValueError, OSError):
        return False
    if not resolved.is_file():
        return False

    within_allowed = False
    for directory in REPORT_DIRS.values():
        try:
            resolved.relative_to(Path(directory).resolve())
            within_allowed = True
            break
        except (ValueError, OSError):
            continue
    if not within_allowed:
        return False

    if sys.platform == "darwin":
        cmd = ["/usr/bin/open", "-R", str(resolved)]
    elif sys.platform.startswith("win"):
        # explorer 选中单个文件:/select 后必须紧跟逗号且不留空格。
        cmd = ["explorer", f"/select,{resolved}"]
    else:  # Linux/其它:xdg-open 不支持选中单个文件,退而打开所在目录。
        opener = shutil.which("xdg-open")
        if not opener:
            return False
        cmd = [opener, str(resolved.parent)]

    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, ValueError):
        return False


def _strip_markup(value):
    text = unescape(str(value or ''))
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('**', '').replace('&nbsp;', ' ')
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _truncate_text(value, limit=180):
    text = _strip_markup(value)
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + '…'


def _extract_detail_section(detail, title):
    pattern = rf'【{re.escape(title)}】([^【]+)'
    match = re.search(pattern, detail or '')
    return _strip_markup(match.group(1)).strip('；; ') if match else ''


# 报告 详细分析 单元格中合法的顶层 【…】 区块标签（其余 【…】 视为正文内嵌）。
_KNOWN_DETAIL_LABELS = {
    '概览', '涨幅', '板块', '量化', '技术', '基本面', '情绪资金', '消息',
    '关键加减分', '入选原因', '最新动态', '高级', '历史重复入选', '风险提示',
}


def _extract_all_detail_sections(detail):
    """Extract every 【label】value section from a packed 详细分析 cell, in order.

    Standard reports pack all rich fields (涨幅/板块/量化/技术/基本面/情绪资金/消息/
    关键加减分/入选原因/最新动态/高级…) into one table cell separated by 【】 markers.
    News/dynamics text frequently embeds its own 【…】 markers (e.g. 【股商异动】 or a
    quoted headline); those are NOT top-level fields, so any 【label】 outside the known
    section set is folded back into the preceding section's value rather than becoming a
    spurious field/column. Returns an ordered list of {'label', 'value'}.
    """
    sections = []
    for label, value in re.findall(r'【([^】]+)】([^【]+)', detail or ''):
        clean_value = _strip_markup(value).strip('；; ')
        clean_label = _strip_markup(label)
        if not clean_label or not clean_value:
            continue
        if clean_label in _KNOWN_DETAIL_LABELS or not sections:
            sections.append({'label': clean_label, 'value': clean_value})
        else:
            prev = sections[-1]
            prev['value'] = f"{prev['value']} 【{clean_label}】{clean_value}".strip()
    return sections


def _parse_rating(detail):
    overview = _extract_detail_section(detail, '概览')
    match = re.search(r'评级\s*([A-Z]\+?|S|C)', overview)
    return match.group(1) if match else ''


def _parse_recommendation(detail):
    overview = _extract_detail_section(detail, '概览')
    match = re.search(r'建议[:：]\s*([^；;，,]+)', overview)
    return _strip_markup(match.group(1)) if match else ''


def _parse_sector(detail):
    sector = _extract_detail_section(detail, '板块')
    return re.split(r'[（(]', sector, maxsplit=1)[0].strip() if sector else ''


def _parse_quant_models(detail):
    quant = _extract_detail_section(detail, '量化')
    match = re.search(r'模型\[([^\]]+)\]', quant)
    if not match:
        return []
    models = [
        _strip_markup(item)
        for item in re.split(r'[,，/、\s]+', match.group(1))
        if _strip_markup(item) and _strip_markup(item) not in {'无', '-', '—'}
    ]
    return models[:8]


def _html_table_rows(markdown):
    rows = []
    for row_html in re.findall(r'<tr[^>]*>(.*?)</tr>', markdown or '', flags=re.I | re.S):
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row_html, flags=re.I | re.S)
        if len(cells) < 5:
            continue
        cleaned = [_strip_markup(cell) for cell in cells]
        if cleaned[0] in {'排名', '#'} or cleaned[1] in {'代码', '股票代码'}:
            continue
        rows.append(cleaned)
    return rows


def _markdown_table_rows(markdown):
    rows = []
    for raw_line in str(markdown or '').splitlines():
        line = raw_line.strip()
        if not line.startswith('|') or '---' in line:
            continue
        cells = [_strip_markup(cell) for cell in line.strip('|').split('|')]
        if len(cells) < 5 or cells[0] in {'排名', '#'} or cells[1] in {'代码', '股票代码'}:
            continue
        rows.append(cells)
    return rows


def _parse_opportunity_report(path):
    report_path = Path(path)
    try:
        content = report_path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        content = ''

    rows = _html_table_rows(content) or _markdown_table_rows(content)
    items = []
    for cells in rows:
        rank_raw, code, name, score_raw = cells[0], cells[1], cells[2], cells[3]
        # 详细分析是第 5 列起唯一带 【…】 的那格 —— 2026-07-29 起排名表在
        # 「综合得分」与「详细分析」之间插入了「表现标记」列,列位不再固定。
        # 昨日复盘 / 置信度表同为 5+ 列但没有 【…】(第 5 格是价格/描述),整行跳过。
        detail = next((cell for cell in cells[4:] if '【' in (cell or '')), '')
        if not detail:
            continue
        code_match = re.search(r'\d{6}', code)
        if not code_match:
            continue
        score_match = re.search(r'-?\d+(?:\.\d+)?', score_raw)
        score = float(score_match.group(0)) if score_match else 0.0
        reason = _extract_detail_section(detail, '入选原因') or _truncate_text(detail, 180)
        quant = _extract_detail_section(detail, '量化')
        degraded = '数据降级' in (detail or '') or '无历史数据' in (detail or '') or 'degraded' in (detail or '').lower()
        item = {
            'rank': _safe_int(rank_raw, len(items) + 1, minimum=1),
            'code': code_match.group(0),
            'stock_code': code_match.group(0),
            'name': _strip_markup(name),
            'stock_name': _strip_markup(name),
            'score': score,
            'rating': _parse_rating(detail),
            'recommendation': _parse_recommendation(detail),
            'sector': _parse_sector(detail),
            'industry': _parse_sector(detail),
            'technical': _extract_detail_section(detail, '技术'),
            'quant': quant,
            'sentiment': _extract_detail_section(detail, '情绪资金'),
            'risk': _extract_detail_section(detail, '关键加减分'),
            'reason': reason,
            'summary': reason,
            'quant_models': _parse_quant_models(detail),
            'fields': _extract_all_detail_sections(detail),
            'degraded': degraded,
            'has_local_kline': False,
        }
        items.append(item)

    market_env = ''
    risk_match = re.search(r'<p[^>]*>\s*⚠️\s*<strong>(.*?)</strong>', content, flags=re.S)
    if risk_match:
        market_env = _strip_markup(risk_match.group(1))
    if not market_env:
        env_match = re.search(r'市场环境[^\n]*', content)
        if env_match:
            market_env = _strip_markup(env_match.group(0))
    if not market_env:
        heading_match = re.search(r'##\s*([^\n]+)', content)
        market_env = _strip_markup(heading_match.group(1)) if heading_match else '已解析最新机会挖掘报告'

    # 报告头的机器可读运行元信息(评分规则版本/来源/配置哈希),用于前端区分 run
    run_meta = {}
    meta_match = re.search(r'<!--\s*kronos-run-meta\s+(\{.*?\})\s*-->', content, flags=re.S)
    if meta_match:
        try:
            run_meta = json.loads(meta_match.group(1))
        except (ValueError, TypeError):
            run_meta = {}

    degraded_count = len([item for item in items if item.get('degraded')])

    return {
        'file': report_path.name,
        'url': _report_url(report_path),
        'updated_at': _format_datetime(report_path.stat().st_mtime) if report_path.exists() else '--',
        'market_env': market_env,
        'run_meta': run_meta,
        'degraded_count': degraded_count,
        'all_degraded': bool(items) and degraded_count == len(items),
        'items': items,
    }


def load_opportunity_report_cards(file):
    """Parse a SPECIFIC opportunity report (by filename) into same-style rich cards.

    Security: only a bare filename inside RESULTS_DIR is accepted (Path(...).name
    strips any traversal), and only ``opportunity_top10_*.md`` reports are served.
    Returns None when the file is invalid or missing so the route can answer 404.
    """
    name = Path(str(file or '')).name
    if not name.startswith('opportunity_top10_') or not name.endswith('.md'):
        return None
    target = RESULTS_DIR / name
    if not target.is_file():
        return None
    parsed = _parse_opportunity_report(target)
    hot_sector = _load_hot_sector_snapshot_summary()
    run_payload = _load_opportunity_run_payload(parsed['file'])
    items = _merge_opportunity_items(parsed['items'], (run_payload or {}).get('items'))
    latest_report = {
        'file': parsed['file'],
        'url': parsed['url'],
        'updated_at': parsed['updated_at'],
        'run_meta': parsed.get('run_meta') or {},
        'degraded_count': parsed.get('degraded_count', 0),
        'all_degraded': parsed.get('all_degraded', False),
    }
    if run_payload:
        latest_report.update({
            'run_id': (run_payload.get('run') or {}).get('id'),
            'run_item_count': len(run_payload.get('items') or []),
        })
    return {
        'file': parsed['file'],
        'url': parsed['url'],
        'updated_at': parsed['updated_at'],
        'market_env': parsed['market_env'],
        'count': len(items),
        'latest_report': latest_report,
        'degraded_count': parsed.get('degraded_count', 0),
        'all_degraded': parsed.get('all_degraded', False),
        'items': items,
        'cards': items,
        'canvas': _build_opportunity_canvas(items, latest_report, parsed['market_env'], hot_sector),
        'hot_sector': hot_sector,
    }


def _opportunity_tag_sections(item):
    """Extract label/value sections for the desktop opportunity canvas.

    Returns every 【…】 section parsed from the report (概览/涨幅/板块/量化/技术/
    基本面/情绪资金/消息/关键加减分/入选原因/最新动态/高级/历史重复入选…) so the
    canvas detail panel and the Excel export show the full, fine-grained content.
    Previously this was capped at 10 sections, which silently dropped 涨幅/概览/
    历史重复入选.
    """
    preferred = [
        '概览', '入选原因', '涨幅', '板块', '量化', '技术', '基本面',
        '情绪资金', '消息', '关键加减分', '最新动态', '高级', '历史重复入选',
    ]
    fields = item.get('fields') if isinstance(item, dict) else []
    by_label = {}
    if isinstance(fields, list):
        for field in fields:
            if not isinstance(field, dict):
                continue
            label = str(field.get('label') or '').strip()
            value = _strip_markup(field.get('value') or '')
            if label and value and label not in by_label:
                by_label[label] = value

    fallback_pairs = [
        ('入选原因', item.get('reason') or item.get('summary')),
        ('板块', item.get('sector') or item.get('industry')),
        ('量化', item.get('quant')),
        ('技术', item.get('technical')),
        ('情绪资金', item.get('sentiment')),
        ('关键加减分', item.get('risk')),
    ]
    for label, value in fallback_pairs:
        value = _strip_markup(value or '')
        if value and label not in by_label:
            by_label[label] = value

    sections = []
    for label in preferred:
        value = by_label.get(label)
        if value:
            sections.append({
                'label': label,
                'value': value,
                'summary': _truncate_text(value, 96),
            })
    for label, value in by_label.items():
        if label not in preferred and value:
            sections.append({
                'label': label,
                'value': value,
                'summary': _truncate_text(value, 96),
            })
    # Keep every parsed section (a generous safety cap guards against pathological
    # input). Do NOT truncate to 10 — that used to drop 涨幅/概览/历史重复入选.
    return sections[:40]


def _opportunity_canvas_views():
    return [
        {'id': 'hierarchy', 'label': '层级', 'description': '报告 → 板块 → 股票 → 分析内容'},
        {'id': 'score_rank', 'label': '排名', 'description': '全部股票按综合评分降序'},
        {'id': 'sector', 'label': '板块', 'description': '按板块聚合股票关系'},
        {'id': 'business_tag', 'label': '标签', 'description': '按概念 / 板块 / 股票等业务标签组织关系'},
        {'id': 'hot_sector', 'label': '热门', 'description': '前十大热门板块全量关系'},
        {'id': 'funds', 'label': '资金', 'description': '按主力净流入维度聚合'},
        {'id': 'dragon_tiger', 'label': '龙虎榜', 'description': '热门板块与龙虎榜命中关系'},
    ]


def _empty_opportunity_canvas(reason='暂无机会挖掘报告'):
    return {
        'title': '投资机会分析',
        'subtitle': reason,
        'levels': ['报告', '板块', '股票', '分析内容'],
        'views': _opportunity_canvas_views(),
        'nodes': [{
            'id': 'root',
            'type': 'root',
            'level': 0,
            'title': '投资机会分析',
            'subtitle': reason,
            'detail': reason,
            'tags': [],
        }],
        'edges': [],
        'stats': {
            'sectors': 0,
            'stocks': 0,
            'tags': 0,
            'analysis': 0,
        },
    }


def _load_hot_sector_snapshot_summary(snapshot_id=None):
    try:
        from data_store import hot_sector_repo
        snapshot = hot_sector_repo.get_snapshot(snapshot_id)
        if not snapshot:
            return None
        boards = hot_sector_repo.boards_for_snapshot(snapshot['id'])
        return {
            'snapshot': snapshot,
            'boards': boards,
            'stats': {
                'boards': len(boards),
                'stocks': int(snapshot.get('stock_count') or 0),
                'relations': int(snapshot.get('relation_count') or 0),
            },
        }
    except Exception as exc:
        logger.debug(f"加载热门板块快照失败: {exc}")
        return None


def list_hot_sector_snapshots(limit=50):
    """Historical hot-sector snapshots for the opportunity discovery page."""
    try:
        from data_store import hot_sector_repo
        limit = _safe_int(limit, 50, minimum=1, maximum=200) or 50
        return hot_sector_repo.list_snapshots(limit=limit)
    except Exception as exc:
        logger.debug(f"加载热门板块历史快照失败: {exc}")
        return []


def hot_sector_stocks_payload(snapshot_id=None, board_code=None, limit=200, offset=0):
    try:
        from data_store import hot_sector_repo
        snapshot = hot_sector_repo.get_snapshot(snapshot_id)
        if not snapshot:
            return {'snapshot': None, 'stocks': [], 'relations': [], 'has_more': False}
        sid = int(snapshot['id'])
        limit = _safe_int(limit, 200, minimum=1, maximum=1000) or 200
        offset = _safe_int(offset, 0, minimum=0, maximum=1000000) or 0
        rows = hot_sector_repo.stocks_for_snapshot(
            sid,
            board_code=board_code,
            limit=limit + 1,
            offset=offset,
        )
        has_more = len(rows) > limit
        stocks = _overlay_board_realtime(rows[:limit], board_code)
        relations = hot_sector_repo.relations_for_snapshot(
            sid,
            board_code=board_code,
            limit=1000,
        )
        return {
            'snapshot': snapshot,
            'stocks': stocks,
            'relations': relations,
            'has_more': has_more,
            'next_offset': offset + len(stocks),
        }
    except Exception as exc:
        return {'snapshot': None, 'stocks': [], 'relations': [], 'has_more': False, 'error': str(exc)}


def export_hot_sector_snapshot(snapshot_id=None):
    from data_store import hot_sector_repo
    snapshot = hot_sector_repo.get_snapshot(snapshot_id)
    if not snapshot:
        return None
    sid = int(snapshot['id'])

    # 逐股评分:关联到该快照的最近一次挖掘 run,按代码映射成 {code: opportunity_item}。
    scores = {}
    try:
        from data_store import opportunity_repo
        run = opportunity_repo.run_for_hot_sector_snapshot(sid)
        if run:
            for item in opportunity_repo.items_for_run(int(run['id'])):
                code = str(item.get('code') or '')
                if code:
                    scores[code] = item
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"关联机会评分失败(导出降级为无评分): {exc}")

    # 实时报价:回填快照里为空的 现价/涨跌幅/主力净流入(东财→腾讯→Tushare 多源)。
    quotes = {}
    try:
        from webui.services import star_orbit_service
        codes = [str(s.get('code') or '') for s in hot_sector_repo.stocks_for_snapshot(sid, limit=100000)]
        quotes = star_orbit_service._quote_overlay([c for c in codes if c]) or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"实时报价回填失败(导出降级为快照原值): {exc}")

    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    path = RESULTS_DIR / f"hot_sector_snapshot_{sid}_{timestamp}.xlsx"
    out = hot_sector_repo.export_snapshot_excel(sid, path, scores=scores, quotes=quotes)
    return {
        'file': out.name,
        'path': str(out),
        'url': _report_url(out),
        'snapshot_id': sid,
    }


def _load_opportunity_run_payload(report_file=None, run_id=None):
    """Load full scored stocks from the structured opportunity run store.

    ``run_id`` 优先(画布按日切换按 run 精确取);否则按 ``report_file`` 匹配;
    都没有则取最近一次 run。
    """
    try:
        from data_store import opportunity_repo
        run = None
        if run_id is not None and hasattr(opportunity_repo, 'get_run'):
            run = opportunity_repo.get_run(run_id)
        if not run and report_file and hasattr(opportunity_repo, 'run_for_report_file'):
            run = opportunity_repo.run_for_report_file(report_file)
        if not run and report_file:
            for candidate in opportunity_repo.list_runs(limit=80):
                if Path(str(candidate.get('report_file') or '')).name == Path(str(report_file)).name:
                    run = candidate
                    break
        if not run and not report_file and run_id is None:
            run = opportunity_repo.latest_run()
        if not run:
            return None
        rows = opportunity_repo.items_for_run(int(run['id']))
        return {
            'run': run,
            'items': [_opportunity_item_from_run_row(row) for row in rows],
        }
    except Exception as exc:
        logger.debug(f"加载机会挖掘全量 run 明细失败: {exc}")
        return None


def _hot_sector_snapshot_for_date(date=None):
    """Pick the hot-sector snapshot best matching ``date`` (YYYY-MM-DD).

    精确匹配该日 ``trade_date``(无则 ``created_at`` 当日);否则回退到 ``<= date``
    最近一条;``date`` 为空时取最新快照。返回 ``_load_hot_sector_snapshot_summary``
    结构或 None。
    """
    try:
        from data_store import hot_sector_repo
        snapshots = hot_sector_repo.list_snapshots(limit=200) or []
    except Exception as exc:
        logger.debug(f"列出热门板块快照失败: {exc}")
        return None
    if not snapshots:
        return None

    def _eff_date(snap):
        td = str(snap.get('trade_date') or '').strip()
        if td:
            return td[:10]
        return str(snap.get('created_at') or '')[:10]

    target = str(date or '').strip()[:10]
    if not target:
        return _load_hot_sector_snapshot_summary(snapshots[0].get('id'))

    # list_snapshots 已按 created_at DESC 排序;先找精确日,再找 <= target 最近一条
    exact = next((s for s in snapshots if _eff_date(s) == target), None)
    if exact:
        return _load_hot_sector_snapshot_summary(exact.get('id'))
    earlier = [s for s in snapshots if _eff_date(s) and _eff_date(s) <= target]
    if earlier:
        chosen = max(earlier, key=_eff_date)
        return _load_hot_sector_snapshot_summary(chosen.get('id'))
    return None


def opportunity_canvas_payload(run_id=None, date=None):
    """Build a canvas payload for a SPECIFIC run/day (画布按日切换)。

    优先 ``run_id``,否则 ``date`` 当日最新 run,都没有则最近一次 run。
    返回 ``{canvas, run, hot_sector, date, degraded_count}``;无对应 run 返回 None
    (路由据此回 404)。
    """
    try:
        from data_store import opportunity_repo
    except Exception as exc:
        logger.debug(f"opportunity_repo 不可用: {exc}")
        return None

    run = None
    if run_id is not None:
        run = opportunity_repo.get_run(run_id) if hasattr(opportunity_repo, 'get_run') else None
    elif date:
        runs = opportunity_repo.list_runs(run_date=str(date)[:10], limit=1)
        run = runs[0] if runs else None
    else:
        run = opportunity_repo.latest_run()
    if not run:
        return None

    rid = int(run['id'])
    run_payload = _load_opportunity_run_payload(run_id=rid)
    run_items = (run_payload or {}).get('items') or []

    # 报告文件存在则解析(取 market_env + 富文本分节);否则降级为最小 meta
    report_name = Path(str(run.get('report_file') or '')).name
    parsed = None
    if report_name and PRIMARY_OPPORTUNITY_REPORT_RE.match(report_name):
        report_path = RESULTS_DIR / report_name
        if report_path.is_file():
            try:
                parsed = _parse_opportunity_report(report_path)
            except Exception as exc:
                logger.warning(f"按日画布解析报告失败: {exc}")
                parsed = None

    market_env = parsed.get('market_env') if parsed else ''
    parsed_items = parsed.get('items') if parsed else []
    items = _merge_opportunity_items(parsed_items, run_items)

    run_date = str(run.get('run_date') or run.get('run_at') or '')[:10]
    latest_report = {
        'file': report_name or (parsed or {}).get('file') or '',
        'url': _report_url(RESULTS_DIR / report_name) if report_name else None,
        'updated_at': (parsed or {}).get('updated_at') or run.get('run_at') or '--',
        'run_id': rid,
        'run_at': run.get('run_at'),
        'run_item_count': len(run_items),
    }
    # 热门板块快照:优先 run 记录的 snapshot_id,否则按日匹配
    run_extra = _json_obj(run.get('extra_json'), {})
    hot_sector = None
    snap_id = run_extra.get('hot_sector_snapshot_id')
    if snap_id:
        hot_sector = _load_hot_sector_snapshot_summary(snap_id)
    if not hot_sector:
        hot_sector = _hot_sector_snapshot_for_date(run_date)

    degraded_count = sum(1 for it in items if isinstance(it, dict) and it.get('degraded'))
    canvas = _build_opportunity_canvas(items, latest_report, market_env, hot_sector)
    return {
        'canvas': canvas,
        'run': {
            'id': rid,
            'run_at': run.get('run_at'),
            'run_date': run_date,
            'source': run.get('source'),
            'mode': run.get('mode'),
            'item_count': run.get('item_count') if run.get('item_count') is not None else len(run_items),
            'report_file': report_name,
        },
        'hot_sector': hot_sector,
        'date': run_date,
        'degraded_count': degraded_count,
    }


def stock_financial_statements(code, force_refresh=False):
    """个股财务三大表:默认读缓存(财报低频更新,不频繁联网);缺失/强制刷新才取数并落库。

    返回 {code, statements:{balance,income,cashflow}, source, as_of, cached, error?}。
    """
    code = str(code or "").strip()
    if not code:
        return {"code": "", "statements": {"balance": [], "income": [], "cashflow": []},
                "source": None, "as_of": None, "cached": False, "error": "缺少股票代码"}
    try:
        from data_store import financial_statements_repo as repo
    except Exception as exc:
        logger.debug(f"financial_statements_repo 不可用: {exc}")
        repo = None

    def _from_cache():
        if not repo:
            return None
        statements = {stype: repo.statements_for(code, stype, limit=8)
                      for stype in ("balance", "income", "cashflow")}
        if not any(statements.values()):
            return None
        all_rows = [r for stype in statements for r in statements[stype]]
        as_of = max((r.get("created_at") or "" for r in all_rows), default="") or None
        source = next((r.get("source") for r in all_rows if r.get("source")), None)
        return {"code": code, "statements": statements, "source": source,
                "as_of": as_of, "cached": True}

    if not force_refresh:
        cached = _from_cache()
        if cached:
            return cached

    fetched = None
    try:
        from analysis import financial_statements_provider as provider
        fetched = provider.fetch_three_statements(code)
    except Exception as exc:
        logger.warning(f"财务三大表取数失败 {code}: {exc}")

    if fetched and repo:
        ts_code = None
        try:
            from data_store import tushare_client
            ts_code = tushare_client.to_ts_code(code)
        except Exception:
            ts_code = None
        for stype in ("balance", "income", "cashflow"):
            try:
                repo.save_statements(code, stype, fetched.get(stype) or [],
                                     source=fetched.get("source"), ts_code=ts_code)
            except Exception as exc:
                logger.debug(f"财务报表落库失败 {code}/{stype}: {exc}")
        cached = _from_cache()
        if cached:
            cached["cached"] = False  # 本次为新取数
            return cached
    if fetched:
        return {"code": code,
                "statements": {s: fetched.get(s, []) for s in ("balance", "income", "cashflow")},
                "source": fetched.get("source"), "as_of": None, "cached": False}
    return {"code": code, "statements": {"balance": [], "income": [], "cashflow": []},
            "source": None, "as_of": None, "cached": False, "error": "暂无财务数据"}


def _format_score_parts(scores):
    # 评分分项 EN→CN(与 lumo_desktop_app.js 维度字典 / _SCORE_PART_COLUMNS 对齐),
    # 覆盖全部分项,避免 momentum/volume_health/liquidity/events/dragon_tiger 等裸键
    # 直接以英文显示在「个股机会」快速信息里(用户看不懂)。
    labels = {
        'sector': '板块',
        'technical': '技术',
        'quantitative': '量化',
        'fundamental': '基本面',
        'sentiment': '情绪',
        'news': '消息',
        'event': '事件',
        'events': '事件',
        'moneyflow': '资金',
        'momentum': '动量',
        'volume_health': '量能',
        'liquidity': '流动性',
        'dragon_tiger': '龙虎榜',
    }
    parts = []
    if isinstance(scores, dict):
        for key, value in scores.items():
            n = _safe_float(value, None)
            label = labels.get(str(key), str(key))
            parts.append(f"{label}:{n:.1f}" if n is not None else f"{label}:{value}")
    return '，'.join(parts)


def _format_signal_parts(signals):
    labels = {
        'chase': '追高风险',
        'rsi': 'RSI',
        'day_change': '当日涨幅',
        'change_3d': '3日涨幅',
        'change_5d': '5日涨幅',
        'sell_signals': '卖出信号',
        'quant_score': '量化分',
    }
    parts = []
    if isinstance(signals, dict):
        for key in ['chase', 'rsi', 'day_change', 'change_3d', 'change_5d', 'sell_signals', 'quant_score']:
            if key not in signals or signals.get(key) in (None, ''):
                continue
            n = _safe_float(signals.get(key), None)
            label = labels.get(key, key)
            parts.append(f"{label}:{n:.1f}" if n is not None else f"{label}:{signals.get(key)}")
        exclusions = signals.get('exclusions')
        if isinstance(exclusions, list) and exclusions:
            parts.append(f"过滤标记:{'、'.join(str(item) for item in exclusions[:5])}")
    return '，'.join(parts)


def _format_outcome_markers(signals) -> str:
    """入选后表现标记 → 一行文本(见 analysis/outcome_markers.py)。旧版 run 无此字段返回空串。

    只出「体质分层 + 命中的标记」, 不复述回测数字 —— 回测是标定这些规则的依据,
    不是给用户看的结论。
    """
    if not isinstance(signals, dict):
        return ''
    markers = signals.get('markers')
    if not isinstance(markers, list) or not markers:
        return ''
    con = signals.get('constitution') or {}
    badges = '、'.join(f"{m.get('emoji', '')}{m.get('label', '')}" for m in markers[:4])
    grade = con.get('grade') or ''
    if not grade:
        return badges
    summary = con.get('summary') or ''
    head = f"{grade}（{summary}）" if summary else grade
    return f"{head}：{badges}"


def _opportunity_item_from_run_row(row):
    row = dict(row or {})
    scores = _json_obj(row.get('scores_json'), {})
    signals = _json_obj(row.get('signals_json'), {})
    code = _stock_code_key(row.get('code'))
    score = _safe_float(row.get('total_score'), 0.0) or 0.0
    fields = []
    score_parts = _format_score_parts(scores)
    signal_parts = _format_signal_parts(signals)
    if score_parts:
        fields.append({'label': '评分分项', 'value': score_parts})
    if signal_parts:
        fields.append({'label': '风险信号', 'value': signal_parts})
    marker_parts = _format_outcome_markers(signals)
    if marker_parts:
        fields.append({'label': '表现标记', 'value': marker_parts})
    if row.get('source'):
        _src = str(row.get('source'))
        fields.append({'label': '候选来源', 'value': _CANVAS_SOURCE_LABELS.get(_src, _src)})
    if row.get('source_detail'):
        fields.append({'label': '来源细节', 'value': str(row.get('source_detail'))})
    if row.get('sector'):
        fields.append({'label': '板块', 'value': str(row.get('sector'))})
    sector_rank = _safe_int(row.get('sector_rank'), None, minimum=1)
    sector_stock_rank = _safe_int(row.get('sector_stock_rank'), None, minimum=1)
    if sector_rank or sector_stock_rank:
        fields.append({
            'label': '板块排名',
            'value': f"板块#{sector_rank or '--'} · 成分股#{sector_stock_rank or '--'}",
        })
    if row.get('change_pct') not in (None, ''):
        fields.append({'label': '涨幅', 'value': f"{_safe_float(row.get('change_pct'), 0.0):.2f}%"})
    rank = _safe_int(row.get('item_rank'), None, minimum=1)
    sector_name = row.get('sector') or ''
    return {
        'rank': rank,
        'report_rank': rank,
        'code': code,
        'stock_code': code,
        'name': row.get('name') or code,
        'stock_name': row.get('name') or code,
        'score': score,
        'rating': row.get('rating') or '',
        'recommendation': '',
        'sector': sector_name,
        'industry': sector_name,
        'technical': '',
        'quant': score_parts,
        'sentiment': '',
        'risk': signal_parts,
        'reason': row.get('source_detail') or f"全量机会挖掘第 {rank or '--'} 名，综合评分 {score:.2f}。",
        'summary': row.get('source_detail') or f"全量机会挖掘第 {rank or '--'} 名，综合评分 {score:.2f}。",
        'quant_models': [],
        'fields': fields,
        'score_breakdown': scores if isinstance(scores, dict) else {},
        'signals': signals if isinstance(signals, dict) else {},
        'source': row.get('source') or '',
        'source_detail': row.get('source_detail') or '',
        'sector_code': row.get('sector_code') or '',
        'sector_rank': sector_rank,
        'sector_stock_rank': sector_stock_rank,
        'change_pct': _safe_float(row.get('change_pct'), None),
        'degraded': bool(row.get('degraded')),
        'has_local_kline': False,
        'from_run_store': True,
    }


def _merge_opportunity_items(parsed_items, run_items):
    """Use full run rows as the source of truth, preserving parsed rich sections."""
    parsed_by_code = {
        _stock_code_key(item.get('stock_code') or item.get('code')): item
        for item in (parsed_items or [])
        if isinstance(item, dict)
    }
    if not run_items:
        return sorted(
            list(parsed_items or []),
            key=lambda item: _safe_float(item.get('score'), 0.0) or 0.0,
            reverse=True,
        )
    merged = []
    for item in run_items:
        if not isinstance(item, dict):
            continue
        code = _stock_code_key(item.get('stock_code') or item.get('code'))
        parsed = dict(parsed_by_code.get(code) or {})
        base_fields = [dict(field) for field in parsed.get('fields') or [] if isinstance(field, dict)]
        existing_labels = {field.get('label') for field in base_fields}
        for field in item.get('fields') or []:
            if isinstance(field, dict) and field.get('label') not in existing_labels:
                base_fields.append(dict(field))
                existing_labels.add(field.get('label'))
        merged_item = {
            **parsed,
            **item,
            'sector': parsed.get('sector') or parsed.get('industry') or item.get('sector') or '',
            'industry': parsed.get('industry') or parsed.get('sector') or item.get('industry') or '',
            'technical': parsed.get('technical') or item.get('technical') or '',
            'sentiment': parsed.get('sentiment') or item.get('sentiment') or '',
            'recommendation': parsed.get('recommendation') or item.get('recommendation') or '',
            'quant_models': parsed.get('quant_models') or item.get('quant_models') or [],
            'fields': base_fields,
            'reason': parsed.get('reason') or item.get('reason') or '',
            'summary': parsed.get('summary') or item.get('summary') or '',
        }
        merged.append(merged_item)
    return sorted(
        merged,
        key=lambda item: _safe_float(item.get('score'), 0.0) or 0.0,
        reverse=True,
    )


def _hot_sector_stock_membership_index(hot_sector):
    snapshot = (hot_sector or {}).get('snapshot') or {}
    sid = snapshot.get('id')
    if not sid:
        return {}
    try:
        from data_store import hot_sector_repo
        boards = {
            str(board.get('board_code') or ''): board
            for board in ((hot_sector or {}).get('boards') or hot_sector_repo.boards_for_snapshot(int(sid)))
            if board.get('board_code')
        }
        relation_rows = hot_sector_repo.relations_for_snapshot(int(sid), limit=100000)
        relations_by_pair = defaultdict(list)
        for rel in relation_rows:
            key = (_stock_code_key(rel.get('code')), str(rel.get('board_code') or ''))
            relations_by_pair[key].append(rel)
        by_code = defaultdict(list)
        for row in hot_sector_repo.stocks_for_snapshot(int(sid), limit=100000):
            code = _stock_code_key(row.get('code'))
            board_code = str(row.get('board_code') or '')
            if not code or not board_code:
                continue
            board = boards.get(board_code) or {}
            rels = relations_by_pair.get((code, board_code), [])
            lhb_hit = bool(row.get('lhb_trade_date')) or any(
                str(rel.get('relation_type') or '').lower() in {'dragon_tiger', 'lhb'}
                for rel in rels
            )
            by_code[code].append({
                'snapshot_id': int(sid),
                'board_code': board_code,
                'board_name': board.get('board_name') or board_code,
                'board_type': board.get('board_type') or '',
                'board_rank': _safe_int(board.get('board_rank'), None, minimum=1),
                'stock_rank': _safe_int(row.get('stock_rank'), None, minimum=1),
                'candidate_rank': _safe_int(row.get('candidate_rank'), None, minimum=1),
                'main_net_inflow': _safe_float(row.get('main_net_inflow'), None),
                'main_net_inflow_text': row.get('main_net_inflow_text') or '',
                'change_pct': _safe_float(row.get('change_pct'), None),
                'lhb_trade_date': row.get('lhb_trade_date') or '',
                'lhb_buy_amount': _safe_float(row.get('lhb_buy_amount'), None),
                'lhb_sell_amount': _safe_float(row.get('lhb_sell_amount'), None),
                'lhb_net_amount': _safe_float(row.get('lhb_net_amount'), None),
                'lhb_reason': row.get('lhb_reason') or '',
                'lhb_hit': lhb_hit,
                'relation_count': len(rels),
                'relation_types': sorted({
                    str(rel.get('relation_type') or '')
                    for rel in rels
                    if rel.get('relation_type')
                }),
            })
        for rows in by_code.values():
            rows.sort(key=lambda item: (
                item.get('board_rank') or 9999,
                item.get('stock_rank') or 9999,
                item.get('board_name') or '',
            ))
        return dict(by_code)
    except Exception as exc:
        logger.debug(f"加载热门板块成分股关联失败: {exc}")
        return {}


def fetch_board_constituents(board_code, limit=60):
    """东财板块成分股实时快照(clist ``fs=b:BKxxxx``)。

    仅对东财 ``BK`` 板块码有效;非 BK(如 Tushare ``.DC`` 兜底码)直接返回 ``[]``,
    由调用方降级 —— 不硬拼 ``fs``。返回 fetch_eastmoney_clist 原始行
    (``code/name/price/change_pct/main_net_inflow/main_net_inflow_text/...``)。
    本机/网络被掐时会抛错或返回空,由调用方兜底。
    """
    code = str(board_code or '').strip().upper()
    if not code.startswith('BK'):
        return []
    return MARKET_INTELLIGENCE_SERVICE.fetch_eastmoney_clist(
        fs=f'b:{code}', fid='f3', limit=int(limit or 60))


def _board_stock_row(s):
    """成分股行统一精简为弹窗所需字段(东财主路/星轨兜底同形)。"""
    return {
        'code': s.get('code'),
        'name': s.get('name'),
        'price': s.get('price'),
        'change_pct': s.get('change_pct'),
        'main_net_inflow': s.get('main_net_inflow'),
        'main_net_inflow_text': s.get('main_net_inflow_text') or '',
    }


def board_stocks_payload(code, name='', limit=60):
    """板块成分股弹窗 payload(行情台/行业热力/画布板块芯片 → 成分股列表)。

    东财 clist 主路失败/空(BK 码)→ 复用星轨成分股三层兜底
    :func:`star_orbit_service.board_constituents`(东财双路由 → Tushare ``dc_member``
    EOD 归属 + 实时报价叠加 → 本地关联缓存);兜底命中 → ``degraded=False`` 并透传
    ``note/source/stale/quoted/as_of``。仍全空/非东财来源 → ``degraded=True`` +
    ``note``,``stocks=[]``,不报错。
    """
    code = str(code or '').strip()
    name = str(name or '').strip()
    stocks = []
    try:
        rows = fetch_board_constituents(code, limit=limit) if code else []
    except Exception as exc:
        logger.debug(f"拉取板块成分股失败 {code}: {exc}")
        rows = []
    stocks = [_board_stock_row(s) for s in rows]
    note = ''
    extra = {}
    if not stocks and code.upper().startswith('BK'):
        # 东财主路挂了(网络/限流)→ 星轨三层兜底(东财直连↔默认路由 → Tushare
        # dc_member → star_orbit_board_member 缓存),命中则叠加实时报价后同形返回。
        try:
            from webui.services import star_orbit_service
            fb = star_orbit_service.board_constituents(code, name, limit=limit) or {}
        except Exception as exc:
            logger.debug(f"星轨兜底拉板块成分股失败 {code}: {exc}")
            fb = {}
        stocks = [_board_stock_row(s) for s in (fb.get('stocks') or [])]
        if stocks:
            note = str(fb.get('note') or '')
            extra = {k: fb[k] for k in ('source', 'stale', 'quoted', 'as_of') if k in fb}
    degraded = not stocks
    if not stocks:
        if code and not code.upper().startswith('BK'):
            note = '该板块非东财来源,暂无实时成分股,请用外链查看。'
        else:
            note = '东财成分股暂不可用(网络/限流),请用外链查看或稍后重试。'
    payload = {
        'board': {'code': code, 'name': name},
        'stocks': stocks,
        'count': len(stocks),
        'degraded': degraded,
        'note': note,
    }
    payload.update(extra)
    return payload


def _overlay_board_realtime(stocks, board_code=None):
    """读时叠加实时报价(价/涨跌/主力净流入),修复快照内成分股字段为空。

    数据源走 :func:`star_orbit_service._quote_overlay` —— 东财 ``ulist.np``(全字段,
    绕系统代理)→ 腾讯 ``qt.gtimg.cn``(仅价/涨跌)→ Tushare ``moneyflow_dc`` 多源兜底,
    按 6 位代码匹配。相比旧实现走 ``push2`` 的 ``clist``(本机/限流时常 http000 全空),
    这条链本机即便 push2 被掐也可达。只填补缺失/为空字段,不覆盖快照已记录的非空值;
    拉取失败/无匹配 → 原样返回(降级)。``board_code`` 仅用于日志。
    """
    if not stocks:
        return stocks
    codes = [c for c in (_stock_code_key((s if isinstance(s, dict) else dict(s)).get('code'))
                         for s in stocks) if c]
    if not codes:
        return stocks
    try:
        from webui.services import star_orbit_service
        quotes = star_orbit_service._quote_overlay(codes)
    except Exception as exc:
        logger.debug(f"成分股实时叠加失败 {board_code}: {exc}")
        quotes = {}
    out = []
    for s in stocks:
        row = s if isinstance(s, dict) else dict(s)
        quote = quotes.get(_stock_code_key(row.get('code'))) if quotes else None
        if quote:
            if row.get('change_pct') in (None, 0, 0.0) and quote.get('change_pct') is not None:
                row['change_pct'] = quote['change_pct']
            if row.get('price') in (None, 0, 0.0) and quote.get('price') is not None:
                row['price'] = quote['price']
            if row.get('main_net_inflow') in (None, 0, 0.0) and quote.get('main_net_inflow') is not None:
                row['main_net_inflow'] = quote['main_net_inflow']
        # 主力净流入有数值时清掉快照里的占位符文案('—'/'--'),交给前端按数值统一格式化
        # ——否则 ``text || format(number)`` 中真truthy的 '—' 会盖掉刚叠加上的数值。
        if row.get('main_net_inflow') not in (None, 0, 0.0):
            if str(row.get('main_net_inflow_text') or '').strip() in ('', '—', '--'):
                row['main_net_inflow_text'] = ''
        out.append(row)
    return out


# 选股来源(screening source)的中文标签。这些是「为什么这只股票进入候选池」的来源标记,
# 不是真正的行业/板块名称;当个股缺少 sector/industry 又没有热门板块归属时,画布过去会
# 直接把英文 source key(如 capital_flow_in)当板块名展示。改为映射成可读中文,未知来源
# 统一归到「未识别板块」,避免内部键名泄漏到 UI。
_CANVAS_SOURCE_LABELS = {
    'capital_flow_in': '资金流入候选',
    'capital_flow_out': '资金流出候选',
    'dragon_tiger': '龙虎榜候选',
    'oversold_rebound': '超跌反弹候选',
    'heat': '热门股候选',
    'moneyflow_dc': '资金流候选',
    'eastmoney_enhanced': '东财候选',
    'tonghuashun': '同花顺候选',
}


def _canvas_sector_fallback(source):
    """Map an internal screening-source key to a readable Chinese label.

    Unknown / empty sources collapse to '未识别板块' so raw English keys never
    surface as board names on the canvas.
    """
    key = str(source or '').strip()
    if not key:
        return '未识别板块'
    return _CANVAS_SOURCE_LABELS.get(key, '未识别板块')


def _prepare_opportunity_canvas_items(items, hot_sector=None):
    """Normalize, rank and enrich opportunity items before canvas construction."""
    membership_index = _hot_sector_stock_membership_index(hot_sector)
    prepared = []
    for raw in sorted(
        [item for item in (items or []) if isinstance(item, dict)],
        key=lambda item: _safe_float(item.get('score'), 0.0) or 0.0,
        reverse=True,
    ):
        item = dict(raw)
        code = _stock_code_key(item.get('stock_code') or item.get('code'))
        memberships = [dict(row) for row in membership_index.get(code, [])]
        sector = (item.get('sector') or item.get('industry') or '').strip()
        if not sector and memberships:
            sector = memberships[0].get('board_name') or ''
        if not sector:
            sector = _canvas_sector_fallback(item.get('source'))
        item.update({
            'code': code,
            'stock_code': code,
            'sector': sector,
            'industry': item.get('industry') or sector,
            'hot_sector_memberships': memberships,
            'hot_sector_count': len(memberships),
            'hot_sector_rank_summary': '；'.join(
                f"{m.get('board_name') or m.get('board_code')} 板块#{m.get('board_rank') or '--'} / 成分股#{m.get('stock_rank') or '--'}"
                for m in memberships[:4]
            ),
            'lhb_hit': any(m.get('lhb_hit') for m in memberships),
            'lhb_relation_count': sum(int(m.get('relation_count') or 0) for m in memberships),
        })
        prepared.append(item)

    for rank, item in enumerate(prepared, start=1):
        item['score_rank'] = rank
        item.setdefault('report_rank', item.get('rank') or rank)

    by_sector = defaultdict(list)
    for item in prepared:
        by_sector[item.get('sector') or '未识别板块'].append(item)
    for sector_items in by_sector.values():
        sector_items.sort(key=lambda item: _safe_float(item.get('score'), 0.0) or 0.0, reverse=True)
        for rank, item in enumerate(sector_items, start=1):
            item['sector_rank'] = rank
            item['sector_peer_count'] = len(sector_items)
    return prepared


def _excel_scalar(value):
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return value


# 评分分项 / 风险信号 字段的中文列名（与 _format_score_parts/_format_signal_parts 对齐）
_SCORE_PART_COLUMNS = {
    'sector': '板块分',
    'technical': '技术分',
    'quantitative': '量化分',
    'fundamental': '基本面分',
    'sentiment': '情绪分',
    'news': '消息分',
    'event': '事件分',
    'events': '事件分',
    'moneyflow': '资金分',
    'momentum': '动量分',
    'volume_health': '量能分',
    'liquidity': '流动性分',
    'dragon_tiger': '龙虎榜分',
}
# 去重后的评分分项列顺序（event/events 同名只保留一列）。
_SCORE_PART_COLUMN_ORDER = list(dict.fromkeys(_SCORE_PART_COLUMNS.values()))
_SIGNAL_COLUMNS = {
    'chase': '追高风险',
    'rsi': 'RSI',
    'day_change': '当日涨幅%',
    'change_3d': '3日涨幅%',
    'change_5d': '5日涨幅%',
    'sell_signals': '卖出信号',
    'quant_score': '量化总分',
}
# 全量明细表中完整文本字段的列顺序（与报告 【…】 区块一致）。
OPPORTUNITY_DETAIL_FIELD_ORDER = [
    '概览', '入选原因', '涨幅', '板块', '量化', '技术', '基本面',
    '情绪资金', '消息', '关键加减分', '最新动态', '高级', '历史重复入选',
]


def _write_excel_sheet(writer, sheet_name, rows, columns=None, style=None):
    """Write rows (list of dicts) to a worksheet without crashing on empty data.

    pandas 3.0 removed ``DataFrame.applymap``; this uses ``DataFrame.map`` (added in
    pandas 2.1, and the project pins ``pandas>=2.2.3``) to JSON-serialise non-scalar
    cells. When ``columns`` is given it becomes the exact leading schema: requested
    columns always appear (missing ones are added empty) so every export has a stable
    column set, and any extra keys are appended after them so nothing is silently
    dropped. An empty sheet still writes its header row. When ``style`` (a declarative
    spec from ``opportunity_excel``) is given, ``_style_worksheet`` renders it.
    """
    df = pd.DataFrame(rows or [])
    if columns:
        extras = [c for c in df.columns if c not in columns]
        df = df.reindex(columns=list(columns) + extras)
    if not df.empty:
        df = df.map(_excel_scalar)
    df.to_excel(writer, sheet_name=sheet_name, index=False)
    if style:
        _style_worksheet(writer.sheets[sheet_name], style)


# 着色调色板（CN 习惯：红涨绿跌；评分蓝深→灰浅；风险越危险越橙红；建议买入红/观望黄/回避灰）。
_XL_FILLS = {
    'red': 'FFF4CCCC', 'green': 'FFD9EAD3', 'yellow': 'FFFFF2CC', 'gray': 'FFEFEFEF',
    'orange': 'FFFCE5CD', 'deep_blue': 'FFC9DAF8', 'blue': 'FFD0E2F3', 'light_blue': 'FFE8F0FB',
}


def _xl_num(value):
    """Coerce a cell value to float for threshold colouring, else None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _xl_rule_color(rule, value):
    """Map a declarative color rule + cell value to a palette key (or None)."""
    kind = rule.get('kind')
    if kind == 'updown':
        v = _xl_num(value)
        if v is None or v == 0:
            return None
        return 'red' if v > 0 else 'green'
    if kind == 'score':
        v = _xl_num(value)
        if v is None:
            return None
        if v >= 85:
            return 'deep_blue'
        if v >= 78:
            return 'blue'
        if v >= 70:
            return 'light_blue'
        return 'gray'
    if kind == 'action':
        s = str(value or '')
        if s in ('买入', '分批建仓'):
            return 'red'
        if s in ('观望', '分批止盈'):
            return 'yellow'
        if s == '卖出':
            return 'green'
        if s in ('回避', '止损', '数据不足'):
            return 'gray'
        return None
    if kind == 'risk':
        v = _xl_num(value)
        if v is None:
            return None
        if rule.get('metric') == 'drawdown':
            if v <= -20:
                return 'red'
            if v <= -15:
                return 'orange'
        return None
    return None


def _style_worksheet(ws, style_spec):
    """Apply a declarative style spec (header/freeze/number-format/colour/width) in place."""
    if not style_spec:
        return
    from openpyxl.styles import PatternFill, Font
    from openpyxl.utils import get_column_letter

    max_row = ws.max_row or 1
    header = [c.value for c in ws[1]] if max_row >= 1 else []
    col_index = {name: i + 1 for i, name in enumerate(header)}

    if style_spec.get('header_bold'):
        head_fill = PatternFill('solid', fgColor='FFF2F2F2')
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.fill = head_fill

    if style_spec.get('freeze_panes'):
        ws.freeze_panes = style_spec['freeze_panes']

    for col in style_spec.get('percent_columns', []):
        idx = col_index.get(col)
        if idx:
            for row in range(2, max_row + 1):
                ws.cell(row=row, column=idx).number_format = '0.0"%"'
    for col in style_spec.get('price_columns', []):
        idx = col_index.get(col)
        if idx:
            for row in range(2, max_row + 1):
                ws.cell(row=row, column=idx).number_format = '#,##0.00'

    fill_cache = {}
    for rule in style_spec.get('color_rules', []):
        idx = col_index.get(rule.get('column'))
        if not idx:
            continue
        for row in range(2, max_row + 1):
            cell = ws.cell(row=row, column=idx)
            key = _xl_rule_color(rule, cell.value)
            if not key:
                continue
            fill = fill_cache.get(key)
            if fill is None:
                fill = fill_cache[key] = PatternFill('solid', fgColor=_XL_FILLS[key])
            cell.fill = fill

    # 列宽按内容估算并设上限（中文按 ~2 宽计）。
    for i, name in enumerate(header, start=1):
        width = len(str(name or '')) + 2
        for row in range(2, min(max_row, 200) + 1):
            val = ws.cell(row=row, column=i).value
            if val is not None:
                cells = sum(2 if ord(ch) > 127 else 1 for ch in str(val))
                width = max(width, cells + 1)
        ws.column_dimensions[get_column_letter(i)].width = min(width, 42)



def _parse_change_breakdown(text):
    """从「当日:-2.91%，3日:+13.71%，5日:+4.24%」文本解析结构化涨幅。"""
    out = {}
    for cn, key in (('当日', '当日涨幅'), ('3日', '3日涨幅'), ('5日', '5日涨幅')):
        match = re.search(rf'{cn}\s*[:：]\s*([+-]?\d+(?:\.\d+)?%?)', text or '')
        if match:
            out[key] = match.group(1)
    return out


def _stock_change_columns(node, analysis_map):
    """优先用 run-store 数值信号，缺失时回退到 涨幅 文本解析。"""
    signals = node.get('signals') if isinstance(node.get('signals'), dict) else {}
    parsed = _parse_change_breakdown(analysis_map.get('涨幅', ''))

    def pick(signal_key, parsed_key):
        value = signals.get(signal_key)
        if value not in (None, ''):
            return value
        return parsed.get(parsed_key)

    return {
        '当日涨幅': pick('day_change', '当日涨幅'),
        '3日涨幅': pick('change_3d', '3日涨幅'),
        '5日涨幅': pick('change_5d', '5日涨幅'),
    }


def _expand_labeled_dict(raw, label_map):
    """把 {'sector': 53, ...} 展开成 {'板块分': 53, ...}（保留未知键的原名）。"""
    out = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            out[label_map.get(str(key), str(key))] = value
    return out


def export_opportunity_canvas_excel():
    """Export the current opportunity canvas even when no hot-sector snapshot exists."""
    opportunity = _load_latest_opportunities()
    canvas = opportunity.get('canvas') or _empty_opportunity_canvas()
    latest_report = opportunity.get('latest_report') or {}
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    path = RESULTS_DIR / f"opportunity_canvas_{timestamp}.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)

    node_rows = []
    stock_rows = []
    stock_detail_rows = []
    analysis_rows = []
    membership_rows = []
    for node in canvas.get('nodes') or []:
        node_rows.append({
            'id': node.get('id'),
            'type': node.get('type'),
            'title': node.get('title'),
            'subtitle': node.get('subtitle'),
            'stock_code': node.get('stock_code'),
            'stock_name': node.get('stock_name'),
            'sector': node.get('sector'),
            'score': node.get('score'),
            'score_rank': node.get('score_rank'),
            'sector_rank': node.get('sector_rank'),
            'board_code': node.get('board_code'),
            'board_rank': node.get('board_rank'),
            'detail': node.get('detail'),
            'tags': '、'.join(str(tag) for tag in node.get('tags') or []),
        })
        if node.get('type') == 'stock':
            analysis_map = {
                str(sec.get('label')): sec.get('value')
                for sec in node.get('analysis') or []
                if isinstance(sec, dict) and sec.get('label')
            }
            change_cols = _stock_change_columns(node, analysis_map)
            score_cols = _expand_labeled_dict(node.get('score_breakdown'), _SCORE_PART_COLUMNS)
            signal_cols = _expand_labeled_dict(node.get('signals'), _SIGNAL_COLUMNS)
            key_signal_cols = {k: signal_cols.get(k) for k in ('追高风险', 'RSI', '卖出信号', '量化总分')}
            base_cols = {
                '全量排名': node.get('score_rank') or node.get('report_rank'),
                '报告排名': node.get('report_rank'),
                '代码': node.get('stock_code'),
                '名称': node.get('stock_name'),
                '所属板块': node.get('sector'),
                '板块内排名': node.get('sector_rank'),
                '板块候选数': node.get('sector_peer_count'),
                '综合评分': node.get('score'),
                '评级': node.get('rating'),
                '候选来源': node.get('source'),
                '来源细节': node.get('source_detail'),
            }
            hot_cols = {
                '相关热门板块数': node.get('hot_sector_count'),
                '热门板块排名摘要': node.get('hot_sector_rank_summary'),
                '龙虎榜命中': '是' if node.get('lhb_hit') else '否',
                '龙虎榜关系数': node.get('lhb_relation_count'),
                '是否降级': '是' if node.get('degraded') else '否',
            }
            # 排名表:基础 + 涨幅 + 评分分项 + 关键信号 + 热门板块
            stock_rows.append({**base_cols, **change_cols, **score_cols, **key_signal_cols, **hot_cols})
            # 全量明细表:排名表所有列 + 量化模型 + 每个分析字段完整文本 + 分析摘要
            detail_row = {**base_cols, **change_cols, **score_cols, **key_signal_cols, **hot_cols}
            detail_row['量化模型'] = '、'.join(str(m) for m in node.get('quant_models') or [])
            for label in OPPORTUNITY_DETAIL_FIELD_ORDER:
                if label in analysis_map:
                    detail_row[label] = analysis_map[label]
            for label, value in analysis_map.items():
                if label not in detail_row:
                    detail_row[label] = value
            detail_row['分析摘要'] = node.get('detail')
            stock_detail_rows.append(detail_row)
            for section in node.get('analysis') or []:
                analysis_rows.append({
                    '代码': node.get('stock_code'),
                    '名称': node.get('stock_name'),
                    '全量排名': node.get('score_rank') or node.get('report_rank'),
                    '字段': section.get('label'),
                    '内容': section.get('value'),
                })
            for membership in node.get('hot_sector_memberships') or []:
                membership_rows.append({
                    '代码': node.get('stock_code'),
                    '名称': node.get('stock_name'),
                    '热门板块代码': membership.get('board_code'),
                    '热门板块名称': membership.get('board_name'),
                    '板块排名': membership.get('board_rank'),
                    '成分股排名': membership.get('stock_rank'),
                    '主力净流入': membership.get('main_net_inflow'),
                    '主力净流入文本': membership.get('main_net_inflow_text'),
                    '涨跌幅': membership.get('change_pct'),
                    '龙虎榜命中': '是' if membership.get('lhb_hit') else '否',
                    '龙虎榜日期': membership.get('lhb_trade_date'),
                    '龙虎榜买入额': membership.get('lhb_buy_amount'),
                    '龙虎榜净额': membership.get('lhb_net_amount'),
                    '龙虎榜原因': membership.get('lhb_reason'),
                    '关联关系数': membership.get('relation_count'),
                    '关系类型': '、'.join(membership.get('relation_types') or []),
                })

    edge_rows = [
        {
            'from': edge.get('from'),
            'to': edge.get('to'),
            'relation': edge.get('relation') or '',
        }
        for edge in canvas.get('edges') or []
    ]
    sector_rows = [
        {
            'id': node.get('id'),
            '名称': node.get('title'),
            '类型': node.get('type'),
            '股票数': node.get('count') or node.get('stock_count'),
            '最高评分': node.get('top_score'),
            '板块排名': node.get('board_rank'),
            '主力净流入': node.get('main_net_inflow'),
            '龙虎榜关系': node.get('relation_count'),
        }
        for node in canvas.get('nodes') or []
        if node.get('type') in {'sector', 'hot_board', 'hot_sector'}
    ]

    # 按需复算：对每只入选股票调用个股分析同款引擎（5 分钟 LRU），逐股 best-effort。
    # 复算失败/超预算/超上限均回退报告文本基线，不让导出崩溃（spec §2/§7）。
    from webui import opportunity_excel
    try:
        suite_limit = int(os.environ.get('KRONOS_OPP_EXCEL_SUITE_LIMIT', '30'))
    except (TypeError, ValueError):
        suite_limit = 30
    suite_sheets, suite_coverage = opportunity_excel.build_suite_sheets(
        canvas,
        suite_fetcher=lambda code: STOCK_SUITE_SERVICE.get_suite(code or ''),
        suite_limit=suite_limit,
    )

    run_meta = latest_report.get('run_meta') if isinstance(latest_report.get('run_meta'), dict) else {}
    total_suite = max(suite_coverage.get('total_stocks', 0), 1)
    report_rows = [{
        '报告文件': latest_report.get('file'),
        '报告URL': latest_report.get('url'),
        '更新时间': latest_report.get('updated_at'),
        '市场环境': opportunity.get('market_env'),
        '股票数': len(stock_rows),
        '板块数': (canvas.get('stats') or {}).get('sectors'),
        '分析节点数': (canvas.get('stats') or {}).get('analysis'),
        '降级股票数': sum(1 for row in stock_detail_rows if row.get('是否降级') == '是'),
        '龙虎榜命中数': sum(1 for row in stock_detail_rows if row.get('龙虎榜命中') == '是'),
        '热门板块关联数': len(membership_rows),
        '复算成功数': suite_coverage.get('succeeded'),
        '复算失败数': suite_coverage.get('failed'),
        '超预算跳过数': suite_coverage.get('skipped_over_budget'),
        '超上限跳过数': suite_coverage.get('skipped_over_limit'),
        '复算覆盖率': f"{suite_coverage.get('succeeded', 0) / total_suite * 100:.0f}%",
        '规则版本': run_meta.get('ruleset_version'),
        '配置哈希': run_meta.get('config_hash'),
        'run_id': latest_report.get('run_id'),
        'run_item_count': latest_report.get('run_item_count'),
        '导出时间': timestamp,
    }]

    # 排名/明细两张表共用的列顺序:基础 → 涨幅 → 评分分项 → 关键信号 → 热门板块
    stock_rank_columns = (
        ['全量排名', '报告排名', '代码', '名称', '所属板块', '板块内排名', '板块候选数',
         '综合评分', '评级', '候选来源', '来源细节', '当日涨幅', '3日涨幅', '5日涨幅']
        + _SCORE_PART_COLUMN_ORDER
        + ['追高风险', 'RSI', '卖出信号', '量化总分',
           '相关热门板块数', '热门板块排名摘要', '龙虎榜命中', '龙虎榜关系数', '是否降级']
    )
    stock_detail_columns = stock_rank_columns + ['量化模型'] + OPPORTUNITY_DETAIL_FIELD_ORDER + ['分析摘要']

    # 写入顺序即阅读动线（spec §4）：投资速览 → 报告信息(+覆盖率) → 雷达/风控/量化/筹码
    # → 原有排名/明细/分析/板块/热门/画布 → 术语表与图例。新增 6 张 sheet 自带样式规格。
    suite_by_name = {s['name']: s for s in suite_sheets}

    def _write_suite(name):
        spec = suite_by_name.get(name)
        if spec:
            _write_excel_sheet(writer, name, spec['rows'], columns=spec['columns'],
                               style=spec.get('style'))

    with pd.ExcelWriter(path, engine='openpyxl') as writer:
        _write_suite('投资速览')
        _write_excel_sheet(writer, '报告信息', report_rows)
        _write_suite('评分雷达')
        _write_suite('风控执行计划')
        _write_suite('量化模型矩阵')
        _write_suite('筹码与机构')
        _write_excel_sheet(writer, '股票排名', stock_rows, columns=stock_rank_columns)
        _write_excel_sheet(writer, '股票全量明细', stock_detail_rows, columns=stock_detail_columns)
        _write_excel_sheet(writer, '分析内容', analysis_rows)
        _write_excel_sheet(writer, '板块汇总', sector_rows)
        _write_excel_sheet(writer, '热门板块关联', membership_rows, columns=[
            '代码', '名称', '热门板块代码', '热门板块名称', '板块排名', '成分股排名',
            '主力净流入', '主力净流入文本', '涨跌幅', '龙虎榜命中', '龙虎榜日期',
            '龙虎榜买入额', '龙虎榜净额', '龙虎榜原因', '关联关系数', '关系类型',
        ])
        _write_excel_sheet(writer, '画布节点', node_rows)
        _write_excel_sheet(writer, '画布关系', edge_rows)
        _write_suite('术语表与图例')

    return {
        'file': path.name,
        'path': str(path),
        'url': _report_url(path),
    }


def _build_opportunity_canvas(items, latest_report=None, market_env='', hot_sector=None):
    """Build a report -> sector -> stock -> tag graph for the desktop canvas."""
    items = _prepare_opportunity_canvas_items(items, hot_sector)
    if not items:
        canvas = _empty_opportunity_canvas('暂无可展示的投资机会分析内容')
        if hot_sector:
            _append_hot_sector_canvas_nodes(canvas, hot_sector)
        return canvas

    nodes = [{
        'id': 'root',
        'type': 'root',
        'level': 0,
        'title': '投资机会分析',
        'subtitle': (latest_report or {}).get('file') or '最新报告',
        'detail': market_env or '已解析最新机会挖掘报告',
        'tags': ['报告', f"{len(items)}只股票"],
    }]
    edges = []
    sector_map = {}
    sector_stats = {}

    for item in items:
        sector = (item.get('sector') or item.get('industry') or '未识别板块').strip()
        stat = sector_stats.setdefault(sector, {'count': 0, 'top_score': 0.0})
        stat['count'] += 1
        stat['top_score'] = max(stat['top_score'], _safe_float(item.get('score'), 0.0) or 0.0)

    sorted_sectors = sorted(
        sector_stats.items(),
        key=lambda kv: (-kv[1]['top_score'], kv[0]),
    )
    for sector_index, (sector, stat) in enumerate(sorted_sectors, start=1):
        sector_id = f"sector-{sector_index}"
        sector_map[sector] = sector_id
        nodes.append({
            'id': sector_id,
            'type': 'sector',
            'level': 1,
            'title': sector,
            'subtitle': f"{stat['count']}只股票 · 最高{stat['top_score']:.2f}",
            'detail': f"{sector}板块共入选{stat['count']}只股票，最高评分{stat['top_score']:.2f}。",
            'tags': ['板块', f"Top{sector_index}"],
            'sector': sector,
            'count': stat['count'],
            'top_score': round(stat['top_score'], 2),
        })
        edges.append({'from': 'root', 'to': sector_id})

    tag_count = 0
    stock_hot_edges = []
    for stock_index, item in enumerate(items, start=1):
        code = item.get('stock_code') or item.get('code') or ''
        name = item.get('stock_name') or item.get('name') or code or '股票'
        sector = (item.get('sector') or item.get('industry') or '未识别板块').strip()
        sector_id = sector_map.get(sector)
        stock_id = f"stock-{stock_index}-{code or stock_index}"
        score = _safe_float(item.get('score'), 0.0) or 0.0
        sections = _opportunity_tag_sections(item)
        report_rank = _safe_int(item.get('report_rank') or item.get('rank'), stock_index, minimum=1) or stock_index
        score_rank = _safe_int(item.get('score_rank'), stock_index, minimum=1) or stock_index
        sector_rank = _safe_int(item.get('sector_rank'), None, minimum=1)
        sector_peer_count = _safe_int(item.get('sector_peer_count'), None, minimum=1)
        memberships = item.get('hot_sector_memberships') or []
        hot_sector_tags = []
        if memberships:
            first_membership = memberships[0]
            hot_sector_tags.append(
                f"{first_membership.get('board_name') or first_membership.get('board_code')}#{first_membership.get('stock_rank') or '--'}"
            )
        if item.get('lhb_hit'):
            hot_sector_tags.append('龙虎榜')
        nodes.append({
            'id': stock_id,
            'type': 'stock',
            'level': 2,
            'title': f"{name} {code}".strip(),
            'subtitle': f"全量#{score_rank} · 板块#{sector_rank or '--'} · {item.get('rating') or '评分'} · {score:.2f}",
            'detail': item.get('reason') or item.get('summary') or item.get('recommendation') or '',
            'tags': ([item.get('rating') or '评分', f"{score:.1f}", f"全量#{score_rank}"] + hot_sector_tags)[:5],
            'stock_code': code,
            'stock_name': name,
            'sector': sector,
            'score': round(score, 2),
            'rating': item.get('rating') or '',
            'report_rank': report_rank,
            'score_rank': score_rank,
            'sector_rank': sector_rank,
            'sector_peer_count': sector_peer_count,
            'source': item.get('source') or '',
            'source_detail': item.get('source_detail') or '',
            'degraded': bool(item.get('degraded')),
            'quant_models': item.get('quant_models') or [],
            'change_pct': item.get('change_pct'),
            'score_breakdown': item.get('score_breakdown') or {},
            'signals': item.get('signals') or {},
            'hot_sector_memberships': memberships,
            'hot_sector_count': len(memberships),
            'hot_sector_rank_summary': item.get('hot_sector_rank_summary') or '',
            'lhb_hit': bool(item.get('lhb_hit')),
            'lhb_relation_count': int(item.get('lhb_relation_count') or 0),
            'analysis': sections,
        })
        if sector_id:
            edges.append({'from': sector_id, 'to': stock_id})
        for membership in memberships[:10]:
            board_code = membership.get('board_code')
            if board_code:
                stock_hot_edges.append({
                    'from': stock_id,
                    'to': f"hot-board-{board_code}",
                    'relation': 'stock_hot_board',
                })

        for tag_index, section in enumerate(sections[:8], start=1):
            tag_count += 1
            tag_id = f"tag-{stock_index}-{tag_index}"
            nodes.append({
                'id': tag_id,
                'type': 'tag',
                'level': 3,
                'title': section['label'],
                'subtitle': section['summary'],
                'detail': section['value'],
                'tags': ['分析内容'],
                'stock_code': code,
                'stock_name': name,
                'sector': sector,
                'label': section['label'],
            })
            edges.append({'from': stock_id, 'to': tag_id})

    canvas = {
        'title': '投资机会分析',
        'subtitle': (latest_report or {}).get('file') or '最新报告',
        'levels': ['报告', '板块', '股票', '分析内容'],
        'views': _opportunity_canvas_views(),
        'nodes': nodes,
        'edges': edges,
        'stats': {
            'sectors': len(sorted_sectors),
            'stocks': len(items),
            'tags': tag_count,
            'analysis': tag_count,
        },
    }
    if hot_sector:
        _append_hot_sector_canvas_nodes(canvas, hot_sector)
        existing_ids = {node.get('id') for node in canvas.get('nodes') or []}
        for edge in stock_hot_edges:
            if edge['from'] in existing_ids and edge['to'] in existing_ids:
                canvas['edges'].append(edge)
        canvas['stats']['stock_hot_board_edges'] = len([
            edge for edge in stock_hot_edges
            if edge['from'] in existing_ids and edge['to'] in existing_ids
        ])
    return canvas


def _append_hot_sector_canvas_nodes(canvas, hot_sector):
    snapshot = (hot_sector or {}).get('snapshot') or {}
    boards = (hot_sector or {}).get('boards') or []
    if not snapshot:
        return canvas
    root_id = 'hot-sector-root'
    canvas.setdefault('nodes', []).append({
        'id': root_id,
        'type': 'hot_sector',
        'level': 1,
        'title': '热门板块全量',
        'subtitle': f"快照 {snapshot.get('id')} · {snapshot.get('stock_count') or 0}只成分股",
        'detail': '记录前十大热门板块下全部成分股排名、主力净流入和龙虎榜关联。',
        'tags': ['全量快照', f"{snapshot.get('relation_count') or 0}条关系"],
        'stock_count': int(snapshot.get('stock_count') or 0),
        'relation_count': int(snapshot.get('relation_count') or 0),
        'snapshot_id': snapshot.get('id'),
        'created_at': snapshot.get('created_at'),
        'drilldown': {
            'type': 'hot_sector_snapshot',
            'snapshot_id': snapshot.get('id'),
        },
    })
    canvas.setdefault('edges', []).append({'from': 'root', 'to': root_id})
    for index, board in enumerate(boards[:10], start=1):
        board_id = f"hot-board-{board.get('board_code') or index}"
        stock_count = int(board.get('stock_count') or 0)
        rel_count = int(board.get('relation_count') or 0)
        canvas['nodes'].append({
            'id': board_id,
            'type': 'hot_board',
            'level': 2,
            'title': board.get('board_name') or board.get('board_code') or '热门板块',
            'subtitle': f"#{board.get('board_rank') or index} · {stock_count}只 · 涨跌{board.get('change_pct') or 0}%",
            'detail': f"主力净流入 {board.get('main_net_inflow') or '—'}；龙虎榜关联 {rel_count} 条。",
            'tags': [board.get('board_type') or '板块', f"{stock_count}只"],
            'board_code': board.get('board_code'),
            'board_rank': board.get('board_rank') or index,
            'board_type': board.get('board_type'),
            'stock_count': stock_count,
            'relation_count': rel_count,
            'main_net_inflow': board.get('main_net_inflow'),
            'change_pct': board.get('change_pct'),
            'snapshot_id': snapshot.get('id'),
            'drilldown': {
                'type': 'hot_sector_stocks',
                'snapshot_id': snapshot.get('id'),
                'board_code': board.get('board_code'),
            },
        })
        canvas['edges'].append({'from': root_id, 'to': board_id})
    stats = canvas.setdefault('stats', {})
    stats['hot_sector_boards'] = len(boards)
    stats['hot_sector_stocks'] = int(snapshot.get('stock_count') or 0)
    stats['hot_sector_relations'] = int(snapshot.get('relation_count') or 0)
    return canvas


def _build_quant_model_summary(items):
    model_meta = {
        'RSI': ('量化', '超买超卖', '相对强弱指标触发的拐点或风险信号'),
        'MACD': ('量化', '趋势动能', 'MACD 金叉、背离或趋势确认信号'),
        'KDJ': ('量化', '短线拐点', 'KDJ 低位/高位拐点和短线动能信号'),
        '布林': ('量化', '波动突破', '布林带突破、收敛或回归信号'),
        '均线': ('技术', '趋势结构', '多周期均线趋势与支撑压力结构'),
    }
    summary = {}
    for item in items or []:
        for name in item.get('quant_models') or []:
            entry = summary.setdefault(name, {
                'name': name,
                'count': 0,
                'category': model_meta.get(name, ('量化', '信号确认', '来自机会挖掘报告的量化模型触发'))[0],
                'focus': model_meta.get(name, ('量化', '信号确认', '来自机会挖掘报告的量化模型触发'))[1],
                'description': model_meta.get(name, ('量化', '信号确认', '来自机会挖掘报告的量化模型触发'))[2],
                'examples': [],
            })
            entry['count'] += 1
            code = item.get('code') or item.get('stock_code') or ''
            # 收录全部触发该模型且有代码的个股(供桌面端「点击模型→相关个股」弹窗定位)；上限放宽避免大报告被截断。
            if code and len(entry['examples']) < 80:
                entry['examples'].append({
                    'code': code,
                    'name': item.get('name') or item.get('stock_name') or '',
                    'score': item.get('score'),
                    'sector': item.get('sector') or item.get('industry') or '',
                })
    return sorted(summary.values(), key=lambda item: item['count'], reverse=True)


def _report_date_label(filename):
    """从 opportunity_top10_YYYYMMDD_HHMMSS.md 提取「MM-DD」用于标注数据来源日期。"""
    match = re.search(r'_(\d{4})(\d{2})(\d{2})_', filename or '')
    if match:
        return f"{match.group(2)}-{match.group(3)}"
    return ''


def _fallback_quant_models(exclude_name=None, scan_limit=12):
    """扫描最近的机会报告，返回首个「确有量化模型信号」的 (quant_models, note)。

    最新报告若处于降级态(取数限流导致全部「模型[无]」)，量化模型摘要会为空。
    此时回退到最近一份有效报告填充该卡片，并以 note 标注数据来源日期，避免误导为今日数据。
    """
    for path in _latest_primary_opportunity_reports(limit=scan_limit):
        if exclude_name and path.name == exclude_name:
            continue
        try:
            parsed = _parse_opportunity_report(path)
        except Exception:
            continue
        models = _build_quant_model_summary(parsed['items'])
        if models:
            label = _report_date_label(path.name)
            note = f"今日挖掘暂无量化信号，下方为最近一次有效挖掘（{label}）的模型摘要" if label \
                else "今日挖掘暂无量化信号，下方为最近一次有效挖掘的模型摘要"
            return models, note
    return None


def _market_symbol_for_code(stock_code):
    code = str(stock_code or '').strip().zfill(6)
    if code.startswith(('43', '83', '87', '92')):
        return {'lower': 'bj', 'upper': 'BJ', 'code': code}
    if code.startswith(('6', '9')):
        return {'lower': 'sh', 'upper': 'SH', 'code': code}
    return {'lower': 'sz', 'upper': 'SZ', 'code': code}


def _xueqiu_symbol(stock_code):
    market = _market_symbol_for_code(stock_code)
    return f"{market['upper']}{market['code']}"


def _stock_external_links(stock_code, stock_name=''):
    market = _market_symbol_for_code(stock_code)
    code = market['code']
    symbol = _xueqiu_symbol(code)
    keyword = (stock_name or code).strip() or code
    encoded_keyword = urllib.parse.quote(keyword)
    encoded_code = urllib.parse.quote(code)
    return {
        'xueqiu_stock': f'https://xueqiu.com/S/{symbol}',
        'xueqiu_search': f'https://xueqiu.com/k?q={encoded_keyword}',
        'jiuyangongshe_home': 'https://www.jiuyangongshe.com/',
        'jiuyangongshe_search': f'https://www.jiuyangongshe.com/search?keyword={encoded_keyword}',
        'eastmoney_quote': f'https://quote.eastmoney.com/{market["lower"]}{code}.html',
        'eastmoney_guba': f'https://guba.eastmoney.com/list,{encoded_code}.html',
        'eastmoney_news_search': f'https://so.eastmoney.com/news/s?keyword={encoded_keyword}',
        'eastmoney_report_search': f'https://so.eastmoney.com/report/s?keyword={encoded_keyword}',
        'ths_news_search': f'https://news.10jqka.com.cn/tapp/search/index/?keyword={encoded_keyword}',
        'sina_quote': f'https://finance.sina.com.cn/realstock/company/{market["lower"]}{code}/nc.shtml',
    }


def _fetch_tencent_quote_on_demand(api_code):
    """按需拉取单只股票的腾讯实时行情(qt.gtimg.cn)。

    个股分析等场景的目标股票通常不在固定监控集(ALL_REAL_CODES)里,real_data_cache
    必然未命中,导致行情(价格/涨幅)显示「--」。这里复用大盘行情同款的 gtimg 接口与
    _parse_tencent_quote_line 解析,对任意 A 股代码即时取价。

    返回与 real_data_cache 同构的 dict(约定: 'change' 字段存的是涨跌幅%),失败返回 None。
    """
    if not api_code:
        return None
    try:
        content = request_text(
            f"http://qt.gtimg.cn/q={api_code}",
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=5,
            encoding='gbk',
            errors='ignore',
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"按需实时行情拉取失败 {api_code}: {exc}")
        return None
    for line in (content or '').strip().split(';'):
        _code, quote = _parse_tencent_quote_line(line)
        if quote and quote.get('available'):
            return {
                'name': quote.get('name'),
                'price': quote.get('price'),
                'change': quote.get('change_pct'),  # real_data_cache 约定: change 存涨跌幅%
                'volume': quote.get('volume'),
                'source': 'tencent',
                'updated_at': quote.get('updated_at'),
            }
    return None


def _latest_stock_quote(stock_code):
    code = str(stock_code or '').strip().zfill(6)
    api_code = f'{_market_symbol_for_code(code)["lower"]}{code}'
    quote = market_state.get('real_data_cache', {}).get(api_code) or {}
    if not quote:
        stock = next(
            (
                item for item in market_state.get('stocks', [])
                if str(item.get('code') or '').zfill(6) == code
            ),
            None,
        )
        if stock:
            quote = {
                'name': stock.get('name'),
                'price': stock.get('price'),
                'change': stock.get('change'),
                'volume': stock.get('volume'),
                'source': 'market_monitor',
            }
    if not quote:
        # 不在固定监控集 → 缓存未命中,按需直接取腾讯实时行情
        quote = _fetch_tencent_quote_on_demand(api_code) or {}
    if not quote:
        return None
    return {
        'code': code,
        'name': quote.get('name') or '',
        'price': round(_safe_float(quote.get('price'), 0.0), 2),
        'change_pct': round(_safe_float(quote.get('change'), 0.0), 2),
        'volume': _safe_float(quote.get('volume'), 0.0),
        'source': quote.get('source') or 'tencent',
        'updated_at': quote.get('updated_at') or _format_datetime(market_state.get('last_update')),
    }


def _stock_kline_summary(kline_payload):
    records = (kline_payload or {}).get('records') or []
    if not records:
        return {
            'available': False,
            'message': (kline_payload or {}).get('message') or '暂无K线数据',
        }
    first = records[0]
    last = records[-1]
    highs = [_safe_float(row.get('high'), None) for row in records]
    lows = [_safe_float(row.get('low'), None) for row in records]
    volumes = [_safe_float(row.get('volume'), 0.0) or 0.0 for row in records]
    highs = [value for value in highs if value is not None]
    lows = [value for value in lows if value is not None]
    first_close = _safe_float(first.get('close'), None)
    last_close = _safe_float(last.get('close'), None)
    change_pct = None
    if first_close and last_close is not None:
        change_pct = round((last_close / first_close - 1) * 100, 2)
    return {
        'available': True,
        'records': len(records),
        'source': (kline_payload or {}).get('source') or '--',
        'latest_date': last.get('date'),
        'latest_close': last_close,
        'latest_change_pct': _safe_float(last.get('pct_chg'), 0.0),
        'range_change_pct': change_pct,
        'range_high': max(highs) if highs else None,
        'range_low': min(lows) if lows else None,
        'avg_volume': round(sum(volumes) / len(volumes), 2) if volumes else None,
    }


def _report_matches_stock(report, stock_code, stock_name=''):
    code = str(stock_code or '').strip().zfill(6)
    name = _strip_markup(stock_name or '')
    text = ' '.join([
        str(report.get('file') or ''),
        str(report.get('type') or ''),
        str(report.get('url') or ''),
    ])
    if code in text or (name and name in text):
        return True
    path = report.get('_path')
    if not path:
        return False
    try:
        report_path = Path(path)
        if not report_path.is_file() or report_path.stat().st_size > 3 * 1024 * 1024:
            return False
        content = report_path.read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return False
    return code in content or (name and name in content)


def _load_stock_report_history(stock_code, stock_name='', limit=8):
    reports = []
    for report in _report_history_candidates(limit=40):
        if _report_matches_stock(report, stock_code, stock_name):
            reports.append({key: value for key, value in report.items() if key != '_path'})
        if len(reports) >= limit:
            break
    return reports


def _stock_row_code(row):
    for column in ['股票代码', 'stock_code', 'code', '代码', 'symbol', 'ts_code']:
        if column in row and row[column] not in (None, ''):
            codes = normalize_stock_codes([row[column]])
            if codes:
                return codes[0]
    for value in row.values():
        codes = normalize_stock_codes([value])
        if codes:
            return codes[0]
    return ''


def _stock_batch_fields(row):
    preferred = [
        '综合评分', '评级', '建议', '量化', '技术', '位置',
        '量价', '情绪', '板块', 'total_score', 'rating',
        'recommendation', 'score',
    ]
    fields = []
    seen = set()
    for column in preferred + list(row.keys()):
        if column in seen or column not in row:
            continue
        seen.add(column)
        value = row.get(column)
        if value is None or value == '':
            continue
        if isinstance(value, float) and math.isnan(value):
            continue
        fields.append({'label': str(column), 'value': str(value)})
        if len(fields) >= 12:
            break
    return fields


def _load_stock_batch_results(stock_code, limit=5):
    code = str(stock_code or '').strip().zfill(6)
    results = []
    for csv_path in _latest_files(RESULTS_DIR, 'batch_results_*.csv', limit=20):
        try:
            df = pd.read_csv(csv_path, dtype=str).fillna('')
        except Exception:
            continue
        for _, series in df.iterrows():
            row = series.to_dict()
            if _stock_row_code(row) != code:
                continue
            results.append({
                'file': csv_path.name,
                'updated_at': _format_datetime(csv_path.stat().st_mtime),
                'url': _report_url(csv_path),
                'fields': _stock_batch_fields(row),
            })
            break
        if len(results) >= limit:
            break
    return results


def _stock_social_sources(stock_code, stock_name=''):
    code = str(stock_code or '').strip().zfill(6)
    name = _strip_markup(stock_name or '')
    links = _stock_external_links(code, name)
    return [
        {
            'platform': '雪球',
            'title': f'{name or code} 雪球个股页',
            'summary': '社区讨论、组合关注、公告和行情聚合入口',
            'url': links['xueqiu_stock'],
            'status': 'external_link',
        },
        {
            'platform': '雪球',
            'title': f'{name or code} 雪球搜索',
            'summary': '按名称检索近期讨论和长文',
            'url': links['xueqiu_search'],
            'status': 'external_link',
        },
        {
            'platform': '韭研公社',
            'title': f'{name or code} 韭研公社检索',
            'summary': '主题投研、事件线索和热帖入口',
            'url': links['jiuyangongshe_search'],
            'status': 'external_link',
        },
        {
            'platform': '东方财富股吧',
            'title': f'{name or code} 股吧讨论',
            'summary': '散户讨论、异动解读和消息反馈',
            'url': links['eastmoney_guba'],
            'status': 'external_link',
        },
    ]


def _stock_news_sources(stock_code, stock_name='', limit=8):
    code = str(stock_code or '').strip().zfill(6)
    name = _strip_markup(stock_name or '')
    keywords = {code}
    if name:
        keywords.add(name)
    items = []

    def add_item(platform, title, summary='', url='', status='cached'):
        clean_title = _truncate_text(title, 120)
        if not clean_title:
            return
        key = (platform, clean_title, url)
        if any((row['platform'], row['title'], row.get('url', '')) == key for row in items):
            return
        items.append({
            'platform': platform,
            'title': clean_title,
            'summary': _truncate_text(summary, 160),
            'url': url,
            'status': status,
        })

    intelligence = _load_market_intelligence()
    for item in intelligence.get('jinshi') or []:
        text = ' '.join([str(item.get('title') or ''), str(item.get('source') or '')])
        if any(keyword and keyword in text for keyword in keywords):
            add_item(
                item.get('source') or '金十数据',
                item.get('title') or '',
                item.get('time') or '',
                item.get('url') or '',
            )

    for item in (intelligence.get('eastmoney') or {}).get('hot_stocks') or []:
        row_code = str(item.get('code') or '').zfill(6)
        if row_code == code or (name and name == str(item.get('name') or '')):
            add_item(
                '东方财富',
                f"{item.get('name') or code} 资金热度",
                f"涨跌幅 {item.get('change_pct', '--')}% · 主力净流入 {item.get('main_net_inflow_text', '--')}",
                _stock_external_links(code, name)['eastmoney_quote'],
            )

    opportunity = _load_latest_opportunities()
    match = next(
        (
            item for item in opportunity.get('items', [])
            if str(item.get('stock_code') or item.get('code') or '').zfill(6) == code
        ),
        None,
    )
    if match:
        add_item(
            '本地机会报告',
            match.get('reason') or match.get('summary') or f'{name or code} 入选机会榜',
            f"{match.get('rating') or '--'} · 评分 {match.get('score', '--')} · {match.get('recommendation') or ''}",
            (opportunity.get('latest_report') or {}).get('url') or '',
        )

    links = _stock_external_links(code, name)
    fallback_links = [
        ('东方财富新闻', f'{name or code} 新闻搜索', '个股新闻、公告和异动解读检索入口', links['eastmoney_news_search']),
        ('东方财富研报', f'{name or code} 研报搜索', '券商研报、评级变化和深度研究检索入口', links['eastmoney_report_search']),
        ('同花顺资讯', f'{name or code} 同花顺资讯', '同花顺新闻、公告和互动信息检索入口', links['ths_news_search']),
    ]
    for platform, title, summary, url in fallback_links:
        if len(items) >= limit:
            break
        add_item(platform, title, summary, url, status='external_link')

    return items[:limit]


def _stock_context_payload(stock_code, stock_name=''):
    codes = normalize_stock_codes([stock_code])
    if not codes:
        return None, 'Invalid stock code'
    code = codes[0]
    opportunity = _load_latest_opportunities()
    opportunity_match = next(
        (
            item for item in opportunity.get('items', [])
            if str(item.get('stock_code') or item.get('code') or '').zfill(6) == code
        ),
        None,
    )
    # 行情按需取一次复用(缓存未命中会触发一次网络请求,避免名称兜底与 quote 字段各拉一次)
    latest_quote = _latest_stock_quote(code)
    resolved_name = (
        stock_name
        or (opportunity_match or {}).get('stock_name')
        or (opportunity_match or {}).get('name')
        or (latest_quote or {}).get('name')
        or ''
    )
    sector = (
        (opportunity_match or {}).get('sector')
        or (opportunity_match or {}).get('industry')
        or ''
    )
    # 标题副行需要「股票名称 · 关联板块」。机会报告未命中该股时 name/sector 全空、且从无 boards 字段，
    # 副行便退化成「代码 · --」。这里回退到 sector_api 补齐：get_stock_sector_info 先命中本地 Tushare
    # 行业缓存(5519 只，即时返回名称+行业)，get_stock_boards 走东财 F10 取行业+概念题材(带本地缓存)。
    # 两者各自 try/except 降级(返回空/[])，不阻断上下文装配；仅在缺名称或行业时才触发 sector_info 查询。
    boards: list = []
    try:
        from analysis.sector_api import get_stock_boards, get_stock_sector_info

        if not resolved_name or not sector:
            info = get_stock_sector_info(code) or {}
            if not resolved_name:
                resolved_name = (info.get('stock_name') or '').strip()
            if not sector:
                sector = (info.get('sector_name') or info.get('industry') or '').strip()
        boards = [str(b).strip() for b in (get_stock_boards(code, limit=6) or []) if str(b).strip()]
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"个股板块/行业兜底失败 {code}: {exc}")
    kline_payload, kline_error = _get_stock_kline_payload(code, limit=240)
    if kline_error:
        kline_payload = {
            'code': code,
            'available': False,
            'records': [],
            'message': kline_error,
        }
    trading_clients = TRADING_CLIENT_SERVICE.discover_clients(refresh=False)
    clients = [
        client for client in trading_clients.get('clients', [])
        if (client.get('capabilities') or {}).get('stock')
    ]
    return {
        'success': True,
        'stock': {
            'code': code,
            'name': resolved_name,
            'sector': sector,
            'boards': boards,
            'symbol': _xueqiu_symbol(code),
            'market': _market_symbol_for_code(code),
        },
        'quote': latest_quote,
        'opportunity': opportunity_match,
        'opportunity_report': opportunity.get('latest_report'),
        'analysis_results': _load_stock_batch_results(code),
        'kline_summary': _stock_kline_summary(kline_payload),
        'reports': _load_stock_report_history(code, resolved_name),
        'news': _stock_news_sources(code, resolved_name),
        'social': _stock_social_sources(code, resolved_name),
        'external_links': _stock_external_links(code, resolved_name),
        'trading_clients': {
            'platform': trading_clients.get('platform'),
            'generated_at': trading_clients.get('generated_at'),
            'clients': clients,
        },
    }, None


def _load_latest_opportunities():
    run_payload = _load_opportunity_run_payload()
    run = (run_payload or {}).get('run') or {}
    run_extra = _json_obj(run.get('extra_json'), {}) if run else {}
    run_hot_sector_id = run_extra.get('hot_sector_snapshot_id')
    hot_sector = _load_hot_sector_snapshot_summary(run_hot_sector_id) if run_hot_sector_id else None
    if not hot_sector:
        hot_sector = _load_hot_sector_snapshot_summary()
    reports = _latest_primary_opportunity_reports(limit=1)
    if run_payload:
        run_report = Path(str((run_payload.get('run') or {}).get('report_file') or '')).name
        if run_report and PRIMARY_OPPORTUNITY_REPORT_RE.match(run_report):
            run_report_path = RESULTS_DIR / run_report
            if run_report_path.is_file():
                reports = [run_report_path]
    if not reports and not run_payload:
        canvas = _empty_opportunity_canvas('暂无机会挖掘报告')
        if hot_sector:
            _append_hot_sector_canvas_nodes(canvas, hot_sector)
        return {
            'latest_report': None,
            'market_env': '暂无机会挖掘报告',
            'items': [],
            'canvas': canvas,
            'hot_sector': hot_sector,
            'quant_models': [],
            'stats': {
                'total': 0,
                'strong_count': 0,
                'average_score': 0,
                'top_score': 0,
            },
        }

    parsed = None
    if reports:
        try:
            parsed = _parse_opportunity_report(reports[0])
        except Exception as exc:
            logger.warning(f"解析机会报告失败: {exc}")
            if not run_payload:
                canvas = _empty_opportunity_canvas('机会报告解析失败')
                if hot_sector:
                    _append_hot_sector_canvas_nodes(canvas, hot_sector)
                return {
                    'latest_report': {
                        'file': reports[0].name,
                        'url': _report_url(reports[0]),
                        'updated_at': _format_datetime(reports[0].stat().st_mtime),
                    },
                    'market_env': '机会报告解析失败，已降级为空视图',
                    'items': [],
                    'canvas': canvas,
                    'hot_sector': hot_sector,
                    'quant_models': [],
                    'stats': {
                        'total': 0,
                        'strong_count': 0,
                        'average_score': 0,
                        'top_score': 0,
                    },
                }
    if parsed is None:
        run = (run_payload or {}).get('run') or {}
        parsed = {
            'file': Path(str(run.get('report_file') or '')).name or '',
            'url': _report_url(RESULTS_DIR / Path(str(run.get('report_file') or '')).name) if run.get('report_file') else None,
            'updated_at': run.get('run_at') or '--',
            'market_env': '已加载最新全量机会挖掘 run 明细',
            'run_meta': {},
            'degraded_count': 0,
            'all_degraded': False,
            'items': [],
        }
    all_degraded = bool(parsed.get('all_degraded'))
    raw_items = parsed['items']
    if all_degraded and not run_payload:
        raw_count = len(raw_items)
        canvas = _empty_opportunity_canvas('最新机会报告全部候选数据降级')
        if hot_sector:
            _append_hot_sector_canvas_nodes(canvas, hot_sector)
        return {
            'latest_report': {
                'file': parsed['file'],
                'url': parsed['url'],
                'updated_at': parsed['updated_at'],
                'run_meta': parsed.get('run_meta') or {},
                'degraded_count': parsed.get('degraded_count', 0),
                'all_degraded': True,
            },
            'market_env': f"最新机会报告数据降级({raw_count}/{raw_count})，量化和技术评分不可用，请重新运行机会挖掘。",
            'empty_reason': "最新机会报告全部候选缺少有效历史行情数据，已隐藏诊断性 Top 榜，避免误当正常推荐。",
            'items': [],
            'canvas': canvas,
            'hot_sector': hot_sector,
            'quant_models': [],
            'quant_models_note': '',
            'stats': {
                'total': 0,
                'strong_count': 0,
                'average_score': 0,
                'top_score': 0,
            },
        }

    items = _merge_opportunity_items(raw_items, (run_payload or {}).get('items'))
    run = (run_payload or {}).get('run') or {}
    run_meta = dict(parsed.get('run_meta') or {})
    for key in ['run_at', 'source', 'candidate_limit', 'ruleset_version', 'config_hash', 'mode']:
        if run.get(key) and key not in run_meta:
            run_meta[key] = run.get(key)
    latest_report = {
        'file': parsed['file'],
        'url': parsed['url'],
        'updated_at': parsed['updated_at'],
        'run_meta': run_meta,
        'degraded_count': parsed.get('degraded_count', 0),
        'all_degraded': False,
    }
    if run:
        latest_report.update({
            'run_id': run.get('id'),
            'run_item_count': len((run_payload or {}).get('items') or []),
            'run_at': run.get('run_at'),
        })
    scores = [_safe_float(item.get('score'), 0.0) or 0.0 for item in items]
    strong_count = len([score for score in scores if score >= 80])
    quant_models = _build_quant_model_summary(items)
    quant_models_note = ''
    if not quant_models:
        fallback = _fallback_quant_models(exclude_name=parsed['file'])
        if fallback:
            quant_models, quant_models_note = fallback
    return {
        'latest_report': latest_report,
        'market_env': parsed['market_env'],
        'empty_reason': '',
        'items': items,
        'canvas': _build_opportunity_canvas(items, latest_report, parsed['market_env'], hot_sector),
        'hot_sector': hot_sector,
        'quant_models': quant_models,
        'quant_models_note': quant_models_note,
        'stats': {
            'total': len(items),
            'strong_count': strong_count,
            'average_score': round(sum(scores) / len(scores), 2) if scores else 0,
            'top_score': round(max(scores), 2) if scores else 0,
        },
    }


def _real_sector_rows(intelligence):
    """板块动量：从东财真实行业板块构造（中文名 + 真实涨跌幅 + 主力净流入）。"""
    boards = ((intelligence or {}).get('eastmoney') or {}).get('industry_boards') or []
    rows = []
    for board in boards:
        name = board.get('name')
        if not name:
            continue
        change = round(_safe_float(board.get('change_pct'), 0.0) or 0.0, 2)
        rows.append({
            'name': name,                       # 中文板块名，如「半导体」
            'key': board.get('code'),           # 东财板块代码 BK....
            'code': board.get('code'),
            'board_code': board.get('code'),
            'avg_change': change,
            'monitored_count': 0,
            'main_net_inflow_text': board.get('main_net_inflow_text'),
            'is_hot': change >= 2.0,
            'leader': None,                      # 领涨股本期后置（spec §10）
        })
    return rows


def _build_market_dashboard(force_market_refresh=False):
    stocks = market_state.get('stocks', [])
    real_stocks = [stock for stock in stocks if stock.get('real_api_code')]
    top_movers = sorted(
        real_stocks,
        key=lambda stock: _safe_float(stock.get('change'), 0.0),
        reverse=True,
    )[:8]

    intelligence = (
        _load_market_intelligence(force_refresh=True)
        if force_market_refresh else _load_market_intelligence()
    )
    # 板块动量：完全由东财真实行业板块驱动（按当日涨跌幅热度降序，含主力净流入）。
    # 不写死板块——拉取失败时返回空（前端显示「暂无数据」），不回退合成板块。
    sector_rows = _real_sector_rows(intelligence)

    index_last_update = market_state.get("index_last_update")
    index_is_fresh = bool(index_last_update and (time.time() - index_last_update <= 600))
    raw_indices = market_state.get("indices", {}) if index_is_fresh else {}
    indices = []
    for code, fallback_name in REAL_INDEX_CODES.items():
        quote = raw_indices.get(code)
        if quote:
            indices.append({
                "code": code,
                "name": quote.get("display_name") or fallback_name,
                "price": round(_safe_float(quote.get("price"), 0.0), 2),
                "change": round(_safe_float(quote.get("change"), 0.0), 2),
                "change_pct": round(_safe_float(quote.get("change_pct"), 0.0), 2),
                "available": True,
                "source": quote.get("source", "tencent"),
                "updated_at": quote.get("updated_at"),
            })
        else:
            indices.append({
                "code": code,
                "name": fallback_name,
                "price": None,
                "change": None,
                "change_pct": None,
                "available": False,
                "source": "tencent",
                "updated_at": None,
            })

    primary_index = next((item for item in indices if item["code"] == "sh000001"), indices[0] if indices else None)

    return {
        'index': primary_index.get("price") if primary_index else None,
        'index_change': primary_index.get("change_pct") if primary_index else None,
        'primary_index': primary_index,
        'indices': indices,
        'index_available': bool(primary_index and primary_index.get("available")),
        'index_last_update': _format_datetime(index_last_update) if index_last_update else None,
        'total_particles': len(stocks),
        'monitored_count': len(real_stocks),
        'limit_up_count': len([stock for stock in real_stocks if stock.get('isLimitUp')]),
        'last_update': _format_datetime(market_state.get('last_update', time.time())),
        'top_movers': [
            {
                'code': stock.get('code'),
                'name': stock.get('name'),
                'price': round(_safe_float(stock.get('price'), 0.0), 2),
                'change': round(_safe_float(stock.get('change'), 0.0), 2),
                'sector': SECTORS[stock.get('sectorId', 0)]['name'] if stock.get('sectorId') is not None else '',
            }
            for stock in top_movers
        ],
        'sectors': sector_rows,
        'news': market_state.get('news', [])[:8],
        'intelligence': intelligence,
    }


def _normalize_market_cloud_trade_date(value):
    text = str(value or '').strip()
    if not text:
        return ''
    compact = re.sub(r'\D', '', text)
    if len(compact) != 8:
        return ''
    try:
        datetime.datetime.strptime(compact, '%Y%m%d')
    except ValueError:
        return ''
    return compact


def _market_cloud_payload(force_refresh=False, limit=5000, trade_date=None):
    limit = _safe_int(limit, 5000, minimum=100, maximum=6000) or 5000
    normalized_trade_date = _normalize_market_cloud_trade_date(trade_date)
    error = None
    try:
        stocks = MARKET_INTELLIGENCE_SERVICE.fetch_market_cloud_stocks(
            limit=limit,
            force_refresh=bool(force_refresh),
            trade_date=normalized_trade_date or None,
        )
    except Exception as exc:
        stocks = []
        error = str(exc)

    dashboard = _build_market_dashboard(force_market_refresh=force_refresh)
    source = (stocks[0].get('source') if stocks and isinstance(stocks[0], dict) else None)
    return {
        'generated_at': datetime.datetime.now().isoformat(),
        'source': source or ('tushare_market_cloud' if normalized_trade_date else ('market_cloud' if stocks else 'dashboard_fallback')),
        'error': error,
        'requested_trade_date': normalized_trade_date,
        'trade_date': (
            stocks[0].get('trade_date')
            if stocks and isinstance(stocks[0], dict) and stocks[0].get('trade_date')
            else normalized_trade_date
        ),
        'stocks': stocks,
        'indices': dashboard.get('indices') or [],
        'primary_index': dashboard.get('primary_index'),
        'sectors': dashboard.get('sectors') or [],
        'dashboard': dashboard,
    }


def _get_stock_kline_payload(stock_code, period='daily', limit=120):
    return STOCK_KLINE_SERVICE.get_payload(stock_code, period=period, limit=limit)


def _load_batch_summary():
    batch_results = _latest_files(RESULTS_DIR, 'batch_results_*.csv', limit=5)
    batch_details = _latest_files(RESULTS_DIR, 'batch_details_*.json', limit=5)
    batch_predictions = _latest_files(RESULTS_DIR, 'batch_prediction_*.csv', limit=8)

    analysis_runs = []
    for csv_path in batch_results:
        rows = 0
        top_score = 0
        try:
            df = pd.read_csv(csv_path)
            rows = int(len(df))
            if not df.empty:
                score_col = next((col for col in ['综合评分', 'score', 'total_score'] if col in df.columns), None)
                if score_col:
                    top_score = round(float(pd.to_numeric(df[score_col], errors='coerce').max()), 2)
        except Exception:
            pass
        analysis_runs.append({
            'file': csv_path.name,
            'updated_at': _format_datetime(csv_path.stat().st_mtime),
            'rows': rows,
            'top_score': top_score,
            'url': _report_url(csv_path),
        })

    detail_runs = []
    for json_path in batch_details:
        summary = {}
        try:
            summary = json.loads(json_path.read_text(encoding='utf-8'))
        except Exception:
            summary = {}
        detail_runs.append({
            'file': json_path.name,
            'updated_at': _format_datetime(json_path.stat().st_mtime),
            'summary': summary,
            'url': _report_url(json_path),
        })

    prediction_runs = []
    for csv_path in batch_predictions:
        rows = 0
        try:
            rows = int(len(pd.read_csv(csv_path)))
        except Exception:
            rows = 0
        prediction_runs.append({
            'file': csv_path.name,
            'updated_at': _format_datetime(csv_path.stat().st_mtime),
            'rows': rows,
            'url': _report_url(csv_path),
        })

    return {
        'analysis_runs': analysis_runs,
        'detail_runs': detail_runs,
        'prediction_runs': prediction_runs,
        'analysis_count': len(batch_results),
        'prediction_count': len(batch_predictions),
    }


def _report_history_candidates(limit=12):
    patterns = [
        ('机会挖掘HTML', RESULTS_DIR, 'opportunity_discovery_*.html'),
        ('机会Top榜', RESULTS_DIR, 'opportunity_top10_*.md'),
        ('个股分析报告', RESULTS_DIR, 'lumo_analysis_*.html'),
        ('重大利好挖掘', PROJECT_ROOT / 'integrated_results', 'major_positive_news_*.html'),
    ]
    reports = []
    for label, directory, pattern in patterns:
        for path in _latest_files(directory, pattern, limit=limit):
            reports.append({
                'type': label,
                'file': path.name,
                'updated_at': _format_datetime(path.stat().st_mtime),
                'url': _report_url(path),
                'size_kb': round(path.stat().st_size / 1024, 1),
                '_path': str(path),
            })

    reports.sort(key=lambda item: item['updated_at'], reverse=True)
    return reports[:limit]


def _load_report_history(limit=12):
    return [
        {key: value for key, value in item.items() if key != '_path'}
        for item in _report_history_candidates(limit=limit)
    ]


def _module_health():
    modules = [
        ('投资机会挖掘', 'scripts.run_opportunity_discovery', '热门股、多源候选、漏斗过滤、报告生成'),
        ('异步批量分析', 'analysis.batch_processor', '批量采集、并发评分、结果汇总'),
        ('机会评分器', 'analysis.opportunity_scorer', '量化、技术、情绪、板块、事件综合打分'),
        ('专业个股分析', 'analysis.professional_stock_analyzer', '基本面、行业对比、风险过滤'),
        ('资金情绪分析', 'analysis.investor_sentiment', '主力资金、龙虎榜、市场情绪、股吧情绪'),
        ('Kronos预测模型', 'model', 'K线预测与多模型推理'),
    ]
    health = []
    for name, module_name, scope in modules:
        try:
            __import__(module_name)
            status = 'ready'
        except Exception:
            status = 'degraded'
        health.append({
            'name': name,
            'module': module_name,
            'scope': scope,
            'status': status,
        })
    return health


def _update_job(job_id, **updates):
    JOB_SERVICE.update(job_id, **updates)


def _append_job_log(job_id, message):
    JOB_SERVICE.append_log(job_id, message)


class _JobLogHandler(logging.Handler):
    """Forward selected Python logger messages into the WebUI job log."""

    def __init__(self, job_id):
        super().__init__(level=logging.INFO)
        self.job_id = job_id
        self._last_message = None

    def emit(self, record):
        try:
            message = record.getMessage()
            message = " / ".join(line.strip() for line in str(message).splitlines() if line.strip())
            if not message or message == self._last_message:
                return
            self._last_message = message
            if len(message) > 700:
                message = f"{message[:697]}..."
            _append_job_log(self.job_id, message)
        except Exception:
            pass


class _JobLogCapture:
    def __init__(self, job_id, logger_names):
        self.handler = _JobLogHandler(job_id)
        self.logger_names = logger_names
        self.loggers = []

    def __enter__(self):
        self.handler.setFormatter(logging.Formatter('%(message)s'))
        for name in self.logger_names:
            logger = logging.getLogger(name)
            logger.addHandler(self.handler)
            self.loggers.append(logger)
        return self

    def __exit__(self, exc_type, exc, traceback):
        for logger in self.loggers:
            logger.removeHandler(self.handler)
        self.loggers.clear()


def _get_job_snapshot(job_id=None):
    snapshot = JOB_SERVICE.snapshot(job_id, limit=20)
    if job_id:
        return _reconcile_completed_opportunity_job(snapshot)
    return [_reconcile_completed_opportunity_job(job) for job in (snapshot or [])]


def _run_opportunity_job(job_id, params):
    _update_job(job_id, status='running', started_at=datetime.datetime.now().isoformat())
    run_started_ts = time.time()
    limit = _safe_int(params.get('limit'), 100, minimum=5, maximum=500) or 100
    workers = _safe_int(params.get('workers'), 10, minimum=1, maximum=32) or 10
    source = str(params.get('source') or 'multi').strip() or 'multi'
    stock_codes = params.get('stock_codes') or []
    source_label = {
        'multi': '多源综合',
        'heat': '仅热度榜',
        'moneyflow_dc': '资金流向榜单',
        'sector_hot': '热门板块成分股',
    }.get(source, source)
    mode = 'specified_pool' if stock_codes else 'market_scan'
    mode_label = '指定股票池' if stock_codes else '全市场扫描'
    stock_hint = f" · 股票 {','.join(stock_codes)}" if stock_codes else ''
    _append_job_log(
        job_id,
        f'开始执行投资机会挖掘 ({mode_label} · 来源 {source_label} · 条数 {limit} · 线程 {workers}{stock_hint})',
    )
    tracker = None
    try:
        from scripts.run_opportunity_discovery import OpportunityDiscovery
        from webui.services.discovery_progress import DiscoveryProgressTracker

        tracker = DiscoveryProgressTracker(
            writer=lambda p: _update_job(job_id, progress=p),
            max_workers=workers,
        )
        with _JobLogCapture(job_id, ['scripts.run_opportunity_discovery']):
            discovery = OpportunityDiscovery(max_workers=workers, progress_hook=tracker)
            report_path = discovery.run(
                limit=limit,
                test_codes=stock_codes or None,
                source=source,
            )
            generated_top_report = getattr(discovery.report_generator, 'latest_top_report_path', '') or ''
        report_file = Path(report_path).name if report_path else ''
        top_report_path = None
        if generated_top_report:
            top_report_path = Path(generated_top_report)
        elif report_file.startswith('opportunity_top10_') and report_file.endswith('.md'):
            top_report_path = Path(report_path)
        else:
            top_report_path = _latest_primary_opportunity_report_since(run_started_ts)
        result = {
            'report_path': str(report_path) if report_path else '',
            'report_file': report_file,
            'report_url': _report_url(report_path) if report_path else None,
            'top_report_path': str(top_report_path) if top_report_path else '',
            'top_report_file': top_report_path.name if top_report_path else '',
            'top_report_url': _report_url(top_report_path) if top_report_path else None,
            'mode': mode,
            'mode_label': mode_label,
            'source': source,
            'source_label': source_label,
            'params': {
                'limit': limit,
                'workers': workers,
                'source': source,
                'stock_codes': stock_codes,
            },
        }
        _append_job_log(job_id, f"机会挖掘完成: {result['report_path'] or '未生成报告'}")
        if tracker is not None:
            tracker.finalize(ok=True)
        _update_job(
            job_id,
            status='finished',
            finished_at=datetime.datetime.now().isoformat(),
            result=result,
        )
        # ── 通知 + 自动跟单钩子(best-effort,绝不影响任务成败)──
        try:
            NOTIFICATION_EVENTS.push(
                'opportunity_done',
                '机会挖掘任务完成',
                f"报告 {result['top_report_file'] or result['report_file'] or '未生成'} 已生成",
                payload={'report_file': result['top_report_file'] or result['report_file']},
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            if top_report_path:
                follow = AUTO_FOLLOW_SERVICE.follow_report(
                    _parse_opportunity_report(top_report_path),
                    report_file=top_report_path.name,
                )
                if follow.get('enabled'):
                    _append_job_log(
                        job_id,
                        f"自动跟单: 新建 {follow['placed']} 笔次日开盘买单(幂等跳过 {follow['skipped']})",
                    )
        except Exception as exc:  # noqa: BLE001
            _append_job_log(job_id, f'自动跟单失败(不影响挖掘结果): {exc}')
    except Exception as exc:
        _append_job_log(job_id, f'机会挖掘失败: {exc}')
        if tracker is not None:
            tracker.finalize(ok=False)
        _update_job(
            job_id,
            status='failed',
            finished_at=datetime.datetime.now().isoformat(),
            error=str(exc),
        )


def _passed_stocks_from_result_df(result_df, limit=50):
    """从批量结果 DataFrame 抽取「通过」个股(code/score/rating)，供前端直达个股分析。

    返回 (stocks, passed_count)。passed_count 为通过总数，stocks 最多 limit 条。
    """
    if result_df is None or getattr(result_df, 'empty', True) or '股票代码' not in result_df.columns:
        return [], 0
    df_pass = result_df
    if '过滤状态' in result_df.columns:
        df_pass = result_df[result_df['过滤状态'] == '通过']
    passed_count = int(len(df_pass))
    stocks = []
    for _, srow in df_pass.head(limit).iterrows():
        code = str(srow.get('股票代码') or '').strip()
        if not code:
            continue
        rating = srow.get('评级')
        rating = str(rating).strip() if rating is not None else ''
        if rating in ('nan', 'None'):
            rating = ''
        stocks.append({
            'code': code,
            'score': _safe_float(srow.get('综合评分'), None),
            'rating': rating or None,
        })
    return stocks, passed_count


def _run_batch_analysis_job(job_id, params):
    _update_job(job_id, status='running', started_at=datetime.datetime.now().isoformat())
    _append_job_log(job_id, '开始执行批量分析')
    try:
        import asyncio
        from analysis.batch_processor import BatchProcessor

        processor = BatchProcessor(
            max_concurrent=params['max_concurrent'],
            collection_timeout=params['collection_timeout'],
            scoring_timeout=params['scoring_timeout'],
            enable_progress_bar=False,
        )
        processor.set_progress_callback(lambda message: _append_job_log(job_id, message))

        result_df, details, stats = asyncio.run(processor.process_batch(
            params['stock_codes'],
            data_types=params['data_types'],
            filter_strategy=params['filter_strategy'],
            skip_scoring=params['skip_scoring'],
        ))

        csv_path = None
        json_path = None
        if result_df is not None:
            csv_path, json_path = processor.save_results(
                result_df,
                details,
                output_dir=str(RESULTS_DIR),
            )

        # 批量完成后抽取「通过」的个股，供前端完成卡直达个股分析（spec 模块 B）。
        passed_rows, passed_count = _passed_stocks_from_result_df(result_df)

        result = {
            'rows': int(len(result_df)) if result_df is not None else 0,
            'statistics': stats.to_dict() if stats else {},
            'csv_path': str(csv_path) if csv_path else '',
            'json_path': str(json_path) if json_path else '',
            'csv_url': _report_url(csv_path) if csv_path else None,
            'json_url': _report_url(json_path) if json_path else None,
            'stocks': passed_rows,
            'passed_count': passed_count,
            'stocks_truncated': passed_count > len(passed_rows),
        }
        _append_job_log(job_id, f"批量分析完成，通过股票 {result['rows']} 只")
        _update_job(
            job_id,
            status='finished',
            finished_at=datetime.datetime.now().isoformat(),
            result=_json_safe(result),
        )
    except Exception as exc:
        _append_job_log(job_id, f'批量分析失败: {exc}')
        _update_job(
            job_id,
            status='failed',
            finished_at=datetime.datetime.now().isoformat(),
            error=str(exc),
        )


def _run_pattern_refresh_job(job_id, params):
    _update_job(job_id, status='running', started_at=datetime.datetime.now().isoformat())
    _append_job_log(job_id, '开始刷新形态指纹库')
    try:
        def log_cb(message):
            _append_job_log(job_id, message)

        result = PATTERN_SEARCH_SERVICE.refresh_fingerprints(params, progress_callback=log_cb)
        _append_job_log(
            job_id,
            f"刷新完成: 总数 {result['total']} 成功 {result['succeeded']} 失败 {result['failed']}"
        )
        _update_job(
            job_id,
            status='finished',
            finished_at=datetime.datetime.now().isoformat(),
            result=result,
        )
    except Exception as exc:
        _append_job_log(job_id, f'指纹刷新失败: {exc}')
        _update_job(
            job_id,
            status='failed',
            finished_at=datetime.datetime.now().isoformat(),
            error=str(exc),
        )


def _run_capital_backfill_job(job_id, params):
    """后台回填资金榜单(主力净流入榜 moneyflow + 龙虎榜 dragon_tiger),最近 N 个交易日。"""
    _update_job(job_id, status='running', started_at=datetime.datetime.now().isoformat())
    days = int(params.get('days') or 30)
    kinds = tuple(params.get('kinds') or ('moneyflow', 'dragon_tiger'))
    _append_job_log(job_id, f'开始回填资金榜单 最近 {days} 个交易日 ({", ".join(kinds)})')
    try:
        summary = CAPITAL_RANKINGS_SERVICE.backfill(kinds=kinds, days=days)
        from webui.services.capital_rankings_service import backfill_outcome
        outcome = backfill_outcome(summary)
        parts = [
            f"{k} {s.get('ok_dates', 0)}/{s.get('dates', 0)}日 {s.get('rows', 0)}行"
            + (f" 机构席位{s.get('inst_rows')}行" if s.get('inst_rows') else "")
            + (f" 跳过已存在{s.get('skipped_dates')}日" if s.get('skipped_dates') else "")
            for k, s in summary.items()
        ]
        if not outcome['ok']:
            # 全部 0 行且有错误(最常见:Tushare Token 无效 / 缺失)→ 标记失败并显形,
            # 不再静默成「回填完成」让用户对着「暂无数据」一脸懵。
            msg = '; '.join(outcome['errors']) or '未获取到任何数据(请检查 Tushare Token)'
            _append_job_log(job_id, '回填失败: ' + msg)
            _update_job(
                job_id,
                status='failed',
                finished_at=datetime.datetime.now().isoformat(),
                error=msg,
                result=summary,
            )
            return
        _append_job_log(job_id, '回填完成: ' + '; '.join(parts))
        if outcome['errors']:
            _append_job_log(job_id, '部分日期失败: ' + '; '.join(outcome['errors']))
        _update_job(
            job_id,
            status='finished',
            finished_at=datetime.datetime.now().isoformat(),
            result=summary,
        )
    except Exception as exc:
        _append_job_log(job_id, f'资金榜单回填失败: {exc}')
        _update_job(
            job_id,
            status='failed',
            finished_at=datetime.datetime.now().isoformat(),
            error=str(exc),
        )


# ----------------------------------------------------------------------------
# 形态指纹库：收盘后自动重建（A 股「隔日快照」问题的服务端兜底）
#   指纹库是隔日冻结的 SQLite 快照，过去只能手动点「刷新」。这里加一个守护线程：
#   工作日收盘整理后若发现快照仍是旧交易日，且无刷新任务在跑，则自动触发重建。
# ----------------------------------------------------------------------------
pattern_autorefresh_thread = None
pattern_autorefresh_lock = threading.Lock()


def _parse_after_close(raw):
    """'15:30' → datetime.time(15,30)；非法值回退 15:30（给收盘后数据整理留足时间）。"""
    try:
        hh, mm = str(raw or '').strip().split(':', 1)
        return datetime.time(hour=int(hh), minute=int(mm))
    except (ValueError, AttributeError, TypeError):
        return datetime.time(hour=15, minute=30)


def _expected_pattern_snapshot_date(now, after_close):
    """最近一个『应已生成指纹』的交易日（粗口径：仅按工作日，不查节假日历）。

    - 工作日且已过收盘整理时刻 → 今天
    - 否则回退到最近的上一个工作日（跨过周末）
    偶发法定节假日会多触发一次重建（无害：拿到的就是最近交易日数据，快照日期随之对齐）。
    """
    today = now.date()
    if today.weekday() < 5 and now.time() >= after_close:
        return today
    probe = today - datetime.timedelta(days=1)
    while probe.weekday() >= 5:  # 5=周六 6=周日
        probe -= datetime.timedelta(days=1)
    return probe


def _pattern_refresh_in_flight():
    """是否已有形态指纹刷新任务在队列/运行中（避免自动调度与手动刷新重复触发）。"""
    try:
        for job in JOB_STORE.list_by_status(('queued', 'running'), limit=50):
            if job.get('type') == 'pattern_refresh':
                return True
    except Exception:  # noqa: BLE001 — 查询失败时按『无在跑任务』处理，最坏多跑一次
        return False
    return False


def _pattern_autorefresh_loop(after_close, interval):
    time.sleep(20)  # 冷启动让位：后端刚拉起时别和首屏请求抢指纹库读
    while True:
        try:
            now = datetime.datetime.now()
            expected = _expected_pattern_snapshot_date(now, after_close)
            current = None
            snapshot_raw = PATTERN_SEARCH_SERVICE.status().get('snapshot_date')
            if snapshot_raw:
                try:
                    current = datetime.date.fromisoformat(snapshot_raw)
                except (ValueError, TypeError):
                    current = None
            if (current is None or current < expected) and not _pattern_refresh_in_flight():
                print(f"[pattern-autorefresh] 指纹库快照={current} < 期望={expected}，自动触发重建")
                JOB_SERVICE.start(
                    "pattern_refresh",
                    PATTERN_SEARCH_SERVICE.refresh_params({}),
                    _run_pattern_refresh_job,
                )
        except Exception as exc:  # noqa: BLE001 — 守护线程绝不能因偶发错误退出
            print(f"[pattern-autorefresh] 调度循环异常: {exc}")
        time.sleep(interval)


def start_pattern_autorefresh():
    """启动『收盘后自动重建形态指纹库』守护线程（进程内只启一次）。

    环境变量：
    - KRONOS_DISABLE_PATTERN_AUTOREFRESH=1   关闭自动重建
    - KRONOS_PATTERN_REFRESH_AFTER=15:30     收盘整理时刻（默认 15:30）
    - KRONOS_PATTERN_REFRESH_INTERVAL=1800   轮询间隔秒（默认 1800，最低 60）
    """
    global pattern_autorefresh_thread
    flag = os.environ.get("KRONOS_DISABLE_PATTERN_AUTOREFRESH", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        print("[pattern-autorefresh] 已被 KRONOS_DISABLE_PATTERN_AUTOREFRESH 关闭")
        return None
    with pattern_autorefresh_lock:
        if pattern_autorefresh_thread and pattern_autorefresh_thread.is_alive():
            return pattern_autorefresh_thread
        after_close = _parse_after_close(os.environ.get("KRONOS_PATTERN_REFRESH_AFTER", "15:30"))
        try:
            interval = float(os.environ.get("KRONOS_PATTERN_REFRESH_INTERVAL", "1800"))
        except (TypeError, ValueError):
            interval = 1800.0
        interval = max(60.0, interval)
        pattern_autorefresh_thread = threading.Thread(
            target=_pattern_autorefresh_loop,
            args=(after_close, interval),
            daemon=True,
        )
        pattern_autorefresh_thread.start()
        print(f"[pattern-autorefresh] 已启动：收盘 {after_close.strftime('%H:%M')} 后自动重建，轮询 {int(interval)}s")
        return pattern_autorefresh_thread


# ----------------------------------------------------------------------------
# 模拟盘 EOD：收盘后自动复盘
#   工作日收盘整理后,若当日有持仓/成交,自动撮合补算 pending 单、按收盘价盯市权益、
#   生成当日复盘 markdown(results_dir/paper_review_YYYY-MM-DD.md)。一天只跑一次。
# ----------------------------------------------------------------------------
paper_eod_thread = None
paper_eod_lock = threading.Lock()
_paper_eod_last_date = None


def _paper_eod_market_env():
    """复盘的市场环境文本:复用最新机会挖掘报告已算好的 market_env(best-effort)。"""
    try:
        env = _load_latest_opportunities().get('market_env')
        if env and '暂无' not in env and '失败' not in env:
            return env
    except Exception:  # noqa: BLE001 — 市场环境只是装饰,取不到不影响复盘
        pass
    return None


def run_paper_eod(date=None):
    """执行一次模拟盘 EOD(撮合补算 + 盯市 + 当日复盘 markdown)。

    供守护线程与 /api/paper/settle 复用;返回 PaperTradingService.run_eod(...) 结果。
    复盘完成后联动:① 自动跟单到期检查(持有 N 个交易日后挂次日开盘卖单);
    ② 当日有持仓/成交时把「复盘完成 + 盈亏/胜率摘要」写入应用内通知事件。"""
    result = PAPER_TRADING_SERVICE.run_eod(
        date=date,
        market_env_text=_paper_eod_market_env(),
        results_dir=str(RESULTS_DIR),
    )
    try:
        result['auto_follow'] = AUTO_FOLLOW_SERVICE.process_eod(result.get('date'))
    except Exception as exc:  # noqa: BLE001 — 跟单收尾失败不影响复盘本身
        logger.warning(f"自动跟单 EOD 处理失败: {exc}")
    if result.get('activity'):
        try:
            stats = PAPER_TRADING_SERVICE.stats()
            equity = result.get('equity') or {}
            NOTIFICATION_EVENTS.push(
                'paper_eod',
                f"模拟盘复盘完成({result.get('date')})",
                f"当日盈亏 {float(equity.get('daily_pnl') or 0):+,.2f}"
                f" · 累计胜率 {float(stats.get('win_rate') or 0) * 100:.1f}%"
                f" · 已实现 {float(stats.get('total_realized') or 0):+,.2f}",
                payload={'date': result.get('date'), 'review_path': result.get('review_path')},
            )
        except Exception:  # noqa: BLE001
            pass
    return result


def _paper_eod_loop(after_close, interval):
    global _paper_eod_last_date
    time.sleep(25)  # 冷启动让位:别和首屏请求抢
    while True:
        try:
            now = datetime.datetime.now()
            today = now.date()
            if today.weekday() < 5 and now.time() >= after_close and _paper_eod_last_date != today:
                result = run_paper_eod(today.isoformat())
                _paper_eod_last_date = today  # 当天只跑一次(run_eod 本身对复盘文件也幂等)
                if result.get('generated'):
                    print(f"[paper-eod] 已生成当日复盘 {result.get('review_path')}")
        except Exception as exc:  # noqa: BLE001 — 守护线程绝不能因偶发错误退出
            print(f"[paper-eod] 调度循环异常: {exc}")
        time.sleep(interval)


def start_paper_eod():
    """启动『收盘后模拟盘自动复盘』守护线程(进程内只启一次)。

    环境变量：
    - KRONOS_DISABLE_PAPER_EOD=1     关闭自动复盘
    - KRONOS_PAPER_EOD_AFTER=15:30   收盘触发时刻(默认 15:30)
    - KRONOS_PAPER_EOD_INTERVAL=1800 轮询间隔秒(默认 1800,最低 60)
    """
    global paper_eod_thread
    flag = os.environ.get("KRONOS_DISABLE_PAPER_EOD", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        print("[paper-eod] 已被 KRONOS_DISABLE_PAPER_EOD 关闭")
        return None
    with paper_eod_lock:
        if paper_eod_thread and paper_eod_thread.is_alive():
            return paper_eod_thread
        after_close = _parse_after_close(os.environ.get("KRONOS_PAPER_EOD_AFTER", "15:30"))
        try:
            interval = float(os.environ.get("KRONOS_PAPER_EOD_INTERVAL", "1800"))
        except (TypeError, ValueError):
            interval = 1800.0
        interval = max(60.0, interval)
        paper_eod_thread = threading.Thread(
            target=_paper_eod_loop,
            args=(after_close, interval),
            daemon=True,
        )
        paper_eod_thread.start()
        print(f"[paper-eod] 已启动：收盘 {after_close.strftime('%H:%M')} 后自动复盘,轮询 {int(interval)}s")
        return paper_eod_thread


# ----------------------------------------------------------------------------
# 整库备份(Phase 6):导出一致性 .db 快照 / 导入(校验 → 自动备份 → 灌库 → migrate)
# ----------------------------------------------------------------------------
def export_db_snapshot():
    """生成整库 .db 快照(临时文件),返回 (路径, 下载文件名)。"""
    path = DB_BACKUP_SERVICE.export_snapshot()
    return path, Path(path).name


def import_db_snapshot(uploaded_path):
    """导入上传库:自动备份当前库到数据目录 backups/,再 backup-into-live + migrate。"""
    from data_store.connection import db_path
    backup_dir = str(Path(db_path()).parent / "backups")
    return DB_BACKUP_SERVICE.import_snapshot(uploaded_path, backup_dir=backup_dir)


def validate_db_snapshot(path):
    """校验上传库是否为合法 Kronos 库(供导入前预检/调试)。"""
    return DB_BACKUP_SERVICE.validate_db(path)


def _klines_to_ohlcv_df(klines):
    """把原始日K(Sina dict / Tencent list / CSV 串)转成 QuantitativeModels 可用的
    OHLCV DataFrame。丢弃非法/0 价行;无有效行返回 None。仅保留 open/high/low/close/
    volume 五列,其余指标由模型自行派生。"""
    rows = []
    for item in klines or []:
        try:
            if isinstance(item, dict):
                o = float(item.get('open') or 0)
                h = float(item.get('high') or 0)
                low_v = float(item.get('low') or 0)
                c = float(item.get('close') or 0)
                v = float(item.get('volume') or 0)
            elif isinstance(item, (list, tuple)) and len(item) >= 6:
                # 腾讯风格: [date, open, close, high, low, volume]
                o = float(item[1]); c = float(item[2]); h = float(item[3])
                low_v = float(item[4]); v = float(item[5])
            else:
                parts = str(item).split(',')
                if len(parts) < 6:
                    continue
                o = float(parts[1]); c = float(parts[2]); h = float(parts[3])
                low_v = float(parts[4]); v = float(parts[5])
        except (TypeError, ValueError):
            continue
        if o <= 0 or c <= 0:
            continue
        rows.append({
            'open': o,
            'high': h if h > 0 else max(o, c),
            'low': low_v if low_v > 0 else min(o, c),
            'close': c,
            'volume': v if v > 0 else 0.0,
        })
    if not rows:
        return None
    return pd.DataFrame(rows)


def _opportunity_jury_from_df(df):
    """在一段日K上跑 30 量化模型,产出「多空评审团」分值:多/空/观望票数 +
    情景概率(多/空/震荡) + 量价博弈分(0–100,50 为均衡) + 态势标签。
    与个股分析套件 30 模型口径一致(count_quant_signals)。失败/数据不足返回降级结构。"""
    from analysis.stock_analysis_suite import (
        count_quant_signals, compute_volume_price_game_score,
    )
    try:
        counts = count_quant_signals(df)
    except Exception as exc:  # noqa: BLE001
        return {'data_status': 'unavailable', 'reason': f'量化模型运行失败: {str(exc)[:80]}'}
    buy = int(counts.get('buy_signal_count') or 0)
    sell = int(counts.get('sell_signal_count') or 0)
    hold = int(counts.get('hold_signal_count') or 0)
    total = int(counts.get('total') or 0)
    if total <= 0:
        return {'data_status': 'unavailable', 'reason': '量化模型未产出信号'}
    game = compute_volume_price_game_score(buy, total, sell)
    bullish = int(round(buy / total * 100))
    bearish = int(round(sell / total * 100))
    sideways = 100 - bullish - bearish
    neutral_ratio = max(0.0, (total - buy - sell) / total)
    if neutral_ratio >= 0.6 and 40 <= game <= 60:
        label = '观望主导'
    elif game >= 80:
        label = '强势多头'
    elif game >= 60:
        label = '震荡偏多'
    elif game > 40:
        label = '震荡'
    elif game > 20:
        label = '震荡偏空'
    else:
        label = '强势空头'
    return {
        'data_status': 'fresh',
        'game_score': game,
        'label': label,
        'buy_signal_count': buy,
        'sell_signal_count': sell,
        'hold_signal_count': hold,
        'total_models': total,
        'bullish': bullish,
        'bearish': bearish,
        'sideways': sideways,
        'source': '30 量化模型多空票数归一化',
    }


def _opportunity_stock_scores(code, name='', window_days=30, fetch_klines=None,
                              capital_summary=None):
    """投资机会挖掘·个股深度评分(懒加载,按需现算)。返回三块分值:

    - ``pattern``:同类图形「自回测」——该股最近 window_days 的形态在自身历史里
      扫描相似片段,统计前向 5/10/20 日胜率/平均收益 → 形态评分(win_rate×100)。
    - ``jury``:多空评审团——30 量化模型末根 K 多空票数 → 量价博弈分 + 情景概率。
    - ``capital``:资金榜单——主力净流入榜名次 + 龙虎榜聚合(CapitalRankingsService)。

    形态回测与评审团共用一次日K拉取(Sina 免 token);资金榜单走本地库查询。
    三者任一失败互不影响,各自降级。``fetch_klines``/``capital_summary`` 可注入以便离线单测。
    """
    from analysis.pattern_backtest import scan_series, parse_close_series, _summarise_horizon

    code = _stock_code_key(code) or str(code or '').strip()
    if not code:
        return {'ok': False, 'error': '缺少股票代码'}
    window_days = _safe_int(window_days, 30, minimum=5, maximum=120) or 30
    horizons = [5, 10, 20]
    result = {
        'ok': True,
        'code': code,
        'name': name or code,
        'window_days': window_days,
        'horizons': horizons,
    }

    if fetch_klines is None:
        try:
            from scripts.build_pattern_fingerprints import fetch_recent_klines as fetch_klines
        except Exception as exc:  # noqa: BLE001
            fetch_klines = None
            result['pattern'] = {'data_status': 'unavailable', 'reason': f'K线取数模块不可用: {exc}'}
            result['jury'] = {'data_status': 'unavailable', 'reason': 'K线取数模块不可用'}

    klines = None
    if fetch_klines is not None:
        digits = ''.join(ch for ch in code if ch.isdigit())
        sina = ('sh' if digits.startswith('6') else 'sz') + digits
        try:
            _name, klines = fetch_klines(sina, 250)
            if _name and not name:
                result['name'] = _name
        except Exception as exc:  # noqa: BLE001
            klines = None
            reason = f'日K拉取失败: {str(exc)[:80]}'
            result['pattern'] = {'data_status': 'unavailable', 'reason': reason}
            result['jury'] = {'data_status': 'unavailable', 'reason': reason}

    # ── 形态回测(自回测)─────────────────────────────────────────
    if klines is not None and 'pattern' not in result:
        closes = parse_close_series(klines)
        need = window_days + max(horizons) + 2
        if len(closes) < need:
            result['pattern'] = {
                'data_status': 'unavailable',
                'reason': f'历史数据不足(需 ≥{need} 个交易日,当前 {len(closes)})',
            }
        else:
            query = closes[-window_days:]
            samples = scan_series(closes, query, window_days=window_days, horizons=horizons)
            per_h = {}
            for h in horizons:
                rets = [s['returns'][h] for s in samples if h in s.get('returns', {})]
                per_h[str(h)] = _summarise_horizon(h, rets)
            primary = per_h.get('10') or per_h.get(str(horizons[0]))
            count = primary['count'] if primary else 0
            wr = primary['win_rate'] if (primary and count) else None
            ar = primary['avg_return'] if (primary and count) else None
            result['pattern'] = {
                'data_status': 'fresh' if count else 'thin',
                'pattern_score': round(wr * 100, 1) if wr is not None else None,
                'win_rate': wr,
                'avg_return': ar,
                'sample_count': count,
                'horizons': per_h,
            }

    # ── 多空评审团(30 模型)──────────────────────────────────────
    if klines is not None and 'jury' not in result:
        df = _klines_to_ohlcv_df(klines)
        if df is None or len(df) < 35:
            have = 0 if df is None else len(df)
            result['jury'] = {
                'data_status': 'unavailable',
                'reason': f'量化模型需 ≥35 交易日,当前仅 {have} 日',
            }
        else:
            result['jury'] = _opportunity_jury_from_df(df)

    # ── 资金榜单(本地库)─────────────────────────────────────────
    try:
        if capital_summary is not None:
            result['capital'] = capital_summary(code)
        else:
            result['capital'] = CAPITAL_RANKINGS_SERVICE.stock_capital_summary(code)
    except Exception as exc:  # noqa: BLE001
        result['capital'] = {
            'kind': 'stock_capital_rankings',
            'code': code,
            'data_status': 'unavailable',
            'reason': f'资金榜单读取失败: {str(exc)[:80]}',
        }

    return result


def _opportunity_pattern_backtest(params, log_cb=None, fetch_klines=None):
    """对某次机会挖掘 run 的 Top-N 股票做「形态自回测」:用每只票最近 window_days
    的形态在自身历史里扫描相似片段,统计前向 5/10/20 日胜率与平均收益 → 形态评分。
    """
    from analysis.pattern_backtest import scan_series, parse_close_series, _summarise_horizon
    from data_store import opportunity_repo

    run_id = params.get('run_id')
    date = params.get('date')
    top_n = _safe_int(params.get('top_n'), 20, minimum=1, maximum=50) or 20
    window_days = _safe_int(params.get('window_days'), 30, minimum=5, maximum=120) or 30
    horizons = [5, 10, 20]

    if run_id is not None:
        run = opportunity_repo.get_run(run_id)
    elif date:
        runs = opportunity_repo.list_runs(run_date=str(date)[:10], limit=1)
        run = runs[0] if runs else None
    else:
        run = opportunity_repo.latest_run()
    if not run:
        return {'ok': False, 'error': '无可回测的机会挖掘 run'}
    items = [i for i in opportunity_repo.items_for_run(int(run['id'])) if i.get('code')][:top_n]
    if not items:
        return {'ok': False, 'error': '该 run 无入库股票明细'}

    if fetch_klines is None:
        try:
            from scripts.build_pattern_fingerprints import fetch_recent_klines as fetch_klines
        except Exception as exc:  # noqa: BLE001
            return {'ok': False, 'error': f'K线取数模块不可用: {exc}'}

    def _sina(code):
        digits = ''.join(ch for ch in str(code or '') if ch.isdigit())
        return ('sh' if digits.startswith('6') else 'sz') + digits

    rows = []
    scanned = 0
    need = window_days + max(horizons) + 2
    for idx, it in enumerate(items, 1):
        code = str(it.get('code'))
        name = it.get('name') or code
        if log_cb:
            log_cb(f"[{idx}/{len(items)}] 形态回测 {name} {code}")
        try:
            _name, klines = fetch_klines(_sina(code), 250)
            closes = parse_close_series(klines)
            if len(closes) < need:
                rows.append({'code': code, 'name': name, 'rating': it.get('rating'),
                             'total_score': it.get('total_score'), 'pattern_score': None,
                             'win_rate': None, 'avg_return': None, 'sample_count': 0,
                             'note': '历史数据不足'})
                continue
            query = closes[-window_days:]
            samples = scan_series(closes, query, window_days=window_days, horizons=horizons)
            scanned += 1
            per_h = {}
            for h in horizons:
                rets = [s['returns'][h] for s in samples if h in s.get('returns', {})]
                per_h[str(h)] = _summarise_horizon(h, rets)
            primary = per_h.get('10') or per_h.get(str(horizons[0]))
            count = primary['count'] if primary else 0
            wr = primary['win_rate'] if (primary and count) else None
            ar = primary['avg_return'] if (primary and count) else None
            rows.append({
                'code': code, 'name': name, 'rating': it.get('rating'),
                'total_score': it.get('total_score'),
                'pattern_score': round(wr * 100, 1) if wr is not None else None,
                'win_rate': wr, 'avg_return': ar, 'sample_count': count,
                'horizons': per_h,
            })
        except Exception as exc:  # noqa: BLE001 — 单票失败不影响整体
            rows.append({'code': code, 'name': name, 'pattern_score': None,
                         'win_rate': None, 'avg_return': None, 'sample_count': 0,
                         'note': str(exc)[:80]})
    rows.sort(key=lambda r: (r.get('pattern_score') is None, -(r.get('pattern_score') or 0)))
    return {
        'ok': True, 'rows': rows, 'horizons': horizons,
        'window_days': window_days, 'scanned': scanned, 'total': len(items),
        'run': {'id': run['id'], 'run_at': run.get('run_at'),
                'date': str(run.get('run_date') or run.get('run_at') or '')[:10]},
    }


def _run_opportunity_pattern_backtest_job(job_id, params):
    _update_job(job_id, status='running', started_at=datetime.datetime.now().isoformat())
    _append_job_log(job_id, '开始机会挖掘形态回测')
    try:
        result = _opportunity_pattern_backtest(params, log_cb=lambda m: _append_job_log(job_id, m))
        if not result.get('ok'):
            raise RuntimeError(result.get('error') or '回测失败')
        _append_job_log(job_id, f"形态回测完成: 扫描 {result.get('scanned', 0)}/{result.get('total', 0)} 只")
        _update_job(job_id, status='finished',
                    finished_at=datetime.datetime.now().isoformat(), result=result)
    except Exception as exc:
        _append_job_log(job_id, f'机会形态回测失败: {exc}')
        _update_job(job_id, status='failed',
                    finished_at=datetime.datetime.now().isoformat(), error=str(exc))


def start_opportunity_pattern_backtest(run_id=None, date=None, top_n=20):
    """启动机会挖掘形态回测后台 job,返回 job 快照。"""
    params = {'run_id': run_id, 'date': date, 'top_n': top_n}
    job = JOB_SERVICE.start('opportunity_pattern_backtest', params, _run_opportunity_pattern_backtest_job)
    return {'success': True, 'job_id': job['id'], 'job': _get_job_snapshot(job['id'])}


def _run_pattern_backtest_job(job_id, params):
    _update_job(job_id, status='running', started_at=datetime.datetime.now().isoformat())
    _append_job_log(job_id, '开始同类图形回测')
    try:
        def log_cb(message):
            _append_job_log(job_id, message)

        result = PATTERN_SEARCH_SERVICE.backtest(params, progress_callback=log_cb)
        if not result.get('ok'):
            raise RuntimeError(result.get('error') or '回测失败')
        _append_job_log(
            job_id,
            f"回测完成: 命中 {result.get('sample_count', 0)} 个相似样本，"
            f"成功扫描 {result.get('candidates_scanned', 0)}/{result.get('candidates_total', 0)} 只股票"
        )
        _update_job(
            job_id,
            status='finished',
            finished_at=datetime.datetime.now().isoformat(),
            result=result,
        )
    except Exception as exc:
        _append_job_log(job_id, f'形态回测失败: {exc}')
        _update_job(
            job_id,
            status='failed',
            finished_at=datetime.datetime.now().isoformat(),
            error=str(exc),
        )


DESKTOP_PAGES = {
    'market_cloud': {
        'title': '大盘云图',
        'subtitle': '全 A 涨跌热力图、行业分组、资金流向与个股快看',
    },
    'features': {
        'title': '总览',
        'subtitle': '市场状态、核心指标、热榜与个股快搜',
    },
    'watchlist': {
        'title': '自选',
        'subtitle': '自选股实时行情、快捷分析与一键管理',
    },
    'command_center': {
        'title': '风险·机遇',
        'subtitle': '统筹机会与四层风险,撮合成出手/规避决策的作战大屏',
    },
    'capital_rankings': {
        'title': '资金榜单',
        'subtitle': '主力净流入榜与龙虎榜:单日/多日聚合、刷新补偿、多选股票用机会挖掘算法分析',
    },
    'futures': {
        'title': '股指期货',
        'subtitle': 'IF/IH/IC/IM 行情与基差、中金所前20席位多空单、会员净持仓榜与持仓趋势',
    },
    'screener': {
        'title': '条件选股',
        'subtitle': '组合行情、资金、盘口异动、吸筹、龙虎榜、机会分与技术形态条件,全市场筛选命中列表',
    },
    'paper_trading': {
        'title': '模拟盘',
        'subtitle': '现价买入、开盘价买入、输入价格买入、持仓盯市、成交流水与胜率回测',
    },
    'workbench': {
        'title': '分析工作台',
        'subtitle': '机会挖掘、批量分析、画布关系、任务日志与历史复盘',
    },
    'discovery_live': {
        'title': '挖掘引擎',
        'subtitle': '投资机会挖掘实时直播:多智能体并行流水线、阶段进度、现场日志与结果直达',
    },
    'patterns': {
        'title': '形态搜股',
        'subtitle': '手绘曲线或载入个股形态检索相似股票',
    },
    'star_orbit': {
        'title': '星轨图谱',
        'subtitle': '物理AI/AI产业链同心轨道图:概念板块→A股个股,可增删板块与股票',
    },
    'reports': {
        'title': '报告与健康',
        'subtitle': '本地报告与模块运行状态',
    },
    'settings': {
        'title': '后台配置',
        'subtitle': '配置 AI 分析模型、TuShare 数据源和模型运行状态',
    },
    'about': {
        'title': '关于',
        'subtitle': '免责声明 · 使用条款 · 数据来源',
    },
}

DESKTOP_PAGE_ALIASES = {
    'overview': 'features',  # 「总览」「功能总览」已合并为 features 单页
    'opportunities': 'workbench',  # 「投资机会挖掘」已并入「分析工作台」
}


def resolve_desktop_page(page):
    """Resolve legacy desktop URLs to the visible desktop information architecture."""
    return DESKTOP_PAGE_ALIASES.get(page, page)


def desktop_mode_enabled():
    """桌面(Tauri)模式:由 src-tauri/main.rs 注入 KRONOS_DESKTOP=tauri。"""
    return os.environ.get('KRONOS_DESKTOP') == 'tauri'


def open_external_url(url):
    """在系统默认浏览器打开外部链接,返回 (payload, status_code)。

    桌面 App 的 WKWebView 没有新窗口处理器,target=_blank 点击会被静默吞掉;
    前端拦截后交由本函数用 webbrowser 代开。非桌面模式一律拒绝,避免服务器
    部署场景被远端请求在宿主机弹浏览器。
    """
    target = str(url or '').strip()
    if not target.lower().startswith(('http://', 'https://')):
        return {'ok': False, 'error': '仅支持 http/https 链接'}, 400
    if not desktop_mode_enabled():
        return {'ok': False, 'error': '仅桌面模式支持系统浏览器代开'}, 403
    try:
        opened = webbrowser.open(target, new=2)
    except Exception as exc:
        return {'ok': False, 'error': f'打开浏览器失败: {exc}'}, 500
    if not opened:
        return {'ok': False, 'error': '系统未能打开默认浏览器'}, 500
    return {'ok': True, 'url': target}, 200


def get_server_config():
    """Return WebUI host, port, and debug mode from environment."""
    host = os.environ.get('KRONOS_HOST', '0.0.0.0')
    port = int(os.environ.get('KRONOS_PORT', '7070'))
    desktop_mode = desktop_mode_enabled()
    debug_env = os.environ.get('FLASK_DEBUG')
    debug = (debug_env not in ('0', 'false', 'False')) if debug_env is not None else not desktop_mode
    return host, port, debug


def _set_loaded_model(new_tokenizer, new_model, new_predictor):
    global tokenizer, model, predictor
    tokenizer = new_tokenizer
    model = new_model
    predictor = new_predictor


def _model_runtime_context():
    return ModelRuntimeContext(
        model_available=lambda: MODEL_AVAILABLE,
        get_predictor=lambda: predictor,
        set_loaded_model=_set_loaded_model,
        available_models=AVAILABLE_MODELS,
        user_root=USER_ROOT,
        kronos_cls=Kronos if MODEL_AVAILABLE else None,
        tokenizer_cls=KronosTokenizer if MODEL_AVAILABLE else None,
        predictor_cls=KronosPredictor if MODEL_AVAILABLE else None,
        pandas=pd,
        load_data_file=load_data_file,
        create_prediction_chart=create_prediction_chart,
        save_prediction_results=save_prediction_results,
    )
