"""Known-answer tests for vintage loss curves and development."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from src.model.vintage import (
    RATE_COLUMNS,
    aggregate_vintage_spark,
    build_vintage_curves,
    compute_development_factors,
    fit_vintage_model,
    loss_decomposition,
)


def _aggregated_triangle() -> pd.DataFrame:
    losses = {
        "2006-01": [0.0, 10.0, 5.0, 5.0],
        "2007-01": [0.0, 5.0, 5.0, 10.0],
        "2018-01": [0.0, 0.0],
    }
    records = []
    for vintage, monthly_losses in losses.items():
        max_mob = len(monthly_losses) - 1
        for mob, loss in enumerate(monthly_losses):
            record = {
                "vintage": vintage,
                "months_on_book": mob,
                "accounts_active": 10,
                "chargeoff_accounts": int(loss > 0),
                "balance_active": 100.0,
                "gross_chargeoff_dollars": loss,
                "recovery_dollars": 0.0,
                "net_chargeoff_dollars": loss,
                "prepay_dollars": 0.0,
                "repurchase_dollars": 0.0,
                "original_balance": 100.0,
                "original_accounts": 10,
                "max_observed_mob": max_mob,
            }
            records.append(record)
    return pd.DataFrame(records)


def test_fully_observed_vintage_projects_to_actual_ultimate() -> None:
    model = fit_vintage_model(build_vintage_curves(_aggregated_triangle(), 3), 3)
    projected = model.project_vintage_to_ultimate("2006-01")

    assert not projected["is_projected"].any()
    assert projected.iloc[-1]["chain_ladder_rate_original_balance"] == 0.20
    assert projected.iloc[-1]["scaled_average_rate_original_balance"] == 0.20
    assert np.isfinite(model.portfolio_ultimate_loss())


def test_development_factors_ignore_masked_cells() -> None:
    curves = build_vintage_curves(_aggregated_triangle(), 3)
    baseline = compute_development_factors(
        curves, "cumulative_net_chargeoff_dollars", 3
    )
    masked = curves.copy()
    masked.loc[~masked["is_observed"], "cumulative_net_chargeoff_dollars"] = 1e12
    recalculated = compute_development_factors(
        masked, "cumulative_net_chargeoff_dollars", 3
    )

    pd.testing.assert_frame_equal(baseline, recalculated)
    assert baseline["contributing_vintages"].eq(2).all()


def test_cumulative_loss_is_monotonic() -> None:
    curves = build_vintage_curves(_aggregated_triangle(), 3)
    observed = curves[curves["is_observed"]]

    for _, vintage in observed.groupby("vintage"):
        assert vintage[RATE_COLUMNS["original_balance"]].diff().fillna(0).ge(0).all()


def test_all_repurchased_vintage_is_zero_not_nan_or_undefined() -> None:
    aggregated = _aggregated_triangle()
    repurchase_terminal = aggregated["vintage"].eq("2018-01") & aggregated[
        "months_on_book"
    ].eq(1)
    aggregated.loc[repurchase_terminal, "repurchase_dollars"] = aggregated.loc[
        repurchase_terminal, "balance_active"
    ]
    model = fit_vintage_model(build_vintage_curves(aggregated, 3), 3)
    projected = model.project_vintage_to_ultimate("2018-01")

    for denominator in RATE_COLUMNS:
        assert np.isfinite(projected[f"chain_ladder_rate_{denominator}"]).all()
        assert projected[f"chain_ladder_rate_{denominator}"].eq(0.0).all()
        assert projected[f"scaled_average_rate_{denominator}"].eq(0.0).all()


def test_chain_ladder_and_scaled_average_are_distinct_methods() -> None:
    aggregated = _aggregated_triangle()
    aggregated.loc[aggregated["vintage"].eq("2007-01"), "balance_active"] = 50.0
    immature_loss = aggregated["vintage"].eq("2018-01") & aggregated[
        "months_on_book"
    ].eq(1)
    aggregated.loc[immature_loss, "gross_chargeoff_dollars"] = 4.0
    aggregated.loc[immature_loss, "net_chargeoff_dollars"] = 4.0
    aggregated.loc[immature_loss, "chargeoff_accounts"] = 1
    model = fit_vintage_model(build_vintage_curves(aggregated, 3), 3)
    projected = model.project_vintage_to_ultimate("2018-01").iloc[-1]

    pd.testing.assert_frame_equal(
        model.development_factors["original_balance"],
        model.development_factors["average_outstanding"],
    )
    assert not np.isclose(
        projected["chain_ladder_rate_average_outstanding"],
        projected["scaled_average_rate_average_outstanding"],
    )


def test_loss_decomposition_reconciles_exactly() -> None:
    aggregated = _aggregated_triangle()
    decomposition = loss_decomposition(aggregated, portfolio_ultimate_rate=0.25)
    product = (
        decomposition.observed_default_rate
        * decomposition.average_ead_fraction_original_balance
        * decomposition.realized_average_lgd
    )

    assert np.isclose(product, decomposition.observed_net_loss_rate)
    assert decomposition.ultimate_development_multiplier == (
        0.25 / decomposition.observed_net_loss_rate
    )


def test_spark_aggregation_treats_all_repurchased_vintage_as_zero_loss(
    spark, quality_config, tmp_path
) -> None:
    curated = tmp_path / "repurchase_known_answer"
    acquisition = spark.sql(
        """
        SELECT * FROM VALUES
          ('L1', DATE '2018-01-01', 100.0D),
          ('L2', DATE '2018-01-01', 100.0D)
        AS acquisition(loan_id, origination_month, original_upb)
        """
    )
    panel = spark.sql(
        """
        SELECT * FROM VALUES
          ('L1', DATE '2018-01-01', '2018-01', 0, 100.0D, CAST(NULL AS STRING), 0.0D, 0.0D),
          ('L2', DATE '2018-01-01', '2018-01', 0, 100.0D, CAST(NULL AS STRING), 0.0D, 0.0D),
          ('L1', DATE '2018-02-01', '2018-01', 1, 90.0D, 'Repurchased', 0.0D, 0.0D),
          ('L2', DATE '2018-02-01', '2018-01', 1, 90.0D, 'Repurchased', 0.0D, 0.0D)
        AS panel(
          loan_id, as_of_month, vintage, months_on_book, upb_bom, exit_reason,
          net_sales_proceeds, foreclosure_costs
        )
        """
    )
    acquisition.write.mode("overwrite").parquet((curated / "acquisition").as_posix())
    panel.write.mode("overwrite").parquet((curated / "panel").as_posix())
    config = replace(
        quality_config,
        paths=replace(quality_config.paths, curated=curated.as_posix()),
        model=replace(
            quality_config.model,
            vintage_analysis_as_of="2018-02-01",
            vintage_maturity_mob=1,
        ),
    )

    aggregated = aggregate_vintage_spark(spark, config).toPandas()
    terminal = aggregated.loc[aggregated["months_on_book"].eq(1)].iloc[0]
    assert terminal["repurchase_dollars"] == 180.0
    assert terminal["gross_chargeoff_dollars"] == 0.0
    assert terminal["net_chargeoff_dollars"] == 0.0
    assert aggregated["months_on_book"].tolist() == [0, 1]

    model = fit_vintage_model(
        build_vintage_curves(aggregated, 1), 1, "quarterly"
    )
    projected = model.project_vintage_to_ultimate("2018-Q1")
    for denominator in RATE_COLUMNS:
        assert projected[f"chain_ladder_rate_{denominator}"].notna().all()
        assert projected[f"chain_ladder_rate_{denominator}"].eq(0.0).all()
