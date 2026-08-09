"""Milestone 7 supervisory-scenario allowance runner."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import fsspec
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.config import EngineConfig
from src.model.cecl import (
    PORTFOLIO_LABEL,
    ECLResult,
    _load_transition_counts,
    _macro_with_lags,
    _write_csv,
    active_cutoff_panel,
    assign_score_band,
    build_ecl_segments,
    calculate_lifetime_ecl,
)
from src.model.conditional import fit_conditional_models, transition_fit_sample
from src.model.forecast import CachedMatrixBuilder
from src.model.lgd import fit_lgd_model
from src.scenarios.paths import SCENARIOS, build_scenario_paths, extrapolation_summary


@dataclass(frozen=True)
class Milestone7Report:
    """Serializable scenario results and artifact locations."""

    baseline_ecl: float
    adverse_ecl: float
    severely_adverse_ecl: float
    adverse_delta_pct: float
    severely_adverse_delta_pct: float
    transition_attribution: str
    summary_path: str
    monthly_path: str
    macro_paths_path: str
    plot_path: str
    transition_attribution_path: str
    extrapolation_path: str


def _model_path(path: pd.DataFrame) -> pd.DataFrame:
    return path.drop(
        columns=["published_quarter", "house_price_index", "within_published_window"],
        errors="ignore",
    )


def _transition_flow_attribution(
    model: object,
    segments: list[object],
    baseline: pd.DataFrame,
    severe: pd.DataFrame,
    *,
    max_mob: int,
) -> pd.DataFrame:
    """Compare exposure-weighted expected state flows under severe and baseline."""

    states = tuple(model.states)  # type: ignore[attr-defined]
    absorbing = {"ChargeOff", "Prepaid", "Repurchased"}
    totals: dict[tuple[str, str], list[float]] = {
        (origin, destination): [0.0, 0.0]
        for origin in states
        if origin not in absorbing
        for destination in states
        if destination != origin
    }
    for scenario_index, path in enumerate((baseline, severe)):
        for segment in segments:
            vector = np.array(
                [segment.initial_state.get(state, 0.0) for state in states], dtype=float
            )
            for offset, row in path.reset_index(drop=True).iterrows():
                macro = {
                    str(column): float(value)
                    for column, value in row.items()
                    if column != "as_of_month" and pd.notna(value)
                }
                matrix = model.build_matrix(  # type: ignore[attr-defined]
                    min(segment.start_mob + offset, max_mob), segment.score_band, macro
                )
                for origin_index, origin in enumerate(states):
                    if origin in absorbing:
                        continue
                    for destination_index, destination in enumerate(states):
                        if destination == origin:
                            continue
                        totals[(origin, destination)][scenario_index] += float(
                            segment.balance
                            * vector[origin_index]
                            * matrix[origin_index, destination_index]
                        )
                vector = vector @ matrix
    records = []
    for (origin, destination), (base_flow, severe_flow) in totals.items():
        records.append(
            {
                "origin_state": origin,
                "destination_state": destination,
                "baseline_expected_balance_flow": base_flow,
                "severely_adverse_expected_balance_flow": severe_flow,
                "increase_in_expected_balance_flow": severe_flow - base_flow,
            }
        )
    return pd.DataFrame.from_records(records).sort_values(
        "increase_in_expected_balance_flow", ascending=False
    )


def plot_scenario_losses(monthly: pd.DataFrame, path: str | Path) -> None:
    """Plot all three cumulative discounted loss curves with portfolio scope."""

    figure, axis = plt.subplots(figsize=(10, 5.5))
    labels = {
        "baseline": "Baseline",
        "adverse": "Adverse",
        "severely_adverse": "Severely adverse",
    }
    for scenario in SCENARIOS:
        frame = monthly[monthly["scenario"].eq(scenario)]
        axis.plot(
            frame["month"],
            frame["cumulative_discounted_loss"],
            linewidth=2,
            label=labels[scenario],
        )
    axis.set_xlabel("Forecast month")
    axis.set_ylabel("Cumulative discounted expected loss ($)")
    axis.set_title(
        "Lifetime ECL under 2019 Federal Reserve scenarios\n"
        f"{PORTFOLIO_LABEL} — production full-history fit"
    )
    axis.legend()
    axis.grid(alpha=0.2)
    figure.tight_layout()
    destination = str(path)
    if "://" in destination:
        buffer = BytesIO()
        figure.savefig(buffer, format="png", dpi=160)
        buffer.seek(0)
        with fsspec.open(destination, "wb") as stream:
            stream.write(buffer.getvalue())
    else:
        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(output, dpi=160)
    plt.close(figure)


def _scenario_summary(results: dict[str, ECLResult]) -> pd.DataFrame:
    baseline = results["baseline"].lifetime_ecl
    records = []
    for scenario in SCENARIOS:
        result = results[scenario]
        delta = result.lifetime_ecl - baseline
        records.append(
            {
                "scenario": scenario,
                "outstanding_balance": result.outstanding_balance,
                "lifetime_ecl": result.lifetime_ecl,
                "ecl_rate": result.ecl_rate,
                "delta_vs_baseline_dollars": delta,
                "delta_vs_baseline_pct": delta / baseline if baseline else 0.0,
                "undiscounted_lifetime_loss": float(result.monthly["undiscounted_loss"].sum()),
            }
        )
    return pd.DataFrame.from_records(records)


def run_scenarios(config: EngineConfig) -> Milestone7Report:
    """Run the three published Fed paths using production full-history fits."""

    output_root = Path(config.paths.output)
    panel = pd.read_parquet(output_root / "synthetic_panel.parquet")
    acquisition = pd.read_csv(
        Path(config.paths.raw_acquisition) / "synthetic_acquisition.txt",
        sep=config.ingest.delimiter,
        parse_dates=["origination_month"],
    )
    macro = pd.read_csv(config.paths.macro, parse_dates=["as_of_month"])
    macro_lagged = _macro_with_lags(macro, config.model.macro_lags)

    # Production allowance fit: all available outcomes. Cutoff-clean estimation
    # is intentionally reserved for the out-of-sample M6 backtest.
    counts = transition_fit_sample(
        _load_transition_counts(config, panel), config.model.production_fit_end
    )
    fitted_transition_model = fit_conditional_models(counts, macro_lagged, config)
    transition_model = CachedMatrixBuilder(fitted_transition_model)
    panel["score_band"] = assign_score_band(panel["orig_score"], config.model.score_bands)
    lgd_model = fit_lgd_model(
        panel,
        fallback_lgd=config.model.fallback_lgd,
        score_bands=fitted_transition_model.score_bands,
    )
    cutoff = pd.Timestamp(config.model.vintage_analysis_as_of)
    active_panel = active_cutoff_panel(panel, cutoff)
    segments = build_ecl_segments(active_panel, acquisition, config, cutoff)

    paths = build_scenario_paths(
        config.paths.scenario_source,
        vintage_year=config.scenarios.source_vintage,
        horizon_months=config.synthetic.max_observation_months,
        horizon_quarters=config.scenarios.horizon_quarters,
        half_life_quarters=config.scenarios.reversion_half_life_quarters,
        long_run_unemployment_rate=config.scenarios.long_run_unemployment_rate,
        long_run_hpi_change_yoy=config.scenarios.long_run_hpi_change_yoy,
        macro_lags=config.model.macro_lags,
        observed_history=macro,
    )
    results = {
        scenario: calculate_lifetime_ecl(
            transition_model,
            lgd_model,
            segments,
            _model_path(paths[scenario]),
            annual_discount_rate=config.model.discount_rate_annual,
            max_mob=config.model.vintage_maturity_mob,
        )
        for scenario in SCENARIOS
    }
    summary = _scenario_summary(results)
    summary.insert(0, "fit_end", config.model.production_fit_end or "full_available_history")
    summary.insert(0, "fit_provenance", "production_full_history")
    monthly = pd.concat(
        [results[scenario].monthly.assign(scenario=scenario) for scenario in SCENARIOS],
        ignore_index=True,
    )
    monthly.insert(0, "fit_end", config.model.production_fit_end or "full_available_history")
    monthly.insert(0, "fit_provenance", "production_full_history")
    path_table = pd.concat(
        [paths[scenario].assign(scenario=scenario) for scenario in SCENARIOS],
        ignore_index=True,
    )
    path_table.insert(0, "fit_end", config.model.production_fit_end or "full_available_history")
    path_table.insert(0, "fit_provenance", "production_full_history")
    extrapolation = extrapolation_summary(
        paths,
        pre_cutoff_max=config.scenarios.pre_cutoff_unemployment_max,
        full_history_max=config.scenarios.full_history_unemployment_max,
    )
    extrapolation.insert(0, "fit_end", config.model.production_fit_end or "full_available_history")
    extrapolation.insert(0, "fit_provenance", "production_full_history")
    attribution = _transition_flow_attribution(
        transition_model,
        segments,
        _model_path(paths["baseline"]),
        _model_path(paths["severely_adverse"]),
        max_mob=config.model.vintage_maturity_mob,
    )
    attribution.insert(0, "fit_end", config.model.production_fit_end or "full_available_history")
    attribution.insert(0, "fit_provenance", "production_full_history")
    leading = attribution.iloc[0]
    attribution_sentence = (
        f"{leading['origin_state']}→{leading['destination_state']} has the largest "
        "severely-adverse increase in cumulative expected balance flow "
        f"(${leading['increase_in_expected_balance_flow']:,.0f} versus baseline)."
    )
    _write_csv(summary, config.paths.scenario_summary)
    _write_csv(monthly, config.paths.scenario_monthly)
    _write_csv(path_table, config.paths.scenario_paths)
    _write_csv(attribution, config.paths.scenario_transition_attribution)
    _write_csv(extrapolation, config.paths.scenario_extrapolation)
    plot_scenario_losses(monthly, config.paths.scenario_plot)

    indexed = summary.set_index("scenario")
    return Milestone7Report(
        baseline_ecl=float(indexed.loc["baseline", "lifetime_ecl"]),
        adverse_ecl=float(indexed.loc["adverse", "lifetime_ecl"]),
        severely_adverse_ecl=float(indexed.loc["severely_adverse", "lifetime_ecl"]),
        adverse_delta_pct=float(indexed.loc["adverse", "delta_vs_baseline_pct"]),
        severely_adverse_delta_pct=float(
            indexed.loc["severely_adverse", "delta_vs_baseline_pct"]
        ),
        transition_attribution=attribution_sentence,
        summary_path=config.paths.scenario_summary,
        monthly_path=config.paths.scenario_monthly,
        macro_paths_path=config.paths.scenario_paths,
        plot_path=config.paths.scenario_plot,
        transition_attribution_path=config.paths.scenario_transition_attribution,
        extrapolation_path=config.paths.scenario_extrapolation,
    )
