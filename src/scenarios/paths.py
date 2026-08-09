"""Load published supervisory scenarios and extend them to a lifetime horizon.

The input contract is provider-neutral: replacing the configured CSV with a
later Federal Reserve vintage requires no code change when it retains the same
five documented fields. Published quarterly averages are repeated over their
three months. HPI levels become year-over-year changes, and the model's
three-month unemployment change is calculated after monthly expansion.

Beyond the published window, unemployment and HPI growth exponentially revert
to configured long-run means. The configured half-life is the number of
quarters required to close half the remaining gap; it is an engine assumption,
not part of the Federal Reserve scenario.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

SCENARIOS = ("baseline", "adverse", "severely_adverse")
REQUIRED_COLUMNS = {
    "vintage_year",
    "scenario",
    "quarter",
    "unemployment_rate",
    "house_price_index",
}


def load_published_scenarios(path: str | Path, *, vintage_year: int) -> pd.DataFrame:
    """Read and validate one vintage of quarterly supervisory-scenario data."""

    frame = pd.read_csv(path)
    missing = REQUIRED_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"Scenario CSV is missing columns: {sorted(missing)}")
    frame = frame[frame["vintage_year"].eq(vintage_year)].copy()
    if frame.empty:
        raise ValueError(f"Scenario CSV has no rows for vintage {vintage_year}")
    frame["quarter_period"] = pd.PeriodIndex(frame["quarter"], freq="Q")
    for scenario in SCENARIOS:
        rows = frame[frame["scenario"].eq(scenario)].sort_values("quarter_period")
        if rows.empty:
            raise ValueError(f"Scenario CSV has no {scenario!r} path")
        if rows["quarter_period"].duplicated().any():
            raise ValueError(f"Scenario CSV contains duplicate {scenario!r} quarters")
    return frame.sort_values(["scenario", "quarter_period"]).reset_index(drop=True)


def _quarterly_path(frame: pd.DataFrame, scenario: str) -> pd.DataFrame:
    history = frame[frame["scenario"].eq("historical")].copy()
    path = frame[frame["scenario"].eq(scenario)].copy()
    combined = pd.concat([history, path], ignore_index=True).sort_values("quarter_period")
    combined["hpi_change_yoy"] = combined["house_price_index"].pct_change(4)
    path = combined[combined["scenario"].eq(scenario)].copy()
    records: list[dict[str, object]] = []
    for row in path.itertuples(index=False):
        first_month = row.quarter_period.asfreq("M", "start")
        for month in pd.period_range(first_month, periods=3, freq="M"):
            records.append(
                {
                    "as_of_month": month.to_timestamp(),
                    "published_quarter": str(row.quarter_period),
                    "unemployment_rate": float(row.unemployment_rate),
                    "house_price_index": float(row.house_price_index),
                    "hpi_change_yoy": float(row.hpi_change_yoy),
                    "within_published_window": True,
                }
            )
    return pd.DataFrame.from_records(records)


def _extend_with_reversion(
    published: pd.DataFrame,
    horizon_months: int,
    *,
    half_life_quarters: int,
    long_run_unemployment_rate: float,
    long_run_hpi_change_yoy: float,
) -> pd.DataFrame:
    if horizon_months < 1 or half_life_quarters < 1:
        raise ValueError("Scenario horizon and reversion half-life must be positive")
    if len(published) >= horizon_months:
        return published.head(horizon_months).copy()
    last = published.iloc[-1]
    extension: list[dict[str, object]] = []
    for step in range(1, horizon_months - len(published) + 1):
        weight = 0.5 ** (step / (3.0 * half_life_quarters))
        extension.append(
            {
                "as_of_month": pd.Timestamp(last["as_of_month"]) + pd.offsets.MonthBegin(step),
                "published_quarter": pd.NA,
                "unemployment_rate": long_run_unemployment_rate
                + (float(last["unemployment_rate"]) - long_run_unemployment_rate) * weight,
                "house_price_index": np.nan,
                "hpi_change_yoy": long_run_hpi_change_yoy
                + (float(last["hpi_change_yoy"]) - long_run_hpi_change_yoy) * weight,
                "within_published_window": False,
            }
        )
    return pd.concat([published, pd.DataFrame.from_records(extension)], ignore_index=True)


def build_scenario_paths(
    source_csv: str | Path,
    *,
    vintage_year: int,
    horizon_months: int,
    horizon_quarters: int,
    half_life_quarters: int,
    long_run_unemployment_rate: float,
    long_run_hpi_change_yoy: float,
    macro_lags: tuple[int, ...],
    observed_history: pd.DataFrame | None = None,
) -> dict[str, pd.DataFrame]:
    """Return model-ready monthly paths for all three supervisory scenarios."""

    source = load_published_scenarios(source_csv, vintage_year=vintage_year)
    result: dict[str, pd.DataFrame] = {}
    for scenario in SCENARIOS:
        published = _quarterly_path(source, scenario)
        if len(published) != horizon_quarters * 3:
            raise ValueError(
                f"{scenario!r} contains {len(published) // 3} quarters; "
                f"expected {horizon_quarters}"
            )
        path = _extend_with_reversion(
            published,
            horizon_months,
            half_life_quarters=half_life_quarters,
            long_run_unemployment_rate=long_run_unemployment_rate,
            long_run_hpi_change_yoy=long_run_hpi_change_yoy,
        )
        if observed_history is not None:
            bridge = observed_history[
                observed_history["as_of_month"] < path["as_of_month"].min()
            ].copy()
            needed = max(max(macro_lags, default=0), 3) + max(macro_lags, default=0)
            bridge = bridge.tail(needed)
            combined = pd.concat([bridge, path], ignore_index=True, sort=False)
        else:
            combined = path.copy()
        combined = combined.sort_values("as_of_month").reset_index(drop=True)
        combined["unemployment_change_3m"] = combined["unemployment_rate"].diff(3)
        for lag in macro_lags:
            for column in ("unemployment_rate", "unemployment_change_3m", "hpi_change_yoy"):
                combined[f"{column}_lag_{lag}"] = combined[column].shift(lag)
        path_columns = list(path.columns)
        lag_columns = [
            f"{column}_lag_{lag}"
            for lag in macro_lags
            for column in ("unemployment_rate", "unemployment_change_3m", "hpi_change_yoy")
        ]
        ready = combined[combined["as_of_month"].isin(path["as_of_month"])][
            [*path_columns, "unemployment_change_3m", *lag_columns]
        ].reset_index(drop=True)
        required_model = [
            "unemployment_rate",
            "unemployment_change_3m",
            "hpi_change_yoy",
            *lag_columns,
        ]
        if ready[required_model].isna().any().any():
            missing = ready.columns[ready.isna().any()].tolist()
            raise ValueError(
                "Scenario path lacks observed history needed for model lags: "
                f"{missing}"
            )
        result[scenario] = ready
    return result


def extrapolation_summary(
    paths: dict[str, pd.DataFrame],
    *,
    pre_cutoff_max: float,
    full_history_max: float,
) -> pd.DataFrame:
    """Measure how much of each published and lifetime path exceeds fit ranges."""

    records: list[dict[str, object]] = []
    for scenario, path in paths.items():
        published = path[path["within_published_window"]]
        records.append(
            {
                "scenario": scenario,
                "published_months": len(published),
                "published_fraction_above_pre_cutoff_max": float(
                    published["unemployment_rate"].gt(pre_cutoff_max).mean()
                ),
                "published_fraction_above_full_history_max": float(
                    published["unemployment_rate"].gt(full_history_max).mean()
                ),
                "lifetime_fraction_above_pre_cutoff_max": float(
                    path["unemployment_rate"].gt(pre_cutoff_max).mean()
                ),
                "lifetime_fraction_above_full_history_max": float(
                    path["unemployment_rate"].gt(full_history_max).mean()
                ),
                "peak_unemployment_rate": float(path["unemployment_rate"].max()),
                "pre_cutoff_max": pre_cutoff_max,
                "full_history_max": full_history_max,
            }
        )
    return pd.DataFrame.from_records(records)
