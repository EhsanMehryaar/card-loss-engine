"""Known-answer CECL tests are added in Milestone 6."""

from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.model.cecl import (
    ECLSegment,
    build_fit_comparison,
    build_reconciliation_bridge,
    calculate_lifetime_ecl,
    exposure_balance,
    validate_forecast_against_ground_truth,
)
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


def test_m4_to_m6_bridge_quantifies_roll_rate_model_difference() -> None:
    bridge = build_reconciliation_bridge(
        m4_ultimate=93.0,
        realized_before_cutoff=79.0,
        m4_original_balance=1_000.0,
        m6_undiscounted=95.0,
        m6_discounted=83.0,
        m6_outstanding_balance=500.0,
    )

    assert bridge["adjustment_dollars"].tolist() == [93.0, -79.0, 0.0, 81.0, -12.0]
    assert bridge.iloc[-1]["subtotal_dollars"] == 83.0
    assert bridge.iloc[-1]["loss_rate"] == pytest.approx(0.166)


def test_ground_truth_validation_reconciles_pd_ead_lgd_error() -> None:
    panel = pd.DataFrame.from_records(
        [
            {
                "loan_id": "A",
                "as_of_month": "2018-12-01",
                "upb_bom": 100.0,
                "upb_eom": 100.0,
                "net_sales_proceeds": 0.0,
                "foreclosure_costs": 0.0,
                "exit_reason": None,
            },
            {
                "loan_id": "B",
                "as_of_month": "2018-12-01",
                "upb_bom": 100.0,
                "upb_eom": 100.0,
                "net_sales_proceeds": 0.0,
                "foreclosure_costs": 0.0,
                "exit_reason": None,
            },
            {
                "loan_id": "A",
                "as_of_month": "2020-01-01",
                "upb_bom": 80.0,
                "upb_eom": 0.0,
                "net_sales_proceeds": 40.0,
                "foreclosure_costs": 0.0,
                "exit_reason": "ChargeOff",
            },
        ]
    )
    monthly = pd.DataFrame.from_records(
        [
            {
                "as_of_month": "2020-01-01",
                "marginal_pd_dollars": 100.0,
                "expected_default_exposure": 90.0,
                "undiscounted_loss": 45.0,
            }
        ]
    )

    validation = validate_forecast_against_ground_truth(
        panel,
        monthly,
        cutoff="2018-12-01",
        outstanding_balance=200.0,
    )
    total = validation.iloc[-1]

    assert total["projected_undiscounted_loss"] == 45.0
    assert total["realized_undiscounted_net_loss"] == 40.0
    assert total["error_dollars"] == 5.0
    assert total["pd_error_contribution"] == pytest.approx(0.0)
    assert total["ead_error_contribution"] == pytest.approx(5.0)
    assert total["lgd_error_contribution"] == pytest.approx(0.0)


def test_fit_comparison_reports_leakage_gap() -> None:
    cutoff = SimpleNamespace(monthly=pd.DataFrame({"undiscounted_loss": [90.0]}))
    production = SimpleNamespace(monthly=pd.DataFrame({"undiscounted_loss": [72.0]}))

    comparison = build_fit_comparison(
        cutoff_result=cutoff,
        production_result=production,
        realized_loss=70.0,
        chain_ladder_projection=14.0,
        cutoff_fit_end="2018-12-01",
        production_fit_end=None,
    )

    assert comparison["fit_provenance"].tolist() == [
        "realized_ground_truth",
        "cutoff_clean_out_of_sample",
        "production_full_history_leaked_for_backtest",
        "pre_cutoff_chain_ladder",
    ]
    assert comparison["leakage_gap_dollars"].iloc[0] == 18.0
    assert comparison["leakage_gap_percentage_points"].iloc[0] == pytest.approx(25.7142857)
