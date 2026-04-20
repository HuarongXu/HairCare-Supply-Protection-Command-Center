"""Generate a weekly mail HTML preview that mirrors the actual Outlook email format.

The generated HTML matches the structure of the real weekly update emails:
  - Dear all greeting
  - Demand System LBE bullet with monthly + quarterly LBE data
  - Supply Protection bullet with monthly + quarterly HS data + inventory totals
  - Dashboard link
  - Auto-captured screenshots of Demand Assumption and Supply Protection pages
  - PDE Alert section (no names, just a placeholder)
"""
from pathlib import Path
from datetime import datetime
import re
import sys
import time
import base64
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
LOCAL_DASHBOARD_URL = "http://127.0.0.1:8050/"

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


# ---------------------------------------------------------------------------
# Screenshot capture
# ---------------------------------------------------------------------------

def capture_dashboard_screenshots(out_dir: Path) -> dict:
    """Capture dashboard tab screenshots using headless Chrome.

    Returns a dict with keys 'demand_assumption' and 'supply_protection',
    values are Path objects (or None on failure).
    """
    result = {'demand_assumption': None, 'supply_protection': None, 'pde_alert': None}
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
    except ImportError:
        print('selenium not installed, skipping screenshots')
        return result

    opts = Options()
    opts.add_argument('--headless=new')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-gpu')
    opts.add_argument('--window-size=1920,4000')
    opts.add_argument('--force-device-scale-factor=1')

    driver = None
    try:
        driver = webdriver.Chrome(options=opts)
        dashboard_url = LOCAL_DASHBOARD_URL

        # 1. Demand Assumption tab (default tab)
        driver.get(dashboard_url)
        time.sleep(10)

        # Set page zoom to 90% so tables are not clipped / overlapping
        driver.execute_script("document.body.style.zoom = '0.9';")
        time.sleep(1)

        demand_path = out_dir / 'demand_assumption.png'
        driver.save_screenshot(str(demand_path))
        result['demand_assumption'] = demand_path
        print(f'screenshot: {demand_path.name}')

        # 2. Supply Protection tab
        tabs = driver.find_elements(By.CSS_SELECTOR, '.page-tab')
        for t in tabs:
            if 'Supply Protection' in t.text:
                t.click()
                break
        time.sleep(5)

        # Hide PDE panel (Past Due Alerts + FG Rolling tables) before screenshot
        driver.execute_script("""
            var panels = document.querySelectorAll('.pde-panel');
            panels.forEach(function(p){ p.style.display = 'none'; });
        """)
        time.sleep(0.5)

        supply_path = out_dir / 'supply_protection.png'
        driver.save_screenshot(str(supply_path))
        result['supply_protection'] = supply_path
        print(f'screenshot: {supply_path.name}')

        # 3. PDE Alert screenshot — element-level capture of .pde-panel only
        driver.execute_script("""
            var panels = document.querySelectorAll('.pde-panel');
            panels.forEach(function(p){ p.style.display = ''; });
        """)
        time.sleep(1)
        pde_el = driver.find_elements(By.CSS_SELECTOR, '.pde-panel')
        if pde_el:
            pde_path = out_dir / 'pde_alert.png'
            pde_el[0].screenshot(str(pde_path))
            result['pde_alert'] = pde_path
            print(f'screenshot: {pde_path.name}')

    except Exception as e:
        print(f'screenshot capture failed: {e}')
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    return result


def autocrop_screenshot(img_path: Path, padding: int = 8) -> None:
    """Trim whitespace (blank rows) from the bottom / right of a screenshot.

    Detects the background colour from the bottom-right corner and crops
    away uniform-background borders.
    """
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return  # Pillow not installed, skip crop

    img = Image.open(img_path).convert('RGB')
    # Use the bottom-right pixel as the "background" colour
    bg_color = img.getpixel((img.width - 1, img.height - 1))
    bg = Image.new('RGB', img.size, bg_color)
    diff = ImageChops.difference(img, bg)
    bbox = diff.getbbox()  # (left, upper, right, lower)
    if bbox:
        left = max(0, bbox[0] - padding)
        upper = max(0, bbox[1] - padding)
        right = min(img.width, bbox[2] + padding)
        lower = min(img.height, bbox[3] + padding)
        cropped = img.crop((left, upper, right, lower))
        cropped.save(img_path)
        print(f'cropped {img_path.name}: {img.size} -> {cropped.size}')


def image_to_base64(img_path: Path) -> str:
    """Read an image file and return a base64 data URI string."""
    if not img_path or not img_path.exists():
        return ''
    data = img_path.read_bytes()
    b64 = base64.b64encode(data).decode('ascii')
    return f'data:image/png;base64,{b64}'


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

# ── Supply protection inventory (all months total) ────────────────────────
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

# ===========================================================================
# Capture dashboard screenshots
# ===========================================================================
out_dir = ROOT / 'data' / 'processed' / 'weekly_mail'
out_dir.mkdir(parents=True, exist_ok=True)

screenshots = capture_dashboard_screenshots(out_dir)

# Auto-crop whitespace from screenshots
for key in ('demand_assumption', 'supply_protection', 'pde_alert'):
    if screenshots.get(key) and screenshots[key].exists():
        autocrop_screenshot(screenshots[key])

# Build image tags (embedded base64 for self-contained HTML)
demand_img_tag = ''
if screenshots.get('demand_assumption') and screenshots['demand_assumption'].exists():
    demand_b64 = image_to_base64(screenshots['demand_assumption'])
    if demand_b64:
        demand_img_tag = f'<img src="{demand_b64}" style="max-width:100%;border:1px solid #e5e7eb;border-radius:6px;" alt="Demand Assumption" />'

supply_img_tag = ''
if screenshots.get('supply_protection') and screenshots['supply_protection'].exists():
    supply_b64 = image_to_base64(screenshots['supply_protection'])
    if supply_b64:
        supply_img_tag = f'<img src="{supply_b64}" style="max-width:100%;border:1px solid #e5e7eb;border-radius:6px;" alt="Supply Protection" />'

pde_img_tag = ''
if screenshots.get('pde_alert') and screenshots['pde_alert'].exists():
    pde_b64 = image_to_base64(screenshots['pde_alert'])
    if pde_b64:
        pde_img_tag = f'<img src="{pde_b64}" style="max-width:100%;border:1px solid #e5e7eb;border-radius:6px;" alt="PDE Alert" />'

# ===========================================================================
# Build HTML — matching real Outlook email structure
# ===========================================================================
run_date = datetime.now().strftime('%Y%m%d')

# ── Demand System LBE sub-bullet ──────────────────────────────────────────
# Bold month/quarter tags and numeric values for readability
demand_lbe_parts = [
    f"<b>{month_text}</b> System LBE: <b>{fmt_msu(lbe_month)} msu</b> / IYA <b>{fmt_iya(lbe_iya_month)}</b>"
]
if quarter_tag:
    demand_lbe_parts.append(
        f"<b>{quarter_tag}</b> System LBE: <b>{fmt_mmsu(q_lbe)} Mmsu</b> / IYA <b>{fmt_iya(q_lbe_iya)}</b>"
    )
if next_quarter_tag:
    demand_lbe_parts.append(
        f"<b>{next_quarter_tag}</b> System LBE: <b>{fmt_mmsu(next_q_lbe)} Mmsu</b> / IYA <b>{fmt_iya(next_q_lbe_iya)}</b>"
    )
demand_lbe_line = ", ".join(demand_lbe_parts) + "."

# ── Supply Protection sub-bullets (one <li> per line) ─────────────────────
# Month line always present; each quarter gets its own <li>
supply_month_line = (
    f"<b>{month_text}</b> System LBE + Supply System Protection: "
    f"<b>{fmt_msu(hs_month)} msu</b> / IYA <b>{fmt_iya(hs_iya_month)}</b>."
)
supply_quarter_line = ''
if quarter_tag:
    supply_quarter_line = (
        f"<b>{quarter_tag}</b> System LBE + Supply System Protection: "
        f"<b>{fmt_mmsu(q_hs)} Mmsu</b> / IYA <b>{fmt_iya(q_hs_iya)}</b>."
    )
supply_next_quarter_line = ''
if next_quarter_tag:
    supply_next_quarter_line = (
        f"<b>{next_quarter_tag}</b> System LBE + Supply System Protection: "
        f"<b>{fmt_mmsu(next_q_hs)} Mmsu</b> / IYA <b>{fmt_iya(next_q_hs_iya)}</b>."
    )

# Supply inventory line
supply_inventory_line = f"Total: {fmt_msu(supply_total)} msu; FG: {fmt_msu(fg)} msu; Material: {fmt_msu(material)} msu."

# Screenshot or placeholder text
demand_screenshot_html = demand_img_tag if demand_img_tag else '<p style="font-size:13px;color:#9ca3af;margin:0;"><i>(Paste dashboard screenshot here)</i></p>'
supply_screenshot_html = supply_img_tag if supply_img_tag else '<p style="font-size:13px;color:#9ca3af;margin:0;"><i>(Paste dashboard screenshot here)</i></p>'
pde_screenshot_html = pde_img_tag if pde_img_tag else '<p style="font-size:13px;color:#9ca3af;margin:0;"><i>(Paste PDE screenshot here)</i></p>'

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
  <!-- Copy button (hidden when printing / pasting) -->
  <div style="text-align:right;padding:10px 24px 0 0;">
    <button onclick="copyEmail()" id="copyBtn"
      style="padding:8px 20px;font-size:13px;font-weight:600;cursor:pointer;
             background:#1d4ed8;color:#fff;border:none;border-radius:6px;">
      &#128203; Copy to Clipboard
    </button>
    <span id="copyMsg" style="margin-left:8px;font-size:12px;color:#16a34a;display:none;">Copied!</span>
  </div>
  <div id="emailContent" style="max-width:860px;margin:0 auto;padding:20px 24px;">

    <!-- Greeting -->
    <p style="font-size:14px;margin:0 0 14px 0;">Dear all</p>

    <p style="font-size:14px;margin:0 0 8px 0;">Here share you the supply protection key HL as below:</p>

    <!-- Demand System LBE -->
    <ul style="list-style-type:disc;margin:8px 0 0 20px;padding:0;">
      <li style="margin:4px 0;font-size:14px;"><b>Demand System LBE:</b>
        <ul style="list-style-type:circle;margin:4px 0 0 20px;padding:0;">
          <li style="margin:2px 0;font-size:14px;">{demand_lbe_line}</li>
        </ul>
      </li>
    </ul>

    <!-- Supply Protection -->
    <ul style="list-style-type:disc;margin:4px 0 0 20px;padding:0;">
      <li style="margin:4px 0;font-size:14px;"><b>Supply Protection:</b>
        <ul style="list-style-type:circle;margin:4px 0 0 20px;padding:0;">
          <li style="margin:2px 0;font-size:14px;">{supply_month_line}</li>
{('          <li style="margin:2px 0;font-size:14px;">' + supply_quarter_line + '</li>') if supply_quarter_line else ''}
{('          <li style="margin:2px 0;font-size:14px;">' + supply_next_quarter_line + '</li>') if supply_next_quarter_line else ''}
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
    {demand_screenshot_html}

    <br/>

    <!-- Supply Protection Section -->
    <p style="font-size:14px;font-weight:700;color:#1e3a8a;margin:18px 0 6px 0;">Supply Protection:</p>
    {supply_screenshot_html}

    <br/>

    <!-- PDE Alert -->
    <p style="font-size:14px;font-weight:700;color:#1e3a8a;margin:18px 0 6px 0;">PDE Alert:</p>
    {pde_screenshot_html}

    <!-- Footer -->
    <p style="font-size:11px;color:#9ca3af;margin:24px 0 0 0;">
      Auto-generated by Supply Protection Command Center &middot; {datetime.now().strftime('%Y-%m-%d %H:%M')}
    </p>

  </div>
  <script>
  function copyEmail() {{
    var content = document.getElementById('emailContent');
    var htmlStr = content.innerHTML;
    var wrapped = '<html><body style="font-family:Calibri,Arial,sans-serif;color:#1f2937;line-height:1.6;">' + htmlStr + '</body></html>';

    // Method 1: Clipboard API (only works on HTTPS / localhost)
    if (navigator.clipboard && typeof ClipboardItem !== 'undefined') {{
      try {{
        var htmlBlob = new Blob([wrapped], {{type: 'text/html'}});
        var textBlob = new Blob([content.innerText], {{type: 'text/plain'}});
        var item = new ClipboardItem({{'text/html': htmlBlob, 'text/plain': textBlob}});
        navigator.clipboard.write([item]).then(function() {{
          showCopyMsg();
        }}).catch(function() {{
          fallbackCopy(content);
        }});
        return;
      }} catch(e) {{}}
    }}
    // Method 2: Fallback for HTTP (non-secure context)
    fallbackCopy(content);
  }}

  function fallbackCopy(el) {{
    var range = document.createRange();
    range.selectNodeContents(el);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    try {{ document.execCommand('copy'); }} catch(e) {{}}
    sel.removeAllRanges();
    showCopyMsg();
  }}

  function showCopyMsg() {{
    var msg = document.getElementById('copyMsg');
    msg.style.display = 'inline';
    setTimeout(function(){{ msg.style.display = 'none'; }}, 2000);
  }}
  </script>
</body>
</html>
"""

# ===========================================================================
# Write output
# ===========================================================================
out_file = out_dir / f'Supply_Protection_Update_{run_date}.html'
out_file.write_text(html, encoding='utf-8')

print(f'generated={out_file}')
print(f'month={current_month}, quarter={quarter_tag}, next_quarter={next_quarter_tag}')
print(f'lbe_month={lbe_month:.3f}, lbe_iya_month={lbe_iya_month:.3f}')
print(f'hs_month={hs_month:.3f}, hs_iya_month={hs_iya_month:.3f}')
print(f'supply_total_all_months={supply_total:.3f}, fg={fg:.3f}, material={material:.3f}')
if screenshots.get('demand_assumption'):
    print(f'screenshot_demand={screenshots["demand_assumption"]}')
if screenshots.get('supply_protection'):
    print(f'screenshot_supply={screenshots["supply_protection"]}')
if screenshots.get('pde_alert'):
    print(f'screenshot_pde={screenshots["pde_alert"]}')