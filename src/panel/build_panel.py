"""Construct the validated account-month state-transition panel with Spark."""

from __future__ import annotations

from dataclasses import dataclass

from pyspark import StorageLevel
from pyspark.sql import Column, DataFrame, SparkSession, Window
from pyspark.sql import functions as F

from src.config import EngineConfig
from src.ingest.raw_to_parquet import (
    DatasetFileSummary,
    SparkUiSummary,
    enforce_quality_rates,
    parquet_file_summary,
    spark_ui_summary,
)
from src.panel.macro import MACRO_VALUE_COLUMNS, join_macro_features, load_macro_features


@dataclass(frozen=True)
class PanelReport:
    """Counts, distributions, integrity evidence, and Spark metrics for M3."""

    row_count: int
    columns: tuple[str, ...]
    state_distribution_by_mob_band: dict[str, dict[str, int]]
    loan_counts_by_exit_reason: dict[str, int]
    transition_counts: dict[str, dict[str, int]]
    censored_terminal_rows: int
    censored_terminal_null_next_rows: int
    is_censored_null_rows: int
    vendor_mob_mismatch_rows: int
    vendor_mob_mismatch_rate: float
    output_path: str
    output_files: DatasetFileSummary
    spark_ui: SparkUiSummary


def _bucket(column: Column, boundaries: tuple[int, ...], prefix: str) -> Column:
    result: Column | None = None
    for lower, upper in zip(boundaries[:-1], boundaries[1:], strict=True):
        label = f"{prefix}{lower:03d}-{upper - 1:03d}"
        condition = (column >= F.lit(lower)) & (column < F.lit(upper))
        result = F.when(condition, F.lit(label)) if result is None else result.when(
            condition, F.lit(label)
        )
    if result is None:
        raise ValueError("Band boundaries must contain at least two values")
    return result


def _mapped_terminal_state(config: EngineConfig) -> Column:
    entries: list[Column] = []
    for code, state in config.states.zero_balance_codes.items():
        entries.extend([F.lit(code), F.lit(state)])
    return F.element_at(F.create_map(*entries), F.trim(F.col("zero_balance_code")))


def _delinquency_state(config: EngineConfig) -> Column:
    mapped_terminal_state = _mapped_terminal_state(config)
    state = F.when(mapped_terminal_state.isNotNull(), mapped_terminal_state).when(
        F.col("delinquency_status")
        >= F.lit(config.states.dpd_thresholds["ChargeOff"]),
        F.lit("ChargeOff"),
    )
    delinquent_states = [
        name
        for name in config.states.ordered
        if name in config.states.dpd_thresholds and name != "ChargeOff"
    ]
    for name in sorted(
        delinquent_states,
        key=lambda item: config.states.dpd_thresholds[item],
        reverse=True,
    ):
        state = state.when(
            F.col("delinquency_status") >= F.lit(config.states.dpd_thresholds[name]),
            F.lit(name),
        )
    return state.otherwise(F.lit("Current"))


def construct_panel(
    acquisition: DataFrame,
    performance: DataFrame,
    macro: DataFrame,
    config: EngineConfig,
) -> DataFrame:
    """Create one account-month row with vendor-derived states and exits.

    ``upb_bom`` is the vendor-reported balance for month ``t``. ``upb_eom`` is
    the following month's BOM balance for continuing accounts, zero for observed
    prepayment/default exits, and null for censored rows because no later balance
    was observed.
    """

    joined = performance.drop("vintage_year").join(
        acquisition.select(
            "loan_id",
            "origination_month",
            "orig_score",
            "orig_ltv",
            "property_state",
            "vintage_year",
        ),
        "loan_id",
        "inner",
    )
    chronological = Window.partitionBy("loan_id").orderBy("as_of_month")
    reverse_chronological = Window.partitionBy("loan_id").orderBy(
        F.col("as_of_month").desc()
    )
    per_loan = Window.partitionBy("loan_id")
    computed_mob = (
        (F.year("as_of_month") - F.year("origination_month")) * F.lit(12)
        + F.month("as_of_month")
        - F.month("origination_month")
    )
    zero_code = F.trim(F.col("zero_balance_code"))
    mapped_terminal_state = _mapped_terminal_state(config)
    is_last = F.row_number().over(reverse_chronological) == 1
    exit_reason = (
        F.when(mapped_terminal_state.isNotNull(), mapped_terminal_state)
        .when(
            F.col("delinquency_status")
            >= F.lit(config.states.dpd_thresholds["ChargeOff"]),
            F.lit("ChargeOff"),
        )
        .when(
            is_last
            & (F.col("zero_balance_code").isNull() | (zero_code == F.lit("")))
            & F.col("disposition_date").isNull(),
            F.lit("Censored"),
        )
    )
    prepared = (
        joined.withColumn("vintage", F.date_format("origination_month", "yyyy-MM"))
        .withColumn("vendor_loan_age", F.col("loan_age"))
        .withColumn("months_on_book", computed_mob)
        .withColumn(
            "_vendor_mob_mismatch",
            ~F.col("vendor_loan_age").eqNullSafe(F.col("months_on_book")),
        )
        .withColumn(
            "_unknown_zero_balance_code",
            F.col("zero_balance_code").isNotNull()
            & (zero_code != F.lit(""))
            & mapped_terminal_state.isNull(),
        )
        .withColumn(
            "mob_band", _bucket(F.col("months_on_book"), config.model.mob_bands, "MOB_")
        )
        .withColumn(
            "score_band", _bucket(F.col("orig_score"), config.model.score_bands, "FICO_")
        )
        .withColumn("delinquency_state", _delinquency_state(config))
        .withColumn("exit_reason", exit_reason)
        .withColumn(
            "is_censored",
            F.coalesce(F.col("exit_reason") == F.lit("Censored"), F.lit(False)),
        )
        .withColumn("previous_delinquency_state", F.lag("delinquency_state").over(chronological))
        .withColumn("_observed_next_state", F.lead("delinquency_state").over(chronological))
        .withColumn(
            "next_delinquency_state",
            F.when(F.col("exit_reason").isNotNull(), F.lit(None).cast("string")).otherwise(
                F.col("_observed_next_state")
            ),
        )
        .withColumn("upb_bom", F.col("current_upb"))
        .withColumn("_next_upb_bom", F.lead("current_upb").over(chronological))
        .withColumn(
            "upb_eom",
            F.when(F.col("exit_reason").isin(*config.states.absorbing), F.lit(0.0))
            .when(F.col("is_censored"), F.lit(None).cast("double"))
            .otherwise(F.col("_next_upb_bom")),
        )
        .withColumn("_previous_month", F.lag("as_of_month").over(chronological))
        .withColumn(
            "_terminal_count", F.sum(F.col("exit_reason").isNotNull().cast("int")).over(per_loan)
        )
        .withColumn("_is_last", is_last)
        .withColumn(
            "_duplicate_key_count",
            F.count(F.lit(1)).over(Window.partitionBy("loan_id", "as_of_month")),
        )
    )
    with_macro = join_macro_features(prepared, macro)
    ordered_columns = [
        "loan_id",
        "as_of_month",
        "vintage",
        "vintage_year",
        "months_on_book",
        "vendor_loan_age",
        "mob_band",
        "delinquency_state",
        "previous_delinquency_state",
        "next_delinquency_state",
        "upb_bom",
        "upb_eom",
        "orig_score",
        "score_band",
        "orig_ltv",
        "property_state",
        "net_sales_proceeds",
        "foreclosure_costs",
        "exit_reason",
        "is_censored",
        *MACRO_VALUE_COLUMNS,
        *[
            f"{column}_lag_{lag}"
            for lag in config.model.macro_lags
            for column in MACRO_VALUE_COLUMNS
        ],
    ]
    integrity_columns = [
        "_vendor_mob_mismatch",
        "_unknown_zero_balance_code",
        "_previous_month",
        "_terminal_count",
        "_is_last",
        "_duplicate_key_count",
    ]
    return with_macro.select(*ordered_columns, *integrity_columns)


def _integrity_summary(
    panel: DataFrame, config: EngineConfig
) -> tuple[int, int, int, int, int, float]:
    checks = panel.agg(
        F.count(F.lit(1)).alias("row_count"),
        F.sum(
            F.when(
                F.col("_previous_month").isNotNull()
                & (F.months_between("as_of_month", "_previous_month") != F.lit(1.0)),
                1,
            ).otherwise(0)
        ).alias("non_contiguous_months"),
        F.sum(F.when(F.col("_vendor_mob_mismatch"), 1).otherwise(0)).alias(
            "vendor_mob_mismatches"
        ),
        F.sum(F.when(F.col("_unknown_zero_balance_code"), 1).otherwise(0)).alias(
            "unknown_zero_balance_code_rows"
        ),
        F.sum(F.when(F.col("_terminal_count") != 1, 1).otherwise(0)).alias(
            "invalid_terminal_count_rows"
        ),
        F.sum(
            F.when(F.col("exit_reason").isNotNull() & ~F.col("_is_last"), 1).otherwise(0)
        ).alias("nonfinal_terminal_rows"),
        F.sum(
            F.when(
                F.col("exit_reason").isNotNull()
                & F.col("next_delinquency_state").isNotNull(),
                1,
            ).otherwise(0)
        ).alias("terminal_rows_with_next_state"),
        F.sum(F.when(F.col("_duplicate_key_count") != 1, 1).otherwise(0)).alias(
            "duplicate_key_rows"
        ),
        F.sum(F.when(F.col("mob_band").isNull(), 1).otherwise(0)).alias(
            "unbanded_mob_rows"
        ),
        F.sum(F.when(F.col("score_band").isNull(), 1).otherwise(0)).alias(
            "unbanded_score_rows"
        ),
        *[
            F.sum(F.when(F.col(column).isNull(), 1).otherwise(0)).alias(
                f"missing_macro_{column}"
            )
            for column in MACRO_VALUE_COLUMNS
        ],
        F.sum(F.when(F.col("is_censored"), 1).otherwise(0)).alias("censored_rows"),
        F.sum(F.when(F.col("is_censored").isNull(), 1).otherwise(0)).alias(
            "is_censored_null_rows"
        ),
        F.sum(
            F.when(
                F.col("is_censored") & F.col("next_delinquency_state").isNull(), 1
            ).otherwise(0)
        ).alias("censored_null_next_rows"),
    ).first()
    allowed = {
        "row_count",
        "censored_rows",
        "censored_null_next_rows",
        "vendor_mob_mismatches",
    }
    violations = {
        name: int(value)
        for name, value in checks.asDict().items()
        if name not in allowed and value
    }
    if violations:
        raise ValueError(f"Panel failed account-month integrity checks: {violations}")
    row_count = int(checks["row_count"])
    mismatch_count = int(checks["vendor_mob_mismatches"])
    mismatch_rate = mismatch_count / row_count if row_count else 0.0
    enforce_quality_rates(
        {"panel_vendor_loan_age_mismatch": mismatch_count},
        {"panel_vendor_loan_age_mismatch": mismatch_rate},
        mismatch_rate,
        config,
        context="Panel vendor MOB",
    )
    return (
        row_count,
        int(checks["censored_rows"]),
        int(checks["censored_null_next_rows"]),
        int(checks["is_censored_null_rows"]),
        mismatch_count,
        mismatch_rate,
    )


def _nested_counts(
    rows: list[object], row_name: str, column_name: str, count_name: str
) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        result.setdefault(str(row[row_name]), {})[str(row[column_name])] = int(row[count_name])
    return result


def run_panel(spark: SparkSession, config: EngineConfig) -> PanelReport:
    """Read curated data, build the M3 modeling panel, validate, and persist it."""

    curated = config.paths.curated.rstrip("/\\")
    acquisition_path = f"{curated}/acquisition"
    performance_path = f"{curated}/performance"
    output_path = f"{curated}/panel"
    acquisition = spark.read.parquet(acquisition_path)
    performance = spark.read.parquet(performance_path)
    macro = load_macro_features(spark, config)
    internal_panel = construct_panel(acquisition, performance, macro, config).persist(
        StorageLevel.MEMORY_AND_DISK
    )
    try:
        (
            row_count,
            censored_rows,
            censored_null_next_rows,
            is_censored_null_rows,
            vendor_mob_mismatch_rows,
            vendor_mob_mismatch_rate,
        ) = _integrity_summary(internal_panel, config)
        helper_columns = [column for column in internal_panel.columns if column.startswith("_")]
        panel = internal_panel.drop(*helper_columns)
        state_rows = panel.groupBy("mob_band", "delinquency_state").count().collect()
        state_distribution = _nested_counts(
            state_rows, "mob_band", "delinquency_state", "count"
        )
        exits = {state: 0 for state in (*config.states.absorbing, "Censored")}
        exits.update({
            str(row["exit_reason"]): int(row["count"])
            for row in panel.where(F.col("exit_reason").isNotNull())
            .groupBy("exit_reason")
            .count()
            .collect()
        })
        transition_rows = (
            panel.where(F.col("next_delinquency_state").isNotNull())
            .groupBy("delinquency_state", "next_delinquency_state")
            .count()
            .collect()
        )
        transitions = _nested_counts(
            transition_rows, "delinquency_state", "next_delinquency_state", "count"
        )
        panel.repartition(F.col("vintage_year")).write.mode("overwrite").partitionBy(
            "vintage_year"
        ).parquet(output_path)
        output_files = parquet_file_summary(spark, output_path)
        summary = spark_ui_summary(spark)
        return PanelReport(
            row_count=row_count,
            columns=tuple(panel.columns),
            state_distribution_by_mob_band=state_distribution,
            loan_counts_by_exit_reason=exits,
            transition_counts=transitions,
            censored_terminal_rows=censored_rows,
            censored_terminal_null_next_rows=censored_null_next_rows,
            is_censored_null_rows=is_censored_null_rows,
            vendor_mob_mismatch_rows=vendor_mob_mismatch_rows,
            vendor_mob_mismatch_rate=vendor_mob_mismatch_rate,
            output_path=output_path,
            output_files=output_files,
            spark_ui=summary,
        )
    finally:
        internal_panel.unpersist(blocking=False)
