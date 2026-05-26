"""
Compare pipeline 1864 Jul & Aug data vs validation spreadsheet.
"""
import sys, json, logging
sys.path.insert(0, 'scripts')
import matres_pipeline as mp
from pathlib import Path
import pandas as pd
import numpy as np

logging.basicConfig(level=logging.WARNING)
pd.set_option('display.width', 300)
pd.set_option('display.max_columns', 30)
pd.set_option('display.float_format', '{:.1f}'.format)

root = Path("0.Data Base/Production Volume")

# ── 1. Read validation file ──
val = pd.read_excel(root / "1864 Validation Data.xlsx")
val["Material"] = val["Material"].astype(str).str.strip()
val["material_key"] = val["Material"].apply(mp.normalize_material_key)

print("="*80)
print("VALIDATION DATA SUMMARY")
print("="*80)
print(f"Total rows: {len(val)}")
print(f"Unique materials: {val['material_key'].nunique()}")
print(f"Line Types: {val['Line Type'].unique()}")
print(f"SKU Types: {val['SKU Type'].unique()}")

# Validation totals for Jul & Aug (columns: 7.2026, 8.2026)
val_jul = pd.to_numeric(val['7.2026'], errors='coerce').fillna(0).sum()
val_aug = pd.to_numeric(val['8.2026'], errors='coerce').fillna(0).sum()
val_may = pd.to_numeric(val['5.2026'], errors='coerce').fillna(0).sum()
val_jun = pd.to_numeric(val['6.2026'], errors='coerce').fillna(0).sum()
print(f"\nValidation totals (raw, NOT divided by 1000):")
print(f"  5月: {val_may:,.0f}")
print(f"  6月: {val_jun:,.0f}")
print(f"  7月: {val_jul:,.0f}")
print(f"  8月: {val_aug:,.0f}")

# Check what SKU types contribute
for col_name, col_label in [('7.2026', '7月'), ('8.2026', '8月')]:
    print(f"\n{col_label} by SKU Type:")
    by_sku = val.groupby('SKU Type')[col_name].apply(lambda x: pd.to_numeric(x, errors='coerce').fillna(0).sum())
    for sku, v in by_sku.items():
        if v != 0:
            print(f"  {sku}: {v:,.0f}")

# ── 2. Pipeline: get our data for 1864 ──
print("\n" + "="*80)
print("PIPELINE DATA - XQTC Production Vol FG 20260525.xls")
print("="*80)

with open('config/config.json') as f:
    raw_cfg = json.load(f)
cfg = mp.PipelineConfig.from_dict(raw_cfg)

# Read FG file
fg_raw = mp.read_production_volume_report(root / "XQTC Production Vol FG 20260525.xls")
fg_raw["Category"] = fg_raw["Categories / Members"].fillna("").astype(str).str.strip()
fg_raw["Plant_str"] = fg_raw["Plant"].fillna("").astype(str).str.strip()
fg_raw["Material_str"] = fg_raw["Material"].fillna("").astype(str).str.strip()
fg_raw["MRP_str"] = fg_raw["MRP Elements"].fillna("").astype(str).str.strip()

# Apply filters
cat_mask = fg_raw["Category"].str.replace(" ", "", regex=False).str.lower().eq("2.0production/receipts")
mrp_mask = fg_raw["MRP_str"].str.replace(" ", "", regex=False).str.lower().isin(mp.PRODUCTION_VOL_ALLOWED_MRP_ELEMENTS)
plant_mask = fg_raw["Plant_str"] == "1864"
mat_mask = fg_raw["Material_str"].ne("")

fg_1864 = fg_raw[cat_mask & mrp_mask & plant_mask & mat_mask].copy()
print(f"After Category + MRP + Plant=1864 filter: {len(fg_1864)} rows")

# Apply bottle filter
xqtc_9su = mp.read_xqtc_9su_mapping(root)
fg_1864["material_key"] = fg_1864["Material_str"].apply(mp.normalize_material_key)
fg_1864_merged = fg_1864.merge(xqtc_9su, on="material_key", how="left")
fg_1864_bottle = fg_1864_merged[fg_1864_merged["is_bottle_line"].fillna(False)].copy()
print(f"After bottle filter: {len(fg_1864_bottle)} rows")

# FG totals for Jul & Aug
fg_jul = mp._parse_numeric_series(fg_1864_bottle["07.2026"]).sum()
fg_aug = mp._parse_numeric_series(fg_1864_bottle["08.2026"]).sum()
fg_may = mp._parse_numeric_series(fg_1864_bottle["05.2026"]).sum()
fg_jun = mp._parse_numeric_series(fg_1864_bottle["06.2026"]).sum()
print(f"\nFG raw totals (before /1000):")
print(f"  5月: {fg_may:,.0f}")
print(f"  6月: {fg_jun:,.0f}")
print(f"  7月: {fg_jul:,.0f}")
print(f"  8月: {fg_aug:,.0f}")
print(f"\nFG /1000 (MSU):")
print(f"  5月: {fg_may/1000:,.1f}")
print(f"  6月: {fg_jun/1000:,.1f}")
print(f"  7月: {fg_jul/1000:,.1f}")
print(f"  8月: {fg_aug/1000:,.1f}")

# ── 3. Pipeline: WIP file ──
print("\n" + "="*80)
print("PIPELINE DATA - XQTC Production Vol WIP 20260525.xls")
print("="*80)

wip_raw = mp.read_production_volume_report(root / "XQTC Production Vol WIP 20260525.xls")
wip_raw["Category"] = wip_raw["Categories / Members"].fillna("").astype(str).str.strip()
wip_raw["Plant_str"] = wip_raw["Plant"].fillna("").astype(str).str.strip()
wip_raw["Material_str"] = wip_raw["Material"].fillna("").astype(str).str.strip()
wip_raw["MRP_str"] = wip_raw["MRP Elements"].fillna("").astype(str).str.strip()

cat_mask = wip_raw["Category"].str.replace(" ", "", regex=False).str.lower().eq("2.0production/receipts")
mrp_mask = wip_raw["MRP_str"].str.replace(" ", "", regex=False).str.lower().isin(mp.PRODUCTION_VOL_ALLOWED_MRP_ELEMENTS)
plant_mask = wip_raw["Plant_str"] == "1864"
mat_mask = wip_raw["Material_str"].ne("")

wip_1864 = wip_raw[cat_mask & mrp_mask & plant_mask & mat_mask].copy()
print(f"After Category + MRP + Plant=1864 filter: {len(wip_1864)} rows")

wip_1864["material_key"] = wip_1864["Material_str"].apply(mp.normalize_material_key)
wip_1864_merged = wip_1864.merge(xqtc_9su, on="material_key", how="left")
wip_1864_bottle = wip_1864_merged[wip_1864_merged["is_bottle_line"].fillna(False)].copy()
print(f"After bottle filter: {len(wip_1864_bottle)} rows")

# WIP: raw × su9 / 1000
wip_su9 = pd.to_numeric(wip_1864_bottle.get("su9", 0.0), errors="coerce").fillna(0.0)
wip_jul_raw = mp._parse_numeric_series(wip_1864_bottle["07.2026"])
wip_aug_raw = mp._parse_numeric_series(wip_1864_bottle["08.2026"])
wip_may_raw = mp._parse_numeric_series(wip_1864_bottle["05.2026"])
wip_jun_raw = mp._parse_numeric_series(wip_1864_bottle["06.2026"])

wip_jul = (wip_jul_raw * wip_su9).sum() / 1000
wip_aug = (wip_aug_raw * wip_su9).sum() / 1000
wip_may = (wip_may_raw * wip_su9).sum() / 1000
wip_jun = (wip_jun_raw * wip_su9).sum() / 1000

print(f"\nWIP converted totals (raw × su9 / 1000, MSU):")
print(f"  5月: {wip_may:,.1f}")
print(f"  6月: {wip_jun:,.1f}")
print(f"  7月: {wip_jul:,.1f}")
print(f"  8月: {wip_aug:,.1f}")

# ── 4. Compare ──
print("\n" + "="*80)
print("COMPARISON: Pipeline (FG+WIP) vs Validation")
print("="*80)
pipeline_may = fg_may/1000 + wip_may
pipeline_jun = fg_jun/1000 + wip_jun
pipeline_jul = fg_jul/1000 + wip_jul
pipeline_aug = fg_aug/1000 + wip_aug

print(f"{'Month':>8} | {'Pipeline (MSU)':>15} | {'Validation (raw)':>16} | {'Val/1000 (MSU)':>15} | {'Gap (MSU)':>10}")
print("-"*75)
for label, p_val, v_raw in [
    ("5月", pipeline_may, val_may),
    ("6月", pipeline_jun, val_jun),
    ("7月", pipeline_jul, val_jul),
    ("8月", pipeline_aug, val_aug),
]:
    v_msu = v_raw / 1000
    gap = p_val - v_msu
    print(f"{label:>8} | {p_val:>15,.1f} | {v_raw:>16,.0f} | {v_msu:>15,.1f} | {gap:>10,.1f}")

# ── 5. Material-level comparison for Jul ──
print("\n" + "="*80)
print("MATERIAL-LEVEL COMPARISON: 7月 (July)")
print("="*80)

# Pipeline FG materials
fg_mats = fg_1864_bottle[["material_key", "07.2026"]].copy()
fg_mats["fg_jul"] = mp._parse_numeric_series(fg_mats["07.2026"])
fg_mats = fg_mats.groupby("material_key")["fg_jul"].sum().reset_index()

# Pipeline WIP materials
wip_mats = wip_1864_bottle[["material_key", "07.2026", "su9"]].copy()
wip_mats["wip_jul_raw"] = mp._parse_numeric_series(wip_mats["07.2026"])
wip_mats["su9_val"] = pd.to_numeric(wip_mats["su9"], errors="coerce").fillna(0.0)
wip_mats["wip_jul"] = wip_mats["wip_jul_raw"] * wip_mats["su9_val"]
wip_mats = wip_mats.groupby("material_key")["wip_jul"].sum().reset_index()

# Validation materials
val_mats = val[["material_key", "7.2026"]].copy()
val_mats["val_jul"] = pd.to_numeric(val_mats["7.2026"], errors="coerce").fillna(0)
val_mats = val_mats.groupby("material_key")["val_jul"].sum().reset_index()

# Merge all
compare = val_mats.merge(fg_mats, on="material_key", how="outer").merge(wip_mats, on="material_key", how="outer")
compare = compare.fillna(0)
compare["pipeline_total"] = compare["fg_jul"] + compare["wip_jul"]
compare["gap"] = compare["pipeline_total"] - compare["val_jul"]
compare = compare[compare["gap"].abs() > 0.5].sort_values("gap", key=abs, ascending=False)

print(f"Materials with gap > 0.5:")
print(f"{'Material':>12} | {'Val (raw)':>10} | {'FG (raw)':>10} | {'WIP (raw*su9)':>14} | {'Pipeline':>10} | {'Gap':>10}")
print("-"*80)
for _, row in compare.head(30).iterrows():
    print(f"{row['material_key']:>12} | {row['val_jul']:>10,.0f} | {row['fg_jul']:>10,.0f} | {row['wip_jul']:>14,.0f} | {row['pipeline_total']:>10,.0f} | {row['gap']:>10,.0f}")

# ── 6. Check materials in pipeline but NOT in validation ──
print("\n" + "="*80)
print("MATERIALS IN PIPELINE BUT NOT IN VALIDATION (non-zero Jul)")
print("="*80)
all_pipeline_mats = set(fg_mats[fg_mats["fg_jul"] != 0]["material_key"]).union(
    set(wip_mats[wip_mats["wip_jul"] != 0]["material_key"])
)
val_mat_set = set(val_mats["material_key"])
only_pipeline = all_pipeline_mats - val_mat_set
if only_pipeline:
    print(f"Count: {len(only_pipeline)}")
    for m in sorted(only_pipeline)[:20]:
        fg_v = fg_mats[fg_mats["material_key"] == m]["fg_jul"].sum()
        wip_v = wip_mats[wip_mats["material_key"] == m]["wip_jul"].sum()
        print(f"  {m}: FG={fg_v:,.0f}, WIP={wip_v:,.0f}")
else:
    print("None")

# ── 7. Check materials in validation but NOT in pipeline (non-zero Jul) ──
print("\n" + "="*80)
print("MATERIALS IN VALIDATION BUT NOT IN PIPELINE (non-zero Jul)")
print("="*80)
val_nonzero = set(val_mats[val_mats["val_jul"] != 0]["material_key"])
only_val = val_nonzero - all_pipeline_mats
if only_val:
    print(f"Count: {len(only_val)}")
    for m in sorted(only_val)[:20]:
        v = val_mats[val_mats["material_key"] == m]["val_jul"].sum()
        print(f"  {m}: Val={v:,.0f}")
else:
    print("None")
