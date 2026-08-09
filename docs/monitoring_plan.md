# Model monitoring plan

This plan applies to a production implementation of the card loss engine. The
model owner calculates the dashboard monthly, the independent model-risk team
reviews threshold breaches, and Credit Risk approves overlays or use
restrictions. Thresholds are initial governance limits and must be recalibrated
after twelve months of production observations.

| Area | Metric and population | Green | Escalation trigger | Required action |
|---|---|---|---|---|
| Input drift | Population Stability Index (PSI) for score band, MOB band, unemployment, unemployment change, and HPI change versus the development sample | PSI < 0.10 | Amber at 0.10–0.25; red above 0.25 for any variable, or amber for three consecutive months | Data owner verifies lineage and coding. Model owner explains the shift, reruns sensitivity, and proposes recalibration or a temporary overlay. Red stops unreviewed model use. |
| Transition stability | Exposure-weighted observed roll rate by origin/destination versus fitted one-month expectation, in basis points and standardized residuals | Absolute difference < 25 bp and < 2 standard errors | Amber at 25–50 bp or 2–3 standard errors; red above 50 bp or 3 standard errors for two consecutive months in any material origin state | Reconcile state construction, isolate segment/macro drivers, run challenger calibration, and submit recalibration or overlay to Model Risk and Credit Risk. |
| Backtest drift | Rolling 12-month realized net charge-off less projected net charge-off, in dollars and percent of realized loss | Absolute percentage error < 15% | Amber at 15–25%; red above 25%, or same-sign error above 15% for three consecutive observations | Refresh PD/EAD/LGD attribution, test population and temporal leakage, assess allowance overlay, and begin re-estimation if the breach persists. |
| Extrapolation coverage | Share of outstanding balance for which unemployment or HPI is outside the relevant fit range; report production and cutoff-clean ranges separately | < 10% of balance | Amber at 10–25%; red above 25%, or any scenario month beyond the full-history maximum used for production fit | Flag the allowance as extrapolative, quantify bounded sensitivities, require an approved overlay/challenger, and prohibit treating the point estimate as validated in that region. |

## Operating process

1. Data Engineering certifies input completeness and schema reconciliation by
   the fifth business day.
2. Model Operations produces metrics by the seventh business day and attaches
   portfolio scope, model version, fit endpoint, reporting cutoff, and scenario
   vintage.
3. The model owner documents every amber or red breach, including root cause,
   financial impact, owner, due date, and whether an allowance overlay changed.
4. Model Risk reviews red breaches within five business days. Credit Risk owns
   the use decision; Model Risk independently approves remediation closure.
5. Quarterly monitoring packages retain inputs, outputs, code revision, config,
   approvals, and exceptions. Annual revalidation repeats conceptual soundness,
   process verification, outcomes analysis, and implementation testing.

## Minimum controls

- Monitoring populations must reconcile to the servicing ledger and allowance
  population before rates are calculated.
- Actuals and forecasts must use the same cutoff, exposure definition, recovery
  window, and fit provenance.
- Production full-history results must never be presented as out-of-sample
  backtests. Cutoff-clean results must never silently replace the production
  allowance fit.
- Missing or late metrics are themselves a red governance breach; they are not
  treated as zero drift.
