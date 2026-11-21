#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""增强因子库 v26 候选 — 基于回测验证的高价值因子
=====================================

新增因子方向（需要数据回填后验证）:
1. 筹码集中度 — 近期换手率 + 流通盘变化
2. 异常成交量 — 量比突破 + 量价背离
3. 市场情绪对冲 — 个股强度 vs 板块/大盘强度的偏离度
4. 时间窗口优化 — 开盘竞价强度 + 尾盘资金动向
5. 机构行为 — 龙虎榜机构席位 + 北向资金持仓变化

设计原则:
- 每个因子必须先用历史数据验证 IC > 0.03 且双半窗一致
- 优先选择与现有因子相关性 < 0.6 的正交维度
- 避免过拟合: 新因子回测样本量 >= 500
"""
from typing import Dict, List, Optional, Tuple
import pandas as pd
import numpy as np


def compute_chip_concentration(
    turnover_rate_recent: List[float],
    float_share_chg_ratio: float
) -> Dict[str, float]:
    """筹码集中度因子

    逻辑: 低换手 + 流通盘稳定 = 筹码锁定良好

    Args:
        turnover_rate_recent: 近5日换手率列表
        float_share_chg_ratio: 近3月流通股本变化比例

    Returns:
        {
            'chip_locked': 0-100, 高=筹码集中
            'turnover_stability': 0-100, 高=换手率稳定
        }
    """
    if not turnover_rate_recent or len(turnover_rate_recent) < 3:
        return {'chip_locked': None, 'turnover_stability': None}

    avg_turnover = np.mean(turnover_rate_recent)
    std_turnover = np.std(turnover_rate_recent)

    # 换手率越低越好（但不是绝对低,避免僵尸股）
    # 理想区间: 2-8%
    if avg_turnover < 2:
        turnover_score = 30  # 过低,可能流动性不足
    elif avg_turnover <= 8:
        turnover_score = 100 - (avg_turnover - 2) * 5  # 线性递减
    else:
        turnover_score = max(0, 100 - (avg_turnover - 8) * 10)

    # 稳定性: 标准差越小越好
    stability_score = max(0, 100 - std_turnover * 20)

    # 流通盘变化: 绝对值越小越好
    float_penalty = min(50, abs(float_share_chg_ratio) * 100)

    chip_locked = (turnover_score * 0.6 + stability_score * 0.4 - float_penalty)
    chip_locked = max(0, min(100, chip_locked))

    return {
        'chip_locked': round(chip_locked, 2),
        'turnover_stability': round(stability_score, 2)
    }


def compute_volume_anomaly(
    volume_ratio: float,
    price_chg: float,
    volume_chg: float,
    avg_volume_20d: float,
    recent_high_volume_days: int
) -> Dict[str, float]:
    """成交量异常检测

    关注点:
    - 放量突破 vs 缩量阴跌
    - 量价背离（价涨量缩 = 警告）
    - 持续放量 vs 脉冲放量

    Returns:
        {
            'volume_quality': 0-100, 量价配合度
            'volume_surge_risk': 0-100, 异常放量风险
        }
    """
    if volume_ratio is None or volume_ratio <= 0:
        return {'volume_quality': None, 'volume_surge_risk': None}

    # 量价配合度
    if price_chg > 3 and volume_ratio > 1.5:
        # 上涨放量 = 健康
        quality = min(100, 60 + volume_ratio * 10)
    elif price_chg > 0 and volume_ratio < 0.8:
        # 价涨量缩 = 背离风险
        quality = 30
    elif price_chg < -3 and volume_ratio > 2.0:
        # 下跌放量 = 恐慌盘
        quality = 20
    elif price_chg < 0 and volume_ratio < 0.8:
        # 缩量阴跌 = 观望
        quality = 40
    else:
        quality = 50

    # 异常放量风险（单日暴量 + 非持续性）
    if volume_ratio > 3.0 and recent_high_volume_days <= 1:
        surge_risk = min(100, (volume_ratio - 3) * 20 + 50)
    elif volume_ratio > 2.0 and recent_high_volume_days <= 2:
        surge_risk = 40
    else:
        surge_risk = max(0, (volume_ratio - 1.5) * 10)

    return {
        'volume_quality': round(quality, 2),
        'volume_surge_risk': round(surge_risk, 2)
    }


def compute_relative_strength_divergence(
    stock_chg_5d: float,
    sector_chg_5d: float,
    index_chg_5d: float,
    stock_rsi: float
) -> Dict[str, float]:
    """相对强度偏离 — 个股 vs 板块/大盘的独立性

    强势股特征: 大盘跌它抗跌,大盘涨它更强
    弱势股特征: 跟跌不跟涨

    Returns:
        {
            'relative_strength': -100 to 100, 正=强于大盘
            'divergence_signal': 0-100, 高=值得关注的偏离
        }
    """
    if None in [stock_chg_5d, index_chg_5d]:
        return {'relative_strength': None, 'divergence_signal': None}

    # 相对强度 = 个股涨幅 - 指数涨幅
    rel_strength = stock_chg_5d - index_chg_5d

    # 如果有板块数据,优先用板块对比
    if sector_chg_5d is not None:
        sector_rel = stock_chg_5d - sector_chg_5d
        rel_strength = sector_rel * 0.6 + rel_strength * 0.4

    # 偏离信号强度
    if abs(rel_strength) > 10:
        # 大幅偏离
        divergence = min(100, abs(rel_strength) * 5)
        # 结合 RSI: 强势股如果RSI<50 = 调整机会
        if rel_strength > 0 and stock_rsi and stock_rsi < 50:
            divergence = min(100, divergence * 1.3)
    else:
        divergence = abs(rel_strength) * 3

    return {
        'relative_strength': round(rel_strength, 2),
        'divergence_signal': round(min(100, divergence), 2)
    }


def compute_intraday_timing_signal(
    auction_strength: float,  # 集合竞价强度 (开盘价-昨收)/昨收 %
    tail_money_flow: float,   # 尾盘30分钟资金净流入(万元)
    intraday_amplitude: float # 当日振幅 %
) -> Dict[str, float]:
    """盘口时间窗口信号

    开盘竞价: 高开+放量 = 资金主动性强
    尾盘资金: 尾盘拉升/砸盘 反映主力意图

    Returns:
        {
            'auction_quality': 0-100
            'tail_signal': -100 to 100, 正=尾盘流入
        }
    """
    if auction_strength is None:
        return {'auction_quality': None, 'tail_signal': None}

    # 竞价质量: 适度高开 (0.5-2%) 最佳
    if 0.5 <= auction_strength <= 2.0:
        auction_q = 80 + (2 - abs(auction_strength - 1.25)) * 10
    elif -1.0 <= auction_strength < 0.5:
        auction_q = 50
    elif auction_strength > 2.0:
        # 过度高开需警惕
        auction_q = max(30, 80 - (auction_strength - 2) * 15)
    else:
        auction_q = 40

    # 尾盘信号: 归一化到 -100~100
    if tail_money_flow is not None:
        tail_sig = np.clip(tail_money_flow / 1000, -100, 100)  # 除以1000归一化
    else:
        tail_sig = None

    return {
        'auction_quality': round(auction_q, 2),
        'tail_signal': round(tail_sig, 2) if tail_sig is not None else None
    }


def compute_institutional_footprint(
    lhb_inst_count: int,      # 龙虎榜机构席位数
    lhb_inst_net: float,      # 机构席位净买入额(万元)
    northbound_chg_5d: float, # 北向资金5日持仓变化(万股)
    qfii_holder_count: int    # QFII持股家数
) -> Dict[str, float]:
    """机构行为痕迹

    机构扎堆 vs 先知先觉 的区分

    Returns:
        {
            'inst_interest': 0-100, 机构关注度
            'smart_money_signal': -100 to 100, 聪明钱方向
        }
    """
    inst_score = 0

    # 龙虎榜机构
    if lhb_inst_count is not None and lhb_inst_count > 0:
        inst_score += min(30, lhb_inst_count * 10)
        if lhb_inst_net and lhb_inst_net > 0:
            inst_score += min(20, lhb_inst_net / 1000)

    # 北向资金
    if northbound_chg_5d is not None:
        if northbound_chg_5d > 0:
            inst_score += min(30, northbound_chg_5d / 100)

    # QFII
    if qfii_holder_count is not None and qfii_holder_count > 0:
        inst_score += min(20, qfii_holder_count * 5)

    # 聪明钱方向 = 北向 + 机构龙虎榜
    smart_signal = 0
    if northbound_chg_5d:
        smart_signal += np.clip(northbound_chg_5d / 50, -50, 50)
    if lhb_inst_net:
        smart_signal += np.clip(lhb_inst_net / 5000, -50, 50)

    return {
        'inst_interest': round(min(100, inst_score), 2),
        'smart_money_signal': round(smart_signal, 2)
    }


# ============================================================
# 因子验证框架
# ============================================================

def validate_factor_ic(
    factor_values: pd.Series,
    forward_returns: pd.Series,
    method: str = 'spearman'
) -> Dict[str, float]:
    """计算因子 IC (Information Coefficient)

    Args:
        factor_values: 因子值序列
        forward_returns: 对应的未来收益率
        method: 'spearman' or 'pearson'

    Returns:
        {
            'ic': IC值,
            'ic_mean': 滚动IC均值,
            'ic_std': IC标准差,
            'ic_ir': IC信息比率 (mean/std)
        }
    """
    if method == 'spearman':
        ic = factor_values.corr(forward_returns, method='spearman')
    else:
        ic = factor_values.corr(forward_returns, method='pearson')

    # 这里简化返回,实际应该用滚动窗口计算时序IC
    return {
        'ic': round(ic, 4),
        'ic_mean': round(ic, 4),  # 需要时序数据
        'ic_std': 0.0,  # 占位
        'ic_ir': 0.0    # 占位
    }


def backtest_factor_monotonicity(
    df: pd.DataFrame,
    factor_col: str,
    return_col: str,
    n_quantiles: int = 5
) -> pd.DataFrame:
    """分层回测 — 验证因子单调性

    将因子值分成N层,计算每层的平均收益和胜率
    理想情况: 随着因子值增大,收益单调递增/递减

    Returns:
        DataFrame with columns: [quantile, count, mean_return, win_rate]
    """
    df = df.copy()
    df['quantile'] = pd.qcut(df[factor_col], q=n_quantiles, labels=False, duplicates='drop')

    results = df.groupby('quantile').agg({
        return_col: ['count', 'mean', lambda x: (x > 0).sum() / len(x)]
    }).round(4)

    results.columns = ['count', 'mean_return', 'win_rate']
    results = results.reset_index()

    return results
