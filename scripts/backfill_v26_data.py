#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v26 数据补全脚本 — 补充筹码/机构行为所需的基础数据

需要补充的数据:
1. 流通股本历史 (daily_basic 表) — 计算换手率、筹码集中度
2. 北向资金持仓 (hk_hold 表) — 机构行为追踪
3. QFII 持股 (stk_holdertrade 表) — 机构关注度
4. 龙虎榜补充字段 (已有部分,补充机构席位统计)

使用方法:
    python scripts/backfill_v26_data.py --start 2025-11-01 --end 2026-07-31 --all
    python scripts/backfill_v26_data.py --table daily_basic  # 只补充指定表
"""
import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import pandas as pd

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from data_store.connection import get_conn
from analysis.retry_utils import with_retries

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)


def ensure_table_schema():
    """确保所需表结构存在"""
    conn = get_conn()

    # daily_basic 表 (流通股本、换手率等)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS daily_basic (
        ts_code TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        close REAL,
        turnover_rate REAL,
        turnover_rate_f REAL,
        volume_ratio REAL,
        pe REAL,
        pe_ttm REAL,
        pb REAL,
        ps REAL,
        ps_ttm REAL,
        dv_ratio REAL,
        dv_ttm REAL,
        total_share REAL,
        float_share REAL,
        free_share REAL,
        total_mv REAL,
        circ_mv REAL,
        PRIMARY KEY (ts_code, trade_date)
    )
    """)

    # 北向资金持仓表
    conn.execute("""
    CREATE TABLE IF NOT EXISTS hk_hold (
        ts_code TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        hold_volume REAL,
        hold_ratio REAL,
        exchange TEXT,
        PRIMARY KEY (ts_code, trade_date)
    )
    """)

    # QFII 持股表
    conn.execute("""
    CREATE TABLE IF NOT EXISTS qfii_hold (
        ts_code TEXT NOT NULL,
        end_date TEXT NOT NULL,
        holder_name TEXT,
        hold_volume REAL,
        hold_ratio REAL,
        PRIMARY KEY (ts_code, end_date, holder_name)
    )
    """)

    conn.commit()
    logger.info("✓ 表结构检查完成")


def backfill_daily_basic(start_date: str, end_date: str, codes: Optional[List[str]] = None):
    """补充 daily_basic 数据 (流通股本、换手率)"""
    logger.info(f"\n{'='*60}")
    logger.info("补充 daily_basic 数据 (流通股本、换手率)")
    logger.info(f"{'='*60}")

    try:
        import tushare as ts
        from config.tushare_config import get_tushare_token

        token = get_tushare_token()
        if not token:
            logger.error("Tushare token 未配置")
            return

        pro = ts.pro_api(token)

        # 获取需要补充的股票列表
        conn = get_conn()
        if codes is None:
            # 从现有数据中提取股票代码
            cursor = conn.execute("""
                SELECT DISTINCT ts_code FROM moneyflow_dc
                WHERE trade_date BETWEEN ? AND ?
                LIMIT 1000
            """, (start_date, end_date))
            codes = [row[0] for row in cursor.fetchall()]

        if not codes:
            logger.warning("未找到需要补充的股票")
            return

        logger.info(f"目标股票: {len(codes)} 只")

        # 逐股票补充
        success_count = 0
        error_count = 0

        for idx, ts_code in enumerate(codes, 1):
            try:
                logger.info(f"[{idx}/{len(codes)}] {ts_code}")

                @with_retries(max_attempts=3, base_delay=1.0)
                def fetch():
                    return pro.daily_basic(
                        ts_code=ts_code,
                        start_date=start_date.replace('-', ''),
                        end_date=end_date.replace('-', ''),
                        fields='ts_code,trade_date,close,turnover_rate,turnover_rate_f,'
                               'volume_ratio,pe,pe_ttm,pb,ps,ps_ttm,'
                               'total_share,float_share,free_share,total_mv,circ_mv'
                    )

                df = fetch()

                if df is None or len(df) == 0:
                    logger.warning(f"  无数据")
                    error_count += 1
                    continue

                # 写入数据库
                df.to_sql('daily_basic', conn, if_exists='append', index=False)
                success_count += 1

                logger.info(f"  ✓ {len(df)} 条记录")

                # 限频: 每分钟 200 次
                time.sleep(0.31)

            except Exception as e:
                logger.error(f"  ✗ {e}")
                error_count += 1
                continue

        conn.commit()
        logger.info(f"\n完成: 成功 {success_count}, 失败 {error_count}")

    except ImportError:
        logger.error("tushare 未安装: pip install tushare")
    except Exception as e:
        logger.exception(f"补充失败: {e}")


def backfill_hk_hold(start_date: str, end_date: str):
    """补充北向资金持仓数据"""
    logger.info(f"\n{'='*60}")
    logger.info("补充北向资金持仓数据")
    logger.info(f"{'='*60}")

    try:
        import tushare as ts
        from config.tushare_config import get_tushare_token

        token = get_tushare_token()
        if not token:
            logger.error("Tushare token 未配置")
            return

        pro = ts.pro_api(token)
        conn = get_conn()

        # 按日期遍历
        start = datetime.strptime(start_date, '%Y-%m-%d')
        end = datetime.strptime(end_date, '%Y-%m-%d')
        current = start

        total_records = 0

        while current <= end:
            trade_date = current.strftime('%Y%m%d')

            try:
                logger.info(f"日期: {trade_date}")

                @with_retries(max_attempts=3, base_delay=1.0)
                def fetch():
                    return pro.hk_hold(trade_date=trade_date)

                df = fetch()

                if df is not None and len(df) > 0:
                    df.to_sql('hk_hold', conn, if_exists='append', index=False)
                    total_records += len(df)
                    logger.info(f"  ✓ {len(df)} 条记录")
                else:
                    logger.info(f"  无数据 (非交易日或无持仓变动)")

                time.sleep(0.31)

            except Exception as e:
                logger.error(f"  ✗ {e}")

            current += timedelta(days=1)

        conn.commit()
        logger.info(f"\n完成: 共 {total_records} 条记录")

    except ImportError:
        logger.error("tushare 未安装: pip install tushare")
    except Exception as e:
        logger.exception(f"补充失败: {e}")


def backfill_qfii_hold(start_date: str, end_date: str):
    """补充 QFII 持股数据 (按季度)"""
    logger.info(f"\n{'='*60}")
    logger.info("补充 QFII 持股数据")
    logger.info(f"{'='*60}")

    try:
        import tushare as ts
        from config.tushare_config import get_tushare_token

        token = get_tushare_token()
        if not token:
            logger.error("Tushare token 未配置")
            return

        pro = ts.pro_api(token)
        conn = get_conn()

        # 生成季度列表
        start = datetime.strptime(start_date, '%Y-%m-%d')
        end = datetime.strptime(end_date, '%Y-%m-%d')

        quarters = []
        year = start.year
        quarter = (start.month - 1) // 3 + 1

        while True:
            end_date_str = f"{year}{quarter * 3:02d}{[31, 30, 30, 31][quarter-1]}"
            quarters.append(end_date_str)

            quarter += 1
            if quarter > 4:
                quarter = 1
                year += 1

            if datetime.strptime(end_date_str, '%Y%m%d') > end:
                break

        logger.info(f"季度: {', '.join(quarters)}")

        total_records = 0

        for end_date_q in quarters:
            try:
                logger.info(f"季度: {end_date_q}")

                @with_retries(max_attempts=3, base_delay=1.0)
                def fetch():
                    return pro.stk_holdertrade(end_date=end_date_q)

                df = fetch()

                if df is not None and len(df) > 0:
                    # 只保留 QFII
                    df = df[df['holder_type'] == 'QFII'].copy()

                    if len(df) > 0:
                        df = df.rename(columns={'end_date': 'end_date'})
                        df.to_sql('qfii_hold', conn, if_exists='append', index=False)
                        total_records += len(df)
                        logger.info(f"  ✓ {len(df)} 条 QFII 记录")
                else:
                    logger.info(f"  无数据")

                time.sleep(1.0)  # QFII 数据限频更严格

            except Exception as e:
                logger.error(f"  ✗ {e}")

        conn.commit()
        logger.info(f"\n完成: 共 {total_records} 条记录")

    except ImportError:
        logger.error("tushare 未安装: pip install tushare")
    except Exception as e:
        logger.exception(f"补充失败: {e}")


def verify_data_coverage(start_date: str, end_date: str):
    """验证数据覆盖率"""
    logger.info(f"\n{'='*60}")
    logger.info("数据覆盖率验证")
    logger.info(f"{'='*60}")

    conn = get_conn()

    tables = {
        'daily_basic': 'trade_date',
        'hk_hold': 'trade_date',
        'qfii_hold': 'end_date'
    }

    for table, date_col in tables.items():
        try:
            cursor = conn.execute(f"""
                SELECT COUNT(*) as total,
                       COUNT(DISTINCT ts_code) as stocks,
                       MIN({date_col}) as min_date,
                       MAX({date_col}) as max_date
                FROM {table}
                WHERE {date_col} BETWEEN ? AND ?
            """, (start_date.replace('-', ''), end_date.replace('-', '')))

            row = cursor.fetchone()
            logger.info(f"{table:20s} | 记录: {row[0]:>6} | 股票: {row[1]:>4} | 日期: {row[2]} ~ {row[3]}")

        except Exception as e:
            logger.warning(f"{table:20s} | ✗ {e}")

    logger.info(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(description='v26 数据补全')
    parser.add_argument('--start', type=str, default='2025-11-01', help='开始日期')
    parser.add_argument('--end', type=str, default='2026-07-31', help='结束日期')
    parser.add_argument('--table', type=str, choices=['daily_basic', 'hk_hold', 'qfii_hold'],
                        help='指定补充的表')
    parser.add_argument('--all', action='store_true', help='补充所有表')
    parser.add_argument('--verify-only', action='store_true', help='只验证不补充')

    args = parser.parse_args()

    logger.info(f"日期范围: {args.start} ~ {args.end}")

    # 确保表结构
    ensure_table_schema()

    if args.verify_only:
        verify_data_coverage(args.start, args.end)
        return 0

    # 执行补充
    if args.all or args.table == 'daily_basic':
        backfill_daily_basic(args.start, args.end)

    if args.all or args.table == 'hk_hold':
        backfill_hk_hold(args.start, args.end)

    if args.all or args.table == 'qfii_hold':
        backfill_qfii_hold(args.start, args.end)

    # 验证
    verify_data_coverage(args.start, args.end)

    return 0


if __name__ == '__main__':
    sys.exit(main())
