# EMR run artifacts

This directory is the landing area for evidence from an actual cluster run. Do
not commit credentials, signed URLs, raw loan records, or an S3 bucket name.

Capture:

- `cluster_summary.json`: redacted `aws emr describe-cluster` output with release,
  instance groups, start/end time, auto-termination policy, and cluster ID hashed
  or removed.
- `s3_listing.txt`: key prefixes, object counts, and bytes for raw, curated panel,
  transition counts, event logs, and EMR logs; redact the bucket name.
- `spark_ui_ingest.png`, `spark_ui_panel.png`, and
  `spark_ui_transitions.png`: stages, task counts, duration, input/output, spill,
  and shuffle panels.
- `throughput.md`: rows and bytes per second, stage wall clocks, Parquet file
  counts/sizes, shuffle totals, and any executor retries.
- `event_log_manifest.txt`: durable event-log object keys and checksums. The
  binary event logs remain in S3 and can be loaded into a compatible local Spark
  History Server after teardown.
- `stage_reports/`: the JSON reports printed by the three submit scripts.

The directory is descriptive until an authorized AWS run is performed. Absence
of screenshots or logs must never be represented as a completed cluster run.
