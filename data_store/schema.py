"""DDL + version-tracked migrations for lumo_data.sqlite.

Each migration is an (version, sql) pair. `migrate()` applies any version
strictly greater than the current `schema_version.MAX(version)` and records
the new version. Safe to call repeatedly.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from typing import List, Tuple


_MIGRATIONS: List[Tuple[int, str]] = [
    (
        1,
        # OHLCV time-series (P1)
        """
        CREATE TABLE IF NOT EXISTS ohlcv (
          code       TEXT NOT NULL,
          frequency  TEXT NOT NULL CHECK(frequency IN ('1d','5m','15m','30m','60m')),
          ts         TEXT NOT NULL,
          open       REAL,
          high       REAL,
          low        REAL,
          close      REAL,
          volume     REAL,
          amount     REAL,
          PRIMARY KEY (code, frequency, ts)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS idx_ohlcv_code_freq_ts ON ohlcv(code, frequency, ts DESC);
        """,
    ),
    (
        2,
        # Sentiment cache (P2)
        """
        CREATE TABLE IF NOT EXISTS sentiment_cache (
          cache_type   TEXT NOT NULL,
          identifier   TEXT NOT NULL DEFAULT '',
          payload      TEXT NOT NULL,
          updated_at   TEXT NOT NULL,
          ttl_seconds  INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (cache_type, identifier)
        );
        """,
    ),
    (
        3,
        # daily_basic + trade_calendar (P3)
        """
        CREATE TABLE IF NOT EXISTS daily_basic (
          ts_code         TEXT NOT NULL,
          trade_date      TEXT NOT NULL,
          close           REAL,
          turnover_rate   REAL,
          turnover_rate_f REAL,
          volume_ratio    REAL,
          pe              REAL,
          pe_ttm          REAL,
          pb              REAL,
          ps              REAL,
          ps_ttm          REAL,
          dv_ratio        REAL,
          dv_ttm          REAL,
          total_share     REAL,
          float_share     REAL,
          free_share      REAL,
          total_mv        REAL,
          circ_mv         REAL,
          PRIMARY KEY (ts_code, trade_date)
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS trade_calendar (
          cal_date TEXT PRIMARY KEY,
          is_open  INTEGER NOT NULL DEFAULT 1
        );
        """,
    ),
    (
        4,
        # moneyflow_dc + kv_cache (P4)
        """
        CREATE TABLE IF NOT EXISTS moneyflow_dc (
          trade_date          TEXT NOT NULL,
          ts_code             TEXT NOT NULL,
          top_n               INTEGER NOT NULL,
          name                TEXT,
          pct_change          REAL,
          close               REAL,
          net_amount          REAL,
          net_amount_rate     REAL,
          buy_elg_amount      REAL,
          buy_elg_amount_rate REAL,
          buy_lg_amount       REAL,
          buy_lg_amount_rate  REAL,
          buy_md_amount       REAL,
          buy_md_amount_rate  REAL,
          buy_sm_amount       REAL,
          buy_sm_amount_rate  REAL,
          amount_unit         TEXT,
          PRIMARY KEY (trade_date, ts_code, top_n)
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS kv_cache (
          namespace   TEXT NOT NULL,
          key         TEXT NOT NULL,
          payload     TEXT NOT NULL,
          updated_at  TEXT NOT NULL,
          ttl_seconds INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (namespace, key)
        );
        """,
    ),
    (
        5,
        # Full-market daily snapshots: market_daily + market_flow_daily (P4)
        """
        CREATE TABLE IF NOT EXISTS market_daily (
          ts_code   TEXT NOT NULL,
          trade_date TEXT NOT NULL,
          open  REAL, high REAL, low REAL, close REAL, pre_close REAL,
          change REAL, pct_chg REAL,
          vol    REAL, amount REAL,
          PRIMARY KEY (ts_code, trade_date)
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS market_flow_daily (
          trade_date          TEXT NOT NULL,
          ts_code             TEXT NOT NULL,
          name                TEXT,
          pct_change          REAL,
          close               REAL,
          net_amount          REAL,
          net_amount_rate     REAL,
          buy_elg_amount      REAL,
          buy_elg_amount_rate REAL,
          buy_lg_amount       REAL,
          buy_lg_amount_rate  REAL,
          buy_md_amount       REAL,
          buy_md_amount_rate  REAL,
          buy_sm_amount       REAL,
          buy_sm_amount_rate  REAL,
          PRIMARY KEY (trade_date, ts_code)
        ) WITHOUT ROWID;
        """,
    ),
    (
        6,
        """
        CREATE TABLE IF NOT EXISTS dragon_tiger_inst (
          ts_code          TEXT NOT NULL,
          trade_date       TEXT NOT NULL,
          inst_name        TEXT NOT NULL,
          side             TEXT NOT NULL,
          net_amount       REAL,
          buy_amount       REAL,
          sell_amount      REAL,
          is_quant         INTEGER DEFAULT 0,
          quant_confidence TEXT,
          reason           TEXT,
          PRIMARY KEY (ts_code, trade_date, inst_name, side)
        );
        CREATE INDEX IF NOT EXISTS idx_lhbi_code_date
          ON dragon_tiger_inst(ts_code, trade_date);
        CREATE INDEX IF NOT EXISTS idx_lhbi_quant
          ON dragon_tiger_inst(is_quant, trade_date);

        CREATE TABLE IF NOT EXISTS hsgt_individual (
          ts_code     TEXT NOT NULL,
          trade_date  TEXT NOT NULL,
          hold_vol    REAL,
          hold_ratio  REAL,
          market_cap  REAL,
          PRIMARY KEY (ts_code, trade_date)
        );

        CREATE TABLE IF NOT EXISTS top10_floatholders (
          ts_code       TEXT NOT NULL,
          end_date      TEXT NOT NULL,
          holder_rank   INTEGER NOT NULL,
          holder_name   TEXT NOT NULL,
          hold_amount   REAL,
          hold_ratio    REAL,
          change_type   TEXT,
          change_amount REAL,
          PRIMARY KEY (ts_code, end_date, holder_rank)
        );

        CREATE TABLE IF NOT EXISTS stk_holdernumber (
          ts_code     TEXT NOT NULL,
          end_date    TEXT NOT NULL,
          holder_num  INTEGER,
          avg_hold    REAL,
          pct_change  REAL,
          PRIMARY KEY (ts_code, end_date)
        );

        CREATE TABLE IF NOT EXISTS jgdy_detail (
          ts_code      TEXT NOT NULL,
          survey_date  TEXT NOT NULL,
          inst_name    TEXT NOT NULL,
          reception    TEXT,
          topic        TEXT,
          PRIMARY KEY (ts_code, survey_date, inst_name)
        );

        CREATE TABLE IF NOT EXISTS fund_hold_detail (
          ts_code      TEXT NOT NULL,
          end_date     TEXT NOT NULL,
          fund_code    TEXT NOT NULL,
          fund_name    TEXT,
          hold_shares  REAL,
          market_value REAL,
          nv_ratio     REAL,
          PRIMARY KEY (ts_code, end_date, fund_code)
        );

        CREATE TABLE IF NOT EXISTS sync_log (
          source    TEXT NOT NULL,
          ts_code   TEXT NOT NULL DEFAULT '',
          ran_at    TEXT NOT NULL,
          status    TEXT NOT NULL,
          rows      INTEGER,
          error     TEXT,
          PRIMARY KEY (source, ts_code, ran_at)
        );
        CREATE INDEX IF NOT EXISTS idx_synclog_source_ran
          ON sync_log(source, ran_at DESC);
        """,
    ),
    (
        7,
        # 资金榜单(龙虎榜单,含游资净额) + 模拟盘台账 (P5)
        """
        -- 龙虎榜单(Tushare top_list 口径:个股/日,net_amount 含游资净买入额)。
        -- 同一股票同日可因多条「上榜原因」出现多行,故 reason 进主键。
        CREATE TABLE IF NOT EXISTS dragon_tiger_list (
          trade_date    TEXT NOT NULL,
          ts_code       TEXT NOT NULL,
          name          TEXT,
          close         REAL,
          pct_change    REAL,
          turnover_rate REAL,
          amount        REAL,   -- 当日总成交额
          l_buy         REAL,   -- 龙虎榜买入额
          l_sell        REAL,   -- 龙虎榜卖出额
          l_amount      REAL,   -- 龙虎榜成交额
          net_amount    REAL,   -- 龙虎榜净买入额(含游资) ← 排序键
          net_rate      REAL,
          amount_rate   REAL,
          reason        TEXT NOT NULL DEFAULT '',  -- 上榜原因
          PRIMARY KEY (trade_date, ts_code, reason)
        );
        CREATE INDEX IF NOT EXISTS idx_dtl_date_net ON dragon_tiger_list(trade_date, net_amount DESC);
        CREATE INDEX IF NOT EXISTS idx_dtl_code     ON dragon_tiger_list(ts_code, trade_date);

        -- 模拟盘单账户(id 恒为 1)
        CREATE TABLE IF NOT EXISTS paper_account (
          id           INTEGER PRIMARY KEY CHECK(id=1),
          initial_cash REAL NOT NULL,
          cash         REAL NOT NULL,
          created_at   TEXT NOT NULL,
          updated_at   TEXT NOT NULL
        );

        -- 委托(即时单直接 filled;open/close/limit 进 pending 等撮合)
        CREATE TABLE IF NOT EXISTS paper_order (
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          ts_code       TEXT NOT NULL,
          name          TEXT,
          side          TEXT NOT NULL CHECK(side IN ('buy','sell')),
          price_type    TEXT NOT NULL CHECK(price_type IN ('market','open','close','limit')),
          limit_price   REAL,
          qty           INTEGER,
          amount_budget REAL,
          status        TEXT NOT NULL CHECK(status IN ('pending','filled','cancelled','rejected')),
          created_at    TEXT NOT NULL,
          created_date  TEXT NOT NULL,
          filled_at     TEXT,
          filled_price  REAL,
          filled_qty    INTEGER,
          fee           REAL,
          note          TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_porder_status ON paper_order(status, ts_code);

        -- 持仓(含费摊薄均价)
        CREATE TABLE IF NOT EXISTS paper_position (
          ts_code    TEXT PRIMARY KEY,
          name       TEXT,
          qty        INTEGER NOT NULL,
          avg_cost   REAL NOT NULL,
          opened_at  TEXT,
          updated_at TEXT
        );

        -- 成交流水(每笔 fill;卖出结算 realized_pnl)
        CREATE TABLE IF NOT EXISTS paper_trade (
          id           INTEGER PRIMARY KEY AUTOINCREMENT,
          order_id     INTEGER,
          ts_code      TEXT NOT NULL,
          name         TEXT,
          side         TEXT NOT NULL,
          price        REAL NOT NULL,
          qty          INTEGER NOT NULL,
          gross        REAL NOT NULL,
          fee          REAL NOT NULL,
          realized_pnl REAL,
          traded_at    TEXT NOT NULL,
          trade_date   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ptrade_code ON paper_trade(ts_code, traded_at);

        -- 每日权益曲线(回撤/收益率)
        CREATE TABLE IF NOT EXISTS paper_equity_curve (
          trade_date     TEXT PRIMARY KEY,
          cash           REAL,
          position_value REAL,
          total_equity   REAL,
          daily_pnl      REAL
        );

        -- 费用等配置(键值)
        CREATE TABLE IF NOT EXISTS paper_settings (
          key   TEXT PRIMARY KEY,
          value TEXT
        );
        """,
    ),
    (
        8,
        # 保留资金榜接口原始列,用于前端展开展示非标准字段。
        "",
    ),
    (
        9,
        # 投资机会挖掘结果入库(按天):每次 run 一行 + 全量评分明细。
        # 桌面 job 与 quick_start CLI 共用 OpportunityDiscovery.run 落库,
        # ruleset_version/config_hash 让两侧分数可比对、分歧可解释。
        """
        CREATE TABLE IF NOT EXISTS opportunity_run (
          id              INTEGER PRIMARY KEY AUTOINCREMENT,
          run_at          TEXT NOT NULL,            -- ISO 时间
          run_date        TEXT NOT NULL,            -- YYYY-MM-DD(按天键)
          source          TEXT,                     -- multi/heat/moneyflow_dc
          candidate_limit INTEGER,
          mode            TEXT,                     -- market_scan/specified_pool
          ruleset_version TEXT,                     -- analysis.scoring_rules.RULESET_VERSION
          config_hash     TEXT,                     -- scoring_runtime_config.json 内容哈希
          report_file     TEXT,
          candidates      INTEGER,
          analyzed        INTEGER,
          duration_sec    REAL,
          extra_json      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_opp_run_date ON opportunity_run(run_date, run_at DESC);

        CREATE TABLE IF NOT EXISTS opportunity_item (
          run_id       INTEGER NOT NULL REFERENCES opportunity_run(id) ON DELETE CASCADE,
          code         TEXT NOT NULL,
          name         TEXT,
          item_rank    INTEGER,                     -- 该次 run 内按总分降序的名次
          total_score  REAL,
          rating       TEXT,
          degraded     INTEGER NOT NULL DEFAULT 0,  -- 数据缺失降级(quant 无历史数据等)
          source       TEXT,                        -- 候选来源 heat/oversold/moneyflow/...
          source_detail TEXT,                       -- 候选来源细节,如热门板块/成分股排名
          sector       TEXT,                        -- 中文板块/行业名称
          sector_code  TEXT,                        -- 东财板块代码(BK...)
          sector_rank  INTEGER,                     -- 热门板块排名
          sector_stock_rank INTEGER,                -- 板块内成分股排名
          change_pct   REAL,
          scores_json  TEXT,                        -- 七维分项
          signals_json TEXT,                        -- chase/rsi/涨幅/卖出信号等风险信号
          PRIMARY KEY (run_id, code)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS idx_opp_item_code ON opportunity_item(code, run_id);
        """,
    ),
    (
        10,
        # 热门板块全量快照:记录 Top 热门板块、板块下全部成分股排名/资金字段,
        # 以及与龙虎榜等外部事实表的关联关系,供桌面无限画布钻取与 Excel 导出。
        """
        CREATE TABLE IF NOT EXISTS hot_sector_snapshot (
          id             INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at     TEXT NOT NULL,
          trade_date     TEXT,
          source         TEXT,
          board_limit    INTEGER,
          board_count    INTEGER,
          stock_count    INTEGER,
          relation_count INTEGER,
          extra_json     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_hss_created ON hot_sector_snapshot(created_at DESC, id DESC);

        CREATE TABLE IF NOT EXISTS hot_sector_board (
          snapshot_id       INTEGER NOT NULL REFERENCES hot_sector_snapshot(id) ON DELETE CASCADE,
          board_code        TEXT NOT NULL,
          board_name        TEXT,
          board_type        TEXT,
          board_rank        INTEGER,
          change_pct        REAL,
          main_net_inflow   REAL,
          raw_json          TEXT,
          PRIMARY KEY (snapshot_id, board_code)
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS hot_sector_stock (
          snapshot_id            INTEGER NOT NULL REFERENCES hot_sector_snapshot(id) ON DELETE CASCADE,
          board_code             TEXT NOT NULL,
          code                   TEXT NOT NULL,
          name                   TEXT,
          stock_rank             INTEGER,
          candidate_rank         INTEGER,
          price                  REAL,
          change_pct             REAL,
          main_net_inflow        REAL,
          main_net_inflow_text   TEXT,
          lhb_trade_date         TEXT,
          lhb_buy_amount         REAL,
          lhb_sell_amount        REAL,
          lhb_net_amount         REAL,
          lhb_reason             TEXT,
          raw_json               TEXT,
          PRIMARY KEY (snapshot_id, board_code, code)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS idx_hss_stock_code ON hot_sector_stock(code, snapshot_id);
        CREATE INDEX IF NOT EXISTS idx_hss_stock_board ON hot_sector_stock(snapshot_id, board_code, stock_rank);

        CREATE TABLE IF NOT EXISTS hot_sector_relation (
          id             INTEGER PRIMARY KEY AUTOINCREMENT,
          snapshot_id    INTEGER NOT NULL REFERENCES hot_sector_snapshot(id) ON DELETE CASCADE,
          board_code     TEXT,
          code           TEXT NOT NULL,
          relation_type  TEXT NOT NULL,
          related_table  TEXT,
          related_key    TEXT,
          trade_date     TEXT,
          amount         REAL,
          detail_json    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_hsr_snapshot_code ON hot_sector_relation(snapshot_id, code);
        CREATE INDEX IF NOT EXISTS idx_hsr_type ON hot_sector_relation(relation_type, trade_date);
        """,
    ),
    (
        11,
        # 机会挖掘 run 明细补充板块维度,让历史页/画布不再只能从 Markdown 或
        # 最新热门板块快照反推中文板块和板块内排名。
        "",
    ),
    (
        12,
        # 财务三大表(资产负债表/利润表/现金流量表)按期缓存。财报低频更新,
        # 默认读缓存,缺失/强制刷新才联网(免费 akshare 优先,付费 Tushare 兜底)。
        # 每个 (code, statement_type, report_date) 一行,行项目以 JSON 存 items_json。
        """
        CREATE TABLE IF NOT EXISTS financial_statement (
          id             INTEGER PRIMARY KEY AUTOINCREMENT,
          code           TEXT NOT NULL,
          ts_code        TEXT,
          statement_type TEXT NOT NULL,           -- balance | income | cashflow
          report_date    TEXT NOT NULL,           -- 报告期 YYYYMMDD / YYYY-MM-DD
          period         TEXT,                     -- 报告期说明(年报/中报/季报)
          source         TEXT,                     -- akshare | tushare
          currency       TEXT,
          items_json     TEXT,                     -- {字段:数值} 行项目
          created_at     TEXT,
          UNIQUE(code, statement_type, report_date)
        );
        CREATE INDEX IF NOT EXISTS idx_fs_code_type ON financial_statement(code, statement_type, report_date DESC);
        """,
    ),
    (
        13,
        # 星轨图谱(物理AI/AI产业链 同心轨道图)。完全数据驱动、用户可增删:
        # ring=轨道环(层),board=环上的板块/概念(真实东财 BK 码),
        # stock=用户钉选到某板块的个股(可选;未钉选则运行时实时拉东财成分股)。
        # 删环级联删其下 board,删 board 级联删其下 pinned stock。
        """
        CREATE TABLE IF NOT EXISTS star_orbit_ring (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          name        TEXT NOT NULL,                 -- 轨道环名称(如「算力底座」)
          subtitle    TEXT,                          -- 副标题/说明
          color       TEXT,                          -- 主题色(前端渲染用,可空)
          sort_order  INTEGER NOT NULL DEFAULT 0,    -- 由内向外的环序
          created_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS star_orbit_board (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          ring_id     INTEGER NOT NULL REFERENCES star_orbit_ring(id) ON DELETE CASCADE,
          board_code  TEXT NOT NULL,                 -- 东财板块码(BKxxxx)或自定义码
          board_name  TEXT NOT NULL,
          board_type  TEXT,                          -- concept | industry | custom
          note        TEXT,
          sort_order  INTEGER NOT NULL DEFAULT 0,
          created_at  TEXT,
          UNIQUE(ring_id, board_code)
        );
        CREATE INDEX IF NOT EXISTS idx_orbit_board_ring ON star_orbit_board(ring_id, sort_order);
        CREATE TABLE IF NOT EXISTS star_orbit_stock (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          board_id    INTEGER NOT NULL REFERENCES star_orbit_board(id) ON DELETE CASCADE,
          stock_code  TEXT NOT NULL,
          stock_name  TEXT,
          note        TEXT,
          sort_order  INTEGER NOT NULL DEFAULT 0,
          created_at  TEXT,
          UNIQUE(board_id, stock_code)
        );
        CREATE INDEX IF NOT EXISTS idx_orbit_stock_board ON star_orbit_stock(board_id, sort_order);
        """,
    ),
    (
        14,
        # 星轨图谱板块成分股「关联关系」缓存。点击板块钻取时东财 push2 实时成分股
        # 接口高频会被代理/限流掐断,membership(板块→个股归属)本身又是低频不变的,
        # 故把成功取到的成分清单按板块快照入库:东财可达时刷新缓存并叠加实时价;
        # 限流时回退 Tushare(dc_member)兜底,再不行回退本表上次缓存的关联关系。
        # 只缓存归属(代码/名称/序),不缓存实时价/涨幅/主力净额(易过期,降级时记空)。
        """
        CREATE TABLE IF NOT EXISTS star_orbit_board_member (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          board_code  TEXT NOT NULL,                 -- 东财板块码(BKxxxx)
          stock_code  TEXT NOT NULL,                 -- 6 位个股代码
          stock_name  TEXT,
          sort_order  INTEGER NOT NULL DEFAULT 0,    -- 缓存当时的排序(东财按主力净流入)
          source      TEXT,                          -- eastmoney | tushare
          updated_at  TEXT,                          -- 该板块成分快照的刷新时间(ISO)
          UNIQUE(board_code, stock_code)
        );
        CREATE INDEX IF NOT EXISTS idx_orbit_member_board ON star_orbit_board_member(board_code, sort_order);
        """,
    ),
    (
        15,
        # 机会挖掘每次 run 的「前十条热点新闻」按天落盘,供风险·机遇大屏右栏按所选
        # 日期读取(撮合矩阵旁的东财热点面板)。结构来自 run_opportunity_discovery
        # 的 self.global_hot_news:{title, url, source, publish_time, heat(0-100)}。
        # news_rank 为落库时按 heat 降序的名次(1..N);删 run 级联清空热点行。
        """
        CREATE TABLE IF NOT EXISTS opportunity_hot_news (
          run_id       INTEGER NOT NULL REFERENCES opportunity_run(id) ON DELETE CASCADE,
          news_rank    INTEGER NOT NULL,
          title        TEXT NOT NULL,
          url          TEXT,
          source       TEXT,
          publish_time TEXT,
          heat         REAL,
          PRIMARY KEY (run_id, news_rank)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS idx_opp_hotnews_run ON opportunity_hot_news(run_id);
        """,
    ),
    (
        16,
        """
        CREATE TABLE IF NOT EXISTS stock_related_news (
          id           INTEGER PRIMARY KEY AUTOINCREMENT,
          code         TEXT NOT NULL,
          tier         TEXT NOT NULL,
          title        TEXT NOT NULL,
          url          TEXT,
          source       TEXT,
          published_at TEXT,
          relation_reason TEXT,
          sentiment    TEXT,
          content_hash TEXT NOT NULL,
          fetched_at   TEXT NOT NULL,
          UNIQUE(code, content_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_srn_code_fetched
          ON stock_related_news(code, fetched_at);
        """,
    ),
    (
        17,
        """
        -- 回测推荐记录(原 results/backtest/recommendations.csv 的 SQLite 化):
        -- 每日 Top10 推荐 + 次日开盘买入的 1/3/5/10 日前瞻收益回填。
        CREATE TABLE IF NOT EXISTS backtest_recommendation (
          report_date TEXT NOT NULL,
          code        TEXT NOT NULL,
          item_rank   INTEGER,
          name        TEXT,
          score       REAL,
          chase_risk  REAL,
          buy_signals  INTEGER,
          sell_signals INTEGER,
          rsi         REAL,
          day_change  REAL,
          change_3d   REAL,
          change_5d   REAL,
          sector_score REAL,
          quant_score REAL,
          tech_score  REAL,
          momentum_pattern TEXT,
          buy_price   REAL,
          return_1d   REAL,
          return_3d   REAL,
          return_5d   REAL,
          return_10d  REAL,
          updated_at  TEXT,
          PRIMARY KEY(report_date, code)
        );
        CREATE INDEX IF NOT EXISTS idx_btrec_date
          ON backtest_recommendation(report_date);
        """,
    ),
    (
        18,
        """
        -- 量化雷达按日榜单(quant_radar_service):每日活跃股五机制评分快照,
        -- 支持按天回看与按代码/名称/行业搜索;完整页面快照仍在 kv_cache(quant_radar)。
        CREATE TABLE IF NOT EXISTS quant_radar_stock_daily (
          trade_date      TEXT NOT NULL,           -- ISO YYYY-MM-DD
          code            TEXT NOT NULL,
          name            TEXT,
          industry        TEXT,
          activity        INTEGER,
          level           TEXT,
          level_rank      INTEGER,
          spoof           INTEGER,
          hft             INTEGER,
          orderbook       INTEGER,
          sentiment       INTEGER,
          bias            INTEGER,
          changes_total   INTEGER,
          changes_bull    INTEGER,
          changes_bear    INTEGER,
          quant_seat      INTEGER DEFAULT 0,
          direction       TEXT,                    -- 砸盘/拉抬/拉锯
          smash           INTEGER DEFAULT 0,       -- 疑似量化砸盘评分 0-100
          price           REAL,
          change_pct      REAL,
          volume_ratio    REAL,
          turnover        REAL,
          amplitude       REAL,
          main_net_inflow REAL,
          badges          TEXT,                    -- JSON array
          reasons         TEXT,                    -- JSON array
          updated_at      TEXT,
          PRIMARY KEY (trade_date, code)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS idx_qrsd_code
          ON quant_radar_stock_daily(code, trade_date);
        CREATE INDEX IF NOT EXISTS idx_qrsd_date_act
          ON quant_radar_stock_daily(trade_date, activity DESC);
        CREATE INDEX IF NOT EXISTS idx_qrsd_date_smash
          ON quant_radar_stock_daily(trade_date, smash DESC);
        """,
    ),
    (
        19,
        # 量化雷达按日表补方向维度列(direction/smash);已建 v18 的库走
        # _migrate_v19_quant_radar_direction 幂等补列,新库在 v18 建表时已含。
        "",
    ),
    (
        20,
        """
        -- 存量 opportunity_item.change_pct 回填(历史入库时上游漏透传 change_pct,
        -- 整列 NULL → 股票池「平均涨跌」恒为空)。同口径的当日涨跌幅一直写在
        -- signals_json.day_change 里,直接回填即可,新数据由 build_items 写入。
        UPDATE opportunity_item
           SET change_pct = json_extract(signals_json, '$.day_change')
         WHERE change_pct IS NULL
           AND signals_json IS NOT NULL
           AND json_valid(signals_json)
           AND json_extract(signals_json, '$.day_change') IS NOT NULL;

        -- 全市场日线(market_daily)按日期查询:入选后涨幅要按交易日取开盘/收盘。
        CREATE INDEX IF NOT EXISTS idx_market_daily_date
          ON market_daily(trade_date);

        -- 按股票取「最近一个交易日」的资金流(股票池/榜单):PK 是
        -- (trade_date, ts_code, top_n),按 ts_code 找最新日期原本要全表扫 110 万行。
        CREATE INDEX IF NOT EXISTS idx_mf_code_date
          ON moneyflow_dc(ts_code, trade_date);
        """,
    ),
    (
        21,
        """
        -- 板块×日序列(spec 2026-08-21):moneyflow_dc 全市场行按板块预聚合。
        -- 每次请求现场聚合要扫 115 万行,不可接受;这里按日增量落表。
        -- sector_type 区分行业/概念:个股属于 1 个行业 + N 个概念,两条线独立成序列。
        -- provisional=1 表示盘中未定稿数据,收盘守护线程重算后改写为 0。
        CREATE TABLE IF NOT EXISTS sector_daily_metrics (
          trade_date        TEXT NOT NULL,           -- ISO YYYY-MM-DD
          sector            TEXT NOT NULL,
          sector_type       TEXT NOT NULL,           -- 行业 | 概念
          member_count      INTEGER,                 -- 参与聚合的成分股数
          net_amount        REAL,                    -- 主力净流入合计(万元)
          net_rate_median   REAL,                    -- 净流入率中位数(%)
          pct_chg_mean      REAL,                    -- 等权涨跌幅(%)
          breadth           REAL,                    -- 上涨家数占比 0~1
          amount_median     REAL,                    -- 成交额中位数(万元)
          excess_vs_market  REAL,                    -- 相对全市场等权超额(百分点)
          seat_count        INTEGER,                 -- 量化异动席位数 + 龙虎榜上榜数
          provisional       INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (trade_date, sector, sector_type)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS idx_sdm_sector
          ON sector_daily_metrics(sector, sector_type, trade_date);

        -- 按日拐点信号:既供页面读取,也作为事后校验与历史回看的数据源。
        CREATE TABLE IF NOT EXISTS sector_turning_signal (
          trade_date    TEXT NOT NULL,
          sector        TEXT NOT NULL,
          sector_type   TEXT NOT NULL,
          rule          TEXT NOT NULL,               -- T1..T5
          state         TEXT NOT NULL,               -- fired | watch
          score         REAL,
          evidence_json TEXT,
          provisional   INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (trade_date, sector, sector_type, rule)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS idx_sts_date_state
          ON sector_turning_signal(trade_date, state);
        """,
    ),
    (22, """
        CREATE TABLE IF NOT EXISTS provider_bar (
          source TEXT NOT NULL, symbol TEXT NOT NULL, frequency TEXT NOT NULL, ts TEXT NOT NULL,
          open REAL, high REAL, low REAL, close REAL NOT NULL, volume REAL, amount REAL,
          provisional INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (source, symbol, frequency, ts)
        ) WITHOUT ROWID;
    """),
]


def _current_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
          version    INTEGER PRIMARY KEY,
          applied_at TEXT NOT NULL
        )
        """
    )
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not row or column in _table_columns(conn, table):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _migrate_v8_raw_json(conn: sqlite3.Connection) -> None:
    _add_column_if_missing(conn, "moneyflow_dc", "raw_json", "raw_json TEXT")
    _add_column_if_missing(conn, "dragon_tiger_list", "raw_json", "raw_json TEXT")


def _migrate_v11_opportunity_item_sector(conn: sqlite3.Connection) -> None:
    _add_column_if_missing(conn, "opportunity_item", "source_detail", "source_detail TEXT")
    _add_column_if_missing(conn, "opportunity_item", "sector", "sector TEXT")
    _add_column_if_missing(conn, "opportunity_item", "sector_code", "sector_code TEXT")


def _migrate_v19_quant_radar_direction(conn: sqlite3.Connection) -> None:
    """量化雷达按日表补方向维度列(dev/App 库已在 v18 建表,幂等补列+索引)。"""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='quant_radar_stock_daily'"
    ).fetchone()
    if not row:
        return  # v18 建表 DDL 已含全部列,无表说明 v18 尚未跑(不可能走到这)
    _add_column_if_missing(conn, "quant_radar_stock_daily", "direction", "direction TEXT")
    _add_column_if_missing(conn, "quant_radar_stock_daily", "smash", "smash INTEGER DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qrsd_date_smash "
                 "ON quant_radar_stock_daily(trade_date, smash DESC)")
    _add_column_if_missing(conn, "opportunity_item", "sector_rank", "sector_rank INTEGER")
    _add_column_if_missing(conn, "opportunity_item", "sector_stock_rank", "sector_stock_rank INTEGER")


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Returns the final version.

    Each migration script uses IF NOT EXISTS, so a partial failure followed
    by re-run lands in the same final state. We do not wrap in a transaction
    because `executescript` auto-commits any pending transaction in
    autocommit-mode connections.
    """
    current = _current_version(conn)
    for version, sql in _MIGRATIONS:
        if version <= current:
            continue
        if version == 8:
            _migrate_v8_raw_json(conn)
        elif version == 11:
            _migrate_v11_opportunity_item_sector(conn)
        elif version == 19:
            _migrate_v19_quant_radar_direction(conn)
        elif sql.strip():
            conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES(?, ?)",
            (version, _dt.datetime.now().isoformat(timespec="seconds")),
        )
        current = version
    return current
