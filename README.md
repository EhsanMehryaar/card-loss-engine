# Card Loss Engine

Production-shaped consumer credit loss forecasting and CECL allowance engine.

This repository is being built incrementally. Milestone 1 provides layered,
typed configuration and deterministic synthetic Fannie Mae-shaped loan data.

## Milestone 1 quick start

```bash
python -m pip install -e ".[dev]"
python -m src.cli config --env local
python -m src.cli synthetic --env local
python -m src.cli ingest --env local
python -m src.cli ingest --env local --sample-fraction 0.10
python -m src.cli panel --env local
python -m src.cli vintage --env local
python -m src.cli transitions --env local
pytest
```

All paths and Spark settings live in YAML configuration. Transformation code is
therefore portable from local files to S3 without logic changes.

## EMR portability

The EMR production enablement changes only deployment configuration, `infra/`
scripts, and documentation; one config test records the intentional scale
override. It changes zero lines under `src/`, including the M2 ingestion, M3
panel, and M5 transition-aggregation logic. The same Spark functions run locally
and on YARN; only their configured paths and compute resources differ. See
[`docs/running_on_aws.md`](docs/running_on_aws.md) for the reproducible runbook
and scope proof.

Local Spark ingestion requires Java 17. On native Windows, Hadoop's local-file
adapter also requires matching `winutils.exe` and `hadoop.dll` binaries available
through `HADOOP_HOME`; Linux and managed Spark environments do not need them.
