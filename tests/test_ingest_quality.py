from pathlib import Path

import pytest

from src.ingest.raw_to_parquet import (
    ACQUISITION_EXCLUSION_REASONS,
    PERFORMANCE_EXCLUSION_REASONS,
    QualitySummary,
    _acquisition_quality,
    _quality_summary,
    enforce_quality_policy,
)
from src.ingest.schemas import acquisition_schema


def _row(loan_id: str, score: int = 700) -> str:
    return f"{loan_id}|2010-01-01|200000|360|0.05|{score}|75|TX|P|R"


def _read_fixture(spark, path: Path, rows: list[str]):
    header = "|".join(field.name for field in acquisition_schema().fields)
    path.write_text("\n".join([header, *rows]), encoding="utf-8")
    return (
        spark.read.option("sep", "|")
        .option("header", "true")
        .option("dateFormat", "yyyy-MM-dd")
        .option("mode", "FAILFAST")
        .schema(acquisition_schema())
        .csv(str(path))
    )


def _clean_performance_summary() -> QualitySummary:
    counts = {reason: 0 for reason in PERFORMANCE_EXCLUSION_REASONS}
    return QualitySummary(201, 0, counts, {reason: 0.0 for reason in counts})


def test_corrupted_fixture_allows_rate_limited_row_below_threshold(
    spark, quality_config, tmp_path
) -> None:
    rows = [_row(f"L{index:04d}") for index in range(200)] + [_row("BAD_SCORE", 999)]
    checked = _acquisition_quality(_read_fixture(spark, tmp_path / "rate_limited.txt", rows))
    acquisition = _quality_summary(checked, ACQUISITION_EXCLUSION_REASONS)

    overall_rate = enforce_quality_policy(
        acquisition, _clean_performance_summary(), quality_config
    )

    assert acquisition.reason_counts["acquisition_invalid_score"] == 1
    assert acquisition.reason_rates["acquisition_invalid_score"] < 0.01
    assert overall_rate < 0.02


def test_corrupted_fixture_rejects_fatal_duplicate(spark, quality_config, tmp_path) -> None:
    rows = [_row(f"L{index:04d}") for index in range(199)] + [
        _row("DUPLICATE"),
        _row("DUPLICATE"),
    ]
    checked = _acquisition_quality(_read_fixture(spark, tmp_path / "fatal.txt", rows))
    acquisition = _quality_summary(checked, ACQUISITION_EXCLUSION_REASONS)

    with pytest.raises(ValueError, match="fatal acquisition_duplicate_loan_id=2"):
        enforce_quality_policy(acquisition, _clean_performance_summary(), quality_config)
