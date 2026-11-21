"""Framework-neutral Kronos model loading and prediction helpers."""

from __future__ import annotations

import json
import math
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from analysis.operation_models import analyze_operations


@dataclass
class ModelRuntimeContext:
    model_available: Callable[[], bool]
    get_predictor: Callable[[], Any]
    set_loaded_model: Callable[[Any, Any, Any], None]
    available_models: dict[str, dict[str, Any]]
    user_root: Path
    kronos_cls: Any
    tokenizer_cls: Any
    predictor_cls: Any
    pandas: Any
    load_data_file: Callable[..., tuple[Any, str | None]]
    create_prediction_chart: Callable[..., str]
    save_prediction_results: Callable[..., str | None]


def _model_cache_name(model_id: str) -> str:
    return str(model_id or "").rsplit("/", 1)[-1]


def _resolve_device(requested_device: Any) -> str:
    device = str(requested_device or "cpu").strip().lower()
    if device != "auto":
        return device

    try:
        import torch

        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda:0"
    except Exception:
        pass
    return "cpu"


def loaded_model_info(predictor: Any) -> dict[str, str]:
    model = getattr(predictor, "model", None)
    name = model.__class__.__name__ if model is not None else predictor.__class__.__name__
    device = str(getattr(predictor, "device", "") or "")
    if not device and model is not None:
        try:
            parameters = model.parameters()
            device = str(next(parameters).device)
        except Exception:
            device = "unknown"
    return {"name": name, "device": device or "unknown"}


def _load_modelscope_artifact(
    *,
    model_id: str,
    cache_dir: Path,
    expected_dir: Path,
    loader_cls: Any,
    label: str,
) -> Any:
    from modelscope import snapshot_download

    if expected_dir.exists() and (expected_dir / "config.json").exists():
        print(f"Found local {label}, loading...")
        return loader_cls.from_pretrained(str(expected_dir))

    print(f"Downloading {label}...")
    downloaded_path = Path(snapshot_download(model_id, cache_dir=str(cache_dir)))
    if downloaded_path.resolve() != expected_dir.resolve():
        if not expected_dir.exists():
            shutil.move(str(downloaded_path), str(expected_dir))
            return loader_cls.from_pretrained(str(expected_dir))
        return loader_cls.from_pretrained(str(downloaded_path))
    return loader_cls.from_pretrained(str(downloaded_path))


def create_prediction_chart(history, predicted, actual):
    """Plotly-compatible traces; each series keeps its own observed or model clock."""
    traces = []
    for key, label, rows in [('history', '输入历史', history), ('prediction', '模型预测', predicted), ('actual', '同期实测', actual)]:
        stamps = [r['timestamp'] for r in rows]
        traces.append({'type': 'candlestick', 'meta': key, 'name': f'{label} · {len(rows)} 根', 'x': stamps,
                       **{field: [r[field] for r in rows] for field in ('open', 'high', 'low', 'close')},
                       'customdata': [r['amount'] for r in rows]})
        traces.append({'type': 'bar', 'meta': key, 'name': f'{label}量', 'x': stamps, 'yaxis': 'y2',
                       'y': [r['volume'] for r in rows]})

    daily = all(r['timestamp'][11:19] == '00:00:00' for r in history + actual)
    period = 'daily' if daily else 'intraday'
    def structure(observed):
        bars = [dict(r, date=r['timestamp'][:10] if daily else r['timestamp'].replace('T', ' ')) for r in observed]
        return analyze_operations(bars, period=period)

    # No held-out prices, waves or stage labels may enter the model path.
    layout = {'title': f'历史 {len(history)} 根 · 预测 {len(predicted)} 根 · 实测 {len(actual)} 根',
              'xaxis': {'type': 'date', 'rangeslider': {'visible': False}}, 'yaxis': {'domain': [.28, 1]},
              'yaxis2': {'domain': [0, .21], 'title': '量'}, 'barmode': 'group',
              'meta': {'period': period, 'history': structure(history), 'actual': structure(history + actual)}}
    return json.dumps({'data': traces, 'layout': layout}, ensure_ascii=False, allow_nan=False)


def run_prediction_payload(data: dict[str, Any], context: ModelRuntimeContext) -> tuple[dict[str, Any], int]:
    try:
        pd = context.pandas
        predictor = context.get_predictor()
        file_path = data.get("file_path")
        lookback = int(data.get("lookback", 400))
        pred_len = int(data.get("pred_len", 120))
        temperature = float(data.get("temperature", 0.6))
        top_p = float(data.get("top_p", 0.9))
        sample_count = int(data.get("sample_count", 10))

        if not file_path:
            return {"error": "File path cannot be empty"}, 400
        if lookback < 1 or pred_len < 1:
            return {"error": "Lookback and prediction length must be positive"}, 400

        bounds = {key: data[key] for key in ('start', 'end') if data.get(key)}
        df, error = context.load_data_file(file_path, **bounds)
        if error:
            return {"error": error}, 400
        if len(df) < lookback:
            return {"error": f"Insufficient data length, need at least {lookback} rows"}, 400
        if not context.model_available() or predictor is None:
            return {"error": "Kronos model not loaded, please load model first"}, 400

        start_date = data.get("start_date")
        window = df
        if start_date:
            try:
                start = pd.Timestamp(start_date)
                window = df[df['timestamps'] >= start]
            except (TypeError, ValueError):
                return {"error": "Invalid prediction start timestamp"}, 400
            if window.empty or window['timestamps'].iloc[0] != start:
                return {"error": "所选起点已不在当前行情序列，请重新载入行情"}, 400
        if len(window) < lookback + pred_len:
            return {"error": f"Insufficient data: need {lookback + pred_len} rows, have {len(window)}"}, 400

        history = window.iloc[:lookback]
        actual = window.iloc[lookback:lookback + pred_len]
        x_timestamp, y_timestamp = history['timestamps'], actual['timestamps']
        columns = ['open', 'high', 'low', 'close']
        has_volume = 'volume' in history and history['volume'].notna().all()
        if has_volume:
            columns.append('volume')
            if 'amount' in history and history['amount'].notna().all():
                columns.append('amount')
        x_df = history[columns].copy()
        prediction_type = f"历史回放：{lookback} 根输入，{pred_len} 根预测与实测对照"

        try:
            pred_df = predictor.predict(
                df=x_df,
                x_timestamp=x_timestamp,
                y_timestamp=y_timestamp,
                pred_len=pred_len,
                T=temperature,
                top_p=top_p,
                sample_count=sample_count,
            )
        except Exception as exc:
            return {"error": f"Kronos model prediction failed: {exc}"}, 500

        if len(pred_df) != len(y_timestamp):
            return {"error": "Model output length differs from the prediction window"}, 500

        def records(frame, stamps, *, model=False):
            rows = []
            for bar, stamp in zip(frame.to_dict('records'), stamps):
                row = {k: float(bar[k]) for k in ('open', 'high', 'low', 'close')}
                if not all(math.isfinite(v) for v in row.values()):
                    raise ValueError('Non-finite OHLC in prediction window')
                for k in ('volume', 'amount'):
                    v = bar.get(k)
                    row[k] = float(v) if (not model or k in columns) and pd.notna(v) and math.isfinite(v) and v >= 0 else None
                rows.append({'timestamp': stamp.isoformat(), **row})
            return rows

        actual_data = records(actual, actual['timestamps'])
        prediction_results = records(pred_df, y_timestamp, model=True)
        historical_data = records(history, x_timestamp)
        chart_json = context.create_prediction_chart(historical_data, prediction_results, actual_data)

        try:
            context.save_prediction_results(
                file_path=file_path,
                prediction_type=prediction_type,
                prediction_results=prediction_results,
                actual_data=actual_data,
                input_data=x_df,
                historical_data=historical_data,
                prediction_params={
                    "lookback": lookback,
                    "pred_len": pred_len,
                    "temperature": temperature,
                    "top_p": top_p,
                    "sample_count": sample_count,
                    "start_date": x_timestamp.iloc[0].isoformat(),
                },
            )
        except Exception as exc:
            print(f"Failed to save prediction results: {exc}")

        return {
            "success": True,
            "prediction_type": prediction_type,
            "chart": chart_json,
            "prediction_results": prediction_results,
            "actual_data": actual_data,
            "has_comparison": len(actual_data) > 0,
            "message": f"Prediction completed, generated {pred_len} prediction points"
            + (f", including {len(actual_data)} actual data points for comparison" if actual_data else ""),
        }, 200
    except Exception as exc:
        return {"error": f"Prediction failed: {exc}"}, 500


def load_model_payload(data: dict[str, Any], context: ModelRuntimeContext) -> tuple[dict[str, Any], int]:
    try:
        if not context.model_available():
            return {"error": "Kronos model library not available"}, 400

        model_key = data.get("model_key", "kronos-small")
        device = _resolve_device(data.get("device", "cpu"))
        if model_key not in context.available_models:
            return {"error": f"Unsupported model: {model_key}"}, 400

        model_config = context.available_models[model_key]
        model_dir = context.user_root / "models"
        model_dir.mkdir(exist_ok=True)
        model_model_id = model_config.get("model_id") or f"northwind9898/{model_config['name']}"
        tokenizer_model_id = model_config.get("tokenizer_id") or "northwind9898/Kronos-Tokenizer-base"
        tokenizer_dir = model_dir / _model_cache_name(tokenizer_model_id)
        model_dir_path = model_dir / _model_cache_name(model_model_id)

        tokenizer = _load_modelscope_artifact(
            model_id=tokenizer_model_id,
            cache_dir=model_dir,
            expected_dir=tokenizer_dir,
            loader_cls=context.tokenizer_cls,
            label="tokenizer",
        )
        model = _load_modelscope_artifact(
            model_id=model_model_id,
            cache_dir=model_dir,
            expected_dir=model_dir_path,
            loader_cls=context.kronos_cls,
            label="model",
        )

        predictor = context.predictor_cls(
            model,
            tokenizer,
            device=device,
            max_context=model_config["context_length"],
        )
        context.set_loaded_model(tokenizer, model, predictor)

        return {
            "success": True,
            "message": f"Model loaded successfully: {model_config['name']} ({model_config['params']}) on {device}",
            "model_info": {
                "name": model_config["name"],
                "params": model_config["params"],
                "context_length": model_config["context_length"],
                "description": model_config["description"],
            },
        }, 200
    except Exception as exc:
        return {"error": f"Model loading failed: {exc}"}, 500
