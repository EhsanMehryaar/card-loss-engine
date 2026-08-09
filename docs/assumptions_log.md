# Modeling assumptions log

This file is cumulative and must be appended to at every subsequent milestone.

## Milestone 1

- Delinquency maps to Current at less than 30 DPD, then DPD30, DPD60, DPD90,
  DPD120, and DPD150 in 30-day increments. This preserves operational roll-rate
  stages without implying false precision within a bucket.
- Default is ChargeOff at 180+ DPD or a terminal charge-off disposition code.
  Six missed-payment buckets are sufficiently severe to represent loss
  recognition and match common mortgage performance-file conventions.
- Prepayment is a competing absorbing risk. Once prepaid, an account can no
  longer default, so omitting this exit would overstate lifetime loss.
- Active loans reaching the 120-month synthetic observation boundary are right
  censored. Their terminal rows have no observed next state and must not enter
  transition-count denominators.
- Real-data censoring is not supplied by acquisition data. Milestone 3 derives
  it when an account's last observed performance row has neither a zero-balance
  code nor a terminal disposition.
- Recovery depends on original LTV, contemporaneous year-over-year HPI change,
  original score, and idiosyncratic noise. Foreclosure costs are modeled
  separately; LGD is one minus net recovery after those costs.
- Repurchase is an independent 0.75% competing exit generated from a dedicated
  deterministic random stream. Discovery MOB follows an early-weighted geometric
  distribution capped at 24 months, reflecting rep-and-warranty breaches found
  shortly after acquisition. Code `06` carries zero proceeds, costs, and credit
  loss and terminates the loan's subsequent exposure.

## Milestone 2

- Local development samples accounts with a project-seeded hash of `loan_id`,
  not rows. Acquisition selection is joined back to monthly performance so no
  selected account has a partial history caused by sampling.
- Missing identifiers, duplicate business keys, and orphan performance records
  are structural and fatal at any count. Plausible field-level dirt is excluded
  up to 1% per reason and 2% overall; every count and rate remains visible.
- Curated acquisition and performance data are partitioned by origination year,
  matching the vintage-oriented access pattern used by later modeling stages.

## Contracts recorded for Milestone 5

- ChargeOff and Prepaid identity rows are injected into transition matrices and
  are never estimated from counts. Absorbed accounts leave the panel, so their
  empirical origin rows contain no observations; normalizing those zero rows
  would divide by zero and violate the row-sum-to-one invariant.
- Delinquent-state prepayment is pooled into a constant hazard because observed
  DPD30 through DPD150 prepayment counts are only 79, 30, 17, 14, and 8. Separate
  multinomial outcomes would exhibit quasi-separation. Delinquent conditional
  models therefore estimate cure, stay, and roll-forward only. Current retains
  its three-way Current/DPD30/Prepaid model, supported by 15,234 prepayments
  after early repurchases are introduced as a competing exit.

## Milestone 3

- Delinquency state is an end-of-month condition derived from configured DPD
  thresholds. Configured zero-balance-code mappings take precedence over DPD:
  `01` and `16` are Prepaid; `02`, `03`, `09`, and `15` are ChargeOff; `06` is
  Repurchased. Repurchased is a non-loss absorbing exit. Only when no terminal
  code is present does 180+ DPD independently trigger ChargeOff. An unmapped
  nonblank code is a named fatal panel-quality violation.
- Censoring is derived only from vendor history: the final observed account row
  is Censored when it has neither a zero-balance code nor terminal disposition.
  Every account must have exactly one terminal row, and terminal rows never have
  an observed next state.
- Vendor `current_upb` is beginning-of-month. Panel `upb_eom` is next month's
  beginning balance for continuing loans, zero for observed absorbing exits, and
  null at censoring because the next balance is unobserved.
- The transition into month `t` uses month `t` MOB and contemporaneous macro
  values. Configured macro lags look backward from `t`; no feature is shifted
  forward to use future information.
- Account histories must be monthly-contiguous and business keys must be unique.
  Calendar-month difference from origination is authoritative MOB. Vendor loan
  age is retained for comparison; disagreements are rate-limited under the same
  1%-per-reason and 2%-overall policy used during ingestion.

## Milestone 4

- Vintage performance is evaluated as of `2018-12-01`. This reporting cutoff
  creates the observed development triangle: MOBs after each cohort's available
  age are undefined and explicitly masked, never filled with zero loss.
- A vintage is fully seasoned at configured MOB 119. Chain-ladder age-to-age
  factors are volume-weighted and fitted only from vintages observed through
  that maturity. The completed-vintage average curve, scaled to the latest
  observed point, is retained as a sensitivity rather than the primary method.
- The configurable cohort grain defaults to quarterly; annual cohorts are run
  as a stability cross-check. A quarterly or annual MOB cell is observed only
  when every constituent monthly cohort is observable at that MOB, preventing
  partially populated boundary cells from biasing development factors.
- Net charge-off dollars equal charge-off BOM UPB less net sales proceeds after
  foreclosure costs, floored at zero at each vintage/MOB. This prevents an
  anomalous recovery from creating negative credit loss; gross loss and recovery
  remain separate columns so the floor is auditable.
- Repurchased BOM UPB is reported separately as `repurchase_dollars`. It is
  excluded from gross charge-off, recovery, and net charge-off, while the code
  `06` terminal row ends the account history so its balance contributes to no
  later active-balance or average-outstanding denominator.
- Cumulative net loss divided by original vintage balance is the primary
  forecast curve because its denominator is fixed and comparable across cohorts.
  Cumulative loss divided by average balance outstanding through the observed
  age is reported as the portfolio-experience sensitivity.
- The 2006–2008 mean chain-ladder ultimate must exceed the 2011+ mean. The M4
  stage fails rather than publishing a curve when this upstream stress signal is
  absent.
- Chain-ladder develops cumulative loss dollars with volume-weighted age-to-age
  factors, then applies that loss-development pattern to either reported rate.
  The scaled-average sensitivity uses the equal-cohort mean completed curve.
  Fitting chain-ladder directly to completed-cohort average-outstanding rates is
  prohibited because it collapses algebraically to the scaled-average method.
- The observed loss decomposition uses the exact portfolio identity: account
  default rate × average default EAD relative to average original balance ×
  gross-charge-off-weighted realized LGD = observed net loss/original balance.
  Ultimate development is disclosed separately rather than attributed to an
  unmodeled future default-frequency or LGD component.
- Chain-ladder development assumes future loss severity resembles the severity
  embedded in observed development. The full-history backtest shows this is
  violated here: realized LGD is 23.65% for 2008–2010 originations but 26.67%
  for 2011+ originations, versus 24.58% in the cutoff-date observed mix. It also
  cannot anticipate the post-cutoff 2020 calendar shock. Accordingly, M4 is a
  baseline benchmark rather than a scenario-sensitive forecast, and its 2011+
  quarterly cohort projections materially understate realized ultimate loss.

## Milestone 5

- Transition denominators contain only rows with an observed next state.
  Censored terminal rows are explicitly filtered before aggregation; all 8,004
  are excluded in the full run. Counts retain exact MOB, score band, and calendar
  month so conditional fitting does not discard seasoning or macro timing.
- ChargeOff, Prepaid, and Repurchased rows in every generated matrix are exact
  identities. A transient origin with no observations uses a conservative
  self-transition prior rather than returning NaN or losing probability mass.
- Current is modeled conditionally over Current, DPD30, and Prepaid. Delinquent
  origins are modeled over cure, stay, and roll-forward; their sparse prepayment
  events are pooled into a constant 0.21096% monthly exit probability. The new
  code-06 events are similarly represented by a pooled 0.00891% competing
  repurchase probability for every transient state.
- Multinomial logits are fitted to the aggregated cells with transition counts
  as frequency weights. MOB enters through a natural cubic spline with boundary
  knots at 0 and 119 and internal knots at configured MOB-band boundaries 12,
  24, 36, and 60; score band enters through its midpoint-implied score risk.
  Unemployment enters through the threshold hinge `max(u - 4.8%, 0)` for both
  contemporaneous and lag candidates. A convex or threshold macro response is
  defensible on real credit data because repayment capacity can deteriorate
  nonlinearly once labor-market slack passes a borrower-stress threshold; this
  is not treated as an artifact unique to the synthetic DGP. BIC compares contemporaneous,
  1-, 3-, and 6-month macro timing on a common complete sample. Contemporaneous
  month-t variables win for every origin, matching the generator's transition-
  into-t convention; no future or forward-shifted covariate is used.
- BIC selecting contemporaneous-only macro variables reflects the synthetic
  DGP's explicit transition-into-month-t timing convention. On real servicing
  data, 1–3 month lags would be expected because job loss generally precedes a
  missed payment. The lag-selection machinery is retained for that real-data
  behavior rather than treating contemporaneous timing as universal.
- DPD150 keeps its own cure/stay/ChargeOff outcome structure and uses configured
  light L2 regularization (`C = 0.002`). Pooling DPD120 and DPD150 would erase
  the explicit DPD150-to-ChargeOff boundary. The penalty stabilizes the sparse
  late-stage fit: the unemployment effects move from -433/+349 basis points for
  cure/roll to -262.6/+256.1, versus the DGP's -240/+260 basis points.
- HPI is retained in lag selection because it was requested as a candidate, but
  transition ground truth contains no HPI effect; recovery/LGD, not transition
  frequency, is its intended primary channel. Interpretation therefore reports
  probability changes and treats only effects of at least one basis point as
  material.

## EMR scale validation (2026-08-08)

- The completed EMR 7.13.0 run used one primary and three `m5.xlarge` core
  nodes in `us-east-1`, processed 250,000 loans and 20,258,094 account-months,
  ran for about 65 minutes, and cost approximately $1.05. Exit counts and the
  transition denominator reconcile exactly, and Current-to-DPD30 was 0.7683%
  versus 0.7678% locally at 25,000 loans. The close agreement across a 10x scale
  change is the primary distributed-correctness validation.
- Executor sizing reserved one 3-core, 9-GiB executor per 4-vCPU, 16-GiB core
  node, leaving one vCPU and roughly 7 GiB per node for YARN, the operating
  system, and overhead. That plan yields three executors and nine executor
  cores. The normal 2–3x-executor-core rule suggests 18–27 shuffle partitions;
  32 rounded slightly above that range to 3.6x planned executor cores and 2.7x
  the 12 physical worker cores. EMR's default dynamic
  allocation won during the completed run and reported 11 executors; future
  submissions disable it so the configured topology is authoritative.
- At 32 shuffle partitions, ingestion ran 424 tasks at about 47,800 rows per
  task in 89.5 seconds. At 200 partitions it ran 2,446 tasks at about 8,300 rows
  per task in 96.0 seconds. The 5.8x task increase and 7.3% slowdown show fixed
  scheduling, serialization, and output-commit overhead. Because CSV parsing
  and S3 reads dominate ingestion, this modest timing result is not assumed to
  transfer unchanged to the shuffle-heavy panel stage.
- Panel shuffle scaled from 213 MB at 2.02 million rows locally to 2,380 MB at
  20.26 million rows on EMR: 11.2x shuffle for 10.0x data. The mild
  superlinearity is attributed to more distinct `loan_id` keys touching more
  exchange partitions. Partitioning by loan remains skew-resistant because an
  individual loan's cardinality is bounded by the observation window.
- Four portability defects appeared only on the cluster. First, backslashes in
  f-string expressions use PEP 701 syntax unavailable in Python 3.11 even
  though the project declared 3.11 support; the local development interpreter
  was Python 3.14. Expressions are now hoisted and Ruff parses against `py311`.
  Second, Hive-style `vintage_year=YYYY` raw
  fixture directories caused Spark partition discovery to inject a colliding
  column; EMR fixtures are now flat vendor-shaped files, and ingestion drops a
  discovered performance-side `vintage_year` before joining the authoritative
  acquisition vintage. Third, bootstrap installed PyYAML into a different
  interpreter than Spark used; bootstrap and both PySpark roles are pinned to
  `/usr/bin/python3`. Fourth, YARN client-mode `--files` localizes files for
  executors, not the submitting driver; the driver now reads configuration from
  the repository checkout. These are environment and deployment findings, not
  changes to credit-state or loss-model assumptions.
- Macro features form one global monthly series of approximately 350 rows.
  Calendar gaps and lags therefore require a global order, so
  `prepare_macro_features` intentionally coalesces the macro frame to one
  partition before its unpartitioned windows. The same design on the
  20.3-million-row account-month panel would create an unacceptable single-node
  bottleneck; panel windows must remain partitioned by `loan_id`.
