from pathlib import Path
from datetime import datetime
import re
import sys
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboards.matres_app import (
    AppConfig,
    CONFIG_PATH,
    load_data_bundle,
    build_demand_hs_dataframe,
    build_demand_iya_table,
    build_demand_iya_by_quarter_table,
)


def to_num(v):
    try:
        if v is None:
            return 0.0
        if isinstance(v, str):
            t = v.replace(',', '').replace('%', '').strip()
            if t in {'', '-'}:
                return 0.0
            return float(t)
        return float(v)
    except Exception:
        return 0.0


def pick_current_month(months):
    cur = pd.Timestamp.today().to_period('M').strftime('%Y-%m')
    if cur in months:
        return cur
    return sorted(months)[0] if months else ''


def get_total_row(df):
    if df.empty:
        return pd.DataFrame()
    key_col = 'Prod Line AS' if 'Prod Line AS' in df.columns else ('Prod Line' if 'Prod Line' in df.columns else None)
    if not key_col:
        return pd.DataFrame()
    hit = df[df[key_col].astype(str).str.strip().str.lower() == 'total']
    if not hit.empty:
        return hit
    return pd.DataFrame()


def get_total_iya_value(rows, period_key):
    for r in rows:
        bucket = str(r.get('Prod Line', r.get('Prod Line AS', ''))).strip().lower()
        if bucket == 'total':
            return to_num(r.get(period_key, 0))
    return 0.0


cfg = AppConfig.load(CONFIG_PATH)
bundle = load_data_bundle(cfg)

hc_idp_monthly = pd.DataFrame(bundle.get('hc_idp_monthly', []))
monthly_level1 = pd.DataFrame(bundle.get('monthly_level1', []))
historical_shipment = pd.DataFrame(bundle.get('historical_shipment', []))
monthly_item = pd.DataFrame(bundle.get('monthly_item', []))

# Demand monthly
month_cols = sorted([c for c in hc_idp_monthly.columns if isinstance(c, str) and len(c) == 7 and c[4] == '-'])
current_month = pick_current_month(month_cols)
month_text = pd.Period(current_month, freq='M').strftime('%b') if current_month else 'Current Month'

total_row = get_total_row(hc_idp_monthly)
lbe_month = to_num(total_row.iloc[0].get(current_month, 0)) if not total_row.empty and current_month else 0.0

_, lbe_iya_rows = build_demand_iya_table(hc_idp_monthly, historical_shipment)
lbe_iya_month = get_total_iya_value(lbe_iya_rows, current_month)

# HS monthly
hs_df = build_demand_hs_dataframe(hc_idp_monthly, monthly_level1)
hs_total_row = get_total_row(hs_df)
hs_month = to_num(hs_total_row.iloc[0].get(current_month, 0)) if not hs_total_row.empty and current_month else 0.0

_, hs_iya_rows = build_demand_iya_table(hs_df, historical_shipment)
hs_iya_month = get_total_iya_value(hs_iya_rows, current_month)

# Quarter and next quarter
q_cols, q_rows = build_demand_iya_by_quarter_table(hc_idp_monthly, hs_df, historical_shipment)
q_total = next((r for r in q_rows if str(r.get('Prod Line', '')).strip().lower() == 'total'), {})

q_tags = []
for col in q_cols:
    col_id = str(col.get('id', ''))
    m = re.match(r'^([A-Z]{3}) LBE$', col_id)
    if m:
        q_tags.append(m.group(1))
q_tags = list(dict.fromkeys(q_tags))

quarter_tag = q_tags[0] if q_tags else 'Current Quarter'
q_lbe = to_num(q_total.get(f'{quarter_tag} LBE', 0))
q_hs = to_num(q_total.get(f'{quarter_tag} DSL+SSP', 0))
q_lbe_iya = to_num(q_total.get(f'{quarter_tag} LBE IYA', 0))
q_hs_iya = to_num(q_total.get(f'{quarter_tag} DSL+SSP IYA', 0))

next_quarter_tag = q_tags[1] if len(q_tags) > 1 else ''
next_q_lbe = to_num(q_total.get(f'{next_quarter_tag} LBE', 0)) if next_quarter_tag else 0.0
next_q_hs = to_num(q_total.get(f'{next_quarter_tag} DSL+SSP', 0)) if next_quarter_tag else 0.0
next_q_lbe_iya = to_num(q_total.get(f'{next_quarter_tag} LBE IYA', 0)) if next_quarter_tag else 0.0
next_q_hs_iya = to_num(q_total.get(f'{next_quarter_tag} DSL+SSP IYA', 0)) if next_quarter_tag else 0.0

# Supply protection: all months total (not restricted to current month)
if not monthly_item.empty:
    monthly_item['total_msu'] = pd.to_numeric(monthly_item.get('total_msu', 0), errors='coerce').fillna(0.0)
    item_sum = monthly_item.groupby('Item Text', dropna=False)['total_msu'].sum(min_count=1).to_dict()
else:
    item_sum = {}

fg = (
    to_num(item_sum.get('FG Rolling', 0))
    + to_num(item_sum.get('R Quotation', 0))
    + to_num(item_sum.get('R Component', 0))
)
material = to_num(item_sum.get('R Material', 0)) + to_num(item_sum.get('RM Material', 0))
supply_total = fg + material

fmt_msu = lambda x: f"{x:,.0f}"
fmt_mmsu = lambda x: f"{x/1000:,.2f}"
fmt_iya = lambda x: f"{x:,.1f}%"

next_quarter_line = f"""
        <li>
          {next_quarter_tag} System LBE: <span class=\"num\">{fmt_mmsu(next_q_lbe)} Mmsu</span> / IYA <span class=\"num\">{fmt_iya(next_q_lbe_iya)}</span>,
          System LBE + Supply System Protection: <span class=\"num\">{fmt_mmsu(next_q_hs)} Mmsu</span> / IYA <span class=\"num\">{fmt_iya(next_q_hs_iya)}</span>.
        </li>
""" if next_quarter_tag else ""

run_date = datetime.now().strftime('%Y-%m-%d')

html = f"""<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>Supply Protection Weekly Update</title>
  <style>
    body {{ font-family: 'Century Gothic', 'Segoe UI', sans-serif; color:#1f2937; line-height:1.5; }}
    .wrap {{ max-width: 980px; margin: 24px auto; padding: 20px 24px; border:1px solid #e5e7eb; border-radius: 14px; }}
    h2 {{ margin: 0 0 8px; color:#1e3a8a; }}
    .sub {{ color:#64748b; margin-bottom: 14px; }}
    .sec {{ margin-top: 16px; }}
    .sec h3 {{ margin: 0 0 8px; color:#1d4ed8; font-size:18px; }}
    ul {{ margin: 8px 0 0 18px; padding:0; }}
    li {{ margin: 6px 0; }}
    .hl {{ font-weight: 700; color:#0f172a; background:#f3f8ff; padding: 1px 6px; border-radius: 8px; }}
    .num {{ font-weight:700; color:#111827; }}
    .ft {{ margin-top: 18px; color:#6b7280; font-size:12px; }}
  </style>
</head>
<body>
  <div class=\"wrap\">
    <h2>Supply Protection Weekly Key Highlights</h2>
    <div class=\"sub\">Run Date: {run_date}</div>

    <div class=\"sec\">
      <h3>Here share you the supply protection key HL as below:</h3>
      <ul>
        <li><span class=\"hl\">Demand</span></li>
        <li>
          {month_text} System LBE: <span class=\"num\">{fmt_msu(lbe_month)} msu</span> / IYA <span class=\"num\">{fmt_iya(lbe_iya_month)}</span>,
          System LBE + Supply System Protection: <span class=\"num\">{fmt_msu(hs_month)} msu</span> / IYA <span class=\"num\">{fmt_iya(hs_iya_month)}</span>.
        </li>
        <li>
          {quarter_tag} System LBE: <span class=\"num\">{fmt_mmsu(q_lbe)} Mmsu</span> / IYA <span class=\"num\">{fmt_iya(q_lbe_iya)}</span>,
          System LBE + Supply System Protection: <span class=\"num\">{fmt_mmsu(q_hs)} Mmsu</span> / IYA <span class=\"num\">{fmt_iya(q_hs_iya)}</span>.
        </li>
{next_quarter_line}
      </ul>
    </div>

    <div class=\"sec\">
      <ul>
        <li><span class=\"hl\">Supply Protection</span></li>
        <li>
          Total: <span class=\"num\">{fmt_msu(supply_total)} msu</span>;
          FG: <span class=\"num\">{fmt_msu(fg)} msu</span>;
          Material: <span class=\"num\">{fmt_msu(material)} msu</span>.
        </li>
      </ul>
    </div>

    <div class=\"ft\">Auto-generated draft. Data source: dashboard processed datasets.</div>
  </div>
</body>
</html>
"""

out_dir = ROOT / 'data' / 'processed' / 'weekly_mail'
out_dir.mkdir(parents=True, exist_ok=True)
out_file = out_dir / f'Supply_Protection_Update_{datetime.now().strftime("%Y%m%d")}.html'
out_file.write_text(html, encoding='utf-8')

print(f'generated={out_file}')
print(f'month={current_month}, quarter={quarter_tag}, next_quarter={next_quarter_tag}')
print(f'lbe_month={lbe_month:.3f}, lbe_iya_month={lbe_iya_month:.3f}')
print(f'hs_month={hs_month:.3f}, hs_iya_month={hs_iya_month:.3f}')
print(f'supply_total_all_months={supply_total:.3f}, fg={fg:.3f}, material={material:.3f}')