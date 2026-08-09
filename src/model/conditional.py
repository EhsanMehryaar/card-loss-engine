"""Frequency-weighted conditional transition models and matrix construction."""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
import pandas as pd
from patsy import dmatrix
from sklearn.linear_model import LogisticRegression

from src.config import EngineConfig
from src.panel.macro import MACRO_VALUE_COLUMNS

TRANSIENT_STATES = ("Current", "DPD30", "DPD60", "DPD90", "DPD120", "DPD150")
DELINQUENT_STATES = TRANSIENT_STATES[1:]


def _score_risk(score_band: str) -> float:
    numbers = [int(value) for value in re.findall(r"\d+", score_band)]
    if len(numbers) < 2:
        raise ValueError(f"Cannot parse score band: {score_band}")
    midpoint = (numbers[-2] + numbers[-1]) / 2.0
    return float(np.clip((700.0 - midpoint) / 100.0, -0.8, 1.8))


def _design_frame(
    frame: pd.DataFrame,
    macro_columns: tuple[str, ...],
    mob_knots: tuple[int, ...],
    mob_lower_bound: int,
    mob_upper_bound: int,
) -> pd.DataFrame:
    mob = frame["months_on_book"].astype(float)
    risk = frame["score_band"].map(_score_risk).astype(float)
    design = pd.DataFrame(index=frame.index)
    knot_text = ", ".join(str(knot) for knot in mob_knots)
    spline = np.asarray(
        dmatrix(
            (
                f"cr(mob, knots=({knot_text}), lower_bound={mob_lower_bound}, "
                f"upper_bound={mob_upper_bound}) - 1"
            ),
            {"mob": mob.to_numpy()},
        )
    )
    for index in range(spline.shape[1]):
        design[f"mob_spline_{index}"] = spline[:, index]
    design["score_risk"] = risk
    design["score_risk_positive"] = risk.clip(lower=0.0)
    for column in macro_columns:
        values = frame[column].astype(float)
        if column == "unemployment_rate" or column.startswith(
            "unemployment_rate_lag_"
        ):
            design[f"{column}_excess_4_8"] = (values - 4.8).clip(lower=0.0)
        else:
            design[column] = values
    return design


@dataclass
class OriginModel:
    origin: str
    destinations: tuple[str, ...]
    macro_columns: tuple[str, ...]
    mob_knots: tuple[int, ...]
    mob_lower_bound: int
    mob_upper_bound: int
    feature_columns: tuple[str, ...]
    estimator: LogisticRegression | None
    prior: dict[str, float]
    bic: float
    regularization_c: float

    def predict(self, frame: pd.DataFrame) -> dict[str, float]:
        if self.estimator is None:
            return dict(self.prior)
        design = _design_frame(
            frame,
            self.macro_columns,
            self.mob_knots,
            self.mob_lower_bound,
            self.mob_upper_bound,
        ).reindex(columns=self.feature_columns)
        probabilities = self.estimator.predict_proba(design)[0]
        return {
            str(label): float(value)
            for label, value in zip(self.estimator.classes_, probabilities, strict=True)
        }


@dataclass
class ConditionalTransitionModel:
    states: tuple[str, ...]
    absorbing: tuple[str, ...]
    origin_models: dict[str, OriginModel]
    delinquent_prepay_hazard: float
    repurchase_hazard: float
    macro_defaults: dict[str, float]
    score_bands: tuple[str, ...]

    def build_matrix(
        self, mob: int, score_band: str, macro: dict[str, float]
    ) -> np.ndarray:
        """Return a full stochastic matrix for one account segment and month."""

        if score_band not in self.score_bands:
            raise ValueError(f"Unknown score band: {score_band}")
        values = dict(self.macro_defaults)
        values.update(macro)
        row_frame = pd.DataFrame(
            [{"months_on_book": mob, "score_band": score_band, **values}]
        )
        matrix = np.zeros((len(self.states), len(self.states)), dtype=float)
        state_index = {state: index for index, state in enumerate(self.states)}
        for state in self.absorbing:
            matrix[state_index[state], state_index[state]] = 1.0
        for origin in TRANSIENT_STATES:
            model = self.origin_models[origin]
            conditional = model.predict(row_frame)
            repurchase = self.repurchase_hazard
            prepay = self.delinquent_prepay_hazard if origin in DELINQUENT_STATES else 0.0
            remaining = 1.0 - repurchase - prepay
            row = matrix[state_index[origin]]
            for destination, probability in conditional.items():
                row[state_index[destination]] = remaining * probability
            row[state_index["Repurchased"]] = repurchase
            if origin in DELINQUENT_STATES:
                row[state_index["Prepaid"]] = prepay
            row /= row.sum()
        if not np.allclose(matrix.sum(axis=1), 1.0):
            raise ValueError("Conditional matrix rows do not sum to one")
        return matrix

    def coefficient_table(self) -> pd.DataFrame:
        records: list[dict[str, object]] = []
        for origin, model in self.origin_models.items():
            if model.estimator is None:
                continue
            for destination, coefficients, intercept in zip(
                model.estimator.classes_,
                model.estimator.coef_,
                model.estimator.intercept_,
                strict=True,
            ):
                records.append(
                    {
                        "origin": origin,
                        "destination": destination,
                        "feature": "intercept",
                        "coefficient": float(intercept),
                        "selected_macro_timing": ",".join(model.macro_columns),
                        "bic": model.bic,
                        "regularization_c": model.regularization_c,
                    }
                )
                for feature, coefficient in zip(
                    model.feature_columns, coefficients, strict=True
                ):
                    records.append(
                        {
                            "origin": origin,
                            "destination": destination,
                            "feature": feature,
                            "coefficient": float(coefficient),
                            "selected_macro_timing": ",".join(model.macro_columns),
                            "bic": model.bic,
                            "regularization_c": model.regularization_c,
                        }
                    )
        return pd.DataFrame.from_records(records)


def _allowed_destinations(origin: str) -> tuple[str, ...]:
    if origin == "Current":
        return ("Current", "DPD30", "Prepaid")
    index = TRANSIENT_STATES.index(origin)
    cure = TRANSIENT_STATES[index - 1]
    roll = "ChargeOff" if origin == "DPD150" else TRANSIENT_STATES[index + 1]
    return (cure, origin, roll)


def _fit_origin(
    frame: pd.DataFrame,
    origin: str,
    candidate_macro_sets: tuple[tuple[str, ...], ...],
    mob_knots: tuple[int, ...],
    mob_lower_bound: int,
    mob_upper_bound: int,
    regularization_c: float,
) -> OriginModel:
    destinations = _allowed_destinations(origin)
    eligible = frame[
        frame["delinquency_state"].eq(origin)
        & frame["next_delinquency_state"].isin(destinations)
    ].copy()
    totals = eligible.groupby("next_delinquency_state")["transition_count"].sum()
    if float(totals.sum()) == 0.0:
        prior = {
            destination: float(destination == origin) for destination in destinations
        }
    else:
        prior = {
            destination: float(totals.get(destination, 0.0) / totals.sum())
            for destination in destinations
        }
    if eligible.empty or eligible["next_delinquency_state"].nunique() < 2:
        return OriginModel(
            origin,
            destinations,
            (),
            mob_knots,
            mob_lower_bound,
            mob_upper_bound,
            (),
            None,
            prior,
            np.inf,
            regularization_c,
        )

    all_macro = sorted({column for columns in candidate_macro_sets for column in columns})
    eligible = eligible.dropna(subset=all_macro)
    best: OriginModel | None = None
    for macro_columns in candidate_macro_sets:
        design = _design_frame(
            eligible,
            macro_columns,
            mob_knots,
            mob_lower_bound,
            mob_upper_bound,
        )
        outcome = eligible["next_delinquency_state"].astype(str)
        weights = eligible["transition_count"].astype(float)
        estimator = LogisticRegression(
            C=regularization_c, solver="lbfgs", max_iter=1_000
        )
        estimator.fit(design, outcome, sample_weight=weights)
        probabilities = estimator.predict_proba(design)
        class_index = {label: idx for idx, label in enumerate(estimator.classes_)}
        chosen = np.array(
            [probabilities[i, class_index[label]] for i, label in enumerate(outcome)]
        )
        log_likelihood = float(np.sum(weights * np.log(np.clip(chosen, 1e-15, 1.0))))
        parameters = estimator.coef_.size + estimator.intercept_.size
        bic = -2.0 * log_likelihood + parameters * np.log(float(weights.sum()))
        fitted = OriginModel(
            origin,
            destinations,
            macro_columns,
            mob_knots,
            mob_lower_bound,
            mob_upper_bound,
            tuple(design.columns),
            estimator,
            prior,
            bic,
            regularization_c,
        )
        if best is None or bic < best.bic:
            best = fitted
    if best is None:
        raise ValueError(f"Unable to fit origin state: {origin}")
    return best


def fit_conditional_models(
    counts: pd.DataFrame, macro: pd.DataFrame, config: EngineConfig
) -> ConditionalTransitionModel:
    """Fit one frequency-weighted multinomial model per transient origin."""

    training = counts.merge(macro, on="as_of_month", how="left", validate="many_to_one")
    candidates = [tuple(MACRO_VALUE_COLUMNS)]
    candidates.extend(
        tuple(f"{column}_lag_{lag}" for column in MACRO_VALUE_COLUMNS)
        for lag in config.model.macro_lags
    )
    candidate_macro_sets = tuple(candidates)
    mob_lower_bound = 0
    mob_upper_bound = config.model.vintage_maturity_mob
    mob_knots = tuple(
        boundary
        for boundary in config.model.mob_bands
        if mob_lower_bound < boundary < mob_upper_bound
    )
    if len(mob_knots) not in {3, 4}:
        raise ValueError(
            "Natural cubic MOB spline requires 3-4 internal configured boundaries"
        )
    origin_models = {
        origin: _fit_origin(
            training,
            origin,
            candidate_macro_sets,
            mob_knots,
            mob_lower_bound,
            mob_upper_bound,
            (
                config.model.conditional_dpd150_regularization_c
                if origin == "DPD150"
                else config.model.conditional_regularization_c
            ),
        )
        for origin in TRANSIENT_STATES
    }
    delinquent = counts[counts["delinquency_state"].isin(DELINQUENT_STATES)]
    delinquent_total = float(delinquent["transition_count"].sum())
    delinquent_prepay = float(
        delinquent.loc[
            delinquent["next_delinquency_state"].eq("Prepaid"), "transition_count"
        ].sum()
        / delinquent_total
    )
    transient = counts[counts["delinquency_state"].isin(TRANSIENT_STATES)]
    repurchase = float(
        transient.loc[
            transient["next_delinquency_state"].eq("Repurchased"), "transition_count"
        ].sum()
        / transient["transition_count"].sum()
    )
    macro_columns = [
        *MACRO_VALUE_COLUMNS,
        *[
            f"{column}_lag_{lag}"
            for lag in config.model.macro_lags
            for column in MACRO_VALUE_COLUMNS
        ],
    ]
    defaults = {
        column: float(macro[column].dropna().median()) for column in macro_columns
    }
    score_bands = tuple(sorted(counts["score_band"].dropna().unique()))
    return ConditionalTransitionModel(
        tuple(config.states.ordered),
        tuple(config.states.absorbing),
        origin_models,
        delinquent_prepay,
        repurchase,
        defaults,
        score_bands,
    )


def macro_sensitivity_table(
    model: ConditionalTransitionModel,
    mob: int,
    score_band: str,
) -> pd.DataFrame:
    """Translate a one-point unemployment shock into transition basis points."""

    benign = dict(model.macro_defaults)
    stressed = dict(benign)
    for key in stressed:
        if key == "unemployment_rate" or key.startswith("unemployment_rate_lag_"):
            stressed[key] = benign[key] + 1.0
    baseline = model.build_matrix(mob, score_band, benign)
    shocked = model.build_matrix(mob, score_band, stressed)
    state_index = {state: idx for idx, state in enumerate(model.states)}
    records = []
    for origin in TRANSIENT_STATES:
        destinations = _allowed_destinations(origin)
        for destination in destinations:
            change = 10_000.0 * (
                shocked[state_index[origin], state_index[destination]]
                - baseline[state_index[origin], state_index[destination]]
            )
            records.append(
                {
                    "origin": origin,
                    "destination": destination,
                    "unemployment_1pp_change_bps": float(change),
                }
            )
    return pd.DataFrame.from_records(records)


def ground_truth_recovery_table(
    model: ConditionalTransitionModel, mob: int, score_band: str
) -> pd.DataFrame:
    """Check the signs and rough probability-space magnitudes of known hazards."""

    macro = dict(model.macro_defaults)
    stressed = dict(macro)
    for key in stressed:
        if key == "unemployment_rate" or key.startswith("unemployment_rate_lag_"):
            stressed[key] = macro[key] + 1.0
    baseline = model.build_matrix(mob, score_band, macro)
    shock = model.build_matrix(mob, score_band, stressed)
    index = {state: position for position, state in enumerate(model.states)}
    records: list[dict[str, object]] = []

    def add_record(
        origin: str,
        destination: str,
        driver: str,
        truth_bps: float,
        fitted_bps: float,
    ) -> None:
        expected_sign = "positive" if truth_bps > 0 else "negative"
        sign_match = fitted_bps > 0 if truth_bps > 0 else fitted_bps < 0
        records.append(
            {
                "origin": origin,
                "destination": destination,
                "driver": driver,
                "ground_truth_effect_bps_approx": truth_bps,
                "fitted_effect_bps": float(fitted_bps),
                "expected_sign": expected_sign,
                "sign_match": bool(sign_match),
                "selected_macro_timing": ",".join(
                    model.origin_models[origin].macro_columns
                ),
            }
        )

    for origin in TRANSIENT_STATES:
        origin_index = index[origin]
        if origin == "Current":
            destinations = [("DPD30", 20.0)]
        else:
            position = TRANSIENT_STATES.index(origin)
            cure = TRANSIENT_STATES[position - 1]
            roll = "ChargeOff" if origin == "DPD150" else TRANSIENT_STATES[position + 1]
            destinations = [(cure, -240.0), (roll, 260.0)]
        for destination, truth_bps in destinations:
            fitted_bps = 10_000.0 * (
                shock[origin_index, index[destination]]
                - baseline[origin_index, index[destination]]
            )
            add_record(origin, destination, "unemployment_1pp", truth_bps, fitted_bps)

    low_score_band = model.score_bands[0]
    low_score = model.build_matrix(mob, low_score_band, macro)
    baseline_score_risk = _score_risk(score_band)
    low_score_risk = _score_risk(low_score_band)
    risk_difference = low_score_risk - baseline_score_risk
    for origin in TRANSIENT_STATES:
        origin_index = index[origin]
        if origin == "Current":
            destinations = [("DPD30", 100.0 * max(risk_difference, 0.0))]
        else:
            position = TRANSIENT_STATES.index(origin)
            cure = TRANSIENT_STATES[position - 1]
            roll = "ChargeOff" if origin == "DPD150" else TRANSIENT_STATES[position + 1]
            destinations = [
                (cure, -800.0 * risk_difference),
                (roll, 700.0 * risk_difference),
            ]
        for destination, truth_bps in destinations:
            fitted_bps = 10_000.0 * (
                low_score[origin_index, index[destination]]
                - baseline[origin_index, index[destination]]
            )
            add_record(origin, destination, "lower_score", truth_bps, fitted_bps)

    mob_28 = model.build_matrix(28, score_band, macro)
    mob_100 = model.build_matrix(100, score_band, macro)
    fitted_seasoning = 10_000.0 * (
        mob_28[index["Current"], index["DPD30"]]
        - mob_100[index["Current"], index["DPD30"]]
    )
    truth_seasoning = 40.0 * (
        np.exp(-((28.0 - 28.0) / 22.0) ** 2)
        - np.exp(-((100.0 - 28.0) / 22.0) ** 2)
    )
    add_record(
        "Current", "DPD30", "seasoning_mob28_vs_100", truth_seasoning, fitted_seasoning
    )
    return pd.DataFrame.from_records(records)
