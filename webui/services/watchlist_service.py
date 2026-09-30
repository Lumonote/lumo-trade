"""用户自选股：JSON 持久化（USER_ROOT/config/watchlist.json）+ 东方财富实时行情。

- 落库走原子写（临时文件 + replace），与 RuntimeConfigurationService 口径一致。
- 实时行情走东方财富 ulist.np 批量报价接口，可对任意一篮子代码取价。
- 纯本地单用户，无并发写竞争；列表读写均以磁盘为准，避免与桌面多页状态漂移。
"""

from __future__ import annotations

import datetime
import json
import os
import re
import tempfile
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from webui.services.http_client import request_json, request_text

_VALID_CODE = re.compile(r"^(?:6[0-9]{5}|[03][0-9]{5}|[84][0-9]{5}|92[0-9]{4})$")
_MAX_ITEMS = 200
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_QUOTE_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://quote.eastmoney.com/",
}
# 腾讯行情（东财 ulist.np 在部分网络/本机被断开时的回退源；返回 GBK 文本，无主力净流入字段）。
_TENCENT_HEADERS = {"User-Agent": _UA, "Referer": "https://gu.qq.com/"}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, "", "-", "—", "N/A"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_code(raw: Any) -> str:
    """'SH600000' / '600000.SH' / '600000' → '600000'；非法输入原样返回（交由校验拒绝）。"""
    text = str(raw or "").strip().upper().split(".")[0]
    digits = re.sub(r"\D", "", text)
    if not digits:
        return text
    return digits.zfill(6) if len(digits) <= 6 else digits


def _eastmoney_secid(code: str) -> str:
    """6 位代码 → 东方财富 secid（沪市前缀 1，深/北前缀 0）。"""
    if code.startswith("92"):  # 北交所 920xxx 新代码段：东财归市场 0，须先于 9→1 判断
        return f"0.{code}"
    prefix = "1" if code[:1] in ("5", "6", "9") else "0"
    return f"{prefix}.{code}"


def _tencent_symbol(code: str) -> str:
    """6 位代码 → 腾讯行情 symbol（沪 sh / 深 sz / 北 bj）。"""
    if code.startswith("92"):  # 北交所 920xxx 新代码段，须先于 9→sh 判断
        return f"bj{code}"
    head = code[:1]
    if head in ("5", "6", "9"):
        return f"sh{code}"
    if head in ("4", "8"):
        return f"bj{code}"
    return f"sz{code}"


class WatchlistService:
    def __init__(self, store_path: Path):
        self.store_path = Path(store_path)
        self._lock = threading.RLock()
        self._sector_cache: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def _read(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = data.get("items") if isinstance(data, dict) else data
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                code = normalize_code(it.get("code"))
                if not _VALID_CODE.match(code) or code in seen:
                    continue
                seen.add(code)
                out.append({
                    "code": code,
                    "name": str(it.get("name") or "").strip(),
                    "added_at": str(it.get("added_at") or _now()),
                    "pinned": bool(it.get("pinned")),
                })
        return out

    def _write(self, items: list[dict[str, Any]]) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.store_path.name}.", suffix=".tmp", dir=str(self.store_path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"items": items, "updated_at": _now()}, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            Path(tmp_name).replace(self.store_path)
        finally:
            Path(tmp_name).unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # 排序：置顶优先；置顶组内按加入时间升序，非置顶组按加入时间降序
    # （非置顶最新加入的紧跟置顶之后，不会被压到列表底）
    # 磁盘仅保存纯加入顺序，排序只在读取时进行；排序键附带原索引，
    # 使同一秒加入的多只股票也保持稳定（后加入在前）。
    # ------------------------------------------------------------------
    @staticmethod
    def _sort_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        indexed = list(enumerate(items))
        pinned = sorted(
            (it for _, it in indexed if it.get("pinned")),
            key=lambda it: it.get("added_at", ""),
        )
        unpinned = sorted(
            ((i, it) for i, it in indexed if not it.get("pinned")),
            key=lambda pair: (pair[1].get("added_at", ""), pair[0]),
            reverse=True,
        )
        return pinned + [it for _, it in unpinned]

    # ------------------------------------------------------------------
    # 增删查
    # ------------------------------------------------------------------
    def list_items(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._sort_items(self._read())

    def pin(self, code: Any, pinned: bool = True) -> tuple[dict[str, Any], int]:
        norm = normalize_code(code)
        with self._lock:
            items = self._read()
            for it in items:
                if it["code"] == norm:
                    it["pinned"] = bool(pinned)
                    self._write(items)
                    return {"items": self._sort_items(items), "pinned": bool(pinned), "code": norm}, 200
            return {"error": "代码不在自选列表中"}, 404

    def add(self, code: Any, name: Any = "") -> tuple[dict[str, Any], int]:
        norm = normalize_code(code)
        if not _VALID_CODE.match(norm):
            return {"error": "无效的股票代码"}, 400
        with self._lock:
            items = self._read()
            if any(it["code"] == norm for it in items):
                return {"items": self._sort_items(items), "added": False, "code": norm}, 200
            if len(items) >= _MAX_ITEMS:
                return {"error": f"自选数量已达上限 {_MAX_ITEMS}"}, 400
            # 磁盘保持纯加入顺序；展示排序在读取时统一做（置顶优先、新加入紧随置顶）
            items.append({"code": norm, "name": str(name or "").strip(), "added_at": _now()})
            self._write(items)
            return {"items": self._sort_items(items), "added": True, "code": norm}, 200

    def remove(self, code: Any) -> tuple[dict[str, Any], int]:
        norm = normalize_code(code)
        with self._lock:
            items = self._read()
            kept = [it for it in items if it["code"] != norm]
            if len(kept) == len(items):
                return {"items": items, "removed": False, "code": norm}, 200
            self._write(kept)
            return {"items": kept, "removed": True, "code": norm}, 200

    def is_member(self, code: Any) -> bool:
        norm = normalize_code(code)
        with self._lock:
            return any(it["code"] == norm for it in self._read())

    # ------------------------------------------------------------------
    # 行业 / 汇总
    # ------------------------------------------------------------------
    def _sector_info(self, code: str) -> dict[str, Any]:
        cached = self._sector_cache.get(code)
        if cached is not None:
            return cached
        info: dict[str, Any] = {"sector": "", "boards": []}
        try:
            from analysis.sector_api import get_stock_boards, get_stock_sector_info
            raw = get_stock_sector_info(code) or {}
            sector = str(raw.get("sector_name") or raw.get("industry") or "").strip()
            boards = [
                str(item).strip()
                for item in (get_stock_boards(code, limit=6) or [])
                if str(item).strip()
            ]
            info = {"sector": sector, "boards": boards}
        except Exception:  # noqa: BLE001 - 行业数据失败不阻断自选行情
            info = {"sector": "", "boards": []}
        self._sector_cache[code] = info
        return info

    def _summary(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        quoted = [it for it in items if _safe_float(it.get("change_pct")) is not None]
        changes = [_safe_float(it.get("change_pct"), 0.0) or 0.0 for it in quoted]
        up = [it for it in quoted if (_safe_float(it.get("change_pct"), 0.0) or 0.0) > 0]
        down = [it for it in quoted if (_safe_float(it.get("change_pct"), 0.0) or 0.0) < 0]
        flat = len(quoted) - len(up) - len(down)
        avg_change = round(sum(changes) / len(changes), 2) if changes else None
        sorted_changes = sorted(changes)
        if sorted_changes:
            mid = len(sorted_changes) // 2
            median_change = (
                round(sorted_changes[mid], 2)
                if len(sorted_changes) % 2
                else round((sorted_changes[mid - 1] + sorted_changes[mid]) / 2, 2)
            )
        else:
            median_change = None

        best = max(quoted, key=lambda it: _safe_float(it.get("change_pct"), -999.0) or -999.0, default=None)
        worst = min(quoted, key=lambda it: _safe_float(it.get("change_pct"), 999.0) or 999.0, default=None)
        inflows = [
            _safe_float(it.get("main_net_inflow"))
            for it in items
            if _safe_float(it.get("main_net_inflow")) is not None
        ]
        net_inflow = round(sum(inflows), 2) if inflows else None

        by_sector: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for it in items:
            sector = str(it.get("sector") or "未识别板块").strip() or "未识别板块"
            by_sector[sector].append(it)
        sectors = []
        for sector, rows in by_sector.items():
            sector_changes = [
                _safe_float(row.get("change_pct"))
                for row in rows
                if _safe_float(row.get("change_pct")) is not None
            ]
            sector_inflows = [
                _safe_float(row.get("main_net_inflow"))
                for row in rows
                if _safe_float(row.get("main_net_inflow")) is not None
            ]
            leader = max(
                rows,
                key=lambda row: _safe_float(row.get("change_pct"), -999.0) or -999.0,
                default=None,
            )
            sectors.append({
                "name": sector,
                "count": len(rows),
                "avg_change_pct": round(sum(sector_changes) / len(sector_changes), 2) if sector_changes else None,
                "main_net_inflow": round(sum(sector_inflows), 2) if sector_inflows else None,
                "leader": {
                    "code": leader.get("code"),
                    "name": leader.get("name"),
                    "change_pct": leader.get("change_pct"),
                } if leader else None,
                "stocks": [
                    {"code": row.get("code"), "name": row.get("name"), "change_pct": row.get("change_pct")}
                    for row in rows[:8]
                ],
            })
        sectors.sort(key=lambda row: (row["count"], row["avg_change_pct"] if row["avg_change_pct"] is not None else -999), reverse=True)

        total = len(items)
        quoted_count = len(quoted)
        up_ratio = round(len(up) / quoted_count, 4) if quoted_count else None
        top_sector = sectors[0] if sectors else None
        concentration = round((top_sector["count"] / total), 4) if top_sector and total else 0.0
        if avg_change is None:
            tone = "暂无行情"
        elif avg_change >= 1 and (up_ratio or 0) >= 0.6:
            tone = "偏强"
        elif avg_change <= -1 and (up_ratio or 0) <= 0.4:
            tone = "偏弱"
        else:
            tone = "震荡"

        # 最强/最弱已在收益卡正文行展示, highlights 芯片只放增量信息避免重复
        highlights = []
        if top_sector:
            highlights.append(f"最大板块 {top_sector['name']} {top_sector['count']} 只")
        if net_inflow is not None:
            highlights.append(f"主力净流入合计 {net_inflow:+.0f}")

        return {
            "sector_summary": {
                "total_sectors": len(sectors),
                "top_sectors": sectors[:8],
                "top_sector": top_sector,
                "concentration": concentration,
                "concentration_label": "集中" if concentration >= 0.45 and total >= 3 else "分散",
                "unknown_count": len(by_sector.get("未识别板块", [])),
            },
            "return_summary": {
                "total": total,
                "quoted_count": quoted_count,
                "avg_change_pct": avg_change,
                "median_change_pct": median_change,
                "up_count": len(up),
                "down_count": len(down),
                "flat_count": flat,
                "up_ratio": up_ratio,
                "best": best,
                "worst": worst,
                "main_net_inflow": net_inflow,
                "tone": tone,
                "highlights": highlights,
            },
        }

    # ------------------------------------------------------------------
    # 实时行情
    # ------------------------------------------------------------------
    def quotes(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        valid: list[str] = []
        seen: set[str] = set()
        for raw in codes or []:
            code = normalize_code(raw)
            if _VALID_CODE.match(code) and code not in seen:
                seen.add(code)
                valid.append(code)
        if not valid:
            return {}
        # 东财 ulist.np 字段最全（含主力净流入），但在部分网络/本机会被直接断开；
        # 取不到时回退腾讯行情（qt.gtimg.cn，无主力净流入字段），保证自选行情可用。
        try:
            out = self._quotes_eastmoney(valid)
        except Exception:  # noqa: BLE001 — 行情失败时降级，不应阻断自选列表
            out = {}
        if out:
            return out
        try:
            return self._quotes_tencent(valid)
        except Exception:  # noqa: BLE001
            return {}

    def _quotes_eastmoney(self, valid: list[str]) -> dict[str, dict[str, Any]]:
        secids = ",".join(_eastmoney_secid(c) for c in valid)
        url = (
            "https://push2.eastmoney.com/api/qt/ulist.np/get?"
            "fltt=2&invt=2&fields=f12,f14,f2,f3,f4,f62,f124&secids=" + secids
        )
        payload = request_json(url, headers=_QUOTE_HEADERS, timeout=4, retries=2)
        diff = ((payload or {}).get("data") or {}).get("diff") or []
        rows = diff.values() if isinstance(diff, dict) else diff
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = str(row.get("f12") or "").strip()
            if not code:
                continue
            quote_date = None
            try:
                from zoneinfo import ZoneInfo
                stamp = _safe_float(row.get("f124"))
                if stamp and stamp > 0:
                    quote_date = datetime.datetime.fromtimestamp(stamp, ZoneInfo("Asia/Shanghai")).date().isoformat()
            except (ValueError, OSError, OverflowError):
                pass
            out[code] = {
                "name": str(row.get("f14") or "").strip(),
                "price": _safe_float(row.get("f2")),
                "change_pct": round(_safe_float(row.get("f3")) or 0.0, 2),
                "change_amount": _safe_float(row.get("f4")),
                "main_net_inflow": _safe_float(row.get("f62")),
                "quote_date": quote_date,
            }
        return out

    def _quotes_tencent(self, valid: list[str]) -> dict[str, dict[str, Any]]:
        symbols = ",".join(_tencent_symbol(c) for c in valid)
        text = request_text(
            "https://qt.gtimg.cn/q=" + symbols,
            headers=_TENCENT_HEADERS,
            timeout=4,
            encoding="gbk",
            errors="ignore",
            retries=2,
        )
        out: dict[str, dict[str, Any]] = {}
        for line in text.splitlines():
            line = line.strip()
            if "=" not in line:
                continue
            body = line.split("=", 1)[1].strip().rstrip(";").strip('"')
            if not body:
                continue
            fields = body.split("~")
            if len(fields) < 33:  # 行情未返回（停牌/无效代码）时字段会被截断
                continue
            code = (fields[2] or "").strip()
            if not code:
                continue
            out[code] = {
                "name": (fields[1] or "").strip(),
                "price": _safe_float(fields[3]),
                "change_pct": round(_safe_float(fields[32]) or 0.0, 2),
                "change_amount": _safe_float(fields[31]),
                "main_net_inflow": None,  # 腾讯基础行情不含主力净流入
                "quote_date": (f"{fields[30][:4]}-{fields[30][4:6]}-{fields[30][6:8]}"
                               if len(fields[30]) >= 8 and fields[30][:8].isdigit() else None),
            }
        return out

    def list_with_quotes(self) -> dict[str, Any]:
        with self._lock:
            items = self._sort_items(self._read())
        quote_map = self.quotes([it["code"] for it in items]) if items else {}
        merged: list[dict[str, Any]] = []
        for it in items:
            quote = quote_map.get(it["code"], {})
            sector_info = self._sector_info(it["code"])
            merged.append({
                "code": it["code"],
                "name": it["name"] or quote.get("name") or it["code"],
                "added_at": it["added_at"],
                "pinned": bool(it.get("pinned")),
                "price": quote.get("price"),
                "change_pct": quote.get("change_pct"),
                "change_amount": quote.get("change_amount"),
                "main_net_inflow": quote.get("main_net_inflow"),
                "sector": sector_info.get("sector") or "",
                "boards": sector_info.get("boards") or [],
            })
        summary = self._summary(merged)
        return {
            "items": merged,
            "count": len(merged),
            "quoted": bool(quote_map),
            "summary": summary,
            "updated_at": _now(),
        }
