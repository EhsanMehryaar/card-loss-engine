"""Classic vintage loss curves and actuarial development-factor projections.

Spark is used only to aggregate account-month records to a small
``(vintage, months_on_book)`` table. Curve construction, chain-ladder fitting,
projection, portfolio summaries, and plotting are deliberately single-node.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import fsspec
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D
from pyspark.sql import DataFrame as SparkDataFrame
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from src.config import EngineConfig
from src.ingest.raw_to_parquet import SparkUiSummary, spark_ui_summary

MONTHLY_VALUE_COLUMNS = (
    "accounts_active",
    "chargeoff_accounts",
    "balance_active",
    "gross_chargeoff_dollars",
    "recovery_dollars",
    "net_chargeoff_dollars",
    "prepay_dollars",
    "repurchase_dollars",
)
RATE_COLUMNS = {
    "original_balance": "cumulative_loss_rate_original_balance",
    "average_outstanding": "cumulative_loss_rate_average_outstanding",
}


@dataclass(frozen=True)
class LossDecomposition:
    """Observed components whose product equals observed net loss/original balance."""

    original_accounts: int
    observed_defaults: int
    observed_default_rate: float
    average_ead_fraction_original_balance: float
    realized_average_lgd: float
    observed_net_loss_rate: float
    ultimate_development_multiplier: float


@dataclass(frozen=True)
class VintageRunReport:
    """Headline M4 outputs and Spark aggregation metrics."""

    aggregated_rows: int
    cohort_grain: str
    observed_vintages: int
    projected_vintages: int
    portfolio_ultimate_original_chain_ladder: float
    portfolio_ultimate_original_scaled_average: float
    portfolio_ultimate_average_outstanding_chain_ladder: float
    portfolio_ultimate_average_outstanding_scaled_average: float
    crisis_2006_2008_mean_ultimate: float
    post_2011_mean_ultimate: float
    crisis_to_post_2011_ratio: float
    plot_path: str
    ultimate_table_path: str
    annual_aggregated_rows: int
    annual_observed_vintages: int
    annual_projected_vintages: int
    annual_portfolio_ultimate_original_chain_ladder: float
    annual_portfolio_ultimate_original_scaled_average: float
    annual_portfolio_ultimate_average_outstanding_chain_ladder: float
    annual_portfolio_ultimate_average_outstanding_scaled_average: float
    annual_ultimate_table_path: str
    loss_decomposition: LossDecomposition
    projection_accuracy: dict[str, dict[str, float | int | None]]
    realized_lgd_by_era: dict[str, dict[str, float | int]]
    projection_accuracy_table_path: str
    spark_ui: SparkUiSummary


@dataclass
class VintageCurveModel:
    """Fitted development patterns and observed vintage loss triangle."""

    curves: pd.DataFrame
    maturity_mob: int
    completed_vintages: tuple[str, ...]
    development_factors: dict[str, pd.DataFrame]
    completed_average_curves: dict[str, pd.Series]
    cohort_grain: str

    def project_vintage_to_ultimate(self, vintage: str) -> pd.DataFrame:
        """Project one vintage with chain-ladder and scaled-average sensitivity.

        Observed cells are always retained exactly. Cells after the vintage's
        maximum observed MOB are explicit projections; no masked cell is treated
        as a zero observation during fitting.
        """

        subset = self.curves[self.curves["vintage"] == vintage].copy()
        if subset.empty:
            raise KeyError(f"Unknown vintage: {vintage}")
        subset = subset.sort_values("months_on_book").reset_index(drop=True)
        max_mob = int(subset.loc[subset["is_observed"], "months_on_book"].max())
        output = subset[["vintage", "months_on_book", "is_observed"]].copy()
        output["is_projected"] = ~output["is_observed"]

        for denominator, rate_column in RATE_COLUMNS.items():
            observed = subset[rate_column].to_numpy(dtype=float)
            chain = observed.copy()
            scaled = observed.copy()
            current = float(observed[max_mob])
            factors = self.development_factors[denominator].set_index("age")[
                "development_factor"
            ]
            for age in range(max_mob, self.maturity_mob):
                factor = float(factors.get(age, 1.0))
                if not np.isfinite(factor):
                    factor = 1.0
                current = max(current * max(factor, 1.0), current)
                chain[age + 1] = current

            average_curve = self.completed_average_curves[denominator]
            average_at_observation = float(average_curve.get(max_mob, np.nan))
            if np.isfinite(average_at_observation) and average_at_observation > 0:
                scale = float(observed[max_mob]) / average_at_observation
            elif float(observed[max_mob]) == 0.0:
                scale = 0.0
            else:
                scale = 1.0
            last_scaled = float(observed[max_mob])
            for age in range(max_mob + 1, self.maturity_mob + 1):
                average_value = float(average_curve.get(age, last_scaled))
                last_scaled = max(average_value * scale, last_scaled)
                scaled[age] = last_scaled

            output[f"observed_rate_{denominator}"] = observed
            output[f"chain_ladder_rate_{denominator}"] = chain
            output[f"scaled_average_rate_{denominator}"] = scaled
        return output

    def ultimate_table(self) -> pd.DataFrame:
        """Return observed maturity and both projected ultimate rates by vintage."""

        records: list[dict[str, object]] = []
        completed = set(self.completed_vintages)
        for vintage in sorted(self.curves["vintage"].unique()):
            source = self.curves[
                (self.curves["vintage"] == vintage) & self.curves["is_observed"]
            ].sort_values("months_on_book")
            projection = self.project_vintage_to_ultimate(vintage)
            last = projection.iloc[-1]
            source_last = source.iloc[-1]
            records.append(
                {
                    "vintage": vintage,
                    "max_observed_mob": int(source_last["months_on_book"]),
                    "status": "Observed" if vintage in completed else "Projected",
                    "original_accounts": int(source_last["original_accounts"]),
                    "original_balance": float(source_last["original_balance"]),
                    "average_outstanding_balance": float(
                        source_last["average_outstanding_balance"]
                    ),
                    "observed_rate_original_balance": float(
                        source_last[RATE_COLUMNS["original_balance"]]
                    ),
                    "ultimate_rate_original_balance_chain_ladder": float(
                        last["chain_ladder_rate_original_balance"]
                    ),
                    "ultimate_rate_original_balance_scaled_average": float(
                        last["scaled_average_rate_original_balance"]
                    ),
                    "observed_rate_average_outstanding": float(
                        source_last[RATE_COLUMNS["average_outstanding"]]
                    ),
                    "ultimate_rate_average_outstanding_chain_ladder": float(
                        last["chain_ladder_rate_average_outstanding"]
                    ),
                    "ultimate_rate_average_outstanding_scaled_average": float(
                        last["scaled_average_rate_average_outstanding"]
                    ),
                }
            )
        return pd.DataFrame.from_records(records)

    def portfolio_ultimate_loss(
        self,
        denominator: str = "original_balance",
        method: str = "chain_ladder",
    ) -> float:
        """Return a balance-weighted portfolio ultimate loss rate.

        Original balance is the primary forecast denominator because it is fixed
        at origination and comparable across cohorts. Average outstanding is
        retained as a portfolio-experience sensitivity.
        """

        if denominator not in RATE_COLUMNS:
            raise ValueError(f"Unsupported denominator: {denominator}")
        if method not in {"chain_ladder", "scaled_average"}:
            raise ValueError(f"Unsupported projection method: {method}")
        table = self.ultimate_table()
        weight_column = (
            "original_balance"
            if denominator == "original_balance"
            else "average_outstanding_balance"
        )
        rate_column = f"ultimate_rate_{denominator}_{method}"
        weights = table[weight_column].to_numpy(dtype=float)
        rates = table[rate_column].to_numpy(dtype=float)
        return float(np.average(rates, weights=weights))


def _cohort_label(date_column: F.Column, grain: str) -> F.Column:
    """Return a stable cohort label at the configured calendar grain."""

    if grain == "monthly":
        return F.date_format(date_column, "yyyy-MM")
    if grain == "quarterly":
        return F.concat(
            F.date_format(date_column, "yyyy"),
            F.lit("-Q"),
            F.quarter(date_column).cast("string"),
        )
    if grain == "annual":
        return F.date_format(date_column, "yyyy")
    raise ValueError(f"Unsupported vintage cohort grain: {grain}")


def aggregate_vintage_spark(
    spark: SparkSession,
    config: EngineConfig,
    cohort_grain: str | None = None,
    apply_reporting_cutoff: bool = True,
) -> SparkDataFrame:
    """Aggregate account-month exposure and realized losses to vintage/MOB.

    Net charge-off is gross defaulted BOM UPB less net recovery proceeds and is
    floored at zero at the vintage-month level so anomalous recoveries cannot
    create a negative credit loss.
    """

    grain = cohort_grain or config.model.vintage_cohort_grain
    curated = config.paths.curated.rstrip("/\\")
    panel = spark.read.parquet(f"{curated}/panel")
    if apply_reporting_cutoff:
        panel = panel.where(
            F.col("as_of_month")
            <= F.lit(config.model.vintage_analysis_as_of).cast("date")
        )
    panel = (
        panel
        .withColumn("_source_vintage", F.col("vintage"))
        .withColumn(
            "vintage",
            _cohort_label(
                F.to_date(F.concat(F.col("vintage"), F.lit("-01"))), grain
            ),
        )
    )
    acquisition = spark.read.parquet(f"{curated}/acquisition").withColumn(
        "vintage", _cohort_label(F.col("origination_month"), grain)
    )
    original_balance = acquisition.groupBy("vintage").agg(
        F.sum("original_upb").alias("original_balance"),
        F.count(F.lit(1)).cast("long").alias("original_accounts"),
    )
    chargeoff = F.col("exit_reason") == F.lit("ChargeOff")
    grouped = panel.groupBy("vintage", "months_on_book").agg(
        F.sum(F.when(F.col("upb_bom") > 0, 1).otherwise(0)).cast("long").alias(
            "accounts_active"
        ),
        F.sum(F.when(chargeoff, 1).otherwise(0)).cast("long").alias(
            "chargeoff_accounts"
        ),
        F.sum("upb_bom").alias("balance_active"),
        F.sum(F.when(chargeoff, F.col("upb_bom")).otherwise(0.0)).alias(
            "gross_chargeoff_dollars"
        ),
        F.sum(
            F.when(
                chargeoff,
                F.col("net_sales_proceeds") - F.col("foreclosure_costs"),
            ).otherwise(0.0)
        ).alias("recovery_dollars"),
        F.sum(
            F.when(F.col("exit_reason") == "Prepaid", F.col("upb_bom")).otherwise(
                0.0
            )
        ).alias("prepay_dollars"),
        F.sum(
            F.when(
                F.col("exit_reason") == "Repurchased", F.col("upb_bom")
            ).otherwise(0.0)
        ).alias("repurchase_dollars"),
    )
    maxima = (
        panel.groupBy("vintage", "_source_vintage")
        .agg(F.max("months_on_book").alias("_source_max_observed_mob"))
        .groupBy("vintage")
        .agg(F.min("_source_max_observed_mob").alias("max_observed_mob"))
    )
    return (
        grouped.withColumn(
            "net_chargeoff_dollars",
            F.greatest(
                F.col("gross_chargeoff_dollars") - F.col("recovery_dollars"),
                F.lit(0.0),
            ),
        )
        .join(original_balance, "vintage", "inner")
        .join(maxima, "vintage", "inner")
        .where(F.col("months_on_book") <= F.col("max_observed_mob"))
        .select(
            "vintage",
            "months_on_book",
            *MONTHLY_VALUE_COLUMNS,
            "original_balance",
            "original_accounts",
            "max_observed_mob",
        )
        .orderBy("vintage", "months_on_book")
    )


def build_vintage_curves(
    aggregated: pd.DataFrame, maturity_mob: int
) -> pd.DataFrame:
    """Build observed cumulative curves with explicit undefined future cells."""

    required = {
        "vintage",
        "months_on_book",
        *MONTHLY_VALUE_COLUMNS,
        "original_balance",
        "original_accounts",
        "max_observed_mob",
    }
    missing = required - set(aggregated.columns)
    if missing:
        raise ValueError(f"Vintage aggregation is missing columns: {sorted(missing)}")
    source = aggregated.copy()
    source["vintage"] = source["vintage"].astype(str)
    source["months_on_book"] = source["months_on_book"].astype(int)
    if (source["net_chargeoff_dollars"] < 0).any():
        raise ValueError("net_chargeoff_dollars cannot be negative")
    vintage_attributes = (
        source.groupby("vintage", as_index=False)
        .agg(
            original_balance=("original_balance", "first"),
            original_accounts=("original_accounts", "first"),
            max_observed_mob=("max_observed_mob", "max"),
        )
        .set_index("vintage")
    )
    grid = pd.MultiIndex.from_product(
        [sorted(source["vintage"].unique()), range(maturity_mob + 1)],
        names=["vintage", "months_on_book"],
    ).to_frame(index=False)
    curves = grid.merge(
        source.drop(
            columns=["original_balance", "original_accounts", "max_observed_mob"]
        ),
        on=["vintage", "months_on_book"],
        how="left",
        indicator=True,
    )
    curves["original_balance"] = curves["vintage"].map(
        vintage_attributes["original_balance"]
    )
    curves["original_accounts"] = curves["vintage"].map(
        vintage_attributes["original_accounts"]
    ).astype(int)
    curves["max_observed_mob"] = curves["vintage"].map(
        vintage_attributes["max_observed_mob"]
    ).astype(int)
    curves["is_observed"] = (
        (curves["_merge"] == "both")
        & (curves["months_on_book"] <= curves["max_observed_mob"])
    )
    internal_gaps = curves[
        (curves["months_on_book"] <= curves["max_observed_mob"])
        & ~curves["is_observed"]
    ]
    if not internal_gaps.empty:
        sample = internal_gaps[["vintage", "months_on_book"]].head(10).to_dict("records")
        raise ValueError(f"Observed vintage triangle contains internal MOB gaps: {sample}")
    curves = curves.drop(columns="_merge").sort_values(
        ["vintage", "months_on_book"], ignore_index=True
    )
    curves["cumulative_net_chargeoff_dollars"] = curves.groupby("vintage")[
        "net_chargeoff_dollars"
    ].transform(lambda values: values.fillna(0.0).cumsum())
    curves["average_outstanding_balance"] = curves.groupby("vintage")[
        "balance_active"
    ].transform(lambda values: values.expanding().mean())
    curves[RATE_COLUMNS["original_balance"]] = (
        curves["cumulative_net_chargeoff_dollars"] / curves["original_balance"]
    )
    curves[RATE_COLUMNS["average_outstanding"]] = (
        curves["cumulative_net_chargeoff_dollars"]
        / curves["average_outstanding_balance"].replace(0.0, np.nan)
    )
    undefined_columns = [
        *MONTHLY_VALUE_COLUMNS,
        "cumulative_net_chargeoff_dollars",
        "average_outstanding_balance",
        *RATE_COLUMNS.values(),
    ]
    curves.loc[~curves["is_observed"], undefined_columns] = np.nan
    curves[RATE_COLUMNS["original_balance"]] = curves[
        RATE_COLUMNS["original_balance"]
    ].fillna(0.0).where(curves["is_observed"])
    curves[RATE_COLUMNS["average_outstanding"]] = curves[
        RATE_COLUMNS["average_outstanding"]
    ].fillna(0.0).where(curves["is_observed"])
    return curves


def compute_development_factors(
    curves: pd.DataFrame,
    value_column: str,
    maturity_mob: int,
    completed_vintages: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    """Compute volume-weighted age-to-age factors using observed cells only."""

    if completed_vintages is None:
        completed_vintages = tuple(
            curves.loc[
                curves["max_observed_mob"] >= maturity_mob, "vintage"
            ].unique()
        )
    eligible = curves[curves["vintage"].isin(completed_vintages)]
    records: list[dict[str, object]] = []
    for age in range(maturity_mob):
        current = eligible[
            (eligible["months_on_book"] == age) & eligible["is_observed"]
        ][["vintage", value_column]].rename(columns={value_column: "current"})
        following = eligible[
            (eligible["months_on_book"] == age + 1) & eligible["is_observed"]
        ][["vintage", value_column]].rename(columns={value_column: "following"})
        pairs = current.merge(following, on="vintage", how="inner").dropna()
        denominator = float(pairs["current"].sum())
        numerator = float(pairs["following"].sum())
        if denominator > 0:
            factor = max(numerator / denominator, 1.0)
        elif numerator == 0:
            factor = 1.0
        else:
            factor = np.nan
        records.append(
            {
                "age": age,
                "development_factor": factor,
                "contributing_vintages": len(pairs),
                "denominator": denominator,
                "numerator": numerator,
            }
        )
    return pd.DataFrame.from_records(records)


def fit_vintage_model(
    curves: pd.DataFrame, maturity_mob: int, cohort_grain: str = "monthly"
) -> VintageCurveModel:
    """Fit completed-vintage chain-ladder factors and average maturity shapes."""

    completed = tuple(
        sorted(
            curves.loc[curves["max_observed_mob"] >= maturity_mob, "vintage"].unique()
        )
    )
    if not completed:
        raise ValueError("No fully seasoned vintages are available for development fitting")
    completed_rows = curves[
        curves["vintage"].isin(completed) & curves["is_observed"]
    ]
    loss_factors = compute_development_factors(
        curves,
        "cumulative_net_chargeoff_dollars",
        maturity_mob,
        completed,
    )
    factors = {
        "original_balance": loss_factors.copy(),
        "average_outstanding": loss_factors.copy(),
    }
    averages = {
        denominator: completed_rows.groupby("months_on_book")[rate_column].mean()
        for denominator, rate_column in RATE_COLUMNS.items()
    }
    return VintageCurveModel(
        curves, maturity_mob, completed, factors, averages, cohort_grain
    )


def plot_vintage_curves(model: VintageCurveModel, path: str) -> None:
    """Save observed solid and chain-ladder projected dashed vintage curves."""

    figure, axis = plt.subplots(figsize=(14, 8))
    completed = set(model.completed_vintages)
    for vintage in sorted(model.curves["vintage"].unique()):
        projected = model.project_vintage_to_ultimate(vintage)
        year = int(vintage[:4])
        if 2006 <= year <= 2008:
            color, alpha, width = "#c0392b", 0.72, 1.35
        elif vintage in completed:
            color, alpha, width = "#7f8c8d", 0.23, 0.75
        else:
            color, alpha, width = "#2980b9", 0.28, 0.8
        observed = projected[projected["is_observed"]]
        axis.plot(
            observed["months_on_book"],
            observed["observed_rate_original_balance"] * 100.0,
            color=color,
            alpha=alpha,
            linewidth=width,
        )
        future = projected[
            projected["months_on_book"] >= int(observed["months_on_book"].max())
        ]
        if len(future) > 1:
            axis.plot(
                future["months_on_book"],
                future["chain_ladder_rate_original_balance"] * 100.0,
                color=color,
                alpha=alpha,
                linewidth=width,
                linestyle="--",
            )
    axis.set(
        title=(
            "Cumulative Net Loss Rate by Origination Vintage "
            f"({model.cohort_grain.title()} Cohorts)"
        ),
        xlabel="Months on book",
        ylabel="Cumulative net loss / original balance (%)",
    )
    axis.grid(alpha=0.2)
    axis.legend(
        handles=[
            Line2D([0], [0], color="#c0392b", label="2006–2008 vintages"),
            Line2D([0], [0], color="#7f8c8d", label="Other mature vintages"),
            Line2D([0], [0], color="#2980b9", label="Incomplete vintages"),
            Line2D([0], [0], color="black", linestyle="--", label="Projected"),
        ],
        loc="upper left",
    )
    figure.tight_layout()
    if "://" in path:
        with fsspec.open(path, "wb") as stream:
            figure.savefig(stream, format="png", dpi=160)
    else:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(output, dpi=160)
    plt.close(figure)


def write_ultimate_table(table: pd.DataFrame, path: str) -> None:
    """Persist the auditable observed/projected vintage ultimate table."""

    if "://" in path:
        with fsspec.open(path, "wt") as stream:
            table.to_csv(stream, index=False)
    else:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        table.to_csv(output, index=False)


def loss_decomposition(
    aggregated: pd.DataFrame, portfolio_ultimate_rate: float
) -> LossDecomposition:
    """Decompose observed net loss into frequency, EAD, and realized LGD.

    The portfolio-weighted identity is exact:
    ``default rate × average EAD fraction × realized LGD = observed loss rate``.
    Ultimate development is shown separately because chain-ladder projects loss,
    not default counts or LGD independently.
    """

    attributes = aggregated.groupby("vintage", as_index=False).agg(
        original_accounts=("original_accounts", "first"),
        original_balance=("original_balance", "first"),
    )
    original_accounts = int(attributes["original_accounts"].sum())
    original_balance = float(attributes["original_balance"].sum())
    observed_defaults = int(aggregated["chargeoff_accounts"].sum())
    gross_chargeoff = float(aggregated["gross_chargeoff_dollars"].sum())
    net_chargeoff = float(aggregated["net_chargeoff_dollars"].sum())
    default_rate = observed_defaults / original_accounts
    average_ead_fraction = (
        (gross_chargeoff / observed_defaults)
        / (original_balance / original_accounts)
        if observed_defaults
        else 0.0
    )
    realized_lgd = net_chargeoff / gross_chargeoff if gross_chargeoff else 0.0
    observed_loss_rate = net_chargeoff / original_balance
    component_product = default_rate * average_ead_fraction * realized_lgd
    if not np.isclose(component_product, observed_loss_rate, rtol=1e-12, atol=1e-12):
        raise ValueError("Loss decomposition does not reconcile to observed net loss")
    return LossDecomposition(
        original_accounts=original_accounts,
        observed_defaults=observed_defaults,
        observed_default_rate=default_rate,
        average_ead_fraction_original_balance=average_ead_fraction,
        realized_average_lgd=realized_lgd,
        observed_net_loss_rate=observed_loss_rate,
        ultimate_development_multiplier=(
            portfolio_ultimate_rate / observed_loss_rate
            if observed_loss_rate
            else 1.0
        ),
    )


def _cohort_era(years: pd.Series) -> pd.Series:
    return pd.Series(
        np.select(
            [years < 2008, years <= 2010],
            ["pre-2008", "2008-2010"],
            default="2011+",
        ),
        index=years.index,
    )


def backtest_projected_vintages(
    projected_ultimate: pd.DataFrame,
    realized_aggregated: pd.DataFrame,
    maturity_mob: int,
) -> tuple[pd.DataFrame, dict[str, dict[str, float | int | None]]]:
    """Compare cutoff-date projections with subsequently realized ultimate loss."""

    realized_curves = build_vintage_curves(realized_aggregated, maturity_mob)
    realized = realized_curves[
        realized_curves["months_on_book"].eq(maturity_mob)
        & realized_curves["is_observed"]
    ][
        [
            "vintage",
            RATE_COLUMNS["original_balance"],
            RATE_COLUMNS["average_outstanding"],
        ]
    ].rename(
        columns={
            RATE_COLUMNS["original_balance"]: "realized_rate_original_balance",
            RATE_COLUMNS["average_outstanding"]: (
                "realized_rate_average_outstanding"
            ),
        }
    )
    projected = projected_ultimate[projected_ultimate["status"].eq("Projected")]
    comparison = projected.merge(realized, on="vintage", how="inner", validate="one_to_one")
    if len(comparison) != len(projected):
        raise ValueError("Not every projected vintage has a realized ultimate")
    years = comparison["vintage"].str[:4].astype(int)
    comparison["era"] = _cohort_era(years)
    for method in ("chain_ladder", "scaled_average"):
        prediction = f"ultimate_rate_original_balance_{method}"
        error = f"error_{method}"
        comparison[error] = comparison[prediction] - comparison[
            "realized_rate_original_balance"
        ]
        comparison[f"absolute_error_{method}"] = comparison[error].abs()
        comparison[f"absolute_percentage_error_{method}"] = (
            comparison[f"absolute_error_{method}"]
            / comparison["realized_rate_original_balance"].replace(0.0, np.nan)
        )

    metrics: dict[str, dict[str, float | int | None]] = {}
    for era in ("overall", "pre-2008", "2008-2010", "2011+"):
        subset = comparison if era == "overall" else comparison[comparison["era"].eq(era)]
        for method in ("chain_ladder", "scaled_average"):
            key = f"{era}:{method}"
            if subset.empty:
                metrics[key] = {
                    "cohorts": 0,
                    "me": None,
                    "mae": None,
                    "mape": None,
                    "mape_nonzero_cohorts": 0,
                }
                continue
            ape = subset[f"absolute_percentage_error_{method}"].dropna()
            metrics[key] = {
                "cohorts": len(subset),
                "me": float(subset[f"error_{method}"].mean()),
                "mae": float(subset[f"absolute_error_{method}"].mean()),
                "mape": float(ape.mean()) if not ape.empty else None,
                "mape_nonzero_cohorts": len(ape),
            }
    return comparison, metrics


def realized_lgd_by_era_spark(
    spark: SparkSession, config: EngineConfig
) -> dict[str, dict[str, float | int]]:
    """Return gross-loss-weighted realized LGD from the full known history."""

    curated = config.paths.curated.rstrip("/\\")
    panel = spark.read.parquet(f"{curated}/panel").where(
        F.col("exit_reason") == F.lit("ChargeOff")
    )
    year = F.substring("vintage", 1, 4).cast("int")
    era = (
        F.when(year < 2008, F.lit("pre-2008"))
        .when(year <= 2010, F.lit("2008-2010"))
        .otherwise(F.lit("2011+"))
    )
    net_loss = F.greatest(
        F.col("upb_bom")
        - (F.col("net_sales_proceeds") - F.col("foreclosure_costs")),
        F.lit(0.0),
    )
    rows = (
        panel.withColumn("era", era)
        .groupBy("era")
        .agg(
            F.count(F.lit(1)).alias("defaults"),
            F.sum("upb_bom").alias("gross_chargeoff_dollars"),
            F.sum(net_loss).alias("net_chargeoff_dollars"),
        )
        .collect()
    )
    result: dict[str, dict[str, float | int]] = {}
    for row in rows:
        gross = float(row["gross_chargeoff_dollars"])
        net = float(row["net_chargeoff_dollars"])
        result[str(row["era"])] = {
            "defaults": int(row["defaults"]),
            "gross_chargeoff_dollars": gross,
            "net_chargeoff_dollars": net,
            "realized_lgd": net / gross if gross else 0.0,
        }
    return result


def run_vintage(spark: SparkSession, config: EngineConfig) -> VintageRunReport:
    """Run the Spark aggregation and single-node vintage baseline end to end."""

    if config.model.vintage_primary_denominator not in RATE_COLUMNS:
        raise ValueError(
            "model.vintage_primary_denominator must be original_balance or "
            "average_outstanding"
        )
    grain = config.model.vintage_cohort_grain
    aggregated_spark = aggregate_vintage_spark(spark, config, grain)
    aggregated = aggregated_spark.toPandas()
    annual_aggregated = aggregate_vintage_spark(spark, config, "annual").toPandas()
    realized_aggregated = aggregate_vintage_spark(
        spark, config, grain, apply_reporting_cutoff=False
    ).toPandas()
    era_lgd = realized_lgd_by_era_spark(spark, config)
    summary = spark_ui_summary(spark)
    curves = build_vintage_curves(aggregated, config.model.vintage_maturity_mob)
    model = fit_vintage_model(curves, config.model.vintage_maturity_mob, grain)
    ultimate = model.ultimate_table()
    annual_curves = build_vintage_curves(
        annual_aggregated, config.model.vintage_maturity_mob
    )
    annual_model = fit_vintage_model(
        annual_curves, config.model.vintage_maturity_mob, "annual"
    )
    annual_ultimate = annual_model.ultimate_table()
    projection_comparison, projection_metrics = backtest_projected_vintages(
        ultimate, realized_aggregated, config.model.vintage_maturity_mob
    )
    plot_vintage_curves(model, config.paths.vintage_plot)
    write_ultimate_table(ultimate, config.paths.vintage_table)
    write_ultimate_table(annual_ultimate, config.paths.vintage_annual_table)
    write_ultimate_table(
        projection_comparison, config.paths.vintage_backtest_table
    )
    years = ultimate["vintage"].str[:4].astype(int)
    crisis_mean = float(
        ultimate.loc[
            years.between(2006, 2008),
            "ultimate_rate_original_balance_chain_ladder",
        ].mean()
    )
    post_mean = float(
        ultimate.loc[
            years >= 2011, "ultimate_rate_original_balance_chain_ladder"
        ].mean()
    )
    if not crisis_mean > post_mean:
        raise ValueError(
            "Upstream signal check failed: 2006–2008 ultimate loss rates do not exceed 2011+"
        )
    primary_original_ultimate = model.portfolio_ultimate_loss()
    decomposition = loss_decomposition(aggregated, primary_original_ultimate)
    return VintageRunReport(
        aggregated_rows=len(aggregated),
        cohort_grain=grain,
        observed_vintages=len(model.completed_vintages),
        projected_vintages=int(ultimate["status"].eq("Projected").sum()),
        portfolio_ultimate_original_chain_ladder=primary_original_ultimate,
        portfolio_ultimate_original_scaled_average=model.portfolio_ultimate_loss(
            method="scaled_average"
        ),
        portfolio_ultimate_average_outstanding_chain_ladder=model.portfolio_ultimate_loss(
            denominator="average_outstanding"
        ),
        portfolio_ultimate_average_outstanding_scaled_average=model.portfolio_ultimate_loss(
            denominator="average_outstanding", method="scaled_average"
        ),
        crisis_2006_2008_mean_ultimate=crisis_mean,
        post_2011_mean_ultimate=post_mean,
        crisis_to_post_2011_ratio=crisis_mean / post_mean if post_mean else np.inf,
        plot_path=config.paths.vintage_plot,
        ultimate_table_path=config.paths.vintage_table,
        annual_aggregated_rows=len(annual_aggregated),
        annual_observed_vintages=len(annual_model.completed_vintages),
        annual_projected_vintages=int(
            annual_ultimate["status"].eq("Projected").sum()
        ),
        annual_portfolio_ultimate_original_chain_ladder=(
            annual_model.portfolio_ultimate_loss()
        ),
        annual_portfolio_ultimate_original_scaled_average=(
            annual_model.portfolio_ultimate_loss(method="scaled_average")
        ),
        annual_portfolio_ultimate_average_outstanding_chain_ladder=(
            annual_model.portfolio_ultimate_loss(denominator="average_outstanding")
        ),
        annual_portfolio_ultimate_average_outstanding_scaled_average=(
            annual_model.portfolio_ultimate_loss(
                denominator="average_outstanding", method="scaled_average"
            )
        ),
        annual_ultimate_table_path=config.paths.vintage_annual_table,
        loss_decomposition=decomposition,
        projection_accuracy=projection_metrics,
        realized_lgd_by_era=era_lgd,
        projection_accuracy_table_path=config.paths.vintage_backtest_table,
        spark_ui=summary,
    )
