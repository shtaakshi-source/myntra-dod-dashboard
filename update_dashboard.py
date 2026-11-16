#!/usr/bin/env python3
"""
Myntra DOD sales dashboard - daily updater.

Usage
-----
    python update_dashboard.py                      # newest CSV in inputs/sales_reports/
    python update_dashboard.py path/to/report.csv   # a specific Myntra Seller Orders Report
    python update_dashboard.py --rebuild            # only rebuild the dashboard from saved data
    python update_dashboard.py --push               # ...and commit + push to GitHub afterwards

What it does
------------
1. Reads the Myntra Seller Orders Report.
2. Adds a `units` column (1 per order line).
3. Keeps only lines where ALL of these are blank (drops cancellations, RTOs, lost, returns):
       cancellation reason id fk, cancellation reason, cancelled on,
       rto creation date, lost date, return creation date
4. Pivots: created-on date x style id -> units, sum(final amount).
5. Saves that into data/sales/YYYY-MM.csv (one file per month, kept forever).
   Dates covered by the new report replace the old numbers for those dates,
   so late cancellations/returns are picked up on every run.
6. Combines saved sales with the month's target + traffic phasing
   (inputs/targets/) and SKU plan (inputs/sku_plans/), and writes
   docs/index.html (the dashboard) plus CSV exports in data/exports/.
"""
from __future__ import annotations

import argparse
import calendar
import json
import re
import subprocess
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
REPORTS_DIR = ROOT / "inputs" / "sales_reports"
TARGETS_DIR = ROOT / "inputs" / "targets"
PLANS_DIR = ROOT / "inputs" / "sku_plans"
SALES_DIR = ROOT / "data" / "sales"
EXPORTS_DIR = ROOT / "data" / "exports"
META_FILE = ROOT / "data" / "meta.json"
LOG_FILE = ROOT / "data" / "update_log.csv"
TEMPLATE = ROOT / "dashboard_template.html"
OUTPUT_HTML = ROOT / "docs" / "index.html"

EXCLUDE_COLS = [
    "cancellation reason id fk",
    "cancellation reason",
    "cancelled on",
    "rto creation date",
    "lost date",
    "return creation date",
]
IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- helpers
def log(msg: str) -> None:
    print(f"  {msg}")


def is_blank(series: pd.Series) -> pd.Series:
    return series.isna() | (series.astype(str).str.strip() == "")


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def month_days(month: str) -> list[date]:
    y, m = map(int, month.split("-"))
    return [date(y, m, d) for d in range(1, calendar.monthrange(y, m)[1] + 1)]


def load_meta() -> dict:
    if META_FILE.exists():
        return json.loads(META_FILE.read_text())
    return {"months": {}}


def save_meta(meta: dict) -> None:
    META_FILE.parent.mkdir(parents=True, exist_ok=True)
    META_FILE.write_text(json.dumps(meta, indent=2, sort_keys=True))


# --------------------------------------------------------------------------- step 1-4
def process_report(path: Path) -> tuple[pd.DataFrame, date, date]:
    """Clean a Seller Orders Report and pivot it to date x style."""
    print(f"\nReading {path.name}")
    df = pd.read_csv(path, dtype=str, keep_default_na=True)
    df.columns = [c.strip().lower() for c in df.columns]

    missing = [c for c in EXCLUDE_COLS + ["created on", "style id", "final amount"] if c not in df.columns]
    if missing:
        sys.exit(f"Report is missing expected columns: {missing}")

    total_lines = len(df)
    df["units"] = 1

    keep = pd.Series(True, index=df.index)
    for col in EXCLUDE_COLS:
        keep &= is_blank(df[col])
    clean = df[keep].copy()
    log(f"{total_lines:,} order lines -> {len(clean):,} kept after removing "
        f"cancelled / RTO / lost / returned ({total_lines - len(clean):,} removed)")

    clean["date"] = pd.to_datetime(clean["created on"], errors="coerce").dt.date
    clean = clean[clean["date"].notna()]
    clean["final amount"] = pd.to_numeric(clean["final amount"], errors="coerce").fillna(0.0)
    clean["style id"] = pd.to_numeric(clean["style id"], errors="coerce")
    clean = clean[clean["style id"].notna()]
    clean["style id"] = clean["style id"].astype("int64")
    sku_col = "seller sku code" if "seller sku code" in clean.columns else "style id"

    pivot = (
        clean.groupby(["date", "style id"])
        .agg(units=("units", "sum"),
             revenue=("final amount", "sum"),
             seller_sku=(sku_col, lambda s: s.mode().iat[0] if len(s.mode()) else s.iat[0]),
             style_name=("style name", "first") if "style name" in clean.columns else (sku_col, "first"))
        .reset_index()
        .rename(columns={"style id": "style_id"})
    )
    pivot["revenue"] = pivot["revenue"].round(2)

    # Which dates does this report fully cover? Prefer the range in the file name
    # (Myntra names it ..._YYYY-MM-DD_YYYY-MM-DD.csv); fall back to the data itself.
    dates = re.findall(r"(\d{4}-\d{2}-\d{2})", path.name)
    if len(dates) >= 2:
        start = datetime.strptime(dates[-2], "%Y-%m-%d").date()
        end = datetime.strptime(dates[-1], "%Y-%m-%d").date()
    else:
        start, end = min(pivot["date"]), max(pivot["date"])
    log(f"Report covers {start} to {end}")

    by_day = clean.groupby("date")["final amount"].sum()
    for d, v in by_day.items():
        log(f"{d}  Rs {v:>12,.2f}   {int((clean['date'] == d).sum()):>5} units")

    _append_log(path.name, total_lines, len(clean), start, end)
    return pivot, start, end


def _append_log(name, total, kept, start, end) -> None:
    row = pd.DataFrame([{
        "run_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M"),
        "report": name, "lines_in_report": total, "lines_kept": kept,
        "from": start, "to": end,
    }])
    row.to_csv(LOG_FILE, mode="a", header=not LOG_FILE.exists(), index=False)


# --------------------------------------------------------------------------- step 5
def merge_into_store(pivot: pd.DataFrame, start: date, end: date) -> list[str]:
    """Replace the covered dates in each month file with the new numbers."""
    SALES_DIR.mkdir(parents=True, exist_ok=True)
    meta = load_meta()
    touched = []
    covered = pd.date_range(start, end).date
    months = sorted({month_key(d) for d in covered} | {month_key(d) for d in pivot["date"]})

    for month in months:
        path = SALES_DIR / f"{month}.csv"
        new = pivot[pivot["date"].map(month_key) == month]
        if path.exists():
            old = pd.read_csv(path, parse_dates=["date"])
            old["date"] = old["date"].dt.date
            replace = set(d for d in covered if month_key(d) == month) | set(new["date"])
            old = old[~old["date"].isin(replace)]
            merged = pd.concat([old, new], ignore_index=True)
        else:
            merged = new
        merged = merged.sort_values(["date", "style_id"])
        merged.to_csv(path, index=False)

        m = meta["months"].setdefault(month, {})
        month_end = [d for d in covered if month_key(d) == month]
        if month_end:
            last = max(month_end).isoformat()
            m["data_through"] = max(m.get("data_through", last), last)
        m["updated_at"] = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
        touched.append(month)
        log(f"Saved data/sales/{month}.csv ({len(merged):,} rows)")

    save_meta(meta)
    return touched


# --------------------------------------------------------------------------- step 6
def load_targets(month: str) -> pd.DataFrame | None:
    path = TARGETS_DIR / f"{month}.csv"
    if not path.exists():
        return None
    t = pd.read_csv(path)
    t.columns = [c.strip().lower() for c in t.columns]
    t["date"] = pd.to_datetime(t["date"]).dt.date
    t["phasing"] = pd.to_numeric(t["phasing"], errors="coerce").fillna(0.0)
    for c in ("day", "event", "offer_type"):
        if c not in t.columns:
            t[c] = ""
        t[c] = t[c].fillna("").astype(str)

    monthly = pd.read_csv(TARGETS_DIR / "monthly_targets.csv")
    monthly["month"] = monthly["month"].astype(str).str.strip()
    row = monthly[monthly["month"] == month]
    if row.empty:
        log(f"WARNING: {month} has a phasing file but no row in monthly_targets.csv - skipping target")
        return None
    target = float(row["target"].iat[0])

    total = t["phasing"].sum()
    if abs(total - 1) > 0.001:
        log(f"NOTE: {month} phasing adds up to {total:.4f}, rescaling so the days sum to the monthly target")
    t["phasing_norm"] = t["phasing"] / total if total else 0
    t["dod_target"] = (t["phasing_norm"] * target).round(2)
    t["cum_target"] = t["dod_target"].cumsum().round(2)
    t.attrs["monthly_target"] = target
    return t


def load_plan(month: str) -> tuple[pd.DataFrame | None, str | None]:
    """SKU plan for the month; falls back to the latest earlier plan."""
    exact = PLANS_DIR / f"{month}.csv"
    candidates = [exact] if exact.exists() else sorted(
        p for p in PLANS_DIR.glob("*.csv") if p.stem < month)[-1:]
    if not candidates:
        return None, None
    plan = pd.read_csv(candidates[0])
    plan.columns = [c.strip().lower() for c in plan.columns]
    plan = plan.drop_duplicates("style_id")
    return plan, candidates[0].stem


def load_sales(month: str) -> pd.DataFrame:
    path = SALES_DIR / f"{month}.csv"
    if not path.exists():
        return pd.DataFrame(columns=["date", "style_id", "units", "revenue", "seller_sku", "style_name"])
    s = pd.read_csv(path, parse_dates=["date"])
    s["date"] = s["date"].dt.date
    return s


def prev_month(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y:04d}-{m - 1:02d}"


def build_month(month: str, meta: dict) -> dict:
    days = month_days(month)
    sales = load_sales(month)
    targets = load_targets(month)
    plan, plan_month = load_plan(month)
    info = meta["months"].get(month, {})
    through = info.get("data_through") or (max(sales["date"]).isoformat() if len(sales) else None)
    through_d = date.fromisoformat(through) if through else None

    # ---- day level
    daily = sales.groupby("date").agg(units=("units", "sum"), revenue=("revenue", "sum"))
    day_rows, cum_t, cum_a = [], 0.0, 0.0
    tmap = {r.date: r for r in targets.itertuples()} if targets is not None else {}
    for d in days:
        t = tmap.get(d)
        has_data = through_d is not None and d <= through_d
        actual = float(daily["revenue"].get(d, 0.0)) if has_data else None
        units = int(daily["units"].get(d, 0)) if has_data else None
        dod = float(t.dod_target) if t is not None else None
        if dod is not None:
            cum_t += dod
        if actual is not None:
            cum_a += actual
        day_rows.append({
            "date": d.isoformat(),
            "day": (t.day if t is not None and t.day else d.strftime("%a")),
            "event": t.event if t is not None else "",
            "offer": t.offer_type if t is not None else "",
            "phasing": float(t.phasing) if t is not None else None,
            "target": round(dod, 2) if dod is not None else None,
            "actual": round(actual, 2) if actual is not None else None,
            "units": units,
            "cumTarget": round(cum_t, 2) if dod is not None else None,
            "cumActual": round(cum_a, 2) if actual is not None else None,
            "delta": round(cum_a - cum_t, 2) if (actual is not None and dod is not None) else None,
            "dayDelta": round(actual - dod, 2) if (actual is not None and dod is not None) else None,
        })

    # ---- SKU level
    prev_sales = load_sales(prev_month(month))
    prev_days = prev_sales["date"].nunique() if len(prev_sales) else 0
    prev_drr_calc = (prev_sales.groupby("style_id")["units"].sum() / prev_days) if prev_days else pd.Series(dtype=float)

    units_pv = sales.pivot_table(index="style_id", columns="date", values="units", aggfunc="sum", fill_value=0)
    rev_pv = sales.pivot_table(index="style_id", columns="date", values="revenue", aggfunc="sum", fill_value=0)
    names = sales.drop_duplicates("style_id", keep="last").set_index("style_id")

    def series_for(style_id, pv, cast):
        out = []
        for d in days:
            if through_d is None or d > through_d:
                out.append(None)
            elif style_id in pv.index and d in pv.columns:
                out.append(cast(pv.at[style_id, d]))
            else:
                out.append(0)
        return out

    skus = []
    seen = set()
    if plan is not None:
        for r in plan.itertuples():
            sid = int(r.style_id)
            seen.add(sid)
            prev = getattr(r, "prev_month_drr", None)
            if sid in prev_drr_calc.index:
                prev = float(prev_drr_calc[sid])
            skus.append({
                "sku": str(r.nh_sku), "style": sid,
                "sp": _num(getattr(r, "sp", None)),
                "cat": str(getattr(r, "category", "") or ""),
                "name": str(getattr(r, "variant", "") or ""),
                "prevDrr": _num(prev, 2), "plan": _num(getattr(r, "planned_drr", None), 2),
                "u": series_for(sid, units_pv, int),
                "r": series_for(sid, rev_pv, lambda v: round(float(v), 2)),
                "inPlan": True,
            })
    for sid in units_pv.index:
        if int(sid) in seen:
            continue
        n = names.loc[sid] if sid in names.index else None
        skus.append({
            "sku": str(n["seller_sku"]) if n is not None else str(sid), "style": int(sid),
            "sp": None, "cat": "Not in plan",
            "name": str(n["style_name"]) if n is not None else "",
            "prevDrr": _num(prev_drr_calc.get(sid), 2), "plan": None,
            "u": series_for(sid, units_pv, int),
            "r": series_for(sid, rev_pv, lambda v: round(float(v), 2)),
            "inPlan": False,
        })

    _export(month, day_rows, skus, days)

    return {
        "month": month,
        "label": datetime.strptime(month, "%Y-%m").strftime("%B %Y"),
        "monthlyTarget": targets.attrs["monthly_target"] if targets is not None else None,
        "dataThrough": through,
        "updatedAt": info.get("updated_at"),
        "planMonth": plan_month,
        "days": [d.isoformat() for d in days],
        "daily": day_rows,
        "skus": skus,
    }


def _num(v, nd=None):
    try:
        if v is None or pd.isna(v):
            return None
        return round(float(v), nd) if nd is not None else float(v)
    except (TypeError, ValueError):
        return None


def _export(month, day_rows, skus, days) -> None:
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(day_rows).to_csv(EXPORTS_DIR / f"{month}_dod_target_vs_actual.csv", index=False)
    rows = []
    for s in skus:
        row = {"nh_sku": s["sku"], "style_id": s["style"], "category": s["cat"], "variant": s["name"],
               "prev_month_drr": s["prevDrr"], "planned_drr": s["plan"]}
        for d, u in zip(days, s["u"]):
            row[d.isoformat()] = u
        row["total_units"] = sum(u for u in s["u"] if u)
        rows.append(row)
    pd.DataFrame(rows).to_csv(EXPORTS_DIR / f"{month}_sku_daily_units.csv", index=False)


def render(meta: dict) -> None:
    months = sorted(
        {p.stem for p in SALES_DIR.glob("*.csv")}
        | {p.stem for p in TARGETS_DIR.glob("*.csv") if re.fullmatch(r"\d{4}-\d{2}", p.stem)}
    )
    if not months:
        sys.exit("Nothing to show yet: add a sales report or a target file first.")
    payload = {"generatedAt": datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST"),
               "months": [build_month(m, meta) for m in months]}
    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("/*__DATA__*/null", json.dumps(payload, separators=(",", ":")))
    OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    print(f"\nDashboard written to {OUTPUT_HTML.relative_to(ROOT)} ({len(months)} month(s): {', '.join(months)})")


# --------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report", nargs="?", help="Myntra Seller Orders Report CSV (default: newest in inputs/sales_reports/)")
    ap.add_argument("--rebuild", action="store_true", help="skip reading a report; rebuild the dashboard only")
    ap.add_argument("--push", action="store_true", help="git add, commit and push data/ docs/ inputs/ after updating")
    args = ap.parse_args()

    if not args.rebuild:
        if args.report:
            report = Path(args.report)
        else:
            files = sorted(REPORTS_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime)
            if not files:
                sys.exit("No report found. Put the Myntra CSV in inputs/sales_reports/ or pass its path.")
            report = files[-1]
        pivot, start, end = process_report(report)
        merge_into_store(pivot, start, end)

    render(load_meta())
    if args.push:
        push()


def push() -> None:
    msg = f"Update dashboard {datetime.now(IST).strftime('%Y-%m-%d %H:%M')} IST"
    try:
        subprocess.run(["git", "add", "data", "docs", "inputs/targets", "inputs/sku_plans"], cwd=ROOT, check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
            print("Nothing new to push.")
            return
        subprocess.run(["git", "commit", "-m", msg], cwd=ROOT, check=True)
        subprocess.run(["git", "push"], cwd=ROOT, check=True)
        print("Pushed to GitHub.")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"Git push failed ({e}). The dashboard is updated locally; push it manually.")


if __name__ == "__main__":
    main()
