# EMR run artifacts

The 250,000-loan EMR 7.13.0 run completed successfully and the cluster was
terminated. The human-readable result record is in [`run_summary.md`](run_summary.md).
Durable evidence remains in these S3 prefixes:

- `s3://card-loss-engine-emh/spark-events/`: Spark event logs.
- `s3://card-loss-engine-emh/emr-logs/`: EMR/YARN application and node logs.
- `s3://card-loss-engine-emh/emr-artifacts/`: the redacted cluster summary JSON,
  S3 object listing, and captured run artifacts.

The event logs can be downloaded and replayed with a Spark 3.5-compatible local
History Server. That exposes the stage DAGs, task counts, shuffle bytes,
durations, executor activity, and failures after cluster termination, making the
reported metrics independently verifiable. For example, point
`spark.history.fs.logDirectory` at a local copy of the event-log prefix and run
`$SPARK_HOME/sbin/start-history-server.sh`.

Do not commit credentials, signed URLs, raw loan records, or unredacted cluster
identifiers. The bucket and prefixes are identifiers only and contain no access
credentials.
