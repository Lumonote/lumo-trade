#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""市场关联网络测试和演示
============================

演示如何使用 market_correlation_network 模块
生成市场指标的树形因果关系图
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from analysis.market_correlation_network import (
    build_correlation_network,
    generate_text_tree,
    generate_mermaid_diagram,
    MarketCorrelationNetwork
)


def demo_bull_market():
    """演示：牛市行情"""
    print("\n" + "=" * 70)
    print("📊 演示场景 1：牛市行情")
    print("=" * 70 + "\n")

    metrics = {
        "symbol": "sh000001",
        "name": "上证指数",
        "as_of": "2024-01-15",
        "close": 3150.5,
        "ma20": 3120.0,
        "ma60": 3080.0,
        "ma120": 3050.0,
        "ma250": 3000.0,
        "rsi14": 65.3,  # 强势区
        "position_60": 72.5,  # 偏高位
        "drawdown": -8.5,  # 小幅回撤
        "volume250": 1.35,  # 放量
        "volume5": 1.28,
        "trend": "多头排列",
        "ret1": 0.8,
        "ret5": 4.2,  # 上涨
        "ret20": 12.5,
        "volatility20": 22.1,
        "ma20_rising": True,
        "new_low20": False
    }

    network = build_correlation_network(metrics)

    # 生成文本树
    text_tree = generate_text_tree(network)
    print(text_tree)

    # 生成 Mermaid 图
    print("\n\n")
    print("=" * 70)
    print("Mermaid 流程图代码（可复制到 https://mermaid.live 查看）:")
    print("=" * 70)
    mermaid = generate_mermaid_diagram(network)
    print(mermaid)

    # 导出 JSON
    print("\n\n")
    print("=" * 70)
    print("JSON 导出（供前端可视化）:")
    print("=" * 70)
    print(network.to_json(indent=2)[:500] + "...")


def demo_bear_market():
    """演示：熊市行情"""
    print("\n" + "=" * 70)
    print("📊 演示场景 2：熊市行情")
    print("=" * 70 + "\n")

    metrics = {
        "symbol": "sh000001",
        "name": "上证指数",
        "as_of": "2024-02-08",
        "close": 2850.2,
        "ma20": 2920.0,
        "ma60": 2980.0,
        "ma120": 3050.0,
        "ma250": 3100.0,
        "rsi14": 32.5,  # 弱势区
        "position_60": 18.3,  # 低位
        "drawdown": -28.7,  # 深度回撤
        "volume250": 1.62,  # 恐慌放量
        "volume5": 1.45,
        "trend": "空头排列",
        "ret1": -1.2,
        "ret5": -6.8,  # 下跌
        "ret20": -18.3,
        "volatility20": 38.5,
        "ma20_rising": False,
        "new_low20": True
    }

    network = build_correlation_network(metrics)
    text_tree = generate_text_tree(network)
    print(text_tree)


def demo_sideways_market():
    """演示：震荡市"""
    print("\n" + "=" * 70)
    print("📊 演示场景 3：震荡市")
    print("=" * 70 + "\n")

    metrics = {
        "symbol": "sh000001",
        "name": "上证指数",
        "as_of": "2024-03-12",
        "close": 3020.8,
        "ma20": 3015.0,
        "ma60": 3025.0,
        "ma120": 3010.0,
        "ma250": 3030.0,
        "rsi14": 52.3,  # 中性
        "position_60": 48.5,  # 中位
        "drawdown": -12.3,
        "volume250": 0.95,  # 缩量
        "volume5": 0.88,
        "trend": "均线交织",
        "ret1": 0.3,
        "ret5": 1.2,  # 小幅波动
        "ret20": -2.5,
        "volatility20": 18.2,
        "ma20_rising": True,
        "new_low20": False
    }

    network = build_correlation_network(metrics)
    text_tree = generate_text_tree(network)
    print(text_tree)


def demo_oversold_bottom():
    """演示：超卖底部（反弹机会）"""
    print("\n" + "=" * 70)
    print("📊 演示场景 4：超卖底部 — 多空信号并存")
    print("=" * 70 + "\n")

    metrics = {
        "symbol": "sh000001",
        "name": "上证指数",
        "as_of": "2024-04-05",
        "close": 2780.5,
        "ma20": 2850.0,
        "ma60": 2920.0,
        "ma120": 2980.0,
        "ma250": 3050.0,
        "rsi14": 22.8,  # 极度超卖
        "position_60": 12.5,  # 极低位
        "drawdown": -32.5,  # 深度回撤
        "volume250": 1.75,  # 恐慌放量
        "volume5": 1.60,
        "trend": "空头排列",
        "ret1": -2.1,
        "ret5": -8.5,  # 近期暴跌
        "ret20": -22.3,
        "volatility20": 42.1,
        "ma20_rising": False,
        "new_low20": True
    }

    network = build_correlation_network(metrics)
    text_tree = generate_text_tree(network)
    print(text_tree)

    print("\n\n💡 解读：")
    print("─" * 70)
    print("这是一个典型的「恐慌性底部」场景：")
    print("  • 看空信号主导：空头排列、创新低、深度回撤")
    print("  • 但同时出现看多信号：极度超卖、极低位、超跌反弹窗口")
    print("  • 策略：短期技术性反弹概率大，但中期趋势仍需观察均线是否转强")
    print("─" * 70)


if __name__ == "__main__":
    # 运行所有演示
    demo_bull_market()
    print("\n\n\n")

    demo_bear_market()
    print("\n\n\n")

    demo_sideways_market()
    print("\n\n\n")

    demo_oversold_bottom()

    print("\n\n")
    print("=" * 70)
    print("✅ 演示完成！")
    print("=" * 70)
    print("\n使用说明：")
    print("  1. 将 Mermaid 代码复制到 https://mermaid.live 可查看流程图")
    print("  2. JSON 数据可用于前端 D3.js/ECharts/Cytoscape 可视化")
    print("  3. 文本树可直接在终端查看或导出为报告")
    print("")
