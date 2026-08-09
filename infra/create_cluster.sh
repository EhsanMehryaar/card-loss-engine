#!/usr/bin/env bash
set -euo pipefail

: "${CLE_S3_BUCKET:?Set CLE_S3_BUCKET to an existing S3 bucket}"
: "${CLE_EC2_KEY_NAME:?Set CLE_EC2_KEY_NAME to an EC2 key pair}"
: "${CLE_EC2_SUBNET_ID:?Set CLE_EC2_SUBNET_ID to a subnet ID}"

# Pinned current EMR 7.x release: reproducible platform rather than floating latest.
RELEASE_LABEL="${CLE_EMR_RELEASE_LABEL:-emr-7.13.0}"
# m5.xlarge supplies 4 vCPU/16 GiB; it matches the explicit 3-core/9-GiB executor.
PRIMARY_INSTANCE_TYPE="${CLE_PRIMARY_INSTANCE_TYPE:-m5.xlarge}"
# EMR requires exactly one primary for this non-HA development topology.
PRIMARY_INSTANCE_COUNT="${CLE_PRIMARY_INSTANCE_COUNT:-1}"
CORE_INSTANCE_TYPE="${CLE_CORE_INSTANCE_TYPE:-m5.xlarge}"
# Three core nodes provide 12 worker vCPU and enough parallelism for ~20M rows.
CORE_INSTANCE_COUNT="${CLE_CORE_INSTANCE_COUNT:-3}"
# One hour idle termination bounds the cost of a forgotten development cluster.
IDLE_TIMEOUT_SECONDS="${CLE_IDLE_TIMEOUT_SECONDS:-3600}"
SERVICE_ROLE="${CLE_EMR_SERVICE_ROLE:-EMR_DefaultRole}"
INSTANCE_PROFILE="${CLE_EMR_EC2_INSTANCE_PROFILE:-EMR_EC2_DefaultRole}"

aws emr create-cluster \
  --name "card-loss-engine" \
  --release-label "${RELEASE_LABEL}" \
  --applications Name=Spark \
  --service-role "${SERVICE_ROLE}" \
  --ec2-attributes "KeyName=${CLE_EC2_KEY_NAME},SubnetId=${CLE_EC2_SUBNET_ID},InstanceProfile=${INSTANCE_PROFILE}" \
  --instance-groups \
    "InstanceGroupType=MASTER,InstanceCount=${PRIMARY_INSTANCE_COUNT},InstanceType=${PRIMARY_INSTANCE_TYPE}" \
    "InstanceGroupType=CORE,InstanceCount=${CORE_INSTANCE_COUNT},InstanceType=${CORE_INSTANCE_TYPE}" \
  --log-uri "s3://${CLE_S3_BUCKET}/emr-logs/" \
  --auto-termination-policy "IdleTimeout=${IDLE_TIMEOUT_SECONDS}" \
  --bootstrap-actions "Name=card-loss-engine-bootstrap,Path=s3://${CLE_S3_BUCKET}/code/bootstrap.sh" \
  --tags Project=card-loss-engine ManagedBy=infra-create-cluster
