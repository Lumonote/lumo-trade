"""模拟盘台账服务(Phase 4)。

单账户(id=1,默认 100W)+ 即时成交 + 持仓(含费摊薄均价)+ 成交流水 +
胜率/收益统计。open/close/limit 委托落 pending,由 Phase 5 撮合引擎回放成交。

费用模型(§9,可配置 paper_settings):
  - 佣金:gross*commission_rate,最低 commission_min(买+卖)
  - 印花税:gross*stamp_tax(仅卖)
  - 过户费:gross*transfer_fee(买+卖)
买入总成本 = gross + 费 → 计入 avg_cost(摊薄);卖出净额 = gross - 费;
卖出 realized_pnl = 卖出净额 - avg_cost*qty。

所有写操作经 get_conn()(线程局部、自动提交);多表成交用显式
BEGIN IMMEDIATE / COMMIT / ROLLBACK 保证原子。
"""
from __future__ import annotations

import math
import re
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from data_store.connection import get_conn


DEFAULT_SETTINGS = {
    "commission_rate": 0.00025,   # 万 2.5
    "commission_min": 5.0,        # 最低 5 元
    "stamp_tax": 0.0005,          # 印花税 0.05%(仅卖)
    "transfer_fee": 0.00001,      # 过户费 万 0.1
    "initial_cash": 1_000_000.0,  # 起始资金
}


def _bare(code) -> str:
    match = re.search(r"\d{6}", str(code or ""))
    return match.group(0) if match else ""


def _load_daily(code):
    """默认日K加载器:ohlcv 表按 6 位裸代码 + '1d' 频率存储。

    返回 DataFrame(timestamps/open/high/low/close/volume/amount,升序)或 None。
    测试中由 ohlcv_loader 注入替身,避免触网。"""
    try:
        from data_store import ohlcv_repo
        df = ohlcv_repo.load_dataframe(_bare(code), "1d")
        return df if df is not None and len(df) > 0 else None
    except Exception:
        return None


def _bar_day(bar) -> str:
    ts = bar["timestamps"]
    return ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]


class PaperTradingService:
    def __init__(self, quote_provider=None, now_fn=None):
        # quote_provider(codes) -> {bare_code: {"price": float, ...}}
        self._quote_provider = quote_provider
        self._now_fn = now_fn or datetime.now

    # ----------------------------- 基础 -----------------------------

    def _now(self) -> datetime:
        return self._now_fn()

    def _now_iso(self) -> str:
        return self._now().strftime("%Y-%m-%d %H:%M:%S")

    def _trade_date(self) -> str:
        return self._now().strftime("%Y-%m-%d")

    @contextmanager
    def _txn(self):
        conn = get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _settings(self) -> dict:
        rows = get_conn().execute("SELECT key, value FROM paper_settings").fetchall()
        raw = {r["key"]: r["value"] for r in rows}
        out = {}
        for key, default in DEFAULT_SETTINGS.items():
            try:
                out[key] = float(raw[key]) if key in raw and raw[key] is not None else float(default)
            except (TypeError, ValueError):
                out[key] = float(default)
        return out

    def _quotes(self, codes) -> dict:
        if not self._quote_provider or not codes:
            return {}
        try:
            result = self._quote_provider([_bare(c) for c in codes]) or {}
        except Exception:
            return {}
        return result if isinstance(result, dict) else {}

    def _quote_price(self, code) -> Optional[float]:
        info = self._quotes([code]).get(_bare(code)) or {}
        price = info.get("price") if isinstance(info, dict) else None
        try:
            return float(price) if price not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _quote_name(self, code) -> str:
        info = self._quotes([code]).get(_bare(code)) or {}
        if not isinstance(info, dict):
            return ""
        return str(info.get("name") or "").strip()

    def _attach_quote_names(self, rows: list[dict]) -> list[dict]:
        missing = [r.get("ts_code") for r in rows if not str(r.get("name") or "").strip()]
        if not missing:
            return rows
        quotes = self._quotes(missing)
        for row in rows:
            if str(row.get("name") or "").strip():
                continue
            info = quotes.get(_bare(row.get("ts_code"))) or {}
            if isinstance(info, dict) and info.get("name"):
                row["name"] = str(info.get("name") or "").strip()
        return rows

    def _fees(self, gross: float, side: str, s: dict) -> float:
        commission = max(gross * s["commission_rate"], s["commission_min"])
        fee = commission + gross * s["transfer_fee"]
        if side == "sell":
            fee += gross * s["stamp_tax"]
        return fee

    # ----------------------------- 账户 -----------------------------

    def ensure_account(self) -> dict:
        row = get_conn().execute("SELECT * FROM paper_account WHERE id=1").fetchone()
        if row is None:
            initial = self._settings()["initial_cash"]
            now_iso = self._now_iso()
            get_conn().execute(
                "INSERT INTO paper_account(id, initial_cash, cash, created_at, updated_at) "
                "VALUES(1, ?, ?, ?, ?)",
                (initial, initial, now_iso, now_iso),
            )
            row = get_conn().execute("SELECT * FROM paper_account WHERE id=1").fetchone()
        return dict(row)

    def reset(self, initial_cash=None) -> dict:
        try:
            initial = float(initial_cash) if initial_cash not in (None, "", 0) else self._settings()["initial_cash"]
        except (TypeError, ValueError):
            initial = self._settings()["initial_cash"]
        now_iso = self._now_iso()
        with self._txn() as conn:
            for table in ("paper_trade", "paper_order", "paper_position", "paper_equity_curve", "paper_account"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute(
                "INSERT INTO paper_account(id, initial_cash, cash, created_at, updated_at) "
                "VALUES(1, ?, ?, ?, ?)",
                (initial, initial, now_iso, now_iso),
            )
        return self.account_summary()

    def account_summary(self) -> dict:
        acc = self.ensure_account()
        positions = self.positions()
        position_value = sum(p["market_value"] for p in positions)
        float_pnl = sum(p["float_pnl"] for p in positions)
        realized = self._sum_realized()
        initial = float(acc["initial_cash"])
        total_equity = float(acc["cash"]) + position_value
        total_return = (total_equity - initial) / initial if initial else 0.0
        return {
            "id": acc["id"],
            "initial_cash": initial,
            "cash": float(acc["cash"]),
            "position_value": position_value,
            "total_equity": total_equity,
            "realized_pnl": realized,
            "float_pnl": float_pnl,
            "total_return": total_return,
            "position_count": len(positions),
            "updated_at": acc.get("updated_at"),
        }

    def _sum_realized(self) -> float:
        row = get_conn().execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS s FROM paper_trade WHERE side='sell'"
        ).fetchone()
        return float(row["s"]) if row and row["s"] is not None else 0.0

    # ----------------------------- 读取 -----------------------------

    def positions(self) -> list:
        rows = get_conn().execute(
            "SELECT * FROM paper_position WHERE qty > 0 ORDER BY ts_code"
        ).fetchall()
        quotes = self._quotes([r["ts_code"] for r in rows])
        out = []
        for r in rows:
            avg_cost = float(r["avg_cost"])
            info = quotes.get(_bare(r["ts_code"])) or {}
            price = info.get("price") if isinstance(info, dict) else None
            try:
                last = float(price) if price not in (None, "") else avg_cost
            except (TypeError, ValueError):
                last = avg_cost
            qty = int(r["qty"])
            market_value = last * qty
            cost = avg_cost * qty
            name = str(r["name"] or "").strip() or str(info.get("name") or "").strip()
            out.append({
                "ts_code": r["ts_code"],
                "name": name,
                "qty": qty,
                "avg_cost": avg_cost,
                "last_price": last,
                "market_value": market_value,
                "float_pnl": market_value - cost,
                "float_pnl_rate": (last - avg_cost) / avg_cost if avg_cost else 0.0,
            })
        return out

    def orders(self, status=None) -> list:
        if status:
            rows = get_conn().execute(
                "SELECT * FROM paper_order WHERE status=? ORDER BY id DESC", (str(status),)
            ).fetchall()
        else:
            rows = get_conn().execute("SELECT * FROM paper_order ORDER BY id DESC").fetchall()
        return self._attach_quote_names([dict(r) for r in rows])

    def trades(self, limit=None) -> list:
        sql = "SELECT * FROM paper_trade ORDER BY id DESC"
        params = ()
        if limit:
            sql += " LIMIT ?"
            params = (int(limit),)
        return self._attach_quote_names([dict(r) for r in get_conn().execute(sql, params).fetchall()])

    def get_order(self, order_id) -> Optional[dict]:
        row = get_conn().execute(
            "SELECT * FROM paper_order WHERE id=?", (int(order_id),)
        ).fetchone()
        return dict(row) if row else None

    def stats(self, as_of=None) -> dict:
        rows = get_conn().execute(
            "SELECT realized_pnl FROM paper_trade WHERE side='sell' AND realized_pnl IS NOT NULL"
            + (" AND trade_date<=?" if as_of else ""), (as_of,) if as_of else (),
        ).fetchall()
        pnls = [float(r["realized_pnl"]) for r in rows]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        sell_count = len(pnls)
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        profit_factor = (sum(wins) / abs(sum(losses))) if losses else None
        return {
            "sell_count": sell_count,
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": (len(wins) / sell_count) if sell_count else 0.0,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": profit_factor,
            "max_win": max(pnls) if pnls else 0.0,
            "max_loss": min(pnls) if pnls else 0.0,
            "total_realized": sum(pnls),
            "max_drawdown": self._max_drawdown(as_of),
        }

    def _max_drawdown(self, as_of=None) -> float:
        rows = get_conn().execute(
            "SELECT total_equity FROM paper_equity_curve" + (" WHERE trade_date<=?" if as_of else "")
            + " ORDER BY trade_date", (as_of,) if as_of else (),
        ).fetchall()
        equity = [float(r["total_equity"]) for r in rows if r["total_equity"] is not None]
        if not equity:
            return 0.0
        peak = equity[0]
        mdd = 0.0
        for value in equity:
            peak = max(peak, value)
            if peak > 0:
                mdd = min(mdd, (value - peak) / peak)
        return mdd

    # ----------------------------- 下单 -----------------------------

    def place_order(self, ts_code, side, price_type, *, qty=None, amount=None,
                    limit_price=None, name="", note="") -> dict:
        self.ensure_account()
        code = _bare(ts_code) or str(ts_code or "").strip()
        name = str(name or "").strip() or self._quote_name(code)
        side = str(side or "").strip().lower()
        price_type = str(price_type or "").strip().lower()
        qty = int(qty) if qty not in (None, "", 0) else None
        amount = float(amount) if amount not in (None, "", 0) else None
        limit_price = float(limit_price) if limit_price not in (None, "", 0) else None
        now_iso, trade_date = self._now_iso(), self._trade_date()

        def reject(reason, *, pt=None):
            oid = self._insert_order(code, name, side or "buy", pt or price_type or "market",
                                     limit_price, qty, amount, "rejected", now_iso, trade_date, note=reason)
            return self.get_order(oid)

        if side not in ("buy", "sell"):
            return reject("方向无效", pt="market")
        if price_type not in ("market", "open", "close", "limit"):
            return reject("价格类型无效", pt="market")
        if price_type == "limit" and not limit_price:
            return reject("指定价(limit)需填写限价")
        if not qty and not amount:
            return reject("需填写股数或金额")

        if price_type == "market":
            price = self._quote_price(code)
            if not price or price <= 0:
                return reject("无实时报价,无法即时成交")
            return self._fill_market(code, name, side, price, qty, amount, now_iso, trade_date)

        # open / close / limit → pending,等 Phase 5 撮合回放
        oid = self._insert_order(code, name, side, price_type, limit_price, qty, amount,
                                 "pending", now_iso, trade_date)
        return self.get_order(oid)

    def import_historical_buy(self, ts_code, *, name="", price=None, qty=None, trade_date=None,
                              note="历史买入导入") -> dict:
        """Import an already-executed buy as a filled order/trade and live position.

        This is for users who started using the paper account after they already held a
        stock. It bypasses quote-based matching, but keeps the same fee and weighted-cost
        model as normal buys.
        """
        self.ensure_account()
        code = _bare(ts_code) or str(ts_code or "").strip()
        name = str(name or "").strip() or self._quote_name(code)
        now_iso = self._now_iso()
        fallback_date = self._trade_date()

        def reject(reason, *, order_date=None, normalized_qty=None, normalized_price=None):
            oid = self._insert_order(
                code, name, "buy", "market", None, normalized_qty, None,
                "rejected", now_iso, order_date or fallback_date, note=reason,
                filled_price=normalized_price, filled_qty=normalized_qty,
            )
            return self.get_order(oid)

        try:
            normalized_price = float(price)
        except (TypeError, ValueError):
            normalized_price = 0.0
        if normalized_price <= 0:
            return reject("历史买入需填写成交价")

        try:
            normalized_qty = (int(qty) // 100) * 100
        except (TypeError, ValueError):
            normalized_qty = 0
        if normalized_qty < 100:
            return reject("历史买入股数需至少一手(100股)", normalized_price=normalized_price)

        try:
            order_date = datetime.strptime(str(trade_date or fallback_date)[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            return reject("历史买入交易日期无效", normalized_qty=normalized_qty, normalized_price=normalized_price)

        traded_at = f"{order_date} 09:30:00"
        gross = normalized_price * normalized_qty
        fee = self._fees(gross, "buy", self._settings())
        total_cost = gross + fee
        if total_cost > float(self.ensure_account()["cash"]) + 1e-9:
            return reject(
                "资金不足",
                order_date=order_date,
                normalized_qty=normalized_qty,
                normalized_price=normalized_price,
            )

        with self._txn() as conn:
            order_id = self._insert_order(
                code, name, "buy", "market", None, normalized_qty, None,
                "filled", traded_at, order_date,
                filled_price=normalized_price, filled_qty=normalized_qty, fee=fee, note=note,
            )
            self._apply_buy(
                conn, code, name, normalized_qty, total_cost, normalized_price,
                gross, fee, order_id, traded_at, order_date,
            )
        result = self.get_order(order_id)
        result["realized_pnl"] = None
        return result

    def cancel_order(self, order_id) -> Optional[dict]:
        order = self.get_order(order_id)
        if not order:
            return None
        if order["status"] != "pending":
            return order
        get_conn().execute(
            "UPDATE paper_order SET status='cancelled', "
            "note=COALESCE(note, '') || '[用户撤单]' WHERE id=?",
            (int(order_id),),
        )
        return self.get_order(order_id)

    def _fill_market(self, code, name, side, price, qty, amount, now_iso, trade_date) -> dict:
        s = self._settings()
        by_amount = qty is None
        if by_amount:
            qty = int(amount // (price * 100)) * 100
        else:
            qty = (int(qty) // 100) * 100
        if qty < 100:
            reason = "金额不足一手(100股)" if by_amount else "股数不足一手(100股)"
            oid = self._insert_order(code, name, side, "market", None, qty or None, amount,
                                     "rejected", now_iso, trade_date, note=reason)
            return self.get_order(oid)

        gross = price * qty
        fee = self._fees(gross, side, s)

        if side == "buy":
            total_cost = gross + fee
            if total_cost > float(self.ensure_account()["cash"]) + 1e-9:
                oid = self._insert_order(code, name, side, "market", None, qty, amount,
                                         "rejected", now_iso, trade_date, note="资金不足")
                return self.get_order(oid)
            with self._txn() as conn:
                order_id = self._insert_order(code, name, "buy", "market", None, qty, amount,
                                              "filled", now_iso, trade_date,
                                              filled_price=price, filled_qty=qty, fee=fee)
                self._apply_buy(conn, code, name, qty, total_cost, price, gross, fee, order_id, now_iso, trade_date)
            result = self.get_order(order_id)
            result["realized_pnl"] = None
            return result

        # sell
        pos = get_conn().execute(
            "SELECT qty, avg_cost FROM paper_position WHERE ts_code=?", (code,)
        ).fetchone()
        if not pos or int(pos["qty"]) < qty:
            oid = self._insert_order(code, name, side, "market", None, qty, amount,
                                     "rejected", now_iso, trade_date, note="持仓不足")
            return self.get_order(oid)
        avg_cost = float(pos["avg_cost"])
        net = gross - fee
        realized = net - avg_cost * qty
        remaining = int(pos["qty"]) - qty
        with self._txn() as conn:
            order_id = self._insert_order(code, name, "sell", "market", None, qty, amount,
                                          "filled", now_iso, trade_date,
                                          filled_price=price, filled_qty=qty, fee=fee)
            self._apply_sell(conn, code, name, qty, net, price, gross, fee, realized,
                             remaining, order_id, now_iso, trade_date)
        result = self.get_order(order_id)
        result["realized_pnl"] = realized
        return result

    # ----------------------------- 内部写 -----------------------------

    def _insert_order(self, code, name, side, price_type, limit_price, qty, amount, status,
                      now_iso, trade_date, *, filled_price=None, filled_qty=None, fee=None, note=None) -> int:
        cur = get_conn().execute(
            """
            INSERT INTO paper_order(
                ts_code, name, side, price_type, limit_price, qty, amount_budget,
                status, created_at, created_date, filled_at, filled_price, filled_qty, fee, note)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (code, name, side, price_type, limit_price, qty, amount, status, now_iso, trade_date,
             (now_iso if status == "filled" else None), filled_price, filled_qty, fee, note),
        )
        return cur.lastrowid

    def _apply_buy(self, conn, code, name, qty, total_cost, price, gross, fee, order_id, now_iso, trade_date) -> None:
        conn.execute(
            """
            INSERT INTO paper_trade(order_id, ts_code, name, side, price, qty, gross, fee, realized_pnl, traded_at, trade_date)
            VALUES(?, ?, ?, 'buy', ?, ?, ?, ?, NULL, ?, ?)
            """,
            (order_id, code, name, price, qty, gross, fee, now_iso, trade_date),
        )
        pos = conn.execute("SELECT qty, avg_cost, name FROM paper_position WHERE ts_code=?", (code,)).fetchone()
        if pos:
            new_qty = int(pos["qty"]) + qty
            new_avg = (float(pos["avg_cost"]) * int(pos["qty"]) + total_cost) / new_qty
            next_name = name or str(pos["name"] or "").strip()
            conn.execute(
                "UPDATE paper_position SET qty=?, avg_cost=?, name=?, updated_at=? WHERE ts_code=?",
                (new_qty, new_avg, next_name, now_iso, code),
            )
        else:
            conn.execute(
                "INSERT INTO paper_position(ts_code, name, qty, avg_cost, opened_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
                (code, name, qty, total_cost / qty, now_iso, now_iso),
            )
        conn.execute("UPDATE paper_account SET cash=cash-?, updated_at=? WHERE id=1", (total_cost, now_iso))

    def _apply_sell(self, conn, code, name, qty, net, price, gross, fee, realized, remaining, order_id, now_iso, trade_date) -> None:
        conn.execute(
            """
            INSERT INTO paper_trade(order_id, ts_code, name, side, price, qty, gross, fee, realized_pnl, traded_at, trade_date)
            VALUES(?, ?, ?, 'sell', ?, ?, ?, ?, ?, ?, ?)
            """,
            (order_id, code, name, price, qty, gross, fee, realized, now_iso, trade_date),
        )
        if remaining <= 0:
            conn.execute("DELETE FROM paper_position WHERE ts_code=?", (code,))
        else:
            conn.execute("UPDATE paper_position SET qty=?, updated_at=? WHERE ts_code=?", (remaining, now_iso, code))
        conn.execute("UPDATE paper_account SET cash=cash+?, updated_at=? WHERE id=1", (net, now_iso))

    # ----------------------------- 撮合引擎(Phase 5) -----------------------------

    def settle(self, as_of=None, *, ohlcv_loader=None) -> dict:
        """惰性补算所有 pending 委托。

        对每张 pending 单回放其 created_date 之后、截至 as_of(含)已有日K的交易日:
        首个满足成交条件的交易日成交,成交归属该补算日(trade_date)。
        撮合价规则见模块/测试文档。无日K的票保持 pending。"""
        self.ensure_account()
        as_of = str(as_of) if as_of else self._trade_date()
        loader = ohlcv_loader or _load_daily
        pending = get_conn().execute(
            "SELECT * FROM paper_order WHERE status='pending' ORDER BY id"
        ).fetchall()
        filled, rejected, still = [], [], 0
        for row in pending:
            outcome = self._settle_one(dict(row), as_of, loader)
            if outcome == "filled":
                filled.append(row["id"])
            elif outcome == "rejected":
                rejected.append(row["id"])
            else:
                still += 1
        return {
            "as_of": as_of,
            "filled_count": len(filled),
            "rejected_count": len(rejected),
            "still_pending": still,
            "filled_ids": filled,
            "rejected_ids": rejected,
        }

    def _settle_one(self, order, as_of, loader) -> str:
        try:
            df = loader(order["ts_code"])
        except Exception:
            df = None
        if df is None or len(df) == 0:
            return "pending"
        created = order["created_date"] or ""
        days = []
        for _, bar in df.iterrows():
            day = _bar_day(bar)
            if created and day <= created:   # created_date 之后(严格)才回放
                continue
            if day > as_of:
                continue
            days.append((day, bar))
        days.sort(key=lambda x: x[0])
        for day, bar in days:
            price = self._match_price(order, bar)
            if price is not None:
                return self._fill_pending(order, float(price), day)
        return "pending"

    def _match_price(self, order, bar) -> Optional[float]:
        pt = order["price_type"]
        side = order["side"]
        try:
            o, h, lo, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        except (TypeError, ValueError, KeyError):
            return None
        if pt == "open":
            return o
        if pt == "close":
            return c
        if pt == "limit":
            lim = order["limit_price"]
            if lim in (None, ""):
                return None
            lim = float(lim)
            if side == "buy":
                return min(o, lim) if lo <= lim else None   # 触价买:低点触及才成,价取 min(open, limit)
            return max(o, lim) if h >= lim else None         # 触价卖:高点触及才成,价取 max(open, limit)
        return None

    def _fill_pending(self, order, price, day) -> str:
        """成交一张 pending 单:UPDATE 该单为 filled 并落账(复用 _apply_buy/_apply_sell)。"""
        order_id = order["id"]
        code = order["ts_code"]
        name = order["name"] or ""
        side = order["side"]
        s = self._settings()
        qty = order["qty"]
        if qty in (None, "", 0) and order["amount_budget"]:
            qty = int(float(order["amount_budget"]) // (price * 100)) * 100
        qty = (int(qty) // 100) * 100 if qty else 0
        traded_at = f"{day} 09:30:00" if order["price_type"] == "open" else f"{day} 15:00:00"
        if qty < 100:
            self._reject_pending(order_id, "数量不足一手(100股)")
            return "rejected"
        gross = price * qty
        fee = self._fees(gross, side, s)

        if side == "buy":
            total_cost = gross + fee
            if total_cost > float(self.ensure_account()["cash"]) + 1e-9:
                self._reject_pending(order_id, "资金不足")
                return "rejected"
            with self._txn() as conn:
                conn.execute(
                    "UPDATE paper_order SET status='filled', filled_at=?, filled_price=?, filled_qty=?, fee=? WHERE id=?",
                    (traded_at, price, qty, fee, order_id),
                )
                self._apply_buy(conn, code, name, qty, total_cost, price, gross, fee, order_id, traded_at, day)
            return "filled"

        # sell
        pos = get_conn().execute(
            "SELECT qty, avg_cost FROM paper_position WHERE ts_code=?", (code,)
        ).fetchone()
        if not pos or int(pos["qty"]) < qty:
            self._reject_pending(order_id, "持仓不足")
            return "rejected"
        avg_cost = float(pos["avg_cost"])
        net = gross - fee
        realized = net - avg_cost * qty
        remaining = int(pos["qty"]) - qty
        with self._txn() as conn:
            conn.execute(
                "UPDATE paper_order SET status='filled', filled_at=?, filled_price=?, filled_qty=?, fee=? WHERE id=?",
                (traded_at, price, qty, fee, order_id),
            )
            self._apply_sell(conn, code, name, qty, net, price, gross, fee, realized,
                             remaining, order_id, traded_at, day)
        return "filled"

    def _reject_pending(self, order_id, reason) -> None:
        get_conn().execute(
            "UPDATE paper_order SET status='rejected', note=COALESCE(note, '') || ? WHERE id=?",
            (f"[{reason}]", int(order_id)),
        )

    # ----------------------------- 盯市 / 权益曲线 -----------------------------

    def equity_curve(self) -> list:
        rows = get_conn().execute(
            "SELECT * FROM paper_equity_curve ORDER BY trade_date"
        ).fetchall()
        return [dict(r) for r in rows]

    def _valuation_date(self, date):
        today = self._trade_date()
        day = str(date or today)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or datetime.strptime(day, "%Y-%m-%d").date().isoformat() != day or day > today:
            raise ValueError("复盘日期必须有效且不能晚于今天")
        return day

    def _ledger_at(self, day):
        cash = initial = float(self.ensure_account()["initial_cash"])
        positions = {}
        for t in get_conn().execute(
            "SELECT ts_code, name, side, qty, gross, fee FROM paper_trade WHERE trade_date<=? "
            "ORDER BY trade_date, traded_at, id", (day,),
        ):
            code, qty, gross, fee = t['ts_code'], t['qty'], t['gross'], t['fee']
            if t['side'] == 'buy':
                p = positions.setdefault(code, {'ts_code': code, 'qty': 0, 'avg_cost': 0, 'name': t['name']})
                p['avg_cost'] = (p['avg_cost'] * p['qty'] + gross + fee) / (p['qty'] + qty)
                p['qty'] += qty
                p['name'] = t['name'] or p['name']
                cash -= gross + fee
            else:
                if code not in positions or positions[code]['qty'] < qty:
                    raise ValueError(f"{day} 前的成交流水持仓不足：{code}，未写入复盘")
                positions[code]['qty'] -= qty
                if not positions[code]['qty']:
                    del positions[code]
                cash += gross - fee
        return {'cash': cash, 'initial_cash': initial, 'positions': [positions[k] for k in sorted(positions)]}

    def marked_positions(self, date=None, *, close_provider=None, ohlcv_loader=None, ledger=None) -> list:
        """按当日收盘价为持仓定价(与 mark_to_market / 复盘持仓表同源,避免与实时报价打架)。"""
        date = self._valuation_date(date)
        rows = (ledger or self._ledger_at(date))['positions']
        out = []
        for r in rows:
            avg_cost = float(r["avg_cost"])
            close = self._eod_close(r["ts_code"], date, close_provider, ohlcv_loader)
            if close is None:
                raise ValueError(f"{r['ts_code']} 缺少截至 {date} 的有效收盘价，未写入权益或复盘")
            qty = int(r["qty"])
            market_value = close * qty
            out.append({
                "ts_code": r["ts_code"],
                "name": r["name"],
                "qty": qty,
                "avg_cost": avg_cost,
                "close": close,
                "market_value": market_value,
                "float_pnl": market_value - avg_cost * qty,
                "float_pnl_rate": (close - avg_cost) / avg_cost if avg_cost else 0.0,
            })
        return out

    def mark_to_market(self, date=None, *, close_provider=None, ohlcv_loader=None, marked=None, ledger=None) -> dict:
        """按当日收盘价为持仓盯市,写入/更新 paper_equity_curve(daily_pnl = 较前一日权益变动)。

        可传入预先算好的 marked(marked_positions 结果)避免重复取价。"""
        date = self._valuation_date(date)
        acc = ledger or self._ledger_at(date)
        if marked is None:
            marked = self.marked_positions(date, close_provider=close_provider, ohlcv_loader=ohlcv_loader, ledger=acc)
        position_value = sum(float(p["market_value"]) for p in marked)
        cash = float(acc["cash"])
        total_equity = cash + position_value
        with self._txn() as conn:
            prev = conn.execute("SELECT total_equity FROM paper_equity_curve WHERE trade_date < ? "
                                "ORDER BY trade_date DESC LIMIT 1", (date,)).fetchone()
            daily_pnl = total_equity - (prev['total_equity'] if prev else acc['initial_cash'])
            conn.execute(
                "INSERT INTO paper_equity_curve(trade_date, cash, position_value, total_equity, daily_pnl) "
                "VALUES(?, ?, ?, ?, ?) ON CONFLICT(trade_date) DO UPDATE SET cash=excluded.cash, "
                "position_value=excluded.position_value, total_equity=excluded.total_equity, daily_pnl=excluded.daily_pnl",
                (date, cash, position_value, total_equity, daily_pnl),
            )
            conn.execute("UPDATE paper_equity_curve SET daily_pnl=total_equity-? WHERE trade_date="
                         "(SELECT MIN(trade_date) FROM paper_equity_curve WHERE trade_date>?)", (total_equity, date))
        return {"trade_date": date, "cash": cash, "position_value": position_value,
                "total_equity": total_equity, "daily_pnl": daily_pnl}

    def _eod_close(self, code, date, close_provider, ohlcv_loader) -> Optional[float]:
        def positive(value):
            try:
                value = float(value)
                return value if math.isfinite(value) and value > 0 else None
            except (TypeError, ValueError):
                return None
        current = date == self._trade_date()
        if close_provider is not None and current:
            v = positive(close_provider(code))
            if v is not None:
                return v
        loader = ohlcv_loader or _load_daily
        try:
            df = loader(code)
        except Exception:
            df = None
        if df is not None and len(df) > 0:
            try:
                sub = df.copy()
                sub["_d"] = sub["timestamps"].apply(
                    lambda ts: ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
                )
                sub = sub[(sub["_d"] <= date) & sub["_d"].str.fullmatch(r"\d{4}-\d{2}-\d{2}")].sort_values("_d")
                sub = sub[sub['close'].map(positive).notna()]
                if len(sub) > 0:
                    return float(sub.iloc[-1]["close"])
            except Exception:
                pass
        return positive(self._quote_price(code)) if current else None

    # ----------------------------- EOD 当日复盘 -----------------------------

    def run_eod(self, date=None, *, ohlcv_loader=None, close_provider=None,
                market_env_text=None, results_dir=None, write_review=True) -> dict:
        """收盘 EOD:撮合补算 → 盯市 → 当日有持仓/成交则生成复盘 markdown(幂等,已存在不重写)。"""
        date = self._valuation_date(date)
        historical = date < self._trade_date()
        settle_summary = ({'filled_count': 0, 'rejected_count': 0} if historical else
                          self.settle(as_of=date, ohlcv_loader=ohlcv_loader))
        ledger = self._ledger_at(date)
        marked = self.marked_positions(date, close_provider=close_provider, ohlcv_loader=ohlcv_loader, ledger=ledger)
        mtm = self.mark_to_market(date, marked=marked, ledger=ledger)
        day_trades = get_conn().execute(
            "SELECT * FROM paper_trade WHERE trade_date=? ORDER BY id", (date,)
        ).fetchall()
        activity = bool(day_trades or marked)
        generated, review_path = False, None
        if write_review and activity and results_dir:
            from pathlib import Path
            path = Path(results_dir) / f"paper_review_{date}.md"
            if path.exists():
                review_path = str(path)   # 幂等:已生成则跳过
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    self._render_review(date, [dict(t) for t in day_trades], mtm, market_env_text, marked),
                    encoding="utf-8",
                )
                review_path, generated = str(path), True
        return {
            "date": date,
            "historical": historical,
            "settled": settle_summary["filled_count"],
            "rejected": settle_summary["rejected_count"],
            "equity": mtm,
            "activity": activity,
            "generated": generated,
            "review_path": review_path,
        }

    def _render_review(self, date, day_trades, mtm, market_env_text, positions=None) -> str:
        stats = self.stats(as_of=date)
        if positions is None:
            positions = self.marked_positions(date)
        initial = float(self.ensure_account()['initial_cash'])
        total_return = (mtm["total_equity"] - initial) / initial if initial else 0.0
        lines = [f"# 📓 模拟盘当日复盘 {date}", ""]
        if market_env_text:
            lines += [f"> **市场环境**:{market_env_text}", ""]
        lines += [
            "## 💰 账户概览", "",
            "| 指标 | 数值 |", "| --- | --- |",
            f"| 总资产 | {mtm['total_equity']:,.2f} |",
            f"| 可用资金 | {mtm['cash']:,.2f} |",
            f"| 持仓市值 | {mtm['position_value']:,.2f} |",
            f"| 当日盈亏 | {mtm['daily_pnl']:+,.2f} |",
            f"| 累计收益率 | {total_return * 100:+.2f}% |",
            f"| 已实现盈亏 | {stats['total_realized']:+,.2f} |",
            "",
            f"## 🧾 当日成交({len(day_trades)} 笔)", "",
        ]
        if day_trades:
            lines += ["| 时间 | 方向 | 代码 | 名称 | 成交价 | 股数 | 费用 | 已实现盈亏 |",
                      "| --- | --- | --- | --- | --- | --- | --- | --- |"]
            for t in day_trades:
                side_cn = "买入" if t.get("side") == "buy" else "卖出"
                realized = t.get("realized_pnl")
                realized_txt = f"{float(realized):+,.2f}" if realized is not None else "—"
                lines.append(
                    f"| {t.get('traded_at', '')} | {side_cn} | {t.get('ts_code', '')} | {t.get('name') or '—'} "
                    f"| {float(t.get('price', 0)):.3f} | {int(t.get('qty', 0))} | {float(t.get('fee', 0)):.2f} | {realized_txt} |"
                )
        else:
            lines.append("当日无成交。")
        lines += ["", f"## 📊 持仓盯市({len(positions)} 只)", ""]
        if positions:
            lines += ["| 代码 | 名称 | 股数 | 成本 | 收盘价 | 浮动盈亏 | 浮动收益率 |",
                      "| --- | --- | --- | --- | --- | --- | --- |"]
            for p in positions:
                close = float(p.get("close", p.get("last_price", p.get("avg_cost", 0))))
                lines.append(
                    f"| {p['ts_code']} | {p['name'] or '—'} | {p['qty']} | {p['avg_cost']:.3f} "
                    f"| {close:.3f} | {p['float_pnl']:+,.2f} | {p['float_pnl_rate'] * 100:+.2f}% |"
                )
        else:
            lines.append("当前无持仓。")
        pf = stats.get("profit_factor")
        lines += [
            "", "## 📈 累计统计", "",
            "| 指标 | 数值 |", "| --- | --- |",
            f"| 总卖出笔数 | {stats['sell_count']} |",
            f"| 胜率 | {stats['win_rate'] * 100:.1f}% |",
            (f"| 盈亏比 | {pf:.2f} |" if pf is not None else "| 盈亏比 | — |"),
            f"| 最大回撤 | {stats['max_drawdown'] * 100:.2f}% |",
            "", "---", "",
            "> ⚠️ 本复盘基于模拟盘台账,不构成任何投资建议。",
        ]
        return "\n".join(lines) + "\n"

    # ----------------------------- 复盘读取(供 API) -----------------------------

    def list_reviews(self, results_dir, limit=None) -> list:
        from pathlib import Path
        base = Path(results_dir)
        if not base.exists():
            return []
        items = []
        for p in sorted(base.glob("paper_review_*.md"), reverse=True):
            items.append({"date": p.stem.replace("paper_review_", ""), "file": p.name})
        if limit:
            items = items[: int(limit)]
        return items

    def read_review(self, date, results_dir) -> Optional[str]:
        from pathlib import Path
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date or "")):   # 防路径穿越
            return None
        path = Path(results_dir) / f"paper_review_{date}.md"
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return None
