"""Macroeconomic source loading, calendar alignment, and lag construction."""

from __future__ import annotations

from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F
from pyspark.sql.types import DateType, DoubleType, StructField, StructType

from src.config import EngineConfig

MACRO_VALUE_COLUMNS = (
    "unemployment_rate",
    "unemployment_change_3m",
    "hpi_change_yoy",
)


def macro_schema() -> StructType:
    """Return stable types for the local macro source and future FRED-shaped input."""

    return StructType(
        [
            StructField("as_of_month", DateType(), True),
            StructField("unemployment_rate", DoubleType(), True),
            StructField("unemployment_change_3m", DoubleType(), True),
            StructField("hpi_change_yoy", DoubleType(), True),
        ]
    )


def read_macro_source(spark: SparkSession, config: EngineConfig) -> DataFrame:
    """Read the configured macro source without coupling joins to its provider.

    A future FRED download only needs to produce this canonical schema; calendar
    alignment and feature timing remain unchanged.
    """

    return (
        spark.read.option("header", "true")
        .option("dateFormat", config.ingest.date_format)
        .option("mode", config.ingest.parse_mode)
        .schema(macro_schema())
        .csv(config.paths.macro)
    )


def prepare_macro_features(frame: DataFrame, config: EngineConfig) -> DataFrame:
    """Align observations to month start and create configured backward lags.

    Unlagged month ``t`` values describe the transition into month ``t``. Lagged
    columns are backward-looking alternatives for conditional-model fitting.
    """

    # Macro data is one global monthly series (~350 rows), so its lags require
    # global calendar order. Make the intentionally tiny single partition
    # explicit; an unpartitioned window would be unacceptable on the loan panel.
    aligned = frame.coalesce(1).withColumn(
        "as_of_month", F.trunc(F.col("as_of_month"), "month").cast("date")
    )
    order = Window.orderBy("as_of_month")
    previous_month = F.lag("as_of_month").over(order)
    checked = aligned.withColumn(
        "_macro_gap",
        previous_month.isNotNull()
        & (F.months_between("as_of_month", previous_month) != F.lit(1.0)),
    ).withColumn(
        "_macro_duplicate",
        F.count(F.lit(1)).over(Window.partitionBy("as_of_month")) > 1,
    )
    quality = checked.agg(
        F.sum(F.when(F.col("as_of_month").isNull(), 1).otherwise(0)).alias("missing_month"),
        F.sum(F.when(F.col("_macro_gap"), 1).otherwise(0)).alias("gaps"),
        F.sum(F.when(F.col("_macro_duplicate"), 1).otherwise(0)).alias("duplicates"),
        *[
            F.sum(F.when(F.col(column).isNull(), 1).otherwise(0)).alias(f"missing_{column}")
            for column in MACRO_VALUE_COLUMNS
        ],
    ).first()
    violations = {name: int(value) for name, value in quality.asDict().items() if value}
    if violations:
        raise ValueError(f"Macro series failed monthly integrity checks: {violations}")

    # The duplicate check partitions by month; restore the intentional global
    # series partition before applying the configured calendar lags.
    result = checked.drop("_macro_gap", "_macro_duplicate").coalesce(1)
    for lag in config.model.macro_lags:
        for column in MACRO_VALUE_COLUMNS:
            result = result.withColumn(f"{column}_lag_{lag}", F.lag(column, lag).over(order))
    return result


def load_macro_features(spark: SparkSession, config: EngineConfig) -> DataFrame:
    """Load and prepare the canonical monthly macro feature table."""

    return prepare_macro_features(read_macro_source(spark, config), config)


def join_macro_features(panel: DataFrame, macro: DataFrame) -> DataFrame:
    """Join month ``t`` macro values to the transition observed in month ``t``."""

    return panel.join(F.broadcast(macro), "as_of_month", "left")
