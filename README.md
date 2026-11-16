# Myntra DOD sales dashboard – Nat Habit

A daily-refreshed dashboard for Myntra: monthly target phased by traffic into
day-on-day (DOD) targets, actuals from the Seller Orders Report, cumulative delta,
and a SKU × day grid coloured against planned DRR. Every month's data is kept.

Open `docs/index.html` in a browser, or publish it with GitHub Pages (below).

## Daily update (2 minutes)

1. Download the Myntra **Seller Orders Report** (month-to-date).
2. Drop the CSV in `inputs/sales_reports/`.
3. Run:

```bash
pip install -r requirements.txt     # first time only
python update_dashboard.py          # uses the newest CSV in inputs/sales_reports/
python update_dashboard.py --push   # same, then commits and pushes to GitHub
```

You can also pass a path: `python update_dashboard.py ~/Downloads/report.csv`.

### What the script does with the report

| Step | Same as the manual Excel process |
|---|---|
| Adds `units` = 1 for every order line | the units column |
| Keeps only lines where **all** of these are blank: cancellation reason id fk, cancellation reason, cancelled on, rto creation date, lost date, return creation date | filtering those columns to (Blanks) |
| Pivots on `created on` date → sum of `final amount` and units, by style ID | the pivot |
| Maps to the SKU plan on **Style ID** | the XLOOKUP |

Re-running with a newer report overwrites the dates it covers, so late cancellations
and returns are picked up automatically. Dates outside the report are left untouched.

## Start of each month (target + phasing)

1. Add a row to `inputs/targets/monthly_targets.csv`:

   ```
   month,target
   2026-12,5800000
   ```

2. Create `inputs/targets/2026-12.csv` with one row per day (copy `templates/target_template.csv`):

   ```
   date,day,event,offer_type,phasing
   2026-12-01,Tue,MFC,BAU,0.0330
   ```

   `phasing` is the traffic share for that day. DOD target = monthly target × phasing,
   and cumulative target is the running sum. If the phasing doesn't add up to exactly
   100%, it's rescaled so the days still sum to the monthly target (the script tells you).

3. Create `inputs/sku_plans/2026-12.csv` (copy `templates/sku_plan_template.csv`):
   `style_id, nh_sku, sp, category, variant, prev_month_drr, planned_drr`.
   If a month has no plan file, the most recent earlier plan is used.
   `prev_month_drr` is replaced automatically by the actual DRR when last month's data is saved.

## What's where

```
update_dashboard.py           the script you run
dashboard_template.html       dashboard layout (data is injected into it)
inputs/
  sales_reports/              raw Myntra CSVs  (NOT committed – contains customer city/pincode)
  targets/                    monthly_targets.csv + YYYY-MM.csv phasing per month
  sku_plans/                  YYYY-MM.csv planned DRR per SKU
data/
  sales/YYYY-MM.csv           cleaned date × style units and revenue – one file per month, kept forever
  exports/                    YYYY-MM_dod_target_vs_actual.csv, YYYY-MM_sku_daily_units.csv
  meta.json, update_log.csv   data-through date per month, run history
docs/index.html               the dashboard (GitHub Pages serves this)
```

## Reading the dashboard

- **Target vs actual** – how far ahead/behind the cumulative target you are, the DRR needed
  for the rest of the month, a day strip (green = beat that day's target, red = missed),
  charts, and the full phasing table with day delta and cumulative delta.
  Delta = cumulative actual − cumulative target: negative means behind plan.
- **SKU daily** – units (or revenue) per SKU per day. Green = at or above planned DRR,
  red = below. Search, filter by category, sort any column, or show only SKUs below plan MTD.
  SKUs selling but missing from the plan appear under "Not in plan".
- **Categories** – the same grid rolled up by category.
- Use the month picker to look at any past month.

## Sharing via GitHub

```bash
git init && git add . && git commit -m "Myntra DOD dashboard"
git remote add origin https://github.com/<org>/myntra-dod-dashboard.git
git push -u origin main
```

Then in the repo: **Settings → Pages → Build from branch → `main` / `docs`**.

⚠️ A GitHub Pages site is publicly reachable by anyone with the link, even when the
repo is private (unless your org is on GitHub Enterprise Cloud with private Pages).
This dashboard shows revenue and targets, so if it shouldn't be public, keep the repo
private and have teammates open `docs/index.html` from a clone instead.
