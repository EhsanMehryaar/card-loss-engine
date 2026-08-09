#!/usr/bin/env bash
set -euo pipefail

STAGE=ingest
source "$(dirname "${BASH_SOURCE[0]}")/submit_stage.sh"
