# Card Loss Engine

[![CI](https://github.com/EhsanMehryaar/card-loss-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/EhsanMehryaar/card-loss-engine/actions/workflows/ci.yml)

Production-shaped consumer credit loss forecasting and CECL allowance engine.

## What this is

Card Loss Engine forecasts vintage and roll-rate credit losses and produces a
CECL-oriented allowance view for a consumer credit portfolio. It covers the
workflow from raw account histories through validated account-month panels,
vintage development, transition estimation, and auditable model outputs.

The core model is a delinquency-state Markov chain whose transition
probabilities vary with seasoning, borrower risk, and macroeconomic conditions.
This complements the vintage chain-ladder baseline: the vintage view provides a
transparent benchmark, while macro-conditioned roll rates can respond to
scenario paths that are outside the observed development window.

### Key results

All modeling results below use the **local 25,000-loan synthetic portfolio**.
At the 2018-12 cutoff, realized future net loss is $71.83M. The cutoff-clean M6
fit, estimated only through 2018-12, projects $78.67M: +$6.84M / +9.51% error.
The pre-cutoff M4 chain ladder projects $14.41M: -$57.43M / -79.94% error. M6
therefore reduces absolute error by **88.1%** out of sample. For comparison, the
production full-history fit projects $72.42M, but that $0.59M / 0.82% error is
leaked for this backtest; leakage removes $6.25M of apparent error, or 8.70
percentage points.

The chain-ladder cohort backtest records 4.71% MAPE on 2008–2010 and 63.51% on
2011+ cohorts. The conditional transition model recovers all eleven synthetic
ground-truth macro slopes with correct signs. Under the production full-history
fit and published 2019 Federal Reserve paths, lifetime ECL is $20.75M baseline,
$50.57M adverse (+143.76%), and $161.34M severely adverse (+677.65%).
Current→DPD30 produces the largest severe-versus-baseline increase in expected
state flow. The EMR run preserves Current→DPD30 to four decimal places at 10×
portfolio scale: 0.7683% on 250,000 loans versus 0.7678% locally.

## Approach

`raw files → curated panel → vintage baseline → conditional transition matrices
→ PD × EAD × LGD → lifetime ECL → Federal Reserve scenarios`

- Spark performs raw ingestion, account-month panel construction, reconciliation,
  vintage aggregation, and compact transition-cell aggregation.
- Pandas, NumPy, and scikit-learn fit the small aggregated tables and iterate the
  Markov state vectors. ChargeOff, Prepaid, and Repurchased remain separate
  absorbing competing risks.
- Production uses all available history. Ground-truth comparisons use the
  separately configured cutoff-clean fit and exclude histories censored before
  the reporting cutoff.

## Repository tour

- `src/ingest/` and `src/panel/`: data contracts, quality gates, and Spark panel.
- `src/model/`: vintage, transition, forecast, LGD, and CECL calculations.
- `src/scenarios/`: published-path loading, transformation, reversion, and M7 run.
- `config/`: common assumptions plus local and EMR execution overlays.
- `infra/`: EMR packaging, bootstrap, submission, diagnostics, and teardown.
- `tests/`: arithmetic, regression, reconciliation, Spark, and scenario tests.
- `docs/`: result artifacts, [model documentation](docs/model_documentation.md),
  [monitoring plan](docs/monitoring_plan.md), and AWS evidence.

## Limitations

The data and known truth are synthetic; behavioral realism on real consumers is
not established. The implementation uses mortgage collateral recovery and
amortizing EAD, while revolving-card EAD requires undrawn-line conversion. The
transition specification was tuned three times while consulting synthetic
truth. Severe scenarios extrapolate beyond observed macro support, with
uncertainty absent from point estimates. A pre-2007 crisis backtest and executed
PSI/stability monitoring were scoped but not implemented. See the full
[limitations and governance discussion](docs/model_documentation.md#limitations-and-weaknesses).

## Quick start

```bash
python -m pip install -e ".[dev]"
python -m src.cli config --env local
python -m src.cli synthetic --env local
python -m src.cli ingest --env local
python -m src.cli ingest --env local --sample-fraction 0.10
python -m src.cli panel --env local
python -m src.cli vintage --env local
python -m src.cli transitions --env local
python -m src.cli ecl --env local
python -m src.cli scenarios --env local
make test
```

All paths and Spark settings live in YAML configuration. Transformation code is
therefore portable from local files to S3 without logic changes.

## Running at scale

The headline result is scale invariance: the 250,000-loan EMR run reproduced the
25,000-loan local run's credit dynamics across a 10x data increase and a
different execution engine. In particular, Current-to-DPD30 agrees to four
decimal places.

| Measure | EMR, 250k loans | Local, 25k loans |
|---|---:|---:|
| Current -> DPD30 | 0.7683% | 0.7678% |
| ChargeOff | 5.922% | 5.740% |
| Prepaid | 61.552% | 61.940% |
| Repurchased | 0.776% | 0.716% |
| Censored | 31.750% | 32.272% |

The EMR exit counts reconcile exactly:
`14,805 + 153,879 + 1,941 + 79,375 = 250,000`. Its transition denominator also
reconciles exactly: `20,008,094 = 20,258,094 account-months - 250,000 terminal
loan rows`. This is evidence that the distributed implementation computed the
same portfolio mechanics, not merely that its jobs completed.

The run used Amazon EMR 7.13.0 in `us-east-1`, with one primary and three
`m5.xlarge` core nodes (12 worker vCPU), and completed in about 65 minutes for an
estimated cost of $1.05. The input contained 250,000 loans, 20,258,094
account-months, and about 1.2 GB of raw delimited data in 19 flat acquisition
files plus 19 flat performance files. Curated Parquet was 226 MB, a 5.3x
compression ratio.

| Stage | Spark stages | Tasks | Shuffle | Result |
|---|---:|---:|---:|---|
| Ingest | 20 | 424 | 1.09 GB | 19 acquisition + 38 performance files, 226 MB |
| Panel | 19 | 235 | 2.38 GB | 38 files, 278 MB, 20,258,094 rows |
| Transitions | 11 | 153 | 0.02 GB | 32 files, 858,394 aggregated rows |

`maxRecordsPerFile=1,000,000` produced the intended split. The average vintage
contains `20,258,094 / 19 = 1,066,215` performance rows, 6.6% above the cap, so
each of the 19 vintages produced two files: 38 total.

Panel shuffle grew from 213 MB at 2.02 million rows locally to 2,380 MB at 20.26
million rows on EMR: 11.2x shuffle for 10.0x data. The mild superlinearity comes
from more distinct `loan_id` keys touching more exchange partitions. The panel
remains skew-resistant because it partitions by `loan_id` and each key's row
count is bounded by the observation window.

An ingestion tuning check compared 32 shuffle partitions (424 tasks, about
47,800 rows/task, 89.5 seconds) with 200 partitions (2,446 tasks, about 8,300
rows/task, 96.0 seconds). Creating 5.8x as many tasks made wall clock 7.3% worse
through fixed scheduling, serialization, and commit overhead. The timing effect
is bounded because ingestion is dominated by S3 reads and CSV parsing; it should
not be generalized to the more shuffle-heavy panel stage.

The durable evidence locations and replay instructions are recorded in
[`docs/emr_artifacts/`](docs/emr_artifacts/README.md).

## AWS portability

The initial local-to-EMR migration changed deployment configuration and
`infra/`, with no changes to credit transformations or modeling equations. See
the [deployment-surface diff summary](docs/running_on_aws.md#portability-diff-summary).
The completed run then exposed four environment assumptions that local tests did
not: Python 3.11 rejected PEP 701 f-strings, Hive-style raw paths injected a
colliding column, Spark used a different Python interpreter than bootstrap, and
YARN client-mode `--files` did not localize configuration to the driver. Their
fixes are compatibility, fixture-layout, and submission hardening; the same M2,
M3, and M5 business transformations still run locally and on YARN.

See [`docs/running_on_aws.md`](docs/running_on_aws.md) for the corrected runbook
and [`docs/assumptions_log.md`](docs/assumptions_log.md) for the findings and
design rationale.

The Milestone 6 allowance result for the **local 25,000-loan portfolio**—not the
250,000-loan EMR scale run—plus its PD/EAD/LGD decompositions, LGD validation,
monthly loss path, and chain-ladder reconciliation are summarized in
[`docs/m6_results.md`](docs/m6_results.md).

The published-scenario source mapping, reversion assumption, three ECL paths,
transition attribution, and extrapolation diagnostics are in
[`docs/m7_results.md`](docs/m7_results.md).

Local Spark ingestion requires Java 17. On native Windows, Hadoop's local-file
adapter also requires matching `winutils.exe` and `hadoop.dll` binaries available
through `HADOOP_HOME`; Linux and managed Spark environments do not need them.
