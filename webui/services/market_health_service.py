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

from analysis.market_health import INDEXES, breadth_history, build_health, build_intraday_health, market_snapshot, normalize_bars, number
from analysis.market_health_fusion import build_fusion
from data_store import kv_repo
from webui.services.http_client import request_json, request_text

NAMESPACE = "market_health"
BASELINE = Path(__file__).resolve().parents[2] / "config/market_health_baseline.json"
_start_lock = threading.Lock()
_quote_lock = threading.Lock()
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


def parse_live_quotes(raw, provider="sina"):
    quotes = {}
    for symbol, body in re.findall(r'(?:hq_str_|v_)((?:sh|sz)\d{6})="([^"]+)"',raw):
        if symbol not in INDEXES:
            continue
        fields = body.split("," if provider == "sina" else "~")
        try:
            if provider == "sina":
                date, time = fields[30], fields[31]
                price, previous, open_, high, low = [number(fields[i]) for i in (3,2,1,4,5)]
                stamp = dt.datetime.fromisoformat(f"{date}T{time}")
            else:
                stamp = dt.datetime.strptime(fields[30],"%Y%m%d%H%M%S")
                price, previous, open_, high, low = [number(fields[i]) for i in (3,4,5,33,34)]
            if price and price > 0 and previous and previous > 0:
                quotes[symbol] = {"price":price,"previous_close":previous,"open":open_,"high":high,"low":low,
                                  "as_of":stamp.date().isoformat(),"quote_at":stamp.isoformat(),
                                  "source":"新浪实时指数报价" if provider == "sina" else "腾讯实时指数报价"}
        except (IndexError,ValueError):
            continue
    return quotes


def fetch_live_quotes(force=False):
    """独立于慢任务的实时通道，缓存30秒；失败保留真实行情时间并返回错误。"""
    with _quote_lock:
        cached = kv_repo.get(NAMESPACE,"live_quotes")
        old = cached[0] if cached else {}
        current = now().replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        fetched = old.get("fetched_at")
        try:
            age = (current-dt.datetime.fromisoformat(fetched)).total_seconds()
            if not force and 0 <= age < 30:
                return old
        except (TypeError,ValueError):
            pass
        quotes = {}
        for provider, url in (("sina","https://hq.sinajs.cn/list="),("tencent","https://qt.gtimg.cn/q=")):
            try:
                raw = request_text(url+",".join(INDEXES),headers={**HEADERS,"Cache-Control":"no-cache"},
                                   encoding="gbk",timeout=6,trust_env=False)
                found = parse_live_quotes(raw,provider)
                for symbol,q in found.items():
                    stamp = dt.datetime.fromisoformat(q["quote_at"]).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                    if stamp <= current+dt.timedelta(seconds=60) and (symbol not in quotes or q["quote_at"] > quotes[symbol]["quote_at"]):
                        quotes[symbol] = q
                if len(quotes) == len(INDEXES) and all(live_status({"quotes":{"sh000001":q}},"1900-01-01")["available"] for q in quotes.values()):
                    break
            except Exception:
                continue
        value = {"quotes":quotes or old.get("quotes",{}),"fetched_at":current.isoformat(),
                 "error":None if len(quotes) == len(INDEXES) else f"实时指数成功{len(quotes)}/7；缺失品种沿用收盘，按实际时点展示" if quotes else "实时行情源不可用，沿用最后成功报价；请以行情时间判断时效"}
        kv_repo.set_(NAMESPACE,"live_quotes",value)
        return value


def live_status(value, closed_date):
    current = now().replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    quotes = value.get("quotes") or {}
    benchmark = quotes.get("sh000001") or {}
    date = benchmark.get("as_of")
    status = {"quote_at":benchmark.get("quote_at"),"requested_at":value.get("fetched_at"),
              "error":value.get("error"),"available":False,"as_of":date,"closed_as_of":closed_date}
    try:
        stamp = dt.datetime.fromisoformat(benchmark["quote_at"]).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        reference = current
        if dt.time(11,30) <= current.time() < dt.time(13):
            reference = current.replace(hour=11,minute=30,second=0,microsecond=0)
        elif current.time() >= dt.time(15):
            reference = current.replace(hour=15,minute=0,second=0,microsecond=0)
        fresh = -60 <= (reference-stamp).total_seconds() <= 300
        status["available"] = date == current.date().isoformat() and fresh and date > str(closed_date or "")
        status["message"] = ("今日盘中预警；尚未收盘确认" if status["available"] and current.time() < dt.time(15) else
                             "今日最新报价；等待收盘日线入库确认" if status["available"] else
                             "已采用最新收盘数据" if date == closed_date and date == current.date().isoformat() else
                             "今日报价已滞后超过5分钟，暂停盘中判定" if date == current.date().isoformat() else
                             f"行情源最新交易日为{date}；当前显示{closed_date}收盘，尚无今日有效报价")
    except (KeyError,TypeError,ValueError):
        status["message"] = "实时行情未取得；当前显示已保存收盘，不能视为今日盘中判断"
    return status


def query(jobs=None, refresh_live=False, force_live=False):
    from webui.services import market_health_cross_service
    state = read_state()
    result = build_health(state)
    result["fusion"] = build_fusion(result, market_health_cross_service.read_cached(result["as_of"]), state.get("bars",{}).get("sh000001",[]))
    result["generated_at"] = now().isoformat()
    result["age_days"] = (now().date()-dt.date.fromisoformat(result["as_of"])).days if result["as_of"] else None
    result["stale"] = result["age_days"] is not None and result["age_days"] > 4
    result["job"] = latest_job(jobs) if jobs else None
    result["report_url"] = "/static/research/ashare_bottom_20261022.html"
    cached = kv_repo.get(NAMESPACE,"live_quotes")
    quotes = fetch_live_quotes(force=force_live) if refresh_live else cached[0] if cached else {}
    status = live_status(quotes,result["as_of"])
    result["live_status"] = status
    result["confirmation_as_of"] = result["as_of"]
    result["intraday"] = None
    if status["available"]:
        snapshot = kv_repo.get(NAMESPACE,"intraday")
        live = snapshot[0] if snapshot and snapshot[0].get("as_of") == status["as_of"] else {}
        live = {**live,"as_of":status["as_of"],"quotes":quotes["quotes"]}
        # 每个品种单独检查行情时间，不能用上证的新时间替其他旧报价背书。
        benchmark = dt.datetime.fromisoformat(status["quote_at"])
        live["quotes"] = {s:q for s,q in live["quotes"].items()
                          if q["as_of"] == status["as_of"] and abs((benchmark-dt.datetime.fromisoformat(q["quote_at"])).total_seconds()) <= 300}
        preview = build_intraday_health(state,live)
        if preview:
            bars = preview.pop("_bars")
            preview["fusion"] = build_fusion(preview,market_health_cross_service.read_cached(preview["as_of"]),bars.get("sh000001",[]))
            preview["updated_at"] = live.get("captured_at")
            preview["market"]["provisional"] = preview["market"].get("as_of") == preview["as_of"]
            preview["breadth"]["provisional"] = preview["breadth"].get("as_of") == preview["as_of"]
            result["intraday"] = preview
        else:
            status.update(available=False,message="已取得今日报价，但历史末日与报价昨收不一致；需更新指数历史后才能计算今日均线")
    result["timing"] = indicator_timing(result,False)
    annotate_rules(result)
    if result["intraday"]:
        result["intraday"]["timing"] = indicator_timing(result["intraday"],True)
        annotate_rules(result["intraday"])
    return result


def annotate_rules(health):
    indices = {r["symbol"]:r for r in health["indices"]}
    for side in ("bottom","top"):
        for rule in health["signals"][side]["rules"]:
            key = rule["key"]
            if key in ("breadth250","breadth20","dd52"):
                source = health.get("breadth") or {}
            elif key in ("up","pb","pb1"):
                source = health.get("market") or {}
            elif key == "volume":
                source = {"as_of":health.get("confirmation_as_of") or health["as_of"]}
            else:
                source = indices.get("sz399006" if key.startswith("cy_") else "sh000001",{})
            rule["as_of"] = source.get("as_of")
            rule["data_at"] = source.get("quote_at") or source.get("captured_at") or source.get("as_of")
            rule["timing_note"] = "全天量能收盘后确认" if key == "volume" and health.get("provisional") else ""


def indicator_timing(health, provisional):
    today = now().date().isoformat()
    rows = []
    for index in health["indices"]:
        rows.append({"label":index["name"]+" · 价格/均线/RSI/涨跌/波动率", "as_of":index.get("as_of"),
                     "data_at":index.get("quote_at") or index.get("as_of"),"source":"实时指数报价＋收盘历史日线" if index.get("provisional") else "指数收盘日线",
                     "kind":"今日盘中" if index.get("provisional") else "今日收盘" if index.get("as_of") == today else "上一收盘 / 非今日",
                     "note":"价格类临时计算，全天量能收盘后确认" if index.get("provisional") else "按该交易日已收盘日线计算"})
    for key,label in (("market","全A涨跌/估值/涨跌停估计"),("breadth","抽样广度/新高新低/回撤")):
        data = health.get(key) or {}
        rows.append({"label":label,"as_of":data.get("as_of"),"data_at":data.get("captured_at") or data.get("as_of"),
                     "source":data.get("source") or data.get("method"),"kind":"今日盘中采样" if data.get("provisional") else "今日收盘" if data.get("as_of") == today else "旧日采样 / 非今日",
                     "note":f"采集区间 {data.get('sample_started_at')} 至 {data.get('captured_at')}，各股票为对应报价时刻" if data.get("provisional") else "按实际采样日展示，旧日期不计入今日规则"})
    for dim in health["fusion"]["dimensions"]:
        if dim["key"] in ("trend","breadth","valuation"):
            continue
        details = dim.get("details") or {}
        kind = ("当日接口数据" if dim.get("as_of") == today else
                "上一交易日披露" if provisional and dim.get("status") == "lagged" else
                "收盘视图所选日" if not provisional and dim.get("as_of") == health.get("as_of") else
                "历史接口数据")
        rows.append({"label":dim["label"],"as_of":dim.get("as_of"),"data_at":dim.get("as_of"),"source":dim["source"],
                     "kind":kind,
                     "note":details.get("error") or "以接口返回交易日为准，披露前不会显示为今日；日期失效时排除评分"})
        if dim["key"] == "futures":
            for basis in details.get("basis",[]):
                rows.append({"label":f"{basis['variety']}期指基差","as_of":basis.get("date"),"data_at":basis.get("quote_at") or basis.get("date"),
                             "source":basis.get("source"),"kind":"当日期现报价" if basis.get("date") == today else "历史期现报价",
                             "note":f"现货报价时间 {basis.get('spot_quote_at') or '历史收盘'}；同合约变化对比{basis.get('previous_date') or '未取得'}"})
            for position in details.get("positions",[]):
                rows.append({"label":f"{position['variety']}前20席位持仓统计","as_of":position.get("date"),"data_at":position.get("date"),
                             "source":"中金所披露 / 前20席位汇总","kind":"当日披露" if position.get("date") == today else "上一交易日披露" if position.get("status") == "lagged" else "历史披露",
                             "note":"持仓排名盘后披露；盘中不能获得今日最终席位持仓"})
    rows.append({"label":"反转确认 / 全天量比 / 连续五日收盘广度","as_of":health.get("confirmation_as_of") or health["as_of"],
                 "data_at":health.get("confirmation_as_of") or health["as_of"],"source":"已收盘交易日序列","kind":"收盘确认",
                 "note":"盘中预警不改写收盘确认，次日守住和五日连续条件只用已完成交易日"})
    rows.append({"label":"历史条件收益 / 胜率 / 情景假设","as_of":health.get("historical_as_of"),"data_at":health.get("historical_as_of"),
                 "source":"原报告固定研究结果","kind":"固定历史研究","note":"不是今日统计；更新行情不会重写原报告回测"})
    rows.append({"label":"温度 / 顶底强度 / 七维综合压力","as_of":health["as_of"],"data_at":health.get("quote_at") or health["as_of"],
                 "source":"逐项有效输入派生计算","kind":"盘中临时判断" if provisional else "收盘口径判断",
                 "note":"综合指标使用不同披露时点，逐维日期及权重见上表；有效权重不足时不输出结论"})
    return rows


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
            # 200响应也可能偶发空页；后页有数据说明并非真正尾页，单独重试该页。
            if len(items) < 100 and any(batch[offset+1:]):
                for _ in range(2):
                    items = page(start+offset)
                    if len(items) == 100:
                        break
                if len(items) < 100:
                    raise ValueError(f"全A行情分页中间缺失（第{start+offset}页仅{len(items)}条），保留已有全市场快照")
            rows.extend(items)
            if len(items) < 100:
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


def fetch_breadth(rows, as_of, progress, sample_symbols=None, calendar=None, intraday=False):
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
                bars = [r for r in normalize_bars(data) if r["date"] < as_of] if intraday else data
                if intraday and (number(row.get("volume")) or 0) > 0 and (number(row.get("open")) or 0) > 0:
                    bars.append({"date":as_of,"close":number(row.get("trade")),
                                 "open":number(row.get("open")),"high":number(row.get("high")),"low":number(row.get("low")),"volume":None})
                return symbol, bars
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
                quote_payload = fetch_live_quotes(force=True)
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
                source_failed = bool(quote_payload.get("error"))
                notes = [f"本轮有效指数{accepted}/7；其余保留原日期。" if accepted else "指数未刷新，沿用已有收盘日期，继续读取其他接口。"]
                current_status = live_status(quote_payload,build_health(state)["as_of"])
                live_date = current_status["as_of"] if current_status["available"] else None
                if quote_payload.get("error"):
                    notes.append(quote_payload["error"])
                # 收盘历史先保存；实时通道可独立读取，不必等待跨市场与400只采样。
                state.update(source="updated",updated_at=now().isoformat(),notes=notes)
                kv_repo.set_(NAMESPACE,"snapshot",state)
                jobs.update(job_id, progress=15)
                if mode in ("full", "cross"):
                    from webui.services import market_health_cross_service
                    as_of = live_date or build_health(state)["as_of"]
                    calendar = [b["date"] for b in normalize_bars(state.get("bars",{}).get("sh000001",[])) if b["date"] < as_of]
                    jobs.append_log(job_id,"并行接入现有期货、两融、量化与资金接口")
                    cross = market_health_cross_service.collect(as_of,lambda message:jobs.append_log(job_id,message), previous_date=calendar[-1] if calendar else None)
                    health = build_health(state)
                    if live_date:
                        health = build_intraday_health(state,{"as_of":live_date,"quotes":quote_payload["quotes"]}) or health
                    fusion = build_fusion(health,cross,(health.pop("_bars",None) or state.get("bars",{})).get("sh000001",[]))
                    if fusion["missing"]:
                        partial = True
                        notes.append("跨市场待补或日期过期："+"、".join(fusion["missing"]))
                    # 跨市场证据先可见，广度采样不阻塞综合结果读取。
                    state.update(source="updated",updated_at=now().isoformat(),notes=notes)
                    kv_repo.set_(NAMESPACE,"snapshot",state)
                if mode == "full":
                    try:
                        before = quote_date()
                        intraday = before == now().date().isoformat() and before > build_health(state)["as_of"]
                        started = now().isoformat()
                        rows = fetch_market(expected_count=(state.get("market",{}).get("values") or {}).get("total"))
                        after = quote_date()
                        if before != after:
                            raise ValueError("采集跨交易日，保留原全市场快照")
                        values = market_snapshot(rows)
                        if values["total"] < 3000:
                            raise ValueError("有效正价格行情不足3000只，原全市场快照保留")
                        market = {"as_of": after,"captured_at":now().isoformat(),"sample_started_at":started,
                                  "provisional":intraday,"values":values,"source":"新浪全A快照 · 指数完整报价校验交易日期"}
                        live = {"as_of":after,"market":market,"captured_at":now().isoformat()}
                        if intraday:
                            kv_repo.set_(NAMESPACE,"intraday",live)
                        else:
                            state["market"] = market
                        jobs.update(job_id, progress=35)
                        jobs.append_log(job_id, f"全A快照{len(rows)}只；开始400只分层广度采样")
                        calendar = [r["date"] for r in normalize_bars(state["bars"].get("sh000001",[])) if r["date"] <= after]
                        if intraday and after not in calendar:
                            calendar.append(after)
                        breadth = fetch_breadth(rows, after, lambda n, total: jobs.update(job_id, progress=35+round(60*n/max(total, 1))), state.get("breadth", {}).get("sample_symbols"), calendar, intraday=intraday)
                        breadth.update(captured_at=now().isoformat(),sample_started_at=started,provisional=intraday)
                        observations = {r["as_of"]: r for r in state.get("observations", [])}
                        for observation in breadth.pop("observations",[]):
                            observations[observation["as_of"]] = observation
                        observations = sorted(observations.values(), key=lambda r: r["as_of"])[-120:]
                        if intraday:
                            live.update(breadth=breadth,observations=observations,captured_at=now().isoformat())
                            kv_repo.set_(NAMESPACE,"intraday",live)
                            notes.append(f"已采集{after}盘中全A和广度；全天量能、反转与连续五日确认保留最新收盘口径。")
                        else:
                            state.update(breadth=breadth,observations=observations)
                    except Exception as exc:
                        partial = True
                        source_failed = True
                        notes.append(str(exc) if isinstance(exc, ValueError) else f"全市场/广度更新失败（{type(exc).__name__}），原值及日期保留")
                elif mode == "indices":
                    notes.append("本轮更新实时指数与收盘历史；广度和估值另按采样日期展示，完整更新可补齐今日采样。")
                else:
                    notes.append("跨市场接口已读取；如广度/估值日期落后，请完整更新公开数据。")
                if mode in ("full","cross"):
                    final_health = build_health(state)
                    if live_date:
                        cached_live = kv_repo.get(NAMESPACE,"intraday")
                        current_live = cached_live[0] if cached_live and cached_live[0].get("as_of") == live_date else {}
                        final_health = build_intraday_health(state,{**current_live,"as_of":live_date,"quotes":quote_payload["quotes"]}) or final_health
                    final_fusion = build_fusion(final_health,cross,(final_health.pop("_bars",None) or state.get("bars",{})).get("sh000001",[]))
                    notes = [n for n in notes if not n.startswith("跨市场待补或日期过期：")]
                    if final_fusion["missing"]:
                        notes.append("跨市场待补或日期过期："+"、".join(final_fusion["missing"]))
                    partial = accepted < len(INDEXES) or source_failed or bool(final_fusion["missing"])
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
