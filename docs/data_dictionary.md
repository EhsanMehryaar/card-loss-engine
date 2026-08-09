# Synthetic panel data dictionary

The transition into month `t` uses month `t` MOB and macro values. State and
terminal flags are end-of-month (EOM); balances explicitly identify their timing.

| Column | Type | Timing | Credit-risk meaning |
|---|---|---|---|
| `loan_id` | string | Static | Stable account identifier. |
| `as_of_month` | date | Month `t` | Calendar month represented by the row. |
| `vintage` | string YYYY-MM | Origination | Origination cohort used for vintage analysis. |
| `months_on_book` | integer | Month `t` | Completed monthly age used in the transition into `t`. |
| `vendor_loan_age` | integer/null | Month `t` | Vendor-reported age retained only for quality comparison with computed MOB. |
| `delinquency_state` | category | EOM | State reached after the month `t` transition. |
| `previous_delinquency_state` | category/null | Prior EOM | State from which the month `t` transition began. |
| `next_delinquency_state` | category/null | Next EOM | Observed state in `t+1`; null on every terminal row. |
| `upb_bom` | decimal | BOM | Contractual balance exposed during month `t`. |
| `upb_eom` | decimal/null | EOM | Next month's BOM for continuing loans, zero at observed exit, null at censoring. |
| `orig_score` | integer | Origination | Borrower credit quality; higher is lower risk. |
| `orig_ltv` | decimal | Origination | Original leverage and collateral recovery risk. |
| `unemployment_rate` | decimal percent | Month `t` | Systematic stress used for transition into `t`. |
| `unemployment_change_3m` | decimal pp | Month `t` | Three-month labor-market deterioration. |
| `hpi_change_yoy` | decimal fraction | Month `t` | Collateral-price change used for recovery at default. |
| `net_sales_proceeds` | decimal | EOM default | Gross collateral proceeds; zero otherwise. |
| `foreclosure_costs` | decimal | EOM default | Workout and liquidation costs; zero otherwise. |
| `exit_reason` | category/null | EOM | ChargeOff, Prepaid, Repurchased, or Censored on the sole terminal row. |
| `is_censored` | boolean | EOM | True when observation ends without a known next state. |
| `mob_band` | category | Month `t` | Configured months-on-book bucket. |
| `score_band` | category | Origination | Configured original-score bucket. |
| `property_state` | string | Origination | Geography retained for regional HPI segmentation. |
| `unemployment_rate_lag_n` | decimal/null | Before month `t` | Unemployment level observed `n` months before `t`. |
| `unemployment_change_3m_lag_n` | decimal/null | Before month `t` | Three-month unemployment change observed `n` months before `t`. |
| `hpi_change_yoy_lag_n` | decimal/null | Before month `t` | Year-over-year HPI change observed `n` months before `t`. |

## Vendor-shaped performance balance

`current_upb` in the raw performance output is beginning-of-month, even though
`delinquency_status` is end-of-month. This intentionally mirrors vendor-file
timing. The modeling panel removes the ambiguity by emitting `upb_bom` and
`upb_eom` separately.

## Vintage aggregate and forecast outputs

| Column | Meaning |
|---|---|
| `accounts_active` | Accounts with positive BOM balance at vintage/MOB. |
| `balance_active` | Sum of BOM UPB at vintage/MOB. |
| `gross_chargeoff_dollars` | BOM UPB on rows exiting as ChargeOff. |
| `recovery_dollars` | Net sales proceeds less foreclosure costs on ChargeOff rows. |
| `net_chargeoff_dollars` | Gross charge-off less recovery, floored at zero. |
| `prepay_dollars` | BOM UPB on rows exiting as Prepaid. |
| `repurchase_dollars` | BOM UPB on rows exiting as Repurchased. |
| `original_balance` | Sum of acquisition original UPB for the vintage. |
| `max_observed_mob` | Greatest MOB observable by the configured analysis date. |
| `original_accounts` | Acquisition account count in the configured monthly, quarterly, or annual cohort. |
| `chargeoff_accounts` | Number of accounts exiting as ChargeOff at vintage/MOB. |
| `transition_count` | Frequency weight for an observed origin/destination/MOB/score/month cell; censored rows excluded. |
| `is_observed` | True only for cells contained in the reporting triangle. |
| `cumulative_loss_rate_original_balance` | Cumulative net charge-off divided by fixed original balance; primary forecast measure. |
| `cumulative_loss_rate_average_outstanding` | Cumulative net charge-off divided by average active balance through that age. |
