import numpy as np
import pandas as pd

from scripts.optimize_factor_scores import (
    BINS, DIMENSIONS, apply_model, bin_indices, eligible, fit_bin_points,
    interaction_score, linear_score, select,
)


def candidates(n=12):
    data = {
        "code": [str(i).zfill(6) for i in range(n)], "entry_date": ["20260708"] * n,
        "item_rank": np.arange(1, n + 1), "total_score": np.arange(90, 90 - n, -1),
        "degraded": [0] * n, "rsi": [55.] * n, "entry_untradeable": [0] * n,
        "recomputed_5d": [1.] * n,
    }
    for column in set(DIMENSIONS.values()).union(BINS):
        data.setdefault(column, [50.] * n)
    return pd.DataFrame(data)


def test_selection_does_not_use_future_labels_or_replace_unfilled_positions():
    frame = candidates()
    frame.loc[0, "recomputed_5d"] = np.nan
    frame.loc[1, "entry_untradeable"] = 1
    selected = select(frame, frame.total_score.to_numpy())
    assert selected.code.tolist() == frame.code.iloc[:10].tolist()
    assert len(selected) == 10


def test_incomplete_early_returns_are_not_resurrected():
    frame = candidates(3)
    frame.loc[0, "total_score"] = 0
    frame.loc[1, "rsi"] = np.nan
    assert eligible(frame).code.tolist() == ["000002"]


def test_dimension_transfer_preserves_common_offset_and_uses_blend():
    frame = candidates(2)
    frame["s_quantitative"] = [100., 70.]
    frame["s_sector"] = [60., 70.]
    score = linear_score(frame, {"quantitative": -.1, "sector": .1})
    np.testing.assert_allclose(score - frame.total_score, [-2.8, 0.])


def test_exact_bin_boundaries_and_missing_factor():
    indices = bin_indices(pd.Series([49.9, 50., 69.9, 70., np.nan]), BINS["s_quantitative"])
    assert indices.tolist() == [0, 1, 1, 2, -1]


def test_rare_factor_bins_get_zero_adjustment():
    frame = candidates(12)
    frame["entry_date"] = [f"202607{i:02d}" for i in range(1, 13)]
    learned, evidence = fit_bin_points(frame)
    assert all(value == 0 for values in learned.values() for value in values)
    assert evidence.score_points.eq(0).all()


def test_missing_factors_do_not_trigger_calibration_points():
    frame = candidates(1)
    frame["rsi"] = np.nan
    model = {"weight_delta": {}, "bin_points": {"rsi": [3.] * 6}, "factors": ["rsi"], "scale": 1.}
    np.testing.assert_allclose(apply_model(frame, model), frame.total_score)


def test_pair_points_use_joint_cell_and_ignore_missing_inputs():
    frame = candidates(2)
    frame["s_quantitative"] = [50., np.nan]
    frame["rsi"] = [55., 55.]
    values = [0.] * 30
    values[8] = 2.
    model = {"pairs": ["s_quantitative__rsi"], "interaction_points": {"s_quantitative__rsi": values}, "interaction_scale": .5}
    np.testing.assert_allclose(interaction_score(frame, model), [1., 0.])


def test_model_predictions_do_not_depend_on_validation_outcomes():
    frame = candidates(2)
    model = {"weight_delta": {"sector": .1, "quantitative": -.1},
             "bin_points": {"rsi": [1.] * 6}, "factors": ["rsi"], "scale": 1.}
    before = apply_model(frame, model)
    frame["recomputed_5d"] = [-30., 80.]
    np.testing.assert_allclose(apply_model(frame, model), before)
