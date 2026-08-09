#!/usr/bin/env bash
set -euo pipefail

: "${CLE_S3_BUCKET:?Set CLE_S3_BUCKET to an existing S3 bucket}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${REPO_ROOT}/build/emr"

mkdir -p "${BUILD_DIR}"
rm -f "${BUILD_DIR}/src.zip"
(
  cd "${REPO_ROOT}"
  zip -qr "${BUILD_DIR}/src.zip" src -x '*/__pycache__/*' '*.pyc'
)

aws s3 cp "${BUILD_DIR}/src.zip" "s3://${CLE_S3_BUCKET}/code/src.zip"
aws s3 cp "${REPO_ROOT}/config/base.yaml" "s3://${CLE_S3_BUCKET}/code/base.yaml"
aws s3 cp "${REPO_ROOT}/config/emr.yaml" "s3://${CLE_S3_BUCKET}/code/emr.yaml"
aws s3 cp "${REPO_ROOT}/infra/bootstrap.sh" "s3://${CLE_S3_BUCKET}/code/bootstrap.sh"
aws s3 cp "${REPO_ROOT}/infra/config_value.py" "s3://${CLE_S3_BUCKET}/code/config_value.py"
aws s3 cp "${REPO_ROOT}/infra/generate_synthetic.py" "s3://${CLE_S3_BUCKET}/code/generate_synthetic.py"
aws s3 cp "${REPO_ROOT}/infra/generate_and_stage_data.sh" "s3://${CLE_S3_BUCKET}/code/generate_and_stage_data.sh"
aws s3 cp "${REPO_ROOT}/infra/run_spark_stage.py" "s3://${CLE_S3_BUCKET}/code/run_spark_stage.py"
for script in "${REPO_ROOT}"/infra/submit_*.sh "${REPO_ROOT}/infra/teardown.sh"; do
  aws s3 cp "${script}" "s3://${CLE_S3_BUCKET}/code/$(basename "${script}")"
done

echo "Staged code and scripts under s3://${CLE_S3_BUCKET}/code/"
