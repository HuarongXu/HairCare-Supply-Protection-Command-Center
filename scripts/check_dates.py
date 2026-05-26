import sys, json
sys.path.insert(0, 'scripts')
from matres_pipeline import *
from pathlib import Path
import pandas as pd

with open('config/config.json') as f:
    raw = json.load(f)
cfg = PipelineConfig.from_dict(raw)
root = cfg.production_data_dir

all_reports = [p for p in root.glob('*.xls*') if p.is_file() and not p.name.startswith('~$')]
date_groups = {}
for p in all_reports:
    d = extract_date_from_filename(p)
    if not d:
        continue
    if d not in date_groups:
        date_groups[d] = {'mtd': [], 'vol': []}
    nl = p.name.lower()
    if 'mtd' in nl and 'production vol' not in nl:
        date_groups[d]['mtd'].append(p)
    elif 'production vol' in nl:
        date_groups[d]['vol'].append(p)

for d in sorted(date_groups):
    print(f"=== Date: {d} ===")
    for f in sorted(date_groups[d]['mtd']):
        print(f"  MTD: {f.name}")
    for f in sorted(date_groups[d]['vol']):
        print(f"  Vol: {f.name}")

    df = build_production_data_summary(
        root, cfg,
        mtd_report_files=sorted(date_groups[d]['mtd']),
        vol_report_files=sorted(date_groups[d]['vol']),
    )
    if not df.empty:
        summary = df.groupby('Plant')[['MTD', 'Left Production', 'Current Month Total']].sum().reset_index()
        for _, r in summary.iterrows():
            plant = r['Plant']
            mtd = r['MTD']
            left = r['Left Production']
            cmt = r['Current Month Total']
            print(f"  {plant:6s} MTD={mtd:>8.1f}  Left={left:>8.1f}  CMT={cmt:>8.1f}")
    else:
        print("  EMPTY")
    print()
