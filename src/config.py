"""Layered, typed configuration for local and cloud execution."""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ProjectConfig:
    """Reproducibility settings shared by every credit-risk stage."""

    name: str
    seed: int


@dataclass(frozen=True)
class StateConfig:
    """Delinquency-state ordering and thresholds used by the Markov chain."""

    ordered: tuple[str, ...]
    dpd_thresholds: Mapping[str, int]
    absorbing: tuple[str, ...]
    zero_balance_codes: Mapping[str, str]


@dataclass(frozen=True)
class ModelConfig:
    """Bands, macro lags, and CECL assumptions used by downstream models."""

    mob_bands: tuple[int, ...]
    score_bands: tuple[int, ...]
    macro_lags: tuple[int, ...]
    discount_rate_annual: float
    fallback_lgd: float
    vintage_analysis_as_of: str
    vintage_maturity_mob: int
    vintage_cohort_grain: str
    vintage_primary_denominator: str
    conditional_regularization_c: float
    conditional_dpd150_regularization_c: float
    production_fit_end: str | None
    backtest_fit_end: str


@dataclass(frozen=True)
class SyntheticConfig:
    """Portfolio size and horizon controls for the synthetic credit panel."""

    number_of_loans: int
    first_vintage_year: int
    last_vintage_year: int
    max_observation_months: int
    unemployment_start: float
    score_mean: float
    score_std: float
    repurchase_rate: float
    repurchase_max_mob: int


@dataclass(frozen=True)
class ScenarioConfig:
    """Published-scenario selection and post-window reversion assumptions."""

    source_vintage: int
    horizon_quarters: int
    reversion_half_life_quarters: int
    long_run_unemployment_rate: float
    long_run_hpi_change_yoy: float
    pre_cutoff_unemployment_max: float
    full_history_unemployment_max: float


@dataclass(frozen=True)
class IngestConfig:
    """Parsing, deterministic sampling, and quality-gate controls for raw files."""

    delimiter: str
    header: bool
    date_format: str
    parse_mode: str
    hash_buckets: int
    max_records_per_file: int
    fatal_reasons: tuple[str, ...]
    max_exclusion_rate_per_reason: float
    max_exclusion_rate_overall: float


@dataclass(frozen=True)
class PathConfig:
    """Storage locations that can be local paths or cloud URIs."""

    raw_acquisition: str
    raw_performance: str
    curated: str
    macro: str
    output: str
    vintage_plot: str
    vintage_table: str
    vintage_annual_table: str
    vintage_backtest_table: str
    transition_empirical_table: str
    transition_coefficients: str
    transition_ground_truth: str
    transition_interpretations: str
    ecl_summary: str
    ecl_by_score_band: str
    ecl_by_vintage: str
    ecl_monthly: str
    ecl_plot: str
    lgd_validation: str
    lgd_coefficients: str
    ecl_reconciliation: str
    ecl_ground_truth_validation: str
    ecl_fit_comparison: str
    scenario_source: str
    scenario_summary: str
    scenario_monthly: str
    scenario_paths: str
    scenario_plot: str
    scenario_transition_attribution: str
    scenario_extrapolation: str


@dataclass(frozen=True)
class SparkConfig:
    """Execution settings supplied to SparkSession without code constants."""

    master: str
    app_name: str
    driver_memory: str
    shuffle_partitions: int


@dataclass(frozen=True)
class EngineConfig:
    """Complete validated configuration for one engine environment."""

    environment: str
    project: ProjectConfig
    states: StateConfig
    model: ModelConfig
    synthetic: SyntheticConfig
    scenarios: ScenarioConfig
    ingest: IngestConfig
    paths: PathConfig
    sample_fraction: float
    spark: SparkConfig


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Required configuration file does not exist: {path}")
    with path.open(encoding="utf-8") as stream:
        loaded = yaml.safe_load(stream) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Configuration root must be a mapping: {path}")
    return loaded


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _expand_environment(value: Any, variables: Mapping[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _expand_environment(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_environment(item, variables) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            raise ValueError(f"Configuration references unset environment variable: {name}")
        return variables[name]

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)


def load_config(environment: str, config_dir: str | Path) -> EngineConfig:
    """Load shared assumptions plus an environment overlay into typed config.

    Credit-state definitions and model assumptions remain identical across
    environments; only storage and compute controls belong in the overlay.
    """

    root = Path(config_dir)
    values = _deep_merge(_read_yaml(root / "base.yaml"), _read_yaml(root / f"{environment}.yaml"))
    variables = dict(os.environ)
    variables.setdefault(
        "CLE_DATA_ROOT", (Path(tempfile.gettempdir()) / "card-loss-engine").as_posix()
    )
    values = _expand_environment(values, variables)
    fraction = float(values["sample_fraction"])
    if not 0 < fraction <= 1:
        raise ValueError("sample_fraction must be in the interval (0, 1]")
    synthetic = SyntheticConfig(**values["synthetic"])
    ingest_values = dict(values["ingest"])
    ingest_values["fatal_reasons"] = tuple(ingest_values["fatal_reasons"])
    if synthetic.first_vintage_year > synthetic.last_vintage_year:
        raise ValueError("first_vintage_year cannot exceed last_vintage_year")
    if not 0 <= synthetic.repurchase_rate <= 1:
        raise ValueError("synthetic.repurchase_rate must be in the interval [0, 1]")
    if synthetic.repurchase_max_mob < 1:
        raise ValueError("synthetic.repurchase_max_mob must be positive")
    if values["model"]["vintage_cohort_grain"] not in {
        "monthly",
        "quarterly",
        "annual",
    }:
        raise ValueError("model.vintage_cohort_grain must be monthly, quarterly, or annual")
    mapped_states = {str(state) for state in values["states"]["zero_balance_codes"].values()}
    unknown_mapped_states = mapped_states - set(values["states"]["ordered"])
    if unknown_mapped_states:
        raise ValueError(
            f"zero_balance_codes reference undefined states: {sorted(unknown_mapped_states)}"
        )
    return EngineConfig(
        environment=environment,
        project=ProjectConfig(**values["project"]),
        states=StateConfig(
            ordered=tuple(values["states"]["ordered"]),
            dpd_thresholds=values["states"]["dpd_thresholds"],
            absorbing=tuple(values["states"]["absorbing"]),
            zero_balance_codes={
                str(code): str(state)
                for code, state in values["states"]["zero_balance_codes"].items()
            },
        ),
        model=ModelConfig(
            mob_bands=tuple(values["model"]["mob_bands"]),
            score_bands=tuple(values["model"]["score_bands"]),
            macro_lags=tuple(values["model"]["macro_lags"]),
            discount_rate_annual=float(values["model"]["discount_rate_annual"]),
            fallback_lgd=float(values["model"]["fallback_lgd"]),
            vintage_analysis_as_of=str(values["model"]["vintage_analysis_as_of"]),
            vintage_maturity_mob=int(values["model"]["vintage_maturity_mob"]),
            vintage_cohort_grain=str(values["model"]["vintage_cohort_grain"]),
            vintage_primary_denominator=str(values["model"]["vintage_primary_denominator"]),
            conditional_regularization_c=float(values["model"]["conditional_regularization_c"]),
            conditional_dpd150_regularization_c=float(
                values["model"]["conditional_dpd150_regularization_c"]
            ),
            production_fit_end=(
                str(values["model"]["production_fit_end"])
                if values["model"].get("production_fit_end") is not None
                else None
            ),
            backtest_fit_end=str(values["model"]["backtest_fit_end"]),
        ),
        synthetic=synthetic,
        scenarios=ScenarioConfig(**values["scenarios"]),
        ingest=IngestConfig(**ingest_values),
        paths=PathConfig(**values["paths"]),
        sample_fraction=fraction,
        spark=SparkConfig(**values["spark"]),
    )
