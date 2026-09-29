"""Temporal ranking and executable-exit experiments on a SQLite audit snapshot.

No production scoring configuration is written. Model candidates are trained only
on earlier, matured outcomes. Test results remain research results; the history has
already been inspected, so this is not an untouched prospective test.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.analyze_installed_opportunities import stats


def paired_bootstrap(frame: pd.DataFrame, baseline: str, treatment: str,
                     cost_pct: float = 0.2, seed: int = 20260929) -> dict:
    """Resample five-entry-day blocks, preserving cross-section and overlapping holds."""
    common = frame.dropna(subset=[baseline, treatment]).copy()
    if common.empty:
        return {}
    daily = common.assign(
        base_win=common[baseline] > cost_pct,
        new_win=common[treatment] > cost_pct,
        ret_delta=common[treatment] - common[baseline],
        new_net=common[treatment] - cost_pct,
    ).groupby("entry_date").agg(
        n=(baseline, "size"), base_win=("base_win", "sum"),
        new_win=("new_win", "sum"), ret_delta=("ret_delta", "sum"),
        new_net=("new_net", "sum"),
    )
    values = daily.to_numpy(dtype=float)
    rng = np.random.default_rng(seed)
    block_length = min(5, len(values))
    starts = rng.integers(0, len(values), size=(4000, int(np.ceil(len(values) / block_length))))
    draws = ((starts[:, :, None] + np.arange(block_length)) % len(values)).reshape(4000, -1)[:, :len(values)]
    samples = values[draws].sum(axis=1)
    n = samples[:, 0]
    win_delta = (samples[:, 2] - samples[:, 1]) / n * 100
    mean_delta = samples[:, 3] / n
    net_mean = samples[:, 4] / n
    return {
        "common_n": len(common), "common_days": len(daily),
        "win_delta_pp": float(((common[treatment] > cost_pct).mean() - (common[baseline] > cost_pct).mean()) * 100),
        "win_delta_ci_low": float(np.quantile(win_delta, 0.025)),
        "win_delta_ci_high": float(np.quantile(win_delta, 0.975)),
        "mean_delta_pct": float((common[treatment] - common[baseline]).mean()),
        "mean_delta_ci_low": float(np.quantile(mean_delta, 0.025)),
        "mean_delta_ci_high": float(np.quantile(mean_delta, 0.975)),
        "new_net_mean_ci_low": float(np.quantile(net_mean, 0.025)),
        "new_net_mean_ci_high": float(np.quantile(net_mean, 0.975)),
    }


def selection(frame: pd.DataFrame, key: str, ascending: bool = False) -> pd.DataFrame:
    # Make the selection before looking at future availability or entry fills.
    eligible = frame[frame.degraded == 0].copy()
    selected = eligible.sort_values(
        ["entry_date", key, "code"], ascending=[True, ascending, True], na_position="last"
    ).groupby("entry_date").head(10)
    return selected[~selected.entry_untradeable.astype(bool)]


def ranking_experiments(candidates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    features = [
        "total_score", "s_quantitative", "s_technical", "s_momentum",
        "s_volume_health", "s_liquidity", "s_sector", "s_fundamental",
        "s_dragon_tiger", "rsi", "chase", "day_change", "change_3d",
        "change_5d", "sell_signals",
    ]
    frame = candidates.copy()
    for key in features:
        frame["rank_" + key] = frame.groupby("entry_date")[key].rank(pct=True)
    rank_features = frame[["rank_" + key for key in features]]
    design = pd.concat([
        rank_features, rank_features.isna().astype(float).add_prefix("missing_"),
        pd.get_dummies(frame.source, prefix="source", dtype=float),
    ], axis=1)
    folds = [
        ("July", "2026-06-29", "2026-07-07", "2026-07-31"),
        ("August", "2026-07-24", "2026-08-03", "2026-08-10"),
    ]
    rows, picked = [], []
    for label, train_end, test_start, test_end in folds:
        train_mask = (frame.run_date <= train_end) & frame.recomputed_5d.notna() & (frame.degraded == 0)
        test_mask = (frame.run_date >= test_start) & (frame.run_date <= test_end)
        validation = frame[test_mask].copy()
        model = make_pipeline(
            SimpleImputer(strategy="median", keep_empty_features=True),
            StandardScaler(), LogisticRegression(C=0.1, max_iter=2000),
        )
        weights = 1 / frame.loc[train_mask].groupby("entry_date").entry_date.transform("size")
        model.fit(
            design[train_mask], (frame.loc[train_mask, "recomputed_5d"] > 0.2).astype(int),
            logisticregression__sample_weight=(weights / weights.mean()).to_numpy(),
        )
        validation["probability"] = model.predict_proba(design[test_mask])[:, 1]
        alternatives = {
            "current_rank": ("item_rank", True),
            "quant_rank": ("s_quantitative", False),
            "volume_rank": ("s_volume_health", False),
            "sector_rank": ("s_sector", False),
            "fundamental_rank": ("s_fundamental", False),
            "low_rsi_rank": ("rsi", True),
            "logistic_percentile_rank": ("probability", False),
        }
        for name, (key, ascending) in alternatives.items():
            selected = selection(validation, key, ascending)
            rows.append({
                "fold": label, "policy": name, "train_end": train_end,
                "test_start": test_start, "test_end": test_end,
                **stats(selected, "recomputed_5d"),
            })
            saved = selected[["entry_date", "code", "item_rank", "recomputed_5d", "probability"]].copy()
            saved["policy"], saved["fold"] = name, label
            picked.append(saved)
    return pd.DataFrame(rows), pd.concat(picked, ignore_index=True)


def exit_experiments(recommendations: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    frame = recommendations[
        recommendations.recomputed_5d.notna() & (~recommendations.entry_untradeable.astype(bool))
    ].copy()
    # Same positions and same 5-day-complete price cohort for all exit comparisons.
    train = frame[(frame.report_date < "2026-08-01") & (frame.exit_date_5d < "20260801")]
    validation = frame[frame.report_date >= "2026-08-01"]
    columns = ["recomputed_2d", "recomputed_3d", "recomputed_5d"]
    columns += [key for key in frame if key.startswith("exit_h")]
    rows = []
    for period, sample in (("train_Feb_Jul", train), ("temporal_Aug_Sep", validation), ("all", frame)):
        for column in columns:
            rows.append({
                "period": period, "policy": column, **stats(sample, column),
                **paired_bootstrap(sample, "recomputed_5d", column),
            })
    table = pd.DataFrame(rows)
    eligible = table[
        (table.period == "train_Feb_Jul") & table.policy.str.startswith("exit_")
        & (table.net_mean_pct > 0) & (table.n >= 100)
    ]
    chosen = eligible.sort_values(["net_win_pct", "net_mean_pct"], ascending=False).iloc[0]
    validation_row = table[(table.period == "temporal_Aug_Sep") & (table.policy == chosen.policy)].iloc[0]
    verdict = {
        "objective": "Maximize training net win rate with positive training net mean and at least 100 observations.",
        "chosen_policy": chosen.policy,
        "training_n": int(chosen.n), "training_days": int(chosen.days),
        "training_net_win_pct": float(chosen.net_win_pct), "training_net_mean_pct": float(chosen.net_mean_pct),
        "temporal_n": int(validation_row.n), "temporal_days": int(validation_row.days),
        "temporal_net_win_pct": float(validation_row.net_win_pct),
        "temporal_net_mean_pct": float(validation_row.net_mean_pct),
        "production_gate_passed": bool(validation_row.net_mean_pct > 0 and validation_row.new_net_mean_ci_low > 0),
        "cost_assumption_pct": 0.2,
        "test_scope": "Exploratory temporal validation on available cached OHLC; not a prospective untouched test.",
    }
    return table, verdict


def comparison_chart(snapshot: Path, exits: pd.DataFrame) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    with sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro", uri=True) as audit:
        monthly = pd.read_sql_query("SELECT * FROM monthly_candidates", audit)
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.7), layout="constrained")
    colors = {"pool": "#73859b", "current_top10": "#ca7148"}
    months = sorted(monthly.month.unique())
    for offset, population, label in ((-0.18, "pool", "All candidates"), (0.18, "current_top10", "Current Top 10")):
        subset = monthly[monthly.population == population].set_index("month").reindex(months)
        bars = axes[0].bar(np.arange(len(months)) + offset, subset.win_pct, width=0.34, label=label, color=colors[population])
        axes[0].bar_label(bars, labels=[f"{v:.1f}%" for v in subset.win_pct], padding=3, fontsize=9)
    axes[0].set_xticks(np.arange(len(months)), [month + ("*" if month.endswith("08") else "") for month in months])
    axes[0].set_ylim(0, 90)
    axes[0].set_ylabel("Gross 5-day win rate (%)")
    axes[0].set_title("Ranking performance by month")
    axes[0].legend(frameon=False)
    test = exits[exits.period == "temporal_Aug_Sep"].set_index("policy")
    policies = [
        ("recomputed_5d", "5-day close", "#73859b"),
        ("exit_h5_tp1_sl12", "TP 1% / SL 12%", "#b94f47"),
        ("exit_h5_tp3_sl8", "TP 3% / SL 8%", "#397567"),
    ]
    for policy, label, color in policies:
        point = test.loc[policy]
        axes[1].scatter(point.net_win_pct, point.net_mean_pct, s=85, color=color)
        axes[1].annotate(label, (point.net_win_pct, point.net_mean_pct), xytext=(0, 10), textcoords="offset points", ha="center", fontsize=9)
    axes[1].axhline(0, color="#919191", linewidth=1, linestyle="--")
    axes[1].set_xlim(44, 84)
    axes[1].set_ylim(-0.16, 0.85)
    axes[1].set_xlabel("Net win rate (%)")
    axes[1].set_ylabel("Mean net return per trade (%)")
    axes[1].set_title("Exit comparison: Aug-Sep, 286 trades")
    for axis in axes:
        axis.spines[["top", "right"]].set_visible(False)
        axis.grid(axis="y", alpha=0.15)
        axis.set_axisbelow(True)
    fig.suptitle("Installed SQLite opportunity audit | 2026", fontsize=14)
    fig.text(0.015, -0.035, "* Aug candidate pool ends at Aug 10. Exit chart assumes 0.2% round-trip costs; no position-level compounding.", fontsize=9, color="#666666")
    path = snapshot.parent / "comparison.png"
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=Path("results/installed_audit_20260929/audit_snapshot.sqlite"))
    args = parser.parse_args()
    with sqlite3.connect(args.snapshot.resolve().as_uri() + "?mode=ro", uri=True) as source:
        candidates = pd.read_sql_query("SELECT * FROM candidate_dataset", source)
        recommendations = pd.read_sql_query("SELECT * FROM recommendation_dataset", source)
    rank_rows, picked = ranking_experiments(candidates)
    exits, verdict = exit_experiments(recommendations)
    with sqlite3.connect(args.snapshot) as audit:
        rank_rows.to_sql("ranking_benchmark", audit, if_exists="replace", index=False)
        picked.to_sql("ranking_selections", audit, if_exists="replace", index=False)
        exits.to_sql("exit_benchmark", audit, if_exists="replace", index=False)
        pd.DataFrame([{"key": key, "value": json.dumps(value, ensure_ascii=False)} for key, value in verdict.items()]).to_sql(
            "optimization_verdict", audit, if_exists="replace", index=False,
        )
    print(rank_rows[["fold", "policy", "n", "days", "net_win_pct", "net_mean_pct"]].round(3).to_string(index=False))
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    print(exits[(exits.period == "temporal_Aug_Sep") & exits.policy.isin([
        "recomputed_5d", "recomputed_3d", "exit_h5_tp1_sl12", "exit_h5_tp1_sl8", "exit_h5_tp3_sl8"
    ])][["policy", "n", "days", "win_pct", "net_win_pct", "net_mean_pct", "win_delta_ci_low", "win_delta_ci_high", "new_net_mean_ci_low", "new_net_mean_ci_high"]].round(3).to_string(index=False))
    print("Chart:", comparison_chart(args.snapshot, exits))


if __name__ == "__main__":
    main()
