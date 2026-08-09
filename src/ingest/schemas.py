"""Stable, explicit schemas for vendor-shaped raw mortgage files."""

from __future__ import annotations

from pyspark.sql.types import (
    DateType,
    DoubleType,
    IntegerType,
    StringType,
    StructField,
    StructType,
)


def acquisition_schema() -> StructType:
    """Return acquisition types needed for cohort, exposure, and risk segmentation."""

    return StructType(
        [
            StructField("loan_id", StringType(), True),
            StructField("origination_month", DateType(), True),
            StructField("original_upb", DoubleType(), True),
            StructField("original_term", IntegerType(), True),
            StructField("orig_interest_rate", DoubleType(), True),
            StructField("orig_score", IntegerType(), True),
            StructField("orig_ltv", DoubleType(), True),
            StructField("property_state", StringType(), True),
            StructField("occupancy_status", StringType(), True),
            StructField("channel", StringType(), True),
        ]
    )


def performance_schema() -> StructType:
    """Return monthly performance types needed for transitions, exits, and LGD."""

    return StructType(
        [
            StructField("loan_id", StringType(), True),
            StructField("as_of_month", DateType(), True),
            StructField("current_upb", DoubleType(), True),
            StructField("delinquency_status", IntegerType(), True),
            StructField("loan_age", IntegerType(), True),
            StructField("remaining_months", IntegerType(), True),
            StructField("zero_balance_code", StringType(), True),
            StructField("zero_balance_date", DateType(), True),
            StructField("modification_flag", StringType(), True),
            StructField("foreclosure_date", DateType(), True),
            StructField("disposition_date", DateType(), True),
            StructField("net_sales_proceeds", DoubleType(), True),
            StructField("foreclosure_costs", DoubleType(), True),
        ]
    )
