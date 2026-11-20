import json
from pathlib import Path
from types import SimpleNamespace

from webui.services import model_runtime
from webui.services.model_runtime import ModelRuntimeContext, load_model_payload, run_prediction_payload


def _context(**overrides):
    defaults = {
        "model_available": lambda: False,
        "get_predictor": lambda: None,
        "set_loaded_model": lambda tokenizer, model, predictor: None,
        "available_models": {
            "kronos-small": {
                "name": "Kronos-small",
                "params": "24.7M",
                "context_length": 512,
                "description": "Small model",
            }
        },
        "user_root": Path("/tmp/kronos-test"),
        "kronos_cls": None,
        "tokenizer_cls": None,
        "predictor_cls": None,
        "pandas": None,
        "load_data_file": lambda _path: (None, "not called"),
        "create_prediction_chart": lambda *_args, **_kwargs: "{}",
        "save_prediction_results": lambda *_args, **_kwargs: None,
    }
    defaults.update(overrides)
    return ModelRuntimeContext(**defaults)


def test_load_model_short_circuits_when_model_library_disabled():
    result, status = load_model_payload({}, _context(model_available=lambda: False))

    assert status == 400
    assert result == {"error": "Kronos model library not available"}


def test_prediction_validates_file_path_before_loading_data():
    calls = []

    result, status = run_prediction_payload(
        {},
        _context(load_data_file=lambda path: calls.append(path)),
    )

    assert status == 400
    assert result == {"error": "File path cannot be empty"}
    assert calls == []


def test_prediction_short_circuits_when_predictor_not_loaded():
    class FakeFrame:
        columns = ["timestamps", "open", "high", "low", "close"]

        def __len__(self):
            return 10

    result, status = run_prediction_payload(
        {"file_path": "sample.csv", "lookback": 5},
        _context(
            model_available=lambda: True,
            get_predictor=lambda: None,
            load_data_file=lambda _path: (FakeFrame(), None),
        ),
    )

    assert status == 400
    assert result == {"error": "Kronos model not loaded, please load model first"}


def test_load_model_rejects_unsupported_model_before_importing_modelscope():
    result, status = load_model_payload(
        {"model_key": "unknown"},
        _context(
            model_available=lambda: True,
            kronos_cls=SimpleNamespace(),
            tokenizer_cls=SimpleNamespace(),
            predictor_cls=SimpleNamespace(),
        ),
    )

    assert status == 400
    assert result == {"error": "Unsupported model: unknown"}


def test_resolve_device_auto_uses_supported_device_name():
    assert model_runtime._resolve_device("auto") in {"cpu", "mps", "cuda:0"}


def test_model_cache_name_uses_repository_leaf():
    assert model_runtime._model_cache_name("northwind9898/Kronos-Tokenizer-2k") == "Kronos-Tokenizer-2k"


def test_loaded_model_info_uses_predictor_device_without_model_parameters():
    predictor = SimpleNamespace(model=SimpleNamespace(), device="cpu")

    assert model_runtime.loaded_model_info(predictor) == {"name": "SimpleNamespace", "device": "cpu"}


def test_prediction_replay_uses_recorded_times_and_separates_the_model_from_actual_prices():
    import pandas as pd

    timestamps = ['2026-01-09 11:25:00', '2026-01-09 11:30:00', '2026-01-09 13:05:00', '2026-01-12 09:35:00']
    frame = pd.DataFrame({
        'timestamps': pd.to_datetime(timestamps), 'open': [1, 2, 3, 4],
        'high': [2, 3, 4, 5], 'low': [1, 2, 3, 4], 'close': [1.5, 2.5, 3.5, 4.5],
        'volume': [0, None, 100, 200], 'amount': [None, None, 150, 250],
    })
    saves = []

    def predict(**kwargs):
        assert list(kwargs['df'].columns) == ['open', 'high', 'low', 'close']
        assert kwargs['df']['close'].tolist() == [1.5, 2.5]
        assert kwargs['x_timestamp'].tolist() == list(frame['timestamps'][:2])
        assert kwargs['y_timestamp'].tolist() == list(frame['timestamps'][2:])
        return pd.DataFrame({'open': [2, 2], 'high': [3, 3], 'low': [1, 1], 'close': [2.8, 2.6], 'volume': [0, 0]})

    ctx = _context(pandas=pd, model_available=lambda: True, load_data_file=lambda _: (frame, None),
                   get_predictor=lambda: SimpleNamespace(predict=predict),
                   create_prediction_chart=model_runtime.create_prediction_chart,
                   save_prediction_results=lambda **kwargs: saves.append(kwargs))
    request = {'file_path': 'sqlite:ohlcv:600000:5m', 'lookback': 2, 'pred_len': 2}
    response, status = run_prediction_payload(dict(request, start_date=timestamps[0]), ctx)

    assert status == 200
    chart = json.loads(response['chart'])
    expected = [stamp.replace(' ', 'T') for stamp in timestamps[2:]]
    assert chart['layout']['meta']['period'] == 'intraday'
    assert chart['layout']['meta']['history']['as_of'] == timestamps[1]
    assert chart['data'][1]['y'] == [0, None]
    assert chart['data'][2]['x'] == chart['data'][4]['x'] == expected
    assert [bar['timestamp'] for bar in saves[0]['prediction_results']] == expected
    assert [bar['timestamp'] for bar in response['actual_data']] == expected
    assert [bar['volume'] for bar in response['prediction_results']] == [None, None]

    invalid, status = run_prediction_payload(dict(request, start_date='2026-01-09 11:26:00'), ctx)
    assert status == 400
    assert '所选起点' in invalid['error']
