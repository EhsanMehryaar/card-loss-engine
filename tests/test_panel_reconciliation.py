from dataclasses import replace

from pyspark.sql import functions as F

from src.ingest.raw_to_parquet import run_ingestion
from src.ingest.synthetic import generate_portfolio, write_portfolio
from src.panel.build_panel import run_panel


def test_spark_panel_reconciles_exactly_to_m1_diagnostic(spark, quality_config, tmp_path) -> None:
    root = tmp_path / "reconciliation"
    config = replace(
        quality_config,
        paths=replace(
            quality_config.paths,
            raw_acquisition=(root / "raw" / "acquisition").as_posix(),
            raw_performance=(root / "raw" / "performance").as_posix(),
            curated=(root / "curated").as_posix(),
            macro=(root / "raw" / "macro" / "unemployment.csv").as_posix(),
            output=(root / "output").as_posix(),
            vintage_plot=(root / "docs" / "vintage_curves.png").as_posix(),
            vintage_table=(root / "docs" / "vintage_ultimate_rates.csv").as_posix(),
            vintage_annual_table=(root / "docs" / "vintage_ultimate_rates_annual.csv").as_posix(),
            vintage_backtest_table=(root / "docs" / "vintage_projection_accuracy.csv").as_posix(),
            transition_empirical_table=(root / "docs" / "empirical.csv").as_posix(),
            transition_coefficients=(root / "docs" / "coefficients.csv").as_posix(),
            transition_ground_truth=(root / "docs" / "ground_truth.csv").as_posix(),
            transition_interpretations=(root / "docs" / "interpretations.csv").as_posix(),
        ),
        sample_fraction=1.0,
        synthetic=replace(
            quality_config.synthetic,
            number_of_loans=150,
            max_observation_months=36,
            score_mean=680.0,
            score_std=45.0,
        ),
    )
    diagnostic = generate_portfolio(config)
    locations = write_portfolio(diagnostic, config)
    run_ingestion(spark, config)
    panel_report = run_panel(spark, config)
    assert panel_report.vendor_mob_mismatch_rows == 0
    assert panel_report.vendor_mob_mismatch_rate == 0.0
    assert panel_report.is_censored_null_rows == 0

    expected = (
        spark.read.parquet(str(locations["panel"]))
        .withColumn("as_of_month", F.to_date("as_of_month"))
        .select(
            "loan_id",
            "as_of_month",
            "delinquency_state",
            "next_delinquency_state",
            "exit_reason",
        )
    )
    actual_panel = spark.read.parquet(f"{config.paths.curated}/panel")
    assert actual_panel.where(F.col("is_censored").isNull()).count() == 0
    actual = actual_panel.select(
        "loan_id",
        "as_of_month",
        "delinquency_state",
        "next_delinquency_state",
        "exit_reason",
    )
    assert actual.count() == expected.count()

    state_discrepancies = (
        actual.alias("a")
        .join(expected.alias("e"), ["loan_id", "as_of_month"], "full")
        .where(~F.col("a.delinquency_state").eqNullSafe(F.col("e.delinquency_state")))
        .select(
            "loan_id",
            "as_of_month",
            F.col("a.delinquency_state").alias("actual_state"),
            F.col("e.delinquency_state").alias("expected_state"),
        )
        .collect()
    )
    assert not state_discrepancies, f"State discrepancies: {state_discrepancies[:20]}"

    expected_exits = expected.where(F.col("exit_reason").isNotNull()).select(
        "loan_id", F.col("exit_reason").alias("expected_exit")
    )
    actual_exits = actual.where(F.col("exit_reason").isNotNull()).select(
        "loan_id", F.col("exit_reason").alias("actual_exit")
    )
    exit_discrepancies = (
        actual_exits.join(expected_exits, "loan_id", "full")
        .where(~F.col("actual_exit").eqNullSafe(F.col("expected_exit")))
        .collect()
    )
    assert not exit_discrepancies, f"Exit discrepancies: {exit_discrepancies[:20]}"

    grouping = ["delinquency_state", "next_delinquency_state"]
    actual_transitions = (
        actual.where(F.col("next_delinquency_state").isNotNull())
        .groupBy(*grouping)
        .count()
        .withColumnRenamed("count", "actual_count")
    )
    expected_transitions = (
        expected.where(F.col("next_delinquency_state").isNotNull())
        .groupBy(*grouping)
        .count()
        .withColumnRenamed("count", "expected_count")
    )
    transition_discrepancies = (
        actual_transitions.join(expected_transitions, grouping, "full")
        .where(~F.col("actual_count").eqNullSafe(F.col("expected_count")))
        .collect()
    )
    assert not transition_discrepancies, (
        f"Transition-count discrepancies: {transition_discrepancies[:20]}"
    )
