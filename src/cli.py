"""Command-line entry point for card-loss-engine stages."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, replace
from pathlib import Path

from src.config import load_config
from src.ingest.raw_to_parquet import run_ingestion
from src.ingest.synthetic import generate_portfolio, write_portfolio
from src.model.cecl import run_cecl
from src.model.transitions import run_transition_model
from src.model.vintage import run_vintage
from src.panel.build_panel import run_panel
from src.scenarios.run import run_scenarios
from src.spark_session import build_spark_session


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage",
        choices=(
            "config",
            "synthetic",
            "ingest",
            "panel",
            "vintage",
            "transitions",
            "ecl",
            "scenarios",
        ),
    )
    parser.add_argument("--env", default="local", help="Configuration overlay name")
    parser.add_argument("--config-dir", default="config", help="Directory containing YAML config")
    parser.add_argument(
        "--sample-fraction",
        type=float,
        help="Override configured loan-level hash sample fraction for this run",
    )
    return parser


def main() -> None:
    """Load configuration and execute the requested credit-engine stage."""

    args = _parser().parse_args()
    config = load_config(args.env, Path(args.config_dir))
    if args.sample_fraction is not None:
        if not 0 < args.sample_fraction <= 1:
            raise ValueError("--sample-fraction must be in the interval (0, 1]")
        config = replace(config, sample_fraction=args.sample_fraction)
    if args.stage == "config":
        print(json.dumps(asdict(config), indent=2))
        return
    if args.stage == "ecl":
        print(json.dumps(asdict(run_cecl(config)), indent=2))
        return
    if args.stage == "scenarios":
        print(json.dumps(asdict(run_scenarios(config)), indent=2))
        return
    if args.stage in {"ingest", "panel", "vintage", "transitions"}:
        spark = build_spark_session(config)
        spark.sparkContext.setLogLevel("WARN")
        try:
            stages = {
                "ingest": run_ingestion,
                "panel": run_panel,
                "vintage": run_vintage,
                "transitions": run_transition_model,
            }
            report = stages[args.stage](spark, config)
            print(json.dumps(asdict(report), indent=2))
        finally:
            spark.stop()
        return
    started = time.perf_counter()
    portfolio = generate_portfolio(config)
    generation_seconds = time.perf_counter() - started
    locations = write_portfolio(portfolio, config)
    print(
        f"Generated {len(portfolio.acquisition):,} loans and "
        f"{len(portfolio.panel):,} account-months in {generation_seconds:.2f} seconds"
    )
    print("Output files:")
    for name, path in locations.items():
        print(f"  {name}: {path}")
    print("\nPanel head:")
    print(portfolio.panel.head().to_string(index=False))


if __name__ == "__main__":
    main()
