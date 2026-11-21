#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大盘技术指标关联网络历史记录仓库
====================================

用于保存每日的关联树数据，支持历史回测和信号准确率分析。
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from data_store import db


def save_correlation_snapshot(
    trade_date: str,
    symbol: str,
    network_data: Dict,
    metadata: Dict,
    text_tree: Optional[str] = None,
) -> bool:
    """保存关联网络快照

    Args:
        trade_date: 交易日期 YYYY-MM-DD
        symbol: 指数代码
        network_data: 完整网络树数据（to_dict()的结果）
        metadata: 元数据（包含bull_signal_count、bear_signal_count等）
        text_tree: 可选的文本树（用于快速查看）

    Returns:
        是否保存成功
    """
    bull_count = metadata.get('bull_signal_count', 0)
    bear_count = metadata.get('bear_signal_count', 0)
    diff = bull_count - bear_count

    # 判断市场偏向
    if diff > 2:
        bias = 'bull'
    elif diff < -2:
        bias = 'bear'
    else:
        bias = 'neutral'

    bias_strength = abs(diff)

    with db.connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO market_correlation_history
            (trade_date, symbol, as_of, close_price, bull_signal_count, bear_signal_count,
             market_bias, bias_strength, network_json, text_tree, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_date,
                symbol,
                metadata.get('as_of', ''),
                metadata.get('close'),
                bull_count,
                bear_count,
                bias,
                bias_strength,
                json.dumps(network_data, ensure_ascii=False),
                text_tree,
                json.dumps(metadata, ensure_ascii=False),
            )
        )

    return True


def get_correlation_history(
    symbol: str,
    start_date: str = '',
    end_date: str = '',
    limit: int = 100
) -> List[Dict]:
    """获取关联网络历史记录

    Args:
        symbol: 指数代码
        start_date: 开始日期（可选）
        end_date: 结束日期（可选）
        limit: 最大返回条数

    Returns:
        历史记录列表
    """
    sql = """
        SELECT trade_date, symbol, as_of, close_price,
               bull_signal_count, bear_signal_count, market_bias, bias_strength,
               network_json, text_tree, metadata_json,
               actual_ret_1d, actual_ret_5d, actual_ret_20d, signal_correct, verified_at
        FROM market_correlation_history
        WHERE symbol = ?
    """

    params = [symbol]

    if start_date:
        sql += " AND trade_date >= ?"
        params.append(start_date)

    if end_date:
        sql += " AND trade_date <= ?"
        params.append(end_date)

    sql += " ORDER BY trade_date DESC LIMIT ?"
    params.append(limit)

    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    results = []
    for row in rows:
        results.append({
            'trade_date': row[0],
            'symbol': row[1],
            'as_of': row[2],
            'close_price': row[3],
            'bull_signal_count': row[4],
            'bear_signal_count': row[5],
            'market_bias': row[6],
            'bias_strength': row[7],
            'network_json': row[8],
            'text_tree': row[9],
            'metadata_json': row[10],
            'actual_ret_1d': row[11],
            'actual_ret_5d': row[12],
            'actual_ret_20d': row[13],
            'signal_correct': row[14],
            'verified_at': row[15],
        })

    return results


def update_backtest_results(
    trade_date: str,
    symbol: str,
    actual_ret_1d: Optional[float] = None,
    actual_ret_5d: Optional[float] = None,
    actual_ret_20d: Optional[float] = None,
) -> bool:
    """更新回测结果（事后验证）

    Args:
        trade_date: 交易日期
        symbol: 指数代码
        actual_ret_1d: 次日实际收益率
        actual_ret_5d: 5日实际收益率
        actual_ret_20d: 20日实际收益率

    Returns:
        是否更新成功
    """
    # 获取原始信号偏向
    with db.connect() as conn:
        row = conn.execute(
            "SELECT market_bias FROM market_correlation_history WHERE trade_date = ? AND symbol = ?",
            (trade_date, symbol)
        ).fetchone()

        if not row:
            return False

        market_bias = row[0]

        # 判断信号是否正确（以5日收益为主要评判标准）
        signal_correct = None
        if actual_ret_5d is not None:
            if market_bias == 'bull' and actual_ret_5d > 0:
                signal_correct = 1
            elif market_bias == 'bear' and actual_ret_5d < 0:
                signal_correct = 1
            elif market_bias == 'neutral':
                # 中性信号：5日涨跌幅在±2%以内视为正确
                signal_correct = 1 if abs(actual_ret_5d) <= 2.0 else 0
            else:
                signal_correct = 0

        conn.execute(
            """
            UPDATE market_correlation_history
            SET actual_ret_1d = ?, actual_ret_5d = ?, actual_ret_20d = ?,
                signal_correct = ?, verified_at = ?
            WHERE trade_date = ? AND symbol = ?
            """,
            (
                actual_ret_1d,
                actual_ret_5d,
                actual_ret_20d,
                signal_correct,
                dt.datetime.now().isoformat(),
                trade_date,
                symbol,
            )
        )

    return True


def get_signal_accuracy_stats(
    symbol: str,
    start_date: str = '',
    end_date: str = '',
) -> Dict:
    """获取信号准确率统计

    Args:
        symbol: 指数代码
        start_date: 开始日期（可选）
        end_date: 结束日期（可选）

    Returns:
        统计数据字典
    """
    sql = """
        SELECT market_bias, COUNT(*) as total,
               SUM(CASE WHEN signal_correct = 1 THEN 1 ELSE 0 END) as correct,
               AVG(actual_ret_5d) as avg_ret_5d
        FROM market_correlation_history
        WHERE symbol = ? AND signal_correct IS NOT NULL
    """

    params = [symbol]

    if start_date:
        sql += " AND trade_date >= ?"
        params.append(start_date)

    if end_date:
        sql += " AND trade_date <= ?"
        params.append(end_date)

    sql += " GROUP BY market_bias"

    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    stats = {
        'symbol': symbol,
        'start_date': start_date,
        'end_date': end_date,
        'by_bias': {},
        'overall': {'total': 0, 'correct': 0, 'accuracy': 0.0}
    }

    total_all = 0
    correct_all = 0

    for row in rows:
        bias, total, correct, avg_ret = row
        accuracy = (correct / total * 100) if total > 0 else 0.0

        stats['by_bias'][bias] = {
            'total': total,
            'correct': correct,
            'accuracy': round(accuracy, 2),
            'avg_ret_5d': round(avg_ret, 2) if avg_ret is not None else None,
        }

        total_all += total
        correct_all += correct

    stats['overall']['total'] = total_all
    stats['overall']['correct'] = correct_all
    stats['overall']['accuracy'] = round((correct_all / total_all * 100) if total_all > 0 else 0.0, 2)

    return stats


def get_latest_snapshot(symbol: str) -> Optional[Dict]:
    """获取最新的关联网络快照

    Args:
        symbol: 指数代码

    Returns:
        最新快照数据，如果不存在返回None
    """
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT trade_date, symbol, as_of, close_price,
                   bull_signal_count, bear_signal_count, market_bias, bias_strength,
                   network_json, metadata_json
            FROM market_correlation_history
            WHERE symbol = ?
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            (symbol,)
        ).fetchone()

    if not row:
        return None

    return {
        'trade_date': row[0],
        'symbol': row[1],
        'as_of': row[2],
        'close_price': row[3],
        'bull_signal_count': row[4],
        'bear_signal_count': row[5],
        'market_bias': row[6],
        'bias_strength': row[7],
        'network': json.loads(row[8]) if row[8] else None,
        'metadata': json.loads(row[9]) if row[9] else None,
    }
