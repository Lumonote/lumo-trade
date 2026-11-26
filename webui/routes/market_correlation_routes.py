#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""市场关联网络 API 路由
=========================

提供市场技术指标关联网络的 HTTP API
"""

from typing import Dict, Optional
from robyn import Request, Response

from analysis.market_correlation_network import (
    build_correlation_network,
    generate_text_tree,
    generate_mermaid_diagram
)
from webui.services import market_health_service


def register_market_correlation_routes(app):
    """注册市场关联网络相关路由"""

    @app.get("/api/market-correlation/network")
    def get_market_correlation_network(request: Request):
        """获取市场关联网络

        Query参数：
            symbol: 指数代码，默认 sh000001
            format: 返回格式 json|text|mermaid，默认 json

        Returns:
            {
                "success": true,
                "data": {
                    "network": {...},  // JSON格式的网络数据
                    "text": "...",     // 文本树（可选）
                    "mermaid": "..."   // Mermaid图（可选）
                },
                "metadata": {...}
            }
        """
        try:
            query_params = request.query_params
            symbol = query_params.get("symbol", "sh000001")
            format_type = query_params.get("format", "json")

            # 获取指数指标数据
            state = market_health_service.read_state()
            bars = state.get("bars", {}).get(symbol, [])

            if not bars:
                return Response(
                    status_code=404,
                    headers={"Content-Type": "application/json"},
                    description='{"success": false, "error": "指数数据不存在"}'
                )

            # 计算指标
            from analysis.market_health import index_metrics, normalize_bars
            rows = normalize_bars(bars)
            metrics = index_metrics(symbol, rows, with_chart=False)

            if not metrics.get("available"):
                return Response(
                    status_code=404,
                    headers={"Content-Type": "application/json"},
                    description='{"success": false, "error": "指标数据不足"}'
                )

            # 构建关联网络
            network = build_correlation_network(metrics)

            # 根据格式返回
            result = {
                "success": True,
                "data": {},
                "metadata": network.metadata
            }

            if format_type in ("json", "all"):
                result["data"]["network"] = network.to_dict()

            if format_type in ("text", "all"):
                result["data"]["text"] = generate_text_tree(network)

            if format_type in ("mermaid", "all"):
                result["data"]["mermaid"] = generate_mermaid_diagram(network)

            import json
            return Response(
                status_code=200,
                headers={"Content-Type": "application/json"},
                description=json.dumps(result, ensure_ascii=False)
            )

        except Exception as e:
            import json
            import traceback
            return Response(
                status_code=500,
                headers={"Content-Type": "application/json"},
                description=json.dumps({
                    "success": False,
                    "error": str(e),
                    "traceback": traceback.format_exc()
                }, ensure_ascii=False)
            )

    @app.get("/api/market-correlation/summary")
    def get_market_correlation_summary(request: Request):
        """获取市场关联网络摘要

        Returns:
            {
                "success": true,
                "data": {
                    "symbol": "sh000001",
                    "name": "上证指数",
                    "as_of": "2024-01-15",
                    "close": 3150.5,
                    "bull_signal_count": 5,
                    "bear_signal_count": 2,
                    "bias": "bull",  // bull|bear|neutral
                    "bias_strength": 3,  // 信号差
                    "top_bull_signals": [...],
                    "top_bear_signals": [...]
                }
            }
        """
        try:
            query_params = request.query_params
            symbol = query_params.get("symbol", "sh000001")

            # 获取指数数据
            state = market_health_service.read_state()
            bars = state.get("bars", {}).get(symbol, [])

            if not bars:
                return Response(
                    status_code=404,
                    headers={"Content-Type": "application/json"},
                    description='{"success": false, "error": "指数数据不存在"}'
                )

            # 计算指标
            from analysis.market_health import index_metrics, normalize_bars
            rows = normalize_bars(bars)
            metrics = index_metrics(symbol, rows, with_chart=False)

            if not metrics.get("available"):
                return Response(
                    status_code=404,
                    headers={"Content-Type": "application/json"},
                    description='{"success": false, "error": "指标数据不足"}'
                )

            # 构建网络
            network = build_correlation_network(metrics)
            meta = network.metadata

            bull_count = meta.get('bull_signal_count', 0)
            bear_count = meta.get('bear_signal_count', 0)
            diff = bull_count - bear_count

            # 判断偏向
            if diff > 2:
                bias = "bull"
            elif diff < -2:
                bias = "bear"
            else:
                bias = "neutral"

            # 提取顶层信号
            top_bull_signals = _extract_top_signals(network.bull_root)
            top_bear_signals = _extract_top_signals(network.bear_root)

            result = {
                "success": True,
                "data": {
                    "symbol": meta.get("symbol"),
                    "name": meta.get("name"),
                    "as_of": meta.get("as_of"),
                    "close": meta.get("close"),
                    "bull_signal_count": bull_count,
                    "bear_signal_count": bear_count,
                    "bias": bias,
                    "bias_strength": abs(diff),
                    "top_bull_signals": top_bull_signals[:5],
                    "top_bear_signals": top_bear_signals[:5]
                }
            }

            import json
            return Response(
                status_code=200,
                headers={"Content-Type": "application/json"},
                description=json.dumps(result, ensure_ascii=False)
            )

        except Exception as e:
            import json
            import traceback
            return Response(
                status_code=500,
                headers={"Content-Type": "application/json"},
                description=json.dumps({
                    "success": False,
                    "error": str(e),
                    "traceback": traceback.format_exc()
                }, ensure_ascii=False)
            )


def _extract_top_signals(node) -> list:
    """提取顶层信号（递归）"""
    signals = []

    if node.type.value == "category":
        signals.append({
            "label": node.label,
            "description": node.description
        })

    for child in node.children:
        signals.extend(_extract_top_signals(child))

    return signals
