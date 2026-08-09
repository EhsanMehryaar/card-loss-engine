from dataclasses import replace
from pathlib import Path

import pytest

from src.config import load_config
from src.spark_session import build_spark_session

CONFIG_DIR = Path(__file__).parents[1] / "config"


@pytest.fixture(scope="session")
def quality_config():
    return load_config("local", CONFIG_DIR)


@pytest.fixture(scope="session")
def spark(quality_config):
    config = replace(
        quality_config,
        spark=replace(
            quality_config.spark,
            master="local[2]",
            app_name="card-loss-engine-tests",
            shuffle_partitions=2,
        ),
    )
    session = build_spark_session(config)
    session.sparkContext.setLogLevel("ERROR")
    yield session
    session.stop()
