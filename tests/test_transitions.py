"""Known-answer tests for empirical and conditional transition matrices."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.model.conditional import (
    DELINQUENT_STATES,
    TRANSIENT_STATES,
    fit_conditional_models,
    transition_fit_sample,
)
from src.model.transitions import empirical_transition_matrix, observed_transition_rows
from src.panel.macro import MACRO_VALUE_COLUMNS


def test_transition_fit_endpoint_is_configurable() -> None:
    counts = pd.DataFrame(
        {
            "as_of_month": pd.to_datetime(["2018-12-01", "2019-01-01"]),
            "transition_count": [10, 20],
        }
    )

    cutoff = transition_fit_sample(counts, "2018-12-01")

    assert cutoff["transition_count"].tolist() == [10]
    assert transition_fit_sample(counts, None)["transition_count"].tolist() == [10, 20]


def _count_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ("Current", "Current", 90),
            ("Current", "DPD30", 8),
            ("Current", "Prepaid", 2),
            ("DPD30", "Current", 50),
            ("DPD30", "DPD30", 30),
            ("DPD30", "DPD60", 20),
        ],
        columns=["delinquency_state", "next_delinquency_state", "transition_count"],
    )


def test_empirical_matrix_is_stochastic_with_absorbing_identity_and_prior(
    quality_config,
) -> None:
    matrix = empirical_transition_matrix(_count_frame(), quality_config)

    assert np.allclose(matrix["row_sum"], 1.0)
    for state in quality_config.states.absorbing:
        assert matrix.loc[state, state] == 1.0
        assert matrix.loc[state].drop(labels=[state, "row_sum"]).eq(0.0).all()
    assert matrix.loc["DPD60", "DPD60"] == 1.0
    assert not matrix.isna().any().any()


def test_censored_rows_are_excluded_from_transition_denominator(spark) -> None:
    panel = spark.sql(
        """
        SELECT * FROM VALUES
          ('Current', 'Current', false),
          ('Current', 'DPD30', false),
          ('Current', 'Current', true),
          ('Current', 'Current', true),
          ('Current', 'Current', true),
          ('Current', 'Current', true)
        AS panel(delinquency_state, next_delinquency_state, is_censored)
        """
    )
    observed = observed_transition_rows(panel).groupBy(
        "delinquency_state", "next_delinquency_state"
    ).count()
    counts = {
        row["next_delinquency_state"]: row["count"] for row in observed.collect()
    }

    assert counts == {"Current": 1, "DPD30": 1}


def _conditional_fixture(config) -> tuple[pd.DataFrame, pd.DataFrame]:
    months = pd.date_range("2010-01-01", periods=5, freq="MS")
    macro_records = []
    count_records = []
    score_bands = ("FICO_620-659", "FICO_700-739")
    for index, (month, unemployment) in enumerate(
        zip(months, range(4, 9), strict=True)
    ):
        macro_row = {
            "as_of_month": month.date(),
            "unemployment_rate": float(unemployment),
            "unemployment_change_3m": 0.0,
            "hpi_change_yoy": 0.0,
        }
        for lag in config.model.macro_lags:
            for column in MACRO_VALUE_COLUMNS:
                macro_row[f"{column}_lag_{lag}"] = macro_row[column]
        macro_records.append(macro_row)
        for mob in (12, 36, 72):
            for score_band in score_bands:
                current_counts = {
                    "Current": 940 - 20 * index,
                    "DPD30": 40 + 20 * index,
                    "Prepaid": 20,
                    "Repurchased": 1,
                }
                for destination, count in current_counts.items():
                    count_records.append(
                        ("Current", destination, mob, score_band, month.date(), count)
                    )
                for state_position, origin in enumerate(DELINQUENT_STATES, start=1):
                    transient_position = TRANSIENT_STATES.index(origin)
                    cure = TRANSIENT_STATES[transient_position - 1]
                    roll = (
                        "ChargeOff"
                        if origin == "DPD150"
                        else TRANSIENT_STATES[transient_position + 1]
                    )
                    outcome_counts = {
                        cure: 560 - 60 * index,
                        origin: 280,
                        roll: 160 + 60 * index,
                        "Prepaid": 2,
                        "Repurchased": 1 if state_position == 1 else 0,
                    }
                    for destination, count in outcome_counts.items():
                        if count:
                            count_records.append(
                                (origin, destination, mob, score_band, month.date(), count)
                            )
    counts = pd.DataFrame(
        count_records,
        columns=[
            "delinquency_state",
            "next_delinquency_state",
            "months_on_book",
            "score_band",
            "as_of_month",
            "transition_count",
        ],
    )
    return counts, pd.DataFrame(macro_records)


def test_conditional_matrices_conserve_mass_and_have_monotone_macro_response(
    quality_config,
) -> None:
    counts, macro = _conditional_fixture(quality_config)
    model = fit_conditional_models(counts, macro, quality_config)
    state_index = {state: index for index, state in enumerate(model.states)}
    for fitted in model.origin_models.values():
        assert fitted.mob_knots == (12, 24, 36, 60)
        assert any(feature.startswith("mob_spline_") for feature in fitted.feature_columns)
        assert "mob_scaled" not in fitted.feature_columns
        assert "mob_squared" not in fitted.feature_columns
        assert "unemployment_rate" not in fitted.feature_columns
        assert any(
            feature.startswith("unemployment_rate") and feature.endswith("excess_4_8")
            for feature in fitted.feature_columns
        )
    assert (
        model.origin_models["DPD150"].regularization_c
        == quality_config.model.conditional_dpd150_regularization_c
    )
    assert all(
        fitted.regularization_c == quality_config.model.conditional_regularization_c
        for origin, fitted in model.origin_models.items()
        if origin != "DPD150"
    )

    for mob in (0, 24, 60, 119):
        for score_band in model.score_bands:
            low = dict(model.macro_defaults)
            high = dict(low)
            for key in high:
                if key == "unemployment_rate" or key.startswith(
                    "unemployment_rate_lag_"
                ):
                    low[key], high[key] = 4.0, 8.0
            low_matrix = model.build_matrix(mob, score_band, low)
            high_matrix = model.build_matrix(mob, score_band, high)
            assert np.allclose(low_matrix.sum(axis=1), 1.0)
            assert np.allclose(high_matrix.sum(axis=1), 1.0)
            for absorbing in model.absorbing:
                row = low_matrix[state_index[absorbing]]
                assert row[state_index[absorbing]] == 1.0
                assert np.count_nonzero(row) == 1
            assert high_matrix[state_index["Current"], state_index["DPD30"]] >= (
                low_matrix[state_index["Current"], state_index["DPD30"]]
            )
            for origin in DELINQUENT_STATES:
                position = TRANSIENT_STATES.index(origin)
                cure = TRANSIENT_STATES[position - 1]
                roll = "ChargeOff" if origin == "DPD150" else TRANSIENT_STATES[position + 1]
                assert high_matrix[state_index[origin], state_index[roll]] >= low_matrix[
                    state_index[origin], state_index[roll]
                ]
                assert high_matrix[state_index[origin], state_index[cure]] <= low_matrix[
                    state_index[origin], state_index[cure]
                ]


def test_conditional_zero_observation_origin_uses_finite_self_prior(
    quality_config,
) -> None:
    counts, macro = _conditional_fixture(quality_config)
    counts = counts[~counts["delinquency_state"].eq("DPD150")]
    model = fit_conditional_models(counts, macro, quality_config)
    matrix = model.build_matrix(36, model.score_bands[0], model.macro_defaults)
    state_index = {state: index for index, state in enumerate(model.states)}
    row = matrix[state_index["DPD150"]]

    assert np.isfinite(row).all()
    assert row[state_index["DPD150"]] > 0.99
    assert np.isclose(row.sum(), 1.0)
