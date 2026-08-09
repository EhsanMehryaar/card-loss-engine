# Milestone 6 results — corrected cutoff-clean backtest

Every result on this page covers the **local synthetic 25,000-loan portfolio**,
not the 250,000-loan EMR run. The corrected 2018-12 population contains 8,927
loans observed active at cutoff and $3.6158B outstanding; 3,902 histories
censored before cutoff remain excluded.

| Measure | Fit provenance | Undiscounted loss | Error vs $71.83M realized | Error % |
|---|---|---:|---:|---:|
| Realized post-cutoff loss | Ground truth | $71.83M | — | — |
| M6 roll-rate | Cutoff-clean through 2018-12 | $78.67M | +$6.84M | +9.51% |
| M6 roll-rate | Full history; leaked for this test | $72.42M | +$0.59M | +0.82% |
| M4 remaining loss | Pre-cutoff chain ladder | $14.41M | -$57.43M | -79.94% |

The cutoff-clean model reduces absolute error by **88.1%** versus chain ladder.
The leaked fit appears $6.25M more accurate, an **8.70 percentage-point leakage
gap**. This is the measured benefit of fitting on the outcome period, not model
skill. The four-way arithmetic is in [m6_fit_comparison.csv](m6_fit_comparison.csv).

Cutoff-clean discounted lifetime ECL is **$68.92M**, or **1.906%** of corrected
outstanding balance. Production full-history discounted ECL is $63.37M on the
same population and is reserved for forward allowance/scenario use. Every
regenerated M6 CSV and the loss plot record fit provenance and portfolio scope.

## Superseded development output — retained only as an audit trail

The sections below describe the original M6 run that incorrectly included
already-censored histories and scored a full-history transition fit against its
own estimation period. Its $94.55M / $82.81M figures and related attribution are
not valid published model-performance results and are superseded by the table
above.

## PD / EAD / LGD decomposition by score band

`Expected default exposure` is `sum(marginal PD * projected EAD)`; PD is the
opening-balance-weighted cumulative probability of entering ChargeOff. The CSV
artifacts also report EAD at default as a percentage of opening exposure and
retain unrounded values.

| Score band | Balance ($M) | Lifetime PD | Expected default exposure ($M) | LGD | Lifetime ECL ($M) | ECL / balance |
|---|---:|---:|---:|---:|---:|---:|
| FICO 000-619 | 157.4 | 36.80% | 53.7 | 31.88% | 15.0 | 9.52% |
| FICO 620-659 | 489.1 | 15.20% | 69.2 | 29.48% | 17.9 | 3.66% |
| FICO 660-699 | 1,124.8 | 9.51% | 99.1 | 26.61% | 23.1 | 2.05% |
| FICO 700-739 | 1,431.2 | 5.86% | 77.8 | 23.73% | 16.1 | 1.13% |
| FICO 740-779 | 1,077.4 | 3.52% | 35.3 | 23.40% | 7.2 | 0.67% |
| FICO 780-850 | 670.2 | 2.74% | 17.2 | 22.83% | 3.5 | 0.52% |

Complete decompositions are available [by score band](ecl_by_score_band.csv)
and [by annual vintage](ecl_by_vintage.csv). More recent vintages generally
carry higher ECL rates because more contractual exposure and forecast life
remain at the cutoff; this is a remaining-life effect, not an era-level constant
LGD assumption.

## Monthly loss path

![Monthly and cumulative discounted expected loss](ecl_monthly_loss_path.png)

The figure and its [monthly loss path](ecl_monthly_loss_path.csv) cover the local
25,000-loan portfolio. The CSV includes monthly and cumulative discounted loss
and expected default exposure.

## LGD macro validation

The model uses HPI at default, not origination vintage. The era table is a
validation grouping that demonstrates the macro-conditioned predictions recover
the observed severity differences.

| Origination era | Defaults | Average HPI YoY | Realized LGD | Modeled LGD |
|---|---:|---:|---:|---:|
| Pre-2008 | 483 | 3.72% | 25.14% | 25.22% |
| 2008-2010 | 159 | 4.38% | 23.65% | 24.47% |
| 2011+ | 793 | 1.94% | 26.67% | 26.45% |

All six score bands had sufficient observed defaults, so none used the 45%
fallback. See the [era validation](lgd_validation_by_era.csv) and
[LGD coefficients](lgd_coefficients.csv) for exact values.

## Reconciliation to the M4 chain ladder

The bridge below is additive; each subtotal equals the prior subtotal plus the
stated adjustment. All amounts refer to the local 25,000-loan portfolio.

| Bridge step | Adjustment ($M) | Subtotal ($M) |
|---|---:|---:|
| M4 chain-ladder ultimate | +93.66 | 93.66 |
| Less: realized losses incurred through cutoff | -79.25 | 14.41 |
| Restriction to surviving exposure | 0.00 | 14.41 |
| Macro-conditioned roll-rate model versus M4 remaining loss | +80.14 | 94.55 |
| Discounting at 5% annual effective rate | -11.74 | 82.81 |

The surviving-exposure row is zero because removing incurred loss leaves the M4
future-loss component, while loans already absorbed through ChargeOff, Prepaid,
or Repurchased have zero future marginal PD in M6. A second exit adjustment
would double-count them. The +$80.14M line is the central model result, not a
plug: **chain ladder projects $14.41M of remaining loss while the
macro-conditioned roll-rate model projects $94.55M undiscounted, a 6.56x
disagreement**. The independent M4 backtest established why this direction is
credible: chain ladder underprojected post-2011 cohorts by 125.61 basis points
mean error and 63.51% MAPE because it could not anticipate a macro shock outside
the observation window. See the exact cohort results in the
[M4 backtest](vintage_projection_accuracy.csv). Discounting the M6 path removes
$11.74M and produces $82.81M.

The exact-dollar [bridge CSV](ecl_chain_ladder_reconciliation.csv) also records
the definition of every row. M4's 0.856% rate uses $10.95B original balance;
M6's 1.673% rate uses $4.95B cutoff outstanding balance.

## Validation against realized post-cutoff loss

The panel contains the complete forecast window through 2028-11, so this is a
known-answer test rather than an in-sample fit statistic. **M6 overprojects.**
Its undiscounted forecast is **$94.55M**, compared with **$71.83M** of realized
post-cutoff net charge-offs. The error is **+$22.72M, or +31.62% of realized
loss**.

| Default year | Projected loss ($M) | Realized loss ($M) | Error ($M) | Error / realized |
|---|---:|---:|---:|---:|
| 2019 | 6.46 | 4.66 | +1.80 | +38.63% |
| 2020 | 24.33 | 20.63 | +3.69 | +17.91% |
| 2021 | 36.84 | 29.20 | +7.64 | +26.17% |
| 2022 | 13.29 | 11.26 | +2.03 | +17.99% |
| 2023 | 5.59 | 3.36 | +2.23 | +66.50% |
| 2024 | 2.93 | 0.79 | +2.14 | +272.72% |
| 2025 | 1.83 | 0.95 | +0.88 | +93.38% |
| 2026 | 1.32 | 0.88 | +0.44 | +49.66% |
| 2027 | 1.11 | 0.05 | +1.06 | +2,075.24% |
| 2028 through November | 0.85 | 0.06 | +0.79 | +1,350.48% |
| **Total** | **94.55** | **71.83** | **+22.72** | **+31.62%** |

The shock is visible in realized loss: it rises from $4.66M in 2019 to $20.63M
in 2020 and peaks at $29.20M in 2021 as accounts migrate through delinquency
states. M6 captures that timing but overstates its magnitude. Percentage errors
after 2026 are very large because realized dollar losses are small.

The multiplicative decomposition uses cutoff balance × lifetime PD × EAD factor
× LGD. Projected PD is 7.666% versus 5.497% realized; projected EAD at default is
92.848% of cutoff exposure versus 96.546% realized; and projected LGD is 26.834%
versus 27.345% realized. An order-neutral Shapley attribution reconciles the
$22.72M error exactly:

| Error component | Contribution ($M) |
|---|---:|
| PD | +27.55 |
| EAD | -3.26 |
| LGD | -1.57 |
| **Total** | **+22.72** |

The arithmetic labels the overprojection as PD, partially offset by slightly
lower projected EAD and LGD. Subsequent diagnosis shows it is primarily a
forecast-population error rather than broad transition-rate overshoot: 3,902
already-censored histories carrying $1.334B were included as if active at the
cutoff. Holding model parameters fixed and excluding them reduces the forecast
to $72.42M, only $0.59M (0.82%) above realized loss. The current fit also uses
post-cutoff transition observations, so the reported +31.62% cannot honestly be
attributed solely to macro extrapolation. See the detailed limitation in the
[model documentation](model_documentation.md#pd-overprojection-diagnosis), the
[population sensitivity](m6_pd_overprojection_sensitivity.csv), and the exact
annual [ground-truth validation](ecl_ground_truth_validation.csv).

## Verification

The hand-computed three-loan/four-month test agrees exactly at $55.6725. Tests
also cover mass conservation over a macro grid, monotone cumulative default,
balance-weighted aggregation, competing-risk prepayment, macro-conditioned LGD,
sparse-data fallback logging, censored exposure, and exact zero ECL when PD is
zero. The final command results are recorded in the implementation handoff.
