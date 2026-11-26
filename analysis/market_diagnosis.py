#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大盘综合诊断与技术指标关联分析
=====================================

功能：
1. 市场状态诊断（牛市/熊市/震荡）
2. 技术指标关联分析（上涨对应什么、下跌对应什么）
3. 顶部/底部特征识别
4. 预警信号生成
5. 多维度综合评分

设计原则：
- 所有判断规则可解释、可回测
- 指标之间的关联基于历史统计规律
- 结论包含触发条件和置信度
"""

from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum
import math


class MarketPhase(Enum):
    """市场阶段"""
    BULL = "牛市"  # 多头排列，趋势向上
    BEAR = "熊市"  # 空头排列，趋势向下
    SIDEWAYS = "震荡"  # 均线交织，无明确方向
    UNKNOWN = "数据不足"


class SignalLevel(Enum):
    """信号强度"""
    STRONG = "强"
    MODERATE = "中等"
    WEAK = "弱"


@dataclass
class IndicatorSignal:
    """单个指标信号"""
    name: str
    value: float
    threshold: float
    direction: str  # "bullish" / "bearish" / "neutral"
    level: SignalLevel
    description: str


@dataclass
class MarketDiagnosis:
    """市场综合诊断结果"""
    phase: MarketPhase
    trend_score: float  # -100 to 100, 负=看空, 正=看多
    top_signals: List[IndicatorSignal]  # 顶部特征
    bottom_signals: List[IndicatorSignal]  # 底部特征
    correlations: Dict[str, List[str]]  # 指标关联关系
    warnings: List[str]  # 风险提示
    opportunities: List[str]  # 机会提示
    confidence: float  # 诊断置信度 0-100


# ============================================================
# 核心诊断逻辑
# ============================================================

def diagnose_market(metrics: Dict[str, Any]) -> MarketDiagnosis:
    """市场综合诊断

    Args:
        metrics: 来自 market_health.index_metrics() 的指标字典

    Returns:
        MarketDiagnosis 诊断结果
    """
    # 提取关键指标
    close = metrics.get("close")
    ma20 = metrics.get("ma20")
    ma60 = metrics.get("ma60")
    ma120 = metrics.get("ma120")
    ma250 = metrics.get("ma250")
    rsi14 = metrics.get("rsi14")
    position_60 = metrics.get("position_60")
    drawdown = metrics.get("drawdown")
    volatility20 = metrics.get("volatility20")
    volume250 = metrics.get("volume250")
    trend = metrics.get("trend", "数据不足")

    # 1. 判断市场阶段
    phase = _identify_phase(trend, ma20, ma60, ma120, ma250, close)

    # 2. 计算趋势评分
    trend_score = _calculate_trend_score(metrics)

    # 3. 识别顶部特征
    top_signals = _identify_top_signals(metrics)

    # 4. 识别底部特征
    bottom_signals = _identify_bottom_signals(metrics)

    # 5. 分析指标关联
    correlations = _analyze_correlations(metrics)

    # 6. 生成风险提示
    warnings = _generate_warnings(metrics, top_signals, phase)

    # 7. 生成机会提示
    opportunities = _generate_opportunities(metrics, bottom_signals, phase)

    # 8. 计算诊断置信度
    confidence = _calculate_confidence(metrics)

    return MarketDiagnosis(
        phase=phase,
        trend_score=trend_score,
        top_signals=top_signals,
        bottom_signals=bottom_signals,
        correlations=correlations,
        warnings=warnings,
        opportunities=opportunities,
        confidence=confidence
    )


def _identify_phase(trend: str, ma20, ma60, ma120, ma250, close) -> MarketPhase:
    """识别市场阶段"""
    if trend == "数据不足":
        return MarketPhase.UNKNOWN
    elif trend == "多头排列":
        return MarketPhase.BULL
    elif trend == "空头排列":
        return MarketPhase.BEAR
    else:
        return MarketPhase.SIDEWAYS


def _calculate_trend_score(metrics: Dict[str, Any]) -> float:
    """计算趋势评分 (-100 到 100)

    综合多个维度：
    - 均线排列 (40%)
    - RSI (20%)
    - 回撤程度 (20%)
    - 相对位置 (20%)
    """
    score = 0.0
    weights = 0.0

    # 1. 均线排列 (40%)
    trend = metrics.get("trend")
    if trend == "多头排列":
        score += 40
        weights += 40
    elif trend == "空头排列":
        score -= 40
        weights += 40
    elif trend == "均线交织":
        # 看20日均线方向
        ma20_rising = metrics.get("ma20_rising")
        if ma20_rising is True:
            score += 10
        elif ma20_rising is False:
            score -= 10
        weights += 40

    # 2. RSI (20%)
    rsi14 = metrics.get("rsi14")
    if rsi14 is not None:
        if rsi14 >= 70:
            score += 20 * min((rsi14 - 50) / 30, 1.0)  # 70-80区间线性映射
        elif rsi14 <= 30:
            score += 20 * max((rsi14 - 50) / 30, -1.0)  # 30-20区间线性映射
        else:
            score += 20 * (rsi14 - 50) / 20  # 30-70区间线性映射
        weights += 20

    # 3. 回撤程度 (20%)
    drawdown = metrics.get("drawdown")
    if drawdown is not None:
        # 回撤越大越悲观
        if drawdown >= -5:
            score += 20  # 接近历史高点
        elif drawdown >= -15:
            score += 10
        elif drawdown >= -25:
            score += 0
        elif drawdown >= -35:
            score -= 10
        else:
            score -= 20  # 深度回撤
        weights += 20

    # 4. 60日相对位置 (20%)
    position_60 = metrics.get("position_60")
    if position_60 is not None:
        # 位置越高越偏多
        score += 20 * (position_60 / 100 * 2 - 1)  # 0-100 映射到 -20 到 20
        weights += 20

    # 归一化到 -100 到 100
    if weights > 0:
        return (score / weights) * 100
    return 0.0


def _identify_top_signals(metrics: Dict[str, Any]) -> List[IndicatorSignal]:
    """识别顶部特征信号"""
    signals = []

    # 1. RSI 超买
    rsi14 = metrics.get("rsi14")
    if rsi14 is not None:
        if rsi14 >= 85:
            signals.append(IndicatorSignal(
                name="RSI极度超买",
                value=rsi14,
                threshold=85,
                direction="bearish",
                level=SignalLevel.STRONG,
                description=f"RSI={rsi14:.1f}，极度超买，回调风险大"
            ))
        elif rsi14 >= 75:
            signals.append(IndicatorSignal(
                name="RSI超买",
                value=rsi14,
                threshold=75,
                direction="bearish",
                level=SignalLevel.MODERATE,
                description=f"RSI={rsi14:.1f}，超买区域，注意回调"
            ))

    # 2. 高位缩量
    position_60 = metrics.get("position_60")
    volume250 = metrics.get("volume250")
    if position_60 is not None and volume250 is not None:
        if position_60 >= 80 and volume250 < 0.8:
            signals.append(IndicatorSignal(
                name="高位缩量",
                value=volume250,
                threshold=0.8,
                direction="bearish",
                level=SignalLevel.STRONG,
                description=f"60日位置={position_60:.1f}%，量比={volume250:.2f}，高位缩量预警"
            ))

    # 3. 接近历史高点 + 回撤极小
    drawdown = metrics.get("drawdown")
    if drawdown is not None and drawdown >= -2:
        signals.append(IndicatorSignal(
            name="接近历史高点",
            value=drawdown,
            threshold=-2,
            direction="bearish",
            level=SignalLevel.MODERATE,
            description=f"距历史高点仅{abs(drawdown):.1f}%，获利盘压力"
            ))

    # 4. 20日新高 + RSI高位
    new_low20 = metrics.get("new_low20")
    if new_low20 is False and rsi14 is not None and rsi14 >= 70:
        # 注意：new_low20=False 说明不是新低，可能是高位
        close = metrics.get("close")
        ma20 = metrics.get("ma20")
        if close is not None and ma20 is not None and close > ma20 * 1.1:
            signals.append(IndicatorSignal(
                name="高位背离",
                value=close / ma20 if ma20 else 0,
                threshold=1.1,
                direction="bearish",
                level=SignalLevel.WEAK,
                description="价格高于MA20超10%，RSI高位，短期超涨"
            ))

    # 5. 波动率放大 + 高位
    volatility20 = metrics.get("volatility20")
    if volatility20 is not None and position_60 is not None:
        if volatility20 >= 30 and position_60 >= 70:
            signals.append(IndicatorSignal(
                name="高位波动加剧",
                value=volatility20,
                threshold=30,
                direction="bearish",
                level=SignalLevel.MODERATE,
                description=f"波动率={volatility20:.1f}%，高位震荡加剧"
            ))

    return signals


def _identify_bottom_signals(metrics: Dict[str, Any]) -> List[IndicatorSignal]:
    """识别底部特征信号"""
    signals = []

    # 1. RSI 超卖
    rsi14 = metrics.get("rsi14")
    if rsi14 is not None:
        if rsi14 <= 20:
            signals.append(IndicatorSignal(
                name="RSI极度超卖",
                value=rsi14,
                threshold=20,
                direction="bullish",
                level=SignalLevel.STRONG,
                description=f"RSI={rsi14:.1f}，极度超卖，反弹概率大"
            ))
        elif rsi14 <= 30:
            signals.append(IndicatorSignal(
                name="RSI超卖",
                value=rsi14,
                threshold=30,
                direction="bullish",
                level=SignalLevel.MODERATE,
                description=f"RSI={rsi14:.1f}，超卖区域，关注反弹"
            ))

    # 2. 深度回撤
    drawdown = metrics.get("drawdown")
    if drawdown is not None:
        if drawdown <= -30:
            signals.append(IndicatorSignal(
                name="深度回撤",
                value=drawdown,
                threshold=-30,
                direction="bullish",
                level=SignalLevel.STRONG,
                description=f"回撤{abs(drawdown):.1f}%，历史低位区域"
            ))
        elif drawdown <= -20:
            signals.append(IndicatorSignal(
                name="显著回撤",
                value=drawdown,
                threshold=-20,
                direction="bullish",
                level=SignalLevel.MODERATE,
                description=f"回撤{abs(drawdown):.1f}%，中期调整充分"
            ))

    # 3. 低位放量
    position_60 = metrics.get("position_60")
    volume250 = metrics.get("volume250")
    if position_60 is not None and volume250 is not None:
        if position_60 <= 20 and volume250 >= 1.5:
            signals.append(IndicatorSignal(
                name="低位放量",
                value=volume250,
                threshold=1.5,
                direction="bullish",
                level=SignalLevel.STRONG,
                description=f"60日位置={position_60:.1f}%，量比={volume250:.2f}，底部放量"
            ))

    # 4. 20日新低 + RSI低位
    new_low20 = metrics.get("new_low20")
    if new_low20 is True and rsi14 is not None and rsi14 <= 35:
        signals.append(IndicatorSignal(
            name="恐慌性新低",
            value=rsi14,
            threshold=35,
            direction="bullish",
            level=SignalLevel.STRONG,
            description="创20日新低且RSI超卖，恐慌情绪极端"
        ))

    # 5. 均线多头排列 + 低位
    trend = metrics.get("trend")
    if trend == "多头排列" and position_60 is not None and position_60 <= 30:
        signals.append(IndicatorSignal(
            name="多头排列回踩",
            value=position_60,
            threshold=30,
            direction="bullish",
            level=SignalLevel.MODERATE,
            description=f"均线多头但回踩至{position_60:.1f}%低位，潜在支撑"
        ))

    return signals


def _analyze_correlations(metrics: Dict[str, Any]) -> Dict[str, List[str]]:
    """分析技术指标关联关系

    Returns:
        关联字典，格式：
        {
            "上涨时常伴随": [...],
            "下跌时常伴随": [...],
            "超买时常伴随": [...],
            "超卖时常伴随": [...]
        }
    """
    correlations = {
        "上涨时常伴随": [],
        "下跌时常伴随": [],
        "超买时常伴随": [],
        "超卖时常伴随": [],
        "高位时常伴随": [],
        "低位时常伴随": []
    }

    ret5 = metrics.get("ret5")
    rsi14 = metrics.get("rsi14")
    position_60 = metrics.get("position_60")
    volume250 = metrics.get("volume250")
    ma20_rising = metrics.get("ma20_rising")
    volatility20 = metrics.get("volatility20")

    # 上涨关联
    if ret5 is not None and ret5 > 5:
        if volume250 is not None and volume250 > 1.2:
            correlations["上涨时常伴随"].append("成交量放大(量比>1.2)")
        if rsi14 is not None and rsi14 > 60:
            correlations["上涨时常伴随"].append(f"RSI走强(当前{rsi14:.1f})")
        if ma20_rising:
            correlations["上涨时常伴随"].append("20日均线上行")

    # 下跌关联
    if ret5 is not None and ret5 < -5:
        if volume250 is not None and volume250 > 1.3:
            correlations["下跌时常伴随"].append("恐慌性放量(量比>1.3)")
        elif volume250 is not None and volume250 < 0.8:
            correlations["下跌时常伴随"].append("缩量阴跌(量比<0.8)")
        if rsi14 is not None and rsi14 < 40:
            correlations["下跌时常伴随"].append(f"RSI走弱(当前{rsi14:.1f})")

    # 超买关联
    if rsi14 is not None and rsi14 >= 70:
        if position_60 is not None and position_60 >= 70:
            correlations["超买时常伴随"].append(f"高位运行(60日位置{position_60:.1f}%)")
        if volume250 is not None and volume250 < 0.9:
            correlations["超买时常伴随"].append("成交量萎缩(顶背离)")
        if volatility20 is not None and volatility20 > 25:
            correlations["超买时常伴随"].append("波动率上升(震荡加剧)")

    # 超卖关联
    if rsi14 is not None and rsi14 <= 30:
        if position_60 is not None and position_60 <= 30:
            correlations["超卖时常伴随"].append(f"低位运行(60日位置{position_60:.1f}%)")
        if volume250 is not None and volume250 > 1.5:
            correlations["超卖时常伴随"].append("恐慌性放量(底部特征)")

    # 高位关联
    if position_60 is not None and position_60 >= 80:
        correlations["高位时常伴随"].append(f"获利盘丰厚(60日位置{position_60:.1f}%)")
        if volume250 is not None and volume250 < 0.8:
            correlations["高位时常伴随"].append("缩量(资金观望)")

    # 低位关联
    if position_60 is not None and position_60 <= 20:
        correlations["低位时常伴随"].append(f"深度调整(60日位置{position_60:.1f}%)")
        drawdown = metrics.get("drawdown")
        if drawdown is not None and drawdown <= -25:
            correlations["低位时常伴随"].append(f"大幅回撤(距高点{abs(drawdown):.1f}%)")

    # 移除空列表
    return {k: v for k, v in correlations.items() if v}


def _generate_warnings(metrics: Dict[str, Any], top_signals: List[IndicatorSignal],
                       phase: MarketPhase) -> List[str]:
    """生成风险提示"""
    warnings = []

    # 强烈顶部信号
    strong_top = [s for s in top_signals if s.level == SignalLevel.STRONG]
    if len(strong_top) >= 2:
        warnings.append(f"⚠️ 检测到{len(strong_top)}个强顶部信号，回调风险极高")
    elif len(strong_top) == 1:
        warnings.append(f"⚠️ {strong_top[0].description}")

    # 牛市末期特征
    if phase == MarketPhase.BULL:
        rsi14 = metrics.get("rsi14")
        position_60 = metrics.get("position_60")
        volume250 = metrics.get("volume250")

        if rsi14 and rsi14 >= 75 and position_60 and position_60 >= 80:
            warnings.append("⚠️ 牛市末期特征：高位+超买，建议逐步减仓")

        if volume250 and volume250 < 0.7 and position_60 and position_60 >= 70:
            warnings.append("⚠️ 高位缩量，资金观望情绪浓厚")

    # 波动率异常
    volatility20 = metrics.get("volatility20")
    if volatility20 and volatility20 >= 35:
        warnings.append(f"⚠️ 波动率高达{volatility20:.1f}%，市场不确定性大")

    # 空头排列 + 仍在高位
    if phase == MarketPhase.BEAR:
        position_60 = metrics.get("position_60")
        if position_60 and position_60 >= 50:
            warnings.append("⚠️ 空头排列但仍在中高位，下跌空间较大")

    return warnings


def _generate_opportunities(metrics: Dict[str, Any], bottom_signals: List[IndicatorSignal],
                           phase: MarketPhase) -> List[str]:
    """生成机会提示"""
    opportunities = []

    # 强烈底部信号
    strong_bottom = [s for s in bottom_signals if s.level == SignalLevel.STRONG]
    if len(strong_bottom) >= 2:
        opportunities.append(f"✓ 检测到{len(strong_bottom)}个强底部信号，反弹概率大")
    elif len(strong_bottom) == 1:
        opportunities.append(f"✓ {strong_bottom[0].description}")

    # 熊市末期特征
    if phase == MarketPhase.BEAR:
        rsi14 = metrics.get("rsi14")
        position_60 = metrics.get("position_60")
        volume250 = metrics.get("volume250")

        if rsi14 and rsi14 <= 25 and position_60 and position_60 <= 20:
            opportunities.append("✓ 熊市末期特征：低位+超卖，逐步布局时机")

        if volume250 and volume250 >= 1.5 and position_60 and position_60 <= 30:
            opportunities.append("✓ 低位放量，可能是筑底信号")

    # 多头排列 + 回踩支撑
    if phase == MarketPhase.BULL:
        position_60 = metrics.get("position_60")
        ma20_rising = metrics.get("ma20_rising")

        if position_60 and position_60 <= 40 and ma20_rising:
            opportunities.append("✓ 多头排列回踩，潜在低吸机会")

    # 超跌反弹
    ret20 = metrics.get("ret20")
    rsi14 = metrics.get("rsi14")
    if ret20 and ret20 <= -15 and rsi14 and rsi14 <= 30:
        opportunities.append("✓ 超跌反弹窗口，短期技术性反抽")

    return opportunities


def _calculate_confidence(metrics: Dict[str, Any]) -> float:
    """计算诊断置信度

    基于数据完整性和市场状态清晰度
    """
    confidence = 100.0

    # 检查关键指标是否存在
    required_fields = ["close", "ma20", "ma60", "ma120", "ma250", "rsi14", "trend"]
    missing = sum(1 for field in required_fields if metrics.get(field) is None)

    if missing > 0:
        confidence -= missing * 10  # 每缺一个关键指标扣10分

    # 市场状态不清晰时降低置信度
    trend = metrics.get("trend")
    if trend == "数据不足":
        confidence -= 30
    elif trend == "均线交织":
        confidence -= 10  # 震荡市难判断

    # 数据点不足时降低置信度
    bars = metrics.get("bars", 0)
    if bars < 250:
        confidence -= (250 - bars) / 5  # 每缺5根K线扣1分

    return max(0.0, min(100.0, confidence))


# ============================================================
# 格式化输出
# ============================================================

def format_diagnosis_report(diagnosis: MarketDiagnosis, metrics: Dict[str, Any]) -> str:
    """格式化诊断报告为可读文本

    Args:
        diagnosis: 诊断结果
        metrics: 原始指标数据

    Returns:
        格式化的报告文本
    """
    lines = []
    lines.append("=" * 60)
    lines.append("大盘综合诊断报告")
    lines.append("=" * 60)
    lines.append("")

    # 基本信息
    symbol = metrics.get("symbol", "unknown")
    name = metrics.get("name", "")
    as_of = metrics.get("as_of", "")
    close = metrics.get("close", 0)

    lines.append(f"指数：{name} ({symbol})")
    lines.append(f"日期：{as_of}")
    lines.append(f"收盘：{close:.2f}")
    lines.append("")

    # 市场阶段与趋势评分
    lines.append(f"【市场阶段】{diagnosis.phase.value}")
    lines.append(f"【趋势评分】{diagnosis.trend_score:+.1f} / 100")

    if diagnosis.trend_score >= 50:
        lines.append("  └─ 强势看多，趋势向上")
    elif diagnosis.trend_score >= 20:
        lines.append("  └─ 偏多，震荡上行")
    elif diagnosis.trend_score >= -20:
        lines.append("  └─ 中性，方向不明")
    elif diagnosis.trend_score >= -50:
        lines.append("  └─ 偏空，震荡下行")
    else:
        lines.append("  └─ 强势看空，趋势向下")

    lines.append(f"【置信度】{diagnosis.confidence:.0f}%")
    lines.append("")

    # 顶部信号
    if diagnosis.top_signals:
        lines.append("【顶部特征】")
        for signal in diagnosis.top_signals:
            emoji = "🔴" if signal.level == SignalLevel.STRONG else "🟠" if signal.level == SignalLevel.MODERATE else "🟡"
            lines.append(f"  {emoji} {signal.description}")
        lines.append("")

    # 底部信号
    if diagnosis.bottom_signals:
        lines.append("【底部特征】")
        for signal in diagnosis.bottom_signals:
            emoji = "🟢" if signal.level == SignalLevel.STRONG else "🟡" if signal.level == SignalLevel.MODERATE else "⚪"
            lines.append(f"  {emoji} {signal.description}")
        lines.append("")

    # 指标关联
    if diagnosis.correlations:
        lines.append("【指标关联分析】")
        for category, items in diagnosis.correlations.items():
            if items:
                lines.append(f"  ▸ {category}:")
                for item in items:
                    lines.append(f"    • {item}")
        lines.append("")

    # 风险提示
    if diagnosis.warnings:
        lines.append("【风险提示】")
        for warning in diagnosis.warnings:
            lines.append(f"  {warning}")
        lines.append("")

    # 机会提示
    if diagnosis.opportunities:
        lines.append("【机会提示】")
        for opp in diagnosis.opportunities:
            lines.append(f"  {opp}")
        lines.append("")

    lines.append("=" * 60)
    lines.append("注：本诊断基于历史统计规律，不构成投资建议")
    lines.append("=" * 60)

    return "\n".join(lines)
