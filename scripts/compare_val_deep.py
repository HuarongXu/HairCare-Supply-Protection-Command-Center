"""
Deep-dive: Why are validation materials missing from pipeline?
Check which filter step removes them.
"""
import sys, json, logging
sys.path.insert(0, 'scripts')
import matres_pipeline as mp
from pathlib import Path
import pandas as pd

logging.basicConfig(level=logging.WARNING)
pd.set_option('display.width', 300)
pd.set_option('display.max_columns', 30)

root = Path("0.Data Base/Production Volume")

# Read validation materials
val = pd.read_excel(root / "1864 Validation Data.xlsx")
val["material_key"] = val["Material"].astype(str).str.strip().apply(mp.normalize_material_key)
val_mats_jul = val[pd.to_numeric(val["7.2026"], errors="coerce").fillna(0) > 0]
val_material_keys = set(val_mats_jul["material_key"])
print(f"Validation materials with non-zero Jul: {len(val_material_keys)}")

# Read FG raw file - NO filters
fg_raw = mp.read_production_volume_report(root / "XQTC Production Vol FG 20260525.xls")
fg_raw["material_key"] = fg_raw["Material"].fillna("").astype(str).str.strip().apply(mp.normalize_material_key)
fg_raw["Plant_str"] = fg_raw["Plant"].fillna("").astype(str).str.strip()
fg_raw["Category_str"] = fg_raw["Categories / Members"].fillna("").astype(str).str.strip()
fg_raw["MRP_str"] = fg_raw["MRP Elements"].fillna("").astype(str).str.strip()

# Check: are these materials even in the FG file?
fg_1864_all = fg_raw[fg_raw["Plant_str"] == "1864"]
fg_all_mats = set(fg_1864_all["material_key"])
print(f"\nAll 1864 materials in FG file (no filter): {len(fg_all_mats)}")
print(f"Validation mats found in FG (no filter): {len(val_material_keys & fg_all_mats)}")
print(f"Validation mats NOT in FG at all: {len(val_material_keys - fg_all_mats)}")

missing_from_fg = val_material_keys - fg_all_mats
if missing_from_fg:
    print(f"\nSample missing from FG file entirely:")
    for m in sorted(missing_from_fg)[:10]:
        v = val_mats_jul[val_mats_jul["material_key"] == m]["7.2026"].sum()
        print(f"  {m}: validation Jul={v:,.0f}")

# Now check which filter removes the rest
present_in_fg = val_material_keys & fg_all_mats
if present_in_fg:
    print(f"\n--- Materials present in FG but need to check filters ---")
    sample_mats = fg_1864_all[fg_1864_all["material_key"].isin(present_in_fg)]
    
    # Check Category
    cat_values = sample_mats["Category_str"].str.replace(" ", "", regex=False).str.lower().unique()
    print(f"\nCategory values for these materials:")
    cat_counts = sample_mats["Category_str"].str.replace(" ", "", regex=False).str.lower().value_counts()
    for cat, cnt in cat_counts.items():
        is_match = cat == "2.0production/receipts"
        print(f"  '{cat}': {cnt} rows {'✓' if is_match else '✗ FILTERED OUT'}")
    
    # Check MRP Elements
    mrp_values = sample_mats["MRP_str"].str.replace(" ", "", regex=False).str.lower().value_counts()
    print(f"\nMRP Elements for these materials:")
    for mrp, cnt in mrp_values.items():
        is_match = mrp in mp.PRODUCTION_VOL_ALLOWED_MRP_ELEMENTS
        print(f"  '{mrp}': {cnt} rows {'✓' if is_match else '✗ FILTERED OUT'}")

    # After category + MRP filter
    cat_mask = sample_mats["Category_str"].str.replace(" ", "", regex=False).str.lower().eq("2.0production/receipts")
    mrp_mask = sample_mats["MRP_str"].str.replace(" ", "", regex=False).str.lower().isin(mp.PRODUCTION_VOL_ALLOWED_MRP_ELEMENTS)
    after_filter = sample_mats[cat_mask & mrp_mask]
    print(f"\nAfter Category + MRP filter: {len(after_filter)} / {len(sample_mats)} rows")
    remaining_mats = set(after_filter["material_key"])
    filtered_out_by_cat_mrp = present_in_fg - remaining_mats
    if filtered_out_by_cat_mrp:
        print(f"Materials lost by Category/MRP filter: {len(filtered_out_by_cat_mrp)}")
        for m in sorted(filtered_out_by_cat_mrp)[:5]:
            rows = sample_mats[sample_mats["material_key"] == m]
            cats = rows["Category_str"].str.replace(" ", "", regex=False).str.lower().unique()
            mrps = rows["MRP_str"].str.replace(" ", "", regex=False).str.lower().unique()
            print(f"  {m}: Category={cats}, MRP={mrps}")

# Also check WIP file
print("\n" + "="*80)
print("CHECK WIP FILE")
print("="*80)
wip_raw = mp.read_production_volume_report(root / "XQTC Production Vol WIP 20260525.xls")
wip_raw["material_key"] = wip_raw["Material"].fillna("").astype(str).str.strip().apply(mp.normalize_material_key)
wip_raw["Plant_str"] = wip_raw["Plant"].fillna("").astype(str).str.strip()
wip_1864_all = wip_raw[wip_raw["Plant_str"] == "1864"]
wip_all_mats = set(wip_1864_all["material_key"])
print(f"All 1864 materials in WIP file (no filter): {len(wip_all_mats)}")
print(f"Validation mats found in WIP (no filter): {len(val_material_keys & wip_all_mats)}")

# Check validation SKU Types that are PP-WIP
print("\n" + "="*80)
print("VALIDATION SKU TYPE BREAKDOWN FOR JUL NON-ZERO")
print("="*80)
val_jul_nz = val[pd.to_numeric(val["7.2026"], errors="coerce").fillna(0) > 0]
sku_breakdown = val_jul_nz.groupby("SKU Type").agg(
    materials=("material_key", "nunique"),
    total_jul=("7.2026", lambda x: pd.to_numeric(x, errors="coerce").fillna(0).sum())
).reset_index()
print(sku_breakdown.to_string(index=False))

# Check: does the validation data include BOTH FG and WIP materials?
# PP-WIP in validation = WIP materials
pp_wip_mats = set(val_jul_nz[val_jul_nz["SKU Type"] == "PP-WIP"]["material_key"])
non_wip_mats = set(val_jul_nz[val_jul_nz["SKU Type"] != "PP-WIP"]["material_key"])
print(f"\nPP-WIP materials in validation: {len(pp_wip_mats)}")
print(f"Non-WIP materials in validation: {len(non_wip_mats)}")
print(f"PP-WIP mats found in WIP file: {len(pp_wip_mats & wip_all_mats)}")
print(f"PP-WIP mats found in FG file: {len(pp_wip_mats & fg_all_mats)}")
print(f"Non-WIP mats found in FG file: {len(non_wip_mats & fg_all_mats)}")
print(f"Non-WIP mats found in WIP file: {len(non_wip_mats & wip_all_mats)}")
