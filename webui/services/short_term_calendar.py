"""可追溯的会议基线与财经日历，过期日程自动移出未来列表。"""
from __future__ import annotations

import datetime as dt
import re

from data_store import tushare_client
from webui.services.market_health_service import now

CONFERENCES = [
    {"date": "2026-11-02", "end_date": "2026-11-05", "title": "2026世界互联网大会乌镇峰会", "category": "产业会议", "location": "浙江乌镇",
     "themes": ["人工智能"], "source": "世界互联网大会", "url": "https://www.wicinternet.org/2026-10/29/c_1223203.htm",
     "summary": "官方预告峰会于11月2日至5日举办，关注开源、人工智能与数字技术议题。", "watch": "核验正式议程、发布成果、参与公司公告和业务落地，勿把参会等同于新增订单。",
     "verified": True, "verified_at": "2026-11-01", "timezone": "Asia/Shanghai"},
    {"date": "2026-11-05", "end_date": "2026-11-10", "title": "第九届中国国际进口博览会", "category": "重大展会", "location": "国家会展中心（上海）",
     "themes": ["消费", "医药", "机器人"], "source": "中国国际进口博览会", "url": "https://www.ciie.org/zbh/cn/19news/notice/20260930/64263.html",
     "summary": "官方公布11月5日至10日会期及首批29场配套活动，涉及政策解读、行业研讨、产品发布与对接签约。", "watch": "核验具体分论坛时间与公开签约公告，区分展示、意向协议与已确认收入。",
     "verified": True, "verified_at": "2026-11-01", "timezone": "Asia/Shanghai"},
    {"date": "2026-11-10", "end_date": "2026-11-15", "title": "第十六届中国国际航空航天博览会", "category": "重大展会", "location": "珠海国际航展中心",
     "themes": ["商业航天"], "source": "中国航展主办方", "url": "https://www.airshow.com.cn/UploadFiles/business/2025/8/%E7%8F%A0%E6%B5%B7%E5%9B%BD%E9%99%85%E8%88%AA%E5%B1%95%E4%B8%AD%E5%BF%83%E4%BB%8B%E7%BB%8D.pdf",
     "summary": "主办方展馆介绍明确第十六届中国航展于2026年11月10日至15日举行。", "watch": "跟踪官方参展名单、首发装备、交付和订单公告；展会题材并不保证业绩兑现。",
     "verified": True, "verified_at": "2026-11-01", "timezone": "Asia/Shanghai"},
    {"date": "2026-12-08", "end_date": "2026-12-09", "title": "FOMC议息会议与经济预测", "category": "宏观会议", "location": "美国",
     "themes": ["金融", "资源"], "source": "美联储", "url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
     "summary": "美联储官方日历列示2026年12月8日至9日议息，并安排经济预测摘要。", "watch": "核对决议、经济预测和记者会原文，关注利率、美元与流动性；日期为美国当地日历，未猜测北京时间。",
     "verified": True, "verified_at": "2026-11-01", "timezone": "America/New_York（官方会议日期）"},
]


def calendar_rows():
    """按日期拆分财经日历；该源会忽略offset，达到100条上限时递归细分。"""
    pro = tushare_client.get_pro()
    if pro is None:
        raise ValueError("财经日历需要现有TuShare配置；官方会议基线仍可查看")
    begin = now().date()
    end = begin + dt.timedelta(days=45)
    result = []
    def fetch(first, last, country):
        frame = pro.eco_cal(start_date=first.strftime("%Y%m%d"), end_date=last.strftime("%Y%m%d"), country=country, limit=100)
        rows = [] if frame is None else frame.to_dict("records")
        if len(rows) < 100:
            return rows
        if first == last:
            raise ValueError("单日财经日历达到100条上限，保留旧日程，请核验来源覆盖")
        middle = first + (last - first) // 2
        return fetch(first, middle, country) + fetch(middle + dt.timedelta(days=1), last, country)

    for country in ("中国", "美国"):
        seen = set()
        cursor = begin
        while cursor <= end:
            last = min(end, cursor + dt.timedelta(days=6))
            for raw in fetch(cursor, last, country):
                signature = tuple(str(raw.get(k)) for k in ("date", "time", "country", "event"))
                if signature in seen:
                    continue
                day = tushare_client.yyyymmdd_to_iso(raw.get("date"))[:10]
                try:
                    day = dt.date.fromisoformat(day).isoformat()
                except ValueError:
                    continue
                if not cursor.isoformat() <= day <= last.isoformat():
                    continue
                seen.add(signature)
                result.append({"date": day, "title": tushare_client.text_field(raw.get("event")),
                    "time": tushare_client.text_field(raw.get("time")), "country": country,
                    "value": tushare_client.text_field(raw.get("value")), "previous": tushare_client.text_field(raw.get("pre_value")),
                    "forecast": tushare_client.text_field(raw.get("fore_value")), "category": "经济数据",
                    "source": "TuShare财经日历", "url": "https://tushare.pro/document/2?doc_id=233", "verified": False,
                    "verified_at": now().date().isoformat(), "summary": "数据商日程，需以发布机构最终安排为准。",
                    "watch": "比较正式公布值与预期、前值；预期值不是实际结果。", "timezone": "来源未明确时区，未转换为北京时间"})
            cursor = last + dt.timedelta(days=1)
    return result
