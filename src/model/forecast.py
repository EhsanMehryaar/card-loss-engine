"""Single-node state-vector iteration over conditional transition matrices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np
import pandas as pd


class MatrixBuilder(Protocol):
    """M5-compatible transition model interface."""

    states: tuple[str, ...]

    def build_matrix(self, mob: int, score_band: str, macro: dict[str, float]) -> np.ndarray: ...


@dataclass(frozen=True)
class ForecastSegment:
    """One balance-weighted portfolio cell with common forecast drivers."""

    segment_id: str
    score_band: str
    vintage: str
    balance: float
    start_mob: int
    initial_state: dict[str, float]


@dataclass(frozen=True)
class ForecastResult:
    """Segment and portfolio state/absorption paths."""

    segments: pd.DataFrame
    portfolio: pd.DataFrame


class CachedMatrixBuilder:
    """Memoize fitted matrices shared by score/vintage forecast cells."""

    def __init__(self, model: MatrixBuilder) -> None:
        self.model = model
        self.states = tuple(model.states)
        self._cache: dict[tuple[object, ...], np.ndarray] = {}

    def build_matrix(
        self, mob: int, score_band: str, macro: dict[str, float]
    ) -> np.ndarray:
        key = (mob, score_band, *sorted(macro.items()))
        if key not in self._cache:
            self._cache[key] = np.asarray(
                self.model.build_matrix(mob, score_band, macro), dtype=float
            )
        return self._cache[key]


ABSORBING_STATES = ("ChargeOff", "Prepaid", "Repurchased")


def _initial_vector(segment: ForecastSegment, states: tuple[str, ...]) -> np.ndarray:
    unknown = set(segment.initial_state) - set(states)
    if unknown:
        raise ValueError(f"Initial state contains unknown states: {sorted(unknown)}")
    vector = np.array([segment.initial_state.get(state, 0.0) for state in states], dtype=float)
    if np.any(vector < 0.0) or not np.isclose(vector.sum(), 1.0, atol=1e-12):
        raise ValueError("Initial state probabilities must be nonnegative and sum to one")
    return vector


def forecast_segment(
    model: MatrixBuilder,
    segment: ForecastSegment,
    macro_path: pd.DataFrame,
    *,
    tolerance: float = 1e-10,
    max_mob: int | None = None,
) -> pd.DataFrame:
    """Iterate ``v[t+1] = v[t] @ P[t]`` for one segment.

    Each output row is the post-transition state at the end of that forecast
    month. Marginal absorbed probabilities are changes in the three absorbing
    state masses, so only ``marginal_chargeoff`` is a default probability.
    """

    if segment.balance < 0.0:
        raise ValueError("Segment balance cannot be negative")
    if macro_path.empty:
        raise ValueError("Macro path must contain at least one month")
    states = tuple(model.states)
    missing_absorbing = set(ABSORBING_STATES) - set(states)
    if missing_absorbing:
        raise ValueError(f"Transition model is missing absorbing states: {missing_absorbing}")
    index = {state: position for position, state in enumerate(states)}
    vector = _initial_vector(segment, states)
    prior_absorbed = {state: float(vector[index[state]]) for state in ABSORBING_STATES}
    prior_default = prior_absorbed["ChargeOff"]
    records: list[dict[str, object]] = []

    for offset, (_, macro_row) in enumerate(macro_path.reset_index(drop=True).iterrows()):
        month = offset + 1
        mob = segment.start_mob + offset
        matrix_mob = min(mob, max_mob) if max_mob is not None else mob
        macro = {
            str(column): float(value)
            for column, value in macro_row.items()
            if column != "as_of_month" and pd.notna(value)
        }
        matrix = np.asarray(model.build_matrix(matrix_mob, segment.score_band, macro), dtype=float)
        if matrix.shape != (len(states), len(states)):
            raise ValueError("Transition matrix shape does not match model states")
        vector = vector @ matrix
        mass = float(vector.sum())
        if not np.isclose(mass, 1.0, atol=tolerance, rtol=0.0):
            raise ValueError(f"Probability mass is not conserved in month {month}: {mass}")
        if np.any(vector < -tolerance):
            raise ValueError(f"Forecast produced negative probability in month {month}")
        cumulative_default = float(vector[index["ChargeOff"]])
        if cumulative_default + tolerance < prior_default:
            raise ValueError("Cumulative default probability decreased")
        record: dict[str, object] = {
            "segment_id": segment.segment_id,
            "score_band": segment.score_band,
            "vintage": segment.vintage,
            "balance": float(segment.balance),
            "month": month,
            "mob": mob,
            "as_of_month": macro_row.get("as_of_month", pd.NaT),
            "probability_mass": mass,
        }
        for state, probability in zip(states, vector, strict=True):
            record[f"state_{state}"] = float(probability)
        for state in ABSORBING_STATES:
            absorbed = float(vector[index[state]])
            record[f"cumulative_{state.lower()}"] = absorbed
            record[f"marginal_{state.lower()}"] = absorbed - prior_absorbed[state]
            prior_absorbed[state] = absorbed
        records.append(record)
        prior_default = cumulative_default
    return pd.DataFrame.from_records(records)


def aggregate_forecasts(segment_paths: pd.DataFrame) -> pd.DataFrame:
    """Aggregate segment probabilities using opening-balance weights."""

    if segment_paths.empty:
        raise ValueError("Cannot aggregate an empty segment forecast")
    probability_columns = [
        column
        for column in segment_paths.columns
        if column.startswith(("state_", "cumulative_", "marginal_"))
    ]
    records: list[dict[str, object]] = []
    for month, frame in segment_paths.groupby("month", sort=True):
        total_balance = float(frame["balance"].sum())
        if total_balance <= 0.0:
            raise ValueError("Portfolio opening balance must be positive")
        record: dict[str, object] = {
            "month": int(month),
            "as_of_month": frame["as_of_month"].iloc[0],
            "opening_balance": total_balance,
        }
        weights = frame["balance"].to_numpy(dtype=float) / total_balance
        for column in probability_columns:
            record[column] = float(np.dot(frame[column].to_numpy(dtype=float), weights))
        record["probability_mass"] = float(
            sum(record[column] for column in probability_columns if column.startswith("state_"))
        )
        records.append(record)
    return pd.DataFrame.from_records(records)


def forecast_segments(
    model: MatrixBuilder,
    segments: list[ForecastSegment],
    macro_path: pd.DataFrame,
    *,
    tolerance: float = 1e-10,
    max_mob: int | None = None,
) -> ForecastResult:
    """Forecast score/vintage segments and their balance-weighted portfolio."""

    if not segments:
        raise ValueError("At least one forecast segment is required")
    paths = pd.concat(
        [
            forecast_segment(
                model,
                segment,
                macro_path,
                tolerance=tolerance,
                max_mob=max_mob,
            )
            for segment in segments
        ],
        ignore_index=True,
    )
    return ForecastResult(paths, aggregate_forecasts(paths))
