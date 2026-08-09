# Model documentation

Final consolidated documentation remains a Milestone 9 deliverable. The core
transition model introduced in Milestone 5 is documented here as it is built.

## Milestone 5 transition model

Spark reduces the account-month panel to frequency-weighted transition cells at
exact MOB, score band, and calendar month. Censored rows never enter a
denominator. Single-node multinomial logits model Current over Current/DPD30/
Prepaid and each delinquent state over cure/stay/roll-forward. Sparse delinquent
prepayment and repurchase are pooled competing exits.

MOB is represented by a natural cubic spline using configured internal knots at
12, 24, 36, and 60 months and natural boundaries at 0 and 119. This replaces the
initial quadratic specification and improves recovery of the Current seasoning
effect from 14.27 to 32.97 basis points, versus 40.00 basis points in the DGP.

The model selects macro timing by BIC from month t and configured backward lags.
All six origin models select contemporaneous month-t unemployment, three-month
change, and HPI in this synthetic dataset. This reflects the DGP; real data may
select 1–3 month lags because labor-market events precede missed payments. The
unemployment level is represented by `max(u - 4.8%, 0)`, including for lagged
candidates. Threshold/convex macro response is also defensible in real credit
data: repayment stress need not rise linearly while labor markets remain tight.
DPD150 retains its distinct ChargeOff boundary and uses light, configured L2
regularization (`C = 0.002`) instead of being pooled with DPD120. This changes
its cure/roll unemployment effects from -433/+349 to -262.6/+256.1 basis points,
close to the -240/+260 DGP values. The
public `build_matrix(mob, score_band, macro)` API emits the
full nine-state matrix, including exact absorbing identities, and validates all
row sums.

Ground-truth validation is performed in probability space. Recovered
unemployment, score-risk, and seasoning effects have the documented signs. Full
coefficient, marginal-effect, empirical-matrix, and ground-truth tables are
written to `docs/` by the M5 CLI stage.

## Milestone 6 lifetime ECL

The forward engine starts with a probability vector for each score-band and
annual-vintage cell and applies `v[t + 1] = v[t] @ P[t]` along the supplied
monthly macro path. Each `P[t]` comes directly from M5's public
`build_matrix(mob, score_band, macro)` interface. ChargeOff, Prepaid, and
Repurchased mass is retained in three separate absorbing states. The engine
checks total probability mass after every multiplication and checks that
cumulative ChargeOff probability cannot decrease. Portfolio paths are opening-
balance-weighted combinations of the segment paths.

LGD is fitted as one exposure-weighted recovery regression per score band, with
default-month year-over-year HPI change as the explanatory macro variable.
Predicted recovery is bounded to `[0, 1]` and converted to LGD. A score band uses
the configured fallback only when it has fewer than 20 usable defaults or no HPI
variation; every fallback is logged. This preserves the economic distinction
between origination era and collateral conditions when default actually occurs.

For each future month, marginal PD is the increase in absorbed ChargeOff mass.
EAD is the beginning-of-month contractual balance from the amortization schedule;
a null `upb_eom` on a censored reporting row explicitly falls back to `upb_bom`
when the opening portfolio snapshot is built. LGD is evaluated at that month's
projected HPI, and loss is discounted at the configured annual effective rate:

`ECL = sum_t(marginal_PD[t] * EAD[t] * LGD[t] / (1 + r) ** (t / 12))`.

Prepayment and repurchase require no post-processing adjustment: because they
are absorbing competing risks in the transition matrix, their probability mass
cannot later enter ChargeOff. For revolving cards, contractual EAD must be
extended beyond this amortizing-loan implementation to include expected draws on
the undrawn commitment, normally `drawn + CCF * undrawn`.
