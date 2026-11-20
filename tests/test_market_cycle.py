import json
from pathlib import Path

import pytest

from analysis.market_cycle import credit_change, cycle_rows
from data_store.connection import reset_for_testing
from webui.services import market_macro_service as service
from webui.services.market_cycle_sources import fetch_releases


def archive():
    return json.loads((Path(__file__).resolve().parents[1] / 'config/market_cycle_releases.json').read_text())


def test_cycle_replay_uses_disclosure_not_month_end():
    releases = archive()
    def rows(day):
        return {r['period']: r for r in cycle_rows(releases, day)}
    assert rows('2026-10-26')['2026-09']['finished_inventory'] is None
    assert rows('2026-10-27')['2026-09']['finished_inventory']['value'] == 11.6
    assert rows('2026-10-31')['2026-10']['new_orders']['value'] == 49.4
    assert '2026-10' not in rows('2026-10-30')
    releases['new_orders'] = [r for r in releases['new_orders'] if r['period'] != '2026-08']
    assert rows('2026-10-27')['2026-09']['phase'] == '待披露 / 缺月'


def test_undated_credit_never_enters_replay_and_collection_failure_keeps_archive():
    rows = [{'date': m + '-25', 'value': 100} for m in ('2025-07', '2025-08', '2025-09', '2026-07', '2026-08', '2026-09')]
    assert credit_change(rows, '2026-10-31')['value'] == 0
    assert credit_change(rows, '2026-10-31', replay=True)['value'] is None
    def offline(url):
        raise OSError('offline')
    saved = archive()
    result, failures = fetch_releases(offline, '2026-11-20', saved)
    assert failures and result == saved


def test_report_seeds_sqlite_and_replays_from_database_without_seed_files(tmp_path, monkeypatch):
    monkeypatch.setenv('KRONOS_SQLITE_PATH', str(tmp_path / 'macro.sqlite'))
    reset_for_testing()
    service._snapshot_for.cache_clear()
    try:
        first = service.query()
        assert first['available'] and first['cycle']['rows']
        for key in (service.BASELINE_KEY, service.CYCLE_BASELINE_KEY):
            assert service.kv_repo.get_raw(service.NAMESPACE, key)

        def no_read():
            pytest.fail('Seed files should only be opened while importing their data')

        monkeypatch.setattr(service, 'baseline', no_read)
        monkeypatch.setattr(service, '_load_cycle_baseline', no_read)
        service._snapshot_for.cache_clear()
        replay = service.cycle('csi300', '2026-10-26')
        sept = next(row for row in replay['rows'] if row['period'] == '2026-09')
        assert sept['finished_inventory'] is None
        assert service.query()['snapshot_version'] == replay['snapshot_version'] == first['snapshot_version']
    finally:
        service._snapshot_for.cache_clear()
        reset_for_testing()
