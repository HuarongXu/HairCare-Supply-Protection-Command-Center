# MR Upload Tool - Requirements & Deployment Guide

## 1) Project Purpose
This project provides:
- Data pipeline processing for MatRes / HC IDP data.
- Interactive Dash dashboard for Demand Assumption, Supply Protection, and Project Details.

## 2) Runtime Requirements
- OS: Windows 10/11 or Windows Server (recommended for VM deployment)
- Python: 3.11+ (3.13 validated in current environment)
- Network: internal network access if shared by URL
- Browser: Edge/Chrome recommended

## 3) Python Dependencies
Install from `requirements.txt`:

```powershell
pip install -r requirements.txt
```

Main packages:
- dash
- pandas
- plotly
- openpyxl
- waitress (for stable VM hosting)

## 4) Project Structure (key paths)
- Dashboard app: `dashboards/matres_app.py`
- Pipeline: `scripts/matres_pipeline.py`
- Config: `config/config.json`
- Processed data output: `data/processed/`

## 5) Local Run (Developer)
### 5.1 Run pipeline
```powershell
& ".\.venv\Scripts\python.exe" scripts\matres_pipeline.py
```

### 5.2 Run dashboard (debug)
```powershell
& ".\.venv\Scripts\python.exe" dashboards\matres_app.py
```

## 6) VM Deployment (Recommended Sharing Mode)
> Goal: share dashboard by URL in email while keeping full interactivity.

### 6.1 Copy project to VM
Copy full folder (including `config/`, `dashboards/`, `scripts/`, `data/`).

### 6.2 Create / activate venv on VM
```powershell
python -m venv .venv
& ".\.venv\Scripts\Activate.ps1"
pip install -r requirements.txt
```

### 6.3 Run pipeline on VM
```powershell
& ".\.venv\Scripts\python.exe" scripts\matres_pipeline.py
```

### 6.4 Start dashboard on all network interfaces
Use non-debug mode and bind all NICs:

```powershell
& ".\.venv\Scripts\python.exe" -c "from dashboards.matres_app import app; app.run(host='0.0.0.0', port=8050, debug=False)"
```

### 6.5 Open firewall port (once)
Run as admin on VM:

```powershell
netsh advfirewall firewall add rule name="Dash8050" dir=in action=allow protocol=TCP localport=8050
```

### 6.6 Access URLs
- VM local test: `http://127.0.0.1:8050`
- LAN access: `http://<VM_IP>:8050`

## 7) Production-Stable Start (Waitress)
Install already included in requirements.

```powershell
& ".\.venv\Scripts\waitress-serve.exe" --listen=0.0.0.0:8050 dashboards.matres_app:app.server
```

## 8) Common Troubleshooting
### 8.1 `127.0.0.1 refused to connect`
- App process not running in current VM session
- Wrong path to python/venv
- Port not listening (check `netstat -ano | findstr :8050`)

### 8.2 `python.exe not recognized`
- Project not copied to VM or wrong path
- Use project-relative command from project root:
```powershell
& ".\.venv\Scripts\python.exe" ...
```

### 8.3 Can access by VM IP but not 127.0.0.1
- Browser proxy/bypass settings may block localhost
- Try `http://localhost:8050` and disable proxy for localhost

## 9) Suggested Operations
- Schedule `scripts/matres_pipeline.py` via Windows Task Scheduler
- Keep dashboard service running on VM (Task Scheduler or NSSM)
- Share only URL by email (do not send HTML expecting full Dash callbacks)

## 10) Business Logic & Calculation Rules

### 10.1 Data Sources
- MatRes source workbook: configured in `config/config.json`.
- HC IDP report: latest file matching `HC IDP HANA TD Report*.xls*` in project root.
	- `Monthly` sheet is used for month-based demand columns.
	- `Weekly(TP)` sheet is used for **current month** override (ER -> LBE logic).
- Historical baseline: `Historical Shipment Data_FY2425.xlsx` (`Sheet1`) for IYA denominator.

### 10.2 Pipeline Core Outputs (`data/processed`)
- `monthly_msu_by_item_text.csv`
- `monthly_msu_by_requester_item.csv`
- `monthly_msu_by_level1.csv`
- `pde_alerts.csv`
- `matres_request_details.csv`
- `level1_unmapped_materials.csv`
- `hc_idp_monthly_summary.csv`

### 10.3 Role / Requester Mapping
- Role mapping file: `config/requester_roles.json`.
- Email normalization includes typo cleanup such as `@pg,com -> @pg.com`.
- Unknown or unmatched emails default to `Others`.

### 10.4 Level1 Mapping Rules (Supply Protection)
- Mapping source workbook: configured Level1 workbook/sheet/columns.
- If Level1 is missing:
	- For `Item Text = RM Material`: force map to `Base`.
	- For other item types: keep as `未映射`.
- `未映射` row is hidden in UI when effective displayed total is near zero.

### 10.5 Demand LBE Logic
- Product line dimension in UI is displayed as `Prod Line` (Base / Promotion / Total).
- Time window: **current quarter + next quarter** (6 months total).
- Current month value source:
	- From `Weekly(TP)` sheet, using ER->LBE column and `Prod Line` bucket.
	- Convert SU to MSU using `/1000`.
- Non-current months value source:
	- From `Monthly` sheet values (also normalized to MSU).

### 10.6 Demand HS Logic
- `Demand HS = Demand LBE + Supply Protection` by month.
- Mapping for Supply Protection contribution:
	- `Base` -> HS Base
	- `PP` / `Promotion` -> HS Promotion
- HS Total = Base + Promotion.

### 10.7 IYA Logic
- `Demand LBE IYA` and `Demand HS IYA` are calculated month by month:
	- `IYA% = current_month_value / last_year_same_month_value * 100`
- Last-year baseline comes from `Historical Shipment Data_FY2425.xlsx` (`Sheet1`).
- If denominator is missing or zero, show `-`.

### 10.8 Demand IYA by quarter
- Quarter label uses current quarter month initials (e.g., `JFM`).
- Rows: `Base`, `Promotion`, `Total`.
- Columns:
	- `JFM LBE`: quarter sum of Demand LBE
	- `JFM HS`: quarter sum of Demand HS
	- `JFM LBE IYA`: quarter LBE / previous-year same-quarter baseline * 100
	- `JFM HS IYA`: quarter HS / previous-year same-quarter baseline * 100

### 10.9 UI Layout Rules (Demand Assumption)
- Two-column layout:
	- Row 1: `Demand LBE` | `Demand LBE IYA`
	- Row 2: `Demand HS` | `Demand HS IYA`
	- Row 3: `Supply Protection` | `Demand IYA by quarter`
- Demand-related table titles do not use `Table x:` prefix.

### 10.10 Formatting Rules
- Month label format: `YYYY-MM` across tables/charts.
- Demand numeric tables (`Demand LBE`, `Demand HS`) display integers (no decimals).
- IYA tables display percentages.

