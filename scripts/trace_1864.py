"""
Trace: 1864 plant, 2026-05 and 2026-06 production volume calculation
Using the CURRENT (20260525) date files as example.
"""
import sys, json, logging
sys.path.insert(0, 'scripts')
import matres_pipeline as mp
from pathlib import Path
import pandas as pd
import re

logging.basicConfig(level=logging.WARNING)
pd.set_option('display.width', 200)
pd.set_option('display.max_columns', 25)
pd.set_option('display.float_format', '{:.1f}'.format)

with open('config/config.json') as f:
    raw = json.load(f)
cfg = mp.PipelineConfig.from_dict(raw)
root = cfg.production_data_dir

print("="*80)
print("STEP-BY-STEP TRACE: Plant 1864, Months 2026-05 & 2026-06")
print("="*80)

# ── PART A: MTD (Month-To-Date actual production) ──
print("\n" + "="*80)
print("PART A: MTD CALCULATION (from XQTC MTD 20260525.xls)")
print("="*80)

mtd_path = root / "XQTC MTD 20260525.xls"
print(f"\nStep A1: Read {mtd_path.name}")
mtd_raw = mp.read_production_schedule_report(mtd_path)
print(f"  Raw rows: {len(mtd_raw)}, Columns: {list(mtd_raw.columns)}")

mtd_raw = mp.standardize_column_names(mtd_raw)
print(f"\nStep A2: Filter to Plant=1864")
mtd_raw["Plant"] = mtd_raw["Plant"].fillna("").astype(str).str.strip()
mtd_1864 = mtd_raw[mtd_raw["Plant"] == "1864"].copy()
print(f"  1864 rows: {len(mtd_1864)}")

print(f"\nStep A3: Parse StartDate and Deliv. Quantity")
date_col = [c for c in mtd_1864.columns if 'start' in c.lower() and 'date' in c.lower()][0]
deliv_col = [c for c in mtd_1864.columns if 'deliv' in c.lower()][0]
mtd_1864["StartDateParsed"] = pd.to_datetime(mtd_1864[date_col], errors="coerce")
mtd_1864 = mtd_1864[mtd_1864["StartDateParsed"].notna()].copy()
mtd_1864["mtd_qty"] = mp._parse_numeric_series(mtd_1864[deliv_col]) / 1000.0
mtd_1864["Month"] = mtd_1864["StartDateParsed"].dt.to_period("M").astype(str)
print(f"  Valid date rows: {len(mtd_1864)}")

print(f"\nStep A4: Sum by Month (unit: MSU = raw / 1000)")
mtd_by_month = mtd_1864.groupby("Month")["mtd_qty"].sum()
for m, v in mtd_by_month.items():
    print(f"  {m}: {v:.1f} MSU")

# ── PART B: Production Volume (planned/process orders) ──
print("\n" + "="*80)
print("PART B: PRODUCTION VOLUME (from XQTC Production Vol FG + WIP)")
print("="*80)

# B1: Read 9SU mapping
print(f"\nStep B0: Read Parameter.xlsx (9SU mapping & bottle filter)")
xqtc_9su = mp.read_xqtc_9su_mapping(root)
print(f"  Total materials in mapping: {len(xqtc_9su)}")
print(f"  Bottle-line materials: {xqtc_9su['is_bottle_line'].sum()}")

# B2: Process FG file
for file_type in ["FG", "WIP"]:
    fname = f"XQTC Production Vol {file_type} 20260525.xls"
    fpath = root / fname
    print(f"\n{'─'*60}")
    print(f"Step B1-{file_type}: Read {fname}")
    vol_raw = mp.read_production_volume_report(fpath)
    print(f"  Raw rows: {len(vol_raw)}, Columns: {list(vol_raw.columns)}")

    # Step B2: Filter Category + MRP Elements
    print(f"\nStep B2-{file_type}: Filter Category='2.0Production/Receipts' + MRP Elements")
    print(f"  Allowed MRP Elements: {mp.PRODUCTION_VOL_ALLOWED_MRP_ELEMENTS}")
    vol_raw["Category"] = vol_raw["Categories / Members"].fillna("").astype(str).str.strip()
    vol_raw["Plant_col"] = vol_raw["Plant"].fillna("").astype(str).str.strip()
    vol_raw["Material_col"] = vol_raw["Material"].fillna("").astype(str).str.strip()
    vol_raw["MRP_col"] = vol_raw["MRP Elements"].fillna("").astype(str).str.strip()
    
    cat_mask = vol_raw["Category"].str.replace(" ", "", regex=False).str.lower().eq("2.0production/receipts")
    mrp_mask = vol_raw["MRP_col"].str.replace(" ", "", regex=False).str.lower().isin(mp.PRODUCTION_VOL_ALLOWED_MRP_ELEMENTS)
    plant_mask = vol_raw["Plant_col"].ne("")
    mat_mask = vol_raw["Material_col"].ne("")
    
    print(f"  Category match: {cat_mask.sum()} / {len(vol_raw)}")
    print(f"  MRP Elements match: {mrp_mask.sum()} / {len(vol_raw)}")
    after_filter = vol_raw[cat_mask & mrp_mask & plant_mask & mat_mask].copy()
    print(f"  After all filters: {len(after_filter)} rows")

    # Step B3: Filter to 1864 plant
    print(f"\nStep B3-{file_type}: Filter to Plant=1864")
    p1864 = after_filter[after_filter["Plant_col"] == "1864"].copy()
    print(f"  1864 rows: {len(p1864)}")

    # Step B4: Bottle line filter
    print(f"\nStep B4-{file_type}: Apply bottle-line filter (Parameter.xlsx Technology='Bottle Line')")
    p1864["material_key"] = p1864["Material_col"].apply(mp.normalize_material_key)
    p1864_merged = p1864.merge(xqtc_9su, on="material_key", how="left")
    bottle_count = p1864_merged["is_bottle_line"].fillna(False).sum()
    non_bottle_count = (~p1864_merged["is_bottle_line"].fillna(False)).sum()
    print(f"  Bottle-line rows: {int(bottle_count)}")
    print(f"  Non-bottle-line (removed): {int(non_bottle_count)}")
    p1864_bottle = p1864_merged[p1864_merged["is_bottle_line"].fillna(False)].copy()

    # Step B5: Identify month columns
    month_cols = []
    for c in vol_raw.columns:
        normalized = mp._normalize_month_label(str(c).strip())
        if normalized:
            month_cols.append((c, normalized))
    
    print(f"\nStep B5-{file_type}: Month columns found: {[m[1] for m in month_cols]}")

    # Step B6: Parse numeric values and convert units
    print(f"\nStep B6-{file_type}: Sum month values for 1864 bottle-line materials")
    is_wip = file_type == "WIP"
    for orig_col, norm_label in month_cols:
        if norm_label not in ["2026-05", "2026-06"]:
            continue
        values = mp._parse_numeric_series(p1864_bottle[orig_col])
        if is_wip:
            su9_vals = pd.to_numeric(p1864_bottle.get("su9", 0.0), errors="coerce").fillna(0.0)
            converted = values * su9_vals / 1000.0
            formula = "raw_value × su9 / 1000"
        else:
            converted = values / 1000.0
            formula = "raw_value / 1000"
        total = converted.sum()
        print(f"  {norm_label}: raw_sum={values.sum():.1f}, formula={formula}, result={total:.1f} MSU")

print("\n" + "="*80)
print("PART C: FINAL ASSEMBLY")
print("="*80)
print("""
For each plant, the final values are:

  MTD = sum of Deliv. Quantity from MTD report (÷ 1000)
        → This is actual production already completed this month

  Left Production = sum of Production Vol month values for CURRENT month (2026-05)
        → This is remaining planned/process orders for current month
        → From XQTC FG (÷ 1000) + XQTC WIP (× su9 ÷ 1000)

  Current Month Total = MTD + Left Production

  2026-05 column = Left Production (same as above, replaces raw vol value)
  2026-06 column = Production Vol for June (planned orders)
  2026-07+ columns = Production Vol for future months
""")

# Run the actual build to show final numbers
df = mp.build_production_data_summary(
    root, cfg,
    mtd_report_files=[root / "XQTC MTD 20260525.xls", root / "HP MTD 20260525.xls"],
    vol_report_files=[root / "XQTC Production Vol FG 20260525.xls",
                      root / "XQTC Production Vol WIP 20260525.xls",
                      root / "HP Production Vol 20260525.xls"],
)
p1864_final = df[df["Plant"] == "1864"]
print("FINAL RESULT for 1864:")
print(p1864_final[["Plant", "MTD", "Left Production", "Current Month Total", "2026-05", "2026-06"]].to_string(index=False))
