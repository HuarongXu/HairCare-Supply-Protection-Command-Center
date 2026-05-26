"""
Compare pipeline vs validation - with PROPER deduplication like the real pipeline.
"""
import sys, json, logging, shutil
sys.path.insert(0, 'scripts')
import matres_pipeline as mp
from pathlib import Path
import pandas as pd

logging.basicConfig(level=logging.WARNING)
pd.set_option('display.width', 300)
pd.set_option('display.max_columns', 30)
pd.set_option('display.float_format', '{:.1f}'.format)

root = Path("0.Data Base/Production Volume")

# ── Build 9SU mapping with proper dedup (same as read_xqtc_9su_mapping) ──
param_tmp = root / "Parameter_copy_tmp.xlsx"
shutil.copy2(root / "Parameter.xlsx", param_tmp)
param_raw = pd.read_excel(param_tmp)

code_col, su9_col, tech_col = "Code", "9字头SU", "Packing Type"
mapping = param_raw[[code_col, su9_col, tech_col]].copy()
mapping.columns = ["Code", "SU9", "Technology"]
mapping["material_key"] = mapping["Code"].apply(mp.normalize_material_key)
mapping["su9"] = pd.to_numeric(mapping["SU9"], errors="coerce")
tech_norm = mapping["Technology"].fillna("").astype(str).str.strip().str.lower()
mapping["is_bottle_line"] = tech_norm.str.replace("-", " ").str.replace("_", " ").str.replace("  ", " ").eq("bottle line")
mapping = mapping[mapping["material_key"].astype(bool)].copy()

# THIS IS THE KEY: deduplicate like the real pipeline does
grouped = (
    mapping.groupby("material_key", dropna=False)
    .agg(
        su9=("su9", lambda s: pd.to_numeric(s, errors="coerce").dropna().iloc[0] if pd.to_numeric(s, errors="coerce").dropna().size > 0 else pd.NA),
        is_bottle_line=("is_bottle_line", "max"),
    )
    .reset_index()
)
grouped["is_bottle_line"] = grouped["is_bottle_line"].fillna(False).astype(bool)
xqtc_9su = grouped[["material_key", "su9", "is_bottle_line"]]
print(f"9SU mapping (deduped): {len(xqtc_9su)} unique materials, bottle_line={xqtc_9su['is_bottle_line'].sum()}")

# Also keep per-material SKU Type from Parameter for reference
param_raw["material_key"] = param_raw["Code"].apply(mp.normalize_material_key)
sku_type_map = param_raw.drop_duplicates("material_key")[["material_key", "SKU Type"]].copy()

# ── Read validation ──
val = pd.read_excel(root / "1864 Validation Data.xlsx")
val["material_key"] = val["Material"].astype(str).str.strip().apply(mp.normalize_material_key)

# ── Read & filter FG ──
fg_raw = mp.read_production_volume_report(root / "XQTC Production Vol FG 20260525.xls")
fg_raw["Category"] = fg_raw["Categories / Members"].fillna("").astype(str).str.strip()
fg_raw["Plant_str"] = fg_raw["Plant"].fillna("").astype(str).str.strip()
fg_raw["Material_str"] = fg_raw["Material"].fillna("").astype(str).str.strip()
fg_raw["MRP_str"] = fg_raw["MRP Elements"].fillna("").astype(str).str.strip()
fg_raw["material_key"] = fg_raw["Material_str"].apply(mp.normalize_material_key)

cat_mask = fg_raw["Category"].str.replace(" ", "", regex=False).str.lower().eq("2.0production/receipts")
mrp_mask = fg_raw["MRP_str"].str.replace(" ", "", regex=False).str.lower().isin(mp.PRODUCTION_VOL_ALLOWED_MRP_ELEMENTS)
plant_mask = fg_raw["Plant_str"] == "1864"
mat_mask = fg_raw["Material_str"].ne("")

fg_1864 = fg_raw[cat_mask & mrp_mask & plant_mask & mat_mask].copy()
print(f"FG after Cat+MRP+Plant=1864: {len(fg_1864)} rows, {fg_1864['material_key'].nunique()} materials")

fg_merged = fg_1864.merge(xqtc_9su, on="material_key", how="left")
fg_bottle = fg_merged[fg_merged["is_bottle_line"].fillna(False)].copy()
print(f"FG after bottle filter (deduped): {len(fg_bottle)} rows, {fg_bottle['material_key'].nunique()} materials")

# ── Read & filter WIP ──
wip_raw = mp.read_production_volume_report(root / "XQTC Production Vol WIP 20260525.xls")
wip_raw["Category"] = wip_raw["Categories / Members"].fillna("").astype(str).str.strip()
wip_raw["Plant_str"] = wip_raw["Plant"].fillna("").astype(str).str.strip()
wip_raw["Material_str"] = wip_raw["Material"].fillna("").astype(str).str.strip()
wip_raw["MRP_str"] = wip_raw["MRP Elements"].fillna("").astype(str).str.strip()
wip_raw["material_key"] = wip_raw["Material_str"].apply(mp.normalize_material_key)

cat_mask2 = wip_raw["Category"].str.replace(" ", "", regex=False).str.lower().eq("2.0production/receipts")
mrp_mask2 = wip_raw["MRP_str"].str.replace(" ", "", regex=False).str.lower().isin(mp.PRODUCTION_VOL_ALLOWED_MRP_ELEMENTS)
plant_mask2 = wip_raw["Plant_str"] == "1864"
mat_mask2 = wip_raw["Material_str"].ne("")

wip_1864 = wip_raw[cat_mask2 & mrp_mask2 & plant_mask2 & mat_mask2].copy()
wip_merged = wip_1864.merge(xqtc_9su, on="material_key", how="left")
wip_bottle = wip_merged[wip_merged["is_bottle_line"].fillna(False)].copy()
print(f"WIP after all filters (deduped): {len(wip_bottle)} rows, {wip_bottle['material_key'].nunique()} materials")

# ── Compute values ──
for month_col_raw, month_label in [("05.2026", "5月"), ("06.2026", "6月"), ("07.2026", "7月"), ("08.2026", "8月")]:
    fg_vals = mp._parse_numeric_series(fg_bottle[month_col_raw])
    wip_vals = mp._parse_numeric_series(wip_bottle[month_col_raw])
    wip_su9 = pd.to_numeric(wip_bottle["su9"], errors="coerce").fillna(0.0)
    
    fg_total = fg_vals.sum()
    wip_total = (wip_vals * wip_su9).sum()
    pipeline_total = fg_total + wip_total
    
    val_col = month_col_raw.replace("0", "", 1) if month_col_raw.startswith("0") else month_col_raw
    # Map to validation column names: 05.2026 -> 5.2026
    val_col_name = str(int(month_col_raw[:2])) + month_col_raw[2:]
    val_total = pd.to_numeric(val[val_col_name], errors="coerce").fillna(0).sum() if val_col_name in val.columns else 0
    
    gap = pipeline_total - val_total
    print(f"\n{month_label}: Pipeline={pipeline_total:,.0f} (FG={fg_total:,.0f} + WIP={wip_total:,.0f}) | Val={val_total:,.0f} | Gap={gap:,.0f} ({gap/1000:,.1f} MSU)")

# ── Material-level comparison for July ──
print("\n" + "="*80)
print("MATERIAL-LEVEL: July comparison")
print("="*80)

fg_by_mat = fg_bottle.copy()
fg_by_mat["jul_val"] = mp._parse_numeric_series(fg_by_mat["07.2026"])
fg_by_mat = fg_by_mat.groupby("material_key")["jul_val"].sum().reset_index().rename(columns={"jul_val": "fg_jul"})

wip_by_mat = wip_bottle.copy()
wip_by_mat["jul_raw"] = mp._parse_numeric_series(wip_by_mat["07.2026"])
wip_by_mat["jul_su9"] = pd.to_numeric(wip_by_mat["su9"], errors="coerce").fillna(0.0)
wip_by_mat["jul_val"] = wip_by_mat["jul_raw"] * wip_by_mat["jul_su9"]
wip_by_mat = wip_by_mat.groupby("material_key")["jul_val"].sum().reset_index().rename(columns={"jul_val": "wip_jul"})

val_by_mat = val.groupby("material_key").agg(
    val_jul=("7.2026", lambda x: pd.to_numeric(x, errors="coerce").fillna(0).sum()),
    sku_type=("SKU Type", "first"),
).reset_index()

compare = fg_by_mat.merge(wip_by_mat, on="material_key", how="outer").merge(val_by_mat, on="material_key", how="outer")
compare = compare.fillna(0)
compare["pipeline"] = compare["fg_jul"] + compare["wip_jul"]
compare["gap"] = compare["pipeline"] - compare["val_jul"]

# Pipeline-only
only_pipe = compare[(compare["pipeline"] > 0) & (compare["val_jul"] == 0)]
print(f"\nMaterials in pipeline but NOT in validation: {len(only_pipe)}, total={only_pipe['pipeline'].sum():,.0f}")
for _, r in only_pipe.sort_values("pipeline", ascending=False).iterrows():
    m = r['material_key']
    sku = sku_type_map[sku_type_map["material_key"] == m]["SKU Type"].values
    sku_str = sku[0] if len(sku) > 0 else "?"
    print(f"  {m}: pipeline={r['pipeline']:,.0f} | SKU={sku_str}")

# Both with gap
both_gap = compare[(compare["pipeline"] > 0) & (compare["val_jul"] > 0) & (compare["gap"].abs() > 0.5)]
print(f"\nMaterials in BOTH with gap > 0.5: {len(both_gap)}, total_gap={both_gap['gap'].sum():,.0f}")
if len(both_gap) > 0:
    for _, r in both_gap.sort_values("gap", key=abs, ascending=False).head(15).iterrows():
        ratio = r['pipeline'] / r['val_jul'] if r['val_jul'] > 0 else float('inf')
        print(f"  {r['material_key']}: pipe={r['pipeline']:,.0f} val={r['val_jul']:,.0f} gap={r['gap']:,.0f} ratio={ratio:.2f}x | {r['sku_type']}")

# Validation-only
only_val = compare[(compare["val_jul"] > 0) & (compare["pipeline"] == 0)]
print(f"\nMaterials in validation but NOT in pipeline: {len(only_val)}, total={only_val['val_jul'].sum():,.0f}")

param_tmp.unlink(missing_ok=True)
