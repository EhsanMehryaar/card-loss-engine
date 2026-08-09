# Running the Spark stages on Amazon EMR

This runbook prepares and runs M2 ingestion, M3 panel construction, and the M5
transition-count aggregation on Amazon EMR against S3. It deliberately does not
run the single-node conditional model on the cluster. The commands below are
reviewable instructions; repository preparation never calls AWS automatically.

## Prerequisites

- AWS CLI v2 authenticated to an account allowed to create/terminate EMR
  clusters and read/write the selected S3 bucket.
- An existing S3 bucket, EC2 key pair, VPC subnet, EMR service role, and EMR EC2
  instance profile in one Region.
- `bash`, `zip`, Python 3.11, and PyYAML on the packaging machine.
- SSH access to the EMR primary node if running the commands interactively.
- Sufficient local disk on the primary node for approximately 1.2 GB of raw
  delimited data plus temporary generation overhead.

Set deployment inputs; no bucket, subnet, key, or credential is committed:

```bash
export CLE_S3_BUCKET="your-existing-bucket"
export CLE_EC2_KEY_NAME="your-key-pair"
export CLE_EC2_SUBNET_ID="subnet-xxxxxxxx"
export CLE_EMR_SERVICE_ROLE="EMR_DefaultRole"
export CLE_EMR_EC2_INSTANCE_PROFILE="EMR_EC2_DefaultRole"
```

`CLE_S3_BUCKET` is also consumed by the ordinary config loader when it expands
`${CLE_S3_BUCKET}` in `config/emr.yaml`. Missing variables fail immediately.

## Sizing

The pinned platform is `emr-7.13.0` with Spark, one primary and three core
`m5.xlarge` nodes. Each core node has 4 vCPU and 16 GiB. One 3-core, 9-GiB
executor per core node leaves one vCPU and about 7 GiB for YARN, the operating
system, and memory overhead. That gives three executors and nine executor cores.
The 32 shuffle partitions are about 3.6 times the usable executor cores and 2.7
times the 12 physical worker cores: close to the 2–3x starting rule while
avoiding the hundreds of tiny tasks caused by the former placeholder of 400.

The event log is written to `s3://${CLE_S3_BUCKET}/spark-events/`, so the Spark
application can be replayed in a History Server after cluster termination.

## Scale projection and bounded-memory generation

A local 5,000-loan measurement generated 405,677 account-months in 5.14 seconds
with 0.36 GB peak traced allocation. Linear projection for 250,000 loans is:

| Measure | Projection |
|---|---:|
| Loans | 250,000 |
| Account-months | about 20.3 million |
| Pure generation wall clock | about 257 seconds (4.3 minutes) |
| Monolithic peak allocation | about 18 GB |
| Acquisition text | about 22 MB |
| Performance text | about 1.18 GB |
| Total raw text including macro | about 1.20 GB |

The time projection is inside five minutes, but the memory projection exceeds
both the 8-GB review threshold and safe primary-node headroom. Therefore
`infra/generate_synthetic.py` generates one origination year at a time, preserves
one shared calendar macro path, gives every chunk a deterministic seed and
globally unique loan IDs, writes the two raw feeds, and releases the chunk before
continuing. Peak working memory is projected below 1.2 GB. CSV serialization and
S3 staging add approximately 5–10 minutes depending on primary-node disk and S3
throughput.

Generate on the primary node—not on a laptop—and then stage to S3:

```bash
bash infra/generate_and_stage_data.sh
```

## Commands in order

From the repository checkout on the packaging machine, package and upload the
bootstrap assets **before** creating the cluster. This ordering is mandatory:
EMR reads `bootstrap.sh` from S3 while the nodes launch, so cluster creation will
fail if `package.sh` has not staged it yet.

```bash
bash infra/package.sh
bash infra/create_cluster.sh > cluster.json
export CLE_EMR_CLUSTER_ID="$(python3 -c 'import json; print(json.load(open("cluster.json"))["ClusterId"])')"
```

After the cluster reaches `WAITING`, SSH to the primary node. The submit and
generation scripts resolve files relative to a git checkout, so cloning the
repository is the first required step after SSH:

```bash
git clone <repository-url> card-loss-engine
cd card-loss-engine
export CLE_S3_BUCKET="your-existing-bucket"
```

Then generate, stage, and run the distributed stages:

```bash
bash infra/generate_and_stage_data.sh
bash infra/submit_ingest.sh
bash infra/submit_panel.sh
bash infra/submit_transitions.sh
```

Each submit script uses YARN client mode, `src.zip`, and explicit executor,
shuffle, and event-log settings read from the checkout's `config/emr.yaml`.
Because the driver runs in the submitting shell, it loads configuration directly
from `${REPO_ROOT}/config`; configuration is not distributed with `--files`.
Client mode preserves the existing access to `sparkContext.uiWebUrl`. The M5
command writes the compact aggregated cube to
`s3://${CLE_S3_BUCKET}/card-loss-engine/curated/transition_counts`; conditional
fitting can consume that small artifact on a single node later.

Planning ranges after cluster startup are 4–8 minutes for M2, 3–6 minutes for
M3, and 1–3 minutes for M5 aggregation. Including data generation/staging, allow
roughly 20–35 minutes; first-time cluster provisioning commonly adds another
5–10 minutes. Actual timings belong in `docs/emr_artifacts/` after the run.

## Cost and teardown

For a planning estimate in `us-east-1`, four on-demand `m5.xlarge` instances at
about $0.192 per instance-hour cost $0.768/hour for EC2. The EMR surcharge,
EBS, S3 requests/storage, and public IPv4 charges are additional; budget roughly
$1.00–$1.25 for a one-hour development run and verify the current price for the
chosen Region before launch. AWS bills EMR on EC2 per second with a one-minute
minimum. Pricing references: [Amazon EMR pricing](https://aws.amazon.com/emr/pricing/)
and [AWS m5.xlarge on-demand example](https://docs.aws.amazon.com/prescriptive-guidance/latest/optimize-costs-microsoft-workloads/right-size-selection.html).

Terminate explicitly even though the cluster has a one-hour idle policy:

```bash
bash infra/teardown.sh
```

Event logs, EMR logs, curated Parquet, transition counts, and output artifacts
remain in S3. Review or lifecycle/delete them separately when no longer needed.

## Path-agnostic verification

M2's `parquet_file_summary` constructs `org.apache.hadoop.fs.Path(path)` and
resolves the filesystem with `path.getFileSystem(hadoopConfiguration)`. For an
`s3://` path on EMR, that is the JVM-backed EMRFS/Hadoop filesystem and recursive
listing plus file sizes work without local `pathlib` assumptions. This private
`_jvm`/`_jsc` integration intentionally targets classic Spark: it would not work
unchanged under Spark Connect or Glue serverless, where those JVM handles are
not exposed.

A source scan confirms the distributed modules contain no literal S3 bucket,
credential, local filesystem root, or Spark master. `src/spark_session.py`
obtains master, application name, driver memory, and shuffle partitions from the
typed config. Submit-time executor and event-log settings are inherited when its
`SparkSession.builder.getOrCreate()` attaches to the YARN Spark configuration.

## Portability diff summary

EMR preparation changes these deployment surfaces only:

- `config/emr.yaml`: bucket substitution, 250k scale, executor/event-log sizing.
- `infra/`: packaging, provisioning, bounded-memory generation, stage submission,
  and teardown.
- `docs/` and the README: reproduction and artifact guidance.
- `tests/test_config.py`: verification that the required bucket expands and the
  EMR-only 250k scale override changes no synthetic DGP assumption.

No file under `src/` is changed. Therefore the M2, M3, and M5 aggregation
transformations used for EMR are exactly the locally accepted implementations.
