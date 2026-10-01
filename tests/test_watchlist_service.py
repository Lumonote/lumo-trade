"""自选行情、升级迁移及持久化保护。"""
import json
import multiprocessing
import os
from pathlib import Path

import pytest

from webui.services.watchlist_service import (
    WatchlistService,
    WatchlistStorageError,
    _VALID_CODE,
    _eastmoney_secid,
    _tencent_symbol,
)


def test_valid_code_accepts_beijing_920():
    """北交所 920xxx 应通过自选代码校验。"""
    assert _VALID_CODE.match("920161")


def test_eastmoney_quote_retains_exchange_date(tmp_path, monkeypatch):
    import datetime
    from zoneinfo import ZoneInfo
    import webui.services.watchlist_service as module
    timestamp = datetime.datetime(2026, 9, 30, 15, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    monkeypatch.setattr(module, "request_json", lambda *a, **k: {"data": {"diff": [
        {"f12": "000001", "f2": 10.5, "f124": timestamp}]}})
    quote = WatchlistService(tmp_path / "watch.json")._quotes_eastmoney(["000001"])["000001"]
    assert quote["quote_date"] == "2026-09-30"


def test_tencent_quote_retains_exchange_date(tmp_path, monkeypatch):
    import webui.services.watchlist_service as module
    fields = [""] * 33
    fields[2], fields[3], fields[30] = "000001", "10.5", "20260930145900"
    monkeypatch.setattr(module, "request_text", lambda *a, **k: 'v_sz000001="' + "~".join(fields) + '";')
    quote = WatchlistService(tmp_path / "watch.json")._quotes_tencent(["000001"])["000001"]
    assert quote["quote_date"] == "2026-09-30"


def test_valid_code_still_accepts_known_segments():
    for code in ("600519", "000001", "300750", "830799", "430047"):
        assert _VALID_CODE.match(code), code


def test_eastmoney_secid_beijing_is_market_0():
    """北交所 920xxx 在东财 secid 中归市场 0（不可因 9 开头被当沪市 1）。"""
    assert _eastmoney_secid("920161") == "0.920161"


def test_eastmoney_secid_shanghai_b_share_is_market_1():
    assert _eastmoney_secid("900001") == "1.900001"
    assert _eastmoney_secid("600519") == "1.600519"


def test_tencent_symbol_beijing_is_bj():
    """北交所 920xxx 腾讯行情前缀须为 bj（不可因 9 开头被当 sh）。"""
    assert _tencent_symbol("920161") == "bj920161"


def test_tencent_symbol_shanghai_b_share_is_sh():
    assert _tencent_symbol("900001") == "sh900001"
    assert _tencent_symbol("600519") == "sh600519"


def test_list_with_quotes_includes_sector_and_return_summary(tmp_path, monkeypatch):
    svc = WatchlistService(tmp_path / "watchlist.json")
    svc.add("600519", "贵州茅台")
    svc.add("000001", "平安银行")
    monkeypatch.setattr(svc, "quotes", lambda codes: {
        "600519": {"name": "贵州茅台", "price": 1800.0, "change_pct": 2.5, "main_net_inflow": 100000000.0},
        "000001": {"name": "平安银行", "price": 12.0, "change_pct": -1.0, "main_net_inflow": -20000000.0},
    })
    monkeypatch.setattr(svc, "_sector_info", lambda code: {
        "600519": {"sector": "白酒", "boards": ["白酒概念"]},
        "000001": {"sector": "银行", "boards": ["银行"]},
    }[code])

    out = svc.list_with_quotes()

    assert out["items"][0]["sector"] == "银行"
    assert out["items"][1]["sector"] == "白酒"
    ret = out["summary"]["return_summary"]
    assert ret["avg_change_pct"] == 0.75
    assert ret["up_count"] == 1
    assert ret["down_count"] == 1
    assert ret["main_net_inflow"] == 80000000.0
    sectors = out["summary"]["sector_summary"]["top_sectors"]
    assert {s["name"] for s in sectors} == {"白酒", "银行"}


def test_new_add_goes_after_pinned_items(tmp_path):
    """置顶后再添加新股票：新股票应排在置顶之后（不压过置顶）。"""
    svc = WatchlistService(tmp_path / "watchlist.json")
    svc.add("600519", "贵州茅台")
    svc.add("000001", "平安银行")
    # 置顶 600519
    svc.pin("600519", True)
    # 添加新股票
    svc.add("300750", "宁德时代")
    codes = [it["code"] for it in svc.list_items()]
    assert codes[0] == "600519", "置顶股票应保持在最上"
    assert codes[1] == "300750", "新添加股票应紧跟置顶之后"
    assert codes[2] == "000001"


def test_pin_then_unpin_restores_order(tmp_path):
    """取消置顶后回到普通排序（非置顶按加入时间倒序）。"""
    svc = WatchlistService(tmp_path / "watchlist.json")
    svc.add("600519", "贵州茅台")
    svc.add("000001", "平安银行")
    svc.pin("600519", True)
    svc.pin("600519", False)  # 取消置顶
    codes = [it["code"] for it in svc.list_items()]
    assert codes == ["000001", "600519"], "取消置顶后按加入时间倒序（后加入在前）"


def test_multiple_pinned_stay_on_top_in_add_order(tmp_path):
    """多个置顶股票保持在最上，新添加股票始终排在其后。"""
    svc = WatchlistService(tmp_path / "watchlist.json")
    svc.add("600519", "贵州茅台")
    svc.add("000001", "平安银行")
    svc.pin("600519", True)
    svc.pin("000001", True)
    svc.add("300750", "宁德时代")
    svc.add("002594", "比亚迪")
    codes = [it["code"] for it in svc.list_items()]
    assert codes[:2] == ["600519", "000001"], "置顶组保持在最上"
    assert codes[2:] == ["002594", "300750"], "新添加的按加入时间倒序排在置顶之后"


def _stored_items(path, items):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"items": items}), encoding="utf-8")


def test_upgrade_migrates_old_directory_and_survives_bundle_replacement(tmp_path, monkeypatch):
    from webui.services.paths import legacy_watchlist_paths

    old = tmp_path / "com.kronos.app/config/watchlist.json"
    item = {"code": "600519", "name": "贵州茅台", "added_at": "2026-06-01T09:30:00", "pinned": True}
    _stored_items(old, [item])
    original = old.read_bytes()
    root = tmp_path / "com.lumo.trade"
    store = root / "config/watchlist.json"
    service = WatchlistService(store, legacy_paths=legacy_watchlist_paths(root))
    monkeypatch.setattr(service, "quotes", lambda codes: {})
    monkeypatch.setattr(service, "_sector_info", lambda code: {})
    payload = service.list_with_quotes()
    assert payload["items"][0]["pinned"] is True
    assert "迁移" in payload["storage_notice"]
    assert old.read_bytes() == original
    assert service.backup_path.exists()
    # Installing a new resource bundle changes PROJECT_ROOT, not user storage.
    monkeypatch.setenv("KRONOS_PROJECT_ROOT", str(tmp_path / "new-install"))
    reinstalled = WatchlistService(store, legacy_paths=legacy_watchlist_paths(root))
    assert reinstalled.list_items() == [item]
    reinstalled.remove("600519")
    assert WatchlistService(store, legacy_paths=[old]).list_items() == []
    assert json.loads(reinstalled.backup_path.read_text())["items"] == []


@pytest.mark.parametrize("items", [[], [{"code": "000001"}]])
def test_existing_list_is_not_overwritten_by_old_directory(tmp_path, items):
    store, old = tmp_path / "current/watchlist.json", tmp_path / "old/watchlist.json"
    _stored_items(store, items)
    _stored_items(old, [{"code": "600519", "pinned": True}])
    original = store.read_bytes()
    service = WatchlistService(store, legacy_paths=[old])
    assert {item["code"] for item in service.list_items()} == {item["code"] for item in items}
    assert store.read_bytes() == original


def test_newest_legacy_store_wins_including_deliberate_empty_list(tmp_path):
    old, newer = tmp_path / "old/watchlist.json", tmp_path / "newer/watchlist.json"
    _stored_items(old, [{"code": "600519"}])
    _stored_items(newer, [])
    os.utime(old, (1, 1))
    os.utime(newer, (2, 2))
    service = WatchlistService(tmp_path / "current/watchlist.json", legacy_paths=[old, newer])
    assert service.list_items() == []
    assert service.store_path.exists()


def test_bad_legacy_file_is_preserved_and_blocks_empty_overwrite(tmp_path):
    old = tmp_path / "old/watchlist.json"
    old.parent.mkdir()
    old.write_text('{"items": [')
    service = WatchlistService(tmp_path / "current/watchlist.json", legacy_paths=[old])
    with pytest.raises(WatchlistStorageError):
        service.add("000001")
    assert old.read_text() == '{"items": ['
    assert not service.store_path.exists()


@pytest.mark.parametrize("raw", [b'{"items": [', b"null", b'{"items": null}', b'{"items": [{"code": "bad"}]}', b"\xff"])
def test_invalid_storage_is_not_empty_and_cannot_be_overwritten(tmp_path, raw):
    store = tmp_path / "watchlist.json"
    store.write_bytes(raw)
    service = WatchlistService(store)
    for action in (service.list_items, lambda: service.add("000001"),
                   lambda: service.remove("600519"), lambda: service.pin("600519")):
        with pytest.raises(WatchlistStorageError):
            action()
        assert store.read_bytes() == raw


@pytest.mark.parametrize("missing", [False, True])
def test_last_saved_backup_restores_complete_list_and_pin_state(tmp_path, monkeypatch, missing):
    service = WatchlistService(tmp_path / "watchlist.json")
    service.add("600519", "贵州茅台")
    service.add("000001", "平安银行")
    service.pin("600519")
    expected = service.list_items()
    if missing:
        service.store_path.unlink()
    else:
        service.store_path.write_bytes(b"damaged bytes")
    reinstalled = WatchlistService(service.store_path)
    monkeypatch.setattr(reinstalled, "quotes", lambda codes: {})
    monkeypatch.setattr(reinstalled, "_sector_info", lambda code: {})
    payload = reinstalled.list_with_quotes()
    assert "备份恢复" in payload["storage_notice"]
    assert reinstalled.list_items() == expected
    damaged = list(tmp_path.glob("watchlist.json.damaged-*"))
    assert len(damaged) == (0 if missing else 1)
    if damaged:
        assert damaged[0].read_bytes() == b"damaged bytes"


def test_two_damaged_files_block_writes_without_replacing_either(tmp_path):
    service = WatchlistService(tmp_path / "watchlist.json")
    service.store_path.write_text("bad primary")
    service.backup_path.write_text("bad backup")
    with pytest.raises(WatchlistStorageError):
        service.add("000001")
    assert service.store_path.read_text() == "bad primary"
    assert service.backup_path.read_text() == "bad backup"


def test_permission_failure_does_not_restore_stale_backup(tmp_path, monkeypatch):
    service = WatchlistService(tmp_path / "watchlist.json")
    service.add("600519")
    original = service.store_path.read_bytes()
    read_text = Path.read_text

    def denied(path, *args, **kwargs):
        if path == service.store_path:
            raise PermissionError("temporarily locked")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(WatchlistStorageError):
        service.add("000001")
    assert service.store_path.read_bytes() == original
    assert not list(tmp_path.glob("watchlist.json.damaged-*"))


def test_failed_atomic_replace_keeps_saved_primary_and_backup(tmp_path, monkeypatch):
    service = WatchlistService(tmp_path / "watchlist.json")
    service.add("600519")
    original, backup = service.store_path.read_bytes(), service.backup_path.read_bytes()
    replace = Path.replace

    def fail_primary(path, target):
        if target == service.store_path:
            raise OSError("replace failed")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_primary)
    with pytest.raises(WatchlistStorageError):
        service.add("000001")
    assert service.store_path.read_bytes() == original
    assert service.backup_path.read_bytes() == backup
    assert not list(tmp_path.glob("*.tmp"))


def _add_from_process(args):
    path, start = args
    service = WatchlistService(Path(path))
    for number in range(start, start + 12):
        _, status = service.add(str(600000 + number))
        assert status == 200


def test_independent_backend_processes_do_not_lose_updates(tmp_path):
    store = tmp_path / "watchlist.json"
    with multiprocessing.get_context("spawn").Pool(4) as pool:
        pool.map(_add_from_process, [(str(store), start) for start in (0, 12, 24, 36)])
    service = WatchlistService(store)
    assert {item["code"] for item in service.list_items()} == {str(600000 + i) for i in range(48)}
    assert json.loads(service.backup_path.read_text())["items"] == json.loads(store.read_text())["items"]
