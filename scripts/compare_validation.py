"""
Compare pipeline 1864 data (Jul & Aug) vs user's validation spreadsheet.
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

# ── 1. Read validation file ──
val_path = root / "1864 Validation Data.xlsx"
# Try reading all sheets
xf = pd.ExcelFile(val_path)
print("Sheets:", xf.sheet_names)
for sheet in xf.sheet_names:
    df = pd.read_excel(val_path, sheet_name=sheet)
    print(f"\n=== Sheet: {sheet} ===")
    print(f"Shape: {df.shape}")
    print(f"Columns: {list(df.columns)}")
    print(df.head(30).to_string())
    print("---")
