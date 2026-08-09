from pathlib import Path

import pandas as pd

from infra.generate_synthetic import _write_vintage
from src.ingest.synthetic import SyntheticPortfolio


def test_vintage_chunks_are_written_as_flat_vendor_files(tmp_path: Path) -> None:
    portfolio = SyntheticPortfolio(
        acquisition=pd.DataFrame(
            {"loan_id": ["L1"], "censoring_date": [pd.Timestamp("2000-12-01")]}
        ),
        performance=pd.DataFrame({"loan_id": ["L1"], "current_upb": [100.0]}),
        macro=pd.DataFrame(),
        panel=pd.DataFrame(),
    )

    _write_vintage(portfolio, tmp_path, 2000)

    assert (tmp_path / "raw" / "acquisition" / "acquisition_2000.txt").is_file()
    assert (tmp_path / "raw" / "performance" / "performance_2000.txt").is_file()
    assert not list((tmp_path / "raw").glob("*/vintage_year=*"))
