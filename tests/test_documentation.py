"""Final artifact-governance checks."""

from pathlib import Path

import pandas as pd


def test_every_documentation_csv_has_scope_and_fit_provenance() -> None:
    csvs = sorted(Path("docs").glob("*.csv"))
    assert csvs
    for path in csvs:
        columns = set(pd.read_csv(path, nrows=1).columns)
        assert {"portfolio_scope", "fit_provenance", "fit_end"} <= columns, path
