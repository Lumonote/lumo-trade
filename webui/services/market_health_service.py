"""大盘体检快照：首次可离线使用，后台采集，失败保留原数据和时间。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import datetime as dt
import json
from pathlib import Path
import random
import re
import threading
from zoneinfo import ZoneInfo

from analysis.market_health import INDEXES, breadth_history, build_health, market_snapshot, normalize_bars, number
from analysis.market_health_fusion import build_fusion
from data_store import kv_repo
from webui.services.http_client import request_json, request_text

NAMESPACE = "market_health"
BASELINE = Path(__file__).resolve().parents[2] / "config/market_health_baseline.json"
_start_lock = threading.Lock()
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}


def now():
    return dt.datetime.now(ZoneInfo("Asia/Shanghai"))


def read_baseline():
    try:
        return json.loads(BASELINE.read_text())
    except (OSError, ValueError):
        return {"bars": {}, "notes": ["尚无快照，请更新公开数据。"], "source": "empty"}


def read_state():
    cached = kv_repo.get(NAMESPACE, "snapshot")
    return cached[0] if cached else read_baseline()


def latest_job(jobs):
    cached = kv_repo.get(NAMESPACE, "latest_job")
    return jobs.get(cached[0]) if cached else None


def query(jobs=None):
    from webui.services import market_health_cross_service
    state = read_state()
    result = build_health(state)
    result["fusion"] = build_fusion(result, market_health_cross_service.read_cached(result["as_of"]), state.get("bars",{}).get("sh000001",[]))
    result["generated_at"] = now().isoformat()
    result["age_days"] = (now().date()-dt.date.fromisoformat(result["as_of"])).days if result["as_of"] else None
    result["stale"] = result["age_days"] is not None and result["age_days"] > 4
    result["job"] = latest_job(jobs) if jobs else None
    result["report_url"] = "/static/research/ashare_bottom_20261022.html"
    return result


def _fetch_json(url):
    return request_json(url, headers=HEADERS, timeout=8, retries=1, trust_env=False)


def fetch_market(expected_count=None):
    """分页完整成功才接受；新浪原始快照保留PB/PE，不用云图的缺字段替代。"""
    rows = []
    for start in range(1, 81, 8):
        def page(n):
            data = _fetch_json("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                               f"Market_Center.getHQNodeData?page={n}&num=100&sort=symbol&asc=1&node=hs_a&symbol=&_s_r_a=page")
            if data is not None and not isinstance(data, list):
                raise ValueError("全A行情分页格式异常")
            return data or []
        with ThreadPoolExecutor(max_workers=6) as pool:
            batch = list(pool.map(page, range(start, start+8)))
        for offset, items in enumerate(batch):
            rows.extend(items)
            if len(items) < 100:
                if any(batch[offset+1:]):
                    raise ValueError("全A行情分页中间缺失，保留已有全市场快照")
                unique = {r.get("symbol"): r for r in rows if r.get("symbol")}
                minimum = max(3000, int((expected_count or 0)*.9))
                if len(unique) < minimum:
                    raise ValueError(f"行情覆盖不足{minimum}只，保留已有全市场快照")
                return list(unique.values())
    raise ValueError("全A行情分页超过上限，保留已有快照")


def quote_date():
    """使用完整指数行情里的交易日期，不以电脑日期假装行情日期。"""
    raw = request_text("https://hq.sinajs.cn/list=sh000001", headers=HEADERS, encoding="gbk", timeout=8, trust_env=False)
    match = re.search(r'hq_str_sh000001="([^"]+)"', raw)
    parts = match.group(1).split(",") if match else []
    date = parts[30] if len(parts) > 30 else ""
    try:
        return dt.date.fromisoformat(date).isoformat()
    except ValueError:
        raise ValueError("行情源未给出可验证的交易日期，保留已有快照") from None


def pick_sample(rows):
    eligible = sorted((r for r in rows if str(r.get("symbol", "")).startswith(("sh", "sz"))
                       and number(r.get("mktcap")) is not None and number(r.get("mktcap")) > 0
                       and number(r.get("trade")) is not None and number(r.get("trade")) > 0),
                      key=lambda r: (float(r["mktcap"]), str(r["symbol"])))
    rng = random.Random(42)
    chosen = []
    for bucket in range(8):
        group = eligible[len(eligible)*bucket//8:len(eligible)*(bucket+1)//8]
        chosen.extend(rng.sample(group, min(50, len(group))))
    return chosen


def fetch_breadth(rows, as_of, progress, sample_symbols=None, calendar=None):
    by_symbol = {r.get("symbol"): r for r in rows}
    targets = ([by_symbol[s] for s in sample_symbols if s in by_symbol] if sample_symbols else pick_sample(rows))
    symbols = sample_symbols or [r["symbol"] for r in targets]
    histories, failed = {}, 0
    # 小批次提交；源不可用时不把400个请求全部排队。
    with ThreadPoolExecutor(max_workers=6) as pool:
        for start in range(0, len(targets), 18):
            def fetch(row):
                symbol = row["symbol"]
                data = _fetch_json("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                                   f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen=300")
                return symbol, data
            futures = [pool.submit(fetch, row) for row in targets[start:start+18]]
            for future in as_completed(futures):
                try:
                    symbol, data = future.result()
                    bars = [r for r in normalize_bars(data) if r["date"] <= as_of]
                    if len(bars) >= 250 and bars[-1]["date"] == as_of:
                        histories[symbol] = bars
                    else:
                        failed += 1
                except Exception:
                    failed += 1
            progress(len(histories)+failed, len(targets))
            if failed >= 18 and not histories:
                break
    if len(histories) < 200:
        raise ValueError(f"同日完整K线仅{len(histories)}只，未达200只；保留原广度及其日期")
    dates = sorted({d for d in (calendar or []) if d <= as_of})[-5:]
    if not dates:
        # 无基准指数时不假定自然日是交易日；以最完整个股序列提供候选日。
        dates = [r["date"] for r in max(histories.values(), key=len)[-5:]]
    observations = breadth_history(histories, dates)
    if not observations or observations[-1]["as_of"] != as_of or observations[-1]["sample"] < 200:
        raise ValueError("连续五日完整同组样本不足200只，保留原广度及其日期")
    result = dict(observations[-1])
    result.update(requested=len(symbols), failed=failed+len(symbols)-len(targets), sample_symbols=symbols,
                  observations=observations,
                  note="首次按市值8层、每层最多50只沪深A股抽样，后续保留样本组；用300根真实历史回算最近5个交易日，窗口固定成分和逐指标分母。新浪未复权日线，除权可能影响均线和回撤。")
    return result


def merge_indices(previous, incoming):
    """失败、短历史或倒退日期都保留旧序列，避免覆盖为更差的数据。"""
    result = deepcopy(previous)
    accepted = 0
    for symbol in INDEXES:
        rows = normalize_bars(incoming.get(symbol, []))
        old = normalize_bars(previous.get(symbol, []))
        if len(rows) >= 250 and (not old or rows[-1]["date"] >= old[-1]["date"]):
            merged = {r["date"]: r for r in old+rows}
            result[symbol] = sorted(merged.values(), key=lambda r: r["date"])[-1023:]
            accepted += 1
    return result, accepted


def start_refresh(mode, jobs, index_loader):
    if mode not in ("indices", "full", "cross"):
        raise ValueError("未知更新模式")
    with _start_lock:
        active = latest_job(jobs)
        if active and active.get("status") in ("queued", "running"):
            return active

        def worker(job_id, params):
            from data_store.connection import close_conn
            jobs.update(job_id, status="running", started_at=now().isoformat(), progress=0)
            try:
                state = read_state()
                jobs.append_log(job_id, "更新七大指数日线（收盘口径）")
                try:
                    incoming = index_loader(INDEXES, datalen=1023) or {}
                except Exception:
                    incoming = {}
                # 当日未收盘K线不参与全天量比和确认判断。
                if now().time() < dt.time(15, 30):
                    incoming = {s: [b for b in rows if str(b.get("day") or b.get("date") or "")[:10] < now().date().isoformat()]
                                for s, rows in incoming.items()}
                state["bars"], accepted = merge_indices(state.get("bars", {}), incoming)
                if not accepted and (mode == "indices" or not build_health(state)["as_of"]):
                    raise ValueError("指数源未返回可用的新日线，原快照保留")
                partial = accepted < len(INDEXES)
                notes = [f"本轮有效指数{accepted}/7；其余保留原日期。" if accepted else "指数未刷新，沿用已有收盘日期，继续读取其他接口。"]
                jobs.update(job_id, progress=15)
                if mode in ("full", "cross"):
                    from webui.services import market_health_cross_service
                    as_of = build_health(state)["as_of"]
                    calendar = [b["date"] for b in normalize_bars(state.get("bars",{}).get("sh000001",[])) if b["date"] < as_of]
                    jobs.append_log(job_id,"并行接入现有期货、两融、量化与资金接口")
                    cross = market_health_cross_service.collect(as_of,lambda message:jobs.append_log(job_id,message), previous_date=calendar[-1] if calendar else None)
                    fusion = build_fusion(build_health(state),cross,state.get("bars",{}).get("sh000001",[]))
                    if fusion["missing"]:
                        partial = True
                        notes.append("跨市场待补或日期过期："+"、".join(fusion["missing"]))
                    # 跨市场证据先可见，广度采样不阻塞综合结果读取。
                    state.update(source="updated",updated_at=now().isoformat(),notes=notes)
                    kv_repo.set_(NAMESPACE,"snapshot",state)
                if mode == "full":
                    try:
                        before = quote_date()
                        if before == now().date().isoformat() and now().time() < dt.time(15, 30):
                            raise ValueError("全市场指标采用收盘口径，请15:30后更新；本轮仅更新已收盘指数")
                        rows = fetch_market(expected_count=(state.get("market",{}).get("values") or {}).get("total"))
                        after = quote_date()
                        if before != after:
                            raise ValueError("采集跨交易日，保留原全市场快照")
                        values = market_snapshot(rows)
                        if values["total"] < 3000:
                            raise ValueError("有效正价格行情不足3000只，原全市场快照保留")
                        state["market"] = {"as_of": after, "values": values, "source": "新浪全A快照 · 指数完整报价校验交易日期"}
                        jobs.update(job_id, progress=35)
                        jobs.append_log(job_id, f"全A快照{len(rows)}只；开始400只分层广度采样")
                        calendar = [r["date"] for r in normalize_bars(state["bars"].get("sh000001",[])) if r["date"] <= after]
                        state["breadth"] = fetch_breadth(rows, after, lambda n, total: jobs.update(job_id, progress=35+round(60*n/max(total, 1))), state.get("breadth", {}).get("sample_symbols"), calendar)
                        observations = {r["as_of"]: r for r in state.get("observations", [])}
                        for observation in state["breadth"].pop("observations",[]):
                            observations[observation["as_of"]] = observation
                        state["observations"] = sorted(observations.values(), key=lambda r: r["as_of"])[-120:]
                    except Exception as exc:
                        partial = True
                        notes.append(str(exc) if isinstance(exc, ValueError) else f"全市场/广度更新失败（{type(exc).__name__}），原值及日期保留")
                elif mode == "indices":
                    notes.append("本轮只更新指数；旧日期广度、估值不参与新交易日评分。完整更新可补齐。")
                else:
                    notes.append("跨市场接口已读取；如广度/估值日期落后，请完整更新公开数据。")
                state.update(source="updated", updated_at=now().isoformat(), notes=notes)
                kv_repo.set_(NAMESPACE, "snapshot", state)
                jobs.update(job_id, status="finished", progress=100, finished_at=now().isoformat(), result={"notes": notes, "partial": partial})
                for note in notes:
                    jobs.append_log(job_id, note)
            except Exception as exc:
                message = str(exc) if isinstance(exc, ValueError) else f"体检更新失败（{type(exc).__name__}），原快照保留"
                jobs.update(job_id, status="failed", finished_at=now().isoformat(), error=message)
                jobs.append_log(job_id, message)
            finally:
                close_conn()

        job = jobs.start("market_health", {"mode": mode}, worker)
        kv_repo.set_(NAMESPACE, "latest_job", job["id"])
        return job
