"""Configuration-only construction of the Spark execution environment."""

from __future__ import annotations

from pyspark.sql import SparkSession

from src.config import EngineConfig


def build_spark_session(config: EngineConfig) -> SparkSession:
    """Build Spark using environment-controlled resources for credit data stages.

    Keeping compute settings outside transformation jobs lets the same ingestion
    and panel logic run locally or on managed Spark without source edits.
    """

    return (
        SparkSession.builder.master(config.spark.master)
        .appName(config.spark.app_name)
        .config("spark.driver.memory", config.spark.driver_memory)
        .config("spark.sql.shuffle.partitions", str(config.spark.shuffle_partitions))
        .config("spark.sql.files.maxRecordsPerFile", str(config.ingest.max_records_per_file))
        .getOrCreate()
    )
