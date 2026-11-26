#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""市场指标关联树形网络 - 可视化因果关系
=====================================

功能：
1. 构建技术指标之间的因果关系树
2. 识别市场涨跌的触发条件链
3. 生成可视化的关联网络图
4. 支持多层级递归推导

设计思路：
根节点 = 市场涨/跌
├─ 一级因素（直接原因）
│   ├─ 二级因素（支撑条件）
│   │   └─ 三级因素（底层信号）
│   └─ 二级因素
└─ 一级因素

示例：
市场上涨 →
  ├─ 成交量放大 → 资金流入 → 情绪转暖
  ├─ RSI上升 → 买盘占优 → 多头排列
  └─ 均线金叉 → 趋势反转 → 技术支撑
"""

from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
import json


class NodeType(Enum):
    """节点类型"""
    ROOT = "root"  # 根节点（市场状态）
    PRIMARY = "primary"  # 一级因素
    SECONDARY = "secondary"  # 二级因素
    TERTIARY = "tertiary"  # 三级因素


class Sentiment(Enum):
    """情绪方向"""
    BULLISH = "bullish"  # 看涨
    BEARISH = "bearish"  # 看跌
    NEUTRAL = "neutral"  # 中性


@dataclass
class TreeNode:
    """关联树节点"""
    id: str
    label: str  # 显示文本
    type: NodeType
    sentiment: Sentiment
    strength: float  # 信号强度 0-100
    active: bool  # 当前是否触发
    description: str = ""
    children: List['TreeNode'] = field(default_factory=list)

    def to_dict(self) -> dict:
        """转换为字典格式（供前端渲染）"""
        return {
            "id": self.id,
            "label": self.label,
            "type": self.type.value,
            "sentiment": self.sentiment.value,
            "strength": self.strength,
            "active": self.active,
            "description": self.description,
            "children": [child.to_dict() for child in self.children]
        }


@dataclass
class MarketCorrelationTree:
    """市场关联关系树"""
    bullish_root: TreeNode  # 看涨关系树
    bearish_root: TreeNode  # 看跌关系树
    neutral_root: TreeNode  # 中性关系树

    def to_dict(self) -> dict:
        """转换为完整的树形结构"""
        return {
            "bullish": self.bullish_root.to_dict(),
            "bearish": self.bearish_root.to_dict(),
            "neutral": self.neutral_root.to_dict()
        }


# ============================================================
# 核心构建逻辑
# ============================================================

def build_correlation_tree(metrics: Dict) -> MarketCorrelationTree:
    """构建完整的市场关联树

    Args:
        metrics: 来自 market_health.index_metrics() 的指标字典

    Returns:
        MarketCorrelationTree 包含看涨/看跌/中性三棵树
    """
    # 提取关键指标
    close = metrics.get("close", 0)
    ret5 = metrics.get("ret5")
    ret20 = metrics.get("ret20")
    rsi14 = metrics.get("rsi14")
    ma20 = metrics.get("ma20")
    ma60 = metrics.get("ma60")
    ma120 = metrics.get("ma120")
    ma250 = metrics.get("ma250")
    position_60 = metrics.get("position_60")
    drawdown = metrics.get("drawdown")
    volume250 = metrics.get("volume250")
    volume5 = metrics.get("volume5")
    volatility20 = metrics.get("volatility20")
    trend = metrics.get("trend", "数据不足")
    ma20_rising = metrics.get("ma20_rising")
    new_low20 = metrics.get("new_low20")

    # 1. 构建看涨关系树
    bullish_root = _build_bullish_tree(metrics)

    # 2. 构建看跌关系树
    bearish_root = _build_bearish_tree(metrics)

    # 3. 构建中性关系树
    neutral_root = _build_neutral_tree(metrics)

    return MarketCorrelationTree(
        bullish_root=bullish_root,
        bearish_root=bearish_root,
        neutral_root=neutral_root
    )


def _build_bullish_tree(metrics: Dict) -> TreeNode:
    """构建看涨关系树

    树形结构：
    市场上涨 (Root)
    ├─ 趋势向上 (Primary)
    │   ├─ 多头排列 (Secondary)
    │   │   ├─ 均线向上 (Tertiary)
    │   │   └─ 突破压力 (Tertiary)
    │   └─ 动量强劲 (Secondary)
    │       ├─ RSI走强 (Tertiary)
    │       └─ MACD金叉 (Tertiary)
    ├─ 资金流入 (Primary)
    │   ├─ 成交量放大 (Secondary)
    │   ├─ 主力买入 (Secondary)
    │   └─ 情绪转暖 (Secondary)
    └─ 位置安全 (Primary)
        ├─ 低位启动 (Secondary)
        ├─ 超跌反弹 (Secondary)
        └─ 回踩支撑 (Secondary)
    """
    close = metrics.get("close", 0)
    ret5 = metrics.get("ret5")
    ret20 = metrics.get("ret20")
    rsi14 = metrics.get("rsi14")
    ma20 = metrics.get("ma20")
    position_60 = metrics.get("position_60")
    volume250 = metrics.get("volume250")
    trend = metrics.get("trend")
    ma20_rising = metrics.get("ma20_rising")
    drawdown = metrics.get("drawdown")

    # 根节点
    root = TreeNode(
        id="bull_root",
        label="市场上涨",
        type=NodeType.ROOT,
        sentiment=Sentiment.BULLISH,
        strength=_calculate_bullish_strength(metrics),
        active=ret5 is not None and ret5 > 2,
        description="5日涨幅 > 2%"
    )

    # === 一级因素 1: 趋势向上 ===
    trend_primary = TreeNode(
        id="bull_trend",
        label="趋势向上",
        type=NodeType.PRIMARY,
        sentiment=Sentiment.BULLISH,
        strength=_calculate_trend_strength(metrics),
        active=trend == "多头排列" or (ma20_rising is True),
        description="均线多头排列或20日均线上行"
    )

    # 二级: 多头排列
    if trend == "多头排列":
        ma_alignment = TreeNode(
            id="bull_ma_alignment",
            label="多头排列",
            type=NodeType.SECONDARY,
            sentiment=Sentiment.BULLISH,
            strength=90.0,
            active=True,
            description=f"价格 > MA20 > MA60 > MA120 > MA250"
        )

        # 三级: 均线向上
        if ma20_rising:
            ma_rising = TreeNode(
                id="bull_ma_rising",
                label="均线向上",
                type=NodeType.TERTIARY,
                sentiment=Sentiment.BULLISH,
                strength=80.0,
                active=True,
                description="MA20 持续上行"
            )
            ma_alignment.children.append(ma_rising)

        # 三级: 突破压力
        if close and ma60 and close > ma60:
            breakthrough = TreeNode(
                id="bull_breakthrough",
                label="突破压力位",
                type=NodeType.TERTIARY,
                sentiment=Sentiment.BULLISH,
                strength=70.0,
                active=True,
                description=f"收盘 {close:.2f} > MA60 {ma60:.2f}"
            )
            ma_alignment.children.append(breakthrough)

        trend_primary.children.append(ma_alignment)

    # 二级: 动量强劲
    momentum_secondary = TreeNode(
        id="bull_momentum",
        label="动量强劲",
        type=NodeType.SECONDARY,
        sentiment=Sentiment.BULLISH,
        strength=_calculate_momentum_strength(rsi14, ret5),
        active=rsi14 is not None and rsi14 > 55,
        description="买盘力量占优"
    )

    # 三级: RSI走强
    if rsi14 and 50 < rsi14 < 75:
        rsi_strong = TreeNode(
            id="bull_rsi",
            label="RSI走强",
            type=NodeType.TERTIARY,
            sentiment=Sentiment.BULLISH,
            strength=min((rsi14 - 50) * 2, 100),
            active=True,
            description=f"RSI={rsi14:.1f}，健康强势区"
        )
        momentum_secondary.children.append(rsi_strong)

    # 三级: 近期涨幅
    if ret5 and ret5 > 3:
        recent_gain = TreeNode(
            id="bull_recent_gain",
            label="短期上涨",
            type=NodeType.TERTIARY,
            sentiment=Sentiment.BULLISH,
            strength=min(ret5 * 5, 100),
            active=True,
            description=f"5日涨幅 {ret5:.1f}%"
        )
        momentum_secondary.children.append(recent_gain)

    trend_primary.children.append(momentum_secondary)
    root.children.append(trend_primary)

    # === 一级因素 2: 资金流入 ===
    capital_primary = TreeNode(
        id="bull_capital",
        label="资金流入",
        type=NodeType.PRIMARY,
        sentiment=Sentiment.BULLISH,
        strength=_calculate_capital_strength(volume250),
        active=volume250 is not None and volume250 > 1.2,
        description="成交量明显放大"
    )

    # 二级: 成交量放大
    if volume250 and volume250 > 1.2:
        volume_up = TreeNode(
            id="bull_volume",
            label="成交量放大",
            type=NodeType.SECONDARY,
            sentiment=Sentiment.BULLISH,
            strength=min((volume250 - 1) * 50, 100),
            active=True,
            description=f"量比={volume250:.2f}"
        )

        # 三级: 量价配合
        if ret5 and ret5 > 2:
            volume_price_match = TreeNode(
                id="bull_volume_price",
                label="量价配合",
                type=NodeType.TERTIARY,
                sentiment=Sentiment.BULLISH,
                strength=85.0,
                active=True,
                description="上涨放量，健康上涨"
            )
            volume_up.children.append(volume_price_match)

        capital_primary.children.append(volume_up)

    # 二级: 情绪转暖
    if rsi14 and rsi14 > 50:
        sentiment_warm = TreeNode(
            id="bull_sentiment",
            label="情绪转暖",
            type=NodeType.SECONDARY,
            sentiment=Sentiment.BULLISH,
            strength=min((rsi14 - 40) * 2.5, 100),
            active=True,
            description="市场情绪由悲观转乐观"
        )
        capital_primary.children.append(sentiment_warm)

    root.children.append(capital_primary)

    # === 一级因素 3: 位置安全 ===
    position_primary = TreeNode(
        id="bull_position",
        label="位置安全",
        type=NodeType.PRIMARY,
        sentiment=Sentiment.BULLISH,
        strength=_calculate_position_safety(position_60, drawdown),
        active=position_60 is not None and position_60 < 50,
        description="相对低位，上涨空间大"
    )

    # 二级: 低位启动
    if position_60 and position_60 < 30:
        low_start = TreeNode(
            id="bull_low_start",
            label="低位启动",
            type=NodeType.SECONDARY,
            sentiment=Sentiment.BULLISH,
            strength=100 - position_60 * 2,
            active=True,
            description=f"60日位置={position_60:.1f}%，低位"
        )

        # 三级: 深度超跌
        if drawdown and drawdown < -25:
            oversold = TreeNode(
                id="bull_oversold",
                label="超跌反弹",
                type=NodeType.TERTIARY,
                sentiment=Sentiment.BULLISH,
                strength=min(abs(drawdown) * 2, 100),
                active=True,
                description=f"回撤{abs(drawdown):.1f}%，超跌"
            )
            low_start.children.append(oversold)

        position_primary.children.append(low_start)

    # 二级: 回踩支撑
    if trend == "多头排列" and position_60 and 30 < position_60 < 50:
        pullback = TreeNode(
            id="bull_pullback",
            label="回踩支撑",
            type=NodeType.SECONDARY,
            sentiment=Sentiment.BULLISH,
            strength=70.0,
            active=True,
            description="多头排列回踩，低吸机会"
        )
        position_primary.children.append(pullback)

    root.children.append(position_primary)

    return root


def _build_bearish_tree(metrics: Dict) -> TreeNode:
    """构建看跌关系树

    树形结构：
    市场下跌 (Root)
    ├─ 趋势向下 (Primary)
    │   ├─ 空头排列 (Secondary)
    │   │   ├─ 均线下行 (Tertiary)
    │   │   └─ 跌破支撑 (Tertiary)
    │   └─ 动量转弱 (Secondary)
    │       ├─ RSI走弱 (Tertiary)
    │       └─ 死叉出现 (Tertiary)
    ├─ 资金流出 (Primary)
    │   ├─ 成交量萎缩 (Secondary)
    │   ├─ 主力撤离 (Secondary)
    │   └─ 情绪转冷 (Secondary)
    └─ 位置危险 (Primary)
        ├─ 高位回落 (Secondary)
        ├─ 超买回调 (Secondary)
        └─ 压力位承压 (Secondary)
    """
    close = metrics.get("close", 0)
    ret5 = metrics.get("ret5")
    ret20 = metrics.get("ret20")
    rsi14 = metrics.get("rsi14")
    ma20 = metrics.get("ma20")
    ma60 = metrics.get("ma60")
    position_60 = metrics.get("position_60")
    volume250 = metrics.get("volume250")
    trend = metrics.get("trend")
    ma20_rising = metrics.get("ma20_rising")
    drawdown = metrics.get("drawdown")

    # 根节点
    root = TreeNode(
        id="bear_root",
        label="市场下跌",
        type=NodeType.ROOT,
        sentiment=Sentiment.BEARISH,
        strength=_calculate_bearish_strength(metrics),
        active=ret5 is not None and ret5 < -2,
        description="5日跌幅 > 2%"
    )

    # === 一级因素 1: 趋势向下 ===
    trend_primary = TreeNode(
        id="bear_trend",
        label="趋势向下",
        type=NodeType.PRIMARY,
        sentiment=Sentiment.BEARISH,
        strength=100 - _calculate_trend_strength(metrics),
        active=trend == "空头排列" or (ma20_rising is False),
        description="均线空头排列或20日均线下行"
    )

    # 二级: 空头排列
    if trend == "空头排列":
        ma_bearish = TreeNode(
            id="bear_ma_alignment",
            label="空头排列",
            type=NodeType.SECONDARY,
            sentiment=Sentiment.BEARISH,
            strength=90.0,
            active=True,
            description="价格 < MA20 < MA60 < MA120 < MA250"
        )

        # 三级: 均线下行
        if ma20_rising is False:
            ma_falling = TreeNode(
                id="bear_ma_falling",
                label="均线下行",
                type=NodeType.TERTIARY,
                sentiment=Sentiment.BEARISH,
                strength=80.0,
                active=True,
                description="MA20 持续下行"
            )
            ma_bearish.children.append(ma_falling)

        # 三级: 跌破支撑
        if close and ma60 and close < ma60:
            breakdown = TreeNode(
                id="bear_breakdown",
                label="跌破支撑",
                type=NodeType.TERTIARY,
                sentiment=Sentiment.BEARISH,
                strength=75.0,
                active=True,
                description=f"收盘 {close:.2f} < MA60 {ma60:.2f}"
            )
            ma_bearish.children.append(breakdown)

        trend_primary.children.append(ma_bearish)

    # 二级: 动量转弱
    momentum_secondary = TreeNode(
        id="bear_momentum",
        label="动量转弱",
        type=NodeType.SECONDARY,
        sentiment=Sentiment.BEARISH,
        strength=100 - _calculate_momentum_strength(rsi14, ret5),
        active=rsi14 is not None and rsi14 < 45,
        description="卖盘力量占优"
    )

    # 三级: RSI走弱
    if rsi14 and 25 < rsi14 < 50:
        rsi_weak = TreeNode(
            id="bear_rsi",
            label="RSI走弱",
            type=NodeType.TERTIARY,
            sentiment=Sentiment.BEARISH,
            strength=(50 - rsi14) * 2,
            active=True,
            description=f"RSI={rsi14:.1f}，弱势区"
        )
        momentum_secondary.children.append(rsi_weak)

    # 三级: 近期跌幅
    if ret5 and ret5 < -3:
        recent_loss = TreeNode(
            id="bear_recent_loss",
            label="短期下跌",
            type=NodeType.TERTIARY,
            sentiment=Sentiment.BEARISH,
            strength=min(abs(ret5) * 5, 100),
            active=True,
            description=f"5日跌幅 {abs(ret5):.1f}%"
        )
        momentum_secondary.children.append(recent_loss)

    trend_primary.children.append(momentum_secondary)
    root.children.append(trend_primary)

    # === 一级因素 2: 资金流出 ===
    capital_primary = TreeNode(
        id="bear_capital",
        label="资金流出",
        type=NodeType.PRIMARY,
        sentiment=Sentiment.BEARISH,
        strength=100 - _calculate_capital_strength(volume250),
        active=volume250 is not None and volume250 < 0.8,
        description="成交量萎缩或恐慌放量"
    )

    # 二级: 成交量异常
    if volume250:
        if volume250 < 0.8:
            # 缩量下跌
            volume_shrink = TreeNode(
                id="bear_volume_shrink",
                label="缩量阴跌",
                type=NodeType.SECONDARY,
                sentiment=Sentiment.BEARISH,
                strength=(1 - volume250) * 100,
                active=True,
                description=f"量比={volume250:.2f}，资金观望"
            )
            capital_primary.children.append(volume_shrink)
        elif volume250 > 1.5 and ret5 and ret5 < -2:
            # 恐慌放量
            panic_volume = TreeNode(
                id="bear_panic_volume",
                label="恐慌放量",
                type=NodeType.SECONDARY,
                sentiment=Sentiment.BEARISH,
                strength=min((volume250 - 1) * 40, 100),
                active=True,
                description=f"量比={volume250:.2f}，下跌放量"
            )

            # 三级: 杀跌盘涌出
            panic_sell = TreeNode(
                id="bear_panic_sell",
                label="杀跌盘涌出",
                type=NodeType.TERTIARY,
                sentiment=Sentiment.BEARISH,
                strength=80.0,
                active=True,
                description="恐慌性抛售"
            )
            panic_volume.children.append(panic_sell)
            capital_primary.children.append(panic_volume)

    # 二级: 情绪转冷
    if rsi14 and rsi14 < 50:
        sentiment_cold = TreeNode(
            id="bear_sentiment",
            label="情绪转冷",
            type=NodeType.SECONDARY,
            sentiment=Sentiment.BEARISH,
            strength=(50 - rsi14) * 2,
            active=True,
            description="市场情绪由乐观转悲观"
        )
        capital_primary.children.append(sentiment_cold)

    root.children.append(capital_primary)

    # === 一级因素 3: 位置危险 ===
    position_primary = TreeNode(
        id="bear_position",
        label="位置危险",
        type=NodeType.PRIMARY,
        sentiment=Sentiment.BEARISH,
        strength=100 - _calculate_position_safety(position_60, drawdown),
        active=position_60 is not None and position_60 > 70,
        description="相对高位，下跌空间大"
    )

    # 二级: 高位回落
    if position_60 and position_60 > 75:
        high_fall = TreeNode(
            id="bear_high_fall",
            label="高位回落",
            type=NodeType.SECONDARY,
            sentiment=Sentiment.BEARISH,
            strength=min(position_60, 100),
            active=True,
            description=f"60日位置={position_60:.1f}%，高位"
        )

        # 三级: 获利盘压力
        if drawdown and drawdown > -10:
            profit_pressure = TreeNode(
                id="bear_profit_pressure",
                label="获利盘压力",
                type=NodeType.TERTIARY,
                sentiment=Sentiment.BEARISH,
                strength=90.0,
                active=True,
                description=f"距高点仅{abs(drawdown):.1f}%，筹码松动"
            )
            high_fall.children.append(profit_pressure)

        position_primary.children.append(high_fall)

    # 二级: 超买回调
    if rsi14 and rsi14 > 75 and position_60 and position_60 > 70:
        overbought = TreeNode(
            id="bear_overbought",
            label="超买回调",
            type=NodeType.SECONDARY,
            sentiment=Sentiment.BEARISH,
            strength=min(rsi14, 100),
            active=True,
            description=f"RSI={rsi14:.1f}，超买回调"
        )
        position_primary.children.append(overbought)

    root.children.append(position_primary)

    return root


def _build_neutral_tree(metrics: Dict) -> TreeNode:
    """构建中性关系树（震荡市特征）"""
    ret5 = metrics.get("ret5")
    trend = metrics.get("trend")
    volatility20 = metrics.get("volatility20")

    root = TreeNode(
        id="neutral_root",
        label="市场震荡",
        type=NodeType.ROOT,
        sentiment=Sentiment.NEUTRAL,
        strength=50.0,
        active=trend == "均线交织" or (ret5 is not None and abs(ret5) < 2),
        description="无明确趋势"
    )

    # 一级: 均线交织
    if trend == "均线交织":
        entangle = TreeNode(
            id="neutral_entangle",
            label="均线交织",
            type=NodeType.PRIMARY,
            sentiment=Sentiment.NEUTRAL,
            strength=60.0,
            active=True,
            description="多空力量均衡"
        )
        root.children.append(entangle)

    # 一级: 窄幅波动
    if ret5 is not None and abs(ret5) < 2:
        narrow = TreeNode(
            id="neutral_narrow",
            label="窄幅震荡",
            type=NodeType.PRIMARY,
            sentiment=Sentiment.NEUTRAL,
            strength=50.0,
            active=True,
            description=f"5日涨跌幅仅{ret5:+.1f}%"
        )
        root.children.append(narrow)

    return root


# ============================================================
# 辅助计算函数
# ============================================================

def _calculate_bullish_strength(metrics: Dict) -> float:
    """计算整体看涨强度"""
    score = 50.0  # 基准分

    ret5 = metrics.get("ret5")
    if ret5 and ret5 > 0:
        score += min(ret5 * 5, 30)

    rsi14 = metrics.get("rsi14")
    if rsi14 and 50 < rsi14 < 75:
        score += (rsi14 - 50) / 25 * 20

    trend = metrics.get("trend")
    if trend == "多头排列":
        score += 20

    return min(100.0, score)


def _calculate_bearish_strength(metrics: Dict) -> float:
    """计算整体看跌强度"""
    score = 50.0

    ret5 = metrics.get("ret5")
    if ret5 and ret5 < 0:
        score += min(abs(ret5) * 5, 30)

    rsi14 = metrics.get("rsi14")
    if rsi14 and 25 < rsi14 < 50:
        score += (50 - rsi14) / 25 * 20

    trend = metrics.get("trend")
    if trend == "空头排列":
        score += 20

    return min(100.0, score)


def _calculate_trend_strength(metrics: Dict) -> float:
    """计算趋势强度"""
    trend = metrics.get("trend")
    if trend == "多头排列":
        return 90.0
    elif trend == "空头排列":
        return 10.0
    else:
        ma20_rising = metrics.get("ma20_rising")
        if ma20_rising is True:
            return 65.0
        elif ma20_rising is False:
            return 35.0
        return 50.0


def _calculate_momentum_strength(rsi14: Optional[float], ret5: Optional[float]) -> float:
    """计算动量强度"""
    if rsi14 is None:
        return 50.0

    # RSI贡献
    rsi_score = rsi14

    # 短期涨幅贡献
    ret_score = 50.0
    if ret5 is not None:
        ret_score = 50 + min(ret5 * 5, 50)

    return (rsi_score * 0.6 + ret_score * 0.4)


def _calculate_capital_strength(volume250: Optional[float]) -> float:
    """计算资金强度"""
    if volume250 is None:
        return 50.0

    if volume250 > 1.5:
        return min(50 + (volume250 - 1) * 30, 100)
    elif volume250 < 0.8:
        return max(volume250 * 60, 10)
    else:
        return 50.0


def _calculate_position_safety(position_60: Optional[float], drawdown: Optional[float]) -> float:
    """计算位置安全度（越低越安全）"""
    if position_60 is None:
        return 50.0

    # 60日位置贡献（反向）
    pos_score = 100 - position_60

    # 回撤贡献
    dd_score = 50.0
    if drawdown is not None:
        if drawdown < -25:
            dd_score = 80.0
        elif drawdown < -15:
            dd_score = 60.0
        elif drawdown > -5:
            dd_score = 20.0

    return (pos_score * 0.7 + dd_score * 0.3)


# ============================================================
# 可视化输出
# ============================================================

def format_tree_text(node: TreeNode, indent: int = 0, is_last: bool = True) -> str:
    """格式化树形结构为文本（ASCII树）

    示例输出：
    市场上涨 ✓ [强度: 75]
    ├─ 趋势向上 ✓ [强度: 80]
    │  ├─ 多头排列 ✓ [强度: 90]
    │  │  └─ 均线向上 ✓ [强度: 80]
    │  └─ 动量强劲 ✓ [强度: 70]
    └─ 资金流入 ✓ [强度: 65]
    """
    lines = []

    # 当前节点
    prefix = "    " * indent
    if indent > 0:
        connector = "└─ " if is_last else "├─ "
        prefix += connector

    # 状态标记
    status = "✓" if node.active else "○"

    # 颜色标记（前端可用）
    color_mark = {
        Sentiment.BULLISH: "🟢",
        Sentiment.BEARISH: "🔴",
        Sentiment.NEUTRAL: "🟡"
    }[node.sentiment]

    # 节点信息
    line = f"{prefix}{color_mark} {node.label} {status} [强度: {node.strength:.0f}]"
    if node.description and node.active:
        line += f" — {node.description}"

    lines.append(line)

    # 递归子节点
    for i, child in enumerate(node.children):
        child_is_last = (i == len(node.children) - 1)
        child_lines = format_tree_text(child, indent + 1, child_is_last)
        lines.append(child_lines)

    return "\n".join(lines)


def export_tree_json(tree: MarketCorrelationTree, filepath: str):
    """导出完整树形结构为JSON（供前端D3.js渲染）"""
    import json

    data = tree.to_dict()

    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return filepath
