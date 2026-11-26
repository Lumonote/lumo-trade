#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大盘体检 - 综合总览关系网络数据服务

汇总市场各维度数据，计算层级关系和联动规则，输出可视化所需的结构化数据。

核心逻辑:
1. 流动性层 (成交量/资金流/北向资金)
2. 情绪层 (涨跌停/新高新低/恐慌指数)
3. 估值层 (PE/PB/股债利差/破净率)
4. 风险层 (融资杠杆/板块过热/回撤)
5. 技术层 (趋势/RSI/均线/背离)

联动规则:
- 涨停数>50 且 融资增速>10% → 过热预警
- 新高占比>60% 且 PE<20 → 健康上涨
- 成交量>2x 且 主力流出 → 高位派发
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


def _safe_get(data: Dict, *keys, default=None):
    """安全获取嵌套字典的值"""
    for key in keys:
        if isinstance(data, dict):
            data = data.get(key)
        else:
            return default
    return data if data is not None else default


def _calc_status(value: Optional[float], low: float, high: float,
                 reverse: bool = False) -> str:
    """计算指标状态

    Args:
        value: 指标值
        low: 健康下限
        high: 健康上限
        reverse: 是否反向（值越高越危险）

    Returns:
        "green" | "yellow" | "red" | "gray"
    """
    if value is None:
        return "gray"

    if not reverse:
        # 正向指标: 低于下限=危险, 高于上限=好
        if value < low:
            return "red"
        elif value > high:
            return "green"
        else:
            return "yellow"
    else:
        # 反向指标: 低于下限=好, 高于上限=危险
        if value < low:
            return "green"
        elif value > high:
            return "red"
        else:
            return "yellow"


def _format_value(value: Optional[float], unit: str = "", decimals: int = 2) -> str:
    """格式化数值显示"""
    if value is None:
        return "—"

    # 处理大数字
    if abs(value) >= 1e8:
        return f"{value/1e8:.{decimals}f}亿{unit}"
    elif abs(value) >= 1e4:
        return f"{value/1e4:.{decimals}f}万{unit}"
    else:
        return f"{value:.{decimals}f}{unit}"


def build_market_relations(health_snapshot: Dict) -> Dict:
    """构建市场关系网络数据

    Args:
        health_snapshot: 来自 market_health_service.read_state() 的快照

    Returns:
        {
            "temperature": 75,  # 市场温度
            "as_of": "2026-11-21",
            "layers": {...},  # 五层数据
            "alerts": [...],  # 联动预警
            "summary": {...}  # 汇总统计
        }
    """
    # 提取基础数据
    fusion = health_snapshot.get("fusion", {})
    market = health_snapshot.get("market", {})
    ice = health_snapshot.get("ice", {})

    # 市场温度 (0-100)
    temperature = _safe_get(fusion, "temperature", default=50)

    # ============================================================
    # 层级 1: 流动性
    # ============================================================

    # 成交量比 (当日/250日均量)
    volume_ratio = _safe_get(health_snapshot, "sh000001", "volume250")

    # 主力净流入 (亿元) — 从资金榜单汇总
    main_inflow_raw = _safe_get(market, "main_inflow_amount")  # 单位: 元
    main_inflow = main_inflow_raw / 1e8 if main_inflow_raw is not None else None

    # 北向资金 (亿元) — 从宏观数据提取
    north_bound = None  # TODO: 接入 hk_hold 数据

    liquidity_status = "yellow"
    if volume_ratio is not None:
        if volume_ratio > 1.5:
            liquidity_status = "green"  # 放量
        elif volume_ratio < 0.5:
            liquidity_status = "red"  # 缩量

    liquidity = {
        "volume_ratio": {
            "value": volume_ratio,
            "display": f"{volume_ratio:.2f}x" if volume_ratio else "—",
            "status": _calc_status(volume_ratio, 0.8, 1.5)
        },
        "main_inflow": {
            "value": main_inflow,
            "display": _format_value(main_inflow, "亿", 1) if main_inflow else "—",
            "status": _calc_status(main_inflow, -50, 50, reverse=False)
        },
        "north_bound": {
            "value": north_bound,
            "display": _format_value(north_bound, "亿", 1) if north_bound else "—",
            "status": "gray"
        },
        "layer_status": liquidity_status
    }

    # ============================================================
    # 层级 2: 情绪
    # ============================================================

    limit_up = _safe_get(ice, "limit_up", "count", default=0)
    limit_down = _safe_get(ice, "limit_down", "count", default=0)
    limit_ratio = limit_up / max(limit_down, 1)  # 涨停/跌停比

    # 新高新低比 (近20日)
    new_high = _safe_get(ice, "new_high_20", "count", default=0)
    new_low = _safe_get(ice, "new_low_20", "count", default=0)
    new_hl_ratio = new_high / max(new_high + new_low, 1)

    # 恐慌指数 (0-100, 越高越恐慌)
    panic_index = 100 - temperature  # 简化: 用温度反推

    sentiment_status = "yellow"
    if limit_up > 50 or panic_index > 70:
        sentiment_status = "red"  # 过热或过度恐慌
    elif new_hl_ratio > 0.6 and panic_index < 40:
        sentiment_status = "green"  # 健康上涨

    sentiment = {
        "limit_up": {
            "value": limit_up,
            "display": str(limit_up),
            "status": _calc_status(limit_up, 20, 50, reverse=True)
        },
        "limit_down": {
            "value": limit_down,
            "display": str(limit_down),
            "status": _calc_status(limit_down, 10, 30, reverse=True)
        },
        "limit_ratio": {
            "value": limit_ratio,
            "display": f"{limit_ratio:.2f}",
            "status": _calc_status(limit_ratio, 0.5, 2.0)
        },
        "new_hl_ratio": {
            "value": new_hl_ratio,
            "display": f"{new_hl_ratio:.1%}",
            "status": _calc_status(new_hl_ratio, 0.3, 0.6)
        },
        "panic_index": {
            "value": panic_index,
            "display": f"{panic_index:.0f}",
            "status": _calc_status(panic_index, 30, 70, reverse=True)
        },
        "layer_status": sentiment_status
    }

    # ============================================================
    # 层级 3: 估值
    # ============================================================

    pe = _safe_get(market, "pe")
    pb = _safe_get(market, "pb")

    # 股债利差 (%) — 股息率 - 10年国债收益率
    bond_spread = _safe_get(market, "bond_spread")

    # 破净率
    broken_net_ratio = _safe_get(market, "pb_below_1_ratio")

    valuation_status = "yellow"
    if pe is not None and pb is not None:
        if pe < 15 and pb < 1.5:
            valuation_status = "green"  # 低估
        elif pe > 30 or pb > 3.0:
            valuation_status = "red"  # 高估

    valuation = {
        "pe": {
            "value": pe,
            "display": f"{pe:.1f}" if pe else "—",
            "status": _calc_status(pe, 15, 25, reverse=True)
        },
        "pb": {
            "value": pb,
            "display": f"{pb:.2f}" if pb else "—",
            "status": _calc_status(pb, 1.2, 2.5, reverse=True)
        },
        "bond_spread": {
            "value": bond_spread,
            "display": f"{bond_spread:.2f}%" if bond_spread else "—",
            "status": _calc_status(bond_spread, 2.0, 4.0)
        },
        "broken_net_ratio": {
            "value": broken_net_ratio,
            "display": f"{broken_net_ratio:.1%}" if broken_net_ratio else "—",
            "status": _calc_status(broken_net_ratio, 0.05, 0.15, reverse=True)
        },
        "layer_status": valuation_status
    }

    # ============================================================
    # 层级 4: 风险
    # ============================================================

    # 融资余额增速 (%) — 从融资面板提取
    margin_growth = _safe_get(health_snapshot, "融资", "net_change_5d")

    # 板块集中度 (Top5板块资金占比)
    sector_concentration = None  # TODO: 从板块深度提取

    # 最大回撤 (%)
    drawdown = _safe_get(health_snapshot, "sh000001", "drawdown")

    risk_status = "yellow"
    if margin_growth is not None and margin_growth > 10:
        risk_status = "red"  # 杠杆风险
    elif drawdown is not None and drawdown < -20:
        risk_status = "red"  # 深度回撤

    risk = {
        "margin_growth": {
            "value": margin_growth,
            "display": f"{margin_growth:+.1f}%" if margin_growth else "—",
            "status": _calc_status(margin_growth, -5, 5, reverse=True)
        },
        "sector_concentration": {
            "value": sector_concentration,
            "display": f"{sector_concentration:.1%}" if sector_concentration else "—",
            "status": "gray"
        },
        "drawdown": {
            "value": drawdown,
            "display": f"{drawdown:.1f}%" if drawdown else "—",
            "status": _calc_status(drawdown, -15, -5, reverse=True)
        },
        "layer_status": risk_status
    }

    # ============================================================
    # 层级 5: 技术
    # ============================================================

    # RSI
    rsi = _safe_get(health_snapshot, "sh000001", "rsi14")

    # 均线趋势
    trend = _safe_get(health_snapshot, "sh000001", "trend", default="数据不足")

    # 乖离率 (价格 vs MA20)
    dev20 = _safe_get(health_snapshot, "sh000001", "dev20")

    technical_status = "yellow"
    if rsi is not None:
        if rsi > 80:
            technical_status = "red"  # 超买
        elif rsi < 20:
            technical_status = "green"  # 超卖

    technical = {
        "rsi": {
            "value": rsi,
            "display": f"{rsi:.0f}" if rsi else "—",
            "status": _calc_status(rsi, 30, 70, reverse=True)
        },
        "trend": {
            "value": trend,
            "display": trend,
            "status": "green" if trend == "多头排列" else "red" if trend == "空头排列" else "yellow"
        },
        "dev20": {
            "value": dev20,
            "display": f"{dev20:+.1f}%" if dev20 else "—",
            "status": _calc_status(abs(dev20) if dev20 else None, 5, 10, reverse=True)
        },
        "layer_status": technical_status
    }

    # ============================================================
    # 联动规则检查
    # ============================================================

    alerts = []

    # 规则1: 涨停数>50 且 融资增速>10% → 过热预警
    if limit_up > 50 and margin_growth is not None and margin_growth > 10:
        alerts.append({
            "level": "danger",
            "title": "市场过热预警",
            "message": f"涨停数 {limit_up} 只 且 融资增速 {margin_growth:+.1f}%",
            "logic": "情绪过热 + 杠杆快速加大 → 短期回调风险",
            "nodes": ["limit_up", "margin_growth"]
        })

    # 规则2: 新高占比>60% 且 PE<20 → 健康上涨
    if new_hl_ratio > 0.6 and pe is not None and pe < 20:
        alerts.append({
            "level": "success",
            "title": "健康上涨信号",
            "message": f"新高占比 {new_hl_ratio:.1%} 且 市场PE {pe:.1f}",
            "logic": "广度扩散 + 估值合理 → 可持续上涨",
            "nodes": ["new_hl_ratio", "pe"]
        })

    # 规则3: 成交量>2x 且 主力流出>100亿 → 高位派发
    if volume_ratio is not None and volume_ratio > 2.0 and main_inflow is not None and main_inflow < -100:
        alerts.append({
            "level": "warning",
            "title": "高位派发风险",
            "message": f"成交量 {volume_ratio:.2f}x 且 主力流出 {abs(main_inflow):.0f}亿",
            "logic": "放量 + 资金撤离 → 主力出货",
            "nodes": ["volume_ratio", "main_inflow"]
        })

    # 规则4: RSI>80 且 涨停>30 → 短期超买
    if rsi is not None and rsi > 80 and limit_up > 30:
        alerts.append({
            "level": "warning",
            "title": "短期超买",
            "message": f"RSI {rsi:.0f} 且 涨停 {limit_up} 只",
            "logic": "技术超买 + 情绪亢奋 → 调整压力",
            "nodes": ["rsi", "limit_up"]
        })

    # 规则5: 破净率>15% 且 回撤>-20% → 底部区域
    if broken_net_ratio is not None and broken_net_ratio > 0.15 and drawdown is not None and drawdown < -20:
        alerts.append({
            "level": "success",
            "title": "底部特征显现",
            "message": f"破净率 {broken_net_ratio:.1%} 且 最大回撤 {drawdown:.1f}%",
            "logic": "估值低位 + 情绪低迷 → 左侧布局机会",
            "nodes": ["broken_net_ratio", "drawdown"]
        })

    # ============================================================
    # 汇总统计
    # ============================================================

    summary = {
        "total_layers": 5,
        "green_layers": sum(1 for layer in [liquidity, sentiment, valuation, risk, technical]
                           if layer["layer_status"] == "green"),
        "red_layers": sum(1 for layer in [liquidity, sentiment, valuation, risk, technical]
                         if layer["layer_status"] == "red"),
        "alert_count": len(alerts),
        "overall_status": "green" if temperature > 60 else "red" if temperature < 40 else "yellow"
    }

    return {
        "temperature": temperature,
        "as_of": health_snapshot.get("as_of", ""),
        "layers": {
            "liquidity": liquidity,
            "sentiment": sentiment,
            "valuation": valuation,
            "risk": risk,
            "technical": technical
        },
        "alerts": alerts,
        "summary": summary
    }
