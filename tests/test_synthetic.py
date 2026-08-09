from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from src.config import load_config
from src.ingest.synthetic import generate_portfolio

CONFIG_DIR = Path(__file__).parents[1] / "config"


def _tiny_config():
    config = load_config("local", CONFIG_DIR)
    return replace(
        config,
        synthetic=replace(config.synthetic, number_of_loans=12, max_observation_months=18),
    )


def test_generation_is_deterministic_and_histories_are_contiguous() -> None:
    config = _tiny_config()
    first = generate_portfolio(config)
    second = generate_portfolio(config)

    pd.testing.assert_frame_equal(first.panel, second.panel)
    gaps = first.panel.groupby("loan_id")["as_of_month"].diff().dropna()
    assert (gaps.dt.days.between(28, 31)).all()


def test_absorbing_event_is_terminal_and_panel_has_macro_signal() -> None:
    portfolio = generate_portfolio(_tiny_config())
    terminal = portfolio.panel[portfolio.panel["exit_reason"].notna()]

    assert len(terminal) == len(portfolio.acquisition)
    assert (terminal.groupby("loan_id").size() == 1).all()
    assert set(terminal["exit_reason"]) <= {
        "ChargeOff",
        "Prepaid",
        "Repurchased",
        "Censored",
    }
    assert terminal["next_delinquency_state"].isna().all()
    assert portfolio.panel["unemployment_rate"].notna().all()


def test_censored_loans_have_explicit_unobserved_terminal_transition() -> None:
    portfolio = generate_portfolio(_tiny_config())
    censored = portfolio.panel[portfolio.panel["exit_reason"] == "Censored"]

    assert not censored.empty
    assert censored["is_censored"].all()
    assert censored["next_delinquency_state"].isna().all()
    dates = portfolio.acquisition.set_index("loan_id").loc[censored["loan_id"], "censoring_date"]
    assert dates.notna().all()


def test_histories_are_stable_when_portfolio_grows() -> None:
    small_config = _tiny_config()
    large_config = replace(
        small_config,
        synthetic=replace(small_config.synthetic, number_of_loans=24),
    )
    small = generate_portfolio(small_config)
    large = generate_portfolio(large_config)
    first_ids = set(small.acquisition["loan_id"])

    pd.testing.assert_frame_equal(
        small.panel.reset_index(drop=True),
        large.panel[large.panel["loan_id"].isin(first_ids)].reset_index(drop=True),
    )


def test_repurchases_are_early_code_06_absorbing_exits() -> None:
    config = load_config("local", CONFIG_DIR)
    config = replace(
        config,
        synthetic=replace(
            config.synthetic, number_of_loans=3_000, max_observation_months=30
        ),
    )
    portfolio = generate_portfolio(config)
    repurchased = portfolio.panel[portfolio.panel["exit_reason"] == "Repurchased"]
    rate = repurchased["loan_id"].nunique() / len(portfolio.acquisition)

    assert 0.005 <= rate <= 0.01
    assert repurchased["months_on_book"].between(1, 24).all()
    assert not repurchased["is_censored"].any()
    assert repurchased["next_delinquency_state"].isna().all()
    raw_terminal = portfolio.performance.merge(
        repurchased[["loan_id", "as_of_month"]],
        on=["loan_id", "as_of_month"],
        how="inner",
    )
    assert raw_terminal["zero_balance_code"].eq("06").all()


@pytest.fixture(scope="session")
def low_score_recovery_defaults() -> pd.DataFrame:
    """Generate one reusable, stressed sample with ample observed defaults."""

    config = load_config("local", CONFIG_DIR)
    config = replace(
        config,
        synthetic=replace(
            config.synthetic,
            number_of_loans=600,
            max_observation_months=120,
            score_mean=610.0,
            score_std=30.0,
        ),
    )
    portfolio = generate_portfolio(config)
    defaults = portfolio.panel[portfolio.panel["exit_reason"] == "ChargeOff"].copy()
    defaults["recovery_rate"] = (
        defaults["net_sales_proceeds"] - defaults["foreclosure_costs"]
    ) / defaults["upb_bom"]
    return defaults


def test_recovery_contains_collateral_and_macro_signal(
    low_score_recovery_defaults: pd.DataFrame,
) -> None:
    defaults = low_score_recovery_defaults

    assert len(defaults) >= 40
    assert defaults["recovery_rate"].corr(defaults["orig_ltv"]) < -0.20
    assert defaults["recovery_rate"].corr(defaults["hpi_change_yoy"]) > 0.20
