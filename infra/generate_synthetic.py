"""Generate the 250k-loan EMR fixture in bounded-memory vintage chunks."""

from __future__ import annotations

import argparse
import gc
import time
from dataclasses import replace
from pathlib import Path

import pandas as pd

from src.config import load_config
from src.ingest import synthetic as generator


def _write_vintage(portfolio: generator.SyntheticPortfolio, root: Path, year: int) -> int:
    ids = portfolio.acquisition["loan_id"].astype(str)
    mapping = {
        loan_id: f"SYN{year}{index:08d}"
        for index, loan_id in enumerate(ids, start=1)
    }
    acquisition = portfolio.acquisition.copy()
    performance = portfolio.performance.copy()
    acquisition["loan_id"] = acquisition["loan_id"].map(mapping)
    performance["loan_id"] = performance["loan_id"].map(mapping)
    acquisition_path = root / "raw" / "acquisition"
    performance_path = root / "raw" / "performance"
    acquisition_path.mkdir(parents=True, exist_ok=True)
    performance_path.mkdir(parents=True, exist_ok=True)
    acquisition.drop(columns=["censoring_date"], errors="ignore").to_csv(
        acquisition_path / f"acquisition_{year}.txt", sep="|", index=False
    )
    performance.to_csv(
        performance_path / f"performance_{year}.txt", sep="|", index=False
    )
    return len(performance)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="emr")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--number-of-loans", type=int)
    args = parser.parse_args()
    config = load_config(args.env, args.config_dir)
    settings = (
        replace(config.synthetic, number_of_loans=args.number_of_loans)
        if args.number_of_loans is not None
        else config.synthetic
    )
    if settings.number_of_loans < 1:
        raise ValueError("--number-of-loans must be positive")
    years = list(range(settings.first_vintage_year, settings.last_vintage_year + 1))
    loans_per_year, remainder = divmod(settings.number_of_loans, len(years))
    base_month = pd.Timestamp(f"{settings.first_vintage_year}-01-01")
    month_count = len(years) * 12 + settings.max_observation_months - 1
    macro_end = base_month + pd.DateOffset(months=month_count - 1)
    global_macro = generator.simulate_unemployment(
        base_month,
        macro_end,
        settings.unemployment_start,
        generator._rng(config.project.seed, 1000),  # noqa: SLF001
    )

    def shared_macro(start, end, _initial, _random):
        mask = global_macro["as_of_month"].between(pd.Timestamp(start), pd.Timestamp(end))
        return global_macro.loc[mask].reset_index(drop=True)

    generator.simulate_unemployment = shared_macro
    macro_path = args.output_root / "raw" / "macro" / "unemployment.csv"
    macro_path.parent.mkdir(parents=True, exist_ok=True)
    global_macro.to_csv(macro_path, index=False)
    started = time.perf_counter()
    total_rows = 0
    for offset, year in enumerate(years):
        loan_count = loans_per_year + int(offset < remainder)
        chunk = replace(
            config,
            project=replace(config.project, seed=config.project.seed + offset * 100_003),
            synthetic=replace(
                settings,
                number_of_loans=loan_count,
                first_vintage_year=year,
                last_vintage_year=year,
            ),
        )
        portfolio = generator.generate_portfolio(chunk)
        total_rows += _write_vintage(portfolio, args.output_root, year)
        print(f"vintage={year} loans={loan_count:,} rows={len(portfolio.performance):,}")
        del portfolio
        gc.collect()
    elapsed = time.perf_counter() - started
    print(
        f"generated_loans={settings.number_of_loans:,} performance_rows={total_rows:,} "
        f"wall_seconds={elapsed:.2f} output_root={args.output_root}"
    )


if __name__ == "__main__":
    main()
