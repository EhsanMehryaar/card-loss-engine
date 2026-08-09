#!/usr/bin/env bash
set -euo pipefail

# M2/M3/M5 use Spark SQL plus YAML config parsing, so bootstrap stays thin.
# statsmodels/scikit-learn are single-node modeling dependencies and are not
# installed across this cluster merely to run the distributed stages.
sudo /usr/bin/python3 -m pip install --disable-pip-version-check \
  "PyYAML==6.0.2" \
  "fsspec==2025.3.2"
