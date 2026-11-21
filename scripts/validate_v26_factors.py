#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v26 因子验证脚本 — 回测 + IC 检验 + 单调性测试

使用方法:
    python scripts/validate_v26_factors.py --factor chip_locked --start 2025-11-01 --end 2026-07-31
    python scripts/validate_v26_factors.py --all  # 测试所有新因子

输出:
    - docs/superpowers/specs/2026-11-v26-validation-{factor}.md
    - results/factor_validation_v26_{timestamp}.csv
"""
import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from analysis.enhanced_factors import (
    compute_chip_concentration,
    compute_volume_anomaly,
    compute_relative_strength_divergence,
    compute_intraday_timing_signal,
    compute_institutional_footprint,
    validate_factor_ic,
    backtest_factor_monotonicity,
)
from data_store.connection import get_conn

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================
# 数据加载
# ============================================================

def load_stock_pool(start_date: str, end_date: str) -> pd.DataFrame:
    """加载回测股票池 — 复用 opportunity_item 表历史记录

    Returns:
        DataFrame with columns: [code, name, date, ...]
    """
    conn = get_conn()

    # 尝试从 opportunity_item 加载
    try:
        query = """
        SELECT
            json_extract(data, '$.code') as code,
            json_extract(data, '$.name') as name,
            run_date as date,
            json_extract(data, '$.total_score') as score,
            json_extract(data, '$.scores.quant') as quant_score,
            json_extract(data, '$.scores.sector') as sector_score
        FROM opportunity_item
        WHERE run_date BETWEEN ? AND ?
        """
        df = pd.read_sql_query(query, conn, params=(start_date, end_date))
        if len(df) > 0:
            logger.info(f"从 opportunity_item 加载 {len(df)} 条历史记录")
            return df
    except Exception as e:
        logger.warning(f"opportunity_item 表不可用: {e}")

    # Fallback: 从报告文件反向解析
    results_dir = project_root / "results"
    pattern = "opportunity_top10_*.md"
    report_files = sorted(results_dir.glob(pattern))

    if not report_files:
        raise ValueError(f"未找到历史报告文件: {results_dir}/{pattern}")

    logger.info(f"从 {len(report_files)} 个报告文件提取股票池")
    # 这里简化实现,实际需要解析 markdown
    return pd.DataFrame()


def fetch_ohlcv_data(codes: List[str], start_date: str, end_date: str) -> pd.DataFrame:
    """获取 OHLCV 数据 + 计算技术指标

    Returns:
        DataFrame with columns: [code, date, open, high, low, close, volume, amount,
                                 turnover_rate, change_5d, rsi, ...]
    """
    from analysis.data_collector import DataCollector
    from analysis.technical_analysis import TechnicalAnalysis

    collector = DataCollector()
    all_data = []

    for code in codes:
        try:
            # 获取日线数据
            df = collector.fetch_stock_data(
                code=code,
                source='auto',
                period='daily',
                start_date=start_date,
                end_date=end_date
            )

            if df is None or len(df) < 20:
                continue

            # 计算技术指标
            ta = TechnicalAnalysis(df)
            indicators = ta.calculate_all_indicators()

            # 合并
            df_merged = df.copy()
            df_merged['code'] = code
            for key, val in indicators.items():
                if isinstance(val, (int, float)):
                    df_merged[key] = val

            all_data.append(df_merged)

        except Exception as e:
            logger.warning(f"股票 {code} 数据获取失败: {e}")
            continue

    if not all_data:
        raise ValueError("未获取到任何有效数据")

    result = pd.concat(all_data, ignore_index=True)
    logger.info(f"成功获取 {len(result)} 条 OHLCV 记录")
    return result


def calculate_forward_returns(df: pd.DataFrame, horizons: List[int] = [1, 3, 5, 10]) -> pd.DataFrame:
    """计算未来收益率

    Args:
        df: OHLCV DataFrame (必须包含 code, date, close 列)
        horizons: 未来 N 日收益率

    Returns:
        添加了 return_1d, return_3d, ... 列的 DataFrame
    """
    df = df.sort_values(['code', 'date']).copy()

    for h in horizons:
        df[f'return_{h}d'] = df.groupby('code')['close'].pct_change(h).shift(-h) * 100

    return df


# ============================================================
# 因子计算
# ============================================================

def compute_chip_factor(df: pd.DataFrame) -> pd.DataFrame:
    """计算筹码集中度因子"""
    df = df.copy()

    # 计算换手率 (需要流通股本数据,这里简化用成交量/总成交额估算)
    if 'turnover_rate' not in df.columns:
        df['turnover_rate'] = (df['volume'] / df['volume'].rolling(20).mean() * 5).fillna(5.0)

    # 近5日换手率
    df['turnover_5d'] = df.groupby('code')['turnover_rate'].rolling(5).apply(list).reset_index(0, drop=True)

    # 流通股本变化 (占位,需要真实数据)
    df['float_chg_ratio'] = 0.0

    # 计算因子
    def apply_chip(row):
        if pd.isna(row['turnover_5d']):
            return pd.Series({'chip_locked': None, 'turnover_stability': None})

        turnover_list = row['turnover_5d'] if isinstance(row['turnover_5d'], list) else [row['turnover_rate']] * 5
        result = compute_chip_concentration(
            turnover_rate_recent=turnover_list,
            float_share_chg_ratio=row['float_chg_ratio']
        )
        return pd.Series(result)

    df[['chip_locked', 'turnover_stability']] = df.apply(apply_chip, axis=1)
    return df


def compute_volume_factor(df: pd.DataFrame) -> pd.DataFrame:
    """计算量价配合因子"""
    df = df.copy()

    # 量比
    df['volume_ratio'] = df['volume'] / df.groupby('code')['volume'].transform(lambda x: x.rolling(20).mean())

    # 价格变化
    df['price_chg'] = df.groupby('code')['close'].pct_change() * 100

    # 成交量变化
    df['volume_chg'] = df.groupby('code')['volume'].pct_change() * 100

    # 近5日高量天数
    df['high_volume_days'] = df.groupby('code')['volume_ratio'].rolling(5).apply(lambda x: (x > 1.5).sum()).reset_index(0, drop=True)

    # 20日均量
    df['avg_volume_20d'] = df.groupby('code')['volume'].transform(lambda x: x.rolling(20).mean())

    # 计算因子
    def apply_volume(row):
        result = compute_volume_anomaly(
            volume_ratio=row['volume_ratio'],
            price_chg=row['price_chg'],
            volume_chg=row['volume_chg'],
            avg_volume_20d=row['avg_volume_20d'],
            recent_high_volume_days=int(row.get('high_volume_days', 0))
        )
        return pd.Series(result)

    df[['volume_quality', 'volume_surge_risk']] = df.apply(apply_volume, axis=1)
    return df


# ============================================================
# IC 检验
# ============================================================

def calculate_ic_series(df: pd.DataFrame, factor_col: str, return_col: str = 'return_5d') -> pd.DataFrame:
    """计算时序 IC

    Returns:
        DataFrame with columns: [date, ic, n_stocks]
    """
    ic_list = []

    for date in df['date'].unique():
        subset = df[df['date'] == date].copy()

        if len(subset) < 10:  # 样本量太小
            continue

        ic = subset[factor_col].corr(subset[return_col], method='spearman')
        ic_list.append({
            'date': date,
            'ic': ic,
            'n_stocks': len(subset)
        })

    ic_df = pd.DataFrame(ic_list)
    return ic_df


def summarize_ic(ic_df: pd.DataFrame) -> Dict:
    """IC 统计摘要"""
    return {
        'ic_mean': ic_df['ic'].mean(),
        'ic_std': ic_df['ic'].std(),
        'ic_ir': ic_df['ic'].mean() / (ic_df['ic'].std() + 1e-9),
        'ic_positive_ratio': (ic_df['ic'] > 0).sum() / len(ic_df),
        'ic_abs_mean': ic_df['ic'].abs().mean(),
    }


# ============================================================
# 单调性测试
# ============================================================

def test_monotonicity(df: pd.DataFrame, factor_col: str, return_col: str = 'return_5d', n_quantiles: int = 5) -> pd.DataFrame:
    """分层回测"""
    result = backtest_factor_monotonicity(df, factor_col, return_col, n_quantiles)

    # 计算单调性得分
    returns = result['mean_return'].values
    monotonic_score = 0
    for i in range(len(returns) - 1):
        if returns[i+1] > returns[i]:
            monotonic_score += 1

    result['monotonic_score'] = monotonic_score / (len(returns) - 1)

    return result


# ============================================================
# 双半窗检验
# ============================================================

def split_window_test(df: pd.DataFrame, factor_col: str, return_col: str = 'return_5d') -> Dict:
    """前后半窗 IC 一致性检验"""
    dates = sorted(df['date'].unique())
    mid_idx = len(dates) // 2

    first_half = df[df['date'].isin(dates[:mid_idx])]
    second_half = df[df['date'].isin(dates[mid_idx:])]

    ic_first = first_half[factor_col].corr(first_half[return_col], method='spearman')
    ic_second = second_half[factor_col].corr(second_half[return_col], method='spearman')

    return {
        'first_half_ic': ic_first,
        'second_half_ic': ic_second,
        'consistent': (ic_first * ic_second) > 0,  # 同号
        'ic_diff': abs(ic_first - ic_second)
    }


# ============================================================
# 主流程
# ============================================================

def validate_factor(
    factor_name: str,
    df: pd.DataFrame,
    return_col: str = 'return_5d',
    output_dir: Optional[Path] = None
) -> Dict:
    """单因子完整验证流程"""
    logger.info(f"\n{'='*60}")
    logger.info(f"开始验证因子: {factor_name}")
    logger.info(f"{'='*60}")

    # 1. IC 检验
    logger.info("1/4 计算时序 IC...")
    ic_series = calculate_ic_series(df, factor_name, return_col)
    ic_summary = summarize_ic(ic_series)

    logger.info(f"  IC 均值: {ic_summary['ic_mean']:.4f}")
    logger.info(f"  IC 标准差: {ic_summary['ic_std']:.4f}")
    logger.info(f"  IC_IR: {ic_summary['ic_ir']:.4f}")
    logger.info(f"  正IC占比: {ic_summary['ic_positive_ratio']:.2%}")

    # 2. 单调性测试
    logger.info("2/4 分层回测...")
    df_clean = df.dropna(subset=[factor_name, return_col])
    monotonicity = test_monotonicity(df_clean, factor_name, return_col)

    logger.info("  分层收益:")
    for _, row in monotonicity.iterrows():
        logger.info(f"    Q{int(row['quantile'])+1}: {row['mean_return']:+.2f}% (胜率 {row['win_rate']:.1%}, n={int(row['count'])})")

    # 3. 双半窗检验
    logger.info("3/4 双半窗一致性检验...")
    split_test = split_window_test(df_clean, factor_name, return_col)

    logger.info(f"  前半窗 IC: {split_test['first_half_ic']:+.4f}")
    logger.info(f"  后半窗 IC: {split_test['second_half_ic']:+.4f}")
    logger.info(f"  方向一致: {'✓' if split_test['consistent'] else '✗'}")

    # 4. 综合评分
    logger.info("4/4 综合评分...")

    score = 0
    reasons = []

    # IC 标准
    if abs(ic_summary['ic_mean']) >= 0.05:
        score += 40
        reasons.append("IC > 0.05 (强)")
    elif abs(ic_summary['ic_mean']) >= 0.03:
        score += 30
        reasons.append("IC > 0.03 (合格)")
    else:
        reasons.append("IC < 0.03 (不合格)")

    # 单调性
    mono_score = monotonicity['monotonic_score'].iloc[0]
    if mono_score >= 0.75:
        score += 30
        reasons.append("单调性强")
    elif mono_score >= 0.5:
        score += 20
        reasons.append("单调性一般")

    # 一致性
    if split_test['consistent']:
        score += 30
        reasons.append("双半窗一致")
    else:
        reasons.append("双半窗不一致 (⚠️)")

    # 判定
    if score >= 80:
        verdict = "✅ 通过 — 建议纳入 v26"
    elif score >= 60:
        verdict = "⚠️ 观察 — 需进一步优化"
    else:
        verdict = "❌ 不通过 — 暂不采用"

    logger.info(f"\n  综合得分: {score}/100")
    logger.info(f"  判定: {verdict}")
    logger.info(f"  理由: {', '.join(reasons)}")

    # 保存结果
    result = {
        'factor_name': factor_name,
        'ic_summary': ic_summary,
        'monotonicity': monotonicity.to_dict('records'),
        'split_window': split_test,
        'score': score,
        'verdict': verdict,
        'reasons': reasons,
        'timestamp': datetime.now().isoformat()
    }

    if output_dir:
        output_file = output_dir / f"validation_{factor_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        output_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        logger.info(f"\n结果已保存: {output_file}")

    return result


def main():
    parser = argparse.ArgumentParser(description='v26 因子验证')
    parser.add_argument('--factor', type=str, help='指定因子名称')
    parser.add_argument('--all', action='store_true', help='验证所有新因子')
    parser.add_argument('--start', type=str, default='2025-11-01', help='开始日期')
    parser.add_argument('--end', type=str, default='2026-07-31', help='结束日期')
    parser.add_argument('--output', type=str, default='results/factor_validation_v26', help='输出目录')

    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 因子列表
    if args.all:
        factors = ['chip_locked', 'turnover_stability', 'volume_quality',
                   'volume_surge_risk', 'relative_strength', 'divergence_signal']
    else:
        factors = [args.factor] if args.factor else []

    if not factors:
        logger.error("请指定 --factor 或 --all")
        return 1

    logger.info(f"验证因子: {', '.join(factors)}")
    logger.info(f"日期范围: {args.start} ~ {args.end}")

    # 加载数据
    logger.info("\n加载基础数据...")
    try:
        stock_pool = load_stock_pool(args.start, args.end)
        codes = stock_pool['code'].unique().tolist()

        if len(codes) == 0:
            logger.error("股票池为空")
            return 1

        logger.info(f"股票池规模: {len(codes)} 只")

        # 获取 OHLCV
        df = fetch_ohlcv_data(codes, args.start, args.end)
        df = calculate_forward_returns(df)

        # 计算因子
        logger.info("\n计算因子...")
        if any(f in factors for f in ['chip_locked', 'turnover_stability']):
            df = compute_chip_factor(df)

        if any(f in factors for f in ['volume_quality', 'volume_surge_risk']):
            df = compute_volume_factor(df)

        # 逐因子验证
        results = {}
        for factor in factors:
            if factor not in df.columns:
                logger.warning(f"因子 {factor} 未计算,跳过")
                continue

            result = validate_factor(factor, df, output_dir=output_dir)
            results[factor] = result

        # 汇总报告
        logger.info(f"\n{'='*60}")
        logger.info("验证汇总")
        logger.info(f"{'='*60}")

        for factor, result in results.items():
            logger.info(f"{factor:30s} {result['verdict']}")

        logger.info(f"\n详细结果: {output_dir}")

    except Exception as e:
        logger.exception(f"验证失败: {e}")
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
