"""Deterministic synthetic mortgage data for credit-loss model development.

The generator creates complete loan histories rather than independent rows. Its
state transitions respond to seasoning, borrower credit quality, and a shared
unemployment path, giving later roll-rate models meaningful signal to recover.

Timing convention: the transition *into* calendar month ``t`` is drawn with
month ``t`` macro values and month ``t`` months-on-book (MOB). The resulting
delinquency state is end-of-month, while ``current_upb`` in the vendor-shaped
performance output is beginning-of-month. Milestone 5 must use this exact
covariate alignment when fitting conditional transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import EngineConfig

ACTIVE_STATES = ("Current", "DPD30", "DPD60", "DPD90", "DPD120", "DPD150")
ALL_STATES = ACTIVE_STATES + ("ChargeOff", "Prepaid", "Repurchased")
STATE_TO_INDEX = {state: index for index, state in enumerate(ALL_STATES)}
DPD_BY_STATE = np.array([0, 30, 60, 90, 120, 150, 180, 0, 0], dtype=np.int16)


@dataclass(frozen=True)
class SyntheticPortfolio:
    """Fannie-shaped acquisition/performance files plus modeling panel."""

    acquisition: pd.DataFrame
    performance: pd.DataFrame
    macro: pd.DataFrame
    panel: pd.DataFrame


def _rng(seed: int, stream: int) -> np.random.Generator:
    """Return an independent, prefix-stable random stream for one data feature."""

    return np.random.default_rng(np.random.SeedSequence([seed, stream]))


def simulate_unemployment(
    start_month: pd.Timestamp,
    end_month: pd.Timestamp,
    start_rate: float,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Simulate a persistent unemployment path with recessionary shocks.

    The 2008–2010 and 2020 stresses create shared systematic risk so synthetic
    delinquency is correlated across borrowers rather than purely idiosyncratic.
    """

    months = pd.date_range(start_month, end_month, freq="MS")
    values = np.empty(len(months), dtype=np.float64)
    rate = start_rate
    for index, month in enumerate(months):
        recession_impulse = 0.0
        if pd.Timestamp("2008-04-01") <= month <= pd.Timestamp("2009-10-01"):
            recession_impulse += 0.20
        if pd.Timestamp("2020-03-01") <= month <= pd.Timestamp("2020-06-01"):
            recession_impulse += 1.35
        rate += 0.055 * (4.8 - rate) + recession_impulse + rng.normal(0.0, 0.055)
        rate = float(np.clip(rate, 3.0, 14.5))
        values[index] = rate
    macro = pd.DataFrame({"as_of_month": months, "unemployment_rate": values})
    macro["unemployment_change_3m"] = macro["unemployment_rate"].diff(3).fillna(0.0)
    macro["hpi_change_yoy"] = np.clip(
        0.055 - 0.018 * (values - 4.8) + rng.normal(0, 0.008, len(months)),
        -0.20,
        0.15,
    )
    return macro


def _draw_next_states(
    states: np.ndarray,
    mobs: np.ndarray,
    scores: np.ndarray,
    unemployment: float,
    unemployment_change: float,
    uniforms: np.ndarray,
) -> np.ndarray:
    """Draw one transition per active loan using vectorized scalar-equivalent hazards."""

    result = states.copy()
    score_risk = np.clip((700.0 - scores) / 100.0, -0.8, 1.8)
    macro_risk = max(unemployment - 4.8, 0.0) / 5.0 + max(unemployment_change, 0.0) / 2.0
    seasoning = np.exp(-((mobs - 28.0) / 22.0) ** 2)
    prepay = np.clip(
        0.0025 + 0.00010 * mobs + 0.0015 * np.maximum(-score_risk, 0.0), 0.001, 0.025
    )

    current = states == STATE_TO_INDEX["Current"]
    if current.any():
        worsen = np.clip(
            0.003
            + 0.010 * np.maximum(score_risk[current], 0.0)
            + 0.010 * macro_risk
            + 0.004 * seasoning[current],
            0.001,
            0.085,
        )
        stay = 1.0 - worsen - prepay[current]
        draws = uniforms[current]
        result[current] = np.where(
            draws < stay,
            STATE_TO_INDEX["Current"],
            np.where(
                draws < stay + worsen,
                STATE_TO_INDEX["DPD30"],
                STATE_TO_INDEX["Prepaid"],
            ),
        )

    for state_index in range(1, len(ACTIVE_STATES)):
        selected = states == state_index
        if not selected.any():
            continue
        selected_score_risk = score_risk[selected]
        worsen = np.clip(
            0.19 + 0.07 * selected_score_risk + 0.13 * macro_risk + 0.02 * state_index,
            0.07,
            0.72,
        )
        cure = np.clip(
            0.50 - 0.055 * state_index - 0.08 * selected_score_risk - 0.12 * macro_risk,
            0.08,
            0.70,
        )
        selected_prepay = np.minimum(prepay[selected] * 0.25, 0.004)
        stay = 1.0 - worsen - cure - selected_prepay
        constrained = stay < 0.03
        scale = (1.0 - selected_prepay - 0.03) / (worsen + cure)
        worsen = np.where(constrained, worsen * scale, worsen)
        cure = np.where(constrained, cure * scale, cure)
        stay = np.where(constrained, 0.03, stay)
        draws = uniforms[selected]
        roll_state = STATE_TO_INDEX["ChargeOff"] if state_index == 5 else state_index + 1
        result[selected] = np.select(
            [draws < cure, draws < cure + stay, draws < cure + stay + worsen],
            [state_index - 1, state_index, roll_state],
            default=STATE_TO_INDEX["Prepaid"],
        )
    return result.astype(np.int8)


def _frame_from_blocks(blocks: list[dict[str, np.ndarray]]) -> pd.DataFrame:
    """Create one DataFrame after concatenating monthly numpy result blocks."""

    return pd.DataFrame(
        {key: np.concatenate([block[key] for block in blocks]) for key in blocks[0]}
    )


def generate_portfolio(config: EngineConfig) -> SyntheticPortfolio:
    """Generate realistic, complete loan histories for loss-model development.

    Feature-specific SeedSequence streams are prefix-stable: loan ``i`` receives
    the same covariates and full random-draw row when portfolio size changes. The
    macro path has its own fixed stream and is independent of loan count.
    """

    settings = config.synthetic
    seed = config.project.seed
    number_of_loans = settings.number_of_loans
    horizon = settings.max_observation_months
    base_month = pd.Timestamp(f"{settings.first_vintage_year}-01-01")
    vintage_month_count = (settings.last_vintage_year - settings.first_vintage_year + 1) * 12
    macro_month_count = vintage_month_count + horizon - 1

    origination_offsets = _rng(seed, 1).integers(0, vintage_month_count, number_of_loans)
    origination_periods = pd.PeriodIndex(
        pd.Period(base_month, freq="M") + origination_offsets, freq="M"
    )
    origination_months = origination_periods.to_timestamp()
    scores = np.clip(
        np.rint(
            _rng(seed, 2).normal(settings.score_mean, settings.score_std, number_of_loans)
        ),
        500,
        850,
    ).astype(np.int16)
    original_upb = _rng(seed, 3).choice(
        np.arange(80_000, 805_000, 5_000), number_of_loans
    ).astype(np.float64)
    terms = _rng(seed, 4).choice(
        np.array([180, 240, 360]), number_of_loans, p=[0.08, 0.07, 0.85]
    ).astype(np.int16)
    rates = np.clip(_rng(seed, 5).normal(0.055, 0.011, number_of_loans), 0.0225, 0.105)
    ltvs = np.clip(_rng(seed, 6).normal(76, 14, number_of_loans), 30, 120)
    property_states = _rng(seed, 7).choice(
        np.array(["CA", "TX", "FL", "NY", "IL", "GA"]), number_of_loans
    )
    occupancies = _rng(seed, 8).choice(
        np.array(["P", "I", "S"]), number_of_loans, p=[0.88, 0.08, 0.04]
    )
    channels = _rng(seed, 9).choice(
        np.array(["R", "B", "C"]), number_of_loans, p=[0.48, 0.32, 0.20]
    )
    transition_draws = _rng(seed, 10).random((number_of_loans, horizon))
    recovery_noise = _rng(seed, 11).normal(0.0, 0.07, number_of_loans)
    cost_rates = np.clip(_rng(seed, 12).normal(0.045, 0.012, number_of_loans), 0.015, 0.10)
    repurchase_selected = (
        _rng(seed, 13).random(number_of_loans) < settings.repurchase_rate
    )
    repurchase_mobs = np.minimum(
        _rng(seed, 14).geometric(0.15, number_of_loans),
        settings.repurchase_max_mob,
    ).astype(np.int16)

    macro_end = base_month + pd.DateOffset(months=macro_month_count - 1)
    macro = simulate_unemployment(
        base_month, macro_end, settings.unemployment_start, _rng(seed, 1000)
    )
    macro_months = macro["as_of_month"].to_numpy(dtype="datetime64[ns]")
    unemployment = macro["unemployment_rate"].to_numpy()
    unemployment_change = macro["unemployment_change_3m"].to_numpy()
    hpi_change = macro["hpi_change_yoy"].to_numpy()

    loan_ids = np.array([f"SYN{number:09d}" for number in range(1, number_of_loans + 1)])
    monthly_rates = rates / 12.0
    scheduled_payments = original_upb * monthly_rates / (
        1.0 - (1.0 + monthly_rates) ** -terms
    )
    state = np.full(number_of_loans, STATE_TO_INDEX["Current"], dtype=np.int8)
    upb = original_upb.copy()
    alive = np.ones(number_of_loans, dtype=bool)
    censoring_dates = np.full(
        number_of_loans, np.datetime64("NaT", "ns"), dtype="datetime64[ns]"
    )
    panel_blocks: list[dict[str, np.ndarray]] = []
    performance_blocks: list[dict[str, np.ndarray]] = []

    for calendar_offset in range(macro_month_count):
        mobs_all = calendar_offset - origination_offsets
        active_indices = np.flatnonzero(alive & (mobs_all >= 0) & (mobs_all < horizon))
        if not len(active_indices):
            continue
        mobs = mobs_all[active_indices].astype(np.int16)
        states_bom = state[active_indices]
        states_eom = states_bom.copy()
        transitioned = mobs > 0
        if transitioned.any():
            subset = active_indices[transitioned]
            states_eom[transitioned] = _draw_next_states(
                states_bom[transitioned],
                mobs[transitioned],
                scores[subset],
                float(unemployment[calendar_offset]),
                float(unemployment_change[calendar_offset]),
                transition_draws[subset, mobs[transitioned]],
            )
        repurchased = repurchase_selected[active_indices] & (
            mobs == repurchase_mobs[active_indices]
        )
        states_eom[repurchased] = STATE_TO_INDEX["Repurchased"]

        upb_bom = upb[active_indices]
        interest = upb_bom * monthly_rates[active_indices]
        scheduled_principal = np.clip(
            scheduled_payments[active_indices] - interest, 0.0, upb_bom
        )
        scheduled_principal = np.where(
            states_bom == STATE_TO_INDEX["Current"], scheduled_principal, 0.0
        )
        absorbing = np.isin(
            states_eom,
            [
                STATE_TO_INDEX["ChargeOff"],
                STATE_TO_INDEX["Prepaid"],
                STATE_TO_INDEX["Repurchased"],
            ],
        )
        upb_eom = np.where(absorbing, 0.0, np.maximum(upb_bom - scheduled_principal, 0.0))
        horizon_end = mobs == horizon - 1
        terminal = absorbing | horizon_end | (upb_eom <= 0.0)
        charge_off = states_eom == STATE_TO_INDEX["ChargeOff"]
        prepaid = states_eom == STATE_TO_INDEX["Prepaid"]
        repurchased = states_eom == STATE_TO_INDEX["Repurchased"]
        censored = terminal & ~charge_off & ~prepaid & ~repurchased
        exit_reason = np.full(len(active_indices), None, dtype=object)
        exit_reason[charge_off] = "ChargeOff"
        exit_reason[prepaid] = "Prepaid"
        exit_reason[repurchased] = "Repurchased"
        exit_reason[censored] = "Censored"
        month = macro_months[calendar_offset]
        censoring_dates[active_indices[censored]] = month

        recovery_rate = np.clip(
            0.78
            - 0.005 * (ltvs[active_indices] - 75.0)
            + 1.10 * hpi_change[calendar_offset]
            + 0.0006 * (scores[active_indices] - 700.0)
            + recovery_noise[active_indices],
            0.10,
            0.98,
        )
        proceeds = np.where(charge_off, upb_bom * recovery_rate, 0.0)
        costs = np.where(charge_off, upb_bom * cost_rates[active_indices], 0.0)
        month_values = np.full(len(active_indices), month, dtype="datetime64[ns]")
        zero_balance_code = np.full(len(active_indices), None, dtype=object)
        zero_balance_code[prepaid] = "01"
        zero_balance_code[charge_off] = "03"
        zero_balance_code[repurchased] = "06"
        zero_balance_date = np.full(
            len(active_indices), np.datetime64("NaT", "ns"), dtype="datetime64[ns]"
        )
        zero_balance_date[absorbing] = month
        foreclosure_date = np.full(
            len(active_indices), np.datetime64("NaT", "ns"), dtype="datetime64[ns]"
        )
        foreclosure_date[charge_off] = month

        panel_blocks.append(
            {
                "loan_id": loan_ids[active_indices],
                "as_of_month": month_values,
                "vintage": origination_periods[active_indices].strftime("%Y-%m").to_numpy(),
                "months_on_book": mobs,
                "delinquency_state": np.asarray(ALL_STATES, dtype=object)[states_eom],
                "upb_bom": np.round(upb_bom, 2),
                "upb_eom": np.round(upb_eom, 2),
                "orig_score": scores[active_indices],
                "orig_ltv": np.round(ltvs[active_indices], 4),
                "unemployment_rate": np.full(
                    len(active_indices), round(float(unemployment[calendar_offset]), 4)
                ),
                "unemployment_change_3m": np.full(
                    len(active_indices), round(float(unemployment_change[calendar_offset]), 4)
                ),
                "hpi_change_yoy": np.full(
                    len(active_indices), round(float(hpi_change[calendar_offset]), 4)
                ),
                "net_sales_proceeds": np.round(proceeds, 2),
                "foreclosure_costs": np.round(costs, 2),
                "exit_reason": exit_reason,
                "is_censored": censored,
            }
        )
        performance_blocks.append(
            {
                "loan_id": loan_ids[active_indices],
                "as_of_month": month_values,
                "current_upb": np.round(upb_bom, 2),
                "delinquency_status": DPD_BY_STATE[states_eom],
                "loan_age": mobs,
                "remaining_months": np.maximum(terms[active_indices] - mobs, 0),
                "zero_balance_code": zero_balance_code,
                "zero_balance_date": zero_balance_date,
                "modification_flag": np.full(len(active_indices), "N"),
                "foreclosure_date": foreclosure_date,
                "disposition_date": foreclosure_date.copy(),
                "net_sales_proceeds": np.round(proceeds, 2),
                "foreclosure_costs": np.round(costs, 2),
            }
        )
        state[active_indices] = states_eom
        upb[active_indices] = upb_eom
        alive[active_indices[terminal]] = False

    panel = _frame_from_blocks(panel_blocks).sort_values(
        ["loan_id", "as_of_month"], ignore_index=True
    )
    panel["previous_delinquency_state"] = panel.groupby("loan_id", sort=False)[
        "delinquency_state"
    ].shift(1)
    panel["next_delinquency_state"] = panel.groupby("loan_id", sort=False)[
        "delinquency_state"
    ].shift(-1)
    # Downstream contract: censored terminal rows are excluded from M5 transition
    # denominators because their next state was not observed; including them would
    # mechanically understate roll rates.
    panel.loc[panel["is_censored"], "next_delinquency_state"] = None

    performance = _frame_from_blocks(performance_blocks).sort_values(
        ["loan_id", "as_of_month"], ignore_index=True
    )
    acquisition = pd.DataFrame(
        {
            "loan_id": loan_ids,
            "origination_month": origination_months,
            "original_upb": original_upb,
            "original_term": terms,
            "orig_interest_rate": rates,
            "orig_score": scores,
            "orig_ltv": ltvs,
            "property_state": property_states,
            "occupancy_status": occupancies,
            "channel": channels,
            "censoring_date": censoring_dates,
        }
    ).sort_values("loan_id", ignore_index=True)
    return SyntheticPortfolio(acquisition, performance, macro, panel)


def write_portfolio(portfolio: SyntheticPortfolio, config: EngineConfig) -> dict[str, Path]:
    """Write synthetic source files to configured local raw-data locations.

    Pipe-delimited raw files mimic vendor delivery. The generated panel is a
    transparent M1 diagnostic only; M3 will rebuild it independently with Spark.
    """

    locations = {
        "acquisition": Path(config.paths.raw_acquisition) / "synthetic_acquisition.txt",
        "performance": Path(config.paths.raw_performance) / "synthetic_performance.txt",
        "macro": Path(config.paths.macro),
        "panel": Path(config.paths.output) / "synthetic_panel.parquet",
    }
    for path in locations.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    portfolio.acquisition.drop(columns=["censoring_date"], errors="ignore").to_csv(
        locations["acquisition"], sep="|", index=False
    )
    portfolio.performance.to_csv(locations["performance"], sep="|", index=False)
    portfolio.macro.to_csv(locations["macro"], index=False)
    portfolio.panel.to_parquet(
        locations["panel"],
        index=False,
        coerce_timestamps="us",
        allow_truncated_timestamps=True,
    )
    return locations
