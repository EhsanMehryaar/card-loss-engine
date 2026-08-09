# EMR 250k run summary

The production-scale validation ran on Amazon EMR 7.13.0 in `us-east-1` using
one primary and three `m5.xlarge` core nodes (12 worker vCPU). It processed
250,000 loans and 20,258,094 account-months in approximately 65 minutes end to
end for an estimated cost of $1.05. The cluster was terminated after the run.

## Storage and stage metrics

The raw fixture was approximately 1.2 GB: 19 flat acquisition files and 19 flat
performance files. Ingestion produced 226 MB of curated Parquet, a 5.3x
compression ratio.

| Stage | Spark stages | Tasks | Shuffle | Output |
|---|---:|---:|---:|---|
| Ingest | 20 | 424 | 1.09 GB | 19 acquisition + 38 performance files, 226 MB |
| Panel | 19 | 235 | 2.38 GB | 38 files, 278 MB, 20,258,094 rows |
| Transitions | 11 | 153 | 0.02 GB | 32 files, 858,394 aggregated rows |

The performance output count follows directly from the configured
`maxRecordsPerFile=1,000,000`: `20,258,094 / 19 = 1,066,215` average rows per
vintage. Each vintage is about 6.6% above the cap and therefore splits into two
files, giving 38 performance files.

## Reconciliation and scale invariance

| Measure | EMR, 250k loans | Local, 25k loans |
|---|---:|---:|
| Current -> DPD30 | 0.7683% | 0.7678% |
| ChargeOff | 5.922% | 5.740% |
| Prepaid | 61.552% | 61.940% |
| Repurchased | 0.776% | 0.716% |
| Censored | 31.750% | 32.272% |

Exit counts reconcile exactly:
`14,805 + 153,879 + 1,941 + 79,375 = 250,000`. Transition counts also reconcile:
`20,008,094 = 20,258,094 account-month rows - 250,000 terminal loan rows`.

Panel shuffle grew from 213 MB for 2.02 million local rows to 2,380 MB for 20.26
million EMR rows, or 11.2x shuffle for 10.0x data. More distinct `loan_id` keys
touch more exchange partitions, causing mild superlinearity. Per-key cardinality
is bounded by the observation window, so loan partitioning remains resistant to
single-key skew.

## Shuffle-partition experiment

| Shuffle partitions | Tasks | Rows/task | Wall clock |
|---:|---:|---:|---:|
| 32 | 424 | about 47,800 | 89.5 s |
| 200 | 2,446 | about 8,300 | 96.0 s |

The 200-partition run created 5.8x as many tasks and was 7.3% slower. Fixed
task-scheduling, serialization, and commit costs explain the mechanism. The
wall-clock penalty remained modest because ingestion is dominated by S3 input
and CSV parsing; the experiment does not establish the best setting for a
shuffle-heavy panel build.

EMR's default dynamic allocation overrode the requested three static executors
and the completed run reported 11 executors. The run record preserves that fact.
Future submissions explicitly disable dynamic allocation so
`spark.executor.instances=3` is authoritative.
