"""统一操作计划：复用行情/日K/机会/板块拐点/模拟账户，原子保存锁定快照。"""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from analysis.trading_plan import evaluate_plan, fresh, market_plan, number, sector_plan, stock_plan
from webui.services.watchlist_service import normalize_code, _VALID_CODE


class TradingPlanService:
    def __init__(self, *, store_path, watchlist, opportunities, market_pulse, holdings,
                 quotes, kline, metadata, now=None):
        self.store_path = Path(store_path)
        self._watchlist, self._opportunities = watchlist, opportunities
        self._pulse, self._holdings, self._quotes = market_pulse, holdings, quotes
        self._kline, self._metadata = kline, metadata
        self._now = now or (lambda: datetime.now(ZoneInfo("Asia/Shanghai")))
        self._lock = threading.RLock()

    @staticmethod
    def _code(raw):
        code = normalize_code(raw)
        if not _VALID_CODE.fullmatch(code):
            raise ValueError("无效的股票代码")
        return code

    @staticmethod
    def _safe(fn, default, degraded, key):
        try:
            return fn()
        except Exception:
            degraded[key] = True
            return default

    def _read(self):
        if not self.store_path.exists():
            return {}
        # 文件损坏必须暴露错误，不能把损坏文件当成空仓覆盖。
        data = json.loads(self.store_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("plans"), dict):
            raise ValueError("操作计划文件格式无效，请恢复备份")
        return data["plans"]

    def _write(self, plans):
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix=".trading_plans.", suffix=".tmp", dir=self.store_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"plans": plans}, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
            Path(path).replace(self.store_path)
        finally:
            Path(path).unlink(missing_ok=True)

    def overview(self, *, scope="watchlist", code=None, offset=0, limit=10):
        if scope not in ("watchlist", "opportunities", "holdings"):
            raise ValueError("不支持的计划范围")
        selected = self._code(code) if code is not None else None
        now = self._now()
        today = now.date()
        degraded = {}
        with self._lock:
            saved = self._read()
        report = self._safe(self._opportunities, {}, degraded, "opportunities") or {}
        items = {self._code(i.get("code") or i.get("stock_code")): dict(i) for i in report.get("items") or []}
        hold = self._safe(self._holdings, {}, degraded, "holdings") or {}
        positions = hold.get("positions") or []
        position_map = {normalize_code(p.get("ts_code")): p for p in positions}
        watch = self._safe(self._watchlist, [], degraded, "watchlist") or []
        pool = watch if scope == "watchlist" else list(items.values()) if scope == "opportunities" else [
            {**p, "code": normalize_code(p.get("ts_code"))} for p in positions]
        if selected:
            pool = [{"code": selected, "name": (items.get(selected) or {}).get("name") or ""}]
        total = len(pool)
        offset, limit = max(0, int(offset)), max(1, min(20, int(limit)))
        pool = pool[offset:offset + limit]
        codes = [self._code(i.get("code")) for i in pool]
        pulse = self._safe(self._pulse, {}, degraded, "market") or {}
        market = market_plan(pulse, hold.get("account"), positions, today=today)
        quotes = self._safe(lambda: self._quotes(list(dict.fromkeys(codes + list(position_map)))), {}, degraded, "quotes") or {}
        if any(not fresh((quotes.get(c) or {}).get("quote_date"), today)
               or not number((quotes.get(c) or {}).get("price")) for c in position_map):
            degraded["holdings_quotes"] = True
            market["entry_allowed"] = False
            market["new_budget_pct"] = 0
            market["account_available"] = False
            market["used_position_pct"] = None
            market["reduce_pct"] = 0
            market["data_note"] = "模拟持仓报价未齐，暂停额度计算；已有计划的止损与止盈仍可复核。"
        meta_map = {}
        for c in set(codes) | set(position_map):
            metadata = self._safe(lambda c=c: self._metadata(c), {}, degraded, f"metadata:{c}") or {}
            meta_map[c] = {**metadata, **{k: v for k, v in (items.get(c) or {}).items()
                                       if k in ("name", "sector") and v}}
        sector_used = {}
        equity = number((hold.get("account") or {}).get("total_equity")) or 0
        unclassified = False
        for c, p in position_map.items():
            names = {meta_map[c].get("sector")} | set(meta_map[c].get("boards") or [])
            names.discard(None)
            names.discard("")
            if not names:
                unclassified = True
            weight = (number(p.get("market_value")) or 0) / equity * 100 if equity > 0 else 0
            for name in names:
                sector_used[name] = sector_used.get(name, 0) + weight

        def build(row):
            c = self._code(row["code"])
            local_degraded = {}
            kl = self._safe(lambda: self._kline(c), {}, local_degraded, "kline") or {}
            meta = {**meta_map[c], "name": row.get("name") or meta_map[c].get("name") or (quotes.get(c) or {}).get("name") or c}
            opportunity = items.get(c)
            if not fresh(report.get("date"), today):
                opportunity = None
            candidate = stock_plan(c, meta, kl, opportunity, today=today)
            plan = saved.get(c) or candidate
            sector = sector_plan(meta, pulse, today=today)
            result = evaluate_plan(plan, candidate, market, sector, quotes.get(c), position_map.get(c), today=today)
            result["candidate"] = candidate
            result["chart"] = {"code": c, "name": meta["name"], "source": kl.get("source"),
                               "records": (kl.get("records") or []) if selected else (kl.get("records") or [])[-60:],
                               "operation": candidate.get("operation") or kl.get("operation") or {}}
            result["revision"] = (saved.get(c) or {}).get("revision", 0)
            result["degraded"] = local_degraded
            if unclassified:
                result["blockers"].append("部分模拟持仓板块未识别，无法核算板块集中度")
                if result["status"] in ("entry", "add"):
                    result["status"], result["status_label"] = "blocked", "组合待复核"
            return result

        # 每页最多20只、最多4条取K链路；不运行完整个股分析或LLM。
        with ThreadPoolExecutor(max_workers=4) as executor:
            plans = list(executor.map(build, pool))
        remaining = market["new_budget_pct"]
        for plan in plans:
            c = plan["code"]
            held_weight = (number((position_map.get(c) or {}).get("market_value")) or 0) / equity * 100 if equity > 0 else 0
            names = plan["sector_plan"]["boards"]
            sector_room = min((market["sector_cap_pct"] - sector_used.get(n, 0) for n in names), default=0)
            known_limits = market["level"] != "unknown" and plan.get("available") and market["account_available"]
            if known_limits and held_weight > plan["max_position_pct"] and plan["held"] and plan["status"] not in ("exit", "take_profit"):
                plan["status"], plan["status_label"] = "reduce", "单票超限·减仓复核"
            elif known_limits and sector_room < 0 and plan["held"] and plan["status"] not in ("exit", "take_profit"):
                plan["status"], plan["status_label"] = "reduce", "板块超限·减仓复核"
            if plan["status"] in ("entry", "add"):
                budget = math.floor(max(0, min(remaining, sector_room, plan["max_position_pct"] - held_weight)) * 100) / 100
                plan["new_position_pct"] = round(budget, 2)
                plan["trial_position_pct"] = round(budget * .4, 2) if plan["status"] == "entry" else 0
                plan["add_position_pct"] = round(budget, 2) if plan["status"] == "add" else 0
                remaining -= budget
                for n in names:
                    sector_used[n] = sector_used.get(n, 0) + budget
                if budget <= 0:
                    plan["blockers"].append("总仓位、单票或板块可用额度已用尽")
                    plan["status"], plan["status_label"] = "blocked", "仓位额度不足"
        return {"scope": scope, "items": plans, "total": total, "offset": offset, "limit": limit,
                "generated_at": now.isoformat(timespec="seconds"), "market": market, "degraded": degraded,
                "opportunity_as_of": report.get("date"), "sectors": pulse.get("sectors") or {},
                "allocation_note": "本页按列表顺序共享额度；跨页与跨范围不是独立预算，执行前须重新核算。",
                "disclaimer": "条件式研究计划，不构成收益承诺；锁定只保存快照，不产生委托。"}

    def lock_plan(self, body):
        code = self._code(body.get("code"))
        expected = body.get("expected_revision")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            return {"error": "缺少有效计划版本，请刷新后重试"}, 400
        payload = self.overview(code=code)
        candidate = payload["items"][0]["candidate"]
        if not candidate.get("available"):
            return {"error": candidate.get("reason") or "数据不足"}, 422
        if body.get("basis_key") != candidate.get("basis_key"):
            return {"error": "价位依据已变化，请刷新后复核再锁定"}, 409
        with self._lock:
            plans = self._read()
            previous = plans.get(code) or {}
            if previous.get("revision", 0) != expected:
                return {"error": "计划已在另一页面更新，请刷新后重试"}, 409
            # 已持仓时，不允许通过重新制定悄悄放宽止损。
            if payload["items"][0]["held"] and previous.get("levels"):
                candidate["levels"]["stop_loss"] = max(candidate["levels"]["stop_loss"], previous["levels"]["stop_loss"])
                if candidate["levels"]["stop_loss"] >= candidate["levels"]["entry_low"]:
                    return {"error": "持仓的原止损已高于新买入区，请先处理持仓"}, 422
                levels = candidate["levels"]
                risk = levels["entry_high"] - levels["stop_loss"]
                levels["risk_pct"] = round(risk / levels["entry_high"] * 100, 3)
                levels["risk_reward"] = round((levels["target1"] - levels["entry_high"]) / risk, 2)
            candidate.update({"locked": True, "locked_at": self._now().isoformat(timespec="seconds"),
                              "revision": expected + 1})
            plans[code] = candidate
            self._write(plans)
        return {"success": True, "code": code, "revision": expected + 1}, 200
