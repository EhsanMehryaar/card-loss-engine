"""Spark transition aggregation and empirical monthly state matrices."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import fsspec
import numpy as np
import pandas as pd
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from src.config import EngineConfig
from src.ingest.raw_to_parquet import SparkUiSummary, spark_ui_summary
from src.panel.macro import MACRO_VALUE_COLUMNS

TRANSITION_KEYS = (
    "delinquency_state",
    "next_delinquency_state",
    "months_on_book",
    "score_band",
    "as_of_month",
)


@dataclass(frozen=True)
class TransitionAggregationReport:
    aggregated_rows: int
    observed_transitions: int
    excluded_censored_rows: int
    spark_ui: SparkUiSummary


@dataclass(frozen=True)
class TransitionModelReport:
    aggregation: TransitionAggregationReport
    empirical_matrix: dict[str, dict[str, float]]
    selected_macro_timing: dict[str, tuple[str, ...]]
    delinquent_prepay_hazard: float
    repurchase_hazard: float
    benign_matrix: dict[str, dict[str, float]]
    stressed_matrix: dict[str, dict[str, float]]
    material_macro_interpretations: tuple[dict[str, object], ...]
    ground_truth_recovery: tuple[dict[str, object], ...]
    empirical_table_path: str
    coefficient_table_path: str
    ground_truth_table_path: str
    interpretation_table_path: str


def aggregate_transition_counts(spark: SparkSession, config: EngineConfig) -> DataFrame:
    """Count observed transitions at exact MOB, excluding right censoring."""

    curated = config.paths.curated.rstrip("/\\")
    panel_path = f"{curated}/panel"
    panel = spark.read.parquet(panel_path)
    return (
        observed_transition_rows(panel)
        .groupBy(*TRANSITION_KEYS)
        .agg(F.count(F.lit(1)).cast("long").alias("transition_count"))
    )


def observed_transition_rows(panel: DataFrame) -> DataFrame:
    """Apply the denominator contract before any transition aggregation."""

    return panel.where(~F.col("is_censored")).where(
        F.col("next_delinquency_state").isNotNull()
    )


def collect_transition_inputs(
    spark: SparkSession, config: EngineConfig
) -> tuple[pd.DataFrame, pd.DataFrame, TransitionAggregationReport]:
    """Collect the compact count cube and one macro row per calendar month."""

    curated = config.paths.curated.rstrip("/\\")
    panel_path = f"{curated}/panel"
    panel = spark.read.parquet(panel_path)
    excluded_censored = panel.where(F.col("is_censored")).count()
    counts_spark = aggregate_transition_counts(spark, config)
    observed_transitions = int(
        counts_spark.agg(F.sum("transition_count").alias("n")).first()["n"]
    )
    counts = counts_spark.toPandas()
    macro_columns = [
        *MACRO_VALUE_COLUMNS,
        *[
            f"{column}_lag_{lag}"
            for lag in config.model.macro_lags
            for column in MACRO_VALUE_COLUMNS
        ],
    ]
    macro = (
        panel.select("as_of_month", *macro_columns)
        .dropDuplicates(["as_of_month"])
        .orderBy("as_of_month")
        .toPandas()
    )
    report = TransitionAggregationReport(
        aggregated_rows=len(counts),
        observed_transitions=observed_transitions,
        excluded_censored_rows=excluded_censored,
        spark_ui=spark_ui_summary(spark),
    )
    return counts, macro, report


def empirical_transition_matrix(
    counts: pd.DataFrame, config: EngineConfig
) -> pd.DataFrame:
    """Build an unconditional matrix with exact absorbing identity rows.

    A transient state with no observations falls back to a self-transition
    prior. This conservative prior avoids NaN while preserving probability mass.
    """

    states = list(config.states.ordered)
    totals = counts.groupby(
        ["delinquency_state", "next_delinquency_state"], as_index=False
    )["transition_count"].sum()
    matrix = (
        totals.pivot(
            index="delinquency_state",
            columns="next_delinquency_state",
            values="transition_count",
        )
        .reindex(index=states, columns=states, fill_value=0.0)
        .fillna(0.0)
        .astype(float)
    )
    absorbing = set(config.states.absorbing)
    for state in states:
        if state in absorbing:
            matrix.loc[state, :] = 0.0
            matrix.loc[state, state] = 1.0
            continue
        total = float(matrix.loc[state].sum())
        if total == 0.0:
            matrix.loc[state, state] = 1.0
        else:
            matrix.loc[state] /= total
    if not np.allclose(matrix.sum(axis=1).to_numpy(), 1.0):
        raise ValueError("Empirical transition matrix rows do not sum to one")
    for state in absorbing:
        expected = np.zeros(len(states))
        expected[states.index(state)] = 1.0
        if not np.array_equal(matrix.loc[state].to_numpy(), expected):
            raise ValueError(f"Absorbing state is not exact identity: {state}")
    matrix["row_sum"] = matrix.sum(axis=1)
    return matrix


def _write_csv(frame: pd.DataFrame, path: str) -> None:
    if "://" in path:
        with fsspec.open(path, "wt") as stream:
            frame.to_csv(stream, index=False)
    else:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(output, index=False)


def _matrix_dict(matrix: np.ndarray, states: tuple[str, ...]) -> dict[str, dict[str, float]]:
    return {
        origin: {
            destination: float(matrix[row, column])
            for column, destination in enumerate(states)
        }
        for row, origin in enumerate(states)
    }


def run_transition_model(
    spark: SparkSession, config: EngineConfig
) -> TransitionModelReport:
    """Aggregate, fit, validate, and report the complete M5 transition model."""

    from src.model.conditional import (
        fit_conditional_models,
        ground_truth_recovery_table,
        macro_sensitivity_table,
    )

    counts, macro, aggregation_report = collect_transition_inputs(spark, config)
    empirical = empirical_transition_matrix(counts, config)
    model = fit_conditional_models(counts, macro, config)
    score_band = model.score_bands[len(model.score_bands) // 2]
    mob = 36
    benign_macro = dict(model.macro_defaults)
    stressed_macro = dict(benign_macro)
    for key in stressed_macro:
        if key == "unemployment_rate" or key.startswith("unemployment_rate_lag_"):
            stressed_macro[key] += 3.0
        elif key == "unemployment_change_3m" or key.startswith(
            "unemployment_change_3m_lag_"
        ):
            stressed_macro[key] += 1.0
        elif key == "hpi_change_yoy" or key.startswith("hpi_change_yoy_lag_"):
            stressed_macro[key] -= 0.10
    benign = model.build_matrix(mob, score_band, benign_macro)
    stressed = model.build_matrix(mob, score_band, stressed_macro)
    sensitivity = macro_sensitivity_table(model, mob, score_band)
    material = sensitivity[
        sensitivity["unemployment_1pp_change_bps"].abs() >= 1.0
    ].copy()
    truth = ground_truth_recovery_table(model, mob, score_band)
    wrong_sign = truth[~truth["sign_match"]]
    if not wrong_sign.empty:
        raise ValueError(
            "Conditional model failed ground-truth sign recovery: "
            f"{wrong_sign[['origin', 'destination']].to_dict('records')}"
        )
    coefficients = model.coefficient_table()
    _write_csv(empirical.reset_index(), config.paths.transition_empirical_table)
    _write_csv(coefficients, config.paths.transition_coefficients)
    _write_csv(truth, config.paths.transition_ground_truth)
    _write_csv(material, config.paths.transition_interpretations)
    states = tuple(config.states.ordered)
    return TransitionModelReport(
        aggregation=aggregation_report,
        empirical_matrix={
            str(origin): {str(destination): float(value) for destination, value in row.items()}
            for origin, row in empirical.iterrows()
        },
        selected_macro_timing={
            origin: fitted.macro_columns
            for origin, fitted in model.origin_models.items()
        },
        delinquent_prepay_hazard=model.delinquent_prepay_hazard,
        repurchase_hazard=model.repurchase_hazard,
        benign_matrix=_matrix_dict(benign, states),
        stressed_matrix=_matrix_dict(stressed, states),
        material_macro_interpretations=tuple(material.to_dict("records")),
        ground_truth_recovery=tuple(truth.to_dict("records")),
        empirical_table_path=config.paths.transition_empirical_table,
        coefficient_table_path=config.paths.transition_coefficients,
        ground_truth_table_path=config.paths.transition_ground_truth,
        interpretation_table_path=config.paths.transition_interpretations,
    )
