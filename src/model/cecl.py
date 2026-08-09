"""Lifetime expected credit loss for amortizing and revolving exposures.

For an amortizing loan, EAD is the projected contractual balance remaining in
each future month. A revolving credit card is different: exposure at default
also includes draws on the currently undrawn line. A card implementation must
therefore add ``credit_conversion_factor * undrawn_commitment`` to the drawn
balance. This mortgage-shaped synthetic implementation projects amortization,
but the card-job target makes that future CCF extension an explicit contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import fsspec
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.config import EngineConfig
from src.model.forecast import ForecastResult, ForecastSegment, MatrixBuilder, forecast_segments
from src.model.lgd import LGDModel, fit_lgd_model, lgd_validation_by_era


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
        payment = balance * monthly_rate / (
            1.0 - (1.0 + monthly_rate) ** -remaining_term_months
        )
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
                "ead_at_default": (
                    float((grouped["marginal_pd"] * grouped["ead"]).sum())
                    / float(grouped["marginal_pd"].sum())
                    if float(grouped["marginal_pd"].sum()) > 0.0
                    else 0.0
                ),
                "lgd_at_default": (
                    float((default_exposure * grouped["lgd"]).sum()) / exposure_total
                    if exposure_total > 0.0
                    else 0.0
                ),
                "undiscounted_loss": float(grouped["undiscounted_loss"].sum()),
                "lifetime_ecl": float(grouped["discounted_loss"].sum()),
                "ecl_rate": (
                    float(grouped["discounted_loss"].sum()) / balance if balance else 0.0
                ),
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
    monthly = detailed.groupby(
        ["month", "as_of_month"], as_index=False, dropna=False
    ).agg(
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


def plot_monthly_loss_path(monthly: pd.DataFrame, path: str | Path) -> None:
    """Write monthly and cumulative discounted loss to a compact PNG."""

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
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
    axis.set_title("Lifetime expected credit loss path")
    axis.legend()
    figure.tight_layout()
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
    latest = history.sort_values(["loan_id", "as_of_month"]).groupby(
        "loan_id", as_index=False
    ).tail(1)
    latest = latest[~latest["exit_reason"].isin(("ChargeOff", "Prepaid", "Repurchased"))]
    latest["exposure"] = exposure_balance(latest["upb_eom"], latest["upb_bom"])
    latest = latest[latest["exposure"] > 0.0].copy()
    source = latest.merge(
        acquisition[
            ["loan_id", "original_term", "orig_interest_rate", "origination_month"]
        ],
        on="loan_id",
        how="left",
        validate="one_to_one",
    )
    if source[["original_term", "orig_interest_rate"]].isna().any().any():
        raise ValueError("Portfolio snapshot is missing acquisition terms")
    source["score_band"] = assign_score_band(
        source["orig_score"], config.model.score_bands
    )
    source["vintage_year"] = source["vintage"].astype(str).str[:4]
    source["remaining_term"] = (
        source["original_term"] - source["months_on_book"]
    ).clip(lower=1)
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


def _write_csv(frame: pd.DataFrame, path: str) -> None:
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
    return frame.groupby(keys, observed=True, as_index=False).size().rename(
        columns={"size": "transition_count"}
    )


def _chain_ladder_reconciliation(config: EngineConfig, result: ECLResult) -> pd.DataFrame:
    ultimate = pd.read_csv(config.paths.vintage_table)
    loss = (
        ultimate["original_balance"]
        * ultimate["ultimate_rate_original_balance_chain_ladder"]
    )
    chain_dollars = float(loss.sum())
    chain_balance = float(ultimate["original_balance"].sum())
    chain_rate = chain_dollars / chain_balance if chain_balance else 0.0
    return pd.DataFrame.from_records(
        [
            {
                "measure": "M6 lifetime ECL",
                "loss_dollars": result.lifetime_ecl,
                "denominator_dollars": result.outstanding_balance,
                "loss_rate": result.ecl_rate,
                "scope": "Discounted future expected loss on exposure outstanding at cutoff",
            },
            {
                "measure": "M4 chain-ladder ultimate",
                "loss_dollars": chain_dollars,
                "denominator_dollars": chain_balance,
                "loss_rate": chain_rate,
                "scope": "Undiscounted projected ultimate loss on original cohort balances",
            },
        ]
    )


def run_cecl(config: EngineConfig) -> Milestone6Report:
    """Run M6 from local pandas artifacts produced by the prior milestones."""

    from src.model.conditional import fit_conditional_models

    output_root = Path(config.paths.output)
    panel = pd.read_parquet(output_root / "synthetic_panel.parquet")
    acquisition = pd.read_csv(
        Path(config.paths.raw_acquisition) / "synthetic_acquisition.txt",
        sep=config.ingest.delimiter,
        parse_dates=["origination_month"],
    )
    macro = pd.read_csv(config.paths.macro, parse_dates=["as_of_month"])
    macro_lagged = _macro_with_lags(macro, config.model.macro_lags)
    counts = _transition_counts(panel, config)
    transition_model = fit_conditional_models(counts, macro_lagged, config)
    panel["score_band"] = assign_score_band(panel["orig_score"], config.model.score_bands)
    lgd_model = fit_lgd_model(
        panel,
        fallback_lgd=config.model.fallback_lgd,
        score_bands=transition_model.score_bands,
    )
    cutoff = pd.Timestamp(config.model.vintage_analysis_as_of)
    macro_path = macro_lagged[macro_lagged["as_of_month"] > cutoff].head(
        config.synthetic.max_observation_months
    )
    segments = build_ecl_segments(panel, acquisition, config, cutoff)
    result = calculate_lifetime_ecl(
        transition_model,
        lgd_model,
        segments,
        macro_path,
        annual_discount_rate=config.model.discount_rate_annual,
        max_mob=config.model.vintage_maturity_mob,
    )
    validation = lgd_validation_by_era(panel, lgd_model)
    reconciliation = _chain_ladder_reconciliation(config, result)
    chain = reconciliation.iloc[1]
    summary = pd.DataFrame.from_records(
        [
            {
                "as_of_month": cutoff,
                "segments": len(segments),
                "forecast_months": len(macro_path),
                "outstanding_balance": result.outstanding_balance,
                "lifetime_ecl": result.lifetime_ecl,
                "ecl_rate": result.ecl_rate,
            }
        ]
    )
    _write_csv(summary, config.paths.ecl_summary)
    _write_csv(result.by_score_band, config.paths.ecl_by_score_band)
    _write_csv(result.by_vintage, config.paths.ecl_by_vintage)
    _write_csv(result.monthly, config.paths.ecl_monthly)
    _write_csv(validation, config.paths.lgd_validation)
    _write_csv(lgd_model.coefficient_table(), config.paths.lgd_coefficients)
    _write_csv(reconciliation, config.paths.ecl_reconciliation)
    plot_monthly_loss_path(result.monthly, config.paths.ecl_plot)
    return Milestone6Report(
        result.lifetime_ecl,
        result.outstanding_balance,
        result.ecl_rate,
        float(chain["loss_dollars"]),
        float(chain["loss_rate"]),
        result.lifetime_ecl - float(chain["loss_dollars"]),
        config.paths.ecl_summary,
        config.paths.ecl_by_score_band,
        config.paths.ecl_by_vintage,
        config.paths.ecl_monthly,
        config.paths.ecl_plot,
        config.paths.lgd_validation,
        config.paths.lgd_coefficients,
        config.paths.ecl_reconciliation,
    )
