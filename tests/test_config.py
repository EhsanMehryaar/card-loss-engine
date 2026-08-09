from dataclasses import replace
from pathlib import Path

from src.config import load_config

CONFIG_DIR = Path(__file__).parents[1] / "config"


def test_local_overlay_loads_shared_and_environment_values(monkeypatch) -> None:
    monkeypatch.setenv("CLE_DATA_ROOT", "C:/scratch/card-loss-engine-test")
    config = load_config("local", CONFIG_DIR)

    assert config.project.seed == 1729
    assert config.states.absorbing == ("ChargeOff", "Prepaid", "Repurchased")
    assert config.states.zero_balance_codes["06"] == "Repurchased"
    assert config.spark.master == "local[*]"
    assert config.sample_fraction == 1.0
    assert config.paths.curated == "C:/scratch/card-loss-engine-test/curated"
    assert config.ingest.max_records_per_file == 5_000_000
    assert config.model.conditional_regularization_c == 10.0
    assert config.model.conditional_dpd150_regularization_c == 0.002


def test_emr_overlay_changes_scale_not_model_assumptions(monkeypatch) -> None:
    monkeypatch.setenv("CLE_S3_BUCKET", "unit-test-bucket")
    local = load_config("local", CONFIG_DIR)
    emr = load_config("emr", CONFIG_DIR)

    assert local.states == emr.states
    assert local.model == emr.model
    assert emr.synthetic == replace(local.synthetic, number_of_loans=250_000)
    assert emr.paths.curated.startswith("s3://")
    assert emr.ingest.max_records_per_file == 1_000_000
