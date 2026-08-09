"""Thin EMR driver for the distributed portions of M2, M3, and M5."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from pyspark.sql import functions as F

from src.config import load_config
from src.ingest.raw_to_parquet import parquet_file_summary, spark_ui_summary
from src.spark_session import build_spark_session


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("ingest", "panel", "transitions"))
    parser.add_argument("--env", default="emr")
    parser.add_argument("--config-dir", default=".")
    args = parser.parse_args()
    config = load_config(args.env, Path(args.config_dir))
    spark = build_spark_session(config)
    spark.sparkContext.setLogLevel("WARN")
    try:
        if args.stage == "ingest":
            from src.ingest.raw_to_parquet import run_ingestion

            report = asdict(run_ingestion(spark, config))
        elif args.stage == "panel":
            from src.panel.build_panel import run_panel

            report = asdict(run_panel(spark, config))
        else:
            from src.model.transitions import aggregate_transition_counts

            curated = config.paths.curated.rstrip("/\\")
            panel = spark.read.parquet(f"{curated}/panel")
            excluded = panel.where(F.col("is_censored")).count()
            counts = aggregate_transition_counts(spark, config).cache()
            aggregated_rows = counts.count()
            observed = int(
                counts.agg(F.sum("transition_count").alias("n")).first()["n"]
            )
            output_path = f"{curated}/transition_counts"
            counts.write.mode("overwrite").parquet(output_path)
            report = {
                "aggregated_rows": aggregated_rows,
                "observed_transitions": observed,
                "excluded_censored_rows": excluded,
                "output_path": output_path,
                "output_files": asdict(parquet_file_summary(spark, output_path)),
                "spark_ui": asdict(spark_ui_summary(spark)),
            }
            counts.unpersist(blocking=False)
        print(json.dumps(report, indent=2))
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
