# Model documentation

## Purpose and scope

The engine estimates consumer-credit vintage loss development, monthly
delinquency migration, lifetime expected credit loss (ECL), and scenario loss
paths. It supports allowance analysis, portfolio loss forecasting, scenario
sensitivity, model validation, and data-pipeline reconciliation. It does not
set underwriting decisions, collections treatments, capital requirements, or
account-level pricing, and it is not approved for production use on real loans.

The allowance implementation is mortgage-shaped because its synthetic data has
amortizing balances and collateral recoveries. A card deployment requires a
revolving EAD model—including a credit conversion factor for undrawn lines—and
validation on card behavior. The current outputs are evidence of engineering
and modeling technique, not a bank allowance estimate.

## Data

The source is a seeded synthetic loan-level acquisition and monthly performance
history with documented generating parameters, observed macro variables, exit
dispositions, and recoveries. Lineage is raw pipe-delimited vendor-shaped files
→ validated curated Parquet → account-month panel → aggregated vintage and
transition tables → single-node model artifacts. Fatal keys, duplicates,
orphans, invalid dates, and exclusion rates are controlled during ingestion.

The headline modeling portfolio has 25,000 loans, $10.9476B original balance,
and 2.02M account-months. At the 2018-12 cutoff, 8,927 loans are demonstrably
active with $3.6158B outstanding. The 3,902 histories censored before cutoff are
excluded from forecast populations. The separate EMR run has 250,000 loans and
20.258M account-months; it validates distributed scale and invariance but does
not produce the published allowance figures. Synthetic outcomes make ground
truth knowable, but they do not establish behavioral realism on real consumers.

## Methodology

1. Vintage curves aggregate net charge-off by quarterly origination cohort.
   Chain-ladder age-to-age factors project ultimate loss as a transparent,
   macro-insensitive baseline.
2. A Markov chain represents Current, DPD30, DPD60, DPD90, DPD120, DPD150,
   ChargeOff, Prepaid, and Repurchased. The last three are distinct absorbing
   competing risks; only ChargeOff produces loss.
3. Frequency-weighted multinomial logits estimate transient-state transitions
   by score band, natural-cubic MOB spline, unemployment above a 4.8% hinge,
   three-month unemployment change, and HPI change. BIC selects macro timing.
4. Monthly state vectors follow `v[t+1] = v[t] @ P[t]`. Probability mass must
   remain one and cumulative default cannot decline.
5. Lifetime ECL is the discounted sum of marginal default probability ×
   amortizing EAD × score-segmented, default-month HPI-conditioned LGD.
6. The scenario overlay maps the published Federal Reserve 2019 baseline,
   adverse, and severely-adverse paths into model features, then mean-reverts
   beyond the 13-quarter publication window.

Two fits are governed separately. `production_full_history` uses all available
history and is correct for current allowance and M7 scenario estimation.
`cutoff_clean_out_of_sample` ends at the configurable 2018-12 endpoint and is
used only to score known post-cutoff outcomes. Every M6/M7 artifact records fit
provenance and endpoint.

## Assumptions

1. Credit state is determined by reported delinquency and zero-balance exit
   codes using the fixed ordering in configuration.
2. Censored observations contribute exposure history but not an unobserved next
   transition; histories censored before a reporting cutoff are not active.
3. Score and MOB bands are stable segmentation variables; MOB is capped at 119
   when forecasting beyond fitted seasoning support.
4. A 4.8% unemployment hinge and the configured macro lag candidates adequately
   represent nonlinear and delayed macro transmission inside observed support.
5. Multinomial transition probabilities are conditionally Markov given state,
   segment, MOB, and macro features.
6. Prepayment and repurchase are absorbing competing risks and cannot default
   after exit.
7. LGD is a bounded score-band recovery regression on HPI conditions at default;
   the configured 45% fallback applies only when observations are insufficient.
8. Amortizing EAD follows contractual principal runoff; censored null EOM UPB
   falls back explicitly to observed BOM UPB.
9. Future loss is discounted at a 5% annual effective rate.
10. After the published scenario window, unemployment and HPI growth revert to
    4.8% and 3.0% with an eight-quarter half-life.
11. The synthetic generator's recorded seed and parameters define reproducible
    truth; they are not empirical estimates of a real card portfolio.

Detailed decisions and changes remain in [assumptions_log.md](assumptions_log.md).

## Validation results

- M3 reconciles each loan to exactly one terminal outcome and reconciles the
  transition denominator to account-months less terminal rows. The 250,000-loan
  EMR identity is `20,008,094 = 20,258,094 - 250,000`.
- M4 chain ladder achieves 4.71% MAPE on 2008–2010 cohorts and 63.51% on 2011+
  cohorts. At 2018-12 it projects $14.41M remaining loss versus $71.83M realized.
- The cutoff-clean M6 fit through 2018-12 projects $78.67M, a +$6.84M / +9.51%
  error. The full-history fit projects $72.42M, a +$0.59M / +0.82% leaked
  backtest error. Leakage therefore removes $6.25M of apparent absolute error,
  or 8.70 percentage points. The cutoff-clean model reduces absolute error
  88.1% versus chain ladder.
- M5 recovers all eleven documented macro slopes with correct signs against the
  known synthetic generating process. Specification was revised three times
  while consulting that truth; this is development evidence, not independent
  validation.
- M7 production-fit ECL is $20.75M baseline, $50.57M adverse, and $161.34M
  severely adverse on the active local portfolio. Current→DPD30 is the largest
  increase in expected state flow under severe stress.

## Limitations and weaknesses

- Macro extrapolation is structural and material. The cutoff-clean unemployment
  maximum is 7.25%; 84.6% of severely-adverse published months exceed it and
  15.4% exceed the 9.86% full-history maximum. The stress regime is predominantly
  outside support, although its first six months are not. Point-estimate
  uncertainty is not quantified.
- The transition specification was tuned three times against known synthetic
  truth. Real independent validation would prohibit using the answer key this
  way and would require a locked development sample and separate challenger.
- All credit behavior is synthetic. Plausibility is designed and asserted, not
  established from observed consumers, servicing practices, or economic cycles.
- Collateral recovery and amortizing EAD are mortgage concepts. Revolving card
  EAD can grow before default through unused-line draw and requires a validated
  CCF model; collateral HPI is not an appropriate card LGD driver.
- A crisis backtest trained pre-2007 and forecast over 2008–2010 was scoped for
  Milestone 8 but deliberately not implemented. PSI and ongoing stability
  monitoring were also scoped but not executed on a production time series.
  Both are future work, along with real-data benchmarking and uncertainty bands.
- Sparse late-stage transitions, model-form risk, recovery truncation, scenario
  tail reversion, and dependence on data coding remain sources of error.

## Governance

In production, Credit Risk owns model use and allowance decisions; Model
Development owns methodology, code, configuration, and change records; Data
Engineering owns certified source lineage; Model Operations owns controlled
runs and monthly monitoring; and independent Model Risk Management approves the
model, challenges limitations, and performs annual revalidation. Internal Audit
reviews adherence to policy and evidence retention. Material data, code,
segmentation, fit-window, threshold, or scenario changes require documented
impact analysis, independent approval, versioning, and parallel-run evidence.
Red monitoring breaches follow the [monitoring plan](monitoring_plan.md) and may
trigger overlays, use restrictions, recalibration, or withdrawal.

## Technical development appendix

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

> Historical development record: the original M6 figures below used an
> already-censored population and a full-history fit for outcome scoring. They
> are retained only to document discovery of those defects. The governed
> validation result is the cutoff-clean comparison in **Validation results**.

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

Post-cutoff validation uses the synthetic panel's known outcomes through
2028-11. For each realized default, its cutoff exposure supplies the PD weight,
its default-month BOM balance supplies EAD, and net charge-off divided by that
balance supplies LGD. The same three multiplicative factors are derived from the
forecast. Error is allocated across PD, EAD, and LGD with a three-factor Shapley
decomposition so the attribution is order-neutral and reconciles exactly.

### PD overprojection diagnosis

The +$27.55M Shapley label initially appears to be a transition-PD calibration
failure, but state-level and population diagnostics show that interpretation is
incomplete. The reported cutoff population contains 3,902 histories already
marked Censored, with $1.334B of exposure—27.0% of the reported $4.950B cutoff
balance. They are carried forward because the segment builder excludes the three
economic absorbing exits but not `Censored`; they cannot produce observed future
defaults. Holding the fitted transition and LGD models fixed and restricting the
forecast to the 8,927 loans actually observed active at 2018-12 reduces
undiscounted loss from $94.55M to $72.42M. That is only $0.59M, or 0.82%, above
the $71.83M realized result. The [population sensitivity](m6_pd_overprojection_sensitivity.csv)
therefore identifies cutoff-population construction as the dominant cause of
the reported +31.62% overprojection.

There is also a temporal-validity defect: the current M6 fitting path supplies
all M5 transition cells to `fit_conditional_models` without filtering at the
2018-12 forecast cutoff. The reported model consequently sees the 2020 shock and
later outcomes during estimation. Its 2019–2021 fitted transition probabilities
underpredict every forward-roll/default transition when aggregated across the
three years; it does not exhibit the hypothesized across-the-board shock-period
overshoot.

An as-of-correct, cutoff-clean refit does face genuine macro extrapolation. The
pre-cutoff monthly unemployment distribution has median 4.95%, 95th percentile
6.77%, and maximum 7.25%. During 2019–2021 the corresponding values are 7.00%,
9.52%, and 9.86%; 16 of 36 months (44.4%), representing 41.0% of transition
weight, exceed the pre-cutoff maximum. These are the actual local synthetic
values; the initially hypothesized 8.4% training peak and 10–14.5% realized
range are not present in this artifact. The current leaked full-history fit has
9.86% as its maximum, with 5.08% of transition weight above the valid pre-cutoff
maximum. Exact distributions are in the
[unemployment-range diagnostic](m6_unemployment_range_diagnostic.csv).

The table reports fitted minus realized basis points for the adverse
roll-forward transition. It shows where a cutoff-clean model overshoots and
contrasts that result with the model actually used in reported M6.

| Origin | Current full-history fit, 2019–2021 | Cutoff-clean fit, 2019–2021 | Cutoff-clean fit, 2020 |
|---|---:|---:|---:|
| Current | -5.4 | +18.8 | +53.1 |
| DPD30 | -140.0 | -213.8 | -346.8 |
| DPD60 | -129.0 | -310.5 | -254.4 |
| DPD90 | -28.5 | +376.2 | +761.1 |
| DPD120 | -50.4 | -95.6 | -75.3 |
| DPD150 | -52.6 | -209.0 | -748.3 |

The cutoff-clean overshoot is concentrated in Current→DPD30 and especially
DPD90→DPD120; the other delinquent forward transitions underpredict realized
movement. The complete destination and annual comparison is in the
[2019–2021 transition backtest](m6_transition_backtest_2019_2021.csv).

Macro extrapolation remains a material model limitation even though it is not
the dominant cause of the reported M6 miss. The fitted hinge/logit form imposes
a response outside the valid 7.25% estimation maximum without observations to
validate its slope or curvature. The Federal Reserve's
[2026 severely adverse scenario](https://www.federalreserve.gov/publications/2026-stress-test-scenarios.htm)
peaks at 10% unemployment, above even the leaked full-history maximum and 2.75
percentage points above the cutoff-valid maximum. Scenario results in that
region require explicit overlays, bounds, or challenger evidence before use.
This is precisely the type of limitation that model validation must surface.

## Milestone 7 supervisory scenarios

M7 uses the Federal Reserve 2019 supervisory scenario vintage because its
2019Q1 start aligns with the 2018-12 allowance cutoff. Quarterly unemployment
averages are repeated over each quarter's months; quarterly HPI index levels are
converted to year-over-year change; and monthly unemployment change is the
three-month difference in level. Observed history supplies the initial lag
bridge. The path interface is a validated CSV schema, so later published
vintages require a data/configuration replacement rather than model-code edits.

After the 13-quarter published window, unemployment and HPI growth revert
exponentially to 4.8% and 3.0% with an eight-quarter half-life. This tail is a
model assumption and is flagged separately from published observations. LGD is
evaluated against each scenario's monthly HPI path; no portfolio-average LGD is
used.

Forward-looking scenario allowance uses the production full-history transition
fit. The cutoff-clean fit remains the out-of-sample backtest model. This choice
does not remove extrapolation risk: 84.6% of severely-adverse published months
exceed the cutoff-valid 7.25% unemployment maximum and 15.4% exceed the
full-history 9.86% maximum. The first six severe months remain inside 7.25%, so
the common shorthand that the entire path is outside range is factually too
strong; the severe stress regime is nevertheless predominantly out of sample.
The $161.34M severely-adverse ECL is a point estimate whose uncertainty from
out-of-support macro response is not quantified. CCAR-style use therefore needs
independent bounds, overlays, or challenger support.
