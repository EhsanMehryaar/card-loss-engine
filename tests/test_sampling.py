from dataclasses import replace

from pyspark.sql import Window
from pyspark.sql import functions as F

from src.config import PathConfig
from src.ingest.raw_to_parquet import run_ingestion
from src.ingest.synthetic import generate_portfolio, write_portfolio


def test_ten_percent_hash_sample_keeps_complete_contiguous_histories(
    spark, quality_config, tmp_path
) -> None:
    root = tmp_path / "sampling"
    config = replace(
        quality_config,
        paths=PathConfig(
            raw_acquisition=(root / "raw" / "acquisition").as_posix(),
            raw_performance=(root / "raw" / "performance").as_posix(),
            curated=(root / "curated").as_posix(),
            macro=(root / "raw" / "macro" / "unemployment.csv").as_posix(),
            output=(root / "output").as_posix(),
            vintage_plot=(root / "docs" / "vintage_curves.png").as_posix(),
            vintage_table=(root / "docs" / "vintage_ultimate_rates.csv").as_posix(),
            vintage_annual_table=(
                root / "docs" / "vintage_ultimate_rates_annual.csv"
            ).as_posix(),
            vintage_backtest_table=(
                root / "docs" / "vintage_projection_accuracy.csv"
            ).as_posix(),
            transition_empirical_table=(root / "docs" / "empirical.csv").as_posix(),
            transition_coefficients=(root / "docs" / "coefficients.csv").as_posix(),
            transition_ground_truth=(root / "docs" / "ground_truth.csv").as_posix(),
            transition_interpretations=(
                root / "docs" / "interpretations.csv"
            ).as_posix(),
        ),
        sample_fraction=0.10,
        synthetic=replace(
            quality_config.synthetic,
            number_of_loans=1_000,
            max_observation_months=24,
        ),
    )
    diagnostic = generate_portfolio(config)
    write_portfolio(diagnostic, config)
    report = run_ingestion(spark, config)

    assert 70 <= report.acquisition_loans_after_sampling <= 130
    curated = spark.read.parquet(f"{config.paths.curated}/performance")
    actual_counts = {
        row["loan_id"]: int(row["count"])
        for row in curated.groupBy("loan_id").count().collect()
    }
    expected_counts = diagnostic.performance.groupby("loan_id").size().to_dict()
    assert actual_counts
    assert all(actual_counts[loan_id] == expected_counts[loan_id] for loan_id in actual_counts)

    order = Window.partitionBy("loan_id").orderBy("as_of_month")
    previous = F.lag("as_of_month").over(order)
    gaps = (
        curated.withColumn("previous_month", previous)
        .where(
            F.col("previous_month").isNotNull()
            & (F.months_between("as_of_month", "previous_month") != F.lit(1.0))
        )
        .count()
    )
    assert gaps == 0
