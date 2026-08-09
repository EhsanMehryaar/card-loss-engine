"""Parse, validate, loan-sample, and curate raw mortgage files with Spark."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Iterable
from urllib.request import urlopen

from pyspark import StorageLevel
from pyspark.sql import Column, DataFrame, SparkSession, Window
from pyspark.sql import functions as F
from pyspark.sql.types import StructType

from src.config import EngineConfig
from src.ingest.schemas import acquisition_schema, performance_schema

LOGGER = logging.getLogger(__name__)

ACQUISITION_EXCLUSION_REASONS = (
    "acquisition_missing_loan_id",
    "acquisition_invalid_origination_month",
    "acquisition_invalid_original_upb",
    "acquisition_invalid_original_term",
    "acquisition_invalid_score",
    "acquisition_duplicate_loan_id",
)
PERFORMANCE_EXCLUSION_REASONS = (
    "performance_missing_loan_id",
    "performance_invalid_as_of_month",
    "performance_invalid_current_upb",
    "performance_invalid_delinquency",
    "performance_invalid_loan_age",
    "performance_duplicate_loan_month",
    "performance_orphan_loan_id",
)


@dataclass(frozen=True)
class SparkUiSummary:
    """Completed-stage and shuffle metrics reported by the Spark UI."""

    stage_count: int
    task_count: int
    shuffle_read_bytes: int
    shuffle_write_bytes: int


@dataclass(frozen=True)
class DatasetFileSummary:
    """Physical Parquet layout metrics that expose small-file regressions."""

    file_count: int
    total_size_bytes: int
    average_file_size_bytes: float


@dataclass(frozen=True)
class QualitySummary:
    """One-pass row exclusions and rates for a raw mortgage dataset."""

    total_rows: int
    excluded_rows: int
    reason_counts: dict[str, int]
    reason_rates: dict[str, float]


@dataclass(frozen=True)
class IngestionReport:
    """Auditable counts, quality results, file layout, and Spark work metrics."""

    acquisition_rows_before: int
    performance_rows_before: int
    acquisition_rows_valid: int
    performance_rows_valid: int
    acquisition_loans_after_sampling: int
    acquisition_rows_after_sampling: int
    performance_rows_after_sampling: int
    exclusion_counts: dict[str, int]
    exclusion_rates: dict[str, float]
    overall_exclusion_rate: float
    acquisition_output: str
    performance_output: str
    acquisition_files: DatasetFileSummary
    performance_files: DatasetFileSummary
    spark_ui: SparkUiSummary


def _read_csv(
    spark: SparkSession,
    path: str,
    schema: StructType,
    config: EngineConfig,
) -> DataFrame:
    return (
        spark.read.option("sep", config.ingest.delimiter)
        .option("header", str(config.ingest.header).lower())
        .option("dateFormat", config.ingest.date_format)
        .option("mode", config.ingest.parse_mode)
        .schema(schema)
        .csv(path)
    )


def _quality_reasons(
    frame: DataFrame, rules: Iterable[tuple[str, Column]]
) -> DataFrame:
    reason_columns = [F.when(condition, F.lit(name)) for name, condition in rules]
    return frame.withColumn("_quality_reasons", F.array_compact(F.array(*reason_columns)))


def _acquisition_quality(frame: DataFrame) -> DataFrame:
    # Duplicate detection is a full-dataset shuffle. It is intentionally retained
    # as the dominant quality cost and primary EMR tuning target at full scale.
    duplicate = F.count(F.lit(1)).over(Window.partitionBy("loan_id")) > 1
    return _quality_reasons(
        frame,
        [
            ("acquisition_missing_loan_id", F.col("loan_id").isNull() | (F.trim("loan_id") == "")),
            ("acquisition_invalid_origination_month", F.col("origination_month").isNull()),
            ("acquisition_invalid_original_upb", F.col("original_upb").isNull() | (F.col("original_upb") <= 0)),
            ("acquisition_invalid_original_term", F.col("original_term").isNull() | (F.col("original_term") <= 0)),
            ("acquisition_invalid_score", F.col("orig_score").isNull() | ~F.col("orig_score").between(300, 850)),
            ("acquisition_duplicate_loan_id", duplicate),
        ],
    )


def _performance_quality(frame: DataFrame) -> DataFrame:
    # This second full shuffle enforces the account-month business key and will be
    # the other primary tuning target when the job moves to full-scale EMR.
    duplicate = F.count(F.lit(1)).over(Window.partitionBy("loan_id", "as_of_month")) > 1
    return _quality_reasons(
        frame,
        [
            ("performance_missing_loan_id", F.col("loan_id").isNull() | (F.trim("loan_id") == "")),
            ("performance_invalid_as_of_month", F.col("as_of_month").isNull()),
            ("performance_invalid_current_upb", F.col("current_upb").isNull() | (F.col("current_upb") < 0)),
            ("performance_invalid_delinquency", F.col("delinquency_status").isNull() | ~F.col("delinquency_status").isin(0, 30, 60, 90, 120, 150, 180)),
            ("performance_invalid_loan_age", F.col("loan_age").isNull() | (F.col("loan_age") < 0)),
            ("performance_duplicate_loan_month", duplicate),
        ],
    )


def _append_orphan_reason(frame: DataFrame, known_loans: DataFrame) -> DataFrame:
    marked = frame.join(
        known_loans.withColumn("_known_loan", F.lit(True)), "loan_id", "left"
    )
    orphan_reason = F.when(
        F.col("_known_loan").isNull(), F.lit("performance_orphan_loan_id")
    )
    return marked.withColumn(
        "_quality_reasons",
        F.array_compact(F.concat("_quality_reasons", F.array(orphan_reason))),
    )


def _quality_summary(frame: DataFrame, reasons: tuple[str, ...]) -> QualitySummary:
    expressions = [
        F.count(F.lit(1)).alias("total_rows"),
        F.sum(F.when(F.size("_quality_reasons") > 0, 1).otherwise(0)).alias(
            "excluded_rows"
        ),
    ]
    expressions.extend(
        F.sum(F.when(F.array_contains("_quality_reasons", reason), 1).otherwise(0)).alias(
            reason
        )
        for reason in reasons
    )
    row = frame.agg(*expressions).first()
    total = int(row["total_rows"])
    counts = {reason: int(row[reason]) for reason in reasons}
    rates = {reason: (count / total if total else 0.0) for reason, count in counts.items()}
    return QualitySummary(total, int(row["excluded_rows"]), counts, rates)


def enforce_quality_policy(
    acquisition: QualitySummary,
    performance: QualitySummary,
    config: EngineConfig,
) -> float:
    """Apply fatal and rate-limited raw-data gates and return overall exclusion rate."""

    counts = acquisition.reason_counts | performance.reason_counts
    rates = acquisition.reason_rates | performance.reason_rates
    total_rows = acquisition.total_rows + performance.total_rows
    excluded_rows = acquisition.excluded_rows + performance.excluded_rows
    overall_rate = excluded_rows / total_rows if total_rows else 0.0
    enforce_quality_rates(
        counts,
        rates,
        overall_rate,
        config,
        fatal_reasons=set(config.ingest.fatal_reasons),
        context="Raw data",
    )
    return overall_rate


def enforce_quality_rates(
    counts: dict[str, int],
    rates: dict[str, float],
    overall_rate: float,
    config: EngineConfig,
    *,
    fatal_reasons: set[str] | None = None,
    context: str,
) -> None:
    """Apply the shared fatal/per-reason/overall threshold policy to quality rates."""

    fatal = fatal_reasons or set()
    for reason in counts:
        LOGGER.warning(
            "Excluded %s rows (%.6f%%): %s",
            counts[reason],
            rates[reason] * 100.0,
            reason,
        )
    failures: list[str] = []
    failures.extend(
        f"fatal {reason}={counts[reason]}" for reason in fatal if counts.get(reason, 0) > 0
    )
    failures.extend(
        f"{reason} rate={rate:.6%} exceeds {config.ingest.max_exclusion_rate_per_reason:.6%}"
        for reason, rate in rates.items()
        if reason not in fatal and rate > config.ingest.max_exclusion_rate_per_reason
    )
    if overall_rate > config.ingest.max_exclusion_rate_overall:
        failures.append(
            f"overall exclusion rate={overall_rate:.6%} exceeds "
            f"{config.ingest.max_exclusion_rate_overall:.6%}"
        )
    if failures:
        raise ValueError(
            f"{context} failed configured quality policy: " + "; ".join(failures)
        )


def _sampled_loans(acquisition: DataFrame, config: EngineConfig) -> DataFrame:
    cutoff = int(config.sample_fraction * config.ingest.hash_buckets)
    bucket = F.pmod(
        F.xxhash64(F.col("loan_id"), F.lit(config.project.seed)),
        F.lit(config.ingest.hash_buckets),
    )
    return acquisition.where(bucket < F.lit(cutoff))


def _output_path(root: str, dataset: str) -> str:
    return f"{root.rstrip('/\\')}/{dataset}"


def parquet_file_summary(spark: SparkSession, path: str) -> DatasetFileSummary:
    """Measure recursive Parquet file count and size through Hadoop FileSystem."""

    hadoop = spark.sparkContext._jvm.org.apache.hadoop  # noqa: SLF001
    configuration = spark.sparkContext._jsc.hadoopConfiguration()  # noqa: SLF001
    output_path = hadoop.fs.Path(path)
    filesystem = output_path.getFileSystem(configuration)
    files = filesystem.listFiles(output_path, True)
    count = 0
    total_bytes = 0
    while files.hasNext():
        status = files.next()
        if str(status.getPath().getName()).endswith(".parquet"):
            count += 1
            total_bytes += int(status.getLen())
    return DatasetFileSummary(
        file_count=count,
        total_size_bytes=total_bytes,
        average_file_size_bytes=(total_bytes / count if count else 0.0),
    )


def spark_ui_summary(spark: SparkSession) -> SparkUiSummary:
    """Read completed stage and shuffle totals from Spark's local UI API."""

    ui_url = spark.sparkContext.uiWebUrl
    if not ui_url:
        return SparkUiSummary(0, 0, 0, 0)
    endpoint = f"{ui_url}/api/v1/applications/{spark.sparkContext.applicationId}/stages"
    with urlopen(endpoint, timeout=10) as response:  # nosec B310: local Spark UI only
        stages = json.load(response)
    completed = [stage for stage in stages if stage.get("status") == "COMPLETE"]
    return SparkUiSummary(
        stage_count=len(completed),
        task_count=sum(int(stage.get("numTasks", 0)) for stage in completed),
        shuffle_read_bytes=sum(int(stage.get("shuffleReadBytes", 0)) for stage in completed),
        shuffle_write_bytes=sum(int(stage.get("shuffleWriteBytes", 0)) for stage in completed),
    )


def run_ingestion(spark: SparkSession, config: EngineConfig) -> IngestionReport:
    """Validate and curate a reproducible loan sample with complete monthly histories."""

    acquisition_checked: DataFrame | None = None
    performance_checked: DataFrame | None = None
    sampled_acquisition: DataFrame | None = None
    sampled_performance: DataFrame | None = None
    try:
        acquisition_checked = _acquisition_quality(
            _read_csv(spark, config.paths.raw_acquisition, acquisition_schema(), config)
        ).persist(StorageLevel.MEMORY_AND_DISK)
        acquisition_quality = _quality_summary(
            acquisition_checked, ACQUISITION_EXCLUSION_REASONS
        )
        known_loans = (
            acquisition_checked.where(
                F.col("loan_id").isNotNull() & (F.trim("loan_id") != "")
            )
            .select("loan_id")
            .dropDuplicates()
        )
        performance_checked = _append_orphan_reason(
            _performance_quality(
                _read_csv(spark, config.paths.raw_performance, performance_schema(), config)
            ),
            known_loans,
        ).persist(StorageLevel.MEMORY_AND_DISK)
        performance_quality = _quality_summary(
            performance_checked, PERFORMANCE_EXCLUSION_REASONS
        )
        overall_rate = enforce_quality_policy(acquisition_quality, performance_quality, config)

        valid_acquisition = acquisition_checked.where(
            F.size("_quality_reasons") == 0
        ).drop("_quality_reasons", "occupancy_status", "channel")
        valid_performance = performance_checked.where(
            F.size("_quality_reasons") == 0
        ).drop("_quality_reasons", "_known_loan", "modification_flag")
        sampled_acquisition = (
            _sampled_loans(valid_acquisition, config)
            .withColumn("vintage_year", F.year("origination_month"))
            .persist(StorageLevel.MEMORY_AND_DISK)
        )
        sampled_acquisition_count = sampled_acquisition.count()
        if sampled_acquisition_count == 0:
            raise ValueError("Hash sampling selected zero loans; increase sample_fraction")
        acquisition_checked.unpersist(blocking=False)
        sampled_performance = (
            valid_performance.join(
                sampled_acquisition.select("loan_id", "vintage_year"), "loan_id", "inner"
            )
            .persist(StorageLevel.MEMORY_AND_DISK)
        )
        sampled_performance_count = sampled_performance.count()
        performance_checked.unpersist(blocking=False)

        acquisition_output = _output_path(config.paths.curated, "acquisition")
        performance_output = _output_path(config.paths.curated, "performance")
        sampled_acquisition.repartition(F.col("vintage_year")).write.mode(
            "overwrite"
        ).partitionBy("vintage_year").parquet(acquisition_output)
        sampled_performance.repartition(F.col("vintage_year")).write.mode(
            "overwrite"
        ).partitionBy("vintage_year").parquet(performance_output)
        acquisition_files = parquet_file_summary(spark, acquisition_output)
        performance_files = parquet_file_summary(spark, performance_output)
        summary = spark_ui_summary(spark)
        counts = acquisition_quality.reason_counts | performance_quality.reason_counts
        rates = acquisition_quality.reason_rates | performance_quality.reason_rates
        return IngestionReport(
            acquisition_rows_before=acquisition_quality.total_rows,
            performance_rows_before=performance_quality.total_rows,
            acquisition_rows_valid=(
                acquisition_quality.total_rows - acquisition_quality.excluded_rows
            ),
            performance_rows_valid=(
                performance_quality.total_rows - performance_quality.excluded_rows
            ),
            acquisition_loans_after_sampling=sampled_acquisition_count,
            acquisition_rows_after_sampling=sampled_acquisition_count,
            performance_rows_after_sampling=sampled_performance_count,
            exclusion_counts=counts,
            exclusion_rates=rates,
            overall_exclusion_rate=overall_rate,
            acquisition_output=acquisition_output,
            performance_output=performance_output,
            acquisition_files=acquisition_files,
            performance_files=performance_files,
            spark_ui=summary,
        )
    finally:
        for frame in (
            sampled_performance,
            sampled_acquisition,
            performance_checked,
            acquisition_checked,
        ):
            if frame is not None:
                frame.unpersist(blocking=False)
