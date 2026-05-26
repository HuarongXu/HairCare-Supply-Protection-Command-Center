"""
Compare pipeline vs validation - work around Parameter.xlsx lock by copying it first.
"""
import sys, json, logging, shutil, tempfile
sys.path.insert(0, 'scripts')
import matres_pipeline as mp
from pathlib import Path
import pandas as pd

logging.basicConfig(level=logging.WARNING)
pd.set_option('display.width', 300)
pd.set_option('display.max_columns', 30)
pd.set_option('display.float_format', '{:.1f}'.format)

root = Path("0.Data Base/Production Volume")

# ── Work around Parameter.xlsx lock ──
param_src = root / "Parameter.xlsx"
param_tmp = root / "Parameter_copy_tmp.xlsx"
try:
    shutil.copy2(param_src, param_tmp)
    print("Copied Parameter.xlsx to temp file")
except Exception as e:
    print(f"Cannot copy Parameter.xlsx: {e}")
    print("Trying to read directly...")

# Read 9SU mapping from copy
try:
    param_raw = pd.read_excel(param_tmp)
    print(f"Parameter rows: {len(param_raw)}, columns: {list(param_raw.columns)}")
except Exception:
    # Try reading from the SPI tools file which may also have the mapping
    print("Failed to read copy. Trying original...")
    param_raw = pd.read_excel(param_src)

# Build the mapping manually since read_xqtc_9su_mapping can't access locked file
normalized_map = {str(col).strip().lower(): col for col in param_raw.columns}

code_col = None
for key in ["code", "material", "material code"]:
    if key in normalized_map:
        code_col = normalized_map[key]
        break
if not code_col:
    for col in param_raw.columns:
        if "code" in str(col).lower() or "material" in str(col).lower():
            code_col = col
            break

su9_col = None
for col in param_raw.columns:
    if "9" in str(col) and "su" in str(col).lower():
        su9_col = col
        break

tech_col = None
for key in ["technology", "tech", "packing type", "type"]:
    if key in normalized_map:
        tech_col = normalized_map[key]
        break
if not tech_col:
    for col in param_raw.columns:
        if "technology" in str(col).lower() or "tech" in str(col).lower():
            tech_col = col
            break

print(f"Code col: {code_col}, SU9 col: {su9_col}, Tech col: {tech_col}")

mapping = param_raw[[code_col, su9_col, tech_col]].copy()
mapping.columns = ["Code", "SU9", "Technology"]
mapping["material_key"] = mapping["Code"].apply(mp.normalize_material_key)
mapping["su9"] = pd.to_numeric(mapping["SU9"], errors="coerce")
tech_norm = mapping["Technology"].fillna("").astype(str).str.strip().str.lower()
mapping["is_bottle_line"] = tech_norm.str.replace("-", " ").str.replace("_", " ").str.replace("  ", " ").eq("bottle line")
mapping = mapping[mapping["material_key"].astype(bool)].copy()
xqtc_9su = mapping[["material_key", "su9", "is_bottle_line"]].copy()
print(f"9SU mapping: {len(xqtc_9su)} rows, bottle_line={xqtc_9su['is_bottle_line'].sum()}")

# ── Read validation ──
val = pd.read_excel(root / "1864 Validation Data.xlsx")
val["material_key"] = val["Material"].astype(str).str.strip().apply(mp.normalize_material_key)

val_jul = pd.to_numeric(val["7.2026"], errors="coerce").fillna(0)
val_aug = pd.to_numeric(val["8.2026"], errors="coerce").fillna(0)
print(f"\nValidation 7月 total (raw): {val_jul.sum():,.0f}")
print(f"Validation 8月 total (raw): {val_aug.sum():,.0f}")

# ── Read FG file ──
fg_raw = mp.read_production_volume_report(root / "XQTC Production Vol FG 20260525.xls")
fg_raw["Category"] = fg_raw["Categories / Members"].fillna("").astype(str).str.strip()
fg_raw["Plant_str"] = fg_raw["Plant"].fillna("").astype(str).str.strip()
fg_raw["Material_str"] = fg_raw["Material"].fillna("").astype(str).str.strip()
fg_raw["MRP_str"] = fg_raw["MRP Elements"].fillna("").astype(str).str.strip()
fg_raw["material_key"] = fg_raw["Material_str"].apply(mp.normalize_material_key)

# Filter
cat_mask = fg_raw["Category"].str.replace(" ", "", regex=False).str.lower().eq("2.0production/receipts")
mrp_mask = fg_raw["MRP_str"].str.replace(" ", "", regex=False).str.lower().isin(mp.PRODUCTION_VOL_ALLOWED_MRP_ELEMENTS)
plant_mask = fg_raw["Plant_str"] == "1864"
mat_mask = fg_raw["Material_str"].ne("")

fg_1864 = fg_raw[cat_mask & mrp_mask & plant_mask & mat_mask].copy()
print(f"\nFG after Category+MRP+Plant=1864: {len(fg_1864)} rows, {fg_1864['material_key'].nunique()} unique materials")

# Bottle filter
fg_merged = fg_1864.merge(xqtc_9su, on="material_key", how="left")
fg_bottle = fg_merged[fg_merged["is_bottle_line"].fillna(False)].copy()
print(f"FG after bottle filter: {len(fg_bottle)} rows, {fg_bottle['material_key'].nunique()} unique materials")

fg_bottle["jul_raw"] = mp._parse_numeric_series(fg_bottle["07.2026"])
fg_bottle["aug_raw"] = mp._parse_numeric_series(fg_bottle["08.2026"])

# ── Read WIP file ──
wip_raw = mp.read_production_volume_report(root / "XQTC Production Vol WIP 20260525.xls")
wip_raw["Category"] = wip_raw["Categories / Members"].fillna("").astype(str).str.strip()
wip_raw["Plant_str"] = wip_raw["Plant"].fillna("").astype(str).str.strip()
wip_raw["Material_str"] = wip_raw["Material"].fillna("").astype(str).str.strip()
wip_raw["MRP_str"] = wip_raw["MRP Elements"].fillna("").astype(str).str.strip()
wip_raw["material_key"] = wip_raw["Material_str"].apply(mp.normalize_material_key)

cat_mask = wip_raw["Category"].str.replace(" ", "", regex=False).str.lower().eq("2.0production/receipts")
mrp_mask = wip_raw["MRP_str"].str.replace(" ", "", regex=False).str.lower().isin(mp.PRODUCTION_VOL_ALLOWED_MRP_ELEMENTS)
plant_mask = wip_raw["Plant_str"] == "1864"
mat_mask = wip_raw["Material_str"].ne("")

wip_1864 = wip_raw[cat_mask & mrp_mask & plant_mask & mat_mask].copy()
wip_merged = wip_1864.merge(xqtc_9su, on="material_key", how="left")
wip_bottle = wip_merged[wip_merged["is_bottle_line"].fillna(False)].copy()
print(f"WIP after all filters: {len(wip_bottle)} rows, {wip_bottle['material_key'].nunique()} unique materials")

wip_su9 = pd.to_numeric(wip_bottle["su9"], errors="coerce").fillna(0.0)
wip_bottle["jul_raw"] = mp._parse_numeric_series(wip_bottle["07.2026"])
wip_bottle["aug_raw"] = mp._parse_numeric_series(wip_bottle["08.2026"])
wip_bottle["jul_converted"] = wip_bottle["jul_raw"] * wip_su9
wip_bottle["aug_converted"] = wip_bottle["aug_raw"] * wip_su9

# ── Summary comparison ──
print("\n" + "="*80)
print("TOTAL COMPARISON (7月 & 8月)")
print("="*80)

fg_jul_total = fg_bottle["jul_raw"].sum()
fg_aug_total = fg_bottle["aug_raw"].sum()
wip_jul_total = wip_bottle["jul_converted"].sum()
wip_aug_total = wip_bottle["aug_converted"].sum()

pipeline_jul = fg_jul_total + wip_jul_total  # raw, same unit as validation
pipeline_aug = fg_aug_total + wip_aug_total

val_jul_total = val_jul.sum()
val_aug_total = val_aug.sum()

print(f"{'':>20} | {'7月 (raw)':>15} | {'8月 (raw)':>15}")
print("-"*60)
print(f"{'FG (raw)':>20} | {fg_jul_total:>15,.0f} | {fg_aug_total:>15,.0f}")
print(f"{'WIP (raw×su9)':>20} | {wip_jul_total:>15,.0f} | {wip_aug_total:>15,.0f}")
print(f"{'Pipeline Total':>20} | {pipeline_jul:>15,.0f} | {pipeline_aug:>15,.0f}")
print(f"{'Validation Total':>20} | {val_jul_total:>15,.0f} | {val_aug_total:>15,.0f}")
print(f"{'Gap (Pipe-Val)':>20} | {pipeline_jul-val_jul_total:>15,.0f} | {pipeline_aug-val_aug_total:>15,.0f}")

print(f"\n{'':>20} | {'7月 (MSU)':>15} | {'8月 (MSU)':>15}")
print("-"*60)
print(f"{'Pipeline /1000':>20} | {pipeline_jul/1000:>15,.1f} | {pipeline_aug/1000:>15,.1f}")
print(f"{'Validation /1000':>20} | {val_jul_total/1000:>15,.1f} | {val_aug_total/1000:>15,.1f}")
print(f"{'Gap (MSU)':>20} | {(pipeline_jul-val_jul_total)/1000:>15,.1f} | {(pipeline_aug-val_aug_total)/1000:>15,.1f}")

# ── Material-level comparison ──
print("\n" + "="*80)
print("MATERIAL-LEVEL: Pipeline materials NOT in Validation (non-zero Jul)")
print("="*80)

# Pipeline FG by material
fg_by_mat = fg_bottle.groupby("material_key").agg(
    fg_jul=("jul_raw", "sum"),
    fg_aug=("aug_raw", "sum"),
).reset_index()

# Pipeline WIP by material
wip_by_mat = wip_bottle.groupby("material_key").agg(
    wip_jul=("jul_converted", "sum"),
    wip_aug=("aug_converted", "sum"),
).reset_index()

# Validation by material
val_by_mat = val.groupby("material_key").agg(
    val_jul=("7.2026", lambda x: pd.to_numeric(x, errors="coerce").fillna(0).sum()),
    val_aug=("8.2026", lambda x: pd.to_numeric(x, errors="coerce").fillna(0).sum()),
    sku_type=("SKU Type", "first"),
).reset_index()

# Merge
compare = fg_by_mat.merge(wip_by_mat, on="material_key", how="outer").merge(val_by_mat, on="material_key", how="outer")
compare = compare.fillna(0)
compare["pipeline_jul"] = compare["fg_jul"] + compare["wip_jul"]
compare["pipeline_aug"] = compare["fg_aug"] + compare["wip_aug"]
compare["gap_jul"] = compare["pipeline_jul"] - compare["val_jul"]
compare["gap_aug"] = compare["pipeline_aug"] - compare["val_aug"]

# Materials in pipeline but NOT in validation
only_pipeline = compare[(compare["pipeline_jul"] > 0) & (compare["val_jul"] == 0)]
print(f"Materials in pipeline but NOT in validation (non-zero Jul): {len(only_pipeline)}")
total_extra_jul = only_pipeline["pipeline_jul"].sum()
total_extra_aug = only_pipeline["pipeline_aug"].sum()
print(f"Total extra Jul: {total_extra_jul:,.0f} (MSU: {total_extra_jul/1000:,.1f})")
print(f"Total extra Aug: {total_extra_aug:,.0f} (MSU: {total_extra_aug/1000:,.1f})")

if len(only_pipeline) > 0:
    # Look up their technology in Parameter
    only_pipeline = only_pipeline.merge(
        mapping[["material_key", "Technology"]].drop_duplicates("material_key"),
        on="material_key", how="left"
    )
    print(f"\nTop 20 by Jul value:")
    top = only_pipeline.sort_values("pipeline_jul", ascending=False).head(20)
    print(f"{'Material':>12} | {'Jul (raw)':>10} | {'Aug (raw)':>10} | {'Technology':>20}")
    print("-"*65)
    for _, r in top.iterrows():
        print(f"{r['material_key']:>12} | {r['pipeline_jul']:>10,.0f} | {r['pipeline_aug']:>10,.0f} | {str(r.get('Technology','')):>20}")

# Materials in validation but NOT in pipeline
only_val = compare[(compare["val_jul"] > 0) & (compare["pipeline_jul"] == 0)]
print(f"\nMaterials in validation but NOT in pipeline (non-zero Jul): {len(only_val)}")
if len(only_val) > 0:
    total_missing_jul = only_val["val_jul"].sum()
    print(f"Total missing Jul: {total_missing_jul:,.0f} (MSU: {total_missing_jul/1000:,.1f})")

# Materials in BOTH - check if values match
both = compare[(compare["pipeline_jul"] > 0) & (compare["val_jul"] > 0)]
print(f"\nMaterials in BOTH (non-zero Jul): {len(both)}")
both_gap = both[both["gap_jul"].abs() > 0.5]
print(f"With non-trivial gap: {len(both_gap)}")
if len(both_gap) > 0:
    print(f"{'Material':>12} | {'Pipeline':>10} | {'Validation':>10} | {'Gap':>10} | {'SKU Type':>15}")
    print("-"*70)
    for _, r in both_gap.sort_values("gap_jul", key=abs, ascending=False).head(20).iterrows():
        print(f"{r['material_key']:>12} | {r['pipeline_jul']:>10,.0f} | {r['val_jul']:>10,.0f} | {r['gap_jul']:>10,.0f} | {str(r['sku_type']):>15}")

# Clean up
try:
    param_tmp.unlink()
except:
    pass
