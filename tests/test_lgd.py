import logging

import pandas as pd

from src.model.lgd import fit_lgd_model, lgd_validation_by_era


def _observations() -> pd.DataFrame:
    records = []
    for band in ("FICO_660-699", "FICO_700-739"):
        for index, hpi in enumerate((-0.10, -0.05, 0.0, 0.05, 0.10) * 5):
            recovery = 0.70 + 0.50 * hpi
            records.append(
                {
                    "score_band": band,
                    "hpi_change_yoy": hpi,
                    "upb_bom": 100.0,
                    "net_sales_proceeds": recovery * 100.0,
                    "foreclosure_costs": 0.0,
                    "exit_reason": "ChargeOff",
                    "vintage": f"{2006 + index % 8}-01",
                }
            )
    return pd.DataFrame.from_records(records)


def test_lgd_is_score_segmented_and_macro_conditioned() -> None:
    observations = _observations()
    model = fit_lgd_model(observations, fallback_lgd=0.45, min_observations=20)

    assert model.predict_lgd("FICO_660-699", -0.10) > model.predict_lgd("FICO_660-699", 0.10)
    validation = lgd_validation_by_era(observations, model)
    assert (validation["realized_lgd"] - validation["modeled_lgd"]).abs().max() < 1e-12


def test_sparse_band_logs_and_uses_configured_fallback(caplog) -> None:
    observations = _observations().head(3)
    with caplog.at_level(logging.WARNING):
        model = fit_lgd_model(
            observations,
            fallback_lgd=0.45,
            score_bands=("FICO_660-699",),
            min_observations=20,
        )

    assert model.predict_lgd("FICO_660-699", -0.25) == 0.45
    assert "Using fallback LGD" in caplog.text
