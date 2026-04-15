"""Generate a weekly mail HTML preview that mirrors the actual Outlook email format.

The generated HTML matches the structure of the real weekly update emails:
  - Dear all greeting
  - Demand bullet points (monthly + quarterly)
  - Supply Protection bullet point
  - Dashboard link
  - Section placeholders for Demand Assumption / Supply Protection screenshots
  - PDE Alert with @-mentions of requester emails
"""
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
    load_request_details,
    build_demand_hs_dataframe,
    build_demand_iya_table,
    build_demand_iya_by_quarter_table,
)

# ---------------------------------------------------------------------------
# Dashboard URL (used in the email body link)
# ---------------------------------------------------------------------------
DASHBOARD_URL = "http://143.35.13.175:8050/"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def to_num(v):
    """Convert a display value (string with commas / %) to float."""
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
    key_col = 'Prod Line AS' if 'Prod Line AS' in df.columns else (
        'Prod Line' if 'Prod Line' in df.columns else None)
    if not key_col:
        return pd.DataFrame()
    hit = df[df[key_col].astype(str).str.strip().str.lower() == 'total']
    return hit if not hit.empty else pd.DataFrame()


def get_total_iya_value(rows, period_key):
    for r in rows:
        bucket = str(r.get('Prod Line', r.get('Prod Line AS', ''))).strip().lower()
        if bucket == 'total':
            return to_num(r.get(period_key, 0))
    return 0.0


def get_pde_alert_emails(bundle):
    """Return a sorted list of unique requester emails from PDE alerts."""
    pde = pd.DataFrame(bundle.get('pde_alerts', []))
    if pde.empty or 'Requester Email' not in pde.columns:
        return []
    emails = set()
    for e in pde['Requester Email'].dropna():
        for part in str(e).split(';'):
            p = part.strip()
            if p:
                emails.add(p)
    return sorted(emails)


def email_display_name(email: str) -> str:
    """Convert 'chen.g.9@pg.com' → 'Chen, G' style display name."""
    local = email.split('@')[0] if '@' in email else email
    parts = [p.strip() for p in local.split('.') if p.strip()]
    if len(parts) >= 2:
        surname = parts[0].capitalize()
        given = parts[1].capitalize()
        return f"{surname}, {given}"
    return local.capitalize()


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------
fmt_msu = lambda x: f"{x:,.0f}"
fmt_mmsu = lambda x: f"{x / 1000:,.2f}"
fmt_iya = lambda x: f"{x:,.1f}%"


# ===========================================================================
# Main
# ===========================================================================
cfg = AppConfig.load(CONFIG_PATH)
bundle = load_data_bundle(cfg)

hc_idp_monthly = pd.DataFrame(bundle.get('hc_idp_monthly', []))
monthly_level1 = pd.DataFrame(bundle.get('monthly_level1', []))
historical_shipment = pd.DataFrame(bundle.get('historical_shipment', []))
monthly_item = pd.DataFrame(bundle.get('monthly_item', []))

# ── Demand monthly ────────────────────────────────────────────────────────
month_cols = sorted([
    c for c in hc_idp_monthly.columns
    if isinstance(c, str) and len(c) == 7 and c[4] == '-'
])
current_month = pick_current_month(month_cols)
month_text = pd.Period(current_month, freq='M').strftime('%b') if current_month else 'Current Month'

total_row = get_total_row(hc_idp_monthly)
lbe_month = to_num(total_row.iloc[0].get(current_month, 0)) if not total_row.empty and current_month else 0.0

_, lbe_iya_rows = build_demand_iya_table(hc_idp_monthly, historical_shipment)
lbe_iya_month = get_total_iya_value(lbe_iya_rows, current_month)

# ── HS monthly ────────────────────────────────────────────────────────────
hs_df = build_demand_hs_dataframe(hc_idp_monthly, monthly_level1)
hs_total_row = get_total_row(hs_df)
hs_month = to_num(hs_total_row.iloc[0].get(current_month, 0)) if not hs_total_row.empty and current_month else 0.0

_, hs_iya_rows = build_demand_iya_table(hs_df, historical_shipment)
hs_iya_month = get_total_iya_value(hs_iya_rows, current_month)

# ── Quarter data ──────────────────────────────────────────────────────────
q_cols, q_rows = build_demand_iya_by_quarter_table(hc_idp_monthly, hs_df, historical_shipment)
q_total = next((r for r in q_rows if str(r.get('Prod Line', '')).strip().lower() == 'total'), {})

q_tags = []
for col in q_cols:
    col_id = str(col.get('id', ''))
    m = re.match(r'^([A-Z]{3}) LBE$', col_id)
    if m:
        q_tags.append(m.group(1))
q_tags = list(dict.fromkeys(q_tags))

quarter_tag = q_tags[0] if q_tags else ''
q_lbe = to_num(q_total.get(f'{quarter_tag} LBE', 0))
q_hs = to_num(q_total.get(f'{quarter_tag} DSL+SSP', 0))
q_lbe_iya = to_num(q_total.get(f'{quarter_tag} LBE IYA', 0))
q_hs_iya = to_num(q_total.get(f'{quarter_tag} DSL+SSP IYA', 0))

next_quarter_tag = q_tags[1] if len(q_tags) > 1 else ''
next_q_lbe = to_num(q_total.get(f'{next_quarter_tag} LBE', 0)) if next_quarter_tag else 0.0
next_q_hs = to_num(q_total.get(f'{next_quarter_tag} DSL+SSP', 0)) if next_quarter_tag else 0.0
next_q_lbe_iya = to_num(q_total.get(f'{next_quarter_tag} LBE IYA', 0)) if next_quarter_tag else 0.0
next_q_hs_iya = to_num(q_total.get(f'{next_quarter_tag} DSL+SSP IYA', 0)) if next_quarter_tag else 0.0

# ── Supply protection (all months total) ──────────────────────────────────
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

# ── PDE alert emails ──────────────────────────────────────────────────────
pde_emails = get_pde_alert_emails(bundle)

# ===========================================================================
# Build HTML — matching real Outlook email structure
# ===========================================================================
run_date = datetime.now().strftime('%Y%m%d')

# Build demand bullet lines (each on one line, matching real email format)
demand_bullets = []
# Monthly
demand_bullets.append(
    f"{month_text} LBE: {fmt_msu(lbe_month)} msu / IYA {fmt_iya(lbe_iya_month)}, "
    f"HS: {fmt_msu(hs_month)} msu / IYA {fmt_iya(hs_iya_month)}."
)
# Current quarter
if quarter_tag:
    demand_bullets.append(
        f"{quarter_tag} LBE: {fmt_mmsu(q_lbe)} Mmsu / IYA {fmt_iya(q_lbe_iya)}, "
        f"HS: {fmt_mmsu(q_hs)} Mmsu / IYA {fmt_iya(q_hs_iya)}."
    )
# Next quarter
if next_quarter_tag:
    demand_bullets.append(
        f"{next_quarter_tag} LBE: {fmt_mmsu(next_q_lbe)} Mmsu / IYA {fmt_iya(next_q_lbe_iya)}, "
        f"HS: {fmt_mmsu(next_q_hs)} Mmsu / IYA {fmt_iya(next_q_hs_iya)}."
    )

demand_bullets_html = "\n".join(
    f'              <li style="margin:2px 0;font-size:14px;">{line}</li>'
    for line in demand_bullets
)

# Supply protection line
supply_line = f"Total: {fmt_msu(supply_total)} msu; FG: {fmt_msu(fg)} msu; Material: {fmt_msu(material)} msu."

# PDE alert @mentions
if pde_emails:
    pde_mentions = "&nbsp;&nbsp;".join(
        f'<a href="mailto:{e}" style="color:#1d4ed8;text-decoration:none;">@{email_display_name(e)}</a>'
        for e in pde_emails
    )
    pde_section = f"""
    <!-- PDE Alert -->
    <p style="font-size:14px;margin:18px 0 0 0;">
      <b>PDE Alert:</b>&nbsp; {pde_mentions}
    </p>"""
else:
    pde_section = ""

# ---------------------------------------------------------------------------
# Outlook-friendly HTML template (inline styles, no CSS classes)
# ---------------------------------------------------------------------------
html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width" />
  <title>Hair Protection Weekly Update-{run_date}</title>
  <!--[if mso]>
  <style type="text/css">
    body, p, li {{ font-family: Calibri, Arial, sans-serif; }}
  </style>
  <![endif]-->
</head>
<body style="margin:0;padding:0;font-family:Calibri,'Century Gothic','Segoe UI',Arial,sans-serif;color:#1f2937;line-height:1.6;background:#ffffff;">
  <div style="max-width:860px;margin:0 auto;padding:20px 24px;">

    <!-- Greeting -->
    <p style="font-size:14px;margin:0 0 14px 0;">Dear all</p>

    <p style="font-size:14px;margin:0 0 8px 0;">Here share you the supply protection key HL as below:</p>

    <!-- Demand -->
    <ul style="list-style-type:disc;margin:8px 0 0 20px;padding:0;">
      <li style="margin:4px 0;font-size:14px;"><b>Demand</b>
        <ul style="list-style-type:disc;margin:4px 0 0 20px;padding:0;">
{demand_bullets_html}
        </ul>
      </li>
    </ul>

    <!-- Supply Protection -->
    <ul style="list-style-type:disc;margin:8px 0 0 20px;padding:0;">
      <li style="margin:4px 0;font-size:14px;"><b>Supply Protection</b>
        <ul style="list-style-type:disc;margin:4px 0 0 20px;padding:0;">
              <li style="margin:2px 0;font-size:14px;">{supply_line}</li>
        </ul>
      </li>
    </ul>

    <!-- Dashboard link -->
    <p style="font-size:14px;margin:16px 0 0 0;">
      Detail, please refer to
      <a href="{DASHBOARD_URL}" style="color:#1d4ed8;text-decoration:underline;">Hair Care Supply Protection Commander Center</a>
      &nbsp;any question please contact with me, thanks
    </p>

    <br/>

    <!-- Demand Assumption Section -->
    <p style="font-size:14px;font-weight:700;color:#1e3a8a;margin:18px 0 6px 0;">Demand Assumption</p>
    <p style="font-size:13px;color:#9ca3af;margin:0;"><i>(Paste dashboard screenshot here)</i></p>

    <br/>

    <!-- Supply Protection Section -->
    <p style="font-size:14px;font-weight:700;color:#1e3a8a;margin:18px 0 6px 0;">Supply Protection:</p>
    <p style="font-size:13px;color:#9ca3af;margin:0;"><i>(Paste dashboard screenshot here)</i></p>

    <br/>
{pde_section}

    <!-- Footer -->
    <p style="font-size:11px;color:#9ca3af;margin:24px 0 0 0;">
      Auto-generated by Supply Protection Command Center &middot; {datetime.now().strftime('%Y-%m-%d %H:%M')}
    </p>

  </div>
</body>
</html>
"""

# ===========================================================================
# Write output
# ===========================================================================
out_dir = ROOT / 'data' / 'processed' / 'weekly_mail'
out_dir.mkdir(parents=True, exist_ok=True)
out_file = out_dir / f'Supply_Protection_Update_{run_date}.html'
out_file.write_text(html, encoding='utf-8')

print(f'generated={out_file}')
print(f'month={current_month}, quarter={quarter_tag}, next_quarter={next_quarter_tag}')
print(f'lbe_month={lbe_month:.3f}, lbe_iya_month={lbe_iya_month:.3f}')
print(f'hs_month={hs_month:.3f}, hs_iya_month={hs_iya_month:.3f}')
print(f'supply_total_all_months={supply_total:.3f}, fg={fg:.3f}, material={material:.3f}')
if pde_emails:
    print(f'pde_alert_emails={", ".join(pde_emails)}')