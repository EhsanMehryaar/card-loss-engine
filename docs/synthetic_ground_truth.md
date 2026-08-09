# Synthetic data-generating ground truth

This document records the known coefficients used by the synthetic portfolio.
It is a benchmark for coefficient recovery and validation in Milestones 5–8.

## Transition hazards

Define:

- `score_risk = clip((700 - orig_score) / 100, -0.8, 1.8)`
- `macro_risk = max(unemployment - 4.8, 0) / 5 + max(unemployment_change_3m, 0) / 2`
- `seasoning = exp(-((MOB - 28) / 22)^2)`

For Current accounts, the probability of rolling to DPD30 is:

`clip(0.003 + 0.010*max(score_risk, 0) + 0.010*macro_risk + 0.004*seasoning, 0.001, 0.085)`.

For delinquent state index `i` (DPD30=1 through DPD150=5):

- Forward roll: `clip(0.19 + 0.07*score_risk + 0.13*macro_risk + 0.02*i, 0.07, 0.72)`.
- Cure: `clip(0.50 - 0.055*i - 0.08*score_risk - 0.12*macro_risk, 0.08, 0.70)`.
- Stay is the residual, with a 3% floor; forward-roll and cure probabilities are
  proportionally rescaled when that floor binds.

DPD150 rolls forward to ChargeOff. Earlier delinquency states roll to the next
30-day bucket.

## Prepayment

Current-state prepayment probability is
`clip(0.0025 + 0.00010*MOB + 0.0015*max(-score_risk, 0), 0.001, 0.025)`.
For delinquent loans it is the smaller of one quarter of that probability and
0.004. Prepaid is terminal and competes with default.

## Recovery

At charge-off:

`recovery_rate = clip(0.78 - 0.005*(orig_ltv - 75) + 1.10*hpi_change_yoy + 0.0006*(orig_score - 700) + Normal(0, 0.07), 0.10, 0.98)`.

`net_sales_proceeds = upb_bom * recovery_rate` and foreclosure costs are
`upb_bom * clip(Normal(0.045, 0.012), 0.015, 0.10)`. Consequently realized LGD
is `1 - (net_sales_proceeds - foreclosure_costs) / upb_bom`.

## Randomness and timing

Project seed 1729 is split into independent feature streams with NumPy
`SeedSequence([seed, stream_id])`. Each stream is prefix-stable, so increasing
loan count leaves all existing loan histories unchanged. The macro path has a
separate stream. A transition into month `t` uses month `t` MOB and macro values.
Original scores are drawn from a clipped normal distribution whose baseline
configuration has mean 720 and standard deviation 55; tests may lower the mean
to create a compact, stable default-rich fixture.
