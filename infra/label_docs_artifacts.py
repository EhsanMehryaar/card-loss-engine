"""Backfill portfolio scope and fit provenance on checked-in result CSVs."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

PORTFOLIO = "Local synthetic portfolio (25,000 loans)"


def provenance(path: Path) -> tuple[str, str]:
    name = path.name
    if name.startswith("vintage_"):
        return "pre_cutoff_chain_ladder", "2018-12-01"
    if name.startswith("transition_"):
        return "production_full_history", "full_available_history"
    if name.startswith("m7_"):
        return "production_full_history", "full_available_history"
    if name.startswith(("ecl_", "lgd_", "m6_")):
        return "see_artifact_rows_or_cutoff_clean", "2018-12-01_or_labelled_in_rows"
    return "not_model_fit", "not_applicable"


def label_csv(path: Path) -> bool:
    """Add missing governance columns without replacing row-level labels."""

    frame = pd.read_csv(path)
    fit, fit_end = provenance(path)
    changed = False
    if "fit_end" not in frame:
        frame.insert(0, "fit_end", fit_end)
        changed = True
    if "fit_provenance" not in frame:
        frame.insert(0, "fit_provenance", fit)
        changed = True
    if "portfolio_scope" not in frame:
        frame.insert(0, "portfolio_scope", PORTFOLIO)
        changed = True
    if changed:
        frame.to_csv(path, index=False)
    return changed


def main() -> None:
    changed = [path for path in Path("docs").glob("*.csv") if label_csv(path)]
    print(f"Labelled {len(changed)} documentation CSVs")


if __name__ == "__main__":
    main()
