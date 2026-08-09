"""Macro-conditioned recovery and loss-given-default modeling."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class LGDSegmentFit:
    """Exposure-weighted recovery regression for one score band."""

    score_band: str
    observations: int
    recovery_intercept: float
    hpi_coefficient: float
    uses_fallback: bool


@dataclass(frozen=True)
class LGDModel:
    """Score-segmented recovery model evaluated at default-month HPI."""

    fits: dict[str, LGDSegmentFit]
    fallback_lgd: float

    def predict_lgd(self, score_band: str, hpi_change_yoy: float) -> float:
        fit = self.fits.get(score_band)
        if fit is None or fit.uses_fallback:
            return float(self.fallback_lgd)
        recovery = fit.recovery_intercept + fit.hpi_coefficient * float(hpi_change_yoy)
        return float(1.0 - np.clip(recovery, 0.0, 1.0))

    def coefficient_table(self) -> pd.DataFrame:
        return pd.DataFrame.from_records(
            [
                {
                    "score_band": fit.score_band,
                    "observations": fit.observations,
                    "recovery_intercept": fit.recovery_intercept,
                    "hpi_coefficient": fit.hpi_coefficient,
                    "uses_fallback": fit.uses_fallback,
                    "fallback_lgd": self.fallback_lgd,
                }
                for fit in self.fits.values()
            ]
        )


def _default_observations(frame: pd.DataFrame) -> pd.DataFrame:
    required = {
        "score_band",
        "hpi_change_yoy",
        "upb_bom",
        "net_sales_proceeds",
        "foreclosure_costs",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"LGD observations are missing columns: {sorted(missing)}")
    defaults = frame.copy()
    if "exit_reason" in defaults:
        defaults = defaults[defaults["exit_reason"].eq("ChargeOff")]
    defaults = defaults.dropna(subset=list(required))
    defaults = defaults[defaults["upb_bom"] > 0.0].copy()
    net_recovery = (defaults["net_sales_proceeds"] - defaults["foreclosure_costs"]) / defaults[
        "upb_bom"
    ]
    defaults["realized_recovery"] = net_recovery.clip(0.0, 1.0)
    defaults["realized_lgd"] = 1.0 - defaults["realized_recovery"]
    return defaults


def fit_lgd_model(
    observations: pd.DataFrame,
    *,
    fallback_lgd: float,
    score_bands: tuple[str, ...] | None = None,
    min_observations: int = 20,
) -> LGDModel:
    """Fit exposure-weighted recovery on HPI separately by score band."""

    if not 0.0 <= fallback_lgd <= 1.0:
        raise ValueError("Fallback LGD must be in [0, 1]")
    if min_observations < 2:
        raise ValueError("LGD minimum observations must be at least two")
    defaults = _default_observations(observations)
    bands = score_bands or tuple(sorted(defaults["score_band"].astype(str).unique()))
    fits: dict[str, LGDSegmentFit] = {}
    for score_band in bands:
        segment = defaults[defaults["score_band"].eq(score_band)]
        sufficient = len(segment) >= min_observations and segment["hpi_change_yoy"].nunique() >= 2
        if not sufficient:
            LOGGER.warning(
                "Using fallback LGD %.2f for score band %s: %d usable defaults",
                fallback_lgd,
                score_band,
                len(segment),
            )
            fits[score_band] = LGDSegmentFit(
                score_band,
                len(segment),
                1.0 - fallback_lgd,
                0.0,
                True,
            )
            continue
        design = np.column_stack(
            [np.ones(len(segment)), segment["hpi_change_yoy"].to_numpy(dtype=float)]
        )
        weights = np.sqrt(segment["upb_bom"].to_numpy(dtype=float))
        coefficients, *_ = np.linalg.lstsq(
            design * weights[:, None],
            segment["realized_recovery"].to_numpy(dtype=float) * weights,
            rcond=None,
        )
        fits[score_band] = LGDSegmentFit(
            score_band,
            len(segment),
            float(coefficients[0]),
            float(coefficients[1]),
            False,
        )
    return LGDModel(fits, float(fallback_lgd))


def lgd_validation_by_era(observations: pd.DataFrame, model: LGDModel) -> pd.DataFrame:
    """Compare exposure-weighted realized and modeled LGD by origination era."""

    defaults = _default_observations(observations)
    if "vintage" not in defaults:
        raise ValueError("LGD era validation requires vintage")
    year = defaults["vintage"].astype(str).str[:4].astype(int)
    defaults["era"] = np.select(
        [year < 2008, year <= 2010], ["pre-2008", "2008-2010"], default="2011+"
    )
    defaults["modeled_lgd"] = [
        model.predict_lgd(str(score_band), float(hpi))
        for score_band, hpi in zip(defaults["score_band"], defaults["hpi_change_yoy"], strict=True)
    ]
    records: list[dict[str, object]] = []
    for era in ("pre-2008", "2008-2010", "2011+"):
        frame = defaults[defaults["era"].eq(era)]
        exposure = frame["upb_bom"].to_numpy(dtype=float)
        total = float(exposure.sum())
        records.append(
            {
                "era": era,
                "defaults": len(frame),
                "default_exposure": total,
                "average_hpi_change_yoy": (
                    float(np.average(frame["hpi_change_yoy"], weights=exposure))
                    if total
                    else np.nan
                ),
                "realized_lgd": (
                    float(np.average(frame["realized_lgd"], weights=exposure)) if total else np.nan
                ),
                "modeled_lgd": (
                    float(np.average(frame["modeled_lgd"], weights=exposure)) if total else np.nan
                ),
            }
        )
    return pd.DataFrame.from_records(records)
