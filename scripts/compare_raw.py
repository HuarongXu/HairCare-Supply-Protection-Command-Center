"""Compare raw Excel file contents between dates 20260524 and 20260525."""
import sys, json
sys.path.insert(0, 'scripts')
from matres_pipeline import *
from pathlib import Path
import pandas as pd

with open('config/config.json') as f:
    raw = json.load(f)
cfg = PipelineConfig.from_dict(raw)
root = cfg.production_data_dir

# Read each XQTC Vol FG file directly
for date_str in ['20260524', '20260525']:
    fname = f"XQTC Production Vol FG {date_str}.xls"
    fpath = root / fname
    print(f"\n{'='*60}")
    print(f"File: {fname}")
    print(f"Modified: {pd.Timestamp(fpath.stat().st_mtime, unit='s')}")
    try:
        raw_df = read_production_volume_report(fpath)
        print(f"Shape: {raw_df.shape}")
        print(f"Columns: {list(raw_df.columns)}")
        # Group by Plant and sum numeric columns
        if 'Plant' in raw_df.columns:
            numeric_cols = raw_df.select_dtypes(include='number').columns.tolist()
            if numeric_cols:
                summary = raw_df.groupby('Plant')[numeric_cols].sum()
                print(summary.to_string())
        else:
            print("No Plant column found")
            print(raw_df.head(3).to_string())
    except Exception as e:
        print(f"Error: {e}")

# Also check XQTC Production Vol WIP
print("\n\n--- WIP FILES ---")
for date_str in ['20260524', '20260525']:
    fname = f"XQTC Production Vol WIP {date_str}.xls"
    fpath = root / fname
    print(f"\n{'='*60}")
    print(f"File: {fname}")
    print(f"Modified: {pd.Timestamp(fpath.stat().st_mtime, unit='s')}")
    try:
        raw_df = read_production_volume_report(fpath)
        print(f"Shape: {raw_df.shape}")
        if 'Plant' in raw_df.columns:
            numeric_cols = raw_df.select_dtypes(include='number').columns.tolist()
            if numeric_cols:
                summary = raw_df.groupby('Plant')[numeric_cols].sum()
                print(summary.to_string())
    except Exception as e:
        print(f"Error: {e}")
