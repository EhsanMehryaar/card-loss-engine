import pytest
from pyspark.sql import functions as F

from src.panel.build_panel import _integrity_summary, construct_panel


def _acquisition(spark, loan_ids: list[str]):
    values = ",".join(f"('{loan_id}')" for loan_id in loan_ids)
    return spark.sql(
        f"""
        SELECT loan_id,
               DATE '2010-01-01' AS origination_month,
               700 AS orig_score,
               75.0 AS orig_ltv,
               'TX' AS property_state,
               2010 AS vintage_year
        FROM VALUES {values} AS t(loan_id)
        """
    )


def _performance(spark, rows: list[tuple[str, str]]):
    values = ",".join(f"('{loan_id}', '{code}')" for loan_id, code in rows)
    return spark.sql(
        f"""
        SELECT loan_id,
               DATE '2010-01-01' AS as_of_month,
               200000.0 AS current_upb,
               0 AS delinquency_status,
               0 AS loan_age,
               zero_balance_code,
               CAST(NULL AS DATE) AS disposition_date,
               0.0 AS net_sales_proceeds,
               0.0 AS foreclosure_costs
        FROM VALUES {values} AS t(loan_id, zero_balance_code)
        """
    )


def _macro(spark, config):
    columns = [
        F.to_date(F.lit("2010-01-01")).alias("as_of_month"),
        F.lit(5.0).alias("unemployment_rate"),
        F.lit(0.1).alias("unemployment_change_3m"),
        F.lit(0.02).alias("hpi_change_yoy"),
    ]
    for lag in config.model.macro_lags:
        for name in (
            "unemployment_rate",
            "unemployment_change_3m",
            "hpi_change_yoy",
        ):
            columns.append(F.lit(None).cast("double").alias(f"{name}_lag_{lag}"))
    return spark.range(1).select(*columns)


def test_all_configured_zero_balance_codes_map_to_expected_exit(spark, quality_config) -> None:
    rows = [(f"L{index}", code) for index, code in enumerate(quality_config.states.zero_balance_codes)]
    panel = construct_panel(
        _acquisition(spark, [loan_id for loan_id, _ in rows]),
        _performance(spark, rows),
        _macro(spark, quality_config),
        quality_config,
    )
    actual = {
        row["zero_balance_code"]: (row["delinquency_state"], row["exit_reason"])
        for row in panel.join(_performance(spark, rows).select("loan_id", "zero_balance_code"), "loan_id")
        .select("zero_balance_code", "delinquency_state", "exit_reason")
        .collect()
    }

    assert actual == {
        code: (state, state)
        for code, state in quality_config.states.zero_balance_codes.items()
    }


def test_unknown_zero_balance_code_fails_with_named_quality_error(
    spark, quality_config
) -> None:
    panel = construct_panel(
        _acquisition(spark, ["UNKNOWN"]),
        _performance(spark, [("UNKNOWN", "99")]),
        _macro(spark, quality_config),
        quality_config,
    )

    with pytest.raises(ValueError, match="unknown_zero_balance_code_rows"):
        _integrity_summary(panel, quality_config)
