#!/usr/bin/env bash
set -euo pipefail

: "${CLE_S3_BUCKET:?Set CLE_S3_BUCKET to an existing S3 bucket}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Keep multi-GB generation on the EMR primary node; never route it through a laptop.
LOCAL_DATA_ROOT="${CLE_EMR_LOCAL_DATA_ROOT:-/mnt/card-loss-engine-generation}"

PYTHONPATH="${REPO_ROOT}:${PYTHONPATH:-}" python3 "${REPO_ROOT}/infra/generate_synthetic.py" \
  --env emr \
  --config-dir "${REPO_ROOT}/config" \
  --output-root "${LOCAL_DATA_ROOT}"

aws s3 sync \
  "${LOCAL_DATA_ROOT}/raw/acquisition/" \
  "s3://${CLE_S3_BUCKET}/card-loss-engine/raw/acquisition/"
aws s3 sync \
  "${LOCAL_DATA_ROOT}/raw/performance/" \
  "s3://${CLE_S3_BUCKET}/card-loss-engine/raw/performance/"
aws s3 cp \
  "${LOCAL_DATA_ROOT}/raw/macro/unemployment.csv" \
  "s3://${CLE_S3_BUCKET}/card-loss-engine/raw/macro/unemployment.csv"

echo "Raw cluster fixture staged under s3://${CLE_S3_BUCKET}/card-loss-engine/raw/"
