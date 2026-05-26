"""Debug: check bottle filter impact on each date's XQTC FG files."""
import sys, json
sys.path.insert(0, 'scripts')
from matres_pipeline import *
from pathlib import Path
import pandas as pd

with open('config/config.json') as f:
    raw = json.load(f)
cfg = PipelineConfig.from_dict(raw)
root = cfg.production_data_dir

xqtc_9su_mapping = read_xqtc_9su_mapping(root)
print(f"9SU Mapping: {len(xqtc_9su_mapping)} rows")
print(f"Bottle lines: {xqtc_9su_mapping['is_bottle_line'].sum()}")
print()

for date_str in ['20260524', '20260525']:
    fname = f"XQTC Production Vol FG {date_str}.xls"
    fpath = root / fname
    print(f"=== {fname} ===")
    raw_df = read_production_volume_report(fpath)
    print(f"Raw rows: {len(raw_df)}")

    # Apply Category/MRP filter
    raw_df = standardize_column_names(raw_df)
    cat_col = _pick_column(raw_df, ["categories / members"], ["categories"])
    mrp_col = _pick_column(raw_df, ["mrp elements", "mrp element"], ["mrp", "element"])
    plant_col = _pick_column(raw_df, ["plant"], ["plant"])
    mat_col = _pick_column(raw_df, ["material"], ["material"])

    if cat_col and mrp_col and plant_col and mat_col:
        working = raw_df.copy()
        working.rename(columns={cat_col: "Category", mrp_col: "MRP Elements", plant_col: "Plant", mat_col: "Material"}, inplace=True)
        working["Category"] = working["Category"].fillna("").astype(str).str.strip()
        working["MRP Elements"] = working["MRP Elements"].fillna("").astype(str).str.strip()
        working["Plant"] = working["Plant"].fillna("").astype(str).str.strip()
        working["Material"] = working["Material"].fillna("").astype(str).str.strip()

        prod_filt = working[
            working["Category"].str.replace(" ", "", regex=False).str.lower().eq("2.0production/receipts")
            & working["MRP Elements"].str.replace(" ", "", regex=False).str.lower().isin(PRODUCTION_VOL_ALLOWED_MRP_ELEMENTS)
            & working["Plant"].ne("")
            & working["Material"].ne("")
        ]
        print(f"After Category/MRP filter: {len(prod_filt)} rows")

        # Apply bottle filter
        prod_filt = prod_filt.copy()
        prod_filt["material_key"] = prod_filt["Material"].apply(normalize_material_key)
        prod_filt = prod_filt[prod_filt["material_key"].astype(bool)].copy()
        merged = prod_filt.merge(xqtc_9su_mapping, on="material_key", how="left")
        bottle = merged[merged.get("is_bottle_line", pd.Series(False)).fillna(False)]
        print(f"After bottle filter: {len(bottle)} rows")

        # Summarize by plant
        if not bottle.empty:
            # Check month columns
            month_cols = [c for c in bottle.columns if re.fullmatch(r"\d{2}\.\d{4}", str(c))]
            print(f"Month columns in data: {month_cols[:3]}...")
            by_plant = bottle.groupby("Plant").size()
            print(f"Rows by plant:\n{by_plant.to_string()}")
    print()
