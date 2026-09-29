"""Learn bounded factor score adjustments from installed SQLite history.

The historical final score is the anchor: advanced analysis and other existing
information remain in it. Dimension transfers are changes to every dynamic
template, propagated through the existing 70% base / 30% advanced blend. They
are a first-order score sensitivity, not an exact replay of historical caps and
bonuses (the database does not preserve that intermediate state).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DIMENSIONS = {
    "quantitative": "s_quantitative", "technical": "s_technical",
    "position_timing": "s_momentum", "volume_health": "s_volume_health",
    "liquidity": "s_liquidity", "sector": "s_sector",
    "fundamental": "s_fundamental", "dragon_tiger": "s_dragon_tiger",
}
# Minimum across all four templates; bounded transfers cannot make weights negative.
MIN_WEIGHTS = dict(zip(DIMENSIONS, (.18, .08, .20, .18, .04, .04, .04, .04)))
BINS = {
    "s_quantitative": [-np.inf, 50, 70, 85, 95, np.inf],
    "s_technical": [-np.inf, 30, 50, 70, np.inf],
    "s_momentum": [-np.inf, 30, 50, 70, 90, np.inf],
    "s_volume_health": [-np.inf, 50, 75, 95, np.inf],
    "s_sector": [-np.inf, 55, 60, 75, 95, np.inf],
    "rsi": [-np.inf, 40, 50, 60, 70, 80, np.inf],
    "chase": [-np.inf, 20, 40, 60, 80, np.inf],
    "sell_signals": [-np.inf, 1, 3, 5, np.inf],
    "day_change": [-np.inf, 0, 3, 7, 9.5, np.inf],
    "change_3d": [-np.inf, 0, 5, 12, 18, np.inf],
    "change_5d": [-np.inf, 0, 5, 15, 25, np.inf],
}
COST = .2
BLEND = .7
PAIRS = [("s_quantitative", "rsi"), ("s_technical", "s_momentum"),
         ("s_sector", "change_3d"), ("chase", "day_change"),
         ("s_quantitative", "sell_signals"), ("rsi", "chase")]


def eligible(frame: pd.DataFrame) -> pd.DataFrame:
    # Zero scores often mean buy<sell caused early return before other factors
    # were computed. Do not resurrect incomplete records by reweighting zeros.
    return frame[(frame.degraded == 0) & (frame.total_score > 0) & frame.rsi.notna()].copy()


def select(frame: pd.DataFrame, scores: np.ndarray) -> pd.DataFrame:
    scored = frame.assign(adjusted_score=np.clip(scores, 0, 100))
    picked = scored.sort_values(
        ["entry_date", "adjusted_score", "item_rank", "code"],
        ascending=[True, False, True, True], kind="stable",
    ).groupby("entry_date", sort=False).head(10)
    # Selection precedes future fill and outcome availability; never backfill.
    return picked


def metrics(picked: pd.DataFrame) -> dict:
    valid = picked[(picked.entry_untradeable == 0) & picked.recomputed_5d.notna()].copy()
    if valid.empty:
        return {"n": 0, "selected": len(picked), "coverage_pct": 0}
    valid["win"] = valid.recomputed_5d > COST
    daily = valid.groupby("entry_date").agg(win=("win", "mean"), ret=("recomputed_5d", "mean"))
    blocks = [daily.win.iloc[index].mean() for index in np.array_split(np.arange(len(daily)), min(3, len(daily))) if len(index)]
    return {
        "n": len(valid), "selected": len(picked), "days": len(daily),
        "coverage_pct": valid.shape[0] / picked.shape[0] * 100,
        "gross_win_pct": float((valid.recomputed_5d > 0).mean() * 100),
        "net_win_pct": float(valid.win.mean() * 100),
        "daily_net_win_pct": float(daily.win.mean() * 100),
        "net_mean_pct": float(valid.recomputed_5d.mean() - COST),
        "block_std": float(np.std(blocks)),
    }


def objective(result: dict, complexity: float = 0) -> float:
    if result["n"] < 20 or result.get("coverage_pct", 0) < 90:
        return -np.inf
    # Aim at win rate, equal-weight decision days; discourage unstable timing
    # and unnecessarily large score changes. Return is a guard and tie-breaker.
    if result["net_mean_pct"] <= 0:
        return -np.inf
    return result["daily_net_win_pct"] / 100 - .2 * result["block_std"] - .002 * complexity


def linear_score(frame: pd.DataFrame, delta: dict) -> np.ndarray:
    score = frame.total_score.to_numpy(dtype=float).copy()
    for dimension, change in delta.items():
        score += BLEND * change * (frame[DIMENSIONS[dimension]].fillna(50).to_numpy() - 50)
    return score


def fit_transfers(train: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    """Greedy bounded pair transfers; each template's weight sum stays unchanged."""
    delta = dict.fromkeys(DIMENSIONS, 0.)
    evaluated = []
    for step in range(3):
        proposals = [("keep", delta.copy())]
        for source in DIMENSIONS:
            for target in DIMENSIONS:
                if target == source:
                    continue
                for amount in (.025, .05, .10):
                    candidate = delta.copy()
                    candidate[source] -= amount
                    candidate[target] += amount
                    if any(candidate[k] < -MIN_WEIGHTS[k] + .01 - 1e-9 or abs(candidate[k]) > .15 + 1e-9 for k in candidate):
                        continue
                    if sum(abs(v) for v in candidate.values()) > .40 + 1e-9:
                        continue
                    proposals.append((f"{source}->{target}:{amount}", candidate))
        winner = None
        for name, candidate in proposals:
            result = metrics(select(train, linear_score(train, candidate)))
            complexity = sum(abs(v) for v in candidate.values()) * 10
            value = objective(result, complexity)
            evaluated.append({"step": step, "move": name, "delta_json": json.dumps(candidate), "objective": value, **result})
            tie = (value, result.get("net_mean_pct", -np.inf), -complexity)
            if winner is None or tie > winner[0]:
                winner = (tie, candidate, name)
        if winner[2] == "keep":
            break
        delta = winner[1]
    return delta, pd.DataFrame(evaluated)


def bin_indices(values: pd.Series, edges: list[float]) -> np.ndarray:
    indices = np.searchsorted(np.array(edges[1:-1]), values.to_numpy(dtype=float), side="right")
    indices[values.isna().to_numpy()] = -1
    return indices


def fit_bin_points(train: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    """Within-day excess win rates, shrunk toward zero by 12 decision days.

    A 10 percentage-point excess before shrinkage maps to 3 final score points.
    Each day contributes one unit of evidence per factor bin regardless of the
    number of stocks; unsupported bins get no points.
    """
    labeled = train[(train.entry_untradeable == 0) & train.recomputed_5d.notna()].copy()
    labeled["win"] = (labeled.recomputed_5d > COST).astype(float)
    labeled["day_win"] = labeled.groupby("entry_date").win.transform("mean")
    learned, rows = {}, []
    for factor, edges in BINS.items():
        labeled["bucket"] = bin_indices(labeled[factor], edges)
        daily = labeled[labeled.bucket >= 0].groupby(["entry_date", "bucket"]).agg(
            bin_win=("win", "mean"), day_win=("day_win", "first"), n=("win", "size"),
        ).reset_index()
        points = [0.] * (len(edges) - 1)
        for bucket in range(len(points)):
            sample = daily[daily.bucket == bucket]
            support = len(sample)
            excess = float((sample.bin_win - sample.day_win).mean()) if support else 0.
            shrunk = excess * support / (support + 12)
            points[bucket] = float(np.clip(30 * shrunk, -6, 6)) if support >= 8 and sample.n.sum() >= 100 else 0.
            rows.append({
                "factor": factor, "bucket": bucket,
                "lower": edges[bucket], "upper": edges[bucket + 1],
                "n": int(sample.n.sum()), "days": support,
                "within_day_excess_win_pp": excess * 100,
                "score_points": points[bucket],
            })
        learned[factor] = points
    return learned, pd.DataFrame(rows)


def points_score(frame: pd.DataFrame, learned: dict, factors: list[str], scale: float = 1.) -> np.ndarray:
    result = np.zeros(len(frame))
    for factor in factors:
        buckets = bin_indices(frame[factor], BINS[factor])
        valid = buckets >= 0
        result[valid] += np.array(learned[factor])[buckets[valid]] * scale
    return np.clip(result, -10, 10)


def fit_interactions(train: pd.DataFrame, learned: dict) -> tuple[dict, pd.DataFrame]:
    """Shrunk cell effects after subtracting the two individual factor effects."""
    sample = train[(train.entry_untradeable == 0) & train.recomputed_5d.notna()].copy()
    sample["win"] = (sample.recomputed_5d > COST).astype(float)
    sample["day_win"] = sample.groupby("entry_date").win.transform("mean")
    cells, rows = {}, []
    for first, second in PAIRS:
        name = first + "__" + second
        a, b = bin_indices(sample[first], BINS[first]), bin_indices(sample[second], BINS[second])
        count_b = len(BINS[second]) - 1
        sample["cell"] = np.where((a >= 0) & (b >= 0), a * count_b + b, -1)
        main_points = points_score(sample, learned, [first, second])
        sample["residual"] = sample.win - sample.day_win - main_points / 30
        daily = sample[sample.cell >= 0].groupby(["entry_date", "cell"]).agg(
            residual=("residual", "mean"), n=("win", "size"),
        ).reset_index()
        points = [0.] * ((len(BINS[first]) - 1) * count_b)
        for cell in range(len(points)):
            group = daily[daily.cell == cell]
            support, n = len(group), int(group.n.sum())
            excess = float(group.residual.mean()) if support else 0.
            if support >= 8 and n >= 100:
                points[cell] = float(np.clip(30 * excess * support / (support + 12), -3, 3))
            rows.append({"pair": name, "cell": cell, "first_bucket": cell // count_b,
                         "second_bucket": cell % count_b, "days": support, "n": n,
                         "residual_excess_win_pp": excess * 100, "score_points": points[cell]})
        cells[name] = points
    return cells, pd.DataFrame(rows)


def interaction_score(frame: pd.DataFrame, model: dict) -> np.ndarray:
    points = np.zeros(len(frame))
    for name in model.get("pairs", []):
        first, second = name.split("__")
        a, b = bin_indices(frame[first], BINS[first]), bin_indices(frame[second], BINS[second])
        valid = (a >= 0) & (b >= 0)
        cell = a[valid] * (len(BINS[second]) - 1) + b[valid]
        points[valid] += np.array(model["interaction_points"][name])[cell] * model["interaction_scale"]
    return np.clip(points, -5, 5)


def fit_model(train: pd.DataFrame, family: str) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    learned, bins = fit_bin_points(train)
    if family in ("weight_transfer", "joint"):
        delta, searches = fit_transfers(train)
    else:
        delta, searches = dict.fromkeys(DIMENSIONS, 0.), pd.DataFrame()
    configuration = {"weight_delta": delta, "bin_points": learned, "factors": [], "scale": 1.}
    score = linear_score(train, delta)
    if family in ("bin_points", "joint", "interaction_points"):
        # Fixed candidate families; do not choose them using outer outcomes.
        factor_sets = [[], list(BINS), ["s_quantitative", "rsi", "chase", "s_sector"],
                       ["s_quantitative", "s_technical", "s_sector"], ["rsi", "chase"]]
        factor_sets += [[factor] for factor in BINS]
        choices = []
        for factors in factor_sets:
            for scale in (.5, 1., 1.5):
                result = metrics(select(train, score + points_score(train, learned, factors, scale)))
                complexity = len(factors) * scale * .25
                choices.append((objective(result, complexity), result.get("net_mean_pct", -np.inf), -complexity, factors, scale))
        best = max(choices, key=lambda x: x[:3])
        configuration.update(factors=best[3], scale=best[4])
    if family == "interaction_points":
        cells, _ = fit_interactions(train, learned)
        configuration.update(interaction_points=cells, pairs=[], interaction_scale=1.)
        score += points_score(train, learned, configuration["factors"], configuration["scale"])
        choices = []
        pair_sets = [[], list(cells)] + [[name] for name in cells]
        for pairs in pair_sets:
            for scale in (.5, 1., 1.5):
                candidate = dict(configuration, pairs=pairs, interaction_scale=scale)
                result = metrics(select(train, score + interaction_score(train, candidate)))
                complexity = len(pairs) * scale * .5
                choices.append((objective(result, complexity), result.get("net_mean_pct", -np.inf), -complexity, pairs, scale))
        best = max(choices, key=lambda x: x[:3])
        configuration.update(pairs=best[3], interaction_scale=best[4])
    return configuration, searches, bins


def apply_model(frame: pd.DataFrame, model: dict) -> np.ndarray:
    return linear_score(frame, model["weight_delta"]) + points_score(
        frame, model["bin_points"], model["factors"], model["scale"],
    ) + interaction_score(frame, model)


def daily_comparison(baseline: pd.DataFrame, treatment: pd.DataFrame, seed: int = 29) -> dict:
    def aggregate(frame):
        valid = frame[(frame.entry_untradeable == 0) & frame.recomputed_5d.notna()]
        return valid.assign(win=(valid.recomputed_5d > COST).astype(float)).groupby("entry_date").agg(
            win=("win", "mean"), ret=("recomputed_5d", "mean"),
        )
    joined = aggregate(baseline).join(aggregate(treatment), lsuffix="_base", rsuffix="_new").dropna()
    delta = (joined.win_new - joined.win_base).to_numpy()
    if len(delta) == 0:
        return {}
    rng = np.random.default_rng(seed)
    block = min(5, len(delta))
    starts = rng.integers(len(delta), size=(4000, int(np.ceil(len(delta) / block))))
    draws = ((starts[..., None] + np.arange(block)) % len(delta)).reshape(4000, -1)[:, :len(delta)]
    samples = delta[draws].mean(axis=1) * 100
    return {"daily_win_delta_pp": delta.mean() * 100,
            "daily_delta_ci_low": float(np.quantile(samples, .025)),
            "daily_delta_ci_high": float(np.quantile(samples, .975))}


def diagnostics(candidates: pd.DataFrame) -> pd.DataFrame:
    rows = []
    frame = eligible(candidates)
    frame = frame[frame.recomputed_5d.notna()]
    for month, group in frame.groupby("month"):
        for factor in list(DIMENSIONS.values()) + ["rsi", "chase", "change_3d", "change_5d", "sell_signals"]:
            daily = [day[factor].corr(day.recomputed_5d, method="spearman")
                     for _, day in group.groupby("entry_date") if day[factor].nunique() > 1]
            rows.append({"month": month, "factor": factor, "n": len(group),
                         "days": len(daily), "mean_daily_rank_ic": float(np.nanmean(daily)) if daily else None})
    return pd.DataFrame(rows)


def plot_factor_comparison(output: Path, results: pd.DataFrame, frozen: dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import TwoSlopeNorm

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8), layout="constrained")
    folds = ["July", "August", "combined"]
    for offset, family, label, color in [(-.18, "current", "Current scoring", "#73859b"),
                                        (.18, "frozen_interaction", "Factor calibration", "#397567")]:
        values = results[results.family == family].set_index("fold").reindex(folds).net_win_pct
        bars = axes[0].bar(np.arange(3) + offset, values, width=.34, label=label, color=color)
        axes[0].bar_label(bars, fmt="%.2f%%", padding=3, fontsize=9)
    axes[0].set_xticks(np.arange(3), ["July", "Early August", "Combined"])
    axes[0].set_ylim(0, 80)
    axes[0].set_ylabel("Net win rate (%)")
    axes[0].set_title("Same frozen factor scoring; same exit rule")
    axes[0].legend(frameon=False)
    name = "s_sector__change_3d"
    matrix = np.array(frozen["interaction_points"][name]).reshape(5, 5)
    axes[1].imshow(matrix, cmap="RdYlGn", norm=TwoSlopeNorm(vcenter=0, vmin=-1.5, vmax=1.5), aspect="auto")
    axes[1].set_xticks(np.arange(5), ["<0", "0-5", "5-12", "12-18", ">=18"])
    axes[1].set_yticks(np.arange(5), ["<55", "55-60", "60-75", "75-95", ">=95"])
    axes[1].set_xlabel("Past 3-day price change (%)")
    axes[1].set_ylabel("Sector score")
    axes[1].set_title("Sector x momentum: added score points")
    for i in range(5):
        for j in range(5):
            axes[1].text(j, i, f"{matrix[i,j]:+.2f}" if matrix[i,j] else "0", ha="center", va="center", fontsize=9)
    axes[0].spines[["top", "right"]].set_visible(False)
    axes[0].grid(axis="y", alpha=.15)
    axes[0].set_axisbelow(True)
    fig.suptitle("SQLite factor-score optimization | 168 observed trades, 17 decision days", fontsize=13)
    fig.text(.015, -.035, "Exploratory temporal checks; 0.2% assumed costs. Zero cell: no adjustment. Frozen parameters use only earlier June outcomes.", fontsize=8, color="#666666")
    fig.savefig(output / "factor_comparison.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def run(snapshot: Path, output: Path) -> dict:
    with sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro", uri=True) as source:
        candidates = pd.read_sql_query("SELECT * FROM candidate_dataset", source)
    candidates = eligible(candidates)
    folds = [("July", "2026-07-07", "2026-07-31"), ("August", "2026-08-03", "2026-08-10")]
    results, picked, configs, all_searches, all_bins, all_interactions = [], [], [], [], [], []
    for fold, start, end in folds:
        train = candidates[(candidates.run_date < start) & (candidates.exit_date_5d < start.replace("-", ""))]
        train = train[train.recomputed_5d.notna()]
        test = candidates[candidates.run_date.between(start, end)]
        base_picks = select(test, test.total_score.to_numpy())
        for family in ("current", "weight_transfer", "bin_points", "joint", "interaction_points"):
            if family == "current":
                model = {"weight_delta": dict.fromkeys(DIMENSIONS, 0.), "bin_points": {}, "factors": [], "scale": 1.}
                searches, bins = pd.DataFrame(), pd.DataFrame()
            else:
                model, searches, bins = fit_model(train, family)
            chosen = select(test, apply_model(test, model))
            result = {"fold": fold, "family": family, "train_days": train.entry_date.nunique(),
                      "train_latest_exit": train.exit_date_5d.max(), "test_start": start, "test_end": end,
                      **metrics(chosen), **daily_comparison(base_picks, chosen)}
            results.append(result)
            chosen = chosen.assign(fold=fold, family=family)
            picked.append(chosen[["fold", "family", "entry_date", "code", "item_rank", "total_score", "adjusted_score", "recomputed_5d", "entry_untradeable"]])
            configs.append({"fold": fold, "family": family, "configuration_json": json.dumps(model)})
            if not searches.empty:
                all_searches.append(searches.assign(fold=fold, family=family))
            if not bins.empty:
                all_bins.append(bins.assign(fold=fold, family=family))
            if family == "interaction_points":
                _, interactions = fit_interactions(train, model["bin_points"])
                all_interactions.append(interactions.assign(fold=fold, family=family))
    results = pd.DataFrame(results)
    picked = pd.concat(picked, ignore_index=True)
    for family, group in picked.groupby("family"):
        results = pd.concat([results, pd.DataFrame([{
            "fold": "combined", "family": family, **metrics(group),
            **daily_comparison(picked[picked.family == "current"], group),
        }])], ignore_index=True)
    # Additional robustness check: freeze the June-trained interaction model
    # rather than updating it with July labels before the August window.
    frozen = json.loads(next(config["configuration_json"] for config in configs
                             if config["fold"] == "July" and config["family"] == "interaction_points"))
    frozen_picks = []
    for fold, start, end in folds:
        test = candidates[candidates.run_date.between(start, end)]
        chosen = select(test, apply_model(test, frozen)).assign(fold=fold, family="frozen_interaction")
        base = picked[(picked.fold == fold) & (picked.family == "current")]
        results = pd.concat([results, pd.DataFrame([{
            "fold": fold, "family": "frozen_interaction", **metrics(chosen), **daily_comparison(base, chosen),
        }])], ignore_index=True)
        frozen_picks.append(chosen[["fold", "family", "entry_date", "code", "item_rank", "total_score", "adjusted_score", "recomputed_5d", "entry_untradeable"]])
    frozen_picks = pd.concat(frozen_picks, ignore_index=True)
    results = pd.concat([results, pd.DataFrame([{
        "fold": "combined", "family": "frozen_interaction", **metrics(frozen_picks),
        **daily_comparison(picked[picked.family == "current"], frozen_picks),
    }])], ignore_index=True)
    picked = pd.concat([picked, frozen_picks], ignore_index=True)
    output.mkdir(parents=True, exist_ok=True)
    candidate = {
        "status": "exploratory_research_candidate", "anchor": "historical_final_selection_score",
        "latest_training_exit": "20260706", "fixed_exit_horizon": 5,
        "cost_assumption_pct": COST, "factor_bin_internal_boundaries": {k: edges[1:-1] for k, edges in BINS.items()},
        "main_score_adjustment_cap": 10, "interaction_score_adjustment_cap": 5,
        "model": frozen,
    }
    (output / "candidate_factor_score_points.json").write_text(
        json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    with sqlite3.connect(output / "factor_optimization.sqlite") as audit:
        results.to_sql("factor_validation", audit, if_exists="replace", index=False)
        picked.to_sql("factor_selections", audit, if_exists="replace", index=False)
        pd.DataFrame(configs).to_sql("factor_configurations", audit, if_exists="replace", index=False)
        pd.concat(all_searches, ignore_index=True).to_sql("weight_search", audit, if_exists="replace", index=False)
        pd.concat(all_bins, ignore_index=True).to_sql("factor_bin_evidence", audit, if_exists="replace", index=False)
        pd.concat(all_interactions, ignore_index=True).to_sql("factor_interaction_evidence", audit, if_exists="replace", index=False)
        diagnostics(candidates).to_sql("factor_ic", audit, if_exists="replace", index=False)
    print(results.round(3).to_string(index=False))
    for config in configs:
        if config["family"] != "current":
            model = json.loads(config["configuration_json"])
            print(config["fold"], config["family"], model["weight_delta"], model["factors"], model["scale"])
            if model.get("pairs"):
                print("  interactions", model["pairs"], model["interaction_scale"])
    plot_factor_comparison(output, results, frozen)
    return {"database": str(output / "factor_optimization.sqlite"), "results": results.to_dict("records")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=Path("results/installed_audit_20260929/audit_snapshot.sqlite"))
    parser.add_argument("--output", type=Path, default=Path("results/installed_audit_20260929"))
    args = parser.parse_args()
    run(args.snapshot, args.output)


if __name__ == "__main__":
    main()
