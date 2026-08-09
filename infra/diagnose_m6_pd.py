"""Diagnose M6 transition forecasts against realized 2019-2021 transitions."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import EngineConfig, load_config
from src.model.cecl import PORTFOLIO_LABEL, _macro_with_lags, _transition_counts
from src.model.conditional import (
    DELINQUENT_STATES,
    TRANSIENT_STATES,
    _design_frame,
    fit_conditional_models,
)


def _weighted_quantile(values: pd.Series, weights: pd.Series, quantile: float) -> float:
    order = np.argsort(values.to_numpy(dtype=float))
    sorted_values = values.to_numpy(dtype=float)[order]
    sorted_weights = weights.to_numpy(dtype=float)[order]
    cumulative = np.cumsum(sorted_weights) - 0.5 * sorted_weights
    cumulative /= sorted_weights.sum()
    return float(np.interp(quantile, cumulative, sorted_values))


def _distribution_record(
    sample: str,
    frame: pd.DataFrame,
    *,
    weight_column: str | None,
    pre_cutoff_max: float,
) -> dict[str, object]:
    values = frame["unemployment_rate"].astype(float)
    weights = (
        frame[weight_column].astype(float)
        if weight_column is not None
        else pd.Series(np.ones(len(frame)), index=frame.index)
    )
    return {
        "portfolio_scope": PORTFOLIO_LABEL,
        "sample": sample,
        "observations": len(frame),
        "total_weight": float(weights.sum()),
        "minimum": float(values.min()),
        "p05": _weighted_quantile(values, weights, 0.05),
        "p25": _weighted_quantile(values, weights, 0.25),
        "median": _weighted_quantile(values, weights, 0.50),
        "p75": _weighted_quantile(values, weights, 0.75),
        "p95": _weighted_quantile(values, weights, 0.95),
        "p99": _weighted_quantile(values, weights, 0.99),
        "maximum": float(values.max()),
        "pct_weight_above_pre_cutoff_max": float(
            weights[values > pre_cutoff_max].sum() / weights.sum()
        ),
    }


def macro_distribution(
    counts: pd.DataFrame,
    macro: pd.DataFrame,
    cutoff: pd.Timestamp,
) -> pd.DataFrame:
    """Summarize calendar-month and transition-weighted unemployment ranges."""

    monthly = macro[["as_of_month", "unemployment_rate"]].drop_duplicates("as_of_month")
    monthly["as_of_month"] = pd.to_datetime(monthly["as_of_month"])
    pre_monthly = monthly[monthly["as_of_month"] <= cutoff]
    post_monthly = monthly[
        monthly["as_of_month"].between(pd.Timestamp("2019-01-01"), pd.Timestamp("2021-12-01"))
    ]
    pre_max = float(pre_monthly["unemployment_rate"].max())
    weighted = counts.merge(monthly, on="as_of_month", how="left", validate="many_to_one")
    pre_weighted = weighted[pd.to_datetime(weighted["as_of_month"]) <= cutoff]
    post_weighted = weighted[
        pd.to_datetime(weighted["as_of_month"]).between(
            pd.Timestamp("2019-01-01"), pd.Timestamp("2021-12-01")
        )
    ]
    return pd.DataFrame.from_records(
        [
            _distribution_record(
                "intended_pre_cutoff_calendar_months",
                pre_monthly,
                weight_column=None,
                pre_cutoff_max=pre_max,
            ),
            _distribution_record(
                "intended_pre_cutoff_transition_weighted",
                pre_weighted,
                weight_column="transition_count",
                pre_cutoff_max=pre_max,
            ),
            _distribution_record(
                "post_cutoff_2019_2021_calendar_months",
                post_monthly,
                weight_column=None,
                pre_cutoff_max=pre_max,
            ),
            _distribution_record(
                "post_cutoff_2019_2021_transition_weighted",
                post_weighted,
                weight_column="transition_count",
                pre_cutoff_max=pre_max,
            ),
            _distribution_record(
                "current_full_history_fit_transition_weighted",
                weighted,
                weight_column="transition_count",
                pre_cutoff_max=pre_max,
            ),
        ]
    )


def _predicted_transition_counts(model, contexts: pd.DataFrame) -> pd.DataFrame:
    records: list[pd.DataFrame] = []
    for origin in TRANSIENT_STATES:
        frame = contexts[contexts["delinquency_state"].eq(origin)].copy()
        if frame.empty:
            continue
        origin_model = model.origin_models[origin]
        if origin_model.estimator is None:
            probabilities = pd.DataFrame(
                {
                    destination: np.full(len(frame), probability)
                    for destination, probability in origin_model.prior.items()
                },
                index=frame.index,
            )
        else:
            design = _design_frame(
                frame,
                origin_model.macro_columns,
                origin_model.mob_knots,
                origin_model.mob_lower_bound,
                origin_model.mob_upper_bound,
            ).reindex(columns=origin_model.feature_columns)
            probabilities = pd.DataFrame(
                origin_model.estimator.predict_proba(design),
                columns=origin_model.estimator.classes_,
                index=frame.index,
            )
        prepay = model.delinquent_prepay_hazard if origin in DELINQUENT_STATES else 0.0
        remaining = 1.0 - model.repurchase_hazard - prepay
        probabilities *= remaining
        probabilities["Repurchased"] = model.repurchase_hazard
        if origin in DELINQUENT_STATES:
            probabilities["Prepaid"] = prepay
        for destination in probabilities.columns:
            records.append(
                pd.DataFrame(
                    {
                        "period": frame["period"].to_numpy(),
                        "origin": origin,
                        "destination": str(destination),
                        "expected_count": (
                            probabilities[destination].to_numpy(dtype=float)
                            * frame["origin_count"].to_numpy(dtype=float)
                        ),
                    }
                )
            )
    return pd.concat(records, ignore_index=True)


def transition_backtest(
    counts: pd.DataFrame,
    macro: pd.DataFrame,
    config: EngineConfig,
    cutoff: pd.Timestamp,
) -> pd.DataFrame:
    """Compare current and cutoff-clean fits with realized 2019-2021 transitions."""

    evaluation = counts[
        pd.to_datetime(counts["as_of_month"]).between(
            pd.Timestamp("2019-01-01"), pd.Timestamp("2021-12-01")
        )
    ].copy()
    evaluation["period"] = pd.to_datetime(evaluation["as_of_month"]).dt.year.astype(str)
    context_keys = [
        "period",
        "delinquency_state",
        "months_on_book",
        "score_band",
        "as_of_month",
    ]
    contexts = evaluation.groupby(context_keys, observed=True, as_index=False).agg(
        origin_count=("transition_count", "sum")
    )
    contexts = contexts.merge(macro, on="as_of_month", how="left", validate="many_to_one")
    realized = evaluation.groupby(
        ["period", "delinquency_state", "next_delinquency_state"],
        observed=True,
        as_index=False,
    ).agg(actual_count=("transition_count", "sum"))
    realized_total = realized.groupby(
        ["delinquency_state", "next_delinquency_state"], observed=True, as_index=False
    ).agg(actual_count=("actual_count", "sum"))
    realized_total.insert(0, "period", "2019-2021")
    realized = pd.concat([realized, realized_total], ignore_index=True)
    origin_totals = realized.groupby(["period", "delinquency_state"])["actual_count"].transform(
        "sum"
    )
    realized["actual_probability"] = realized["actual_count"] / origin_totals
    models = {
        "current_full_history_fit": fit_conditional_models(counts, macro, config),
        "cutoff_clean_fit": fit_conditional_models(
            counts[pd.to_datetime(counts["as_of_month"]) <= cutoff], macro, config
        ),
    }
    outputs: list[pd.DataFrame] = []
    for sample, model in models.items():
        expected_detail = _predicted_transition_counts(model, contexts)
        expected = expected_detail.groupby(["period", "origin", "destination"], as_index=False).agg(
            expected_count=("expected_count", "sum")
        )
        expected_total = expected_detail.groupby(["origin", "destination"], as_index=False).agg(
            expected_count=("expected_count", "sum")
        )
        expected_total.insert(0, "period", "2019-2021")
        expected = pd.concat([expected, expected_total], ignore_index=True)
        expected["fitted_probability"] = expected["expected_count"] / expected.groupby(
            ["period", "origin"]
        )["expected_count"].transform("sum")
        comparison = expected.merge(
            realized,
            left_on=["period", "origin", "destination"],
            right_on=["period", "delinquency_state", "next_delinquency_state"],
            how="outer",
        )
        comparison = comparison.fillna(0.0)
        comparison["fit_sample"] = sample
        comparison["difference_bps"] = 10_000.0 * (
            comparison["fitted_probability"] - comparison["actual_probability"]
        )
        outputs.append(
            comparison[
                [
                    "fit_sample",
                    "period",
                    "origin",
                    "destination",
                    "actual_count",
                    "expected_count",
                    "actual_probability",
                    "fitted_probability",
                    "difference_bps",
                ]
            ]
        )
    result = pd.concat(outputs, ignore_index=True)
    result.insert(0, "portfolio_scope", PORTFOLIO_LABEL)
    return result.sort_values(["fit_sample", "period", "origin", "destination"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="local")
    parser.add_argument("--config-dir", default="config")
    args = parser.parse_args()
    config = load_config(args.env, Path(args.config_dir))
    panel_path = Path(config.paths.output) / "synthetic_panel.parquet"
    panel = pd.read_parquet(panel_path)
    macro = pd.read_csv(config.paths.macro, parse_dates=["as_of_month"])
    macro_lagged = _macro_with_lags(macro, config.model.macro_lags)
    counts = _transition_counts(panel, config)
    cutoff = pd.Timestamp(config.model.vintage_analysis_as_of)
    macro_distribution(counts, macro_lagged, cutoff).to_csv(
        "docs/m6_unemployment_range_diagnostic.csv", index=False
    )
    transition_backtest(counts, macro_lagged, config, cutoff).to_csv(
        "docs/m6_transition_backtest_2019_2021.csv", index=False
    )


if __name__ == "__main__":
    main()
