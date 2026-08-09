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
