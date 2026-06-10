"""Trace 1864 W23 weekly data to find double-counting."""
import pandas as pd
import pathlib, sys

root = pathlib.Path("0.Data Base/Production Volume")

weekly_files = sorted(root.glob("*Weekly*.xls"))
print(f"Weekly files found: {len(weekly_files)}")
for f in weekly_files:
    print(f"  {f.name}")
print()

# Group by type prefix (HP Weekly, XQTC FG Weekly, XQTC WIP Weekly)
from collections import defaultdict
type_groups = defaultdict(list)
for f in weekly_files:
    name = f.name.lower()
    if "hp" in name:
        type_groups["HP"].append(f)
    elif "wip" in name:
        type_groups["XQTC_WIP"].append(f)
    elif "xqtc" in name:
        type_groups["XQTC_FG"].append(f)

print("File groups:")
for gtype, files in sorted(type_groups.items()):
    print(f"  {gtype}: {[f.name for f in files]}")
print()

# For each file, read 1864 W23 data
for f in weekly_files:
    raw = pd.read_csv(f, sep="\t", encoding="utf-16")
    plant_mask = raw["Plant"].astype(str).str.strip() == "1864"
    if not plant_mask.any():
        plant_mask = raw["Plant"].astype(str).str.strip() == "1864.0"
    if not plant_mask.any():
        for p in raw["Plant"].unique():
            if "1864" in str(p):
                plant_mask = raw["Plant"] == p
                break

    sub = raw[plant_mask]
    if sub.empty:
        print(f"{f.name}: NO 1864 data")
        continue

    # Find category and MRP columns
    cat_col = [c for c in raw.columns if "categor" in c.lower()][0]
    mrp_col = [c for c in raw.columns if "mrp" in c.lower()][0]

    # Filter to production/receipts
    prod_mask = (
        sub[cat_col].astype(str).str.replace(" ", "").str.lower()
        == "2.0production/receipts"
    )
    allowed_mrp = {"2.1plannedorders", "2.2processorders"}
    mrp_mask = (
        sub[mrp_col].astype(str).str.replace(" ", "").str.lower().isin(allowed_mrp)
    )
    filtered = sub[prod_mask & ~mrp_mask]

    w23_cols = [c for c in raw.columns if "23" in str(c) and "2026" in str(c)]
    if not w23_cols:
        w23_cols = [c for c in raw.columns if "23." in str(c)]

    if w23_cols:
        wc = w23_cols[0]
        vals = pd.to_numeric(
            filtered[wc].astype(str).str.replace(",", ""), errors="coerce"
        )
        print(f"{f.name}: 1864 W23 = {vals.sum():.2f} CS ({len(filtered)} rows after filter)")
    else:
        print(f"{f.name}: no W23 column found")

print()
print("=" * 60)
print("CONCLUSION: If XQTC WIP has TWO files (0528 + 0529),")
print("pipeline sums both -> double counts WIP for 1864 W23.")
print("Should only keep the LATEST file per type.")
