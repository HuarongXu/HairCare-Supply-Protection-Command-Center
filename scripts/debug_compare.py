"""Debug: compare what build_production_data_summary produces for each date, without bottle filter."""
import sys, json, logging
sys.path.insert(0, 'scripts')
import matres_pipeline as mp
from pathlib import Path
import pandas as pd

logging.basicConfig(level=logging.WARNING)

with open('config/config.json') as f:
    raw = json.load(f)
cfg = mp.PipelineConfig.from_dict(raw)
root = cfg.production_data_dir

# Build summary for each date, check results
for date_str in ['20260524', '20260525']:
    mtd_files = sorted(root.glob(f"*MTD*{date_str}*"))
    vol_files = sorted(root.glob(f"*Production Vol*{date_str}*"))
    mtd_files = [f for f in mtd_files if not f.name.startswith("~$")]
    vol_files = [f for f in vol_files if not f.name.startswith("~$")]
    
    print(f"\n=== Date: {date_str} ===")
    print(f"MTD files: {[f.name for f in mtd_files]}")
    print(f"Vol files: {[f.name for f in vol_files]}")
    
    df = mp.build_production_data_summary(root, cfg, mtd_report_files=mtd_files, vol_report_files=vol_files)
    print(f"Summary shape: {df.shape}")
    if not df.empty:
        # Show all plants, all numeric columns
        numeric_cols = [c for c in df.columns if c not in ['Plant', 'Level1', 'Level2']]
        plant_summary = df.groupby('Plant')[numeric_cols].sum().reset_index()
        pd.set_option('display.width', 300)
        pd.set_option('display.max_columns', 25)
        pd.set_option('display.float_format', '{:.1f}'.format)
        print(plant_summary.to_string(index=False))
