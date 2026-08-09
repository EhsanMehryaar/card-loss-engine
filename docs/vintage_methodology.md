# Vintage curve methodology

Milestone 4 aggregates the account-month panel in Spark to one row per
`(vintage, months_on_book)`. The cohort grain is configurable as monthly,
quarterly, or annual and defaults to quarterly; annual is always produced as a
cross-check. Curve fitting and plotting then operate on the small aggregate in
pandas. The configured analysis date bounds observability; cells after a
vintage's `max_observed_mob` remain undefined. For multi-month cohorts, that
maximum is the minimum observable age of their constituent monthly cohorts, so
every retained cell contains the complete cohort.

Repurchases are non-loss exits. Their terminal BOM exposure is reported as
`repurchase_dollars`, their EOM balance is zero, and no later exposure row is
present. They never enter gross or net charge-off and are not censoring events.

The primary curve is cumulative net charge-off divided by original vintage
balance. Original balance is fixed at origination, so vintages remain directly
comparable. The average-outstanding curve divides the same cumulative loss by
the mean BOM active balance through each age and is reported as a portfolio
experience sensitivity.

Chain-ladder is the primary extrapolation. For every age, its development factor
is the ratio of cumulative loss dollars at age + 1 to cumulative loss dollars at
age, summed across only fully seasoned, observed vintages. An incomplete
vintage's last observed rate is developed through the remaining loss factors.
The sensitivity method instead scales the equal-cohort mean fully seasoned rate
curve to that last observed point. This distinction matters for the
average-outstanding denominator: developing the mean rate itself would make the
two methods algebraically identical. Fully observed vintages retain their actual
ultimate under both methods.

The portfolio audit decomposes observed loss as default frequency multiplied by
average EAD at default relative to average original account balance and realized
LGD. The product reconciles exactly to observed net loss/original balance. A
separate development multiplier bridges that observed loss rate to chain-ladder
ultimate.

The plot uses solid lines for observed cells and dashed lines for chain-ladder
projections. The 2006–2008 vintages are highlighted: they should be visibly
above the 2011+ vintages, and the pipeline treats failure of that comparison as
an upstream data or timing error.
