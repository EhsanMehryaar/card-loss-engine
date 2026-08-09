"""Tests for published scenario ingestion, transformations, and reversion."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.scenarios.paths import (
    SCENARIOS,
    build_scenario_paths,
    extrapolation_summary,
    load_published_scenarios,
)

SOURCE = Path("data/scenarios/fed_2019_supervisory_scenarios.csv")


def _history() -> pd.DataFrame:
    dates = pd.date_range("2018-01-01", "2018-12-01", freq="MS")
    return pd.DataFrame(
        {
            "as_of_month": dates,
            "unemployment_rate": [4.1, 4.1, 4.1, 3.9, 3.9, 3.9, 3.8, 3.8, 3.8, 3.8, 3.8, 3.8],
            "unemployment_change_3m": [0.0] * 12,
            "hpi_change_yoy": [0.05] * 12,
        }
    )


def _paths() -> dict[str, pd.DataFrame]:
    return build_scenario_paths(
        SOURCE,
        vintage_year=2019,
        horizon_months=120,
        horizon_quarters=13,
        half_life_quarters=8,
        long_run_unemployment_rate=4.8,
        long_run_hpi_change_yoy=0.03,
        macro_lags=(1, 3, 6),
        observed_history=_history(),
    )


def test_published_fed_vintage_has_all_three_13_quarter_paths() -> None:
    frame = load_published_scenarios(SOURCE, vintage_year=2019)
    counts = frame[frame["scenario"].isin(SCENARIOS)].groupby("scenario").size()
    assert counts.to_dict() == {scenario: 13 for scenario in SCENARIOS}
    assert frame[frame["scenario"].eq("severely_adverse")]["unemployment_rate"].max() == 10.0


def test_monthly_mapping_and_post_window_reversion() -> None:
    paths = _paths()
    assert set(paths) == set(SCENARIOS)
    for path in paths.values():
        assert len(path) == 120
        assert path["within_published_window"].sum() == 39
        assert not path.filter(regex=r"lag_|unemployment_change_3m").isna().any().any()

    severe = paths["severely_adverse"]
    assert severe.loc[0, "hpi_change_yoy"] == pytest.approx(0.0)
    assert severe["hpi_change_yoy"].min() < -0.16
    published_end = severe.loc[38, "unemployment_rate"]
    assert abs(severe.iloc[-1]["unemployment_rate"] - 4.8) < abs(published_end - 4.8)


def test_extrapolation_fractions_are_measured_on_published_months() -> None:
    summary = extrapolation_summary(_paths(), pre_cutoff_max=7.25, full_history_max=9.86)
    severe = summary.set_index("scenario").loc["severely_adverse"]
    assert severe["published_fraction_above_pre_cutoff_max"] == pytest.approx(33 / 39)
    assert severe["published_fraction_above_full_history_max"] == pytest.approx(6 / 39)
    assert summary.set_index("scenario").loc["adverse", "peak_unemployment_rate"] == 7.0
