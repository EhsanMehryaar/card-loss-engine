#!/usr/bin/env bash
set -euo pipefail

: "${STAGE:?Set STAGE before sourcing submit_stage.sh}"
: "${CLE_S3_BUCKET:?Set CLE_S3_BUCKET to an existing S3 bucket}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_FILE="${REPO_ROOT}/config/emr.yaml"
CONFIG_VALUE=(python3 "${REPO_ROOT}/infra/config_value.py" "${CONFIG_FILE}")

EXECUTOR_MEMORY="$("${CONFIG_VALUE[@]}" spark_submit.executor_memory)"
EXECUTOR_CORES="$("${CONFIG_VALUE[@]}" spark_submit.executor_cores)"
NUM_EXECUTORS="$("${CONFIG_VALUE[@]}" spark_submit.num_executors)"
SPARK_MASTER="$("${CONFIG_VALUE[@]}" spark.master)"
DRIVER_MEMORY="$("${CONFIG_VALUE[@]}" spark.driver_memory)"
SHUFFLE_PARTITIONS="$("${CONFIG_VALUE[@]}" spark.shuffle_partitions)"
EVENT_LOG_ENABLED="$("${CONFIG_VALUE[@]}" spark_submit.event_log_enabled)"
EVENT_LOG_DIR="$("${CONFIG_VALUE[@]}" spark_submit.event_log_dir)"

# Client mode preserves access to sparkContext.uiWebUrl for the existing UI summary.
spark-submit \
  --master "${SPARK_MASTER}" \
  --deploy-mode client \
  --driver-memory "${DRIVER_MEMORY}" \
  --py-files "s3://${CLE_S3_BUCKET}/code/src.zip" \
  --conf "spark.executor.memory=${EXECUTOR_MEMORY}" \
  --conf "spark.executor.cores=${EXECUTOR_CORES}" \
  --conf "spark.executor.instances=${NUM_EXECUTORS}" \
  --conf "spark.sql.shuffle.partitions=${SHUFFLE_PARTITIONS}" \
  --conf "spark.eventLog.enabled=${EVENT_LOG_ENABLED}" \
  --conf "spark.eventLog.dir=${EVENT_LOG_DIR}" \
  "${REPO_ROOT}/infra/run_spark_stage.py" "${STAGE}" \
  --env emr \
  --config-dir "${REPO_ROOT}/config"
