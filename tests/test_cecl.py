"""Known-answer CECL tests are added in Milestone 6."""

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

from src.model.cecl import ECLSegment, calculate_lifetime_ecl, exposure_balance
from src.model.lgd import LGDModel, LGDSegmentFit


@dataclass
class FixedMatrixModel:
    default: float
    prepay: float
    repurchase: float = 0.0
    states: tuple[str, ...] = ("Current", "ChargeOff", "Prepaid", "Repurchased")

    def build_matrix(self, mob: int, score_band: str, macro: dict[str, float]) -> np.ndarray:
        matrix = np.eye(4)
        matrix[0] = [
            1.0 - self.default - self.prepay - self.repurchase,
            self.default,
            self.prepay,
            self.repurchase,
        ]
        return matrix


def _segments() -> list[ECLSegment]:
    return [
        ECLSegment(f"L{index}", "FICO_700-739", "2018", balance, 0, {"Current": 1.0}, 0.0, 4)
        for index, balance in enumerate((100.0, 200.0, 300.0), start=1)
    ]


def _lgd(value: float) -> LGDModel:
    return LGDModel(
        {"FICO_700-739": LGDSegmentFit("FICO_700-739", 100, 1.0 - value, 0.0, False)},
        0.45,
    )


def test_hand_computed_three_loan_lifetime_ecl() -> None:
    macro = pd.DataFrame({"hpi_change_yoy": [0.0] * 4})

    result = calculate_lifetime_ecl(
        FixedMatrixModel(default=0.10, prepay=0.20),
        _lgd(0.50),
        _segments(),
        macro,
        annual_discount_rate=0.0,
    )

    # PD = [0.1, 0.07, 0.049, 0.0343], EAD = [600, 450, 300, 150], LGD = 0.5.
    assert result.lifetime_ecl == pytest.approx(55.6725, abs=1e-12)
    assert result.by_score_band.loc[0, "expected_default_exposure"] == pytest.approx(
        111.345, abs=1e-12
    )
    assert result.by_score_band.loc[0, "lgd_at_default"] == pytest.approx(0.50)
    assert result.by_vintage.loc[0, "lifetime_pd"] == pytest.approx(0.2533)


def test_higher_prepayment_lowers_ecl() -> None:
    macro = pd.DataFrame({"hpi_change_yoy": [0.0] * 4})
    low_prepay = calculate_lifetime_ecl(
        FixedMatrixModel(default=0.10, prepay=0.05),
        _lgd(0.50),
        _segments(),
        macro,
        annual_discount_rate=0.0,
    )
    high_prepay = calculate_lifetime_ecl(
        FixedMatrixModel(default=0.10, prepay=0.40),
        _lgd(0.50),
        _segments(),
        macro,
        annual_discount_rate=0.0,
    )

    assert high_prepay.lifetime_ecl < low_prepay.lifetime_ecl


def test_zero_default_portfolio_has_exact_zero_ecl() -> None:
    result = calculate_lifetime_ecl(
        FixedMatrixModel(default=0.0, prepay=0.20),
        _lgd(0.50),
        _segments(),
        pd.DataFrame({"hpi_change_yoy": [0.0] * 4}),
        annual_discount_rate=0.05,
    )

    assert result.lifetime_ecl == 0.0
    assert not np.isnan(result.ecl_rate)


def test_censored_null_eom_uses_bom_exposure() -> None:
    result = exposure_balance(
        pd.Series([90.0, np.nan]),
        pd.Series([100.0, 80.0]),
    )

    assert result.tolist() == [90.0, 80.0]
