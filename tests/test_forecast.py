"""Forecast mass-conservation tests are added in Milestone 6."""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.model.forecast import ForecastSegment, forecast_segment, forecast_segments


@dataclass
class MacroToyModel:
    states: tuple[str, ...] = ("Current", "ChargeOff", "Prepaid", "Repurchased")

    def build_matrix(self, mob: int, score_band: str, macro: dict[str, float]) -> np.ndarray:
        default = 0.01 + 0.002 * float(macro.get("stress", 0.0))
        prepay = 0.03
        repurchase = 0.005 if mob < 24 else 0.0
        matrix = np.eye(4)
        matrix[0] = [1.0 - default - prepay - repurchase, default, prepay, repurchase]
        return matrix


def test_mass_conservation_and_monotone_default_across_macro_grid() -> None:
    model = MacroToyModel()
    segment = ForecastSegment("S", "FICO_700-739", "2018", 100.0, 0, {"Current": 1.0})
    for stress in (0.0, 1.0, 3.0, 6.0):
        macro = pd.DataFrame({"stress": np.full(48, stress)})
        path = forecast_segment(model, segment, macro)

        assert np.allclose(path["probability_mass"], 1.0)
        assert path["cumulative_chargeoff"].is_monotonic_increasing
        absorption = path[["marginal_chargeoff", "marginal_prepaid", "marginal_repurchased"]]
        assert (absorption >= 0).all().all()


def test_segment_forecasts_aggregate_with_balance_weights() -> None:
    macro = pd.DataFrame({"stress": [0.0, 0.0]})
    result = forecast_segments(
        MacroToyModel(),
        [
            ForecastSegment("A", "FICO_700-739", "2017", 100.0, 0, {"Current": 1.0}),
            ForecastSegment("B", "FICO_700-739", "2018", 300.0, 0, {"Current": 1.0}),
        ],
        macro,
    )

    assert len(result.segments) == 4
    assert np.allclose(result.portfolio["probability_mass"], 1.0)
    assert (result.portfolio["opening_balance"] == 400.0).all()
