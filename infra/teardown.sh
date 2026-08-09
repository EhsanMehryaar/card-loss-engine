#!/usr/bin/env bash
set -euo pipefail

: "${CLE_S3_BUCKET:?Set CLE_S3_BUCKET to an existing S3 bucket}"
: "${CLE_EMR_CLUSTER_ID:?Set CLE_EMR_CLUSTER_ID to the cluster ID}"

aws emr terminate-clusters --cluster-ids "${CLE_EMR_CLUSTER_ID}"

cat <<EOF
Termination requested for ${CLE_EMR_CLUSTER_ID}.
Durable artifacts remain at:
  s3://${CLE_S3_BUCKET}/spark-events/
  s3://${CLE_S3_BUCKET}/emr-logs/
  s3://${CLE_S3_BUCKET}/card-loss-engine/curated/
  s3://${CLE_S3_BUCKET}/card-loss-engine/output/
EOF
