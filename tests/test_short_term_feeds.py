"""分区独立采集、版本响应与慢来源隔离。"""
import copy
import datetime as dt
import threading
import time

import pytest

from webui.services import short_term_screen_service as service


@pytest.fixture
def isolated(monkeypatch):
    stored={}
    monkeypatch.setattr(service,"_feeds",{})
    monkeypatch.setattr(service,"now",lambda:dt.datetime(2026,11,2,10,0))
    monkeypatch.setattr(service,"_local_sources",lambda:copy.deepcopy(stored.get("inputs",{})))
    monkeypatch.setattr(service.kv_repo,"set_",lambda namespace,key,value:stored.__setitem__(key,copy.deepcopy(value)))
    monkeypatch.setattr(service,"close_conn",lambda:None)
    return stored


def completed(kind):
    deadline=time.monotonic()+4
    while service._feeds[kind]["running"] and time.monotonic()<deadline:
        time.sleep(.005)
    assert not service._feeds[kind]["running"]
    return service._feeds[kind]


def test_slow_sector_does_not_hold_radar_or_lose_other_inputs(isolated):
    entered=threading.Event();release=threading.Event();count=[]
    def sectors():
        count.append("sectors");entered.set();assert release.wait(3)
        return {"as_of":"2026-10-30","rows":[{"code":"BK1","name":"半导体","net":1e8}]}
    def radar():
        count.append("radar")
        return {"fallback_date":"2026-11-02","stocks":[{"code":"600001","name":"示例","industry":"半导体","changes_total":2}]}
    readers={"sectors":sectors,"radar":radar}
    try:
        assert service.feed("sectors",None,readers=readers)["running"]
        assert entered.wait(1)
        service.feed("sectors",None,readers=readers)
        service.feed("radar",None,readers=readers)
        state=completed("radar")
        assert state["payload"]["stocks"][0]["code"]=="600001"
        assert service._feeds["sectors"]["running"]
        first=service.feed("radar",None,readers=readers)
        assert first["changed"] and first["revision"]>0
        same=service.feed("radar",None,after=first["version"],readers=readers)
        assert not same["changed"] and same["payload"] is None
    finally:
        release.set();completed("sectors")
    assert count.count("sectors")==count.count("radar")==1
    assert set(isolated["inputs"])>={"radar","sectors"}
    assert service._feeds["sectors"]["revision"]>state["revision"]


def test_failed_source_keeps_visible_snapshot_and_retries_independently(isolated):
    isolated["inputs"]={"jinshi":[{"title":"已有消息","time":"2026-11-02 09:00"}]}
    def failed():raise ValueError("unavailable")
    service.feed("news",None,readers={"jinshi":failed})
    state=completed("news")
    assert state["error"] and state["payload"] is None
    assert isolated["inputs"]["jinshi"][0]["title"]=="已有消息"
    assert not service.feed("news",None,readers={"jinshi":failed})["running"]


def test_feed_intervals_differ_and_history_never_launches_workers(isolated,monkeypatch):
    assert service.FEED_INTERVALS=={"radar":60,"news":45,"popularity":180,"sectors":120,"limits":90}
    monkeypatch.setattr(service,"query",lambda **kwargs:{"news":[{"id":"old"}]})
    assert service.feed("news",None,date="2026-10-30")["historical"]
    assert not service._feeds
    with pytest.raises(ValueError):service.feed("invalid",None)
