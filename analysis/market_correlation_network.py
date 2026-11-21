#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大盘技术指标关联网络 - 树形因果图谱
=====================================

功能：
1. 构建指标间的因果关系网络
2. 生成可视化树形图（JSON/Mermaid/HTML）
3. 一眼看出：什么导致上涨、什么导致下跌
4. 支持多层级关联分析

示例网络结构：
    上涨 ← RSI走强 + 放量 + 均线多头
    下跌 ← RSI走弱 + 缩量 + 均线空头
"""

from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict
from enum import Enum
import json


class NodeType(Enum):
    """节点类型"""
    ROOT = "root"  # 根节点（上涨/下跌）
    CATEGORY = "category"  # 类别节点（趋势/量能/情绪等）
    INDICATOR = "indicator"  # 指标节点
    SIGNAL = "signal"  # 信号节点


class SignalStrength(Enum):
    """信号强度"""
    STRONG_BULLISH = "强烈看多"
    BULLISH = "看多"
    WEAK_BULLISH = "偏多"
    NEUTRAL = "中性"
    WEAK_BEARISH = "偏空"
    BEARISH = "看空"
    STRONG_BEARISH = "强烈看空"


@dataclass
class NetworkNode:
    """网络节点"""
    id: str
    type: NodeType
    label: str
    value: Optional[float] = None
    strength: Optional[SignalStrength] = None
    description: str = ""
    color: str = "#888888"  # 节点颜色
    children: List['NetworkNode'] = None

    def __post_init__(self):
        if self.children is None:
            self.children = []

    def to_dict(self) -> Dict:
        """转换为字典（用于JSON序列化）"""
        return {
            "id": self.id,
            "type": self.type.value,
            "label": self.label,
            "value": self.value,
            "strength": self.strength.value if self.strength else None,
            "description": self.description,
            "color": self.color,
            "children": [child.to_dict() for child in self.children]
        }


@dataclass
class MarketCorrelationNetwork:
    """市场关联网络"""
    bull_root: NetworkNode  # 上涨关联树
    bear_root: NetworkNode  # 下跌关联树
    metadata: Dict[str, Any]

    def to_dict(self) -> Dict:
        return {
            "bull": self.bull_root.to_dict(),
            "bear": self.bear_root.to_dict(),
            "metadata": self.metadata
        }

    def to_json(self, indent=2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


# ============================================================
# 核心：构建关联网络
# ============================================================

def build_correlation_network(metrics: Dict[str, Any]) -> MarketCorrelationNetwork:
    """构建市场指标关联网络

    Args:
        metrics: 来自 market_health.index_metrics() 的指标字典

    Returns:
        MarketCorrelationNetwork: 包含上涨和下跌两棵树
    """
    # 提取关键指标
    close = metrics.get("close", 0)
    ma20 = metrics.get("ma20")
    ma60 = metrics.get("ma60")
    ma120 = metrics.get("ma120")
    ma250 = metrics.get("ma250")
    rsi14 = metrics.get("rsi14")
    position_60 = metrics.get("position_60")
    drawdown = metrics.get("drawdown")
    volume250 = metrics.get("volume250")
    volume5 = metrics.get("volume5")
    trend = metrics.get("trend", "数据不足")
    ret1 = metrics.get("ret1")
    ret5 = metrics.get("ret5")
    ret20 = metrics.get("ret20")
    volatility20 = metrics.get("volatility20")
    ma20_rising = metrics.get("ma20_rising")
    new_low20 = metrics.get("new_low20")

    # 构建上涨树
    bull_root = NetworkNode(
        id="bull_root",
        type=NodeType.ROOT,
        label="📈 上涨驱动因素",
        color="#10b981",
        description="当前市场看多信号"
    )

    # 构建下跌树
    bear_root = NetworkNode(
        id="bear_root",
        type=NodeType.ROOT,
        label="📉 下跌驱动因素",
        color="#ef4444",
        description="当前市场看空信号"
    )

    # 分析各维度并添加到对应树
    _add_trend_dimension(bull_root, bear_root, metrics)
    _add_momentum_dimension(bull_root, bear_root, metrics)
    _add_volume_dimension(bull_root, bear_root, metrics)
    _add_position_dimension(bull_root, bear_root, metrics)
    _add_volatility_dimension(bull_root, bear_root, metrics)
    _add_technical_dimension(bull_root, bear_root, metrics)

    # 元数据
    metadata = {
        "symbol": metrics.get("symbol"),
        "name": metrics.get("name"),
        "as_of": metrics.get("as_of"),
        "close": close,
        "generated_at": metrics.get("as_of"),
        "bull_signal_count": _count_signals(bull_root),
        "bear_signal_count": _count_signals(bear_root)
    }

    return MarketCorrelationNetwork(
        bull_root=bull_root,
        bear_root=bear_root,
        metadata=metadata
    )


def _add_trend_dimension(bull_root: NetworkNode, bear_root: NetworkNode,
                        metrics: Dict[str, Any]):
    """维度1：趋势（均线）"""
    trend = metrics.get("trend")
    close = metrics.get("close")
    ma20 = metrics.get("ma20")
    ma60 = metrics.get("ma60")
    ma120 = metrics.get("ma120")
    ma250 = metrics.get("ma250")
    ma20_rising = metrics.get("ma20_rising")

    # 看多信号
    if trend == "多头排列":
        trend_node = NetworkNode(
            id="trend_bull",
            type=NodeType.CATEGORY,
            label="🔼 趋势看多",
            color="#10b981",
            description="均线多头排列，趋势向上"
        )

        # 子节点：具体均线状态
        if close and ma20 and ma60 and ma120 and ma250:
            trend_node.children.append(NetworkNode(
                id="ma_bull",
                type=NodeType.INDICATOR,
                label="均线排列",
                value=100,
                strength=SignalStrength.STRONG_BULLISH,
                description=f"收盘({close:.0f}) > MA20({ma20:.0f}) > MA60({ma60:.0f}) > MA120({ma120:.0f}) > MA250({ma250:.0f})",
                color="#10b981"
            ))

        if ma20_rising:
            trend_node.children.append(NetworkNode(
                id="ma20_rising",
                type=NodeType.SIGNAL,
                label="MA20向上",
                strength=SignalStrength.BULLISH,
                description="20日均线持续上行",
                color="#34d399"
            ))

        bull_root.children.append(trend_node)

    elif trend == "空头排列":
        # 看空信号
        trend_node = NetworkNode(
            id="trend_bear",
            type=NodeType.CATEGORY,
            label="🔽 趋势看空",
            color="#ef4444",
            description="均线空头排列，趋势向下"
        )

        if close and ma20 and ma60 and ma120 and ma250:
            trend_node.children.append(NetworkNode(
                id="ma_bear",
                type=NodeType.INDICATOR,
                label="均线排列",
                value=-100,
                strength=SignalStrength.STRONG_BEARISH,
                description=f"收盘({close:.0f}) < MA20({ma20:.0f}) < MA60({ma60:.0f}) < MA120({ma120:.0f}) < MA250({ma250:.0f})",
                color="#ef4444"
            ))

        if ma20_rising is False:
            trend_node.children.append(NetworkNode(
                id="ma20_falling",
                type=NodeType.SIGNAL,
                label="MA20向下",
                strength=SignalStrength.BEARISH,
                description="20日均线持续下行",
                color="#f87171"
            ))

        bear_root.children.append(trend_node)

    elif trend == "均线交织":
        # 震荡市，根据MA20方向判断
        if ma20_rising:
            trend_node = NetworkNode(
                id="trend_sideways_up",
                type=NodeType.CATEGORY,
                label="↗️ 震荡偏多",
                color="#f59e0b",
                description="均线交织但MA20向上"
            )
            trend_node.children.append(NetworkNode(
                id="ma20_rising_sideways",
                type=NodeType.SIGNAL,
                label="MA20向上",
                strength=SignalStrength.WEAK_BULLISH,
                description="短期均线向上，潜在突破",
                color="#fbbf24"
            ))
            bull_root.children.append(trend_node)
        elif ma20_rising is False:
            trend_node = NetworkNode(
                id="trend_sideways_down",
                type=NodeType.CATEGORY,
                label="↘️ 震荡偏空",
                color="#f59e0b",
                description="均线交织且MA20向下"
            )
            trend_node.children.append(NetworkNode(
                id="ma20_falling_sideways",
                type=NodeType.SIGNAL,
                label="MA20向下",
                strength=SignalStrength.WEAK_BEARISH,
                description="短期均线向下，潜在破位",
                color="#fb923c"
            ))
            bear_root.children.append(trend_node)


def _add_momentum_dimension(bull_root: NetworkNode, bear_root: NetworkNode,
                           metrics: Dict[str, Any]):
    """维度2：动量（RSI）"""
    rsi14 = metrics.get("rsi14")
    if rsi14 is None:
        return

    # RSI > 70: 超买（可能回调，但也说明强势）
    # RSI 50-70: 强势
    # RSI 30-50: 中性偏弱
    # RSI < 30: 超卖（可能反弹）

    if rsi14 >= 70:
        # 超买区：既是看多（强势）也是看空（回调风险）
        momentum_bull = NetworkNode(
            id="momentum_overbought",
            type=NodeType.CATEGORY,
            label="💪 动量强劲",
            color="#10b981",
            description=f"RSI={rsi14:.1f}，市场强势"
        )
        momentum_bull.children.append(NetworkNode(
            id="rsi_strong",
            type=NodeType.INDICATOR,
            label=f"RSI超买({rsi14:.1f})",
            value=rsi14,
            strength=SignalStrength.STRONG_BULLISH if rsi14 >= 80 else SignalStrength.BULLISH,
            description="动量强劲，但注意回调风险",
            color="#10b981"
        ))
        bull_root.children.append(momentum_bull)

        # 同时添加到看空树（回调风险）
        if rsi14 >= 80:
            momentum_bear = NetworkNode(
                id="momentum_overbought_risk",
                type=NodeType.CATEGORY,
                label="⚠️ 超买回调风险",
                color="#f59e0b",
                description=f"RSI={rsi14:.1f}，极度超买"
            )
            momentum_bear.children.append(NetworkNode(
                id="rsi_overbought_risk",
                type=NodeType.SIGNAL,
                label="极度超买",
                strength=SignalStrength.BEARISH,
                description="短期回调概率大",
                color="#f59e0b"
            ))
            bear_root.children.append(momentum_bear)

    elif rsi14 >= 50:
        # 强势区
        momentum_bull = NetworkNode(
            id="momentum_strong",
            type=NodeType.CATEGORY,
            label="📊 动量偏强",
            color="#34d399",
            description=f"RSI={rsi14:.1f}，偏强势"
        )
        momentum_bull.children.append(NetworkNode(
            id="rsi_bullish",
            type=NodeType.INDICATOR,
            label=f"RSI偏强({rsi14:.1f})",
            value=rsi14,
            strength=SignalStrength.WEAK_BULLISH,
            description="动量健康上行",
            color="#34d399"
        ))
        bull_root.children.append(momentum_bull)

    elif rsi14 >= 30:
        # 中性偏弱
        momentum_bear = NetworkNode(
            id="momentum_weak",
            type=NodeType.CATEGORY,
            label="📉 动量偏弱",
            color="#fb923c",
            description=f"RSI={rsi14:.1f}，偏弱势"
        )
        momentum_bear.children.append(NetworkNode(
            id="rsi_bearish",
            type=NodeType.INDICATOR,
            label=f"RSI偏弱({rsi14:.1f})",
            value=rsi14,
            strength=SignalStrength.WEAK_BEARISH,
            description="动量疲软",
            color="#fb923c"
        ))
        bear_root.children.append(momentum_bear)

    else:
        # 超卖区：既是看空（弱势）也是看多（反弹机会）
        momentum_bear = NetworkNode(
            id="momentum_oversold",
            type=NodeType.CATEGORY,
            label="🔻 动量极弱",
            color="#ef4444",
            description=f"RSI={rsi14:.1f}，市场弱势"
        )
        momentum_bear.children.append(NetworkNode(
            id="rsi_weak",
            type=NodeType.INDICATOR,
            label=f"RSI超卖({rsi14:.1f})",
            value=rsi14,
            strength=SignalStrength.STRONG_BEARISH if rsi14 <= 20 else SignalStrength.BEARISH,
            description="动量极弱，但注意反弹",
            color="#ef4444"
        ))
        bear_root.children.append(momentum_bear)

        # 同时添加到看多树（反弹机会）
        if rsi14 <= 25:
            momentum_bull = NetworkNode(
                id="momentum_oversold_bounce",
                type=NodeType.CATEGORY,
                label="💡 超卖反弹机会",
                color="#3b82f6",
                description=f"RSI={rsi14:.1f}，极度超卖"
            )
            momentum_bull.children.append(NetworkNode(
                id="rsi_oversold_bounce",
                type=NodeType.SIGNAL,
                label="极度超卖",
                strength=SignalStrength.BULLISH,
                description="技术性反弹概率大",
                color="#3b82f6"
            ))
            bull_root.children.append(momentum_bull)


def _add_volume_dimension(bull_root: NetworkNode, bear_root: NetworkNode,
                         metrics: Dict[str, Any]):
    """维度3：量能"""
    volume250 = metrics.get("volume250")
    volume5 = metrics.get("volume5")
    ret5 = metrics.get("ret5")

    if volume250 is None:
        return

    # 量价配合分析
    if volume250 >= 1.5:
        # 放量
        if ret5 is not None and ret5 > 3:
            # 上涨放量 = 看多
            volume_node = NetworkNode(
                id="volume_surge_up",
                type=NodeType.CATEGORY,
                label="📈 上涨放量",
                color="#10b981",
                description=f"量比={volume250:.2f}，5日涨{ret5:.1f}%"
            )
            volume_node.children.append(NetworkNode(
                id="volume_price_match",
                type=NodeType.INDICATOR,
                label="量价配合",
                value=volume250,
                strength=SignalStrength.STRONG_BULLISH,
                description="放量上涨，资金积极介入",
                color="#10b981"
            ))
            bull_root.children.append(volume_node)
        elif ret5 is not None and ret5 < -3:
            # 下跌放量 = 看空（恐慌）
            volume_node = NetworkNode(
                id="volume_surge_down",
                type=NodeType.CATEGORY,
                label="📉 下跌放量",
                color="#ef4444",
                description=f"量比={volume250:.2f}，5日跌{abs(ret5):.1f}%"
            )
            volume_node.children.append(NetworkNode(
                id="volume_panic",
                type=NodeType.INDICATOR,
                label="恐慌性放量",
                value=volume250,
                strength=SignalStrength.STRONG_BEARISH,
                description="放量下跌，恐慌抛售",
                color="#ef4444"
            ))
            bear_root.children.append(volume_node)

    elif volume250 < 0.8:
        # 缩量
        if ret5 is not None and ret5 > 3:
            # 上涨缩量 = 看空（背离）
            volume_node = NetworkNode(
                id="volume_shrink_up",
                type=NodeType.CATEGORY,
                label="⚠️ 上涨缩量",
                color="#f59e0b",
                description=f"量比={volume250:.2f}，5日涨{ret5:.1f}%"
            )
            volume_node.children.append(NetworkNode(
                id="volume_price_diverge",
                type=NodeType.SIGNAL,
                label="量价背离",
                strength=SignalStrength.BEARISH,
                description="缩量上涨，资金观望",
                color="#f59e0b"
            ))
            bear_root.children.append(volume_node)
        elif ret5 is not None and ret5 < -3:
            # 下跌缩量 = 中性（弱势但无恐慌）
            volume_node = NetworkNode(
                id="volume_shrink_down",
                type=NodeType.CATEGORY,
                label="📊 下跌缩量",
                color="#9ca3af",
                description=f"量比={volume250:.2f}，5日跌{abs(ret5):.1f}%"
            )
            volume_node.children.append(NetworkNode(
                id="volume_weak",
                type=NodeType.SIGNAL,
                label="阴跌缩量",
                strength=SignalStrength.WEAK_BEARISH,
                description="弱势下跌，无恐慌",
                color="#9ca3af"
            ))
            bear_root.children.append(volume_node)


def _add_position_dimension(bull_root: NetworkNode, bear_root: NetworkNode,
                           metrics: Dict[str, Any]):
    """维度4：位置（60日相对位置）"""
    position_60 = metrics.get("position_60")
    drawdown = metrics.get("drawdown")

    if position_60 is None:
        return

    if position_60 >= 80:
        # 高位
        pos_node = NetworkNode(
            id="position_high",
            type=NodeType.CATEGORY,
            label="🔝 高位运行",
            color="#f59e0b",
            description=f"60日位置={position_60:.1f}%"
        )
        pos_node.children.append(NetworkNode(
            id="high_position",
            type=NodeType.INDICATOR,
            label=f"高位({position_60:.0f}%)",
            value=position_60,
            strength=SignalStrength.BEARISH,
            description="获利盘丰厚，回调压力大",
            color="#f59e0b"
        ))
        if drawdown is not None and drawdown >= -5:
            pos_node.children.append(NetworkNode(
                id="near_peak",
                type=NodeType.SIGNAL,
                label="接近历史高点",
                strength=SignalStrength.BEARISH,
                description=f"距高点仅{abs(drawdown):.1f}%",
                color="#ef4444"
            ))
        bear_root.children.append(pos_node)

    elif position_60 >= 50:
        # 中位偏高
        pos_node = NetworkNode(
            id="position_mid_high",
            type=NodeType.CATEGORY,
            label="📊 中高位",
            color="#9ca3af",
            description=f"60日位置={position_60:.1f}%"
        )
        pos_node.children.append(NetworkNode(
            id="mid_position",
            type=NodeType.SIGNAL,
            label="中性位置",
            strength=SignalStrength.NEUTRAL,
            description="不高不低，观望",
            color="#9ca3af"
        ))
        # 中性信号可以不加到任何树

    elif position_60 >= 20:
        # 中位偏低
        pos_node = NetworkNode(
            id="position_mid_low",
            type=NodeType.CATEGORY,
            label="📊 中低位",
            color="#34d399",
            description=f"60日位置={position_60:.1f}%"
        )
        pos_node.children.append(NetworkNode(
            id="mid_low_position",
            type=NodeType.SIGNAL,
            label="调整充分",
            strength=SignalStrength.WEAK_BULLISH,
            description="回调空间有限",
            color="#34d399"
        ))
        bull_root.children.append(pos_node)

    else:
        # 低位
        pos_node = NetworkNode(
            id="position_low",
            type=NodeType.CATEGORY,
            label="🔻 低位运行",
            color="#10b981",
            description=f"60日位置={position_60:.1f}%"
        )
        pos_node.children.append(NetworkNode(
            id="low_position",
            type=NodeType.INDICATOR,
            label=f"低位({position_60:.0f}%)",
            value=position_60,
            strength=SignalStrength.STRONG_BULLISH,
            description="历史低位，安全边际高",
            color="#10b981"
        ))
        if drawdown is not None and drawdown <= -30:
            pos_node.children.append(NetworkNode(
                id="deep_drawdown",
                type=NodeType.SIGNAL,
                label="深度回撤",
                strength=SignalStrength.STRONG_BULLISH,
                description=f"回撤{abs(drawdown):.1f}%，筑底区域",
                color="#10b981"
            ))
        bull_root.children.append(pos_node)


def _add_volatility_dimension(bull_root: NetworkNode, bear_root: NetworkNode,
                              metrics: Dict[str, Any]):
    """维度5：波动率"""
    volatility20 = metrics.get("volatility20")
    position_60 = metrics.get("position_60")

    if volatility20 is None:
        return

    if volatility20 >= 30:
        # 高波动
        vol_node = NetworkNode(
            id="volatility_high",
            type=NodeType.CATEGORY,
            label="⚡ 波动加剧",
            color="#f59e0b",
            description=f"波动率={volatility20:.1f}%"
        )

        if position_60 is not None and position_60 >= 70:
            # 高位高波动 = 风险
            vol_node.children.append(NetworkNode(
                id="high_vol_high_pos",
                type=NodeType.SIGNAL,
                label="高位震荡",
                strength=SignalStrength.BEARISH,
                description="高位波动加剧，不确定性大",
                color="#ef4444"
            ))
            bear_root.children.append(vol_node)
        elif position_60 is not None and position_60 <= 30:
            # 低位高波动 = 机会（可能筑底）
            vol_node.children.append(NetworkNode(
                id="high_vol_low_pos",
                type=NodeType.SIGNAL,
                label="底部震荡",
                strength=SignalStrength.BULLISH,
                description="低位波动，可能筑底",
                color="#10b981"
            ))
            bull_root.children.append(vol_node)

    elif volatility20 <= 15:
        # 低波动
        vol_node = NetworkNode(
            id="volatility_low",
            type=NodeType.CATEGORY,
            label="💤 波动平静",
            color="#9ca3af",
            description=f"波动率={volatility20:.1f}%"
        )
        vol_node.children.append(NetworkNode(
            id="low_volatility",
            type=NodeType.SIGNAL,
            label="平静期",
            strength=SignalStrength.NEUTRAL,
            description="波动率低，可能酝酿变盘",
            color="#9ca3af"
        ))
        # 中性信号不强制加到树


def _add_technical_dimension(bull_root: NetworkNode, bear_root: NetworkNode,
                             metrics: Dict[str, Any]):
    """维度6：技术形态"""
    new_low20 = metrics.get("new_low20")
    ret20 = metrics.get("ret20")

    # 20日新低
    if new_low20 is True:
        tech_node = NetworkNode(
            id="tech_new_low",
            type=NodeType.CATEGORY,
            label="🔽 创新低",
            color="#ef4444",
            description="创20日新低"
        )
        tech_node.children.append(NetworkNode(
            id="new_low_signal",
            type=NodeType.SIGNAL,
            label="突破支撑",
            strength=SignalStrength.BEARISH,
            description="跌破前期低点，弱势",
            color="#ef4444"
        ))
        bear_root.children.append(tech_node)

    # 超跌反弹
    if ret20 is not None and ret20 <= -15:
        tech_node = NetworkNode(
            id="tech_oversold",
            type=NodeType.CATEGORY,
            label="💡 超跌反弹",
            color="#3b82f6",
            description=f"20日跌{abs(ret20):.1f}%"
        )
        tech_node.children.append(NetworkNode(
            id="oversold_bounce",
            type=NodeType.SIGNAL,
            label="反弹窗口",
            strength=SignalStrength.BULLISH,
            description="短期超跌，技术性反弹",
            color="#3b82f6"
        ))
        bull_root.children.append(tech_node)


def _count_signals(node: NetworkNode) -> int:
    """递归统计信号数量"""
    count = 1 if node.type in (NodeType.INDICATOR, NodeType.SIGNAL) else 0
    for child in node.children:
        count += _count_signals(child)
    return count


# ============================================================
# 可视化：生成 Mermaid 流程图
# ============================================================

def generate_mermaid_diagram(network: MarketCorrelationNetwork) -> str:
    """生成 Mermaid 流程图代码

    可直接复制到 Markdown 或在线 Mermaid 编辑器查看
    """
    lines = ["graph TD"]
    lines.append("")

    # 生成上涨树
    lines.append("    %% 上涨驱动因素")
    _generate_mermaid_subtree(network.bull_root, lines, parent_id=None)
    lines.append("")

    # 生成下跌树
    lines.append("    %% 下跌驱动因素")
    _generate_mermaid_subtree(network.bear_root, lines, parent_id=None)
    lines.append("")

    # 样式定义
    lines.append("    %% 样式")
    lines.append("    classDef bullish fill:#10b981,stroke:#059669,color:#fff")
    lines.append("    classDef bearish fill:#ef4444,stroke:#dc2626,color:#fff")
    lines.append("    classDef warning fill:#f59e0b,stroke:#d97706,color:#fff")
    lines.append("    classDef neutral fill:#9ca3af,stroke:#6b7280,color:#fff")

    return "\n".join(lines)


def _generate_mermaid_subtree(node: NetworkNode, lines: List[str], parent_id: Optional[str]):
    """递归生成 Mermaid 子树"""
    # 节点定义
    label = node.label.replace('"', "'")
    if node.type == NodeType.ROOT:
        shape = f'{node.id}["{label}"]'
    elif node.type == NodeType.CATEGORY:
        shape = f'{node.id}("{label}")'
    elif node.type == NodeType.INDICATOR:
        shape = f'{node.id}["{label}"]'
    else:  # SIGNAL
        shape = f'{node.id}(["{label}"])'

    # 连接到父节点
    if parent_id:
        lines.append(f"    {parent_id} --> {shape}")
    else:
        lines.append(f"    {shape}")

    # 递归处理子节点
    for child in node.children:
        _generate_mermaid_subtree(child, lines, parent_id=node.id)


# ============================================================
# 可视化：生成简化的文本树
# ============================================================

def generate_text_tree(network: MarketCorrelationNetwork) -> str:
    """生成简化的文本树形图"""
    lines = []
    lines.append("=" * 70)
    lines.append("📊 大盘技术指标关联网络")
    lines.append("=" * 70)
    lines.append("")

    # 元数据
    meta = network.metadata
    lines.append(f"指数：{meta.get('name')} ({meta.get('symbol')})")
    lines.append(f"日期：{meta.get('as_of')}")
    lines.append(f"收盘：{meta.get('close'):.2f}")
    lines.append("")

    # 统计
    bull_count = meta.get('bull_signal_count', 0)
    bear_count = meta.get('bear_signal_count', 0)
    lines.append(f"看多信号数：{bull_count}")
    lines.append(f"看空信号数：{bear_count}")

    if bull_count > bear_count:
        lines.append(f"💡 市场偏向：看多（信号差 +{bull_count - bear_count}）")
    elif bear_count > bull_count:
        lines.append(f"⚠️  市场偏向：看空（信号差 -{bear_count - bull_count}）")
    else:
        lines.append("⚖️  市场偏向：中性（多空平衡）")

    lines.append("")
    lines.append("─" * 70)
    lines.append("")

    # 上涨树
    lines.append(f"【{network.bull_root.label}】")
    lines.append("")
    _generate_text_subtree(network.bull_root, lines, indent="")

    lines.append("")
    lines.append("─" * 70)
    lines.append("")

    # 下跌树
    lines.append(f"【{network.bear_root.label}】")
    lines.append("")
    _generate_text_subtree(network.bear_root, lines, indent="")

    lines.append("")
    lines.append("=" * 70)

    return "\n".join(lines)


def _generate_text_subtree(node: NetworkNode, lines: List[str], indent: str):
    """递归生成文本子树"""
    if node.type == NodeType.ROOT:
        # 根节点不打印自己，直接打印子节点
        for i, child in enumerate(node.children):
            _generate_text_subtree(child, lines, indent)
            if i < len(node.children) - 1:
                lines.append("")  # 类别间空行
        return

    # 打印当前节点
    if node.type == NodeType.CATEGORY:
        lines.append(f"{indent}▸ {node.label}")
        if node.description:
            lines.append(f"{indent}  {node.description}")

        # 打印子节点
        for child in node.children:
            _generate_text_subtree(child, lines, indent + "  ")

    elif node.type == NodeType.INDICATOR:
        emoji = "🟢" if "bullish" in (node.strength.name.lower() if node.strength else "") else \
                "🔴" if "bearish" in (node.strength.name.lower() if node.strength else "") else "🟡"
        lines.append(f"{indent}  {emoji} {node.label}")
        if node.description:
            lines.append(f"{indent}     └─ {node.description}")

    elif node.type == NodeType.SIGNAL:
        lines.append(f"{indent}    • {node.label}")
        if node.description:
            lines.append(f"{indent}      {node.description}")
