"""Lifetime expected credit loss for amortizing and revolving exposures.

For an amortizing loan, EAD is the projected contractual balance remaining in
each future month. A revolving credit card is different: exposure at default
also includes draws on the currently undrawn line. A card implementation must
therefore add ``credit_conversion_factor * undrawn_commitment`` to the drawn
balance. This mortgage-shaped synthetic implementation projects amortization,
but the card-job target makes that future CCF extension an explicit contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO
from itertools import permutations
from pathlib import Path

import fsspec
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.config import EngineConfig
from src.model.forecast import (
    CachedMatrixBuilder,
    ForecastResult,
    ForecastSegment,
    MatrixBuilder,
    forecast_segments,
)
from src.model.lgd import LGDModel, fit_lgd_model, lgd_validation_by_era

LOGGER = logging.getLogger(__name__)
PORTFOLIO_LABEL = "Local synthetic portfolio (25,000 loans)"


@dataclass(frozen=True)
class ECLSegment:
    """Forecast segment plus contractual amortization assumptions."""

    segment_id: str
    score_band: str
    vintage: str
    balance: float
    start_mob: int
    initial_state: dict[str, float]
    annual_interest_rate: float
    remaining_term_months: int

    def forecast_segment(self) -> ForecastSegment:
        return ForecastSegment(
            self.segment_id,
            self.score_band,
            self.vintage,
            self.balance,
            self.start_mob,
            self.initial_state,
        )


@dataclass(frozen=True)
class ECLResult:
    """Portfolio result with auditable monthly and segment decomposition."""

    lifetime_ecl: float
    outstanding_balance: float
    ecl_rate: float
    monthly: pd.DataFrame
    segments: pd.DataFrame
    by_score_band: pd.DataFrame
    by_vintage: pd.DataFrame
    forecast: ForecastResult


@dataclass(frozen=True)
class Milestone6Report:
    """Serializable M6 headline results and artifact locations."""

    lifetime_ecl: float
    outstanding_balance: float
    ecl_rate: float
    chain_ladder_ultimate_dollars: float
    chain_ladder_ultimate_rate: float
    ecl_minus_chain_ladder_dollars: float
    summary_path: str
    score_band_path: str
    vintage_path: str
    monthly_path: str
    plot_path: str
    lgd_validation_path: str
    lgd_coefficients_path: str
    reconciliation_path: str
    ground_truth_validation_path: str
    fit_comparison_path: str
    cutoff_clean_undiscounted_loss: float
    production_undiscounted_loss: float
    realized_post_cutoff_loss: float
    leakage_gap_dollars: float
    leakage_gap_percentage_points: float


def exposure_balance(upb_eom: pd.Series, upb_bom: pd.Series) -> pd.Series:
    """Use EOM exposure when observed and BOM explicitly at censored nulls."""

    exposure = pd.to_numeric(upb_eom, errors="coerce").fillna(
        pd.to_numeric(upb_bom, errors="coerce")
    )
    if exposure.isna().any():
        raise ValueError("Exposure is missing from both upb_eom and upb_bom")
    return exposure.clip(lower=0.0)


def amortization_schedule(
    balance: float,
    annual_interest_rate: float,
    remaining_term_months: int,
    horizon_months: int,
) -> np.ndarray:
    """Return beginning-of-month contractual EAD for each forecast month."""

    if balance < 0.0 or annual_interest_rate < 0.0:
        raise ValueError("Balance and interest rate must be nonnegative")
    if remaining_term_months < 1 or horizon_months < 1:
        raise ValueError("Term and horizon must be positive")
    monthly_rate = annual_interest_rate / 12.0
    if monthly_rate == 0.0:
        payment = balance / remaining_term_months
    else:
        payment = balance * monthly_rate / (1.0 - (1.0 + monthly_rate) ** -remaining_term_months)
    schedule = np.zeros(horizon_months, dtype=float)
    current = float(balance)
    for month in range(horizon_months):
        schedule[month] = current
        if month + 1 >= remaining_term_months:
            current = 0.0
            continue
        interest = current * monthly_rate
        current = max(current - max(payment - interest, 0.0), 0.0)
    return schedule


def _decomposition(frame: pd.DataFrame, group: str) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    for value, grouped in frame.groupby(group, sort=True):
        segment_opening = grouped.drop_duplicates("segment_id")["segment_balance"]
        balance = float(segment_opening.sum())
        pd_weight = grouped["marginal_pd"] * grouped["segment_balance"]
        default_exposure = grouped["marginal_pd"] * grouped["ead"]
        pd_total = float(pd_weight.sum())
        exposure_total = float(default_exposure.sum())
        records.append(
            {
                group: value,
                "outstanding_balance": balance,
                "lifetime_pd": pd_total / balance if balance else 0.0,
                "expected_default_exposure": exposure_total,
                "ead_at_default_pct_of_opening": (
                    exposure_total / pd_total if pd_total > 0.0 else 0.0
                ),
                "lgd_at_default": (
                    float((default_exposure * grouped["lgd"]).sum()) / exposure_total
                    if exposure_total > 0.0
                    else 0.0
                ),
                "undiscounted_loss": float(grouped["undiscounted_loss"].sum()),
                "lifetime_ecl": float(grouped["discounted_loss"].sum()),
                "ecl_rate": (float(grouped["discounted_loss"].sum()) / balance if balance else 0.0),
            }
        )
    return pd.DataFrame.from_records(records)


def calculate_lifetime_ecl(
    transition_model: MatrixBuilder,
    lgd_model: LGDModel,
    segments: list[ECLSegment],
    macro_path: pd.DataFrame,
    *,
    annual_discount_rate: float,
    max_mob: int | None = None,
) -> ECLResult:
    """Calculate discounted lifetime ECL from PD, EAD, and LGD paths."""

    if annual_discount_rate < 0.0:
        raise ValueError("Discount rate cannot be negative")
    if not segments:
        raise ValueError("At least one ECL segment is required")
    forecast = forecast_segments(
        transition_model,
        [segment.forecast_segment() for segment in segments],
        macro_path,
        max_mob=max_mob,
    )
    segment_lookup = {segment.segment_id: segment for segment in segments}
    detail: list[pd.DataFrame] = []
    hpi = macro_path["hpi_change_yoy"].to_numpy(dtype=float)
    for segment_id, path in forecast.segments.groupby("segment_id", sort=False):
        segment = segment_lookup[str(segment_id)]
        frame = path.sort_values("month").copy()
        frame["segment_balance"] = segment.balance
        frame["ead"] = amortization_schedule(
            segment.balance,
            segment.annual_interest_rate,
            segment.remaining_term_months,
            len(frame),
        )
        frame["marginal_pd"] = frame["marginal_chargeoff"].clip(lower=0.0)
        frame["lgd"] = [
            lgd_model.predict_lgd(segment.score_band, value) for value in hpi[: len(frame)]
        ]
        frame["discount_factor"] = (1.0 + annual_discount_rate) ** (
            frame["month"].to_numpy(dtype=float) / 12.0
        )
        frame["undiscounted_loss"] = frame["marginal_pd"] * frame["ead"] * frame["lgd"]
        frame["discounted_loss"] = frame["undiscounted_loss"] / frame["discount_factor"]
        detail.append(frame)
    detailed = pd.concat(detail, ignore_index=True)
    segment_summary = _decomposition(detailed, "segment_id")
    metadata = pd.DataFrame.from_records(
        [
            {
                "segment_id": segment.segment_id,
                "score_band": segment.score_band,
                "vintage": segment.vintage,
            }
            for segment in segments
        ]
    )
    segment_summary = segment_summary.merge(metadata, on="segment_id", validate="one_to_one")
    monthly = detailed.groupby(["month", "as_of_month"], as_index=False, dropna=False).agg(
        undiscounted_loss=("undiscounted_loss", "sum"),
        discounted_loss=("discounted_loss", "sum"),
    )
    weighted = detailed.assign(
        marginal_pd_dollars=detailed["marginal_pd"] * detailed["segment_balance"],
        expected_default_exposure=detailed["marginal_pd"] * detailed["ead"],
    )
    weights = weighted.groupby("month", as_index=False).agg(
        marginal_pd_dollars=("marginal_pd_dollars", "sum"),
        expected_default_exposure=("expected_default_exposure", "sum"),
    )
    monthly = monthly.merge(weights, on="month", validate="one_to_one")
    monthly["cumulative_discounted_loss"] = monthly["discounted_loss"].cumsum()
    outstanding = float(sum(segment.balance for segment in segments))
    lifetime_ecl = float(detailed["discounted_loss"].sum())
    return ECLResult(
        lifetime_ecl,
        outstanding,
        lifetime_ecl / outstanding if outstanding else 0.0,
        monthly,
        segment_summary,
        _decomposition(detailed, "score_band"),
        _decomposition(detailed, "vintage"),
        forecast,
    )


def plot_monthly_loss_path(
    monthly: pd.DataFrame,
    path: str | Path,
    *,
    fit_provenance: str = "unspecified",
) -> None:
    """Write monthly and cumulative discounted loss to a compact PNG."""

    figure, axis = plt.subplots(figsize=(10, 5))
    axis.bar(monthly["month"], monthly["discounted_loss"], label="Monthly ECL")
    axis.plot(
        monthly["month"],
        monthly["cumulative_discounted_loss"],
        color="black",
        linewidth=2,
        label="Cumulative ECL",
    )
    axis.set_xlabel("Forecast month")
    axis.set_ylabel("Discounted expected loss ($)")
    axis.set_title(
        f"Lifetime expected credit loss path\n{PORTFOLIO_LABEL} — {fit_provenance}"
    )
    axis.legend()
    figure.tight_layout()
    destination = str(path)
    if "://" in destination:
        buffer = BytesIO()
        figure.savefig(buffer, format="png", dpi=160)
        buffer.seek(0)
        with fsspec.open(destination, "wb") as stream:
            stream.write(buffer.getvalue())
    else:
        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(output, dpi=160)
    plt.close(figure)


def assign_score_band(scores: pd.Series, boundaries: tuple[int, ...]) -> pd.Series:
    """Apply the same inclusive integer labels as the Spark panel."""

    labels = [
        f"FICO_{lower:03d}-{upper - 1:03d}"
        for lower, upper in zip(boundaries[:-1], boundaries[1:], strict=True)
    ]
    return pd.cut(scores, bins=boundaries, labels=labels, right=False).astype("string")


def build_ecl_segments(
    panel: pd.DataFrame,
    acquisition: pd.DataFrame,
    config: EngineConfig,
    as_of_month: str | pd.Timestamp,
) -> list[ECLSegment]:
    """Create score/vintage cells from the latest observable loan state."""

    cutoff = pd.Timestamp(as_of_month)
    history = panel[pd.to_datetime(panel["as_of_month"]) <= cutoff].copy()
    if history.empty:
        raise ValueError("No portfolio observations exist at the CECL reporting date")
    latest = (
        history.sort_values(["loan_id", "as_of_month"]).groupby("loan_id", as_index=False).tail(1)
    )
    latest = latest[~latest["exit_reason"].isin(("ChargeOff", "Prepaid", "Repurchased"))]
    latest["exposure"] = exposure_balance(latest["upb_eom"], latest["upb_bom"])
    latest = latest[latest["exposure"] > 0.0].copy()
    source = latest.merge(
        acquisition[["loan_id", "original_term", "orig_interest_rate", "origination_month"]],
        on="loan_id",
        how="left",
        validate="one_to_one",
    )
    if source[["original_term", "orig_interest_rate"]].isna().any().any():
        raise ValueError("Portfolio snapshot is missing acquisition terms")
    source["score_band"] = assign_score_band(source["orig_score"], config.model.score_bands)
    source["vintage_year"] = source["vintage"].astype(str).str[:4]
    source["remaining_term"] = (source["original_term"] - source["months_on_book"]).clip(lower=1)
    segments: list[ECLSegment] = []
    for (score_band, vintage), frame in source.groupby(
        ["score_band", "vintage_year"], observed=True, sort=True
    ):
        balance = float(frame["exposure"].sum())
        weights = frame["exposure"] / balance
        states = {
            str(state): float(frame.loc[frame["delinquency_state"].eq(state), "exposure"].sum())
            / balance
            for state in frame["delinquency_state"].unique()
        }
        segment_id = f"{score_band}|{vintage}"
        segments.append(
            ECLSegment(
                segment_id,
                str(score_band),
                str(vintage),
                balance,
                int(round(float(np.average(frame["months_on_book"], weights=weights)))),
                states,
                float(np.average(frame["orig_interest_rate"], weights=weights)),
                max(1, int(round(float(np.average(frame["remaining_term"], weights=weights))))),
            )
        )
    return segments


def active_cutoff_panel(panel: pd.DataFrame, cutoff: str | pd.Timestamp) -> pd.DataFrame:
    """Keep histories only for loans observed economically active at cutoff."""

    cutoff_month = pd.Timestamp(cutoff)
    snapshot = panel[pd.to_datetime(panel["as_of_month"]).eq(cutoff_month)]
    active_ids = snapshot.loc[
        ~snapshot["exit_reason"].isin(("ChargeOff", "Prepaid", "Repurchased", "Censored")),
        "loan_id",
    ]
    return panel[panel["loan_id"].isin(active_ids)].copy()


def _with_fit_provenance(
    frame: pd.DataFrame, fit_provenance: str, fit_end: str | None
) -> pd.DataFrame:
    labelled = frame.copy()
    labelled.insert(0, "fit_end", fit_end or "full_available_history")
    labelled.insert(0, "fit_provenance", fit_provenance)
    return labelled


def _write_csv(frame: pd.DataFrame, path: str) -> None:
    frame = frame.copy()
    if "portfolio_scope" not in frame:
        frame.insert(0, "portfolio_scope", PORTFOLIO_LABEL)
    if "://" in path:
        with fsspec.open(path, "wt") as stream:
            frame.to_csv(stream, index=False)
        return
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output, index=False)


def _macro_with_lags(macro: pd.DataFrame, lags: tuple[int, ...]) -> pd.DataFrame:
    result = macro.sort_values("as_of_month").copy()
    for lag in lags:
        for column in ("unemployment_rate", "unemployment_change_3m", "hpi_change_yoy"):
            result[f"{column}_lag_{lag}"] = result[column].shift(lag)
    return result


def _transition_counts(panel: pd.DataFrame, config: EngineConfig) -> pd.DataFrame:
    frame = panel[
        ~panel["is_censored"].astype(bool) & panel["next_delinquency_state"].notna()
    ].copy()
    frame["score_band"] = assign_score_band(frame["orig_score"], config.model.score_bands)
    keys = [
        "delinquency_state",
        "next_delinquency_state",
        "months_on_book",
        "score_band",
        "as_of_month",
    ]
    return (
        frame.groupby(keys, observed=True, as_index=False)
        .size()
        .rename(columns={"size": "transition_count"})
    )


def _load_transition_counts(config: EngineConfig, panel: pd.DataFrame) -> pd.DataFrame:
    """Load M5's compact cells, with a diagnostic fallback for old local runs."""

    curated = config.paths.curated.rstrip("/\\")
    path = f"{curated}/transition_counts"
    if "://" in path or Path(path).exists():
        return pd.read_parquet(path)
    LOGGER.warning(
        "M5 transition counts were not found at %s; rebuilding them from the local "
        "panel for backward compatibility. Run the M5 Spark stage to persist the "
        "small aggregate before production M6 execution.",
        path,
    )
    return _transition_counts(panel, config)


def _shapley_loss_attribution(
    balance: float,
    actual: dict[str, float],
    projected: dict[str, float],
) -> dict[str, float]:
    """Attribute a multiplicative loss difference without imposing factor order."""

    factors = ("pd", "ead", "lgd")

    def loss(values: dict[str, float]) -> float:
        return balance * values["pd"] * values["ead"] * values["lgd"]

    contributions = {factor: 0.0 for factor in factors}
    orders = tuple(permutations(factors))
    for order in orders:
        values = dict(actual)
        for factor in order:
            before = loss(values)
            values[factor] = projected[factor]
            contributions[factor] += loss(values) - before
    return {factor: value / len(orders) for factor, value in contributions.items()}


def validate_forecast_against_ground_truth(
    panel: pd.DataFrame,
    monthly_forecast: pd.DataFrame,
    *,
    cutoff: str | pd.Timestamp,
    outstanding_balance: float,
) -> pd.DataFrame:
    """Compare undiscounted M6 loss with known post-cutoff loan outcomes."""

    required = {
        "loan_id",
        "as_of_month",
        "upb_bom",
        "upb_eom",
        "net_sales_proceeds",
        "foreclosure_costs",
        "exit_reason",
    }
    missing = required - set(panel.columns)
    if missing:
        raise ValueError(f"Ground-truth panel is missing columns: {sorted(missing)}")
    cutoff_month = pd.Timestamp(cutoff)
    forecast = monthly_forecast.copy()
    forecast["as_of_month"] = pd.to_datetime(forecast["as_of_month"])
    forecast_end = forecast["as_of_month"].max()
    history = panel.copy()
    history["as_of_month"] = pd.to_datetime(history["as_of_month"])
    snapshot = (
        history[history["as_of_month"] <= cutoff_month]
        .sort_values(["loan_id", "as_of_month"])
        .groupby("loan_id", as_index=False)
        .tail(1)
    )
    snapshot = snapshot[
        ~snapshot["exit_reason"].isin(("ChargeOff", "Prepaid", "Repurchased"))
    ].copy()
    snapshot["cutoff_exposure"] = exposure_balance(snapshot["upb_eom"], snapshot["upb_bom"])
    snapshot = snapshot[snapshot["cutoff_exposure"] > 0.0]
    snapshot_balance = float(snapshot["cutoff_exposure"].sum())
    if not np.isclose(snapshot_balance, outstanding_balance, atol=0.01, rtol=0.0):
        raise ValueError(
            f"Validation snapshot balance {snapshot_balance} does not reconcile to "
            f"forecast balance {outstanding_balance}"
        )

    defaults = history[
        (history["as_of_month"] > cutoff_month)
        & (history["as_of_month"] <= forecast_end)
        & history["exit_reason"].eq("ChargeOff")
    ].copy()
    defaults = defaults.merge(
        snapshot[["loan_id", "cutoff_exposure"]],
        on="loan_id",
        how="left",
        validate="one_to_one",
    )
    if defaults["cutoff_exposure"].isna().any():
        raise ValueError("A realized post-cutoff default is absent from the forecast snapshot")
    defaults["realized_net_loss"] = (
        defaults["upb_bom"] - (defaults["net_sales_proceeds"] - defaults["foreclosure_costs"])
    ).clip(lower=0.0)
    defaults["year"] = defaults["as_of_month"].dt.year
    forecast["year"] = forecast["as_of_month"].dt.year
    projected = forecast.groupby("year", as_index=False).agg(
        projected_pd_dollars=("marginal_pd_dollars", "sum"),
        projected_default_exposure=("expected_default_exposure", "sum"),
        projected_net_loss=("undiscounted_loss", "sum"),
    )
    realized = defaults.groupby("year", as_index=False).agg(
        realized_defaults=("loan_id", "size"),
        realized_pd_dollars=("cutoff_exposure", "sum"),
        realized_default_exposure=("upb_bom", "sum"),
        realized_net_loss=("realized_net_loss", "sum"),
    )
    annual = projected.merge(realized, on="year", how="outer").fillna(0.0)

    def record(period: str, frame: pd.DataFrame) -> dict[str, object]:
        projected_pd_dollars = float(frame["projected_pd_dollars"].sum())
        realized_pd_dollars = float(frame["realized_pd_dollars"].sum())
        projected_ead = float(frame["projected_default_exposure"].sum())
        realized_ead = float(frame["realized_default_exposure"].sum())
        projected_loss = float(frame["projected_net_loss"].sum())
        realized_loss = float(frame["realized_net_loss"].sum())
        projected_factors = {
            "pd": projected_pd_dollars / outstanding_balance,
            "ead": projected_ead / projected_pd_dollars if projected_pd_dollars else 0.0,
            "lgd": projected_loss / projected_ead if projected_ead else 0.0,
        }
        actual_factors = {
            "pd": realized_pd_dollars / outstanding_balance,
            "ead": realized_ead / realized_pd_dollars if realized_pd_dollars else 0.0,
            "lgd": realized_loss / realized_ead if realized_ead else 0.0,
        }
        attribution = _shapley_loss_attribution(
            outstanding_balance, actual_factors, projected_factors
        )
        error = projected_loss - realized_loss
        return {
            "period": period,
            "realized_defaults": int(frame["realized_defaults"].sum()),
            "projected_undiscounted_loss": projected_loss,
            "realized_undiscounted_net_loss": realized_loss,
            "error_dollars": error,
            "error_pct_of_realized": error / realized_loss if realized_loss else np.nan,
            "projected_pd": projected_factors["pd"],
            "realized_pd": actual_factors["pd"],
            "projected_ead_factor": projected_factors["ead"],
            "realized_ead_factor": actual_factors["ead"],
            "projected_lgd": projected_factors["lgd"],
            "realized_lgd": actual_factors["lgd"],
            "pd_error_contribution": attribution["pd"],
            "ead_error_contribution": attribution["ead"],
            "lgd_error_contribution": attribution["lgd"],
        }

    records = [record(str(int(row["year"])), pd.DataFrame([row])) for _, row in annual.iterrows()]
    records.append(record("Total", annual))
    result = pd.DataFrame.from_records(records)
    attributed = result[
        [
            "pd_error_contribution",
            "ead_error_contribution",
            "lgd_error_contribution",
        ]
    ].sum(axis=1)
    if not np.allclose(attributed, result["error_dollars"], atol=0.01, rtol=0.0):
        raise ValueError("PD/EAD/LGD error attribution does not reconcile")
    return result


def build_reconciliation_bridge(
    *,
    m4_ultimate: float,
    realized_before_cutoff: float,
    m4_original_balance: float,
    m6_undiscounted: float,
    m6_discounted: float,
    m6_outstanding_balance: float,
) -> pd.DataFrame:
    """Build an additive bridge from M4 ultimate loss to M6 lifetime ECL."""

    m4_future = m4_ultimate - realized_before_cutoff
    surviving_exposure_adjustment = 0.0
    roll_rate_model_uplift = m6_undiscounted - m4_future
    discounting_adjustment = m6_discounted - m6_undiscounted
    return pd.DataFrame.from_records(
        [
            {
                "step": "M4 chain-ladder ultimate",
                "adjustment_dollars": m4_ultimate,
                "subtotal_dollars": m4_ultimate,
                "loss_rate": m4_ultimate / m4_original_balance,
                "basis": "Undiscounted realized plus projected ultimate on original cohorts",
            },
            {
                "step": "Less: realized losses incurred through cutoff",
                "adjustment_dollars": -realized_before_cutoff,
                "subtotal_dollars": m4_future,
                "loss_rate": np.nan,
                "basis": "Observed cumulative net loss through 2018-12-01",
            },
            {
                "step": "Restriction to surviving exposure",
                "adjustment_dollars": surviving_exposure_adjustment,
                "subtotal_dollars": m4_future,
                "loss_rate": np.nan,
                "basis": (
                    "No separate adjustment: after incurred loss is removed, exited loans have "
                    "zero future marginal PD; another subtraction would double-count exits"
                ),
            },
            {
                "step": "Macro-conditioned roll-rate model versus M4 remaining loss",
                "adjustment_dollars": roll_rate_model_uplift,
                "subtotal_dollars": m6_undiscounted,
                "loss_rate": np.nan,
                "basis": (
                    f"M6 undiscounted is {m6_undiscounted / m4_future:.2f}x M4 remaining; "
                    "M4 backtest underprojects 2011+ cohorts by 125.61 bps ME / 63.51% MAPE"
                ),
            },
            {
                "step": "Discounting at 5% annual effective rate",
                "adjustment_dollars": discounting_adjustment,
                "subtotal_dollars": m6_discounted,
                "loss_rate": m6_discounted / m6_outstanding_balance,
                "basis": "Difference between undiscounted and discounted M6 monthly loss",
            },
        ]
    )


def _chain_ladder_reconciliation(config: EngineConfig, result: ECLResult) -> pd.DataFrame:
    ultimate = pd.read_csv(config.paths.vintage_table)
    original_balance = float(ultimate["original_balance"].sum())
    m4_ultimate = float(
        (
            ultimate["original_balance"] * ultimate["ultimate_rate_original_balance_chain_ladder"]
        ).sum()
    )
    realized = float(
        (ultimate["original_balance"] * ultimate["observed_rate_original_balance"]).sum()
    )
    return build_reconciliation_bridge(
        m4_ultimate=m4_ultimate,
        realized_before_cutoff=realized,
        m4_original_balance=original_balance,
        m6_undiscounted=float(result.monthly["undiscounted_loss"].sum()),
        m6_discounted=result.lifetime_ecl,
        m6_outstanding_balance=result.outstanding_balance,
    )


def build_fit_comparison(
    *,
    cutoff_result: ECLResult,
    production_result: ECLResult,
    realized_loss: float,
    chain_ladder_projection: float,
    cutoff_fit_end: str,
    production_fit_end: str | None,
) -> pd.DataFrame:
    """Build the four-way OOS comparison and quantify leakage advantage."""

    cutoff_loss = float(cutoff_result.monthly["undiscounted_loss"].sum())
    production_loss = float(production_result.monthly["undiscounted_loss"].sum())
    cutoff_error = cutoff_loss - realized_loss
    production_error = production_loss - realized_loss
    leakage_gap = abs(cutoff_error) - abs(production_error)
    leakage_gap_pp = 100.0 * (abs(cutoff_error) - abs(production_error)) / realized_loss
    rows = [
        {
            "measure": "Realized post-cutoff net loss",
            "fit_provenance": "realized_ground_truth",
            "fit_end": "not_applicable",
            "projected_or_realized_loss": realized_loss,
        },
        {
            "measure": "M6 macro-conditioned roll-rate",
            "fit_provenance": "cutoff_clean_out_of_sample",
            "fit_end": cutoff_fit_end,
            "projected_or_realized_loss": cutoff_loss,
        },
        {
            "measure": "M6 macro-conditioned roll-rate",
            "fit_provenance": "production_full_history_leaked_for_backtest",
            "fit_end": production_fit_end or "full_available_history",
            "projected_or_realized_loss": production_loss,
        },
        {
            "measure": "M4 chain-ladder remaining loss",
            "fit_provenance": "pre_cutoff_chain_ladder",
            "fit_end": cutoff_fit_end,
            "projected_or_realized_loss": chain_ladder_projection,
        },
    ]
    comparison = pd.DataFrame.from_records(rows)
    comparison["error_dollars"] = comparison["projected_or_realized_loss"] - realized_loss
    comparison["absolute_error_dollars"] = comparison["error_dollars"].abs()
    comparison["error_pct_of_realized"] = comparison["error_dollars"] / realized_loss
    comparison.loc[comparison["fit_provenance"].eq("realized_ground_truth"), [
        "error_dollars",
        "absolute_error_dollars",
        "error_pct_of_realized",
    ]] = np.nan
    comparison["leakage_gap_dollars"] = leakage_gap
    comparison["leakage_gap_percentage_points"] = leakage_gap_pp
    return comparison


def run_cecl(config: EngineConfig) -> Milestone6Report:
    """Run M6 from local pandas artifacts produced by the prior milestones."""

    from src.model.conditional import fit_conditional_models, transition_fit_sample

    output_root = Path(config.paths.output)
    panel = pd.read_parquet(output_root / "synthetic_panel.parquet")
    acquisition = pd.read_csv(
        Path(config.paths.raw_acquisition) / "synthetic_acquisition.txt",
        sep=config.ingest.delimiter,
        parse_dates=["origination_month"],
    )
    macro = pd.read_csv(config.paths.macro, parse_dates=["as_of_month"])
    macro_lagged = _macro_with_lags(macro, config.model.macro_lags)
    counts = _load_transition_counts(config, panel)
    panel["score_band"] = assign_score_band(panel["orig_score"], config.model.score_bands)
    cutoff = pd.Timestamp(config.model.vintage_analysis_as_of)
    macro_path = macro_lagged[macro_lagged["as_of_month"] > cutoff].head(
        config.synthetic.max_observation_months
    )
    active_panel = active_cutoff_panel(panel, cutoff)
    segments = build_ecl_segments(active_panel, acquisition, config, cutoff)
    fit_specs = {
        "cutoff_clean_out_of_sample": config.model.backtest_fit_end,
        "production_full_history_leaked_for_backtest": config.model.production_fit_end,
    }
    results: dict[str, ECLResult] = {}
    validations: list[pd.DataFrame] = []
    coefficients: list[pd.DataFrame] = []
    ground_truth_frames: list[pd.DataFrame] = []
    for provenance, fit_end in fit_specs.items():
        fit_counts = transition_fit_sample(counts, fit_end)
        fitted_transition_model = fit_conditional_models(fit_counts, macro_lagged, config)
        transition_model = CachedMatrixBuilder(fitted_transition_model)
        lgd_sample = panel[
            pd.to_datetime(panel["as_of_month"]) <= pd.Timestamp(fit_end)
        ] if fit_end is not None else panel
        lgd_model = fit_lgd_model(
            lgd_sample,
            fallback_lgd=config.model.fallback_lgd,
            score_bands=fitted_transition_model.score_bands,
        )
        result = calculate_lifetime_ecl(
            transition_model,
            lgd_model,
            segments,
            macro_path,
            annual_discount_rate=config.model.discount_rate_annual,
            max_mob=config.model.vintage_maturity_mob,
        )
        results[provenance] = result
        validations.append(
            _with_fit_provenance(lgd_validation_by_era(lgd_sample, lgd_model), provenance, fit_end)
        )
        coefficients.append(
            _with_fit_provenance(lgd_model.coefficient_table(), provenance, fit_end)
        )
        ground_truth_frames.append(
            _with_fit_provenance(
                validate_forecast_against_ground_truth(
                    active_panel,
                    result.monthly,
                    cutoff=cutoff,
                    outstanding_balance=result.outstanding_balance,
                ),
                provenance,
                fit_end,
            )
        )
    result = results["cutoff_clean_out_of_sample"]
    production_result = results["production_full_history_leaked_for_backtest"]
    ground_truth = pd.concat(ground_truth_frames, ignore_index=True)
    realized_loss = float(
        ground_truth.loc[
            ground_truth["fit_provenance"].eq("cutoff_clean_out_of_sample")
            & ground_truth["period"].eq("Total"),
            "realized_undiscounted_net_loss",
        ].iloc[0]
    )
    reconciliation = _chain_ladder_reconciliation(config, result)
    reconciliation = _with_fit_provenance(
        reconciliation, "cutoff_clean_out_of_sample", config.model.backtest_fit_end
    )
    chain = reconciliation.iloc[0]
    chain_ladder_projection = float(reconciliation.iloc[1]["subtotal_dollars"])
    comparison = build_fit_comparison(
        cutoff_result=result,
        production_result=production_result,
        realized_loss=realized_loss,
        chain_ladder_projection=chain_ladder_projection,
        cutoff_fit_end=config.model.backtest_fit_end,
        production_fit_end=config.model.production_fit_end,
    )
    summary_records = []
    for provenance, fit_end in fit_specs.items():
        fit_result = results[provenance]
        summary_records.append(
            {
                "as_of_month": cutoff,
                "segments": len(segments),
                "forecast_months": len(macro_path),
                "original_balance": float(acquisition["original_upb"].sum()),
                "outstanding_balance": fit_result.outstanding_balance,
                "lifetime_ecl": fit_result.lifetime_ecl,
                "ecl_rate": fit_result.ecl_rate,
                "undiscounted_lifetime_loss": float(
                    fit_result.monthly["undiscounted_loss"].sum()
                ),
                "fit_provenance": provenance,
                "fit_end": fit_end or "full_available_history",
            }
        )
    summary = pd.DataFrame.from_records(summary_records)
    _write_csv(summary, config.paths.ecl_summary)
    _write_csv(
        _with_fit_provenance(
            result.by_score_band, "cutoff_clean_out_of_sample", config.model.backtest_fit_end
        ),
        config.paths.ecl_by_score_band,
    )
    _write_csv(
        _with_fit_provenance(
            result.by_vintage, "cutoff_clean_out_of_sample", config.model.backtest_fit_end
        ),
        config.paths.ecl_by_vintage,
    )
    _write_csv(
        _with_fit_provenance(
            result.monthly, "cutoff_clean_out_of_sample", config.model.backtest_fit_end
        ),
        config.paths.ecl_monthly,
    )
    _write_csv(pd.concat(validations, ignore_index=True), config.paths.lgd_validation)
    _write_csv(pd.concat(coefficients, ignore_index=True), config.paths.lgd_coefficients)
    _write_csv(reconciliation, config.paths.ecl_reconciliation)
    _write_csv(ground_truth, config.paths.ecl_ground_truth_validation)
    _write_csv(comparison, config.paths.ecl_fit_comparison)
    plot_monthly_loss_path(
        result.monthly,
        config.paths.ecl_plot,
        fit_provenance="cutoff-clean fit through 2018-12",
    )
    cutoff_total = float(result.monthly["undiscounted_loss"].sum())
    production_total = float(production_result.monthly["undiscounted_loss"].sum())
    return Milestone6Report(
        result.lifetime_ecl,
        result.outstanding_balance,
        result.ecl_rate,
        float(chain["subtotal_dollars"]),
        float(chain["loss_rate"]),
        result.lifetime_ecl - float(chain["subtotal_dollars"]),
        config.paths.ecl_summary,
        config.paths.ecl_by_score_band,
        config.paths.ecl_by_vintage,
        config.paths.ecl_monthly,
        config.paths.ecl_plot,
        config.paths.lgd_validation,
        config.paths.lgd_coefficients,
        config.paths.ecl_reconciliation,
        config.paths.ecl_ground_truth_validation,
        config.paths.ecl_fit_comparison,
        cutoff_total,
        production_total,
        realized_loss,
        float(comparison["leakage_gap_dollars"].iloc[0]),
        float(comparison["leakage_gap_percentage_points"].iloc[0]),
    )
