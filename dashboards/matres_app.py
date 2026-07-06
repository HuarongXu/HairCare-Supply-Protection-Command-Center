"""Plotly Dash application for the MatRes dashboard MVP."""
from __future__ import annotations

import json
import ipaddress
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import dash
from dash import Dash, Input, Output, State, dcc, html
from dash.dash_table import DataTable
from dash.exceptions import PreventUpdate
from flask import Response, abort, request, session as flask_session
import pandas as pd
import plotly.graph_objects as go

# ---------------------------------------------------------------------------
# Pipeline integration paths
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.getenv("MATRES_CONFIG", str(_PROJECT_ROOT / "config" / "config.json")))
if not CONFIG_PATH.is_absolute():
    CONFIG_PATH = (_PROJECT_ROOT / CONFIG_PATH).resolve()
_PIPELINE_SCRIPT = _PROJECT_ROOT / "scripts" / "matres_pipeline.py"
_PIPELINE_PROGRESS_FILE = _PROJECT_ROOT / "data" / "processed" / "pipeline_progress.json"
_DATA_VERSION_FILE = _PROJECT_ROOT / "data" / "processed" / ".data_version"
_PIPELINE_LOG_FILE = _PROJECT_ROOT / "data" / "processed" / "pipeline_output.log"


def _get_git_version() -> str:
    """Return short git commit hash + date for display."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%h (%ci)"],
            cwd=str(_PROJECT_ROOT),
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _read_data_version() -> str:
    """Read the server-side data version (timestamp string)."""
    try:
        return _DATA_VERSION_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _write_data_version() -> str:
    """Write a new data version timestamp. Returns the version string."""
    ts = datetime.now().isoformat()
    _DATA_VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    _DATA_VERSION_FILE.write_text(ts, encoding="utf-8")
    return ts


def _format_data_version_display() -> Tuple[str, bool]:
    """Return (human-readable last-refresh time, refreshed_today) for the header
    badge. When the timestamp is from today the badge is styled as fresh (green);
    otherwise it is styled as stale (amber) so users can tell at a glance whether
    the scheduled daily refresh actually ran."""
    raw = _read_data_version()
    if not raw:
        return ("no data yet", False)
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return (raw, False)
    refreshed_today = dt.date() == datetime.now().date()
    return (dt.strftime("%Y-%m-%d %H:%M"), refreshed_today)


@dataclass
class AppConfig:
    processed_dir: Path
    data_base_dir: Path
    admin_password: str = ""

    @staticmethod
    def load(path: Path) -> "AppConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        processed_dir = Path(raw["processed_dir"])
        if not processed_dir.is_absolute():
            processed_dir = (path.parent.parent / processed_dir).resolve()

        data_base_dir = Path(raw.get("data_base_dir", path.parent.parent))
        if not data_base_dir.is_absolute():
            data_base_dir = (path.parent.parent / data_base_dir).resolve()

        # Priority: env var > config.json (never fall back to a hardcoded default)
        admin_password = os.getenv("MATRES_ADMIN_PASSWORD", "").strip()
        if not admin_password:
            admin_password = raw.get("admin_password", "")
        if not admin_password:
            logging.warning(
                "No admin password configured. Set MATRES_ADMIN_PASSWORD env var "
                "or admin_password in config.json."
            )

        return AppConfig(processed_dir=processed_dir, data_base_dir=data_base_dir, admin_password=admin_password)


def load_dataset(processed_dir: Path, filename: str) -> pd.DataFrame:
    csv_path = processed_dir / filename
    if not csv_path.exists():
        return pd.DataFrame()
    return pd.read_csv(csv_path)


def load_historical_shipment_dataset(cfg: AppConfig) -> pd.DataFrame:
    search_roots: List[Path] = []
    for root in [cfg.data_base_dir, cfg.processed_dir.parent.parent]:
        resolved = Path(root)
        if resolved not in search_roots:
            search_roots.append(resolved)

    candidates: List[Path] = []
    patterns = [
        "Historical Shipment Data_FY2425*.xls*",
        "Historical Shipment Data*.xls*",
    ]
    for root in search_roots:
        for pattern in patterns:
            candidates.extend(
                p for p in root.glob(pattern)
                if not p.name.startswith("~$")
            )

    # de-duplicate same file collected via multiple patterns/roots
    unique_candidates: List[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_candidates.append(candidate)

    if not unique_candidates:
        return pd.DataFrame()

    accessible_candidates: List[Tuple[float, Path]] = []
    for candidate in unique_candidates:
        try:
            accessible_candidates.append((candidate.stat().st_mtime, candidate))
        except (PermissionError, OSError):
            logging.warning("Skipping inaccessible historical shipment file: %s", candidate)

    if not accessible_candidates:
        return pd.DataFrame()

    file_path = max(accessible_candidates, key=lambda item: item[0])[1]
    try:
        raw = pd.read_excel(file_path, sheet_name="Sheet1", header=None)
    except Exception:
        logging.exception("Failed to read historical shipment file: %s", file_path)
        return pd.DataFrame()

    if raw.empty or raw.shape[0] < 2 or raw.shape[1] < 2:
        return pd.DataFrame()

    month_cols: List[str] = []
    for value in raw.iloc[0, 1:].tolist():
        ts = pd.to_datetime(str(value), errors="coerce")
        if pd.notna(ts):
            month_cols.append(ts.strftime("%Y-%m"))
        else:
            month_cols.append(str(value).strip())

    rows: List[Dict[str, Any]] = []
    for row_idx in range(1, min(20, len(raw))):
        label = str(raw.iloc[row_idx, 0]).strip().lower()
        if "base" in label:
            bucket = "Base"
        elif "promotion" in label or "pp" in label:
            bucket = "Promotion"
        elif "total" in label:
            bucket = "Total"
        else:
            continue

        row: Dict[str, Any] = {"Prod Line AS": bucket}
        for col_idx, month in enumerate(month_cols, start=1):
            row[month] = pd.to_numeric(raw.iloc[row_idx, col_idx], errors="coerce")
        rows.append(row)

    if not rows:
        return pd.DataFrame()

    hist = pd.DataFrame(rows).drop_duplicates(subset=["Prod Line AS"], keep="first")
    if "Total" not in hist["Prod Line AS"].tolist():
        month_keys = [c for c in hist.columns if re.fullmatch(r"\d{4}-\d{2}", str(c))]
        if month_keys:
            total_values = hist[hist["Prod Line AS"].isin(["Base", "Promotion"])][month_keys].sum(axis=0)
            total_row = {"Prod Line AS": "Total", **{m: total_values.get(m, 0) for m in month_keys}}
            hist = pd.concat([hist, pd.DataFrame([total_row])], ignore_index=True)
    return hist


# ---------------------------------------------------------------------------
# Refresh group definitions – each group maps to the data keys it covers.
# "all" reloads everything (same as the old full refresh).
# ---------------------------------------------------------------------------
REFRESH_GROUPS: Dict[str, Dict[str, str]] = {
    "all": {
        "label": "All Data",
        "description": "Reload everything",
    },
    "demand": {
        "label": "Demand (HC IDP)",
        "description": "hc_idp_monthly + historical_shipment",
    },
    "supply": {
        "label": "Supply Protection (MR)",
        "description": "monthly_item / requester / level1 / pde / details",
    },
    "td": {
        "label": "TD Validation",
        "description": "td_validation",
    },
    "production": {
        "label": "Production Data",
        "description": "production_data + production_data_by_level",
    },
}

# Which data‐bundle keys belong to which refresh group
_REFRESH_GROUP_KEYS: Dict[str, List[str]] = {
    "demand": ["hc_idp_monthly", "historical_shipment"],
    "supply": [
        "monthly_item",
        "monthly_requester",
        "monthly_level1",
        "pde_alerts",
    ],
    "td": ["td_validation"],
    "production": ["production_data", "production_data_by_level", "production_data_weekly", "production_data_by_level_weekly", "td_demand_by_dimension", "production_version_compare"],
}


def _load_single_key(cfg: AppConfig, key: str) -> Any:
    """Load a single data-bundle key from disk and return list-of-dicts."""
    loaders: Dict[str, Any] = {
        "monthly_item": lambda: load_dataset(cfg.processed_dir, "monthly_msu_by_item_text.csv"),
        "monthly_requester": lambda: load_dataset(cfg.processed_dir, "monthly_msu_by_requester_item.csv"),
        "monthly_level1": lambda: load_dataset(cfg.processed_dir, "monthly_msu_by_level1.csv"),
        "hc_idp_monthly": lambda: load_dataset(cfg.processed_dir, "hc_idp_monthly_summary.csv"),
        "production_data": lambda: load_dataset(cfg.processed_dir, "production_data_summary.csv"),
        "production_data_by_level": lambda: load_dataset(cfg.processed_dir, "production_data_summary_by_level.csv"),
        "production_data_weekly": lambda: load_dataset(cfg.processed_dir, "production_data_summary_weekly.csv"),
        "production_data_by_level_weekly": lambda: load_dataset(cfg.processed_dir, "production_data_summary_by_level_weekly.csv"),
        "td_demand_by_dimension": lambda: load_dataset(cfg.processed_dir, "td_demand_by_dimension.csv"),
        "production_version_compare": lambda: load_dataset(cfg.processed_dir, "production_version_comparison.csv"),
        "td_validation": lambda: load_dataset(cfg.processed_dir, "td_version_monthly_comparison.csv"),
        "historical_shipment": lambda: load_historical_shipment_dataset(cfg),
        "pde_alerts": lambda: load_dataset(cfg.processed_dir, "pde_alerts.csv"),
    }
    loader = loaders.get(key)
    if loader is None:
        return []
    return loader().to_dict("records")


def load_data_bundle(cfg: AppConfig) -> Dict[str, Any]:
    bundle: Dict[str, Any] = {}
    for group_keys in _REFRESH_GROUP_KEYS.values():
        for key in group_keys:
            bundle[key] = _load_single_key(cfg, key)

    request_details_path = cfg.processed_dir / "matres_request_details.csv"
    bundle["request_details_version"] = (
        request_details_path.stat().st_mtime if request_details_path.exists() else None
    )
    return bundle


def load_data_bundle_partial(
    cfg: AppConfig, existing: Dict[str, Any], group: str
) -> Dict[str, Any]:
    """Merge only the keys belonging to *group* into *existing*, return new bundle."""
    if group == "all" or group not in _REFRESH_GROUP_KEYS:
        return load_data_bundle(cfg)

    merged = dict(existing)
    for key in _REFRESH_GROUP_KEYS[group]:
        merged[key] = _load_single_key(cfg, key)

    # Always refresh request_details_version
    request_details_path = cfg.processed_dir / "matres_request_details.csv"
    merged["request_details_version"] = (
        request_details_path.stat().st_mtime if request_details_path.exists() else None
    )
    return merged


# ---------------------------------------------------------------------------
# Pipeline subprocess helpers
# ---------------------------------------------------------------------------

# Cooldown: minimum seconds between two pipeline triggers
_PIPELINE_COOLDOWN_SECONDS = 60
_last_pipeline_trigger: float = 0.0


def _start_pipeline_subprocess(group: str) -> str:
    """Launch matres_pipeline.py in a detached subprocess.

    *group* is one of the REFRESH_GROUPS keys (``all``, ``demand``, etc.).
    Progress is written to ``_PIPELINE_PROGRESS_FILE`` by the pipeline.
    Output is redirected to ``pipeline_output.log`` for debugging.

    Returns empty string on success, or an error message string if blocked.
    """
    global _last_pipeline_trigger

    # --- Guard 1: validate group against allow-list ---
    if group not in REFRESH_GROUPS:
        logging.warning("Blocked pipeline trigger with invalid group: %s", group)
        return f"Invalid refresh scope: {group}"

    # --- Guard 2: cooldown ---
    import time as _time
    now = _time.time()
    elapsed = now - _last_pipeline_trigger
    if elapsed < _PIPELINE_COOLDOWN_SECONDS:
        remaining = int(_PIPELINE_COOLDOWN_SECONDS - elapsed)
        logging.info("Pipeline trigger blocked by cooldown (%ds remaining).", remaining)
        return f"Please wait {remaining}s before triggering again."

    _last_pipeline_trigger = now

    # Clear any stale progress file from a previous run so the first poll does
    # not read an old "completed" state before this subprocess writes its own
    # "running" status (otherwise the progress bar jumps to 100% instantly and
    # the first trigger appears to fail).
    try:
        _PIPELINE_PROGRESS_FILE.unlink(missing_ok=True)
    except OSError:
        logging.warning("Could not remove stale progress file: %s", _PIPELINE_PROGRESS_FILE)

    cmd = [sys.executable, str(_PIPELINE_SCRIPT), "--progress-file", str(_PIPELINE_PROGRESS_FILE)]
    if group and group != "all":
        cmd += ["--stages", group]
    logging.info("Starting pipeline subprocess: %s", " ".join(cmd))
    _PIPELINE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(_PIPELINE_LOG_FILE, "w", encoding="utf-8")  # noqa: SIM115
    subprocess.Popen(
        cmd,
        cwd=str(_PROJECT_ROOT),
        stdout=log_fh,
        stderr=log_fh,
    )
    return ""


def _read_pipeline_progress() -> Optional[Dict[str, Any]]:
    """Read current pipeline progress from the JSON file."""
    try:
        if _PIPELINE_PROGRESS_FILE.exists():
            return json.loads(_PIPELINE_PROGRESS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pass
    return None


# ---------------------------------------------------------------------------
# Daily auto-refresh scheduler
# ---------------------------------------------------------------------------

# Local time the daily pipeline refresh runs (24h clock).
_DAILY_REFRESH_HOUR = 9
_DAILY_REFRESH_MINUTE = 0
_daily_scheduler_started = False


def _run_pipeline_and_notify() -> None:
    """Trigger the full pipeline and notify browsers once it completes."""
    err = _start_pipeline_subprocess("all")
    if err:
        logging.warning("Scheduled daily refresh skipped: %s", err)
        return
    logging.info("Scheduled daily refresh started.")
    import time as _time

    for _ in range(1800):  # wait up to ~30 minutes for completion
        _time.sleep(1)
        progress = _read_pipeline_progress()
        if progress and progress.get("status") in ("completed", "error"):
            if progress["status"] == "completed":
                logging.info("Scheduled daily refresh completed. Writing data version.")
                _write_data_version()
            else:
                logging.warning(
                    "Scheduled daily refresh finished with error: %s",
                    progress.get("error_message"),
                )
            return
    logging.warning("Scheduled daily refresh monitor timed out after 1800s.")


def _start_daily_scheduler() -> None:
    """Start a background thread that runs the pipeline once per day at the
    configured local time (default 09:00)."""
    global _daily_scheduler_started
    if _daily_scheduler_started:
        return
    _daily_scheduler_started = True

    import threading as _threading
    import time as _time

    def _loop():
        while True:
            now = datetime.now()
            target = now.replace(
                hour=_DAILY_REFRESH_HOUR,
                minute=_DAILY_REFRESH_MINUTE,
                second=0,
                microsecond=0,
            )
            if target <= now:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            logging.info(
                "Daily auto-refresh scheduled for %s (in %.0f min).",
                target.strftime("%Y-%m-%d %H:%M"),
                wait_seconds / 60,
            )
            _time.sleep(wait_seconds)
            try:
                _run_pipeline_and_notify()
            except Exception:
                logging.exception("Scheduled daily refresh failed unexpectedly.")

    _threading.Thread(target=_loop, daemon=True, name="daily-refresh").start()



REQUEST_DETAILS_CACHE: Dict[str, Any] = {"mtime": None, "data": pd.DataFrame()}


def load_request_details(cfg: AppConfig) -> pd.DataFrame:
    csv_path = cfg.processed_dir / "matres_request_details.csv"
    if not csv_path.exists():
        REQUEST_DETAILS_CACHE["mtime"] = None
        REQUEST_DETAILS_CACHE["data"] = pd.DataFrame()
        return pd.DataFrame()

    current_mtime = csv_path.stat().st_mtime
    cached_mtime = REQUEST_DETAILS_CACHE.get("mtime")
    cached_df = REQUEST_DETAILS_CACHE.get("data")
    if cached_df is not None and cached_mtime == current_mtime:
        return cached_df.copy()

    fresh_df = pd.read_csv(csv_path)
    REQUEST_DETAILS_CACHE["mtime"] = current_mtime
    REQUEST_DETAILS_CACHE["data"] = fresh_df
    return fresh_df.copy()


UNKNOWN_ROLE = "Others"
GLOBAL_FONT_FAMILY = "Century Gothic, Segoe UI, sans-serif"
ROLE_DISPLAY_MAP = {
    "CSP": "On Going",
    "IOL": "NI",
}

PDE_STYLE_HEADER = {
    "backgroundColor": "#eef4fb",
    "color": "#1f3b6d",
    "border": "1px solid #d6e2f0",
    "fontWeight": "600",
    "fontFamily": GLOBAL_FONT_FAMILY,
    "fontSize": "16px",
}
PDE_STYLE_CELL = {
    "backgroundColor": "#ffffff",
    "color": "#1f2937",
    "border": "1px solid #e2e8f0",
    "textAlign": "center",
    "fontFamily": GLOBAL_FONT_FAMILY,
    "fontSize": "15px",
}
PDE_STYLE_DATA_CONDITIONAL = [
    {"if": {"row_index": "odd"}, "backgroundColor": "#f8fbff"},
    {"if": {"state": "active"}, "backgroundColor": "#e8f1ff", "border": "1px solid #93c5fd"},
    {"if": {"state": "selected"}, "backgroundColor": "#dbeafe", "border": "1px solid #60a5fa"},
]
TOTAL_LABEL = "Total"
ROLE_ALL_VALUE = "ALL"
DETAIL_VIEW_FIELDS = [
    "Material Number",
    "Material Description",
    "Plant",
    "MRP Element Indicator",
    "Item Text",
    "Availability Date",
    "Quantity",
    "Request Date",
    "Requester Email",
    "Reservation No",
    "Deletion Remark",
    "Upload Date",
    "DeleteDate",
    "MSU",
    "Type",
    "Active in System",
    "PDE Checking",
    "Rolling Checking",
]


def compute_metrics(
    monthly_item: pd.DataFrame,
    pde_alerts: pd.DataFrame,
    request_details: Optional[pd.DataFrame] = None,
) -> Dict[str, str]:
    total_msu = monthly_item["total_msu"].sum() if not monthly_item.empty else 0
    if not pde_alerts.empty:
        if "msu_due" in pde_alerts.columns:
            pde_open = pde_alerts["msu_due"].sum(min_count=1)
        elif "open_items" in pde_alerts.columns:
            pde_open = pde_alerts["open_items"].sum(min_count=1)
        else:
            pde_open = 0
    else:
        pde_open = 0

    pde_actual = 0
    if request_details is not None and not request_details.empty:
        if "PDE Checking" in request_details.columns and "MSU" in request_details.columns:
            pde_working = request_details[["PDE Checking", "MSU"]].copy()
            pde_working["PDE Checking"] = pd.to_numeric(pde_working["PDE Checking"], errors="coerce")
            pde_working["MSU"] = pd.to_numeric(pde_working["MSU"], errors="coerce").fillna(0.0)
            pde_actual = pde_working.loc[pde_working["PDE Checking"].le(0), "MSU"].sum(min_count=1)
            if pd.isna(pde_actual):
                pde_actual = 0

    return {
        "total_msu": f"{total_msu:,.0f}",
        "pde_actual": f"{pde_actual:,.0f}",
        "pde_open": f"{pde_open:,.0f}",
    }


def sort_month_labels(labels: List[str]) -> List[str]:
    def sort_key(value: str):
        try:
            return (0, pd.Period(value).to_timestamp())
        except Exception:
            return (1, value)

    return sorted({label for label in labels if isinstance(label, str)}, key=sort_key)


def sort_date_labels(labels: List[str]) -> List[str]:
    def sort_key(value: str):
        try:
            return (0, pd.to_datetime(value))
        except Exception:
            return (1, value)

    return sorted({label for label in labels if isinstance(label, str)}, key=sort_key)


def format_month_label(label: str) -> str:
    """Render month labels in canonical YYYY-MM across charts."""
    try:
        period = pd.Period(label)
        return f"{period.year}-{period.month:02d}"
    except Exception:
        return str(label)


def format_month_label_slash(label: str) -> str:
    """Render month labels into canonical YYYY-MM for table headers."""
    try:
        period = pd.Period(label)
        return f"{period.year}-{period.month:02d}"
    except Exception:
        return str(label)


def _build_matrix_tooltip_data(
    columns: List[Dict[str, str]],
    data: List[Dict[str, Any]],
    request_details: Optional[pd.DataFrame] = None,
) -> List[Dict[str, Any]]:
    """Build tooltip_data for role-item-table showing project name, owner, and MSU."""
    tooltip_rows: List[Dict[str, Any]] = []
    month_col_ids = [c["id"] for c in columns if c["id"] not in ("Role", "Item Text", TOTAL_LABEL)]

    # Build a lookup: (role_raw, item_text, month) -> list of (project, owner, msu)
    detail_lookup: Dict[Tuple[str, str, str], List[Tuple[str, str, float]]] = {}
    if request_details is not None and not request_details.empty:
        rd = request_details.copy()
        rd["requester_role"] = rd.get("requester_role", UNKNOWN_ROLE).fillna(UNKNOWN_ROLE)
        rd["Item Text"] = rd.get("Item Text", "").fillna("").astype(str).str.strip()
        rd["MRP Element Indicator"] = rd.get("MRP Element Indicator", "").fillna("").astype(str).str.strip()
        rd["Requester Email"] = rd.get("Requester Email", "").fillna("").astype(str).str.strip()
        rd["MSU"] = pd.to_numeric(rd.get("MSU", 0), errors="coerce").fillna(0)
        if "availability_month" not in rd.columns:
            rd["availability_month"] = pd.to_datetime(rd.get("Availability Date", pd.NaT), errors="coerce").dt.to_period("M").astype(str)
        rd["availability_month"] = rd["availability_month"].astype(str).str.strip()
        # Map raw role to display role for matching
        for _, row in rd.iterrows():
            role_raw = str(row["requester_role"]).strip()
            role_display = ROLE_DISPLAY_MAP.get(role_raw, role_raw)
            item = str(row["Item Text"]).strip()
            month = str(row["availability_month"]).strip()
            project = str(row["MRP Element Indicator"]).strip() or "N/A"
            owner = str(row["Requester Email"]).strip() or "N/A"
            msu = float(row["MSU"])
            key = (role_display, item, month)
            if key not in detail_lookup:
                detail_lookup[key] = []
            detail_lookup[key].append((project, owner, msu))

    # Track the current role for rows where Role is empty (continuation rows)
    current_role = ""
    for row in data:
        role_val = str(row.get("Role", "")).strip()
        if role_val and role_val != TOTAL_LABEL:
            current_role = role_val
        item_val = str(row.get("Item Text", "")).strip()
        tip_row: Dict[str, Any] = {}
        tip_row["Role"] = {"value": f"**{current_role}**", "type": "markdown"}
        tip_row["Item Text"] = {"value": f"**{item_val}**", "type": "markdown"}
        for col_id in month_col_ids:
            cell_val = str(row.get(col_id, "-"))
            col_name = next((c["name"] for c in columns if c["id"] == col_id), col_id)
            details = detail_lookup.get((current_role, item_val, col_id), [])
            if details:
                # Aggregate by project
                proj_agg: Dict[str, Tuple[set, float]] = {}
                for proj, owner, msu in details:
                    if proj not in proj_agg:
                        proj_agg[proj] = (set(), 0.0)
                    proj_agg[proj][0].add(owner)
                    proj_agg[proj] = (proj_agg[proj][0], proj_agg[proj][1] + msu)
                lines = [f"**{current_role}** | {item_val} | {col_name}: **{cell_val}** MSU", ""]
                lines.append("| Project | Owner | MSU |")
                lines.append("|---|---|---|")
                for proj, (owners, total_msu) in sorted(proj_agg.items(), key=lambda x: -x[1][1]):
                    owner_str = ", ".join(sorted(owners))
                    lines.append(f"| {proj} | {owner_str} | {total_msu:,.1f} |")
                tip_row[col_id] = {"value": "\n".join(lines[:15]), "type": "markdown"}
            else:
                tip_row[col_id] = {
                    "value": f"**{current_role}** | {item_val}  \n{col_name}: **{cell_val}** MSU",
                    "type": "markdown",
                }
        total_val = str(row.get(TOTAL_LABEL, "-"))
        tip_row[TOTAL_LABEL] = {
            "value": f"**{current_role}** | {item_val}  \nTotal: **{total_val}** MSU",
            "type": "markdown",
        }
        tooltip_rows.append(tip_row)
    return tooltip_rows


def _build_pde_tooltip_data(
    columns: List[Dict[str, str]],
    data: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build tooltip_data for PDE alert tables in the same markdown style as summary table."""
    if not columns or not data:
        return []

    col_names = {str(col.get("id", "")): str(col.get("name", col.get("id", ""))) for col in columns}
    date_col_ids = [
        str(col.get("id", ""))
        for col in columns
        if str(col.get("id", "")) not in ("Requester Email", "Project", TOTAL_LABEL)
    ]

    tooltip_rows: List[Dict[str, Any]] = []
    for row in data:
        requester = str(row.get("Requester Email", "")).strip() or "N/A"
        project = str(row.get("Project", "")).strip() or "N/A"
        total = str(row.get(TOTAL_LABEL, "-")).strip() or "-"
        row_header = f"**{requester}** | {project}"

        def _read_project_breakdown(detail_key: str) -> List[Tuple[str, str, float]]:
            raw = row.get(detail_key, "")
            if not raw:
                return []
            try:
                parsed = json.loads(str(raw))
            except Exception:
                return []
            details: List[Tuple[str, str, float]] = []
            for entry in parsed if isinstance(parsed, list) else []:
                if not isinstance(entry, dict):
                    continue
                proj = str(entry.get("project", "")).strip() or "N/A"
                owner = str(entry.get("owner", "")).strip() or requester
                msu_val = pd.to_numeric(entry.get("msu", 0), errors="coerce")
                if pd.notna(msu_val) and float(msu_val) != 0:
                    details.append((proj, owner, float(msu_val)))
            return sorted(details, key=lambda x: -x[2])

        total_breakdown = _read_project_breakdown(f"__detail__{TOTAL_LABEL}")
        total_detail_rows = ["| Project | Owner | MSU |", "|---|---|---|"]
        if total_breakdown:
            for proj, owner, msu_val in total_breakdown[:12]:
                total_detail_rows.append(f"| {proj} | {owner} | {msu_val:,.1f} |")
        else:
            total_detail_rows.append("| N/A | N/A | - |")

        tip_row: Dict[str, Any] = {
            "Requester Email": {
                "value": "\n".join([
                    f"{row_header} | {TOTAL_LABEL}: **{total}** MSU",
                    "",
                    *total_detail_rows,
                ]),
                "type": "markdown",
            },
            "Project": {
                "value": "\n".join([
                    row_header,
                    "",
                    f"{TOTAL_LABEL}: **{total}** MSU",
                    "",
                    *total_detail_rows,
                ]),
                "type": "markdown",
            },
            TOTAL_LABEL: {
                "value": "\n".join([
                    f"{row_header} | {TOTAL_LABEL}: **{total}** MSU",
                    "",
                    *total_detail_rows,
                ]),
                "type": "markdown",
            },
        }

        for col_id in date_col_ids:
            value = str(row.get(col_id, "-")).strip() or "-"
            col_label = col_names.get(col_id, col_id)
            per_date_breakdown = _read_project_breakdown(f"__detail__{col_id}")
            date_detail_rows = ["| Project | Owner | MSU |", "|---|---|---|"]
            if per_date_breakdown:
                for proj, owner, msu_val in per_date_breakdown[:12]:
                    date_detail_rows.append(f"| {proj} | {owner} | {msu_val:,.1f} |")
            else:
                date_detail_rows.append("| N/A | N/A | - |")
            tip_row[col_id] = {
                "value": "\n".join([
                    f"{row_header} | {col_label}: **{value}** MSU",
                    "",
                    *date_detail_rows,
                ]),
                "type": "markdown",
            }

        tooltip_rows.append(tip_row)

    return tooltip_rows


def _sort_pde_records_keep_total_last(
    records: List[Dict[str, Any]],
    sort_by: Optional[List[Dict[str, str]]],
) -> List[Dict[str, Any]]:
    """Sort PDE rows while pinning the Total row at the bottom."""
    if not records:
        return []

    all_records = list(records)
    total_rows = [
        row for row in all_records
        if str(row.get("Requester Email", "")).strip() == TOTAL_LABEL
    ]
    body_rows = [
        row for row in all_records
        if str(row.get("Requester Email", "")).strip() != TOTAL_LABEL
    ]
    if not body_rows:
        return total_rows

    sort_rule = (sort_by or [{"column_id": TOTAL_LABEL, "direction": "desc"}])[0]
    sort_col = str(sort_rule.get("column_id", TOTAL_LABEL))
    sort_desc = str(sort_rule.get("direction", "desc")).lower() == "desc"

    body_df = pd.DataFrame(body_rows)
    if sort_col not in body_df.columns:
        return body_rows + total_rows

    def parse_numeric(value: Any) -> float:
        text = str(value).replace(",", "").strip()
        if text in {"", "-", "nan", "None"}:
            return float("nan")
        return pd.to_numeric(text, errors="coerce")

    body_df["__sort_num"] = body_df[sort_col].apply(parse_numeric)
    body_df["__is_num"] = body_df["__sort_num"].notna().astype(int)
    body_df["__sort_txt"] = body_df[sort_col].astype(str).str.lower()
    body_df = body_df.sort_values(
        by=["__is_num", "__sort_num", "__sort_txt"],
        ascending=[False, not sort_desc, True],
        kind="mergesort",
    )
    sorted_body = body_df.drop(columns=["__sort_num", "__is_num", "__sort_txt"]).to_dict("records")
    return sorted_body + total_rows


def build_monthly_matrix(df: pd.DataFrame, role: str) -> Tuple[List[Dict], List[Dict]]:
    columns = [
        {"name": "Role", "id": "Role"},
        {"name": "Item Text", "id": "Item Text"},
    ]
    if df.empty:
        columns.append({"name": TOTAL_LABEL, "id": TOTAL_LABEL})
        return columns, []

    working = df.copy()
    working["requester_role"] = working["requester_role"].fillna(UNKNOWN_ROLE)
    working["item_label"] = working["Item Text"].fillna("").astype(str).str.strip()
    working.loc[
        working["item_label"].str.lower().isin(["", "nan", "none"]),
        "item_label",
    ] = "Other"
    working["total_msu"] = pd.to_numeric(working.get("total_msu", 0), errors="coerce").fillna(0.0)

    working["availability_month_norm"] = working["availability_month"].astype(str).str.strip()
    working.loc[
        working["availability_month_norm"].str.lower().isin(["", "nan", "none", "nat", "unknown", "unknow"]),
        "availability_month_norm",
    ] = pd.NA

    months = sort_month_labels(working["availability_month_norm"].dropna().tolist())
    if months:
        month_totals = (
            working.dropna(subset=["availability_month_norm"])
            .groupby("availability_month_norm", dropna=False)["total_msu"]
            .sum(min_count=1)
        )
        filtered_months: List[str] = []
        for month in months:
            month_total = pd.to_numeric(month_totals.get(month, 0), errors="coerce")
            if pd.notna(month_total) and float(month_total) > 0:
                filtered_months.append(month)
        months = filtered_months
    if not months:
        columns.append({"name": TOTAL_LABEL, "id": TOTAL_LABEL})
        return columns, []

    for month in months:
        columns.append({"name": format_month_label_slash(month), "id": month})
    columns.append({"name": TOTAL_LABEL, "id": TOTAL_LABEL})

    preferred_items = ["R Material", "RM Material", "R Quotation", "FG Rolling"]
    item_totals = working.groupby("item_label", dropna=False)["total_msu"].sum(min_count=1)
    unique_items = []
    for item in preferred_items:
        item_total = pd.to_numeric(item_totals.get(item, 0), errors="coerce")
        if item in working["item_label"].unique() and pd.notna(item_total) and float(item_total) > 0:
            unique_items.append(item)
    sorted_items = sorted(
        {
            str(item).strip()
            for item in working["item_label"].unique()
            if pd.notna(pd.to_numeric(item_totals.get(str(item).strip(), 0), errors="coerce"))
            and float(pd.to_numeric(item_totals.get(str(item).strip(), 0), errors="coerce")) > 0
        },
        key=lambda x: x.lower(),
    )
    remaining_items = [item for item in sorted_items if item not in unique_items]
    item_order = unique_items + remaining_items

    role_series = working["requester_role"]
    seen_roles: List[str] = []
    for val in role_series:
        if val not in seen_roles:
            seen_roles.append(val)

    role_totals = (
        working.groupby("requester_role", dropna=False)["total_msu"].sum(min_count=1)
        if "total_msu" in working.columns
        else pd.Series(dtype=float)
    )

    def role_has_content(role_name: str) -> bool:
        if role_name not in role_totals.index:
            return False
        value = role_totals.get(role_name, 0)
        if pd.isna(value):
            return False
        return float(value) > 0

    if role and role != ROLE_ALL_VALUE:
        role_order = [role] if role_has_content(role) else []
    else:
        preferred_roles = ["IOL", "CSP", "CROSS REGION", UNKNOWN_ROLE]
        role_order = [r for r in preferred_roles if r in seen_roles and role_has_content(r)]
        role_order.extend(r for r in seen_roles if r not in role_order and role_has_content(r))

    scope_df = working if not role or role == ROLE_ALL_VALUE else working[working["requester_role"] == role]
    records: List[Dict] = []

    def format_value(value: float) -> str:
        if pd.isna(value) or value == 0:
            return "-"
        return f"{value:,.0f}"

    def display_role(role_name: str) -> str:
        normalized = str(role_name).strip()
        return ROLE_DISPLAY_MAP.get(normalized, normalized)

    for current_role in role_order:
        role_subset = working[working["requester_role"] == current_role]
        if role_subset.empty:
            pivot = pd.DataFrame(0, index=item_order, columns=months)
        else:
            grouped = (
                role_subset.groupby(["item_label", "availability_month_norm"], dropna=False)["total_msu"]
                .sum(min_count=1)
                .reset_index()
            )
            pivot = (
                grouped.pivot_table(
                    index="item_label",
                    columns="availability_month_norm",
                    values="total_msu",
                    aggfunc="sum",
                    fill_value=0,
                )
                .reindex(index=item_order, fill_value=0)
                .reindex(columns=months, fill_value=0)
            )

        role_has_rows = False
        for item_name in item_order:
            row = {
                "Role": display_role(current_role) if not role_has_rows else "",
                "Item Text": item_name,
            }
            row_values = pivot.loc[item_name] if item_name in pivot.index else pd.Series(0, index=months)
            row_total = row_values.sum()
            if pd.isna(row_total) or float(row_total) <= 0:
                continue
            for month in months:
                row[month] = format_value(row_values.get(month, 0))
            row[TOTAL_LABEL] = format_value(row_total)
            records.append(row)
            role_has_rows = True

        # Add role subtotal row
        if role_has_rows:
            role_subtotal_label = f"{display_role(current_role)} Total"
            role_month_totals = (
                role_subset.groupby("availability_month_norm", dropna=False)["total_msu"]
                .sum(min_count=1)
                if not role_subset.empty
                else pd.Series(dtype=float)
            )
            role_total_value = role_month_totals.sum() if not role_month_totals.empty else 0
            if pd.notna(role_total_value) and float(role_total_value) > 0:
                subtotal_record: Dict[str, Any] = {"Role": "", "Item Text": role_subtotal_label}
                for month in months:
                    subtotal_record[month] = format_value(role_month_totals.get(month, 0))
                subtotal_record[TOTAL_LABEL] = format_value(role_total_value)
                records.append(subtotal_record)

    totals = (
        scope_df.groupby("availability_month_norm", dropna=False)["total_msu"].sum(min_count=1)
        if not scope_df.empty
        else pd.Series(dtype=float)
    )

    category_records: List[Dict[str, Any]] = []
    if not scope_df.empty:
        material_mask = scope_df["item_label"].astype(str).str.strip().str.lower().isin(["r material", "rm material"])
        category_defs = [
            ("Total FG", ~material_mask),
            ("Total material", material_mask),
        ]
        for category_name, category_mask in category_defs:
            category_df = scope_df[category_mask].copy()
            if category_df.empty:
                continue
            category_totals = category_df.groupby("availability_month_norm", dropna=False)["total_msu"].sum(min_count=1)
            category_total_value = category_totals.sum() if not category_totals.empty else 0
            if pd.isna(category_total_value) or float(category_total_value) <= 0:
                continue

            category_record = {"Role": TOTAL_LABEL, "Item Text": category_name}
            for month in months:
                category_record[month] = format_value(category_totals.get(month, 0))
            category_record[TOTAL_LABEL] = format_value(category_total_value)
            category_records.append(category_record)

    total_record = {"Role": TOTAL_LABEL, "Item Text": "Total"}
    total_value = totals.sum() if not totals.empty else 0
    if pd.notna(total_value) and float(total_value) > 0:
        for month in months:
            total_record[month] = format_value(totals.get(month, 0))
        total_record[TOTAL_LABEL] = format_value(total_value)
        records.append(total_record)

    records.extend(category_records)

    return columns, records


def build_item_summary(df: pd.DataFrame, role: str) -> Tuple[List[Dict], List[Dict]]:
    columns = [{"name": "Item Text", "id": "Item Text"}]
    if df.empty:
        columns.append({"name": TOTAL_LABEL, "id": TOTAL_LABEL})
        return columns, []

    scope_df = df.copy()
    scope_df["Item Text"] = scope_df["Item Text"].fillna("").astype(str).str.strip()
    scope_df.loc[
        scope_df["Item Text"].str.lower().isin(["", "nan", "none"]),
        "Item Text",
    ] = "Other"
    scope_df["total_msu"] = pd.to_numeric(scope_df.get("total_msu", 0), errors="coerce").fillna(0.0)
    scope_df["availability_month_norm"] = scope_df["availability_month"].astype(str).str.strip()
    scope_df.loc[
        scope_df["availability_month_norm"].str.lower().isin(["", "nan", "none", "nat", "unknown", "unknow"]),
        "availability_month_norm",
    ] = pd.NA
    if role and role != ROLE_ALL_VALUE:
        scope_df = scope_df[scope_df["requester_role"] == role]

    months = sort_month_labels(scope_df["availability_month_norm"].dropna().tolist())
    if months:
        month_totals = (
            scope_df.dropna(subset=["availability_month_norm"])
            .groupby("availability_month_norm", dropna=False)["total_msu"]
            .sum(min_count=1)
        )
        filtered_months: List[str] = []
        for month in months:
            month_total = pd.to_numeric(month_totals.get(month, 0), errors="coerce")
            if pd.notna(month_total) and float(month_total) > 0:
                filtered_months.append(month)
        months = filtered_months
    for month in months:
        columns.append({"name": format_month_label_slash(month), "id": month})
    columns.append({"name": TOTAL_LABEL, "id": TOTAL_LABEL})

    if scope_df.empty:
        placeholder = {"Item Text": TOTAL_LABEL}
        for month in months:
            placeholder[month] = "-"
        placeholder[TOTAL_LABEL] = "-"
        return columns, [placeholder]

    preferred_items = ["R Material", "RM Material", "R Quotation", "FG Rolling"]
    item_totals = scope_df.groupby("Item Text", dropna=False)["total_msu"].sum(min_count=1)
    unique_items = []
    for item in preferred_items:
        item_total = pd.to_numeric(item_totals.get(item, 0), errors="coerce")
        if item in scope_df["Item Text"].unique() and pd.notna(item_total) and float(item_total) > 0:
            unique_items.append(item)
    sorted_items = sorted(
        {
            str(item).strip()
            for item in scope_df["Item Text"].unique()
            if pd.notna(pd.to_numeric(item_totals.get(str(item).strip(), 0), errors="coerce"))
            and float(pd.to_numeric(item_totals.get(str(item).strip(), 0), errors="coerce")) > 0
        },
        key=lambda x: x.lower(),
    )
    remaining_items = [item for item in sorted_items if item not in unique_items]
    item_order = unique_items + remaining_items
    if not item_order:
        item_order = ["未定义"]

    grouped = (
        scope_df.groupby(["Item Text", "availability_month_norm"], dropna=False)["total_msu"]
        .sum(min_count=1)
        .reset_index()
    )
    if grouped.empty:
        pivot = pd.DataFrame(0, index=item_order, columns=months)
    else:
        pivot = (
            grouped.pivot_table(
                index="Item Text",
                columns="availability_month_norm",
                values="total_msu",
                aggfunc="sum",
                fill_value=0,
            )
            .reindex(index=item_order, fill_value=0)
            .reindex(columns=months, fill_value=0)
        )

    def format_value(value: float) -> str:
        if pd.isna(value) or value == 0:
            return "-"
        return f"{value:,.0f}"

    records: List[Dict] = []
    for item_name in item_order:
        row = {"Item Text": item_name}
        row_values = pivot.loc[item_name] if item_name in pivot.index else pd.Series(0, index=months)
        row_total = row_values.sum()
        if pd.isna(row_total) or float(row_total) <= 0:
            continue
        for month in months:
            row[month] = format_value(row_values.get(month, 0))
        row[TOTAL_LABEL] = format_value(row_total)
        records.append(row)

    totals = (
        scope_df.groupby("availability_month_norm", dropna=False)["total_msu"].sum(min_count=1)
        if not scope_df.empty
        else pd.Series(dtype=float)
    )
    total_record = {"Item Text": TOTAL_LABEL}
    total_value = totals.sum() if not totals.empty else 0
    if pd.notna(total_value) and float(total_value) > 0:
        for month in months:
            total_record[month] = format_value(totals.get(month, 0))
        total_record[TOTAL_LABEL] = format_value(total_value)
        records.append(total_record)

    return columns, records


def build_first_level_summary(
    df: pd.DataFrame,
    source_level_column: str = "First Level",
    display_level_column: str = "Level 1",
    include_levels: List[str] | None = None,
) -> Tuple[List[Dict], List[Dict]]:
    unmapped_labels = {"未映射", "unmapped"}
    display_zero_threshold = 0.5

    columns = [{"name": display_level_column, "id": display_level_column}]
    if (
        df.empty
        or "availability_month" not in df.columns
        or "total_msu" not in df.columns
        or source_level_column not in df.columns
    ):
        columns.append({"name": TOTAL_LABEL, "id": TOTAL_LABEL})
        return columns, []

    working = df.copy()
    working[source_level_column] = working[source_level_column].astype(str).str.strip()
    working["availability_month"] = working["availability_month"].astype(str)
    working["total_msu"] = pd.to_numeric(working["total_msu"], errors="coerce")

    if include_levels:
        include_level_keys = {str(level).strip().lower() for level in include_levels if str(level).strip()}
        working = working[
            working[source_level_column].astype(str).str.strip().str.lower().isin(include_level_keys)
        ].copy()

    if working.empty:
        columns.append({"name": TOTAL_LABEL, "id": TOTAL_LABEL})
        return columns, []

    def normalize_month(value: str) -> str:
        try:
            period = pd.Period(str(value))
            return str(period)
        except Exception:
            try:
                ts = pd.to_datetime(str(value), errors="coerce")
                if pd.notna(ts):
                    return str(ts.to_period("M"))
            except Exception:
                pass
        return str(value)

    working["availability_month_norm"] = working["availability_month"].apply(normalize_month)
    months_norm = sort_month_labels(working["availability_month_norm"].dropna().tolist())

    def month_display(value: str) -> str:
        try:
            period = pd.Period(value)
            return f"{period.year}-{period.month:02d}"
        except Exception:
            return str(value)

    month_map = {m: month_display(m) for m in months_norm}
    months_display = [month_map[m] for m in months_norm]

    for month in months_display:
        columns.append({"name": month, "id": month})
    columns.append({"name": TOTAL_LABEL, "id": TOTAL_LABEL})

    if not months_norm:
        return columns, []

    grouped = (
        working.groupby([source_level_column, "availability_month_norm"], dropna=False)["total_msu"]
        .sum(min_count=1)
        .reset_index()
    )

    pivot = (
        grouped.pivot_table(
            index=source_level_column,
            columns="availability_month_norm",
            values="total_msu",
            aggfunc="sum",
            fill_value=0,
        )
        .reindex(columns=months_norm, fill_value=0)
    )

    pivot = pivot.rename(columns=month_map)

    pivot[TOTAL_LABEL] = pivot.sum(axis=1)

    if not pivot.empty:
        keep_mask = pd.Series(True, index=pivot.index)
        for idx in pivot.index:
            level_name = str(idx).strip().lower()
            if level_name in {label.lower() for label in unmapped_labels}:
                if abs(float(pivot.loc[idx, TOTAL_LABEL])) < display_zero_threshold:
                    keep_mask.loc[idx] = False
        pivot = pivot.loc[keep_mask]

    pivot = pivot[pivot[TOTAL_LABEL] > 0]
    pivot = pivot.sort_values(TOTAL_LABEL, ascending=False)

    totals_by_month = pivot[months_display].sum(axis=0)
    total_value = float(pivot[TOTAL_LABEL].sum()) if not pivot.empty else 0

    def fmt(value: float) -> str:
        if pd.isna(value) or value == 0:
            return "-"
        return f"{value:,.0f}"

    records: List[Dict] = []
    for level_name, row in pivot.iterrows():
        record = {display_level_column: level_name}
        for month in months_display:
            record[month] = fmt(row.get(month, 0))
        record[TOTAL_LABEL] = fmt(row.get(TOTAL_LABEL, 0))
        records.append(record)

    total_record = {display_level_column: TOTAL_LABEL}
    if pd.notna(total_value) and float(total_value) > 0:
        for month in months_display:
            total_record[month] = fmt(totals_by_month.get(month, 0))
        total_record[TOTAL_LABEL] = fmt(total_value)
        records.append(total_record)

    return columns, records


def build_pde_matrix(df: pd.DataFrame) -> Tuple[List[Dict], List[Dict]]:
    if df.empty:
        columns = [
            {"name": "Requester", "id": "Requester Email"},
            {"name": "Project", "id": "Project"},
            {"name": "Availability Date", "id": "无数据"},
        ]
        return columns, []

    working = df.copy()
    working["availability_date"] = pd.to_datetime(working["availability_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    if "project_label" not in working.columns:
        working["project_label"] = "未定义"
    working["project_label"] = working["project_label"].fillna("未定义")

    date_labels = sort_date_labels(working["availability_date"].dropna().tolist())

    # Build per-requester per-date project-owner breakdown for tooltip details.
    project_detail_map: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    if "detail_json" in working.columns:
        for _, rec in working.iterrows():
            requester = str(rec.get("Requester Email", "")).strip()
            date_label = str(rec.get("availability_date", "")).strip()
            raw = rec.get("detail_json", "")
            details: List[Dict[str, Any]] = []
            if raw:
                try:
                    parsed = json.loads(str(raw))
                except Exception:
                    parsed = []
                if isinstance(parsed, list):
                    for item in parsed:
                        if not isinstance(item, dict):
                            continue
                        proj = str(item.get("project", "")).strip() or "未定义"
                        owner = str(item.get("owner", requester)).strip() or requester
                        msu_val = pd.to_numeric(item.get("msu", 0), errors="coerce")
                        if pd.notna(msu_val) and float(msu_val) != 0:
                            details.append({"project": proj, "owner": owner, "msu": float(msu_val)})
            project_detail_map[(requester, date_label)] = sorted(details, key=lambda x: -float(x.get("msu", 0)))
    else:
        detail_frame = working[["Requester Email", "availability_date", "project_label", "msu_due"]].copy()
        detail_frame["msu_due"] = pd.to_numeric(detail_frame["msu_due"], errors="coerce").fillna(0.0)
        detail_grouped = (
            detail_frame.groupby(["Requester Email", "availability_date", "project_label"], dropna=False)["msu_due"]
            .sum(min_count=1)
            .reset_index(name="msu_due")
        )
        for (requester, date_label), grp in detail_grouped.groupby(["Requester Email", "availability_date"], dropna=False):
            items: List[Dict[str, Any]] = []
            for _, rec in grp.sort_values("msu_due", ascending=False).iterrows():
                proj = str(rec.get("project_label", "")).strip() or "N/A"
                msu_val = pd.to_numeric(rec.get("msu_due", 0), errors="coerce")
                if pd.notna(msu_val) and float(msu_val) != 0:
                    items.append({"project": proj, "owner": str(requester), "msu": float(msu_val)})
            project_detail_map[(str(requester), str(date_label))] = items

    pivot = (
        working.pivot_table(
            index="Requester Email",
            columns="availability_date",
            values="msu_due",
            aggfunc="sum",
            fill_value=0,
        )
        .reindex(columns=date_labels)
        .sort_index()
    )

    pivot[TOTAL_LABEL] = pivot.sum(axis=1)
    total_row = pivot.sum(axis=0).to_frame().T
    total_row.index = [TOTAL_LABEL]
    pivot = pd.concat([pivot, total_row])

    def combine_project(values: pd.Series) -> str:
        cleaned = sorted({str(v).strip() for v in values if isinstance(v, str) and v.strip()})
        return " / ".join(cleaned) if cleaned else "未定义"

    project_map = (
        working.groupby("Requester Email")["project_label"].agg(combine_project).to_dict()
    )
    project_map[TOTAL_LABEL] = "-"

    columns = [
        {"name": "Requester", "id": "Requester Email"},
        {"name": "Project", "id": "Project"},
    ]
    for label in date_labels:
        columns.append({"name": label, "id": label})
    columns.append({"name": TOTAL_LABEL, "id": TOTAL_LABEL})

    records: List[Dict] = []

    def fmt(value: Any) -> str:
        numeric = pd.to_numeric(value, errors="coerce")
        if pd.isna(numeric) or float(numeric) == 0.0:
            return "-"
        return f"{float(numeric):,.1f}"

    for requester, row in pivot.iterrows():
        record = {
            "Requester Email": requester,
            "Project": project_map.get(requester, "未定义"),
        }
        total_project_owner_agg: Dict[Tuple[str, str], float] = {}
        for label in date_labels + [TOTAL_LABEL]:
            value = row.get(label, 0)
            record[label] = fmt(value)
            if label != TOTAL_LABEL:
                date_details = project_detail_map.get((str(requester), str(label)), [])
                record[f"__detail__{label}"] = json.dumps(date_details, ensure_ascii=False)
                for entry in date_details:
                    proj = str(entry.get("project", "N/A"))
                    owner = str(entry.get("owner", str(requester)))
                    key = (proj, owner)
                    total_project_owner_agg[key] = total_project_owner_agg.get(key, 0.0) + float(entry.get("msu", 0.0))

        total_details = [
            {"project": proj, "owner": owner, "msu": float(msu)}
            for (proj, owner), msu in sorted(total_project_owner_agg.items(), key=lambda x: -x[1])
            if float(msu) != 0
        ]
        record[f"__detail__{TOTAL_LABEL}"] = json.dumps(total_details, ensure_ascii=False)
        records.append(record)

    return columns, records


def summarize_pde_alerts_from_details(df: pd.DataFrame, fg_only: bool) -> pd.DataFrame:
    output_columns = [
        "Requester Email",
        "availability_date",
        "availability_month",
        "msu_due",
        "open_items",
        "max_pde",
        "avg_pde",
        "closest_availability",
        "project_label",
        "detail_json",
        "requester_role",
    ]
    required_columns = ["PDE Checking", "Availability Date", "MSU", "Requester Email", "Item Text"]
    if df.empty or any(col not in df.columns for col in required_columns):
        return pd.DataFrame(columns=output_columns)

    working = df.copy()
    working["Item Text"] = working["Item Text"].fillna("").astype(str).str.strip()
    item_text_key = working["Item Text"].str.lower()
    if fg_only:
        working = working[item_text_key == "fg rolling"].copy()
    else:
        working = working[item_text_key != "fg rolling"].copy()

    if working.empty:
        return pd.DataFrame(columns=output_columns)

    working["PDE Checking"] = pd.to_numeric(working["PDE Checking"], errors="coerce")
    working = working[working["PDE Checking"].notna()].copy()
    if working.empty:
        return pd.DataFrame(columns=output_columns)

    working["Availability Date"] = pd.to_datetime(working["Availability Date"], errors="coerce")
    working = working[working["Availability Date"].notna()].copy()
    if working.empty:
        return pd.DataFrame(columns=output_columns)

    working["MSU"] = pd.to_numeric(working.get("MSU", 0), errors="coerce").fillna(0.0)
    if "MRP Element Indicator" not in working.columns:
        working["MRP Element Indicator"] = ""
    if "requester_role" not in working.columns:
        working["requester_role"] = UNKNOWN_ROLE
    working["requester_role"] = working["requester_role"].fillna(UNKNOWN_ROLE)

    def combine_project(values: pd.Series) -> str:
        cleaned = sorted({str(v).strip() for v in values if pd.notna(v) and str(v).strip()})
        return " / ".join(cleaned) if cleaned else "未定义"

    working["availability_date"] = working["Availability Date"].dt.date

    detail_by_project = (
        working.assign(
            __project=working["MRP Element Indicator"].fillna("").astype(str).str.strip(),
            __owner=working["Requester Email"].fillna("").astype(str).str.strip(),
        )
        .groupby(["Requester Email", "availability_date", "__project", "__owner"], dropna=False)["MSU"]
        .sum(min_count=1)
        .reset_index(name="msu")
    )
    detail_map: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for (requester, date_val), grp in detail_by_project.groupby(["Requester Email", "availability_date"], dropna=False):
        rows: List[Dict[str, Any]] = []
        for _, rec in grp.sort_values("msu", ascending=False).iterrows():
            proj = str(rec.get("__project", "")).strip() or "未定义"
            owner = str(rec.get("__owner", "")).strip() or "N/A"
            msu_val = pd.to_numeric(rec.get("msu", 0), errors="coerce")
            if pd.notna(msu_val) and float(msu_val) != 0:
                rows.append({"project": proj, "owner": owner, "msu": float(msu_val)})
        detail_map[(str(requester), str(date_val))] = rows

    summary = (
        working.groupby(["Requester Email", "availability_date"], dropna=False)
        .agg(
            msu_due=("MSU", "sum"),
            open_items=("PDE Checking", "count"),
            max_pde=("PDE Checking", "max"),
            avg_pde=("PDE Checking", "mean"),
            closest_availability=("Availability Date", "min"),
            project_label=("MRP Element Indicator", combine_project),
            requester_role=("requester_role", "first"),
        )
        .reset_index()
    )

    summary["avg_pde"] = summary["avg_pde"].round(1)
    summary["msu_due"] = summary["msu_due"].round(2)
    summary["availability_date"] = summary["availability_date"].astype(str)
    summary["availability_month"] = (
        pd.to_datetime(summary["availability_date"], errors="coerce")
        .dt.to_period("M")
        .astype(str)
    )
    summary["closest_availability"] = summary["closest_availability"].dt.strftime("%Y-%m-%d")
    summary["detail_json"] = summary.apply(
        lambda row: json.dumps(
            detail_map.get((str(row.get("Requester Email", "")), str(row.get("availability_date", ""))), []),
            ensure_ascii=False,
        ),
        axis=1,
    )

    return summary.sort_values(["closest_availability", "max_pde"], ascending=[True, False])


def build_pde_tables(
    pde_alerts_df: pd.DataFrame,
    request_details_df: pd.DataFrame,
) -> Tuple[List[Dict], List[Dict], List[Dict], List[Dict]]:
    if not request_details_df.empty and "Item Text" in request_details_df.columns:
        normal_summary = summarize_pde_alerts_from_details(request_details_df, fg_only=False)
        fg_summary = summarize_pde_alerts_from_details(request_details_df, fg_only=True)
        normal_columns, normal_records = build_pde_matrix(normal_summary)
        fg_columns, fg_records = build_pde_matrix(fg_summary)
        return normal_columns, normal_records, fg_columns, fg_records

    normal_columns, normal_records = build_pde_matrix(pde_alerts_df)
    fg_columns, fg_records = build_pde_matrix(pd.DataFrame())
    return normal_columns, normal_records, fg_columns, fg_records


def build_hc_idp_monthly_table(df: pd.DataFrame, as_percent: bool = False) -> Tuple[List[Dict], List[Dict]]:
    if df.empty:
        return [{"name": "Prod Line", "id": "Prod Line"}, {"name": "Overall", "id": "Overall Result"}], []

    working = df.copy()
    if "Prod Line" in working.columns:
        pass
    elif "Prod Line AS" in working.columns:
        working = working.rename(columns={"Prod Line AS": "Prod Line"})
    else:
        first_col = working.columns[0]
        working = working.rename(columns={first_col: "Prod Line"})

    value_cols = [col for col in working.columns if col != "Prod Line"]
    for col in value_cols:
        working[col] = pd.to_numeric(working[col], errors="coerce")

    def format_value(value: float) -> str:
        if pd.isna(value) or value == 0:
            return "-"
        if as_percent:
            return f"{float(value):,.1f}%"
        return f"{float(value):,.0f}"

    for col in value_cols:
        working[col] = working[col].apply(format_value)

    columns = [
        {"name": ("Overall" if str(col) == "Overall Result" else str(col)), "id": str(col)}
        for col in working.columns
    ]
    return columns, working.to_dict("records")


def build_production_data_table(df: pd.DataFrame) -> Tuple[List[Dict], List[Dict]]:
    base_cols = ["Plant", "Level1", "Level2", "MTD", "Left Production", "Current Month Total"]
    if df.empty:
        columns = [{"name": col, "id": col} for col in base_cols]
        return columns, []

    working = df.copy()
    value_cols = ["MTD", "Left Production", "Current Month Total"]
    source_value_cols = value_cols
    source_month_suffix = ""

    for col in base_cols:
        if col not in working.columns:
            working[col] = ""

    month_cols = sorted([c for c in working.columns if re.fullmatch(r"\d{4}-\d{2}", str(c))])
    ordered_cols = base_cols + month_cols

    for display_col, source_col in zip(value_cols, source_value_cols):
        if source_col in working.columns:
            working[display_col] = working[source_col]
        elif display_col not in working.columns:
            working[display_col] = 0.0

    for month in month_cols:
        source_month_col = f"{month}{source_month_suffix}"
        if source_month_col in working.columns:
            working[month] = working[source_month_col]
        elif month not in working.columns:
            working[month] = 0.0

    numeric_cols = ["MTD", "Left Production", "Current Month Total", *month_cols]
    for col in numeric_cols:
        working[col] = pd.to_numeric(working[col], errors="coerce").fillna(0.0)

    working = working[working[numeric_cols].abs().sum(axis=1) > 0].copy()

    for col in numeric_cols:
        working[col] = working[col].apply(lambda v: "-" if pd.isna(v) or float(v) == 0.0 else f"{float(v):,.0f}")

    for col in ["Plant", "Level1", "Level2"]:
        working[col] = working[col].fillna("").astype(str).str.strip()

    columns = [{"name": col, "id": col} for col in ordered_cols]
    return columns, working[ordered_cols].to_dict("records")


def build_production_data_table_no_level2(df: pd.DataFrame) -> Tuple[List[Dict], List[Dict]]:
    base_cols = ["Plant", "Level1", "MTD", "Left Production", "Current Month Total"]
    if df.empty:
        columns = [{"name": col, "id": col} for col in base_cols]
        return columns, []

    working = df.copy()
    month_cols = sorted([c for c in working.columns if re.fullmatch(r"\d{4}-\d{2}", str(c))])
    numeric_cols = ["MTD", "Left Production", "Current Month Total", *month_cols]
    numeric_cols_msu = [f"{col}_MSU" for col in numeric_cols]
    all_numeric_cols = [col for col in [*numeric_cols, *numeric_cols_msu] if col in working.columns]

    for col in ["Plant", "Level1"]:
        if col not in working.columns:
            working[col] = ""
        working[col] = working[col].fillna("").astype(str).str.strip()

    for col in all_numeric_cols:
        working[col] = pd.to_numeric(working[col], errors="coerce").fillna(0.0)

    grouped = (
        working.groupby(["Plant", "Level1"], dropna=False)[all_numeric_cols]
        .sum(min_count=1)
        .reset_index()
    )

    factory_alias_map = {
        "C810": "0386",
        "D352": "1864",
        "A673": "A868",
    }
    grouped["Plant"] = grouped["Plant"].astype(str).str.strip()
    grouped["Level1"] = grouped["Level1"].astype(str).str.strip()
    grouped["factory_group"] = grouped["Plant"].map(lambda v: factory_alias_map.get(str(v).strip(), str(v).strip()))

    source_suffix = ""
    for col in numeric_cols:
        source_col = f"{col}{source_suffix}"
        if source_col in grouped.columns:
            grouped[col] = grouped[source_col]
        elif col not in grouped.columns:
            grouped[col] = 0.0

    for col in numeric_cols:
        grouped[col] = pd.to_numeric(grouped[col], errors="coerce").fillna(0.0)

    grouped = grouped[grouped[numeric_cols].abs().sum(axis=1) > 0].copy()
    level1_priority = {
        "base": 0,
        "pp": 1,
        "hktw": 2,
        "export": 3,
        "ess": 4,
    }
    grouped["__level1_order"] = grouped["Level1"].astype(str).str.strip().str.lower().map(level1_priority).fillna(9)
    grouped["__plant_primary_order"] = (grouped["Plant"] != grouped["factory_group"]).astype(int)
    grouped = grouped.sort_values(
        ["factory_group", "__plant_primary_order", "Plant", "__level1_order", "Level1"],
        ascending=[True, True, True, True, True],
    ).reset_index(drop=True)

    ordered_cols = base_cols + month_cols
    result_rows: List[Dict[str, Any]] = []
    for factory, block in grouped.groupby("factory_group", dropna=False, sort=False):
        block = block.copy()
        for _, row in block.iterrows():
            record: Dict[str, Any] = {
                "Plant": str(row.get("Plant", "")).strip(),
                "Level1": str(row.get("Level1", "")).strip(),
                "__factory_group": str(factory).strip(),
                "__row_type": "detail",
                "__level1_key": str(row.get("Level1", "")).strip(),
            }
            for col in numeric_cols:
                value = pd.to_numeric(row.get(col), errors="coerce")
                record[col] = "-" if pd.isna(value) or float(value) == 0.0 else f"{float(value):,.0f}"
            result_rows.append(record)

        total_level_priority = {
            "pp": 0,
            "base": 1,
            "hktw": 2,
            "export": 3,
            "ess": 4,
        }
        level_totals = (
            block.groupby("Level1", dropna=False)[numeric_cols]
            .sum(min_count=1)
            .reset_index()
        )
        level_totals["__level_order"] = (
            level_totals["Level1"].astype(str).str.strip().str.lower().map(total_level_priority).fillna(9)
        )
        level_totals = level_totals.sort_values(["__level_order", "Level1"], ascending=[True, True]).reset_index(drop=True)

        for _, total_row in level_totals.iterrows():
            level1_value = str(total_row.get("Level1", "")).strip()
            subtotal_record: Dict[str, Any] = {
                "Plant": f"Total-{level1_value}",
                "Level1": "",
                "__factory_group": str(factory).strip(),
                "__row_type": "total_level1",
                "__level1_key": level1_value,
            }
            for col in numeric_cols:
                total_val = pd.to_numeric(total_row.get(col), errors="coerce")
                subtotal_record[col] = "-" if pd.isna(total_val) or float(total_val) == 0.0 else f"{float(total_val):,.0f}"
            result_rows.append(subtotal_record)

        total_record: Dict[str, Any] = {
            "Plant": "Total-ALL",
            "Level1": "",
            "__factory_group": str(factory).strip(),
            "__row_type": "total_all",
            "__level1_key": "",
        }
        for col in numeric_cols:
            total_val = pd.to_numeric(block[col], errors="coerce").fillna(0.0).sum(min_count=1)
            total_record[col] = "-" if pd.isna(total_val) or float(total_val) == 0.0 else f"{float(total_val):,.0f}"
        result_rows.append(total_record)

    columns = [{"name": col, "id": col} for col in ordered_cols]
    return columns, result_rows


def build_production_data_table_by_plant(
    df: pd.DataFrame,
    plant_order: Optional[List[str]] = None,
    include_segment_totals: bool = True,
    segment_totals_after: Optional[Dict[str, Tuple[str, List[str]]]] = None,
) -> Tuple[List[Dict], List[Dict]]:
    base_cols = ["Plant", "MTD", "Left Production", "Current Month"]
    if df.empty:
        columns = [{"name": col, "id": col} for col in base_cols]
        return columns, []

    working = df.copy()
    source_suffix = ""

    month_cols = sorted([c for c in working.columns if re.fullmatch(r"\d{4}-\d{2}", str(c))])
    numeric_cols = ["MTD", "Left Production", "Current Month Total", *month_cols]

    if "Plant" not in working.columns:
        working["Plant"] = ""
    working["Plant"] = working["Plant"].fillna("").astype(str).str.strip()

    for col in numeric_cols:
        source_col = f"{col}{source_suffix}"
        if source_col in working.columns:
            working[col] = pd.to_numeric(working[source_col], errors="coerce").fillna(0.0)
        elif col in working.columns:
            working[col] = pd.to_numeric(working[col], errors="coerce").fillna(0.0)
        else:
            working[col] = 0.0

    grouped = (
        working.groupby(["Plant"], dropna=False)[numeric_cols]
        .sum(min_count=1)
        .reset_index()
    )

    grouped = grouped[grouped["Plant"] != ""].copy()
    if grouped.empty:
        columns = [{"name": col, "id": col} for col in base_cols]
        return columns, []

    grouped = grouped[grouped[numeric_cols].abs().sum(axis=1) > 0].copy()

    display_month_cols = month_cols[1:] if month_cols else []
    grouped["Current Month"] = pd.to_numeric(grouped["Current Month Total"], errors="coerce").fillna(0.0)

    # Only keep months that have non-zero data across all plants
    display_month_cols = [
        c for c in display_month_cols
        if pd.to_numeric(grouped[c], errors="coerce").fillna(0.0).abs().sum() > 0
    ]

    ordered_cols = ["Plant", "MTD", "Left Production", "Current Month", *display_month_cols]

    total_numeric_cols = ["MTD", "Left Production", "Current Month", *display_month_cols]
    grouped = grouped[ordered_cols].copy()

    if plant_order:
        allowed_plants = {str(p).strip() for p in plant_order}
        grouped = grouped[grouped["Plant"].astype(str).str.strip().isin(allowed_plants)].copy()
        if grouped.empty:
            columns = [{"name": col, "id": col} for col in ordered_cols]
            return columns, []

    def sum_for_plants(plants: List[str]) -> pd.Series:
        subset = grouped[grouped["Plant"].isin(plants)]
        if subset.empty:
            return pd.Series({col: 0.0 for col in total_numeric_cols})
        return subset[total_numeric_cols].apply(pd.to_numeric, errors="coerce").fillna(0.0).sum(min_count=1)

    ordered_rows: List[Dict[str, Any]] = []

    def append_row(label: str, plants: List[str]) -> None:
        summed = sum_for_plants(plants)
        row: Dict[str, Any] = {"Plant": label}
        for col in total_numeric_cols:
            row[col] = float(summed.get(col, 0.0))
        ordered_rows.append(row)

    if plant_order:
        for plant in plant_order:
            append_row(str(plant).strip(), [str(plant).strip()])
    else:
        append_row("0386", ["0386"])
        append_row("C810", ["C810"])
        append_row("A868", ["A868"])
        append_row("A673", ["A673"])
        append_row("1864", ["1864"])
        append_row("D352", ["D352"])

    if include_segment_totals:
        if plant_order and segment_totals_after:
            inserted_labels = set()
            reordered_rows: List[Dict[str, Any]] = []
            for plant in plant_order:
                plant_label = str(plant).strip()
                matched_rows = [row for row in ordered_rows if str(row.get("Plant", "")).strip() == plant_label]
                reordered_rows.extend(matched_rows)
                total_spec = segment_totals_after.get(plant_label)
                if total_spec:
                    total_label, total_plants = total_spec
                    if total_label not in inserted_labels:
                        append_row(total_label, total_plants)
                        inserted_labels.add(total_label)
                        reordered_rows.append(ordered_rows[-1])
            ordered_rows = reordered_rows
        else:
            append_row("HP Total", ["0386", "C810"])
            append_row("TC Total", ["A868", "A673"])
            append_row("XQ Total", ["1864", "D352"])
    append_row("GC Total", grouped["Plant"].astype(str).str.strip().tolist())

    grouped = pd.DataFrame(ordered_rows)

    for col in total_numeric_cols:
        grouped[col] = grouped[col].apply(lambda v: "-" if pd.isna(v) or float(v) == 0.0 else f"{float(v):,.0f}")

    columns = [{"name": col, "id": col} for col in ordered_cols]
    return columns, grouped[ordered_cols].to_dict("records")


def build_production_version_comparison_table(
    df: pd.DataFrame,
) -> Tuple[List[Dict], List[Dict]]:
    """Format the production version comparison DataFrame for DataTable display.

    Returns (columns, rows) with numeric values kept as integers so that
    DataTable filter_query comparisons (< 0, > 0) work for Gap styling.
    """
    base_cols = ["Version", "Version Group", "Plant"]
    if df.empty:
        return [{"name": c, "id": c} for c in base_cols], []

    working = df.copy()

    # Fix NaN in Version column from CSV roundtrip
    working["Version"] = working["Version"].fillna("").astype(str)
    working["Version"] = working["Version"].replace("nan", "")
    working["Version Group"] = working["Version Group"].fillna("").astype(str)
    working["Version Group"] = working["Version Group"].replace("nan", "")
    working["Plant"] = working["Plant"].fillna("").astype(str)
    working["Plant"] = working["Plant"].replace("nan", "")

    # Discover month columns and skip the first one (same logic as Table 1)
    month_cols = sorted(
        [c for c in working.columns if re.fullmatch(r"\d{4}-\d{2}", str(c))]
    )
    display_month_cols = month_cols[1:] if month_cols else []

    # Rename Current Month Total → Current Month for display
    if "Current Month Total" in working.columns:
        working = working.rename(columns={"Current Month Total": "Current Month"})

    numeric_display = ["MTD", "Left Production", "Current Month"] + display_month_cols
    ordered_cols = ["Version", "Version Group", "Plant"] + numeric_display

    # Keep only available columns
    ordered_cols = [c for c in ordered_cols if c in working.columns]

    # Convert numeric columns: spacer rows stay as empty string, others become int
    for col in numeric_display:
        if col not in working.columns:
            continue
        working[col] = working[col].apply(
            lambda v: (
                ""
                if (isinstance(v, str) and v.strip() == "")
                or (isinstance(v, float) and pd.isna(v))
                else int(round(float(v)))
            )
        )

    # Build columns with text columns + numeric columns
    columns: List[Dict] = []
    for c in ordered_cols:
        if c in numeric_display:
            columns.append({"name": c, "id": c, "type": "numeric"})
        else:
            columns.append({"name": c, "id": c})

    return columns, working[ordered_cols].to_dict("records")


def build_production_version_style_data_conditional(
    columns: List[Dict],
) -> List[Dict]:
    """Build conditional style rules for the production version comparison table."""
    rules: List[Dict] = list(PDE_STYLE_DATA_CONDITIONAL)

    numeric_cols = [
        col.get("id")
        for col in columns
        if col.get("id") in ("MTD", "Left Production", "Current Month")
        or re.fullmatch(r"\d{4}-\d{2}", str(col.get("id", "")))
    ]

    for col in numeric_cols:
        rules.append(
            {
                "if": {
                    "filter_query": f'{{Version Group}} = "Gap" && {{{col}}} < 0',
                    "column_id": col,
                },
                "color": "#dc2626",
                "fontWeight": "700",
            }
        )
        rules.append(
            {
                "if": {
                    "filter_query": f'{{Version Group}} = "Gap" && {{{col}}} > 0',
                    "column_id": col,
                },
                "color": "#16a34a",
                "fontWeight": "700",
            }
        )

    rules.append(
        {
            "if": {"filter_query": '{Plant} = "GC Total"'},
            "backgroundColor": "#eaf2ff",
            "fontWeight": "700",
        }
    )
    return rules


def build_production_data_table_by_plant_level(
    df: pd.DataFrame,
    plant_order: Optional[List[str]] = None,
    include_segment_totals: bool = True,
    segment_totals_after: Optional[Dict[str, Tuple[str, List[str]]]] = None,
) -> Tuple[List[Dict], List[Dict]]:
    base_cols = ["Plant", "Level1", "Level2", "MTD", "Left Production", "Current Month"]
    if df.empty:
        columns = [{"name": col, "id": col} for col in base_cols]
        return columns, []

    working = df.copy()
    for col in base_cols:
        if col not in working.columns:
            working[col] = ""
        working[col] = working[col].fillna("").astype(str).str.strip()

    month_cols = sorted([c for c in working.columns if re.fullmatch(r"\d{4}-\d{2}", str(c))])
    display_month_cols = month_cols[1:] if month_cols else []
    numeric_cols = ["MTD", "Left Production", "Current Month Total", *display_month_cols]

    for col in numeric_cols:
        if col in working.columns:
            working[col] = pd.to_numeric(working[col], errors="coerce").fillna(0.0)
        else:
            working[col] = 0.0

    grouped = (
        working.groupby(["Plant", "Level1", "Level2"], dropna=False)[numeric_cols]
        .sum(min_count=1)
        .reset_index()
    )

    grouped["Current Month"] = pd.to_numeric(grouped["Current Month Total"], errors="coerce").fillna(0.0)

    # Only keep months that have non-zero data across all rows
    display_month_cols = [
        c for c in display_month_cols
        if pd.to_numeric(grouped[c], errors="coerce").fillna(0.0).abs().sum() > 0
    ]

    grouped = grouped[["Plant", "Level1", "Level2", "MTD", "Left Production", "Current Month", *display_month_cols]].copy()

    ordered_cols = ["Plant", "Level1", "Level2", "MTD", "Left Production", "Current Month", *display_month_cols]
    data_numeric_cols = ["MTD", "Left Production", "Current Month", *display_month_cols]

    if data_numeric_cols:
        grouped = grouped[grouped[data_numeric_cols].abs().sum(axis=1) > 0].copy()

    grouped = grouped.sort_values(["Plant", "Level1", "Level2"], ascending=[True, True, True]).reset_index(drop=True)

    if plant_order:
        allowed_plants = {str(p).strip() for p in plant_order}
        grouped = grouped[grouped["Plant"].astype(str).str.strip().isin(allowed_plants)].copy()
        if grouped.empty:
            columns = [{"name": col, "id": col} for col in ordered_cols]
            return columns, []

    result_rows: List[Dict[str, Any]] = []

    def append_plant_detail(plant: str) -> None:
        subset = grouped[grouped["Plant"].astype(str).str.strip() == plant].copy()
        if subset.empty:
            return
        for _, row in subset.iterrows():
            record = {
                "Plant": plant,
                "Level1": str(row.get("Level1", "")).strip(),
                "Level2": str(row.get("Level2", "")).strip(),
            }
            for col in data_numeric_cols:
                record[col] = pd.to_numeric(row.get(col), errors="coerce")
            result_rows.append(record)

    def ensure_empty_plant_row(plant: str) -> None:
        exists = any(str(row.get("Plant", "")).strip() == plant for row in result_rows)
        if exists:
            return
        record: Dict[str, Any] = {"Plant": plant, "Level1": "", "Level2": ""}
        for col in data_numeric_cols:
            record[col] = 0.0
        result_rows.append(record)

    def append_group_total(label: str, plants: List[str]) -> None:
        subset = grouped[grouped["Plant"].astype(str).str.strip().isin(plants)].copy()
        if subset.empty:
            month_sum = {col: 0.0 for col in data_numeric_cols}
        else:
            month_sum = {
                col: pd.to_numeric(subset[col], errors="coerce").fillna(0.0).sum(min_count=1)
                for col in data_numeric_cols
            }
        record = {
            "Plant": label,
            "Level1": "",
            "Level2": "",
            **month_sum,
        }
        result_rows.append(record)

    if plant_order:
        for plant in plant_order:
            plant_key = str(plant).strip()
            append_plant_detail(plant_key)
            ensure_empty_plant_row(plant_key)
            if include_segment_totals and segment_totals_after:
                total_spec = segment_totals_after.get(plant_key)
                if total_spec:
                    total_label, total_plants = total_spec
                    append_group_total(total_label, total_plants)
    else:
        append_plant_detail("0386")
        append_plant_detail("C810")
        ensure_empty_plant_row("C810")
        append_plant_detail("A868")
        append_plant_detail("A673")
        append_plant_detail("1864")
        append_plant_detail("D352")

    if include_segment_totals and not (plant_order and segment_totals_after):
        append_group_total("HP Total", ["0386", "C810"])
        append_group_total("TC Total", ["A868", "A673"])
        append_group_total("XQ Total", ["1864", "D352"])

    all_plants = grouped["Plant"].astype(str).str.strip().unique().tolist()
    append_group_total("GC Total", all_plants)

    result_df = pd.DataFrame(result_rows)
    for col in data_numeric_cols:
        result_df[col] = result_df[col].apply(lambda v: "-" if pd.isna(v) or float(v) == 0.0 else f"{float(v):,.0f}")

    columns = [{"name": col, "id": col} for col in ordered_cols]
    return columns, result_df[ordered_cols].to_dict("records")


# ---------------------------------------------------------------------------
# Weekly Production Data table builders
# ---------------------------------------------------------------------------

def build_production_data_table_by_plant_weekly(
    df: pd.DataFrame,
    plant_order: Optional[List[str]] = None,
    include_segment_totals: bool = True,
    segment_totals_after: Optional[Dict[str, Tuple[str, List[str]]]] = None,
    max_weeks: int = 13,
) -> Tuple[List[Dict], List[Dict]]:
    """Weekly production by plant. Only week columns, limited to max_weeks from current week."""
    base_cols = ["Plant"]
    if df.empty:
        columns = [{"name": col, "id": col} for col in base_cols]
        return columns, []

    working = df.copy()
    all_week_cols = sorted([c for c in working.columns if re.fullmatch(r"\d{4}-W\d{2}", str(c))])

    # Determine current ISO week and keep only future max_weeks weeks
    from datetime import date
    today = date.today()
    current_iso = today.isocalendar()
    current_week_label = f"{current_iso.year}-W{current_iso.week:02d}"
    week_cols = [c for c in all_week_cols if c >= current_week_label][:max_weeks]
    if not week_cols:
        week_cols = all_week_cols[-max_weeks:] if all_week_cols else []

    if not week_cols:
        columns = [{"name": col, "id": col} for col in base_cols]
        return columns, []

    if "Plant" not in working.columns:
        working["Plant"] = ""
    working["Plant"] = working["Plant"].fillna("").astype(str).str.strip()

    for col in week_cols:
        if col in working.columns:
            working[col] = pd.to_numeric(working[col], errors="coerce").fillna(0.0)
        else:
            working[col] = 0.0

    grouped = (
        working.groupby(["Plant"], dropna=False)[week_cols]
        .sum(min_count=1)
        .reset_index()
    )
    grouped = grouped[grouped["Plant"] != ""].copy()
    if grouped.empty:
        columns = [{"name": col, "id": col} for col in ["Plant", *week_cols]]
        return columns, []
    grouped = grouped[grouped[week_cols].abs().sum(axis=1) > 0].copy()

    ordered_cols = ["Plant", *week_cols]
    grouped = grouped[ordered_cols].copy()

    if plant_order:
        allowed_plants = {str(p).strip() for p in plant_order}
        grouped = grouped[grouped["Plant"].astype(str).str.strip().isin(allowed_plants)].copy()
        if grouped.empty:
            columns = [{"name": col, "id": col} for col in ordered_cols]
            return columns, []

    def sum_for_plants(plants: List[str]) -> pd.Series:
        subset = grouped[grouped["Plant"].isin(plants)]
        if subset.empty:
            return pd.Series({col: 0.0 for col in week_cols})
        return subset[week_cols].apply(pd.to_numeric, errors="coerce").fillna(0.0).sum(min_count=1)

    ordered_rows: List[Dict[str, Any]] = []

    def append_row(label: str, plants: List[str]) -> None:
        summed = sum_for_plants(plants)
        row: Dict[str, Any] = {"Plant": label}
        for col in week_cols:
            row[col] = float(summed.get(col, 0.0))
        ordered_rows.append(row)

    if plant_order:
        for plant in plant_order:
            append_row(plant, [plant])
            if include_segment_totals and segment_totals_after and plant in segment_totals_after:
                total_label, total_plants = segment_totals_after[plant]
                append_row(total_label, total_plants)
        append_row("GC Total", list(grouped["Plant"].unique()))
    else:
        for plant in sorted(grouped["Plant"].unique()):
            append_row(plant, [plant])
        append_row("GC Total", list(grouped["Plant"].unique()))

    result_rows: List[Dict[str, Any]] = []
    for row in ordered_rows:
        display_row: Dict[str, Any] = {"Plant": row["Plant"]}
        for col in week_cols:
            val = row.get(col, 0.0)
            display_row[col] = "-" if pd.isna(val) or float(val) == 0.0 else f"{float(val):,.0f}"
        result_rows.append(display_row)

    columns = [{"name": col, "id": col} for col in ordered_cols]
    return columns, result_rows


def build_production_data_table_by_plant_level_weekly(
    df: pd.DataFrame,
    plant_order: Optional[List[str]] = None,
    include_segment_totals: bool = True,
    segment_totals_after: Optional[Dict[str, Tuple[str, List[str]]]] = None,
    max_weeks: int = 13,
) -> Tuple[List[Dict], List[Dict]]:
    """Weekly production by plant/level. Only week columns, limited to max_weeks from current week."""
    base_cols = ["Plant", "Level1", "Level2"]
    if df.empty:
        columns = [{"name": col, "id": col} for col in base_cols]
        return columns, []

    working = df.copy()
    for col in ["Plant", "Level1", "Level2"]:
        if col not in working.columns:
            working[col] = ""
        working[col] = working[col].fillna("").astype(str).str.strip()

    all_week_cols = sorted([c for c in working.columns if re.fullmatch(r"\d{4}-W\d{2}", str(c))])

    # Determine current ISO week and keep only future max_weeks weeks
    from datetime import date
    today = date.today()
    current_iso = today.isocalendar()
    current_week_label = f"{current_iso.year}-W{current_iso.week:02d}"
    week_cols = [c for c in all_week_cols if c >= current_week_label][:max_weeks]
    if not week_cols:
        week_cols = all_week_cols[-max_weeks:] if all_week_cols else []

    if not week_cols:
        columns = [{"name": col, "id": col} for col in base_cols]
        return columns, []

    ordered_cols = ["Plant", "Level1", "Level2", *week_cols]

    for col in week_cols:
        if col in working.columns:
            working[col] = pd.to_numeric(working[col], errors="coerce").fillna(0.0)
        else:
            working[col] = 0.0

    grouped = (
        working.groupby(["Plant", "Level1", "Level2"], dropna=False)[week_cols]
        .sum(min_count=1)
        .reset_index()
    )

    if week_cols:
        grouped = grouped[grouped[week_cols].abs().sum(axis=1) > 0].copy()

    grouped = grouped.sort_values(["Plant", "Level1", "Level2"]).reset_index(drop=True)

    if plant_order:
        allowed_plants = {str(p).strip() for p in plant_order}
        grouped = grouped[grouped["Plant"].astype(str).str.strip().isin(allowed_plants)].copy()
        if grouped.empty:
            columns = [{"name": col, "id": col} for col in ordered_cols]
            return columns, []

    result_rows: List[Dict[str, Any]] = []

    def append_plant_detail(plant: str) -> None:
        subset = grouped[grouped["Plant"].astype(str).str.strip() == plant].copy()
        if subset.empty:
            return
        for _, row in subset.iterrows():
            record = {"Plant": plant, "Level1": str(row.get("Level1", "")).strip(), "Level2": str(row.get("Level2", "")).strip()}
            for col in week_cols:
                record[col] = pd.to_numeric(row.get(col), errors="coerce")
            result_rows.append(record)

    def ensure_empty_plant_row(plant: str) -> None:
        exists = any(str(row.get("Plant", "")).strip() == plant for row in result_rows)
        if exists:
            return
        record: Dict[str, Any] = {"Plant": plant, "Level1": "", "Level2": ""}
        for col in week_cols:
            record[col] = 0.0
        result_rows.append(record)

    def append_group_total(label: str, plants: List[str]) -> None:
        subset = grouped[grouped["Plant"].astype(str).str.strip().isin(plants)].copy()
        if subset.empty:
            wk_sum = {col: 0.0 for col in week_cols}
        else:
            wk_sum = {col: pd.to_numeric(subset[col], errors="coerce").fillna(0.0).sum(min_count=1) for col in week_cols}
        result_rows.append({"Plant": label, "Level1": "", "Level2": "", **wk_sum})

    if plant_order:
        for plant in plant_order:
            append_plant_detail(plant)
            ensure_empty_plant_row(plant)
            if include_segment_totals and segment_totals_after and plant in segment_totals_after:
                total_label, total_plants = segment_totals_after[plant]
                append_group_total(total_label, total_plants)
        append_group_total("GC Total", list(grouped["Plant"].unique()))
    else:
        for plant in sorted(grouped["Plant"].unique()):
            append_plant_detail(plant)
        append_group_total("GC Total", list(grouped["Plant"].unique()))

    result_df = pd.DataFrame(result_rows)
    for col in week_cols:
        result_df[col] = result_df[col].apply(lambda v: "-" if pd.isna(v) or float(v) == 0.0 else f"{float(v):,.0f}")

    columns = [{"name": col, "id": col} for col in ordered_cols]
    return columns, result_df[ordered_cols].to_dict("records")


# ---------------------------------------------------------------------------
# Production Detail by Dimension (Brand / Size / Variant etc.)
# ---------------------------------------------------------------------------

_PROD_DIM_COLS = ["Plant", "Brand", "Lineup", "Size", "Type", "NI/Conversion", "Prod Line", "Variant"]


def build_production_dimension_options(df: pd.DataFrame) -> Dict[str, List[Dict[str, str]]]:
    """Return dropdown options for each dimension column present in the data."""
    options: Dict[str, List[Dict[str, str]]] = {}
    for dim in _PROD_DIM_COLS:
        if dim not in df.columns:
            options[dim] = []
            continue
        unique_values = sorted(df[dim].fillna("").astype(str).str.strip().unique())
        unique_values = [v for v in unique_values if v]
        options[dim] = [{"label": v, "value": v} for v in unique_values]
    return options


def build_production_dimension_table(
    df: pd.DataFrame,
    group_by: List[str],
    filters: Optional[Dict[str, List[str]]] = None,
) -> Tuple[List[Dict], List[Dict]]:
    """Build a production data table grouped by the selected dimensions.

    The source data has the same structure as the production summary tables:
    Plant + dimension columns + MTD / Left Production / Current Month Total + month columns.
    """
    if df.empty or not group_by:
        columns = [{"name": dim, "id": dim} for dim in (group_by or ["Brand"])]
        return columns, []

    working = df.copy()

    # Clean dimension columns
    for dim in _PROD_DIM_COLS:
        if dim in working.columns:
            working[dim] = working[dim].fillna("").astype(str).str.strip()

    # Apply filters
    if filters:
        for dim, selected_values in filters.items():
            if selected_values and dim in working.columns:
                working = working[working[dim].isin(selected_values)].copy()

    if working.empty:
        columns = [{"name": dim, "id": dim} for dim in group_by]
        return columns, []

    # Identify numeric columns: MTD, Left Production, Current Month Total + month cols
    month_cols = sorted([c for c in working.columns if re.fullmatch(r"\d{4}-\d{2}", str(c))])
    # Exclude current month (first month) – it is already covered by
    # "Current Month Total" (= MTD + Left Production).
    if month_cols:
        month_cols = month_cols[1:]
    fixed_numeric = ["MTD", "Left Production", "Current Month Total"]
    numeric_cols = [c for c in fixed_numeric if c in working.columns] + month_cols

    for col in numeric_cols:
        working[col] = pd.to_numeric(working[col], errors="coerce").fillna(0.0)

    # Group by selected dimensions
    valid_group_by = [g for g in group_by if g in working.columns]
    if not valid_group_by:
        valid_group_by = [group_by[0]] if group_by else ["Brand"]
        for g in valid_group_by:
            if g not in working.columns:
                working[g] = ""

    grouped = (
        working.groupby(valid_group_by, dropna=False)[numeric_cols]
        .sum(min_count=1)
        .reset_index()
    )

    # Remove rows where all numeric values are 0
    grouped = grouped[grouped[numeric_cols].abs().sum(axis=1) > 0].copy()

    # Add a Total row
    if not grouped.empty:
        total_row = {dim: "Total" if dim == valid_group_by[0] else "" for dim in valid_group_by}
        for col in numeric_cols:
            total_row[col] = grouped[col].sum()
        grouped = pd.concat([grouped, pd.DataFrame([total_row])], ignore_index=True)

    # Format numeric columns
    for col in numeric_cols:
        grouped[col] = grouped[col].apply(lambda v: "-" if pd.isna(v) or float(v) == 0.0 else f"{float(v):,.0f}")

    ordered_cols = valid_group_by + numeric_cols
    columns = [{"name": col, "id": col} for col in ordered_cols]
    return columns, grouped[ordered_cols].to_dict("records")


def build_demand_hs_dataframe(hc_idp_df: pd.DataFrame, monthly_level1_df: pd.DataFrame) -> pd.DataFrame:
    if hc_idp_df.empty:
        return pd.DataFrame(columns=["Prod Line AS", "Overall Result"])

    lbe = hc_idp_df.copy()
    if "Prod Line AS" not in lbe.columns:
        first_col = lbe.columns[0]
        lbe = lbe.rename(columns={first_col: "Prod Line AS"})

    for col in [c for c in lbe.columns if c != "Prod Line AS"]:
        lbe[col] = pd.to_numeric(lbe[col], errors="coerce").fillna(0)

    month_cols = [
        c for c in lbe.columns
        if re.fullmatch(r"\d{4}-\d{2}", str(c))
    ]
    month_cols = sorted(month_cols)

    if not month_cols:
        return lbe

    base_lbe = lbe.loc[lbe["Prod Line AS"].astype(str).str.lower() == "base", month_cols].sum(axis=0)
    promo_lbe = lbe.loc[lbe["Prod Line AS"].astype(str).str.lower() == "promotion", month_cols].sum(axis=0)

    supply_base = pd.Series(0.0, index=month_cols)
    supply_promo = pd.Series(0.0, index=month_cols)
    if not monthly_level1_df.empty:
        level = monthly_level1_df.copy()
        if "availability_month" in level.columns and "First Level" in level.columns and "total_msu" in level.columns:
            level["availability_month"] = level["availability_month"].astype(str)
            level["total_msu"] = pd.to_numeric(level["total_msu"], errors="coerce").fillna(0)
            level = level[level["availability_month"].isin(month_cols)].copy()
            level_name = level["First Level"].astype(str).str.strip().str.lower()

            base_mask = level_name == "base"
            promo_mask = level_name.isin(["pp", "promotion"])

            supply_base = (
                level.loc[base_mask]
                .groupby("availability_month", dropna=False)["total_msu"]
                .sum(min_count=1)
                .reindex(month_cols, fill_value=0)
            )
            supply_promo = (
                level.loc[promo_mask]
                .groupby("availability_month", dropna=False)["total_msu"]
                .sum(min_count=1)
                .reindex(month_cols, fill_value=0)
            )

    hs_base = base_lbe.add(supply_base, fill_value=0)
    hs_promo = promo_lbe.add(supply_promo, fill_value=0)
    hs_total = hs_base.add(hs_promo, fill_value=0)

    rows = [
        {"Prod Line AS": "Base", **{m: hs_base.get(m, 0) for m in month_cols}},
        {"Prod Line AS": "Promotion", **{m: hs_promo.get(m, 0) for m in month_cols}},
        {"Prod Line AS": "Total", **{m: hs_total.get(m, 0) for m in month_cols}},
    ]
    hs_df = pd.DataFrame(rows)
    hs_df["Overall Result"] = hs_df[month_cols].sum(axis=1)
    return hs_df


def build_demand_hs_table(hc_idp_df: pd.DataFrame, monthly_level1_df: pd.DataFrame) -> Tuple[List[Dict], List[Dict]]:
    hs_df = build_demand_hs_dataframe(hc_idp_df, monthly_level1_df)
    return build_hc_idp_monthly_table(hs_df)


def build_demand_iya_table(current_df: pd.DataFrame, historical_df: pd.DataFrame) -> Tuple[List[Dict], List[Dict]]:
    if current_df.empty or historical_df.empty:
        return [{"name": "Prod Line", "id": "Prod Line"}, {"name": "Overall", "id": "Overall Result"}], []

    current = current_df.copy()
    if "Prod Line AS" not in current.columns:
        first_col = current.columns[0]
        current = current.rename(columns={first_col: "Prod Line AS"})
    for col in [c for c in current.columns if c != "Prod Line AS"]:
        current[col] = pd.to_numeric(current[col], errors="coerce").fillna(0)

    history = historical_df.copy()
    if "Prod Line AS" not in history.columns:
        return [{"name": "Prod Line", "id": "Prod Line"}, {"name": "Overall", "id": "Overall Result"}], []
    for col in [c for c in history.columns if c != "Prod Line AS"]:
        history[col] = pd.to_numeric(history[col], errors="coerce")

    month_cols = sorted([c for c in current.columns if re.fullmatch(r"\d{4}-\d{2}", str(c))])
    if not month_cols:
        return [{"name": "Prod Line", "id": "Prod Line"}, {"name": "Overall", "id": "Overall Result"}], []

    history_lookup: Dict[Tuple[str, str], float] = {}
    for _, row in history.iterrows():
        bucket = str(row.get("Prod Line AS", "")).strip().lower()
        for month in [c for c in history.columns if re.fullmatch(r"\d{4}-\d{2}", str(c))]:
            value = pd.to_numeric(row.get(month), errors="coerce")
            if pd.notna(value):
                history_lookup[(bucket, month)] = float(value)

    rows: List[Dict[str, Any]] = []
    for bucket in ["Base", "Promotion", "Total"]:
        current_row = current[current["Prod Line AS"].astype(str).str.lower() == bucket.lower()]
        if current_row.empty:
            current_values = pd.Series(0.0, index=month_cols)
        else:
            current_values = current_row[month_cols].sum(axis=0)

        row: Dict[str, Any] = {"Prod Line AS": bucket}
        for month in month_cols:
            prev_month = (pd.Period(month, freq="M") - 12).strftime("%Y-%m")
            base = history_lookup.get((bucket.lower(), prev_month))
            if base is None or base == 0:
                row[month] = None
            else:
                row[month] = float(current_values.get(month, 0)) / float(base) * 100.0
        rows.append(row)

    iya_df = pd.DataFrame(rows)
    iya_df["Overall Result"] = iya_df[[m for m in month_cols if m in iya_df.columns]].mean(axis=1, skipna=True)
    return build_hc_idp_monthly_table(iya_df, as_percent=True)


def build_demand_iya_by_quarter_table(
    lbe_df: pd.DataFrame,
    hs_df: pd.DataFrame,
    historical_df: pd.DataFrame,
) -> Tuple[List[Dict], List[Dict]]:
    base_columns = [{"name": "Prod Line", "id": "Prod Line"}]
    if lbe_df.empty or hs_df.empty or historical_df.empty:
        return base_columns, []

    lbe = lbe_df.copy()
    hs = hs_df.copy()
    history = historical_df.copy()

    if "Prod Line AS" not in lbe.columns or "Prod Line AS" not in hs.columns or "Prod Line AS" not in history.columns:
        return base_columns, []

    for frame in [lbe, hs, history]:
        for col in [c for c in frame.columns if c != "Prod Line AS"]:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")

    month_letter = {
        1: "J", 2: "F", 3: "M", 4: "A", 5: "M", 6: "J",
        7: "J", 8: "A", 9: "S", 10: "O", 11: "N", 12: "D",
    }

    current_period = pd.Timestamp.today().to_period("M")
    quarter_start_month = ((current_period.month - 1) // 3) * 3 + 1
    current_quarter_start = pd.Period(f"{current_period.year}-{quarter_start_month:02d}", freq="M")
    quarter_starts = [current_quarter_start, current_quarter_start + 3]

    quarter_specs: List[Dict[str, Any]] = []
    for q_start in quarter_starts:
        quarter_months = [q_start + i for i in range(3)]
        tag = "".join(month_letter.get(p.month, "") for p in quarter_months)
        month_labels = [p.strftime("%Y-%m") for p in quarter_months]
        prev_year_labels = [(p - 12).strftime("%Y-%m") for p in quarter_months]
        quarter_specs.append(
            {
                "tag": tag,
                "month_labels": month_labels,
                "prev_year_labels": prev_year_labels,
                "col_lbe": f"{tag} LBE",
                "col_hs": f"{tag} DSL+SSP",
                "col_lbe_iya": f"{tag} LBE IYA",
                "col_hs_iya": f"{tag} DSL+SSP IYA",
            }
        )

    columns = base_columns.copy()
    for spec in quarter_specs:
        tag = spec["tag"]
        columns.extend(
            [
                {"name": f"{tag} MSU", "id": spec["col_lbe"]},
                {"name": f"{tag} MSU", "id": spec["col_hs"]},
                {"name": f"{tag} IYA", "id": spec["col_lbe_iya"]},
                {"name": f"{tag} IYA", "id": spec["col_hs_iya"]},
            ]
        )

    history_lookup: Dict[Tuple[str, str], float] = {}
    hist_month_cols = [c for c in history.columns if re.fullmatch(r"\d{4}-\d{2}", str(c))]
    for _, row in history.iterrows():
        bucket = str(row.get("Prod Line AS", "")).strip().lower()
        for month in hist_month_cols:
            val = pd.to_numeric(row.get(month), errors="coerce")
            if pd.notna(val):
                history_lookup[(bucket, month)] = float(val)

    def quarter_sum(df: pd.DataFrame, bucket: str, labels: List[str]) -> float:
        data = df[df["Prod Line AS"].astype(str).str.lower() == bucket.lower()]
        if data.empty:
            return 0.0
        present_cols = [m for m in labels if m in data.columns]
        if not present_cols:
            return 0.0
        return float(data[present_cols].fillna(0).sum(axis=1).sum())

    records: List[Dict[str, Any]] = []
    for bucket in ["Base", "Promotion", "Total"]:
        record: Dict[str, Any] = {"Prod Line": bucket}
        for spec in quarter_specs:
            lbe_quarter = quarter_sum(lbe, bucket, spec["month_labels"])
            hs_quarter = quarter_sum(hs, bucket, spec["month_labels"])
            lbe_prev = sum(history_lookup.get((bucket.lower(), month), 0.0) for month in spec["prev_year_labels"])
            hs_prev = lbe_prev

            lbe_iya = (lbe_quarter / lbe_prev * 100.0) if lbe_prev else None
            hs_iya = (hs_quarter / hs_prev * 100.0) if hs_prev else None

            record[spec["col_lbe"]] = f"{lbe_quarter:,.0f}" if lbe_quarter else "-"
            record[spec["col_hs"]] = f"{hs_quarter:,.0f}" if hs_quarter else "-"
            record[spec["col_lbe_iya"]] = f"{lbe_iya:,.1f}%" if lbe_iya is not None else "-"
            record[spec["col_hs_iya"]] = f"{hs_iya:,.1f}%" if hs_iya is not None else "-"

        records.append(record)

    return columns, records


def split_quarter_iya_tables(
    columns: List[Dict[str, Any]],
    rows: List[Dict[str, Any]],
) -> Tuple[
    Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]],
    Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]],
]:
    if not columns:
        empty_columns = [{"name": "Prod Line", "id": "Prod Line"}]
        return (
            ("Quarter 1", empty_columns, []),
            ("Quarter 2", empty_columns, []),
        )

    prod_line_col = columns[0]
    metric_cols = columns[1:]
    # Each quarter has 4 cols: {tag} LBE, {tag} HS, {tag} LBE IYA, {tag} HS IYA
    # Q1 = metric_cols[0:4], Q2 = metric_cols[4:8]
    q1_cols = metric_cols[:4]
    q2_cols = metric_cols[4:8] if len(metric_cols) >= 8 else []

    # LBE table: Q1 LBE + Q1 LBE IYA + Q2 LBE + Q2 LBE IYA
    lbe_cols: List[Dict[str, Any]] = []
    if len(q1_cols) >= 4:
        lbe_cols.extend([q1_cols[0], q1_cols[2]])
    if len(q2_cols) >= 4:
        lbe_cols.extend([q2_cols[0], q2_cols[2]])

    # HS table: Q1 HS + Q1 HS IYA + Q2 HS + Q2 HS IYA
    hs_cols: List[Dict[str, Any]] = []
    if len(q1_cols) >= 4:
        hs_cols.extend([q1_cols[1], q1_cols[3]])
    if len(q2_cols) >= 4:
        hs_cols.extend([q2_cols[1], q2_cols[3]])

    def build_subset(subset_cols: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        selected_cols = [prod_line_col, *subset_cols] if subset_cols else [prod_line_col]
        selected_ids = [str(col.get("id", "")) for col in selected_cols]
        subset_rows: List[Dict[str, Any]] = []
        for row in rows or []:
            subset_rows.append({col_id: row.get(col_id, "-") for col_id in selected_ids if col_id})
        return selected_cols, subset_rows

    lbe_columns, lbe_rows = build_subset(lbe_cols)
    hs_columns, hs_rows = build_subset(hs_cols)

    lbe_title = "Demand System LBE By Quarter"
    hs_title = "Demand System LBE + Supply System Protection By Quarter"

    return (
        (lbe_title, lbe_columns, lbe_rows),
        (hs_title, hs_columns, hs_rows),
    )


def _snapshot_to_dataframe(columns: List[Dict[str, Any]], rows: List[Dict[str, Any]]) -> pd.DataFrame:
    ordered_ids = [str(col.get("id", "")).strip() for col in (columns or []) if str(col.get("id", "")).strip()]
    if not rows:
        return pd.DataFrame(columns=ordered_ids)

    data = pd.DataFrame(rows)
    if ordered_ids:
        for col in ordered_ids:
            if col not in data.columns:
                data[col] = ""
        return data[ordered_ids]
    return data


def _snapshot_clean_sheet_name(name: str) -> str:
    normalized = re.sub(r"[\\/*?:\[\]]", "_", str(name)).strip()
    if not normalized:
        normalized = "Sheet"
    return normalized[:31]


def create_dashboard_snapshot(cfg: AppConfig) -> Tuple[Path, Path, int]:
    data_bundle = load_data_bundle(cfg)
    monthly_requester = pd.DataFrame(data_bundle.get("monthly_requester", []))
    monthly_level1 = pd.DataFrame(data_bundle.get("monthly_level1", []))
    hc_idp_monthly = pd.DataFrame(data_bundle.get("hc_idp_monthly", []))
    production_data_df = pd.DataFrame(data_bundle.get("production_data", []))
    production_data_by_level_df = pd.DataFrame(data_bundle.get("production_data_by_level", []))
    td_demand_by_dimension_df = pd.DataFrame(data_bundle.get("td_demand_by_dimension", []))
    td_validation_detail = load_dataset(cfg.processed_dir, "td_version_gap_details.csv")
    historical_shipment = pd.DataFrame(data_bundle.get("historical_shipment", []))
    pde_alerts = pd.DataFrame(data_bundle.get("pde_alerts", []))
    request_details = load_request_details(cfg)

    page_sheets: Dict[str, List[Tuple[str, pd.DataFrame]]] = {
        "Demand Assumption": [],
        "Supply Protection": [],
        "Project Details": [],
        "Demand Data": [],
        "Production Data": [],
        "Raw Data": [],
    }

    role_matrix_columns, role_matrix_data = build_monthly_matrix(monthly_requester, ROLE_ALL_VALUE)
    page_sheets["Supply Protection"].append(("01 Role x Item", _snapshot_to_dataframe(role_matrix_columns, role_matrix_data)))

    summary_columns, summary_data = build_item_summary(monthly_requester, ROLE_ALL_VALUE)
    page_sheets["Supply Protection"].append(("02 Monthly Summary", _snapshot_to_dataframe(summary_columns, summary_data)))

    pde_columns, pde_data, pde_fg_columns, pde_fg_data = build_pde_tables(pde_alerts, request_details)
    pde_data = _sort_pde_records_keep_total_last(pde_data, [{"column_id": TOTAL_LABEL, "direction": "desc"}])
    pde_fg_data = _sort_pde_records_keep_total_last(pde_fg_data, [{"column_id": TOTAL_LABEL, "direction": "desc"}])
    page_sheets["Supply Protection"].append(("03 Past Due Alerts", _snapshot_to_dataframe(pde_columns, pde_data)))

    drill_columns, drill_rows = build_role_item_project_summary(request_details, ROLE_ALL_VALUE, [], [])
    page_sheets["Project Details"].append(("01 Role x Item x Project", _snapshot_to_dataframe(drill_columns, drill_rows)))

    hc_idp_columns, hc_idp_rows = build_hc_idp_monthly_table(hc_idp_monthly)
    page_sheets["Demand Assumption"].append(("01 Demand System LBE", _snapshot_to_dataframe(hc_idp_columns, hc_idp_rows)))

    hc_idp_hs_df = build_demand_hs_dataframe(hc_idp_monthly, monthly_level1)
    hc_idp_hs_columns, hc_idp_hs_rows = build_hc_idp_monthly_table(hc_idp_hs_df)
    page_sheets["Demand Assumption"].append(("02 Demand System LBE + Supply System Protection", _snapshot_to_dataframe(hc_idp_hs_columns, hc_idp_hs_rows)))

    hc_idp_iya_columns, hc_idp_iya_rows = build_demand_iya_table(hc_idp_monthly, historical_shipment)
    page_sheets["Demand Assumption"].append(("03 Demand System LBE IYA", _snapshot_to_dataframe(hc_idp_iya_columns, hc_idp_iya_rows)))

    hc_idp_hs_iya_columns, hc_idp_hs_iya_rows = build_demand_iya_table(hc_idp_hs_df, historical_shipment)
    page_sheets["Demand Assumption"].append(("04 Demand System LBE + Supply System Protection IYA", _snapshot_to_dataframe(hc_idp_hs_iya_columns, hc_idp_hs_iya_rows)))

    quarter_columns, quarter_rows = build_demand_iya_by_quarter_table(hc_idp_monthly, hc_idp_hs_df, historical_shipment)
    (_, q1_columns, q1_rows), (_, q2_columns, q2_rows) = split_quarter_iya_tables(quarter_columns, quarter_rows)
    page_sheets["Demand Assumption"].append(("05 Demand System LBE Quarter", _snapshot_to_dataframe(q1_columns, q1_rows)))
    page_sheets["Demand Assumption"].append(("06 Demand SysLBE+SSP Quarter", _snapshot_to_dataframe(q2_columns, q2_rows)))

    level1_core_columns, level1_core_rows = build_first_level_summary(
        monthly_level1,
        source_level_column="First Level",
        display_level_column="Level 1",
        include_levels=["Base", "PP"],
    )
    page_sheets["Demand Assumption"].append(("07 Supply Protection PP+Base", _snapshot_to_dataframe(level1_core_columns, level1_core_rows)))

    level1_hktw_ess_columns, level1_hktw_ess_rows = build_first_level_summary(
        monthly_level1,
        source_level_column="First Level",
        display_level_column="Level 1",
        include_levels=["HKTW", "ESS"],
    )
    page_sheets["Demand Assumption"].append(("08 Supply Protection HKTW+ESS", _snapshot_to_dataframe(level1_hktw_ess_columns, level1_hktw_ess_rows)))

    td_validation_columns, td_validation_rows = build_td_validation_table_from_detail(td_validation_detail)
    page_sheets["Demand Data"].append(("01 TD Version Monthly Comparison", _snapshot_to_dataframe(td_validation_columns, td_validation_rows)))

    production_group_1 = ["0386", "1864", "A868"]
    production_group_1_totals_after = {
        "0386": ("HP Total", ["0386", "C810"]),
        "1864": ("XQ Total", ["1864", "D352"]),
        "A868": ("TC Total", ["A868", "A673"]),
    }

    p1_columns, p1_rows = build_production_data_table_by_plant(
        production_data_df,
        plant_order=production_group_1,
        include_segment_totals=False,
    )
    page_sheets["Production Data"].append(("01 Table1 By Plant", _snapshot_to_dataframe(p1_columns, p1_rows)))

    prod_ver_df = pd.DataFrame(data_bundle.get("production_version_compare", []))
    pv_columns, pv_rows = build_production_version_comparison_table(prod_ver_df)
    page_sheets["Production Data"].append(("01b Version Comparison", _snapshot_to_dataframe(pv_columns, pv_rows)))

    p2_columns, p2_rows = build_production_data_table_by_plant_level(
        production_data_by_level_df,
        plant_order=production_group_1,
        include_segment_totals=True,
        segment_totals_after=production_group_1_totals_after,
    )
    page_sheets["Production Data"].append(("02 Table2 Plant-Level1-Level2", _snapshot_to_dataframe(p2_columns, p2_rows)))

    production_data_weekly_df = pd.DataFrame(data_bundle.get("production_data_weekly", []))
    production_data_by_level_weekly_df = pd.DataFrame(data_bundle.get("production_data_by_level_weekly", []))
    pw3_columns, pw3_rows = build_production_data_table_by_plant_weekly(
        production_data_weekly_df,
        plant_order=production_group_1,
        include_segment_totals=False,
    )
    page_sheets["Production Data"].append(("03 Table3 Weekly By Plant", _snapshot_to_dataframe(pw3_columns, pw3_rows)))

    pw4_columns, pw4_rows = build_production_data_table_by_plant_level_weekly(
        production_data_by_level_weekly_df,
        plant_order=production_group_1,
        include_segment_totals=True,
        segment_totals_after=production_group_1_totals_after,
    )
    page_sheets["Production Data"].append(("04 Table4 Weekly Plant-Lv1-Lv2", _snapshot_to_dataframe(pw4_columns, pw4_rows)))

    if not td_demand_by_dimension_df.empty:
        dim_columns, dim_rows = build_production_dimension_table(
            td_demand_by_dimension_df, ["Brand", "Size", "Variant"]
        )
        page_sheets["Production Data"].append(("05 Detail Brand-Size-Variant", _snapshot_to_dataframe(dim_columns, dim_rows)))

    page_sheets["Raw Data"].extend(
        [
            ("01 monthly_item", pd.DataFrame(data_bundle.get("monthly_item", []))),
            ("02 monthly_requester", monthly_requester),
            ("03 monthly_level1", monthly_level1),
            ("04 hc_idp_monthly", hc_idp_monthly),
            ("05 production_data", production_data_df),
            ("06 production_data_by_level", production_data_by_level_df),
            ("07 td_validation_detail", td_validation_detail),
            ("08 historical_shipment", historical_shipment),
            ("09 pde_alerts", pde_alerts),
            ("10 request_details", request_details),
            ("11 td_demand_by_dimension", td_demand_by_dimension_df),
        ]
    )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = cfg.processed_dir.parent / "history" / "dashboard_snapshots" / f"snapshot_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    logging.info("Snapshot output directory: %s (exists=%s)", out_dir, out_dir.exists())

    excel_path = out_dir / f"dashboard_snapshot_{timestamp}.xlsx"
    page_prefix = {
        "Demand Assumption": "DMD",
        "Supply Protection": "SP",
        "Project Details": "PRJ",
        "Demand Data": "VAL",
        "Production Data": "PRD",
        "Raw Data": "RAW",
    }
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        used_names = set()
        for page_name, sheet_items in page_sheets.items():
            prefix = page_prefix.get(page_name, "PG")
            for index, (sheet_name, frame) in enumerate(sheet_items, start=1):
                safe_name = _snapshot_clean_sheet_name(f"{prefix}{index:02d}_{sheet_name}")
                candidate = safe_name
                seq = 1
                while candidate in used_names:
                    suffix = f"_{seq}"
                    candidate = f"{safe_name[:31-len(suffix)]}{suffix}"
                    seq += 1
                used_names.add(candidate)
                frame.to_excel(writer, index=False, sheet_name=candidate)

    csv_dir = out_dir / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)
    for page_name, sheet_items in page_sheets.items():
        page_dir = csv_dir / page_name
        page_dir.mkdir(parents=True, exist_ok=True)
        for index, (sheet_name, frame) in enumerate(sheet_items, start=1):
            csv_name = _snapshot_clean_sheet_name(f"{index:02d}_{sheet_name}")
            frame.to_csv(page_dir / f"{csv_name}.csv", index=False, encoding="utf-8-sig")

    total_tables = sum(len(items) for items in page_sheets.values())
    return out_dir, excel_path, total_tables


def regenerate_weekly_mail_preview(cfg: AppConfig) -> Path:
    script_path = cfg.processed_dir.parent.parent / "scripts" / "generate_weekly_mail_preview.py"
    if not script_path.exists():
        raise FileNotFoundError(f"Mail preview script not found: {script_path}")

    subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(cfg.processed_dir.parent.parent),
        check=True,
        capture_output=True,
        text=True,
    )

    weekly_mail_dir = cfg.processed_dir / "weekly_mail"
    if not weekly_mail_dir.exists():
        raise FileNotFoundError(f"Weekly mail output directory not found: {weekly_mail_dir}")

    html_candidates = sorted(weekly_mail_dir.glob("Supply_Protection_Update_*.html"), key=lambda p: p.stat().st_mtime)
    if not html_candidates:
        raise FileNotFoundError(f"No weekly mail html generated in: {weekly_mail_dir}")
    return html_candidates[-1]


def build_td_validation_table(df: pd.DataFrame) -> Tuple[List[Dict], List[Dict]]:
    base_columns = [
        {"name": "Version", "id": "Version"},
        {"name": "Version Group", "id": "Version Group"},
        {"name": "Prod Line", "id": "Prod Line"},
    ]
    if df.empty:
        return base_columns + [{"name": "Total", "id": "Total"}], []

    working = df.copy()
    ordered_cols: List[str] = ["Version", "Version Group", "Prod Line"]
    month_cols = sorted(
        [col for col in working.columns if re.fullmatch(r"\d{4}-\d{2}", str(col))],
        key=lambda value: pd.Period(str(value), freq="M") if re.fullmatch(r"\d{4}-\d{2}", str(value)) else str(value),
    )
    ordered_cols.extend(month_cols)
    if "Total" in working.columns:
        ordered_cols.append("Total")

    for col in ordered_cols:
        if col not in working.columns:
            working[col] = ""

    numeric_cols = month_cols + (["Total"] if "Total" in ordered_cols else [])
    for col in numeric_cols:
        working[col] = pd.to_numeric(working[col], errors="coerce")

    working = working[ordered_cols].fillna("")
    columns = [{"name": col, "id": col} for col in ordered_cols]
    return columns, working.to_dict("records")


def build_td_validation_table_from_detail(td_detail_df: pd.DataFrame) -> Tuple[List[Dict], List[Dict]]:
    base_columns = [
        {"name": "Version", "id": "Version"},
        {"name": "Version Group", "id": "Version Group"},
        {"name": "Prod Line", "id": "Prod Line"},
    ]
    if td_detail_df.empty:
        return base_columns + [{"name": "Total", "id": "Total"}], []

    working = td_detail_df.copy()
    required_cols = ["Month", "Prod Line", "Current", "Previous", "Gap"]
    if any(col not in working.columns for col in required_cols):
        return base_columns + [{"name": "Total", "id": "Total"}], []

    month_cols = sorted(
        [str(col) for col in working["Month"].dropna().astype(str).unique() if re.fullmatch(r"\d{4}-\d{2}", str(col))],
        key=lambda value: pd.Period(str(value), freq="M"),
    )
    if not month_cols:
        return base_columns + [{"name": "Total", "id": "Total"}], []

    for col in ["Current", "Previous", "Gap"]:
        working[col] = pd.to_numeric(working[col], errors="coerce").fillna(0.0)

    core = working[working["Prod Line"].astype(str).isin(["Base", "PP"])]
    if core.empty:
        return base_columns + [{"name": "Total", "id": "Total"}], []

    grouped = (
        core.groupby(["Prod Line", "Month"], dropna=False)[["Current", "Previous", "Gap"]]
        .sum(min_count=1)
        .reset_index()
    )

    def make_frame(value_col: str) -> pd.DataFrame:
        frame = (
            grouped.pivot_table(
                index="Prod Line",
                columns="Month",
                values=value_col,
                aggfunc="sum",
                fill_value=0,
            )
            .reindex(index=["Base", "PP"], fill_value=0)
            .reindex(columns=month_cols, fill_value=0)
        )
        frame.loc["Total"] = frame.loc[["Base", "PP"]].sum(axis=0)
        return frame

    current_frame = make_frame("Current")
    previous_frame = make_frame("Previous")
    gap_frame = make_frame("Gap")

    current_version = str(working["Current Version"].dropna().astype(str).iloc[0]) if "Current Version" in working.columns and not working["Current Version"].dropna().empty else "Current"
    previous_version = str(working["Previous Version"].dropna().astype(str).iloc[0]) if "Previous Version" in working.columns and not working["Previous Version"].dropna().empty else "Previous"

    version_rows = [
        (f"Current ({current_version})", "Current", current_frame),
        (f"Previous ({previous_version})", "Previous", previous_frame),
        ("Gap", "Gap", gap_frame),
    ]

    records: List[Dict[str, Any]] = []
    for idx_version, (version_label, version_group, frame) in enumerate(version_rows):
        for idx, prod_line in enumerate(["Base", "PP", "Total"]):
            row: Dict[str, Any] = {
                "Version": version_label if idx == 0 else "",
                "Version Group": version_group,
                "Prod Line": prod_line,
            }
            total_value = 0
            for month in month_cols:
                int_value = int(round(float(frame.loc[prod_line, month])))
                total_value += int_value
                row[month] = int_value
            row["Total"] = total_value
            records.append(row)

        if idx_version < len(version_rows) - 1:
            spacer: Dict[str, Any] = {"Version": "", "Version Group": "", "Prod Line": ""}
            for month in month_cols:
                spacer[month] = ""
            spacer["Total"] = ""
            records.append(spacer)

    ordered_cols = ["Version", "Version Group", "Prod Line", *month_cols, "Total"]
    columns = [{"name": col, "id": col} for col in ordered_cols]
    return columns, records


def build_td_validation_style_data_conditional(columns: List[Dict]) -> List[Dict]:
    rules: List[Dict] = list(PDE_STYLE_DATA_CONDITIONAL)
    numeric_cols = [
        col.get("id")
        for col in columns
        if re.fullmatch(r"\d{4}-\d{2}", str(col.get("id", ""))) or str(col.get("id", "")) == "Total"
    ]

    for col in numeric_cols:
        rules.append(
            {
                "if": {"filter_query": f'{{Version Group}} = "Gap" && {{{col}}} < 0', "column_id": col},
                "color": "#dc2626",
                "fontWeight": "700",
            }
        )
        rules.append(
            {
                "if": {"filter_query": f'{{Version Group}} = "Gap" && {{{col}}} > 0', "column_id": col},
                "color": "#16a34a",
                "fontWeight": "700",
            }
        )

    rules.append(
        {
            "if": {"filter_query": '{Prod Line} = "Total"'},
            "backgroundColor": "#eaf2ff",
            "fontWeight": "700",
        }
    )
    return rules


def build_td_gap_detail_table(
    active_cell: Dict[str, Any] | None,
    table_rows: List[Dict[str, Any]] | None,
    level2_active_cell: Dict[str, Any] | None,
    level2_rows: List[Dict[str, Any]] | None,
    td_detail_df: pd.DataFrame,
) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    def normalize_level2_label(value: Any) -> str:
        text = str(value).strip() if value is not None else ""
        key = text.lower()
        if key in {"o-hot", "o hot"}:
            return "O-Hot"
        if key in {"o-non hot", "o non hot", "o-nonhot", "o nonhot"}:
            return "O-Non Hot"
        return text

    month_name = "Selected Month"
    month_col = ""
    if active_cell and isinstance(active_cell.get("column_id"), str):
        raw_month = str(active_cell.get("column_id")).strip()
        if re.fullmatch(r"\d{4}-\d{2}", raw_month):
            month_col = raw_month
            try:
                month_name = pd.Period(raw_month, freq="M").strftime("%b")
            except Exception:
                month_name = raw_month

    columns = [
        {"name": "APO Product", "id": "APO Product"},
        {"name": "Des", "id": "Des"},
        {"name": "Prod Line", "id": "Prod Line"},
        {"name": "Current", "id": "Current"},
        {"name": "Previous", "id": "Previous"},
        {"name": "GAP", "id": "GAP"},
    ]
    style_data_conditional = list(PDE_STYLE_DATA_CONDITIONAL)
    style_data_conditional.extend(
        [
            {
                "if": {"filter_query": "{GAP} < 0", "column_id": "GAP"},
                "color": "#dc2626",
                "fontWeight": "700",
            },
            {
                "if": {"filter_query": "{GAP} > 0", "column_id": "GAP"},
                "color": "#16a34a",
                "fontWeight": "700",
            },
        ]
    )

    if not table_rows:
        return "GAP Difference Details（请先点击上方 GAP 月份单元格）", columns, [], style_data_conditional

    if not active_cell:
        return "GAP Difference Details（请先点击上方 GAP 月份单元格）", columns, [], style_data_conditional

    row_index = active_cell.get("row")
    if row_index is None or row_index < 0 or row_index >= len(table_rows):
        return "GAP Difference Details（请先点击上方 GAP 月份单元格）", columns, [], style_data_conditional

    if not re.fullmatch(r"\d{4}-\d{2}", month_col):
        return "GAP Difference Details（请选择上方 GAP 行中的月份列）", columns, [], style_data_conditional

    selected_row = table_rows[row_index]
    if str(selected_row.get("Version Group", "")).strip() != "Gap":
        return "GAP Difference Details（请选择上方 GAP 行中的月份列）", columns, [], style_data_conditional

    prod_line = str(selected_row.get("Prod Line", "")).strip()
    if prod_line not in {"Base", "PP", "Total"}:
        return "GAP Difference Details（请选择上方 GAP 的 Base/PP/Total 行）", columns, [], style_data_conditional

    if td_detail_df.empty:
        return "GAP Difference Details（明细数据未生成，请先运行 pipeline）", columns, [], style_data_conditional

    working = td_detail_df.copy()
    required_cols = ["Month", "Prod Line", "APO Product", "Des", "Current", "Previous", "Gap"]
    if any(col not in working.columns for col in required_cols):
        return "GAP Difference Details（明细字段不完整）", columns, [], style_data_conditional

    filtered = working[
        (working["Month"].astype(str) == month_col)
        & (working["Prod Line"].astype(str) == prod_line)
    ].copy()
    if filtered.empty:
        return f"GAP Difference Details - {prod_line} / {month_col}（无明细）", columns, [], style_data_conditional

    selected_level2 = ""
    if level2_active_cell and level2_rows:
        level2_row_index = level2_active_cell.get("row")
        if isinstance(level2_row_index, int) and 0 <= level2_row_index < len(level2_rows):
            selected_level2_row = level2_rows[level2_row_index]
            selected_level2 = str(selected_level2_row.get("Level2", "")).strip()
            selected_level2_prod_line = str(selected_level2_row.get("Prod Line", "")).strip()
            if selected_level2 and selected_level2_prod_line in {"Base", "PP"} and selected_level2_prod_line == prod_line:
                normalized_selected_level2 = normalize_level2_label(selected_level2)
                filtered["Level2"] = filtered.get("Level2", "").fillna("").apply(normalize_level2_label)
                filtered = filtered[filtered["Level2"].eq(normalized_selected_level2)].copy()

    for col in ["Current", "Previous", "Gap"]:
        filtered[col] = pd.to_numeric(filtered[col], errors="coerce").fillna(0).round().astype(int)

    filtered = filtered[filtered["Gap"] != 0].copy()
    if filtered.empty:
        if selected_level2:
            return f"GAP Difference Details - {prod_line} / {month_col} / {selected_level2}（无差异）", columns, [], style_data_conditional
        return f"GAP Difference Details - {prod_line} / {month_col}（无差异）", columns, [], style_data_conditional

    filtered["abs_gap"] = filtered["Gap"].abs()
    filtered = filtered.sort_values(["abs_gap", "APO Product", "Des"], ascending=[False, True, True])

    current_version = str(filtered["Current Version"].iloc[0]) if "Current Version" in filtered.columns and not filtered.empty else "Current"
    previous_version = str(filtered["Previous Version"].iloc[0]) if "Previous Version" in filtered.columns and not filtered.empty else "Previous"

    columns = [
        {"name": "APO Product", "id": "APO Product"},
        {"name": "Des", "id": "Des"},
        {"name": "Prod Line", "id": "Prod Line"},
        {"name": current_version, "id": "Current"},
        {"name": previous_version, "id": "Previous"},
        {"name": "GAP", "id": "GAP"},
    ]

    records: List[Dict[str, Any]] = []
    for _, row in filtered.iterrows():
        apo_raw = row.get("APO Product", "")
        apo_num = pd.to_numeric(apo_raw, errors="coerce")
        if pd.notna(apo_num):
            apo_product = str(int(round(float(apo_num))))
        else:
            apo_product = str(apo_raw).strip()
        description = str(row.get("Des", "")).strip()
        records.append(
            {
                "APO Product": apo_product,
                "Des": description,
                "Prod Line": prod_line,
                "Current": int(row.get("Current", 0)),
                "Previous": int(row.get("Previous", 0)),
                "GAP": int(row.get("Gap", 0)),
            }
        )

    if selected_level2:
        return f"GAP Difference Details - {prod_line} / {month_name} ({month_col}) / {selected_level2}", columns, records, style_data_conditional
    return f"GAP Difference Details - {prod_line} / {month_name} ({month_col})", columns, records, style_data_conditional


def build_td_gap_level2_table(
    active_cell: Dict[str, Any] | None,
    table_rows: List[Dict[str, Any]] | None,
    td_detail_df: pd.DataFrame,
) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    def normalize_level2_label(value: Any) -> str:
        text = str(value).strip() if value is not None else ""
        key = text.lower()
        if key in {"o-hot", "o hot"}:
            return "O-Hot"
        if key in {"o-non hot", "o non hot", "o-nonhot", "o nonhot"}:
            return "O-Non Hot"
        return text

    month_col = ""
    month_name = "Selected Month"
    if active_cell and isinstance(active_cell.get("column_id"), str):
        raw_month = str(active_cell.get("column_id")).strip()
        if re.fullmatch(r"\d{4}-\d{2}", raw_month):
            month_col = raw_month
            try:
                month_name = pd.Period(raw_month, freq="M").strftime("%b")
            except Exception:
                month_name = raw_month

    columns = [
        {"name": "Prod Line", "id": "Prod Line"},
        {"name": "Level2", "id": "Level2"},
        {"name": "Current", "id": "Current"},
        {"name": "Previous", "id": "Previous"},
        {"name": "GAP", "id": "GAP"},
    ]
    style_data_conditional = list(PDE_STYLE_DATA_CONDITIONAL)
    style_data_conditional.extend(
        [
            {
                "if": {"filter_query": "{GAP} < 0", "column_id": "GAP"},
                "color": "#dc2626",
                "fontWeight": "700",
            },
            {
                "if": {"filter_query": "{GAP} > 0", "column_id": "GAP"},
                "color": "#16a34a",
                "fontWeight": "700",
            },
            {
                "if": {"filter_query": '{Prod Line} = "Total"'},
                "backgroundColor": "#eaf2ff",
                "fontWeight": "700",
            },
        ]
    )

    if not table_rows or not active_cell:
        return "Level2 GAP Details（请先点击上方 GAP 月份单元格）", columns, [], style_data_conditional

    row_index = active_cell.get("row")
    if row_index is None or row_index < 0 or row_index >= len(table_rows):
        return "Level2 GAP Details（请先点击上方 GAP 月份单元格）", columns, [], style_data_conditional

    if not re.fullmatch(r"\d{4}-\d{2}", month_col):
        return "Level2 GAP Details（请选择上方 GAP 行中的月份列）", columns, [], style_data_conditional

    selected_row = table_rows[row_index]
    if str(selected_row.get("Version Group", "")).strip() != "Gap":
        return "Level2 GAP Details（请选择上方 GAP 行中的月份列）", columns, [], style_data_conditional

    if td_detail_df.empty:
        return "Level2 GAP Details（明细数据未生成，请先运行 pipeline）", columns, [], style_data_conditional

    required_cols = ["Month", "Prod Line", "Level2", "Current", "Previous", "Gap", "Current Version", "Previous Version"]
    working = td_detail_df.copy()
    if any(col not in working.columns for col in required_cols):
        return "Level2 GAP Details（缺少 Level2 字段，请先重跑 pipeline）", columns, [], style_data_conditional

    month_df = working[working["Month"].astype(str) == month_col].copy()
    if month_df.empty:
        return f"Level2 GAP Details - {month_name} ({month_col})（无明细）", columns, [], style_data_conditional

    for col in ["Current", "Previous", "Gap"]:
        month_df[col] = pd.to_numeric(month_df[col], errors="coerce").fillna(0.0)

    month_df["Level2"] = month_df["Level2"].fillna("").apply(normalize_level2_label)
    month_df.loc[month_df["Level2"] == "", "Level2"] = "未映射"

    grouped_all = (
        month_df[month_df["Prod Line"].astype(str).isin(["Base", "PP"])]
        .groupby(["Prod Line", "Level2"], dropna=False)[["Current", "Previous", "Gap"]]
        .sum(min_count=1)
        .reset_index()
    )

    grouped = grouped_all.copy()
    grouped["Gap Rounded"] = grouped["Gap"].round().astype(int)
    grouped = grouped[grouped["Gap Rounded"] != 0].copy()
    if grouped.empty:
        return f"Level2 GAP Details - {month_name} ({month_col})（无差异）", columns, [], style_data_conditional

    current_version = str(month_df["Current Version"].iloc[0]) if not month_df.empty else "Current"
    previous_version = str(month_df["Previous Version"].iloc[0]) if not month_df.empty else "Previous"
    columns = [
        {"name": "Prod Line", "id": "Prod Line"},
        {"name": "Level2", "id": "Level2"},
        {"name": current_version, "id": "Current"},
        {"name": previous_version, "id": "Previous"},
        {"name": "GAP", "id": "GAP"},
    ]

    grouped["prod_order"] = grouped["Prod Line"].map({"Base": 0, "PP": 1}).fillna(2)
    grouped = grouped.sort_values(["prod_order", "Level2"], ascending=[True, True])

    records: List[Dict[str, Any]] = []
    for _, row in grouped.iterrows():
        records.append(
            {
                "Prod Line": str(row.get("Prod Line", "")).strip(),
                "Level2": str(row.get("Level2", "")).strip(),
                "Current": int(round(float(row.get("Current", 0)))),
                "Previous": int(round(float(row.get("Previous", 0)))),
                "GAP": int(row.get("Gap Rounded", 0)),
            }
        )

    total_row = {
        "Prod Line": "Total",
        "Level2": "",
        "Current": int(round(float(grouped_all["Current"].sum()))),
        "Previous": int(round(float(grouped_all["Previous"].sum()))),
        "GAP": int(round(float(grouped_all["Gap"].sum()))),
    }
    records.append(total_row)

    return f"Level2 GAP Details - {month_name} ({month_col})", columns, records, style_data_conditional


# ---------------------------------------------------------------------------
# Brand Dimension GAP – summary table (left) & detail table (right)
# ---------------------------------------------------------------------------

_BRAND_DIM_COLS = ["Brand", "NI/Conversion", "Variant", "Size"]


def build_td_gap_brand_summary_table(
    active_cell: Dict[str, Any] | None,
    table_rows: List[Dict[str, Any]] | None,
    td_detail_df: pd.DataFrame,
) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build a Brand-level summary of GAP for the selected month."""
    month_col = ""
    month_name = "Selected Month"
    if active_cell and isinstance(active_cell.get("column_id"), str):
        raw_month = str(active_cell.get("column_id")).strip()
        if re.fullmatch(r"\d{4}-\d{2}", raw_month):
            month_col = raw_month
            try:
                month_name = pd.Period(raw_month, freq="M").strftime("%b")
            except Exception:
                month_name = raw_month

    columns = [
        {"name": "Brand", "id": "Brand"},
        {"name": "Current", "id": "Current"},
        {"name": "Previous", "id": "Previous"},
        {"name": "GAP", "id": "GAP"},
    ]
    style_data_conditional = list(PDE_STYLE_DATA_CONDITIONAL)
    style_data_conditional.extend([
        {"if": {"filter_query": "{GAP} < 0", "column_id": "GAP"}, "color": "#dc2626", "fontWeight": "700"},
        {"if": {"filter_query": "{GAP} > 0", "column_id": "GAP"}, "color": "#16a34a", "fontWeight": "700"},
        {"if": {"filter_query": '{Brand} = "Total"'}, "backgroundColor": "#eaf2ff", "fontWeight": "700"},
    ])

    if not table_rows or not active_cell:
        return "Brand GAP Summary（请先点击上方 GAP 行）", columns, [], style_data_conditional
    row_index = active_cell.get("row")
    if row_index is None or row_index < 0 or row_index >= len(table_rows):
        return "Brand GAP Summary（请先点击上方 GAP 行）", columns, [], style_data_conditional
    if not re.fullmatch(r"\d{4}-\d{2}", month_col):
        return "Brand GAP Summary（请选择 GAP 行中的月份列）", columns, [], style_data_conditional
    selected_row = table_rows[row_index]
    if str(selected_row.get("Version Group", "")).strip() != "Gap":
        return "Brand GAP Summary（请选择 GAP 行中的月份列）", columns, [], style_data_conditional
    if td_detail_df.empty or "Brand" not in td_detail_df.columns:
        return "Brand GAP Summary（无 Brand 维度数据，请重跑 pipeline）", columns, [], style_data_conditional

    month_df = td_detail_df[td_detail_df["Month"].astype(str) == month_col].copy()
    if month_df.empty:
        return f"Brand GAP Summary - {month_name} ({month_col})（无明细）", columns, [], style_data_conditional

    for col in ["Current", "Previous", "Gap"]:
        month_df[col] = pd.to_numeric(month_df[col], errors="coerce").fillna(0.0)

    month_df["Brand"] = month_df["Brand"].fillna("").astype(str).str.strip()
    month_df.loc[month_df["Brand"] == "", "Brand"] = "未映射"

    # Only use Base + PP rows (exclude Total to avoid double-counting)
    base_pp = month_df[month_df["Prod Line"].astype(str).isin(["Base", "PP"])]
    grouped = base_pp.groupby("Brand", dropna=False)[["Current", "Previous", "Gap"]].sum(min_count=1).reset_index()
    grouped["Gap Rounded"] = grouped["Gap"].round().astype(int)

    current_version = str(month_df["Current Version"].iloc[0]) if "Current Version" in month_df.columns and not month_df.empty else "Current"
    previous_version = str(month_df["Previous Version"].iloc[0]) if "Previous Version" in month_df.columns and not month_df.empty else "Previous"
    columns = [
        {"name": "Brand", "id": "Brand"},
        {"name": current_version, "id": "Current"},
        {"name": previous_version, "id": "Previous"},
        {"name": "GAP", "id": "GAP"},
    ]

    grouped = grouped.sort_values("Brand")
    records: List[Dict[str, Any]] = []
    for _, row in grouped.iterrows():
        records.append({
            "Brand": str(row["Brand"]),
            "Current": int(round(float(row["Current"]))),
            "Previous": int(round(float(row["Previous"]))),
            "GAP": int(row["Gap Rounded"]),
        })
    total_row = {
        "Brand": "Total",
        "Current": int(round(float(grouped["Current"].sum()))),
        "Previous": int(round(float(grouped["Previous"].sum()))),
        "GAP": int(round(float(grouped["Gap"].sum()))),
    }
    records.append(total_row)

    return f"Brand GAP Summary - {month_name} ({month_col})", columns, records, style_data_conditional


def build_td_gap_brand_detail_table(
    active_cell: Dict[str, Any] | None,
    table_rows: List[Dict[str, Any]] | None,
    brand_active_cell: Dict[str, Any] | None,
    brand_rows: List[Dict[str, Any]] | None,
    td_detail_df: pd.DataFrame,
    visible_dims: List[str] | None = None,
) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build a Brand × NI/Conversion × Variant × Size detail table for the selected month."""
    if visible_dims is None:
        visible_dims = list(_BRAND_DIM_COLS)
    # Ensure at least Brand is always shown
    if not visible_dims:
        visible_dims = ["Brand"]

    month_col = ""
    month_name = "Selected Month"
    if active_cell and isinstance(active_cell.get("column_id"), str):
        raw_month = str(active_cell.get("column_id")).strip()
        if re.fullmatch(r"\d{4}-\d{2}", raw_month):
            month_col = raw_month
            try:
                month_name = pd.Period(raw_month, freq="M").strftime("%b")
            except Exception:
                month_name = raw_month

    dim_columns = [{"name": d, "id": d} for d in visible_dims]
    value_columns = [
        {"name": "Prod Line", "id": "Prod Line"},
        {"name": "Current", "id": "Current"},
        {"name": "Previous", "id": "Previous"},
        {"name": "GAP", "id": "GAP"},
    ]
    columns = dim_columns + value_columns

    style_data_conditional = list(PDE_STYLE_DATA_CONDITIONAL)
    style_data_conditional.extend([
        {"if": {"filter_query": "{GAP} < 0", "column_id": "GAP"}, "color": "#dc2626", "fontWeight": "700"},
        {"if": {"filter_query": "{GAP} > 0", "column_id": "GAP"}, "color": "#16a34a", "fontWeight": "700"},
    ])

    if not table_rows or not active_cell:
        return "Brand Dimension Detail（请先点击上方 GAP 行）", columns, [], style_data_conditional
    row_index = active_cell.get("row")
    if row_index is None or row_index < 0 or row_index >= len(table_rows):
        return "Brand Dimension Detail（请先点击上方 GAP 行）", columns, [], style_data_conditional
    if not re.fullmatch(r"\d{4}-\d{2}", month_col):
        return "Brand Dimension Detail（请选择 GAP 行中的月份列）", columns, [], style_data_conditional
    selected_row = table_rows[row_index]
    if str(selected_row.get("Version Group", "")).strip() != "Gap":
        return "Brand Dimension Detail（请选择 GAP 行中的月份列）", columns, [], style_data_conditional

    needed_cols = ["Month", "Prod Line", "Current", "Previous", "Gap", "Brand"]
    if td_detail_df.empty or any(c not in td_detail_df.columns for c in needed_cols):
        return "Brand Dimension Detail（无维度数据，请重跑 pipeline）", columns, [], style_data_conditional

    month_df = td_detail_df[td_detail_df["Month"].astype(str) == month_col].copy()
    if month_df.empty:
        return f"Brand Dimension Detail - {month_name} ({month_col})（无明细）", columns, [], style_data_conditional

    for col in ["Current", "Previous", "Gap"]:
        month_df[col] = pd.to_numeric(month_df[col], errors="coerce").fillna(0.0)

    # Fill missing dimension values
    for dc in _BRAND_DIM_COLS:
        if dc in month_df.columns:
            month_df[dc] = month_df[dc].fillna("").astype(str).str.strip()
        else:
            month_df[dc] = ""

    # Only Base + PP
    filtered = month_df[month_df["Prod Line"].astype(str).isin(["Base", "PP"])].copy()
    if filtered.empty:
        return f"Brand Dimension Detail - {month_name} ({month_col})（无明细）", columns, [], style_data_conditional

    # Filter by selected Brand from summary table
    selected_brand = ""
    if brand_active_cell and brand_rows:
        br_idx = brand_active_cell.get("row")
        if isinstance(br_idx, int) and 0 <= br_idx < len(brand_rows):
            selected_brand = str(brand_rows[br_idx].get("Brand", "")).strip()
            if selected_brand and selected_brand != "Total":
                filtered = filtered[filtered["Brand"] == selected_brand].copy()
            else:
                selected_brand = ""

    # Group by visible dims + Prod Line
    group_cols = [d for d in visible_dims if d in filtered.columns] + ["Prod Line"]
    grouped = filtered.groupby(group_cols, dropna=False)[["Current", "Previous", "Gap"]].sum(min_count=1).reset_index()
    grouped["Gap Rounded"] = grouped["Gap"].round().astype(int)
    grouped = grouped[grouped["Gap Rounded"] != 0].copy()

    if grouped.empty:
        suffix = f" / {selected_brand}" if selected_brand else ""
        return f"Brand Dimension Detail - {month_name} ({month_col}){suffix}（无差异）", columns, [], style_data_conditional

    current_version = str(month_df["Current Version"].iloc[0]) if "Current Version" in month_df.columns and not month_df.empty else "Current"
    previous_version = str(month_df["Previous Version"].iloc[0]) if "Previous Version" in month_df.columns and not month_df.empty else "Previous"
    dim_columns = [{"name": d, "id": d} for d in visible_dims]
    value_columns = [
        {"name": "Prod Line", "id": "Prod Line"},
        {"name": current_version, "id": "Current"},
        {"name": previous_version, "id": "Previous"},
        {"name": "GAP", "id": "GAP"},
    ]
    columns = dim_columns + value_columns

    grouped["abs_gap"] = grouped["Gap"].abs()
    sort_cols = ["abs_gap"] + [d for d in visible_dims if d in grouped.columns]
    grouped = grouped.sort_values(sort_cols, ascending=[False] + [True] * len([d for d in visible_dims if d in grouped.columns]))

    records: List[Dict[str, Any]] = []
    for _, row in grouped.iterrows():
        rec: Dict[str, Any] = {}
        for d in visible_dims:
            rec[d] = str(row.get(d, ""))
        rec["Prod Line"] = str(row.get("Prod Line", ""))
        rec["Current"] = int(round(float(row.get("Current", 0))))
        rec["Previous"] = int(round(float(row.get("Previous", 0))))
        rec["GAP"] = int(row.get("Gap Rounded", 0))
        records.append(rec)

    suffix = f" / {selected_brand}" if selected_brand else ""
    return f"Brand Dimension Detail - {month_name} ({month_col}){suffix}", columns, records, style_data_conditional


def normalize_requester_values(values: Any) -> List[str]:
    if values is None:
        return []
    if isinstance(values, list):
        cleaned = [str(v).strip() for v in values if str(v).strip()]
        return sorted(set(cleaned))
    text = str(values).strip()
    return [text] if text else []


def normalize_mrp_values(values: Any) -> List[str]:
    if values is None:
        return []
    if isinstance(values, list):
        cleaned = [str(v).strip() for v in values if str(v).strip()]
        return sorted(set(cleaned))
    text = str(values).strip()
    return [text] if text else []


def build_requester_email_options(df: pd.DataFrame, role: str) -> List[Dict[str, str]]:
    if df.empty or "Requester Email" not in df.columns:
        return []

    working = df.copy()
    if "requester_role" in working.columns:
        working["requester_role"] = working["requester_role"].fillna(UNKNOWN_ROLE)
    if role and role != ROLE_ALL_VALUE and "requester_role" in working.columns:
        working = working[working["requester_role"] == role]

    emails = sorted({
        str(email).strip()
        for email in working["Requester Email"].dropna().tolist()
        if str(email).strip()
    })
    return [{"label": email, "value": email} for email in emails]


def build_mrp_indicator_options(df: pd.DataFrame, role: str, requester_emails: Optional[List[str]] = None) -> List[Dict[str, str]]:
    if df.empty or "MRP Element Indicator" not in df.columns:
        return []

    working = df.copy()
    if "requester_role" in working.columns:
        working["requester_role"] = working["requester_role"].fillna(UNKNOWN_ROLE)
    if role and role != ROLE_ALL_VALUE and "requester_role" in working.columns:
        working = working[working["requester_role"] == role]

    selected_emails = normalize_requester_values(requester_emails)
    if selected_emails and "Requester Email" in working.columns:
        working = working[working["Requester Email"].fillna("").astype(str).isin(set(selected_emails))]

    indicators = sorted({
        str(val).strip()
        for val in working["MRP Element Indicator"].dropna().tolist()
        if str(val).strip()
    })
    return [{"label": val, "value": val} for val in indicators]


def build_item_text_options(df: pd.DataFrame, role: str, requester_emails: Optional[List[str]] = None, mrp_indicators: Optional[List[str]] = None) -> List[Dict[str, str]]:
    if df.empty or "Item Text" not in df.columns:
        return []
    working = df.copy()
    if "requester_role" in working.columns:
        working["requester_role"] = working["requester_role"].fillna(UNKNOWN_ROLE)
    if role and role != ROLE_ALL_VALUE and "requester_role" in working.columns:
        working = working[working["requester_role"] == role]
    selected_emails = normalize_requester_values(requester_emails)
    if selected_emails and "Requester Email" in working.columns:
        working = working[working["Requester Email"].fillna("").astype(str).isin(set(selected_emails))]
    selected_mrp = normalize_mrp_values(mrp_indicators)
    if selected_mrp and "MRP Element Indicator" in working.columns:
        working = working[working["MRP Element Indicator"].astype(str).isin(set(selected_mrp))]
    items = sorted({
        str(val).strip()
        for val in working["Item Text"].dropna().tolist()
        if str(val).strip()
    })
    return [{"label": val, "value": val} for val in items]


def build_role_item_project_summary(
    df: pd.DataFrame,
    role: str,
    requester_emails: Optional[List[str]] = None,
    mrp_indicators: Optional[List[str]] = None,
    item_texts: Optional[List[str]] = None,
) -> Tuple[List[Dict], List[Dict]]:
    base_columns = [
        {"name": "Role", "id": "Role"},
        {"name": "Item Text", "id": "Item Text"},
        {"name": "MRP Element Indicator", "id": "MRP Element Indicator"},
    ]
    if df.empty or "MSU" not in df.columns:
        return base_columns + [{"name": TOTAL_LABEL, "id": TOTAL_LABEL}], []

    working = df.copy()
    working["requester_role"] = working.get("requester_role", UNKNOWN_ROLE).fillna(UNKNOWN_ROLE)
    working["Item Text"] = working.get("Item Text", "").astype(str)
    working["MRP Element Indicator"] = working.get("MRP Element Indicator", "").astype(str)
    working["Requester Email"] = working.get("Requester Email", "").fillna("").astype(str).str.strip()

    if "availability_month" not in working.columns:
        working["availability_month"] = working.get("Availability Date", pd.NaT)
        working["availability_month"] = pd.to_datetime(working["availability_month"], errors="coerce").dt.to_period("M").astype(str)

    if role and role != ROLE_ALL_VALUE:
        working = working[working["requester_role"] == role]

    selected_emails = normalize_requester_values(requester_emails)
    if selected_emails:
        working = working[working["Requester Email"].isin(set(selected_emails))]

    selected_mrp_indicators = normalize_mrp_values(mrp_indicators)
    if selected_mrp_indicators:
        working = working[working["MRP Element Indicator"].astype(str).isin(set(selected_mrp_indicators))]

    selected_item_texts = normalize_mrp_values(item_texts)
    if selected_item_texts:
        working = working[working["Item Text"].astype(str).isin(set(selected_item_texts))]

    if working.empty:
        return base_columns + [{"name": TOTAL_LABEL, "id": TOTAL_LABEL}], []

    months = sort_month_labels(working.get("availability_month", pd.Series(dtype=str)).dropna().tolist())

    grouped = (
        working.groupby(["requester_role", "Item Text", "MRP Element Indicator", "availability_month"], dropna=False)["MSU"]
        .sum(min_count=1)
        .reset_index()
    )

    if grouped.empty:
        return base_columns + [{"name": TOTAL_LABEL, "id": TOTAL_LABEL}], []

    pivot = (
        grouped.pivot_table(
            index=["requester_role", "Item Text", "MRP Element Indicator"],
            columns="availability_month",
            values="MSU",
            aggfunc="sum",
            fill_value=0,
        )
        .reindex(columns=months, fill_value=0)
        .sort_index()
    )

    pivot[TOTAL_LABEL] = pivot.sum(axis=1)
    pivot = pivot.reset_index().rename(columns={"requester_role": "Role"})

    columns = base_columns + [{"name": month, "id": month} for month in months] + [{"name": TOTAL_LABEL, "id": TOTAL_LABEL}]

    def fmt(value: float) -> str:
        if pd.isna(value) or value == 0:
            return "-"
        return f"{value:,.1f}"

    def display_role(role_name: str) -> str:
        normalized = str(role_name).strip()
        return ROLE_DISPLAY_MAP.get(normalized, normalized)

    records_with_totals: List[Tuple[float, Dict]] = []
    for _, row in pivot.iterrows():
        role_raw = row.get("Role", UNKNOWN_ROLE) or UNKNOWN_ROLE
        record = {
            "Role": display_role(role_raw),
            "Item Text": row.get("Item Text", "未定义") or "未定义",
            "MRP Element Indicator": row.get("MRP Element Indicator", "未定义") or "未定义",
            "__role_raw": role_raw,
        }
        for month in months:
            record[month] = fmt(row.get(month, 0))
        total_value = row.get(TOTAL_LABEL, 0)
        record[TOTAL_LABEL] = fmt(total_value)
        records_with_totals.append((total_value if pd.notna(total_value) else 0, record))

    sorted_records = [rec for _, rec in sorted(records_with_totals, key=lambda item: item[0], reverse=True)]

    total_record: Dict[str, Any] = {
        "Role": TOTAL_LABEL,
        "Item Text": TOTAL_LABEL,
        "MRP Element Indicator": "",
        "__role_raw": TOTAL_LABEL,
    }
    for month in months:
        total_record[month] = fmt(pd.to_numeric(pivot[month], errors="coerce").fillna(0.0).sum(min_count=1))
    total_record[TOTAL_LABEL] = fmt(pd.to_numeric(pivot[TOTAL_LABEL], errors="coerce").fillna(0.0).sum(min_count=1))
    sorted_records.append(total_record)

    return columns, sorted_records


def extract_role_item_project_total_row(
    columns: List[Dict[str, Any]],
    rows: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    total_rows = [
        row for row in (rows or [])
        if str(row.get("Role", "")).strip() == TOTAL_LABEL and str(row.get("Item Text", "")).strip() == TOTAL_LABEL
    ]
    non_total_rows = [
        row for row in (rows or [])
        if not (str(row.get("Role", "")).strip() == TOTAL_LABEL and str(row.get("Item Text", "")).strip() == TOTAL_LABEL)
    ]
    return columns or [], total_rows[:1], non_total_rows


def build_modal_detail_rows(
    df: pd.DataFrame,
    role: str,
    item_text: str,
    mrp_indicator: str,
    requester_emails: Optional[List[str]] = None,
    selected_month: Optional[str] = None,
) -> Tuple[List[Dict], List[Dict]]:
    if df.empty:
        columns = [{"name": field, "id": field} for field in DETAIL_VIEW_FIELDS]
        return columns, []

    working = df.copy()
    if role and role != ROLE_ALL_VALUE and "requester_role" in working.columns:
        working = working[working["requester_role"] == role]
    if item_text and "Item Text" in working.columns:
        working = working[working["Item Text"].astype(str) == str(item_text)]
    if mrp_indicator and "MRP Element Indicator" in working.columns:
        working = working[working["MRP Element Indicator"].astype(str) == str(mrp_indicator)]

    selected_emails = normalize_requester_values(requester_emails)
    if selected_emails and "Requester Email" in working.columns:
        working = working[working["Requester Email"].fillna("").astype(str).isin(set(selected_emails))]

    if selected_month and re.fullmatch(r"\d{4}-\d{2}", str(selected_month).strip()):
        month_value = str(selected_month).strip()
        if "availability_month" in working.columns:
            working = working[working["availability_month"].astype(str).str.strip() == month_value]
        elif "Availability Date" in working.columns:
            derived_month = pd.to_datetime(working["Availability Date"], errors="coerce").dt.to_period("M").astype(str)
            working = working[derived_month == month_value]

    if working.empty:
        columns = [{"name": field, "id": field} for field in DETAIL_VIEW_FIELDS]
        return columns, []

    subset = working.copy()

    for field in DETAIL_VIEW_FIELDS:
        if field not in subset.columns:
            subset[field] = "-"

    if "Quantity" in subset.columns:
        subset["Quantity"] = pd.to_numeric(subset["Quantity"], errors="coerce")
        subset["Quantity"] = subset["Quantity"].apply(lambda val: f"{val:,.0f}" if pd.notna(val) else "-")
    if "MSU" in subset.columns:
        subset["MSU"] = pd.to_numeric(subset["MSU"], errors="coerce")
        subset["MSU"] = subset["MSU"].apply(lambda val: f"{val:,.2f}" if pd.notna(val) else "-")
    if "PDE Checking" in subset.columns:
        subset["PDE Checking"] = pd.to_numeric(subset["PDE Checking"], errors="coerce")
        subset["PDE Checking"] = subset["PDE Checking"].apply(lambda val: f"{val:,.0f}" if pd.notna(val) else "-")
    if "Rolling Checking" in subset.columns:
        subset["Rolling Checking"] = pd.to_numeric(subset["Rolling Checking"], errors="coerce")
        subset["Rolling Checking"] = subset["Rolling Checking"].apply(lambda val: f"{val:,.0f}" if pd.notna(val) else "-")

    subset = subset.fillna("-")
    columns = [{"name": field, "id": field} for field in DETAIL_VIEW_FIELDS]
    return columns, subset[DETAIL_VIEW_FIELDS].to_dict("records")


def build_role_options(df: pd.DataFrame) -> List[Dict[str, Any]]:
    if "requester_role" in df.columns:
        roles = sorted({role for role in df["requester_role"].dropna().tolist() if role})
        if "total_msu" in df.columns:
            role_totals = df.groupby("requester_role", dropna=False)["total_msu"].sum(min_count=1)
            roles = [r for r in roles if r in role_totals.index and pd.notna(role_totals.get(r, 0)) and float(role_totals.get(r, 0)) > 0]
    else:
        roles = []

    def make_option(label: str, value: str) -> Dict[str, Any]:
        return {"label": html.Span(label, className="role-chip-label"), "value": value}

    options: List[Dict[str, Any]] = [make_option("ALL", ROLE_ALL_VALUE)]
    options.extend(make_option(ROLE_DISPLAY_MAP.get(role, role), role) for role in roles)
    return options


def build_role_trend(df: pd.DataFrame, role: str) -> go.Figure:
    fig = go.Figure()
    if df.empty:
        fig.update_layout(title="暂无数据")
        return fig

    all_months = sort_month_labels(df["availability_month"].dropna().tolist())
    if not all_months:
        fig.update_layout(title="暂无月份数据")
        return fig
    month_tick_text = [format_month_label(label) for label in all_months]

    frame = df.copy()
    if role and role != ROLE_ALL_VALUE:
        frame = frame[frame["requester_role"] == role]

    grouped = (
        frame.groupby(["Item Text", "availability_month"], dropna=False)["total_msu"]
        .sum(min_count=1)
        .reset_index()
    )

    if grouped.empty:
        pivot = pd.DataFrame(0, index=pd.Index([], name="Item Text"), columns=all_months)
    else:
        pivot = (
            grouped.pivot_table(
                index="Item Text",
                columns="availability_month",
                values="total_msu",
                aggfunc="sum",
                fill_value=0,
            )
            .reindex(columns=all_months, fill_value=0)
        )

    has_volume = not pivot.empty and pivot.values.sum() > 0
    if has_volume:
        for item in pivot.index:
            row_sum = pivot.loc[item].sum()
            if row_sum == 0:
                continue
            values = pivot.loc[item].tolist()
            fig.add_bar(
                name=item,
                x=all_months,
                y=values,
                text=[f"{v:,.0f}" for v in values],
                textposition="inside",
                insidetextanchor="middle",
                hovertemplate="%{x}<br>%{y:,.0f} MSU<extra>%{fullData.name}</extra>",
                marker=dict(line=dict(width=0)),
            )

    totals = pivot.sum(axis=0).reindex(all_months, fill_value=0)
    max_total_value = totals.max() if not totals.empty else 0
    upper_bound = float(max_total_value) * 1.15 if pd.notna(max_total_value) and max_total_value > 0 else 1.0
    if has_volume:
        fig.add_scatter(
            name="月度总计",
            x=all_months,
            y=totals,
            mode="lines+markers+text",
            text=[f"{v:,.0f}" for v in totals],
            textposition="top center",
            line=dict(color="#ff8c00", width=3),
        )
    else:
        fig.add_annotation(
            text="该角色暂无 MSU 数据",
            xref="paper",
            yref="paper",
            x=0.5,
            y=0.5,
            showarrow=False,
            font=dict(color="#7f8c8d", size=14, family=GLOBAL_FONT_FAMILY),
        )

    fig.update_layout(
        title=None,
        barmode="stack",
        legend=dict(orientation="h", x=0.5, xanchor="center", y=1.08),
        yaxis_title="MSU",
        xaxis_title="月份",
        template="plotly_white",
        plot_bgcolor="#ffffff",
        paper_bgcolor="#ffffff",
        font=dict(color="#334155", family=GLOBAL_FONT_FAMILY),
        colorway=["#2563eb", "#0ea5e9", "#14b8a6", "#6366f1", "#f59e0b", "#ec4899"],
        margin=dict(l=30, r=20, t=70, b=50),
    )
    fig.update_yaxes(showgrid=True, gridcolor="#e2e8f0", zerolinecolor="#cbd5e1")
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(range=[0, upper_bound])
    fig.update_xaxes(
        categoryorder="array",
        categoryarray=all_months,
        tickmode="array",
        tickvals=all_months,
        ticktext=month_tick_text,
    )
    return fig


def build_layout(app: Dash, cfg: AppConfig) -> html.Div:
    data_bundle = load_data_bundle(cfg)
    monthly_item = pd.DataFrame(data_bundle["monthly_item"])
    monthly_requester = pd.DataFrame(data_bundle["monthly_requester"])
    monthly_level1 = pd.DataFrame(data_bundle.get("monthly_level1", []))
    hc_idp_monthly = pd.DataFrame(data_bundle.get("hc_idp_monthly", []))
    production_data_df = pd.DataFrame(data_bundle.get("production_data", []))
    production_data_by_level_df = pd.DataFrame(data_bundle.get("production_data_by_level", []))
    td_validation_detail = load_dataset(cfg.processed_dir, "td_version_gap_details.csv")
    historical_shipment = pd.DataFrame(data_bundle.get("historical_shipment", []))
    pde_alerts = pd.DataFrame(data_bundle["pde_alerts"])
    request_details = load_request_details(cfg)
    details_version = data_bundle.get("request_details_version")
    _dv_text, _dv_today = _format_data_version_display()
    metrics = compute_metrics(monthly_item, pde_alerts, request_details)
    role_options = build_role_options(monthly_requester)
    default_role = role_options[0]["value"] if role_options else ROLE_ALL_VALUE
    role_matrix_columns, role_matrix_data = build_monthly_matrix(monthly_requester, ROLE_ALL_VALUE)
    pde_columns, pde_data, pde_fg_columns, pde_fg_data = build_pde_tables(pde_alerts, request_details)
    summary_drill_columns, summary_drill_rows = build_role_item_project_summary(request_details, default_role)
    summary_total_columns, summary_total_rows, summary_drill_rows = extract_role_item_project_total_row(summary_drill_columns, summary_drill_rows)
    drill_requester_options = build_requester_email_options(request_details, default_role)
    drill_mrp_options = build_mrp_indicator_options(request_details, default_role)
    drill_item_text_options = build_item_text_options(request_details, default_role)

    level1_core_columns, level1_core_rows = build_first_level_summary(
        monthly_level1,
        source_level_column="First Level",
        display_level_column="Level 1",
        include_levels=["Base", "PP"],
    )
    level1_hktw_ess_columns, level1_hktw_ess_rows = build_first_level_summary(
        monthly_level1,
        source_level_column="First Level",
        display_level_column="Level 1",
        include_levels=["HKTW", "ESS"],
    )
    hc_idp_columns, hc_idp_rows = build_hc_idp_monthly_table(hc_idp_monthly)
    hc_idp_hs_df = build_demand_hs_dataframe(hc_idp_monthly, monthly_level1)
    hc_idp_hs_columns, hc_idp_hs_rows = build_hc_idp_monthly_table(hc_idp_hs_df)
    hc_idp_iya_columns, hc_idp_iya_rows = build_demand_iya_table(hc_idp_monthly, historical_shipment)
    hc_idp_hs_iya_columns, hc_idp_hs_iya_rows = build_demand_iya_table(hc_idp_hs_df, historical_shipment)
    hc_idp_quarter_iya_columns, hc_idp_quarter_iya_rows = build_demand_iya_by_quarter_table(
        hc_idp_monthly,
        hc_idp_hs_df,
        historical_shipment,
    )
    (quarter1_title, quarter1_columns, quarter1_rows), (quarter2_title, quarter2_columns, quarter2_rows) = split_quarter_iya_tables(
        hc_idp_quarter_iya_columns,
        hc_idp_quarter_iya_rows,
    )
    production_group_1 = ["0386", "1864", "A868"]
    production_group_1_totals_after = {
        "0386": ("HP Total", ["0386", "C810"]),
        "1864": ("XQ Total", ["1864", "D352"]),
        "A868": ("TC Total", ["A868", "A673"]),
    }
    production_level_columns_1, production_level_rows_1 = build_production_data_table_by_plant_level(
        production_data_by_level_df,
        plant_order=production_group_1,
    )
    production_version_df = pd.DataFrame(data_bundle.get("production_version_compare", []))
    if not production_version_df.empty:
        keep = production_version_df["Plant"].fillna("").isin(["1864", "0386", "A868", "GC Total", ""])
        production_version_df = production_version_df[keep].reset_index(drop=True)
    production_version_columns, production_version_rows = build_production_version_comparison_table(production_version_df)
    production_version_styles = build_production_version_style_data_conditional(production_version_columns)

    production_data_weekly_df = pd.DataFrame(data_bundle.get("production_data_weekly", []))
    production_data_by_level_weekly_df = pd.DataFrame(data_bundle.get("production_data_by_level_weekly", []))
    weekly_plant_columns, weekly_plant_rows = build_production_data_table_by_plant_weekly(
        production_data_weekly_df,
        plant_order=production_group_1,
        include_segment_totals=False,
    )
    weekly_level_columns, weekly_level_rows = build_production_data_table_by_plant_level_weekly(
        production_data_by_level_weekly_df,
        plant_order=production_group_1,
        include_segment_totals=True,
        segment_totals_after=production_group_1_totals_after,
    )

    td_validation_columns, td_validation_rows = build_td_validation_table_from_detail(td_validation_detail)
    td_validation_styles = build_td_validation_style_data_conditional(td_validation_columns)

    overview_tab = dcc.Tab(
        label="Supply Protection",
        value="overview",
        className="page-tab",
        selected_className="page-tab--active",
        children=[
            html.Div(
                className="metrics",
                children=[
                    html.Div([
                        html.H4("ALL Protection MSU"),
                        html.Span(metrics["total_msu"], id="metric-total-msu", className="metric-value"),
                    ]),
                    html.Div([
                        html.H4("Actual PDE (<=0 days)"),
                        html.Span(metrics["pde_actual"], id="metric-item-count", className="metric-value warning"),
                    ]),
                    html.Div([
                        html.H4("PDE Alerts（Coming 7days）"),
                        html.Span(metrics["pde_open"], id="metric-pde-open", className="metric-value warning"),
                    ]),
                ],
            ),
            html.Div(
                className="charts-grid",
                children=[
                    html.Div(
                        className="role-card",
                        children=[
                            html.Div(
                                className="role-filter",
                                children=[
                                    html.Span("Role"),
                                    html.Div(
                                        dcc.RadioItems(
                                            id="role-filter",
                                            options=role_options,
                                            value=default_role,
                                            className="role-toggle",
                                        ),
                                        className="role-button-group",
                                    ),
                                ],
                            ),
                            dcc.Loading(
                                dcc.Graph(
                                    id="role-trend",
                                    figure=build_role_trend(monthly_requester, default_role),
                                )
                            ),
                        ],
                    ),
                    html.Div(
                        className="matrix-card",
                        children=[
                            dcc.Loading(
                                DataTable(
                                    id="role-item-table",
                                    columns=role_matrix_columns,
                                    data=role_matrix_data,
                                    style_header=PDE_STYLE_HEADER,
                                    style_cell=PDE_STYLE_CELL,
                                    style_data_conditional=[
                                        *PDE_STYLE_DATA_CONDITIONAL,
                                        {
                                            "if": {"filter_query": '{Role} = "Total" && {Item Text} = "Total"'},
                                            "fontWeight": "700",
                                            "backgroundColor": "#eaf2ff",
                                        },
                                        {
                                            "if": {"filter_query": '{Item Text} contains "Total"'},
                                            "fontWeight": "700",
                                            "backgroundColor": "#f0f5ff",
                                        },
                                    ],
                                    style_cell_conditional=[
                                        {"if": {"column_id": "Role"}, "textAlign": "left"},
                                        {"if": {"column_id": "Item Text"}, "textAlign": "left"},
                                    ],
                                    page_size=30,
                                    style_table={"overflowX": "auto"},
                                    tooltip_delay=2000,
                                    tooltip_duration=None,
                                    css=[
                                        {"selector": ".dash-tooltip .dash-table-tooltip", "rule": "max-width: none !important; min-width: 0 !important; width: max-content !important; background-color: #eef3fb !important; padding: 10px 14px !important; font-family: Century Gothic, Segoe UI, sans-serif; font-size: 13px; white-space: pre-line;"},
                                    ],
                                )
                            ),
                            html.Div(
                                style={"marginTop": "12px", "color": "#334155", "fontSize": "14px"},
                                children=[
                                    html.Strong("Item Text Description"),
                                    html.Ul(
                                        style={"margin": "6px 0 0 18px", "padding": "0"},
                                        children=[
                                            html.Li("R Quotation/R Component: FG Protection"),
                                            html.Li("R Material: Material Protection with rolling delay"),
                                            html.Li("FG Rolling: FG Protection with rolling delay"),
                                            html.Li("RM Material: RM Protection"),
                                        ],
                                    ),
                                ],
                            ),
                        ],
                    ),
                ],
            ),
            html.Div(
                className="pde-panel",
                children=[
                    html.H3("Past Due Alerts"),
                    DataTable(
                        id="pde-table",
                        columns=pde_columns,
                        data=pde_data,
                        style_header=PDE_STYLE_HEADER,
                        style_cell=PDE_STYLE_CELL,
                        style_data_conditional=PDE_STYLE_DATA_CONDITIONAL,
                        style_cell_conditional=[
                            {
                                "if": {"column_id": "Requester Email"},
                                "textAlign": "center",
                                "whiteSpace": "normal",
                                "height": "auto",
                                "minWidth": "180px",
                                "width": "auto",
                            },
                            {
                                "if": {"column_id": "Project"},
                                "textAlign": "center",
                                "whiteSpace": "normal",
                                "height": "auto",
                                "minWidth": "320px",
                                "width": "auto",
                            },
                        ],
                        sort_action="custom",
                        sort_mode="single",
                        sort_by=[{"column_id": TOTAL_LABEL, "direction": "desc"}],
                        tooltip_delay=200,
                        tooltip_duration=None,
                        page_size=10,
                        style_table={"overflowX": "auto"},
                    ),
                    html.H3("FG Rolling", style={"marginTop": "14px"}),
                    DataTable(
                        id="pde-fg-table",
                        columns=pde_fg_columns,
                        data=pde_fg_data,
                        style_header=PDE_STYLE_HEADER,
                        style_cell=PDE_STYLE_CELL,
                        style_data_conditional=PDE_STYLE_DATA_CONDITIONAL,
                        style_cell_conditional=[
                            {
                                "if": {"column_id": "Requester Email"},
                                "textAlign": "center",
                                "whiteSpace": "normal",
                                "height": "auto",
                                "minWidth": "180px",
                                "width": "auto",
                            },
                            {
                                "if": {"column_id": "Project"},
                                "textAlign": "center",
                                "whiteSpace": "normal",
                                "height": "auto",
                                "minWidth": "320px",
                                "width": "auto",
                            },
                        ],
                        sort_action="custom",
                        sort_mode="single",
                        sort_by=[{"column_id": TOTAL_LABEL, "direction": "desc"}],
                        tooltip_delay=200,
                        tooltip_duration=None,
                        page_size=10,
                        style_table={"overflowX": "auto"},
                    ),
                ],
            ),
        ],
    )

    drill_tab = dcc.Tab(
        label="Project Details",
        value="drill",
        className="page-tab",
        selected_className="page-tab--active",
        children=[
            html.Div(
                className="drill-panel",
                children=[
                    html.Div(
                        className="drill-header",
                        children=[
                            html.Div(
                                style={"display": "flex", "justifyContent": "space-between", "alignItems": "center", "gap": "12px"},
                                children=[
                                    html.H3("Role × Item × Project", style={"margin": "0"}),
                                    html.Div(
                                        dcc.RadioItems(
                                            id="drill-role-filter",
                                            options=[
                                                {"label": html.Span("ALL", className="role-chip-label"), "value": ROLE_ALL_VALUE},
                                                {"label": html.Span("NI", className="role-chip-label"), "value": "IOL"},
                                                {"label": html.Span("On Going", className="role-chip-label"), "value": "CSP"},
                                                {"label": html.Span("Cross Region", className="role-chip-label"), "value": "CROSS REGION"},
                                            ],
                                            value=ROLE_ALL_VALUE,
                                            className="role-toggle",
                                        ),
                                        className="role-button-group",
                                    ),
                                ],
                            ),
                            html.P("点击任意组合即可查看对应的所有物料请求明细"),
                            html.Div(
                                style={"display": "grid", "gridTemplateColumns": "minmax(280px, 1fr) minmax(240px, 0.8fr) minmax(200px, 0.7fr) minmax(200px, 0.7fr)", "gap": "10px", "maxWidth": "1400px"},
                                children=[
                                    dcc.Dropdown(
                                        id="drill-requester-filter",
                                        options=drill_requester_options,
                                        value=[],
                                        placeholder="筛选 Requester Email（可多选+搜索）",
                                        multi=True,
                                        clearable=True,
                                        searchable=True,
                                    ),
                                    dcc.Dropdown(
                                        id="drill-mrp-filter",
                                        options=drill_mrp_options,
                                        value=[],
                                        placeholder="筛选 MRP Element Indicator（可多选+搜索）",
                                        multi=True,
                                        clearable=True,
                                        searchable=True,
                                    ),
                                    dcc.Dropdown(
                                        id="drill-item-text-filter",
                                        options=drill_item_text_options,
                                        value=[],
                                        placeholder="筛选 Item Text（可多选+搜索）",
                                        multi=True,
                                        clearable=True,
                                        searchable=True,
                                    ),
                                    dcc.Input(
                                        id="drill-fuzzy-search",
                                        type="text",
                                        placeholder="关键词模糊搜索",
                                        debounce=True,
                                        style={
                                            "height": "36px",
                                            "padding": "0 12px",
                                            "border": "1px solid #d6e2f0",
                                            "borderRadius": "4px",
                                            "fontSize": "14px",
                                            "fontFamily": GLOBAL_FONT_FAMILY,
                                        },
                                    ),
                                ],
                            ),
                        ],
                    ),
                    dcc.Loading(
                        html.Div(
                            className="drill-table-wrapper",
                            children=DataTable(
                                id="role-item-mrp-summary",
                                columns=summary_drill_columns,
                                data=summary_drill_rows,
                                style_header=PDE_STYLE_HEADER,
                                style_cell=PDE_STYLE_CELL,
                                style_data_conditional=[
                                    *PDE_STYLE_DATA_CONDITIONAL,
                                    {
                                        "if": {"filter_query": '{Role} = "Total" && {Item Text} = "Total"'},
                                        "fontWeight": "700",
                                        "backgroundColor": "#eaf2ff",
                                    },
                                ],
                                page_size=10,
                                style_table={"overflowX": "auto"},
                                sort_action="native",
                                filter_action="native",
                            )
                        )
                    ),
                    html.Div(
                        className="drill-table-wrapper",
                        style={"marginTop": "8px"},
                        children=[
                            html.H4("Total（跨所有分页汇总）", style={"margin": "0 0 8px 0"}),
                            DataTable(
                                id="role-item-mrp-total-summary",
                                columns=summary_total_columns,
                                data=summary_total_rows,
                                style_header=PDE_STYLE_HEADER,
                                style_cell=PDE_STYLE_CELL,
                                style_data_conditional=[
                                    {
                                        "if": {"filter_query": '{Role} = "Total" && {Item Text} = "Total"'},
                                        "fontWeight": "700",
                                        "backgroundColor": "#eaf2ff",
                                    },
                                ],
                                page_action="none",
                                style_table={"overflowX": "auto"},
                            ),
                        ],
                    ),
                    html.Div(
                        className="drill-detail-panel",
                        children=[
                            html.Div(
                                className="drill-detail-header",
                                children=[
                                    html.H4("明细列表"),
                                    html.Span("请选择上方 Role × Item × Project 组合", id="drill-detail-title"),
                                    html.Button("下载全部明细", id="drill-detail-export-btn", n_clicks=0),
                                    dcc.Download(id="drill-detail-download"),
                                ],
                            ),
                            dcc.Loading(
                                html.Div(
                                    className="drill-table-wrapper",
                                    children=DataTable(
                                        id="drill-detail-table",
                                        columns=[{"name": field, "id": field} for field in DETAIL_VIEW_FIELDS],
                                        data=[],
                                        style_header=PDE_STYLE_HEADER,
                                        style_cell=PDE_STYLE_CELL,
                                        style_data_conditional=PDE_STYLE_DATA_CONDITIONAL,
                                        page_action="none",
                                        style_table={"maxHeight": "60vh", "overflowY": "auto", "overflowX": "auto"},
                                    ),
                                )
                            ),
                        ],
                    ),
                ],
            ),
        ],
    )

    demand_assumption_tab = dcc.Tab(
        label="Demand Assumption",
        value="demand-assumption",
        className="page-tab",
        selected_className="page-tab--active",
        children=[
            html.Div(
                className="demand-panel",
                children=[
                    html.Div(
                        className="demand-table-card",
                        children=[
                            html.H3("Demand System LBE"),
                            dcc.Loading(
                                DataTable(
                                    id="hc-idp-monthly-table",
                                    columns=hc_idp_columns,
                                    data=hc_idp_rows,
                                    style_header=PDE_STYLE_HEADER,
                                    style_cell=PDE_STYLE_CELL,
                                    style_data_conditional=PDE_STYLE_DATA_CONDITIONAL,
                                    page_size=10,
                                    style_table={"overflowX": "auto"},
                                    sort_action="native",
                                )
                            ),
                        ],
                    ),
                    html.Div(
                        className="demand-table-card",
                        children=[
                            html.H3("Demand System LBE IYA"),
                            dcc.Loading(
                                DataTable(
                                    id="hc-idp-monthly-iya-table",
                                    columns=hc_idp_iya_columns,
                                    data=hc_idp_iya_rows,
                                    style_header=PDE_STYLE_HEADER,
                                    style_cell=PDE_STYLE_CELL,
                                    style_data_conditional=PDE_STYLE_DATA_CONDITIONAL,
                                    page_size=10,
                                    style_table={"overflowX": "auto"},
                                    sort_action="native",
                                )
                            ),
                        ],
                    ),
                    html.Div(
                        className="demand-table-card",
                        children=[
                            html.H3("Demand System LBE + Supply System Protection"),
                            dcc.Loading(
                                DataTable(
                                    id="hc-idp-hs-table",
                                    columns=hc_idp_hs_columns,
                                    data=hc_idp_hs_rows,
                                    style_header=PDE_STYLE_HEADER,
                                    style_cell=PDE_STYLE_CELL,
                                    style_data_conditional=PDE_STYLE_DATA_CONDITIONAL,
                                    page_size=10,
                                    style_table={"overflowX": "auto"},
                                    sort_action="native",
                                )
                            ),
                        ],
                    ),
                    html.Div(
                        className="demand-table-card",
                        children=[
                            html.H3("Demand System LBE + Supply System Protection IYA"),
                            dcc.Loading(
                                DataTable(
                                    id="hc-idp-hs-iya-table",
                                    columns=hc_idp_hs_iya_columns,
                                    data=hc_idp_hs_iya_rows,
                                    style_header=PDE_STYLE_HEADER,
                                    style_cell=PDE_STYLE_CELL,
                                    style_data_conditional=PDE_STYLE_DATA_CONDITIONAL,
                                    page_size=10,
                                    style_table={"overflowX": "auto"},
                                    sort_action="native",
                                )
                            ),
                        ],
                    ),
                    html.Div(
                        className="demand-table-card demand-table-card--left",
                        children=[
                            html.H3("Supply Protection (PP + Base)"),
                            dcc.Loading(
                                DataTable(
                                    id="first-level-core-table",
                                    columns=level1_core_columns,
                                    data=level1_core_rows,
                                    style_header=PDE_STYLE_HEADER,
                                    style_cell=PDE_STYLE_CELL,
                                    style_data_conditional=PDE_STYLE_DATA_CONDITIONAL,
                                    page_size=10,
                                    style_table={"overflowX": "auto", "marginBottom": "16px"},
                                    sort_action="native",
                                )
                            ),
                            html.H3("Supply Protection (HKTW + ESS)"),
                            dcc.Loading(
                                DataTable(
                                    id="first-level-hktw-ess-table",
                                    columns=level1_hktw_ess_columns,
                                    data=level1_hktw_ess_rows,
                                    style_header=PDE_STYLE_HEADER,
                                    style_cell=PDE_STYLE_CELL,
                                    style_data_conditional=PDE_STYLE_DATA_CONDITIONAL,
                                    page_size=10,
                                    style_table={"overflowX": "auto"},
                                    sort_action="native",
                                )
                            ),
                        ],
                    ),
                    html.Div(
                        className="demand-table-card demand-table-card--right",
                        children=[
                            html.H3(quarter1_title),
                            dcc.Loading(
                                DataTable(
                                    id="hc-idp-quarter1-iya-table",
                                    columns=quarter1_columns,
                                    data=quarter1_rows,
                                    style_header=PDE_STYLE_HEADER,
                                    style_cell=PDE_STYLE_CELL,
                                    style_data_conditional=PDE_STYLE_DATA_CONDITIONAL,
                                    page_size=10,
                                    style_table={"overflowX": "auto", "marginBottom": "16px"},
                                    sort_action="native",
                                )
                            ),
                            html.H3(quarter2_title),
                            dcc.Loading(
                                DataTable(
                                    id="hc-idp-quarter2-iya-table",
                                    columns=quarter2_columns,
                                    data=quarter2_rows,
                                    style_header=PDE_STYLE_HEADER,
                                    style_cell=PDE_STYLE_CELL,
                                    style_data_conditional=PDE_STYLE_DATA_CONDITIONAL,
                                    page_size=10,
                                    style_table={"overflowX": "auto"},
                                    sort_action="native",
                                )
                            ),
                        ],
                    ),
                ],
            )
        ],
    )

    data_validation_tab = dcc.Tab(
        label="Demand Data",
        value="data-validation",
        className="page-tab",
        selected_className="page-tab--active",
        children=[
            html.Div(
                className="summary-panel",
                children=[
                    html.H3("TD Version Monthly Comparison"),
                    DataTable(
                        id="td-validation-table",
                        columns=td_validation_columns,
                        data=td_validation_rows,
                        hidden_columns=["Version Group"],
                        css=[{"selector": ".show-hide", "rule": "display: none"}],
                        style_header=PDE_STYLE_HEADER,
                        style_cell=PDE_STYLE_CELL,
                        style_data_conditional=td_validation_styles,
                        style_cell_conditional=[
                            {
                                "if": {"column_id": "Version"},
                                "textAlign": "left",
                                "whiteSpace": "normal",
                                "width": "auto",
                                "minWidth": "110px",
                                "maxWidth": "190px",
                            },
                            {"if": {"column_id": "Prod Line"}, "textAlign": "left"},
                        ],
                        page_size=20,
                        style_table={"overflowX": "auto"},
                    ),
                    html.Div(
                        style={
                            "marginTop": "18px",
                            "display": "grid",
                            "gridTemplateColumns": "1fr 1fr",
                            "gap": "14px",
                            "alignItems": "start",
                        },
                        children=[
                            html.Div(
                                style={"width": "100%", "minWidth": "0"},
                                children=[
                                    html.H4("Level2 GAP Details（请先点击上方 GAP 行）", id="td-gap-level2-title"),
                                    html.Div(
                                        style={"marginBottom": "10px"},
                                        children=[
                                            html.Button("Export Level2 Gap to Excel", id="td-gap-level2-export-btn", n_clicks=0),
                                            dcc.Download(id="td-gap-level2-download"),
                                        ],
                                    ),
                                    DataTable(
                                        id="td-gap-level2-table",
                                        columns=[
                                            {"name": "Prod Line", "id": "Prod Line"},
                                            {"name": "Level2", "id": "Level2"},
                                            {"name": "Current", "id": "Current"},
                                            {"name": "Previous", "id": "Previous"},
                                            {"name": "GAP", "id": "GAP"},
                                        ],
                                        data=[],
                                        style_header=PDE_STYLE_HEADER,
                                        style_cell=PDE_STYLE_CELL,
                                        style_data_conditional=PDE_STYLE_DATA_CONDITIONAL,
                                        style_cell_conditional=[
                                            {"if": {"column_id": "Prod Line"}, "textAlign": "left", "minWidth": "80px", "width": "100px"},
                                            {"if": {"column_id": "Level2"}, "textAlign": "left", "minWidth": "140px", "width": "180px"},
                                        ],
                                        sort_action="native",
                                        sort_mode="single",
                                        page_size=30,
                                        style_table={"overflowX": "auto", "width": "100%", "maxWidth": "100%"},
                                    ),
                                ],
                            ),
                            html.Div(
                                style={"width": "100%", "minWidth": "0"},
                                children=[
                                    html.H4("GAP Difference Details（请先点击上方 GAP 行）", id="td-gap-detail-title"),
                                    html.Div(
                                        style={"marginBottom": "10px"},
                                        children=[
                                            html.Button("Export Gap Details to Excel", id="td-gap-export-btn", n_clicks=0),
                                            dcc.Download(id="td-gap-detail-download"),
                                        ],
                                    ),
                                    DataTable(
                                        id="td-gap-detail-table",
                                        columns=[
                                            {"name": "APO Product", "id": "APO Product"},
                                            {"name": "Des", "id": "Des"},
                                            {"name": "Prod Line", "id": "Prod Line"},
                                            {"name": "Current", "id": "Current"},
                                            {"name": "Previous", "id": "Previous"},
                                            {"name": "GAP", "id": "GAP"},
                                        ],
                                        data=[],
                                        style_header=PDE_STYLE_HEADER,
                                        style_cell=PDE_STYLE_CELL,
                                        style_data_conditional=PDE_STYLE_DATA_CONDITIONAL,
                                        style_cell_conditional=[
                                            {"if": {"column_id": "APO Product"}, "textAlign": "left", "minWidth": "110px", "width": "120px", "maxWidth": "140px"},
                                            {"if": {"column_id": "Des"}, "textAlign": "left", "whiteSpace": "nowrap", "overflow": "hidden", "textOverflow": "ellipsis", "minWidth": "160px", "width": "220px", "maxWidth": "280px"},
                                            {"if": {"column_id": "Prod Line"}, "textAlign": "left", "minWidth": "80px", "width": "90px"},
                                        ],
                                        sort_action="native",
                                        sort_mode="single",
                                        sort_by=[{"column_id": "GAP", "direction": "asc"}],
                                        page_size=18,
                                        style_table={"overflowX": "auto", "width": "100%", "minWidth": "100%", "maxWidth": "100%"},
                                    ),
                                ],
                            ),
                        ],
                    ),
                    # ── Brand Dimension GAP Section ──
                    html.Hr(style={"margin": "24px 0 12px 0", "borderColor": "#e2e8f0"}),
                    html.Div(
                        style={"marginBottom": "10px", "display": "flex", "alignItems": "center", "gap": "16px"},
                        children=[
                            html.H3("Brand Dimension GAP", style={"margin": "0"}),
                            html.Label("显示维度：", style={"fontWeight": "600", "fontSize": "14px", "marginLeft": "20px"}),
                            dcc.Checklist(
                                id="brand-dim-checklist",
                                options=[
                                    {"label": "Brand", "value": "Brand"},
                                    {"label": "NI/Conversion", "value": "NI/Conversion"},
                                    {"label": "Variant", "value": "Variant"},
                                    {"label": "Size", "value": "Size"},
                                ],
                                value=["Brand", "NI/Conversion", "Variant", "Size"],
                                inline=True,
                                style={"display": "flex", "gap": "14px", "fontSize": "14px"},
                                inputStyle={"marginRight": "4px"},
                            ),
                        ],
                    ),
                    html.Div(
                        style={
                            "display": "grid",
                            "gridTemplateColumns": "1fr 2fr",
                            "gap": "14px",
                            "alignItems": "start",
                        },
                        children=[
                            html.Div(
                                style={"width": "100%", "minWidth": "0"},
                                children=[
                                    html.H4("Brand GAP Summary（请先点击上方 GAP 行）", id="td-gap-brand-summary-title"),
                                    html.Div(
                                        style={"marginBottom": "10px"},
                                        children=[
                                            html.Button("Export Brand Summary to Excel", id="td-gap-brand-summary-export-btn", n_clicks=0),
                                            dcc.Download(id="td-gap-brand-summary-download"),
                                        ],
                                    ),
                                    DataTable(
                                        id="td-gap-brand-summary-table",
                                        columns=[
                                            {"name": "Brand", "id": "Brand"},
                                            {"name": "Current", "id": "Current"},
                                            {"name": "Previous", "id": "Previous"},
                                            {"name": "GAP", "id": "GAP"},
                                        ],
                                        data=[],
                                        style_header=PDE_STYLE_HEADER,
                                        style_cell=PDE_STYLE_CELL,
                                        style_data_conditional=PDE_STYLE_DATA_CONDITIONAL,
                                        style_cell_conditional=[
                                            {"if": {"column_id": "Brand"}, "textAlign": "left", "minWidth": "100px", "width": "140px"},
                                        ],
                                        sort_action="native",
                                        sort_mode="single",
                                        page_size=15,
                                        style_table={"overflowX": "auto", "width": "100%", "maxWidth": "100%"},
                                    ),
                                ],
                            ),
                            html.Div(
                                style={"width": "100%", "minWidth": "0"},
                                children=[
                                    html.H4("Brand Dimension Detail（请先点击上方 GAP 行）", id="td-gap-brand-detail-title"),
                                    html.Div(
                                        style={"marginBottom": "10px"},
                                        children=[
                                            html.Button("Export Brand Detail to Excel", id="td-gap-brand-detail-export-btn", n_clicks=0),
                                            dcc.Download(id="td-gap-brand-detail-download"),
                                        ],
                                    ),
                                    DataTable(
                                        id="td-gap-brand-detail-table",
                                        columns=[
                                            {"name": "Brand", "id": "Brand"},
                                            {"name": "NI/Conversion", "id": "NI/Conversion"},
                                            {"name": "Variant", "id": "Variant"},
                                            {"name": "Size", "id": "Size"},
                                            {"name": "Prod Line", "id": "Prod Line"},
                                            {"name": "Current", "id": "Current"},
                                            {"name": "Previous", "id": "Previous"},
                                            {"name": "GAP", "id": "GAP"},
                                        ],
                                        data=[],
                                        style_header=PDE_STYLE_HEADER,
                                        style_cell=PDE_STYLE_CELL,
                                        style_data_conditional=PDE_STYLE_DATA_CONDITIONAL,
                                        style_cell_conditional=[
                                            {"if": {"column_id": "Brand"}, "textAlign": "left", "minWidth": "80px", "width": "100px"},
                                            {"if": {"column_id": "NI/Conversion"}, "textAlign": "left", "minWidth": "120px", "width": "180px"},
                                            {"if": {"column_id": "Variant"}, "textAlign": "left", "minWidth": "100px", "width": "160px"},
                                            {"if": {"column_id": "Size"}, "textAlign": "left", "minWidth": "60px", "width": "80px"},
                                            {"if": {"column_id": "Prod Line"}, "textAlign": "left", "minWidth": "70px", "width": "80px"},
                                        ],
                                        sort_action="native",
                                        sort_mode="single",
                                        filter_action="native",
                                        sort_by=[{"column_id": "GAP", "direction": "asc"}],
                                        page_size=20,
                                        style_table={"overflowX": "auto", "width": "100%", "maxWidth": "100%"},
                                    ),
                                ],
                            ),
                        ],
                    ),
                ],
            ),
        ],
    )

    production_data_tab = dcc.Tab(
        label="Production Data",
        value="production-data",
        className="page-tab",
        selected_className="page-tab--active",
        children=[
            html.Div(
                className="summary-panel",
                children=[
                    html.H3("Production Data"),
                    dcc.Tabs(
                        id="production-sub-tabs",
                        value="prod-summary",
                        className="sub-tabs",
                        children=[
                            dcc.Tab(
                                label="Summary",
                                value="prod-summary",
                                children=[
                                    html.Div(
                                        style={"paddingTop": "12px"},
                                        children=[
                                            html.H4("Production Data Version Comparison (By Plant)"),
                                            html.P(
                                                "Auto-detected from the two latest dated file sets in Production Volume folder.",
                                                style={"color": "#666", "fontSize": "13px", "marginBottom": "8px"},
                                            ),
                                            DataTable(
                                                id="production-version-compare-table",
                                                columns=production_version_columns,
                                                data=production_version_rows,
                                                style_header=PDE_STYLE_HEADER,
                                                style_cell=PDE_STYLE_CELL,
                                                style_data_conditional=production_version_styles,
                                                style_cell_conditional=[
                                                    {"if": {"column_id": "Plant"}, "textAlign": "left"},
                                                    {"if": {"column_id": "Version"}, "textAlign": "left"},
                                                    {"if": {"column_id": "Version Group"}, "display": "none"},
                                                ],
                                                style_header_conditional=[
                                                    {"if": {"column_id": "Version Group"}, "display": "none"},
                                                ],
                                                page_action="none",
                                                style_table={"overflowX": "auto"},
                                                sort_action="none",
                                                filter_action="none",
                                            ),
                                            html.H4("Production Data (By Plant / Level1 / Level2) - Table 2", style={"marginTop": "18px"}),
                                            DataTable(
                                                id="production-data-level-table-2",
                                                columns=production_level_columns_1,
                                                data=production_level_rows_1,
                                                style_header=PDE_STYLE_HEADER,
                                                style_cell=PDE_STYLE_CELL,
                                                style_data_conditional=[
                                                    *PDE_STYLE_DATA_CONDITIONAL,
                                                    {
                                                        "if": {
                                                            "filter_query": '{Plant} = "HP Total" || {Plant} = "TC Total" || {Plant} = "XQ Total"'
                                                        },
                                                        "fontWeight": "700",
                                                        "backgroundColor": "#f3f8ff",
                                                    },
                                                    {
                                                        "if": {"filter_query": '{Plant} = "GC Total"'},
                                                        "fontWeight": "700",
                                                        "backgroundColor": "#edf4ff",
                                                    },
                                                ],
                                                style_cell_conditional=[
                                                    {"if": {"column_id": "Plant"}, "textAlign": "left"},
                                                    {"if": {"column_id": "Level1"}, "textAlign": "left"},
                                                    {"if": {"column_id": "Level2"}, "textAlign": "left"},
                                                ],
                                                page_action="none",
                                                style_table={"overflowX": "auto"},
                                                sort_action="native",
                                                filter_action="none",
                                            ),
                                            html.H4("Weekly Production Data (By Plant) - Table 3", style={"marginTop": "24px"}),
                                            DataTable(
                                                id="production-weekly-plant-table-3",
                                                columns=weekly_plant_columns,
                                                data=weekly_plant_rows,
                                                style_header=PDE_STYLE_HEADER,
                                                style_cell=PDE_STYLE_CELL,
                                                style_data_conditional=[
                                                    *PDE_STYLE_DATA_CONDITIONAL,
                                                    {
                                                        "if": {
                                                            "filter_query": '{Plant} = "HP Total" || {Plant} = "TC Total" || {Plant} = "XQ Total"'
                                                        },
                                                        "fontWeight": "700",
                                                        "backgroundColor": "#f3f8ff",
                                                    },
                                                    {
                                                        "if": {"filter_query": '{Plant} = "GC Total"'},
                                                        "fontWeight": "700",
                                                        "backgroundColor": "#edf4ff",
                                                    },
                                                ],
                                                style_cell_conditional=[
                                                    {"if": {"column_id": "Plant"}, "textAlign": "left"},
                                                ],
                                                page_action="none",
                                                style_table={"overflowX": "auto"},
                                                sort_action="none",
                                                filter_action="none",
                                            ),
                                            html.H4("Weekly Production Data (By Plant / Level1 / Level2) - Table 4", style={"marginTop": "18px"}),
                                            DataTable(
                                                id="production-weekly-level-table-4",
                                                columns=weekly_level_columns,
                                                data=weekly_level_rows,
                                                style_header=PDE_STYLE_HEADER,
                                                style_cell=PDE_STYLE_CELL,
                                                style_data_conditional=[
                                                    *PDE_STYLE_DATA_CONDITIONAL,
                                                    {
                                                        "if": {
                                                            "filter_query": '{Plant} = "HP Total" || {Plant} = "TC Total" || {Plant} = "XQ Total"'
                                                        },
                                                        "fontWeight": "700",
                                                        "backgroundColor": "#f3f8ff",
                                                    },
                                                    {
                                                        "if": {"filter_query": '{Plant} = "GC Total"'},
                                                        "fontWeight": "700",
                                                        "backgroundColor": "#edf4ff",
                                                    },
                                                ],
                                                style_cell_conditional=[
                                                    {"if": {"column_id": "Plant"}, "textAlign": "left"},
                                                    {"if": {"column_id": "Level1"}, "textAlign": "left"},
                                                    {"if": {"column_id": "Level2"}, "textAlign": "left"},
                                                ],
                                                page_action="none",
                                                style_table={"overflowX": "auto"},
                                                sort_action="native",
                                                filter_action="none",
                                            ),
                                        ],
                                    ),
                                ],
                            ),
                            dcc.Tab(
                                label="Detail by Brand/Size/Variant",
                                value="prod-detail-dim",
                                children=[
                                    html.Div(
                                        style={"paddingTop": "12px"},
                                        children=[
                                            html.H4("TD Demand by Dimension (Brand / Size / Variant)"),
                                            html.P(
                                                "Select dimensions to group by and filter values. Data from the latest TD Report (MSU).",
                                                style={"color": "#666", "fontSize": "13px", "marginBottom": "10px"},
                                            ),
                                            html.Div(
                                                style={
                                                    "display": "flex",
                                                    "flexWrap": "wrap",
                                                    "gap": "12px",
                                                    "marginBottom": "14px",
                                                    "alignItems": "flex-end",
                                                },
                                                children=[
                                                    html.Div(
                                                        style={"minWidth": "200px", "flex": "1"},
                                                        children=[
                                                            html.Label("Group By", style={"fontWeight": "600", "fontSize": "13px"}),
                                                            dcc.Dropdown(
                                                                id="prod-dim-group-by",
                                                                options=[{"label": d, "value": d} for d in _PROD_DIM_COLS],
                                                                value=["Plant", "Brand", "Size", "Variant"],
                                                                multi=True,
                                                                placeholder="Select dimensions to group by...",
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        style={"minWidth": "120px", "flex": "1"},
                                                        children=[
                                                            html.Label("Plant", style={"fontWeight": "600", "fontSize": "13px"}),
                                                            dcc.Dropdown(
                                                                id="prod-dim-filter-plant",
                                                                options=[],
                                                                value=[],
                                                                multi=True,
                                                                placeholder="All",
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        style={"minWidth": "150px", "flex": "1"},
                                                        children=[
                                                            html.Label("Brand", style={"fontWeight": "600", "fontSize": "13px"}),
                                                            dcc.Dropdown(
                                                                id="prod-dim-filter-brand",
                                                                options=[],
                                                                value=[],
                                                                multi=True,
                                                                placeholder="All",
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        style={"minWidth": "120px", "flex": "1"},
                                                        children=[
                                                            html.Label("Size", style={"fontWeight": "600", "fontSize": "13px"}),
                                                            dcc.Dropdown(
                                                                id="prod-dim-filter-size",
                                                                options=[],
                                                                value=[],
                                                                multi=True,
                                                                placeholder="All",
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        style={"minWidth": "120px", "flex": "1"},
                                                        children=[
                                                            html.Label("Variant", style={"fontWeight": "600", "fontSize": "13px"}),
                                                            dcc.Dropdown(
                                                                id="prod-dim-filter-variant",
                                                                options=[],
                                                                value=[],
                                                                multi=True,
                                                                placeholder="All",
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        style={"minWidth": "120px", "flex": "1"},
                                                        children=[
                                                            html.Label("Prod Line", style={"fontWeight": "600", "fontSize": "13px"}),
                                                            dcc.Dropdown(
                                                                id="prod-dim-filter-prodline",
                                                                options=[],
                                                                value=[],
                                                                multi=True,
                                                                placeholder="All",
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        style={"minWidth": "100px", "flex": "1"},
                                                        children=[
                                                            html.Label("Type", style={"fontWeight": "600", "fontSize": "13px"}),
                                                            dcc.Dropdown(
                                                                id="prod-dim-filter-type",
                                                                options=[],
                                                                value=[],
                                                                multi=True,
                                                                placeholder="All",
                                                            ),
                                                        ],
                                                    ),
                                                ],
                                            ),
                                            DataTable(
                                                id="production-dim-detail-table",
                                                columns=[],
                                                data=[],
                                                style_header=PDE_STYLE_HEADER,
                                                style_cell=PDE_STYLE_CELL,
                                                style_data_conditional=[
                                                    *PDE_STYLE_DATA_CONDITIONAL,
                                                    {
                                                        "if": {"filter_query": '{Plant} = "Total" || {Brand} = "Total" || {Lineup} = "Total" || {Size} = "Total" || {Variant} = "Total" || {Type} = "Total" || {Prod Line} = "Total"'},
                                                        "fontWeight": "700",
                                                        "backgroundColor": "#f3f8ff",
                                                    },
                                                ],
                                                style_cell_conditional=[
                                                    {"if": {"column_id": d}, "textAlign": "left"} for d in _PROD_DIM_COLS
                                                ],
                                                page_action="none",
                                                style_table={"overflowX": "auto"},
                                                sort_action="native",
                                                filter_action="none",
                                            ),
                                        ],
                                    ),
                                ],
                            ),
                        ],
                    ),
                ],
            ),
        ],
    )

    return html.Div(
        className="page",
        children=[
            dcc.Interval(id="refresh-interval", interval=15 * 60 * 1000, n_intervals=0),
            dcc.Interval(id="pipeline-progress-interval", interval=1000, n_intervals=0, disabled=True),
            dcc.Interval(id="force-refresh-poll", interval=5000, n_intervals=0),
            dcc.Store(id="data-store", data=data_bundle),
            dcc.Store(id="details-version-store", data={"version": details_version}),
            dcc.Store(id="data-version-store", data=_read_data_version()),
            # --- hidden placeholders that admin callbacks target ---
            dcc.Dropdown(id="refresh-scope-dropdown", value="all", style={"display": "none"}),
            html.Button(id="manual-refresh-btn", n_clicks=0, style={"display": "none"}),
            html.Div(id="pipeline-progress-container", style={"display": "none"}),
            html.Div(id="pipeline-progress-fill", style={"display": "none"}),
            html.Span("", id="pipeline-progress-pct", style={"display": "none"}),
            html.Div("", id="pipeline-progress-text", style={"display": "none"}),
            html.Span("", id="manual-refresh-status", style={"display": "none"}),
            html.Button(id="backup-snapshot-btn", n_clicks=0, style={"display": "none"}),
            html.Span("", id="backup-snapshot-status", style={"display": "none"}),
            dcc.Download(id="backup-snapshot-download"),
            html.Div(
                className="hero",
                children=[
                    html.Div(
                        [
                            html.H1("Hair Care Supply Protection Command Center"),
                        ]
                    ),
                    html.Div(
                        style={"display": "flex", "gap": "6px", "marginLeft": "auto", "alignSelf": "center", "alignItems": "center"},
                        children=[
                            html.Span(
                                id="last-refresh-badge",
                                children=f"Data updated: {_dv_text}",
                                title="Time the dashboard data was last refreshed by the pipeline. "
                                      "Green = refreshed today; amber = today's scheduled refresh has not run yet. "
                                      "Open tabs reload automatically when new data arrives.",
                                style={
                                    "fontSize": "12px",
                                    "fontWeight": "600",
                                    "padding": "4px 10px",
                                    "borderRadius": "999px",
                                    "whiteSpace": "nowrap",
                                    "color": "#065f46" if _dv_today else "#92400e",
                                    "backgroundColor": "#d1fae5" if _dv_today else "#fef3c7",
                                    "border": f"1px solid {'#6ee7b7' if _dv_today else '#fcd34d'}",
                                },
                            ),
                            html.A(
                                html.Span("\U0001F4D6", style={
                                    "fontSize": "18px",
                                    "color": "#94a3b8",
                                    "cursor": "pointer",
                                    "padding": "4px 8px",
                                    "borderRadius": "8px",
                                    "transition": "color 0.2s",
                                }),
                                href="/docs/user-guide",
                                target="_blank",
                                title="User Guide",
                                style={"textDecoration": "none"},
                            ),
                            html.A(
                                html.Span("\u2699", style={
                                    "fontSize": "22px",
                                    "color": "#94a3b8",
                                    "cursor": "pointer",
                                    "padding": "4px 8px",
                                    "borderRadius": "8px",
                                    "transition": "color 0.2s",
                                }),
                                href="/admin",
                                title="Admin Panel",
                                style={"textDecoration": "none"},
                            ),
                        ],
                    ),
                ],
            ),
            dcc.Tabs(
                id="page-tabs",
                value="demand-assumption",
                children=[demand_assumption_tab, production_data_tab, overview_tab, drill_tab, data_validation_tab],
            ),
        ],
    )


def build_admin_layout(cfg: AppConfig) -> html.Div:
    """Build the /admin page layout with login gate and management panel."""
    return html.Div(
        className="admin-page",
        children=[
            dcc.Store(id="admin-session", storage_type="session", data={"authenticated": False}),
            # ── Login form (visible when not authenticated) ──
            html.Div(
                id="admin-login-box",
                className="admin-login-box",
                children=[
                    html.H2("Admin Login", style={"margin": "0 0 16px 0", "color": "#1e3a8a"}),
                    dcc.Input(
                        id="admin-password-input",
                        type="password",
                        placeholder="Enter password",
                        className="admin-input",
                        n_submit=0,
                    ),
                    html.Button("Login", id="admin-login-btn", n_clicks=0, className="admin-btn admin-btn--primary"),
                    html.Span("", id="admin-login-error", style={"color": "#dc2626", "fontSize": "13px", "marginTop": "6px"}),
                ],
            ),
            # ── Admin panel (hidden until authenticated) ──
            html.Div(
                id="admin-panel",
                className="admin-panel",
                style={"display": "none"},
                children=[
                    html.Div(
                        className="admin-header",
                        children=[
                            html.Div([
                                html.H1("Admin Panel", style={"margin": "0", "color": "#1e3a8a"}),
                                html.Span(
                                    f"Git: {_get_git_version()}",
                                    style={"fontSize": "12px", "color": "#6b7280", "marginTop": "4px", "display": "block"},
                                ),
                            ]),
                            html.A(
                                html.Button("Back to Dashboard", className="admin-btn"),
                                href="/",
                                style={"textDecoration": "none"},
                            ),
                        ],
                    ),
                    # ── Card 1: Run Pipeline & Refresh ──
                    html.Div(
                        className="admin-card",
                        children=[
                            html.H3("\U0001F504 Run Pipeline & Refresh"),
                            html.P("Run the data pipeline and refresh dashboard data."),
                            html.Div(
                                style={"display": "flex", "gap": "8px", "alignItems": "center", "flexWrap": "wrap"},
                                children=[
                                    dcc.Dropdown(
                                        id="admin-refresh-scope",
                                        options=[
                                            {"label": info["label"], "value": key}
                                            for key, info in REFRESH_GROUPS.items()
                                        ],
                                        value="all",
                                        clearable=False,
                                        style={"width": "200px", "fontSize": "13px"},
                                    ),
                                    html.Button("Run Pipeline", id="admin-run-pipeline-btn", n_clicks=0, className="admin-btn admin-btn--primary"),
                                ],
                            ),
                            html.Div(
                                id="admin-pipeline-progress",
                                style={"marginTop": "10px", "display": "none"},
                                children=[
                                    html.Div(
                                        style={
                                            "background": "#e5e7eb", "borderRadius": "4px",
                                            "height": "18px", "overflow": "hidden",
                                            "position": "relative", "width": "100%",
                                        },
                                        children=[
                                            html.Div(id="admin-progress-fill", style={
                                                "width": "0%", "height": "100%",
                                                "backgroundColor": "#3b82f6", "borderRadius": "4px",
                                                "transition": "width 0.4s ease",
                                            }),
                                            html.Span("0%", id="admin-progress-pct", style={
                                                "position": "absolute", "top": "0", "left": "50%",
                                                "transform": "translateX(-50%)", "fontSize": "11px",
                                                "lineHeight": "18px", "color": "#1f2937", "fontWeight": "600",
                                            }),
                                        ],
                                    ),
                                    html.Div("", id="admin-progress-text", style={
                                        "fontSize": "12px", "color": "#6b7280",
                                        "marginTop": "2px", "textAlign": "center",
                                    }),
                                ],
                            ),
                            dcc.Interval(id="admin-pipeline-interval", interval=1000, n_intervals=0, disabled=True),
                            html.Span("", id="admin-pipeline-status", style={"fontSize": "13px", "color": "#16a34a", "marginTop": "6px", "display": "block"}),
                        ],
                    ),
                    # ── Card 2: Refresh Data ──
                    html.Div(
                        className="admin-card",
                        children=[
                            html.H3("\U0001F4C4 Refresh Data"),
                            html.P("Reload dashboard data from processed CSV files without re-running the pipeline."),
                            html.Button("Refresh Data", id="admin-refresh-data-btn", n_clicks=0, className="admin-btn admin-btn--primary"),
                            html.Span("", id="admin-refresh-data-status", style={"fontSize": "13px", "color": "#334155", "marginTop": "6px", "display": "block"}),
                        ],
                    ),
                    # ── Card 3: Backup Snapshot ──
                    html.Div(
                        className="admin-card",
                        children=[
                            html.H3("\U0001F4BE Backup Snapshot"),
                            html.P("Export all dashboard tables to an Excel snapshot."),
                            html.Button("Create Backup", id="admin-backup-btn", n_clicks=0, className="admin-btn admin-btn--primary"),
                            dcc.Download(id="admin-backup-download"),
                            html.Span("", id="admin-backup-status", style={"fontSize": "13px", "color": "#334155", "marginTop": "6px", "display": "block"}),
                        ],
                    ),
                    # ── Card 4: Weekly Mail Preview ──
                    html.Div(
                        className="admin-card",
                        children=[
                            html.H3("\U00002709 Weekly Mail Preview"),
                            html.P("Regenerate and open the weekly mail HTML preview."),
                            html.A(
                                html.Button("Refresh Mail & Open", id="admin-mail-btn", n_clicks=0, className="admin-btn admin-btn--primary"),
                                href="/mail-preview/latest",
                                target="_blank",
                                style={"textDecoration": "none"},
                            ),
                        ],
                    ),
                    # ── Card 5: Update & Restart ──
                    html.Div(
                        className="admin-card",
                        children=[
                            html.H3("\U0001F680 Update & Restart"),
                            html.P("Pull latest code from GitHub, install dependencies, and restart the application. After restart, pipeline will run automatically."),
                            html.Button("Update & Restart", id="admin-update-btn", n_clicks=0, className="admin-btn admin-btn--danger"),
                            html.Span("", id="admin-update-status", style={"fontSize": "13px", "color": "#334155", "marginTop": "6px", "display": "block", "whiteSpace": "pre-wrap"}),
                        ],
                    ),
                    # ── Card 6: Master Data Update ──
                    html.Div(
                        className="admin-card",
                        children=[
                            html.H3("\U0001F50D Master Data Update"),
                            html.P("Scan Production Volume data for materials missing Seg mapping or SU Factor in Parameter."),
                            html.Div(
                                style={"display": "flex", "gap": "8px", "alignItems": "center", "flexWrap": "wrap"},
                                children=[
                                    html.Button("Scan Missing Data", id="admin-masterdata-btn", n_clicks=0, className="admin-btn admin-btn--primary"),
                                    html.Button("Export to Excel", id="admin-masterdata-export-btn", n_clicks=0, className="admin-btn"),
                                ],
                            ),
                            dcc.Download(id="admin-masterdata-download"),
                            html.Span("", id="admin-masterdata-status", style={"fontSize": "13px", "color": "#334155", "marginTop": "6px", "display": "block"}),
                            dcc.Loading(
                                html.Div(
                                    id="admin-masterdata-table-wrapper",
                                    style={"marginTop": "10px"},
                                    children=[],
                                ),
                            ),
                            dcc.Store(id="admin-masterdata-store", data=None),
                        ],
                    ),
                    # ── Card 7: Data Source Status ──
                    html.Div(
                        className="admin-card",
                        children=[
                            html.H3("\U0001F4CB Data Source Status"),
                            html.P("Check timestamps of all data source files to verify they are up to date."),
                            html.Button("Scan Data Sources", id="admin-datasource-btn", n_clicks=0, className="admin-btn admin-btn--primary"),
                            html.Span("", id="admin-datasource-status", style={"fontSize": "13px", "color": "#334155", "marginTop": "6px", "display": "block"}),
                            dcc.Loading(
                                html.Div(
                                    id="admin-datasource-table-wrapper",
                                    style={"marginTop": "10px"},
                                    children=[],
                                ),
                            ),
                        ],
                    ),
                ],
            ),
        ],
    )


def register_callbacks(app: Dash, cfg: AppConfig) -> None:

    # ── unified refresh + pipeline progress callback ──────────────
    @app.callback(
        Output("data-store", "data"),
        Output("details-version-store", "data"),
        Output("manual-refresh-status", "children"),
        Output("pipeline-progress-interval", "disabled"),
        Output("pipeline-progress-container", "style"),
        Output("pipeline-progress-fill", "style"),
        Output("pipeline-progress-pct", "children"),
        Output("pipeline-progress-text", "children"),
        Output("manual-refresh-btn", "disabled"),
        Input("refresh-interval", "n_intervals"),
        Input("manual-refresh-btn", "n_clicks"),
        Input("pipeline-progress-interval", "n_intervals"),
        State("refresh-scope-dropdown", "value"),
        State("data-store", "data"),
        prevent_initial_call=True,
    )
    def refresh_data(n_intervals, n_clicks, progress_ticks, scope, existing_data):
        from dash import ctx

        trigger = ctx.triggered_id
        _FILL_BASE = {
            "height": "100%",
            "backgroundColor": "#3b82f6",
            "borderRadius": "4px",
            "transition": "width 0.4s ease",
        }
        _HIDE = {"display": "none", "width": "100%"}
        _SHOW = {"display": "block", "width": "100%"}

        # ── 1) Manual click → launch pipeline subprocess ──
        if trigger == "manual-refresh-btn":
            group = scope or "all"
            err = _start_pipeline_subprocess(group)
            if err:
                # Blocked by cooldown or invalid group
                return (
                    dash.no_update, dash.no_update,
                    f"⚠️ {err}",
                    True, _HIDE,
                    dash.no_update, dash.no_update, dash.no_update,
                    False,
                )
            label = REFRESH_GROUPS.get(group, {}).get("label", group)
            ts = datetime.now().strftime("%H:%M:%S")
            return (
                dash.no_update,                        # data-store (unchanged while running)
                dash.no_update,                        # details-version-store
                f"⏳ Pipeline started at {ts} ({label})",
                False,                                 # enable progress-interval
                _SHOW,                                 # show progress container
                {**_FILL_BASE, "width": "5%"},         # initial fill
                "0%",                                  # pct label
                f"Preparing {label} …",                # text
                True,                                  # disable button while running
            )

        # ── 2) Progress polling tick ──
        if trigger == "pipeline-progress-interval":
            progress = _read_pipeline_progress()

            if progress is None:
                return (
                    dash.no_update, dash.no_update, dash.no_update,
                    False, _SHOW,
                    {**_FILL_BASE, "width": "5%"}, "…",
                    "Waiting for pipeline to start …",
                    True,
                )

            status = progress.get("status", "unknown")
            done = progress.get("stages_done", 0)
            total = max(progress.get("stages_total", 1), 1)
            pct = int(done / total * 100)
            current_label = progress.get("current_stage_label", "")

            if status == "running":
                return (
                    dash.no_update, dash.no_update, dash.no_update,
                    False, _SHOW,
                    {**_FILL_BASE, "width": f"{max(pct, 5)}%"},
                    f"{pct}%",
                    f"Running: {current_label} ({done}/{total})",
                    True,
                )

            if status == "completed":
                # Pipeline finished → reload data from new CSVs
                bundle = load_data_bundle(cfg)
                _write_data_version()  # notify all browsers
                ts = datetime.now().strftime("%H:%M:%S")
                completed_stages = progress.get("completed_stages", [])
                labels = ", ".join(
                    REFRESH_GROUPS.get(s, {}).get("label", s) for s in completed_stages
                )
                return (
                    bundle,
                    {"version": bundle.get("request_details_version")},
                    f"✓ Pipeline completed at {ts} — {labels}",
                    True,                              # stop polling
                    _HIDE,                             # hide progress bar
                    {**_FILL_BASE, "width": "100%"},
                    "100%",
                    "",
                    False,                             # re-enable button
                )

            if status == "error":
                error_msg = progress.get("error_message", "Unknown error")
                failed = progress.get("current_stage_label", "")
                ts = datetime.now().strftime("%H:%M:%S")
                return (
                    dash.no_update, dash.no_update,
                    f"✗ Pipeline failed at {ts} ({failed}): {error_msg}",
                    True,                              # stop polling
                    _HIDE,
                    {**_FILL_BASE, "width": "0%", "backgroundColor": "#ef4444"},
                    "",
                    "",
                    False,
                )

            # unknown status – keep polling
            return (
                dash.no_update, dash.no_update, dash.no_update,
                False, _SHOW,
                {**_FILL_BASE, "width": "5%"}, "…",
                "Checking pipeline …",
                True,
            )

        # ── 3) Auto-refresh (15-min interval) → just reload CSVs ──
        bundle = load_data_bundle(cfg)
        ts = datetime.now().strftime("%H:%M:%S")
        return (
            bundle,
            {"version": bundle.get("request_details_version")},
            f"Auto-refreshed at {ts}",
            dash.no_update,
            dash.no_update,
            dash.no_update,
            dash.no_update,
            dash.no_update,
            dash.no_update,
        )

    # ── Data-version poll (detects server-side data changes) ────
    @app.callback(
        Output("data-store", "data", allow_duplicate=True),
        Output("details-version-store", "data", allow_duplicate=True),
        Output("data-version-store", "data", allow_duplicate=True),
        Input("force-refresh-poll", "n_intervals"),
        State("data-version-store", "data"),
        prevent_initial_call=True,
    )
    def force_refresh_poll(n, client_version):
        server_version = _read_data_version()
        if not server_version or server_version == (client_version or ""):
            raise PreventUpdate
        bundle = load_data_bundle(cfg)
        return bundle, {"version": bundle.get("request_details_version")}, server_version

    @app.callback(
        Output("metric-total-msu", "children"),
        Output("metric-item-count", "children"),
        Output("metric-pde-open", "children"),
        Input("data-store", "data"),
    )
    def update_metrics(data):
        monthly_item = pd.DataFrame(data.get("monthly_item", []))
        pde_alerts = pd.DataFrame(data.get("pde_alerts", []))
        request_details = load_request_details(cfg)
        metrics = compute_metrics(monthly_item, pde_alerts, request_details)
        return metrics["total_msu"], metrics["pde_actual"], metrics["pde_open"]

    @app.callback(
        Output("role-item-table", "columns"),
        Output("role-item-table", "data"),
        Output("role-item-table", "tooltip_data"),
        Output("role-trend", "figure"),
        Output("pde-table", "columns"),
        Output("pde-table", "data"),
        Output("pde-fg-table", "columns"),
        Output("pde-fg-table", "data"),
        Output("drill-requester-filter", "options"),
        Output("drill-requester-filter", "value"),
        Output("drill-mrp-filter", "options"),
        Output("drill-mrp-filter", "value"),
        Output("drill-item-text-filter", "options"),
        Output("drill-item-text-filter", "value"),
        Output("role-item-mrp-summary", "columns"),
        Output("role-item-mrp-summary", "data"),
        Output("role-item-mrp-total-summary", "columns"),
        Output("role-item-mrp-total-summary", "data"),
        Output("hc-idp-monthly-table", "columns"),
        Output("hc-idp-monthly-table", "data"),
        Output("hc-idp-monthly-iya-table", "columns"),
        Output("hc-idp-monthly-iya-table", "data"),
        Output("hc-idp-hs-table", "columns"),
        Output("hc-idp-hs-table", "data"),
        Output("hc-idp-hs-iya-table", "columns"),
        Output("hc-idp-hs-iya-table", "data"),
        Output("hc-idp-quarter1-iya-table", "columns"),
        Output("hc-idp-quarter1-iya-table", "data"),
        Output("hc-idp-quarter2-iya-table", "columns"),
        Output("hc-idp-quarter2-iya-table", "data"),
        Output("first-level-core-table", "columns"),
        Output("first-level-core-table", "data"),
        Output("first-level-hktw-ess-table", "columns"),
        Output("first-level-hktw-ess-table", "data"),
        Output("td-validation-table", "columns"),
        Output("td-validation-table", "data"),
        Output("td-validation-table", "style_data_conditional"),
        Output("production-version-compare-table", "columns"),
        Output("production-version-compare-table", "data"),
        Output("production-version-compare-table", "style_data_conditional"),
        Output("production-data-level-table-2", "columns"),
        Output("production-data-level-table-2", "data"),
        Output("production-weekly-plant-table-3", "columns"),
        Output("production-weekly-plant-table-3", "data"),
        Output("production-weekly-level-table-4", "columns"),
        Output("production-weekly-level-table-4", "data"),
        Input("data-store", "data"),
        Input("role-filter", "value"),
        Input("drill-role-filter", "value"),
        Input("drill-requester-filter", "value"),
        Input("drill-mrp-filter", "value"),
        Input("drill-item-text-filter", "value"),
        Input("drill-fuzzy-search", "value"),
    )
    def update_visuals(
        data,
        role_value,
        drill_role_value,
        drill_requester_value,
        drill_mrp_value,
        drill_item_text_value,
        drill_fuzzy_search,
    ):
        monthly_requester = pd.DataFrame(data.get("monthly_requester", []))
        monthly_level1 = pd.DataFrame(data.get("monthly_level1", []))
        hc_idp_monthly = pd.DataFrame(data.get("hc_idp_monthly", []))
        production_data_df = pd.DataFrame(data.get("production_data", []))
        production_data_by_level_df = pd.DataFrame(data.get("production_data_by_level", []))
        td_validation_detail = load_dataset(cfg.processed_dir, "td_version_gap_details.csv")
        historical_shipment = pd.DataFrame(data.get("historical_shipment", []))
        pde_alerts = pd.DataFrame(data.get("pde_alerts", []))
        request_details = load_request_details(cfg)
        selected_role = role_value or ROLE_ALL_VALUE
        table_columns, table_data = build_monthly_matrix(monthly_requester, selected_role)

        # Generate tooltip data for role-item-table with project/owner/MSU details
        table_tooltip_data = _build_matrix_tooltip_data(table_columns, table_data, request_details)

        selected_drill_role = drill_role_value or ROLE_ALL_VALUE
        drill_requester_options = build_requester_email_options(request_details, selected_drill_role)
        selected_requesters = normalize_requester_values(drill_requester_value)
        valid_requesters = [
            requester for requester in selected_requesters
            if any(option.get("value") == requester for option in drill_requester_options)
        ]
        drill_mrp_options = build_mrp_indicator_options(request_details, selected_drill_role, valid_requesters)
        selected_mrp_indicators = normalize_mrp_values(drill_mrp_value)
        valid_mrp_indicators = [
            indicator for indicator in selected_mrp_indicators
            if any(option.get("value") == indicator for option in drill_mrp_options)
        ]
        drill_item_text_options = build_item_text_options(request_details, selected_drill_role, valid_requesters, valid_mrp_indicators)
        selected_item_texts = normalize_mrp_values(drill_item_text_value)
        valid_item_texts = [
            item for item in selected_item_texts
            if any(option.get("value") == item for option in drill_item_text_options)
        ]
        role_fig = build_role_trend(monthly_requester, selected_role)
        pde_columns, pde_records, pde_fg_columns, pde_fg_records = build_pde_tables(pde_alerts, request_details)
        pde_records = _sort_pde_records_keep_total_last(pde_records, [{"column_id": TOTAL_LABEL, "direction": "desc"}])
        pde_fg_records = _sort_pde_records_keep_total_last(pde_fg_records, [{"column_id": TOTAL_LABEL, "direction": "desc"}])
        drill_columns, drill_rows = build_role_item_project_summary(
            request_details,
            selected_drill_role,
            valid_requesters,
            valid_mrp_indicators,
            valid_item_texts,
        )
        drill_total_columns, drill_total_rows, drill_rows = extract_role_item_project_total_row(drill_columns, drill_rows)

        # Apply fuzzy keyword filter to both drill rows and total row
        fuzzy_keyword = (drill_fuzzy_search or "").strip()
        if fuzzy_keyword:
            keyword_lower = fuzzy_keyword.lower()
            drill_rows = [
                row for row in drill_rows
                if keyword_lower in str(row.get("Role", "")).lower()
                or keyword_lower in str(row.get("Item Text", "")).lower()
                or keyword_lower in str(row.get("MRP Element Indicator", "")).lower()
            ]
            # Recalculate total row from filtered drill_rows
            if drill_rows:
                month_cols = [c["id"] for c in drill_columns if c["id"] not in ("Role", "Item Text", "MRP Element Indicator", TOTAL_LABEL)]
                new_total: Dict[str, Any] = {"Role": TOTAL_LABEL, "Item Text": TOTAL_LABEL, "MRP Element Indicator": "", "__role_raw": TOTAL_LABEL}
                for col_id in month_cols:
                    col_sum = sum(
                        float(str(r.get(col_id, "0")).replace(",", "").replace("-", "0"))
                        for r in drill_rows
                        if str(r.get(col_id, "-")) not in ("-", "")
                    )
                    new_total[col_id] = f"{col_sum:,.1f}" if col_sum else "-"
                grand_total = sum(
                    float(str(r.get(TOTAL_LABEL, "0")).replace(",", "").replace("-", "0"))
                    for r in drill_rows
                    if str(r.get(TOTAL_LABEL, "-")) not in ("-", "")
                )
                new_total[TOTAL_LABEL] = f"{grand_total:,.1f}" if grand_total else "-"
                drill_total_rows = [new_total]
            else:
                drill_total_rows = []

        hc_idp_columns, hc_idp_rows = build_hc_idp_monthly_table(hc_idp_monthly)
        hc_idp_hs_df = build_demand_hs_dataframe(hc_idp_monthly, monthly_level1)
        hc_idp_iya_columns, hc_idp_iya_rows = build_demand_iya_table(hc_idp_monthly, historical_shipment)
        hc_idp_hs_columns, hc_idp_hs_rows = build_hc_idp_monthly_table(hc_idp_hs_df)
        hc_idp_hs_iya_columns, hc_idp_hs_iya_rows = build_demand_iya_table(hc_idp_hs_df, historical_shipment)
        hc_idp_quarter_iya_columns, hc_idp_quarter_iya_rows = build_demand_iya_by_quarter_table(
            hc_idp_monthly,
            hc_idp_hs_df,
            historical_shipment,
        )
        (_, quarter1_columns, quarter1_rows), (_, quarter2_columns, quarter2_rows) = split_quarter_iya_tables(
            hc_idp_quarter_iya_columns,
            hc_idp_quarter_iya_rows,
        )
        td_validation_columns, td_validation_rows = build_td_validation_table_from_detail(td_validation_detail)
        td_validation_styles = build_td_validation_style_data_conditional(td_validation_columns)
        production_group_1 = ["0386", "1864", "A868"]
        production_group_1_totals_after = {
            "0386": ("HP Total", ["0386", "C810"]),
            "1864": ("XQ Total", ["1864", "D352"]),
            "A868": ("TC Total", ["A868", "A673"]),
        }
        production_version_df = pd.DataFrame(data.get("production_version_compare", []))
        if not production_version_df.empty:
            keep = production_version_df["Plant"].fillna("").isin(["1864", "0386", "A868", "GC Total", ""])
            production_version_df = production_version_df[keep].reset_index(drop=True)
        production_version_columns, production_version_rows = build_production_version_comparison_table(production_version_df)
        production_version_styles = build_production_version_style_data_conditional(production_version_columns)
        production_level_columns_1, production_level_rows_1 = build_production_data_table_by_plant_level(
            production_data_by_level_df,
            plant_order=production_group_1,
            include_segment_totals=True,
            segment_totals_after=production_group_1_totals_after,
        )
        production_data_weekly_df = pd.DataFrame(data.get("production_data_weekly", []))
        production_data_by_level_weekly_df = pd.DataFrame(data.get("production_data_by_level_weekly", []))
        weekly_plant_columns, weekly_plant_rows = build_production_data_table_by_plant_weekly(
            production_data_weekly_df,
            plant_order=production_group_1,
            include_segment_totals=False,
        )
        weekly_level_columns, weekly_level_rows = build_production_data_table_by_plant_level_weekly(
            production_data_by_level_weekly_df,
            plant_order=production_group_1,
            include_segment_totals=True,
            segment_totals_after=production_group_1_totals_after,
        )
        level1_core_columns, level1_core_rows = build_first_level_summary(
            monthly_level1,
            source_level_column="First Level",
            display_level_column="Level 1",
            include_levels=["Base", "PP"],
        )
        level1_hktw_ess_columns, level1_hktw_ess_rows = build_first_level_summary(
            monthly_level1,
            source_level_column="First Level",
            display_level_column="Level 1",
            include_levels=["HKTW", "ESS"],
        )
        return (
            table_columns,
            table_data,
            table_tooltip_data,
            role_fig,
            pde_columns,
            pde_records,
            pde_fg_columns,
            pde_fg_records,
            drill_requester_options,
            valid_requesters,
            drill_mrp_options,
            valid_mrp_indicators,
            drill_item_text_options,
            valid_item_texts,
            drill_columns,
            drill_rows,
            drill_total_columns,
            drill_total_rows,
            hc_idp_columns,
            hc_idp_rows,
            hc_idp_iya_columns,
            hc_idp_iya_rows,
            hc_idp_hs_columns,
            hc_idp_hs_rows,
            hc_idp_hs_iya_columns,
            hc_idp_hs_iya_rows,
            quarter1_columns,
            quarter1_rows,
            quarter2_columns,
            quarter2_rows,
            level1_core_columns,
            level1_core_rows,
            level1_hktw_ess_columns,
            level1_hktw_ess_rows,
            td_validation_columns,
            td_validation_rows,
            td_validation_styles,
            production_version_columns,
            production_version_rows,
            production_version_styles,
            production_level_columns_1,
            production_level_rows_1,
            weekly_plant_columns,
            weekly_plant_rows,
            weekly_level_columns,
            weekly_level_rows,
        )

    @app.callback(
        Output("pde-table", "data", allow_duplicate=True),
        Output("pde-table", "tooltip_data", allow_duplicate=True),
        Output("pde-fg-table", "data", allow_duplicate=True),
        Output("pde-fg-table", "tooltip_data", allow_duplicate=True),
        Input("pde-table", "sort_by"),
        Input("pde-fg-table", "sort_by"),
        State("pde-table", "data"),
        State("pde-table", "columns"),
        State("pde-fg-table", "data"),
        State("pde-fg-table", "columns"),
        prevent_initial_call=True,
    )
    def resort_pde_tables(pde_sort_by, pde_fg_sort_by, pde_data, pde_columns, pde_fg_data, pde_fg_columns):
        sorted_pde = _sort_pde_records_keep_total_last(pde_data or [], pde_sort_by)
        sorted_pde_fg = _sort_pde_records_keep_total_last(pde_fg_data or [], pde_fg_sort_by)
        return (
            sorted_pde,
            _build_pde_tooltip_data(pde_columns or [], sorted_pde),
            sorted_pde_fg,
            _build_pde_tooltip_data(pde_fg_columns or [], sorted_pde_fg),
        )

    @app.callback(
        Output("pde-table", "tooltip_data", allow_duplicate=True),
        Output("pde-fg-table", "tooltip_data", allow_duplicate=True),
        Input("pde-table", "derived_viewport_data"),
        Input("pde-table", "derived_viewport_indices"),
        State("pde-table", "columns"),
        State("pde-table", "data"),
        Input("pde-fg-table", "derived_viewport_data"),
        Input("pde-fg-table", "derived_viewport_indices"),
        State("pde-fg-table", "columns"),
        State("pde-fg-table", "data"),
        prevent_initial_call=True,
    )
    def sync_pde_tooltips_to_viewport(
        pde_view_rows,
        pde_view_indices,
        pde_columns,
        pde_all_rows,
        pde_fg_view_rows,
        pde_fg_view_indices,
        pde_fg_columns,
        pde_fg_all_rows,
    ):
        pde_all_rows = pde_all_rows or []
        pde_fg_all_rows = pde_fg_all_rows or []
        pde_tips_all = [{} for _ in pde_all_rows]
        pde_fg_tips_all = [{} for _ in pde_fg_all_rows]

        pde_view_tips = _build_pde_tooltip_data(pde_columns or [], pde_view_rows or [])
        for i, raw_idx in enumerate(pde_view_indices or []):
            try:
                idx = int(raw_idx)
            except Exception:
                continue
            if 0 <= idx < len(pde_tips_all) and i < len(pde_view_tips):
                pde_tips_all[idx] = pde_view_tips[i]

        pde_fg_view_tips = _build_pde_tooltip_data(pde_fg_columns or [], pde_fg_view_rows or [])
        for i, raw_idx in enumerate(pde_fg_view_indices or []):
            try:
                idx = int(raw_idx)
            except Exception:
                continue
            if 0 <= idx < len(pde_fg_tips_all) and i < len(pde_fg_view_tips):
                pde_fg_tips_all[idx] = pde_fg_view_tips[i]

        return pde_tips_all, pde_fg_tips_all

    # ── Production Dimension Detail callback ──────────────────────
    @app.callback(
        Output("prod-dim-filter-plant", "options"),
        Output("prod-dim-filter-brand", "options"),
        Output("prod-dim-filter-size", "options"),
        Output("prod-dim-filter-variant", "options"),
        Output("prod-dim-filter-prodline", "options"),
        Output("prod-dim-filter-type", "options"),
        Output("production-dim-detail-table", "columns"),
        Output("production-dim-detail-table", "data"),
        Input("data-store", "data"),
        Input("prod-dim-group-by", "value"),
        Input("prod-dim-filter-plant", "value"),
        Input("prod-dim-filter-brand", "value"),
        Input("prod-dim-filter-size", "value"),
        Input("prod-dim-filter-variant", "value"),
        Input("prod-dim-filter-prodline", "value"),
        Input("prod-dim-filter-type", "value"),
    )
    def update_production_dimension_detail(
        data, group_by, plant_filter, brand_filter, size_filter, variant_filter, prodline_filter, type_filter
    ):
        td_dim_df = pd.DataFrame((data or {}).get("td_demand_by_dimension", []))
        dim_options = build_production_dimension_options(td_dim_df)
        filters: Dict[str, List[str]] = {}
        if plant_filter:
            filters["Plant"] = plant_filter
        if brand_filter:
            filters["Brand"] = brand_filter
        if size_filter:
            filters["Size"] = size_filter
        if variant_filter:
            filters["Variant"] = variant_filter
        if prodline_filter:
            filters["Prod Line"] = prodline_filter
        if type_filter:
            filters["Type"] = type_filter
        selected_group_by = group_by if group_by else ["Plant", "Brand", "Size", "Variant"]
        dim_columns, dim_rows = build_production_dimension_table(
            td_dim_df, selected_group_by, filters
        )
        return (
            dim_options.get("Plant", []),
            dim_options.get("Brand", []),
            dim_options.get("Size", []),
            dim_options.get("Variant", []),
            dim_options.get("Prod Line", []),
            dim_options.get("Type", []),
            dim_columns,
            dim_rows,
        )

    @app.callback(
        Output("td-gap-detail-title", "children"),
        Output("td-gap-detail-table", "columns"),
        Output("td-gap-detail-table", "data"),
        Output("td-gap-detail-table", "style_data_conditional"),
        Input("td-validation-table", "active_cell"),
        Input("td-validation-table", "derived_viewport_data"),
        Input("td-gap-level2-table", "active_cell"),
        Input("td-gap-level2-table", "derived_viewport_data"),
    )
    def update_td_gap_details(active_cell, table_rows, level2_active_cell, level2_rows):
        td_detail_df = load_dataset(cfg.processed_dir, "td_version_gap_details.csv")
        title, columns, data, style_conditional = build_td_gap_detail_table(
            active_cell,
            table_rows or [],
            level2_active_cell,
            level2_rows or [],
            td_detail_df,
        )
        return title, columns, data, style_conditional

    @app.callback(
        Output("td-gap-level2-title", "children"),
        Output("td-gap-level2-table", "columns"),
        Output("td-gap-level2-table", "data"),
        Output("td-gap-level2-table", "style_data_conditional"),
        Input("td-validation-table", "active_cell"),
        Input("td-validation-table", "derived_viewport_data"),
    )
    def update_td_gap_level2_details(active_cell, table_rows):
        td_detail_df = load_dataset(cfg.processed_dir, "td_version_gap_details.csv")
        title, columns, data, style_conditional = build_td_gap_level2_table(active_cell, table_rows or [], td_detail_df)
        return title, columns, data, style_conditional

    @app.callback(
        Output("td-gap-detail-download", "data"),
        Input("td-gap-export-btn", "n_clicks"),
        State("td-gap-detail-table", "columns"),
        State("td-gap-detail-table", "data"),
        State("td-gap-detail-title", "children"),
        prevent_initial_call=True,
    )
    def export_td_gap_details(n_clicks, columns, rows, title):
        if not n_clicks or not rows:
            raise PreventUpdate

        if not columns:
            raise PreventUpdate

        export_df = pd.DataFrame(rows)
        column_ids = [str(col.get("id", "")) for col in columns if col.get("id")]
        if not column_ids:
            raise PreventUpdate

        for col in column_ids:
            if col not in export_df.columns:
                export_df[col] = ""

        export_df = export_df[column_ids]
        rename_map = {str(col.get("id", "")): str(col.get("name", col.get("id", ""))) for col in columns}
        export_df = export_df.rename(columns=rename_map)

        safe_title = re.sub(r"[^0-9A-Za-z\-_]+", "_", str(title or "GAP_Details")).strip("_")
        if not safe_title:
            safe_title = "GAP_Details"
        filename = f"{safe_title}.xlsx"

        return dcc.send_data_frame(export_df.to_excel, filename, index=False, sheet_name="GAP Details")

    @app.callback(
        Output("td-gap-level2-download", "data"),
        Input("td-gap-level2-export-btn", "n_clicks"),
        State("td-gap-level2-table", "columns"),
        State("td-gap-level2-table", "data"),
        State("td-gap-level2-title", "children"),
        prevent_initial_call=True,
    )
    def export_td_gap_level2_details(n_clicks, columns, rows, title):
        if not n_clicks or not rows:
            raise PreventUpdate

        if not columns:
            raise PreventUpdate

        export_df = pd.DataFrame(rows)
        column_ids = [str(col.get("id", "")) for col in columns if col.get("id") and col.get("id") != "Version Group"]
        if not column_ids:
            raise PreventUpdate

        for col in column_ids:
            if col not in export_df.columns:
                export_df[col] = ""

        export_df = export_df[column_ids]
        rename_map = {str(col.get("id", "")): str(col.get("name", col.get("id", ""))) for col in columns}
        export_df = export_df.rename(columns=rename_map)

        safe_title = re.sub(r"[^0-9A-Za-z\-_]+", "_", str(title or "Level2_GAP_Details")).strip("_")
        if not safe_title:
            safe_title = "Level2_GAP_Details"
        filename = f"{safe_title}.xlsx"

        return dcc.send_data_frame(export_df.to_excel, filename, index=False, sheet_name="Level2 GAP")

    # ── Brand Dimension GAP callbacks ──

    @app.callback(
        Output("td-gap-brand-summary-title", "children"),
        Output("td-gap-brand-summary-table", "columns"),
        Output("td-gap-brand-summary-table", "data"),
        Output("td-gap-brand-summary-table", "style_data_conditional"),
        Input("td-validation-table", "active_cell"),
        Input("td-validation-table", "derived_viewport_data"),
    )
    def update_td_gap_brand_summary(active_cell, table_rows):
        td_detail_df = load_dataset(cfg.processed_dir, "td_version_gap_details.csv")
        title, columns, data, style_conditional = build_td_gap_brand_summary_table(
            active_cell, table_rows or [], td_detail_df,
        )
        return title, columns, data, style_conditional

    @app.callback(
        Output("td-gap-brand-detail-title", "children"),
        Output("td-gap-brand-detail-table", "columns"),
        Output("td-gap-brand-detail-table", "data"),
        Output("td-gap-brand-detail-table", "style_data_conditional"),
        Input("td-validation-table", "active_cell"),
        Input("td-validation-table", "derived_viewport_data"),
        Input("td-gap-brand-summary-table", "active_cell"),
        Input("td-gap-brand-summary-table", "derived_viewport_data"),
        Input("brand-dim-checklist", "value"),
    )
    def update_td_gap_brand_detail(active_cell, table_rows, brand_active_cell, brand_rows, visible_dims):
        td_detail_df = load_dataset(cfg.processed_dir, "td_version_gap_details.csv")
        title, columns, data, style_conditional = build_td_gap_brand_detail_table(
            active_cell, table_rows or [],
            brand_active_cell, brand_rows or [],
            td_detail_df,
            visible_dims=visible_dims or ["Brand"],
        )
        return title, columns, data, style_conditional

    @app.callback(
        Output("td-gap-brand-summary-download", "data"),
        Input("td-gap-brand-summary-export-btn", "n_clicks"),
        State("td-gap-brand-summary-table", "columns"),
        State("td-gap-brand-summary-table", "data"),
        State("td-gap-brand-summary-title", "children"),
        prevent_initial_call=True,
    )
    def export_td_gap_brand_summary(n_clicks, columns, rows, title):
        if not n_clicks or not rows:
            raise PreventUpdate
        if not columns:
            raise PreventUpdate
        export_df = pd.DataFrame(rows)
        column_ids = [str(col.get("id", "")) for col in columns if col.get("id")]
        if not column_ids:
            raise PreventUpdate
        for col in column_ids:
            if col not in export_df.columns:
                export_df[col] = ""
        export_df = export_df[column_ids]
        rename_map = {str(col.get("id", "")): str(col.get("name", col.get("id", ""))) for col in columns}
        export_df = export_df.rename(columns=rename_map)
        safe_title = re.sub(r"[^0-9A-Za-z\-_]+", "_", str(title or "Brand_GAP_Summary")).strip("_")
        if not safe_title:
            safe_title = "Brand_GAP_Summary"
        return dcc.send_data_frame(export_df.to_excel, f"{safe_title}.xlsx", index=False, sheet_name="Brand Summary")

    @app.callback(
        Output("td-gap-brand-detail-download", "data"),
        Input("td-gap-brand-detail-export-btn", "n_clicks"),
        State("td-gap-brand-detail-table", "columns"),
        State("td-gap-brand-detail-table", "data"),
        State("td-gap-brand-detail-title", "children"),
        prevent_initial_call=True,
    )
    def export_td_gap_brand_detail(n_clicks, columns, rows, title):
        if not n_clicks or not rows:
            raise PreventUpdate
        if not columns:
            raise PreventUpdate
        export_df = pd.DataFrame(rows)
        column_ids = [str(col.get("id", "")) for col in columns if col.get("id")]
        if not column_ids:
            raise PreventUpdate
        for col in column_ids:
            if col not in export_df.columns:
                export_df[col] = ""
        export_df = export_df[column_ids]
        rename_map = {str(col.get("id", "")): str(col.get("name", col.get("id", ""))) for col in columns}
        export_df = export_df.rename(columns=rename_map)
        safe_title = re.sub(r"[^0-9A-Za-z\-_]+", "_", str(title or "Brand_Dimension_Detail")).strip("_")
        if not safe_title:
            safe_title = "Brand_Dimension_Detail"
        return dcc.send_data_frame(export_df.to_excel, f"{safe_title}.xlsx", index=False, sheet_name="Brand Detail")

    @app.callback(
        Output("drill-detail-title", "children"),
        Output("drill-detail-table", "columns"),
        Output("drill-detail-table", "data"),
        Input("role-item-mrp-summary", "active_cell"),
        Input("details-version-store", "data"),
        State("role-item-mrp-summary", "columns"),
        State("role-item-mrp-summary", "data"),
        State("role-item-mrp-summary", "derived_viewport_data"),
        State("drill-requester-filter", "value"),
    )
    def update_drill_details(active_cell, _version_data, summary_columns, summary_rows, viewport_rows, drill_requester_value):
        details = load_request_details(cfg)
        default_columns = [{"name": field, "id": field} for field in DETAIL_VIEW_FIELDS]
        if not active_cell:
            return "请选择上方 Role × Item × Project 组合", default_columns, []

        working_rows: List[Dict] = []
        if viewport_rows:
            working_rows = viewport_rows
        elif summary_rows:
            working_rows = summary_rows

        if not working_rows:
            return "请选择上方 Role × Item × Project 组合", default_columns, []

        row_index = active_cell.get("row")
        if row_index is None or row_index < 0 or row_index >= len(working_rows):
            return "请选择上方 Role × Item × Project 组合", default_columns, []

        selected_row = working_rows[row_index]
        role = selected_row.get("__role_raw") or selected_row.get("Role") or ROLE_ALL_VALUE
        item_text = selected_row.get("Item Text") or ""
        mrp_indicator = selected_row.get("MRP Element Indicator") or ""
        selected_requesters = normalize_requester_values(drill_requester_value)

        clicked_column = str(active_cell.get("column_id", "") or "").strip()
        if not clicked_column and summary_columns:
            col_index = active_cell.get("column")
            if isinstance(col_index, int) and 0 <= col_index < len(summary_columns):
                clicked_column = str(summary_columns[col_index].get("id", "") or "").strip()

        def normalize_selected_month(value: str) -> Optional[str]:
            text = str(value or "").strip()
            if not text or text == TOTAL_LABEL:
                return None
            if re.fullmatch(r"\d{4}-\d{2}", text):
                return text
            if re.fullmatch(r"\d{4}-\d{1}", text):
                year, month = text.split("-", 1)
                return f"{year}-{int(month):02d}"
            try:
                period = pd.Period(text, freq="M")
                return f"{period.year}-{period.month:02d}"
            except Exception:
                pass
            parsed = pd.to_datetime(text, errors="coerce")
            if pd.notna(parsed):
                return parsed.strftime("%Y-%m")
            return None

        selected_month = normalize_selected_month(clicked_column)

        try:
            columns, rows = build_modal_detail_rows(
                details,
                role,
                item_text,
                mrp_indicator,
                selected_requesters,
                selected_month,
            )
        except Exception:
            logging.exception("Failed to build drill detail rows: role=%s item=%s mrp=%s", role, item_text, mrp_indicator)
            return "加载明细失败，请稍后重试", [{"name": "错误", "id": "error"}], [{"error": "加载明细失败"}]

        role_title = ROLE_DISPLAY_MAP.get(str(role).strip(), str(role).strip())
        title_parts = [part for part in [role_title, item_text, mrp_indicator] if part]
        title = " / ".join(title_parts) if title_parts else "明细列表"
        if selected_month:
            title = f"{title} / {selected_month}"
        if not rows:
            return f"{title} · 暂无数据", columns or default_columns, rows
        return title, columns, rows

    @app.callback(
        Output("drill-detail-download", "data"),
        Input("drill-detail-export-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def export_all_drill_details(n_clicks):
        if not n_clicks:
            raise PreventUpdate

        details = load_request_details(cfg)
        if details.empty:
            raise PreventUpdate

        export_df = details.copy()
        for field in DETAIL_VIEW_FIELDS:
            if field not in export_df.columns:
                export_df[field] = ""
        export_df = export_df[DETAIL_VIEW_FIELDS]

        return dcc.send_data_frame(
            export_df.to_excel,
            "Project_Details_All.xlsx",
            index=False,
            sheet_name="Project Details",
        )

    @app.callback(
        Output("backup-snapshot-download", "data"),
        Output("backup-snapshot-status", "children"),
        Input("backup-snapshot-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def backup_snapshot(n_clicks):
        if not n_clicks:
            raise PreventUpdate

        try:
            snapshot_dir, excel_file, exported_count = create_dashboard_snapshot(cfg)
            status_text = f"Backup completed: {exported_count} tables saved to {snapshot_dir}"
            return dash.no_update, status_text
        except Exception as exc:
            logging.exception("Failed to create dashboard snapshot")
            return dash.no_update, f"Backup failed: {exc}"


# ---------------------------------------------------------------------------
# Server-side admin auth helpers
# ---------------------------------------------------------------------------
_LOGIN_FAIL_WINDOW = 300       # 5-minute window
_LOGIN_FAIL_MAX = 5            # max failures per IP in window
_login_failures: Dict[str, List[float]] = {}   # ip -> [timestamps]


def _is_admin_authenticated() -> bool:
    """Check if the current request has a valid server-side admin session."""
    return bool(flask_session.get("admin_authenticated"))


def _record_login_failure(ip: str) -> None:
    """Record a failed login attempt for rate limiting."""
    import time as _time
    now = _time.time()
    if ip not in _login_failures:
        _login_failures[ip] = []
    # Prune old entries
    _login_failures[ip] = [t for t in _login_failures[ip] if now - t < _LOGIN_FAIL_WINDOW]
    _login_failures[ip].append(now)


def _is_login_blocked(ip: str) -> bool:
    """Check if an IP has exceeded the login failure limit."""
    import time as _time
    now = _time.time()
    attempts = _login_failures.get(ip, [])
    recent = [t for t in attempts if now - t < _LOGIN_FAIL_WINDOW]
    return len(recent) >= _LOGIN_FAIL_MAX


def register_admin_callbacks(app: Dash, cfg: AppConfig) -> None:
    """Register all callbacks for the /admin page."""

    # ── Login (server-side session + rate limiting) ─────────────
    @app.callback(
        Output("admin-session", "data"),
        Output("admin-login-error", "children"),
        Output("admin-login-box", "style"),
        Output("admin-panel", "style"),
        Input("admin-login-btn", "n_clicks"),
        Input("admin-password-input", "n_submit"),
        State("admin-password-input", "value"),
        State("admin-session", "data"),
        prevent_initial_call=True,
    )
    def admin_login(n_clicks, n_submit, password, session):
        # Already authenticated (server-side check)
        if _is_admin_authenticated():
            return {"authenticated": True}, dash.no_update, {"display": "none"}, {"display": "block"}

        client_ip = str(request.remote_addr or "unknown")

        # Rate limiting: block after too many failures
        if _is_login_blocked(client_ip):
            logging.warning("Login blocked for IP %s (too many failures).", client_ip)
            return dash.no_update, "Too many failed attempts. Please wait 5 minutes.", dash.no_update, dash.no_update

        if not password:
            return dash.no_update, "Please enter a password.", dash.no_update, dash.no_update

        if not cfg.admin_password:
            logging.error("Admin login attempted but no password is configured.")
            return dash.no_update, "Admin password not configured. Contact the administrator.", dash.no_update, dash.no_update

        if password == cfg.admin_password:
            # Set server-side session
            flask_session["admin_authenticated"] = True
            flask_session.permanent = True
            logging.info("Admin login successful from IP %s.", client_ip)
            return {"authenticated": True}, "", {"display": "none"}, {"display": "block"}

        # Failed login
        _record_login_failure(client_ip)
        logging.warning("Admin login failed from IP %s.", client_ip)
        return dash.no_update, "Incorrect password.", dash.no_update, dash.no_update

    # ── Restore session on page load (check server-side) ──
    @app.callback(
        Output("admin-login-box", "style", allow_duplicate=True),
        Output("admin-panel", "style", allow_duplicate=True),
        Input("admin-session", "data"),
        prevent_initial_call=True,
    )
    def admin_restore_session(session):
        # Server-side session is the source of truth
        if _is_admin_authenticated():
            return {"display": "none"}, {"display": "block"}
        return dash.no_update, dash.no_update

    # ── Run Pipeline ──────────────────────────────────────────────
    @app.callback(
        Output("admin-pipeline-status", "children"),
        Output("admin-pipeline-interval", "disabled"),
        Output("admin-pipeline-progress", "style"),
        Output("admin-run-pipeline-btn", "disabled"),
        Input("admin-run-pipeline-btn", "n_clicks"),
        State("admin-refresh-scope", "value"),
        prevent_initial_call=True,
    )
    def admin_run_pipeline(n_clicks, scope):
        if not n_clicks:
            raise PreventUpdate
        if not _is_admin_authenticated():
            raise PreventUpdate
        group = scope or "all"
        err = _start_pipeline_subprocess(group)
        if err:
            return (
                f"⚠️ {err}",
                True,    # keep interval disabled
                {"marginTop": "10px", "display": "block"},
                False,   # re-enable button
            )
        label = REFRESH_GROUPS.get(group, {}).get("label", group)
        ts = datetime.now().strftime("%H:%M:%S")
        return (
            f"\u23f3 Pipeline started at {ts} ({label})",
            False,   # enable interval
            {"marginTop": "10px", "display": "block"},
            True,    # disable button
        )

    # ── Pipeline progress polling ─────────────────────────────────
    @app.callback(
        Output("admin-progress-fill", "style"),
        Output("admin-progress-pct", "children"),
        Output("admin-progress-text", "children"),
        Output("admin-pipeline-status", "children", allow_duplicate=True),
        Output("admin-pipeline-interval", "disabled", allow_duplicate=True),
        Output("admin-pipeline-progress", "style", allow_duplicate=True),
        Output("admin-run-pipeline-btn", "disabled", allow_duplicate=True),
        Input("admin-pipeline-interval", "n_intervals"),
        prevent_initial_call=True,
    )
    def admin_pipeline_progress(n_intervals):
        _FILL_BASE = {
            "width": "0%", "height": "100%",
            "backgroundColor": "#3b82f6", "borderRadius": "4px",
            "transition": "width 0.4s ease",
        }
        progress = _read_pipeline_progress()
        if progress is None:
            return (
                {**_FILL_BASE, "width": "5%"}, "\u2026",
                "Waiting for pipeline ...",
                dash.no_update, False,
                {"marginTop": "10px", "display": "block"}, True,
            )

        status = progress.get("status", "unknown")
        done = progress.get("stages_done", 0)
        total = max(progress.get("stages_total", 1), 1)
        pct = int(done / total * 100)
        current_label = progress.get("current_stage_label", "")

        if status == "running":
            return (
                {**_FILL_BASE, "width": f"{max(pct, 5)}%"}, f"{pct}%",
                f"Running: {current_label} ({done}/{total})",
                dash.no_update, False,
                {"marginTop": "10px", "display": "block"}, True,
            )
        if status == "completed":
            ts = datetime.now().strftime("%H:%M:%S")
            completed = progress.get("completed_stages", [])
            labels = ", ".join(REFRESH_GROUPS.get(s, {}).get("label", s) for s in completed)
            return (
                {**_FILL_BASE, "width": "100%"}, "100%", "",
                f"\u2713 Pipeline completed at {ts} \u2014 {labels}",
                True,  # stop polling
                {"marginTop": "10px", "display": "none"},
                False,  # re-enable button
            )
        if status == "error":
            error_msg = progress.get("error_message", "Unknown error")
            ts = datetime.now().strftime("%H:%M:%S")
            return (
                {**_FILL_BASE, "width": "0%", "backgroundColor": "#ef4444"}, "", "",
                f"\u2717 Pipeline failed at {ts}: {error_msg}",
                True, {"marginTop": "10px", "display": "none"}, False,
            )
        return (
            {**_FILL_BASE, "width": "5%"}, "\u2026", "Checking...",
            dash.no_update, False,
            {"marginTop": "10px", "display": "block"}, True,
        )

    # ── Refresh Data (no pipeline) ────────────────────────────────
    @app.callback(
        Output("admin-refresh-data-status", "children"),
        Output("admin-refresh-data-btn", "disabled"),
        Input("admin-refresh-data-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def admin_refresh_data(n_clicks):
        if not n_clicks:
            raise PreventUpdate
        if not _is_admin_authenticated():
            raise PreventUpdate
        try:
            bundle = load_data_bundle(cfg)
            # Write data version so ALL connected dashboards detect and refresh
            _write_data_version()
            n_details = len(bundle.get("pde_alerts", []))
            n_items = len(pd.DataFrame(bundle.get("monthly_item", []))["Item Text"].unique()) if bundle.get("monthly_item") else 0
            ts = datetime.now().strftime("%H:%M:%S")
            return f"\u2713 Data refreshed at {ts} \u2014 {n_items} items, {n_details} PDE alerts. All dashboards will update within seconds.", False
        except Exception:
            logging.exception("Failed to refresh data")
            return "\u2717 Refresh failed, check server logs.", False

    # ── Backup Snapshot ───────────────────────────────────────────
    @app.callback(
        Output("admin-backup-download", "data"),
        Output("admin-backup-status", "children"),
        Input("admin-backup-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def admin_backup(n_clicks):
        if not n_clicks:
            raise PreventUpdate
        if not _is_admin_authenticated():
            raise PreventUpdate
        try:
            snapshot_dir, excel_file, exported_count = create_dashboard_snapshot(cfg)
            logging.info("Backup snapshot saved: %s (%d tables)", snapshot_dir, exported_count)
            return dash.no_update, f"✓ Backup completed: {exported_count} tables saved to {snapshot_dir}"
        except Exception as exc:
            logging.exception("Failed to create dashboard snapshot")
            return dash.no_update, f"✗ Backup failed: {exc}"

    # ── Update & Restart ──────────────────────────────────────────
    @app.callback(
        Output("admin-update-status", "children"),
        Output("admin-update-btn", "disabled"),
        Input("admin-update-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def admin_update_restart(n_clicks):
        if not n_clicks:
            raise PreventUpdate
        if not _is_admin_authenticated():
            raise PreventUpdate

        messages: List[str] = []

        # Since update_and_start_matres.bat handles EVERYTHING
        # (git pull → venv → pip → pipeline → start dashboard),
        # we just need to launch it and exit.
        messages.append("[restart] Launching update_and_start_matres.bat ...")
        messages.append("A new CMD window will open. This page will stop responding.")
        messages.append("Please wait ~2 minutes, then visit http://localhost:8050")

        import threading

        def _delayed_restart():
            import time
            time.sleep(2)

            bat_path = _PROJECT_ROOT / "update_and_start_matres.bat"
            logging.info("Restart: launching %s", bat_path)

            if sys.platform == "win32":
                if not bat_path.exists():
                    logging.error("update_and_start_matres.bat not found: %s", bat_path)
                    return

                # Use "start" command to open a NEW independent CMD window.
                # "start" with empty title "" and the bat path opens it correctly.
                # This is more reliable than os.startfile which uses cmd /c
                # and may close immediately on errors.
                subprocess.Popen(
                    f'start "" "{bat_path.resolve()}"',
                    shell=True,
                    cwd=str(_PROJECT_ROOT),
                )
                logging.info("Bat launched. Exiting current process in 3s ...")
                time.sleep(3)
                os._exit(0)
            else:
                restart_cmd = [sys.executable] + sys.argv
                os.execv(sys.executable, restart_cmd)

        threading.Thread(target=_delayed_restart, daemon=False).start()
        return "\n".join(messages), True

    # ── Master Data Update ─────────────────────────────────────────
    @app.callback(
        Output("admin-masterdata-table-wrapper", "children"),
        Output("admin-masterdata-status", "children"),
        Output("admin-masterdata-store", "data"),
        Input("admin-masterdata-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def admin_masterdata_scan(n_clicks):
        if not n_clicks:
            raise PreventUpdate
        if not _is_admin_authenticated():
            raise PreventUpdate
        try:
            # Import pipeline functions to build the report
            sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
            from matres_pipeline import PipelineConfig, build_master_data_update_report

            pipeline_cfg = PipelineConfig.from_dict(
                json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            )
            report_df = build_master_data_update_report(pipeline_cfg)

            if report_df.empty:
                return (
                    html.P("\u2705 No missing master data found. All materials are mapped.",
                           style={"color": "#16a34a", "fontWeight": "600"}),
                    "",
                    None,
                )

            seg_count = int((report_df["Miss"] == "Seg \u7f3a\u5931").sum())
            su_count = int((report_df["Miss"] == "SU Factor").sum())
            status_text = f"Found {len(report_df)} items: {seg_count} Seg \u7f3a\u5931, {su_count} SU Factor"

            table = DataTable(
                id="admin-masterdata-result-table",
                columns=[
                    {"name": "Code", "id": "Code"},
                    {"name": "Description", "id": "Description"},
                    {"name": "Miss", "id": "Miss"},
                    {"name": "Data Source", "id": "Data Source"},
                ],
                data=report_df.to_dict("records"),
                style_header=PDE_STYLE_HEADER,
                style_cell=PDE_STYLE_CELL,
                style_data_conditional=[
                    *PDE_STYLE_DATA_CONDITIONAL,
                    {
                        "if": {"filter_query": '{Miss} = "Seg \u7f3a\u5931"', "column_id": "Miss"},
                        "color": "#dc2626",
                        "fontWeight": "700",
                    },
                    {
                        "if": {"filter_query": '{Miss} = "SU Factor"', "column_id": "Miss"},
                        "color": "#d97706",
                        "fontWeight": "700",
                    },
                    {
                        "if": {"filter_query": '{Data Source} contains "Production Data"', "column_id": "Data Source"},
                        "color": "#2563eb",
                    },
                    {
                        "if": {"filter_query": '{Data Source} contains "Demand Data"', "column_id": "Data Source"},
                        "color": "#7c3aed",
                    },
                ],
                style_cell_conditional=[
                    {"if": {"column_id": "Code"}, "textAlign": "left", "minWidth": "100px", "width": "120px"},
                    {"if": {"column_id": "Description"}, "textAlign": "left", "minWidth": "250px", "width": "400px"},
                    {"if": {"column_id": "Miss"}, "textAlign": "center", "minWidth": "100px", "width": "120px"},
                    {"if": {"column_id": "Data Source"}, "textAlign": "center", "minWidth": "140px", "width": "180px"},
                ],
                page_size=20,
                sort_action="native",
                filter_action="native",
                style_table={"overflowX": "auto", "maxHeight": "500px", "overflowY": "auto"},
            )

            return table, status_text, report_df.to_dict("records")
        except Exception:
            logging.exception("Failed to scan master data")
            return html.P("Scan failed. Check server logs.", style={"color": "#dc2626"}), "", None

    @app.callback(
        Output("admin-masterdata-download", "data"),
        Input("admin-masterdata-export-btn", "n_clicks"),
        State("admin-masterdata-store", "data"),
        prevent_initial_call=True,
    )
    def admin_masterdata_export(n_clicks, store_data):
        if not n_clicks or not store_data:
            raise PreventUpdate
        if not _is_admin_authenticated():
            raise PreventUpdate
        try:
            report_df = pd.DataFrame(store_data)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_dir = cfg.processed_dir.parent / "history" / "master_data_reports"
            out_dir.mkdir(parents=True, exist_ok=True)
            excel_path = out_dir / f"master_data_update_{timestamp}.xlsx"
            with pd.ExcelWriter(str(excel_path), engine="openpyxl") as writer:
                report_df.to_excel(writer, sheet_name="Missing Master Data", index=False)
            return dcc.send_file(str(excel_path))
        except Exception:
            logging.exception("Failed to export master data report")
            raise PreventUpdate

    # ── Data Source Status ─────────────────────────────────────────
    @app.callback(
        Output("admin-datasource-table-wrapper", "children"),
        Output("admin-datasource-status", "children"),
        Input("admin-datasource-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def admin_datasource_scan(n_clicks):
        if not n_clicks:
            raise PreventUpdate
        if not _is_admin_authenticated():
            raise PreventUpdate
        try:
            sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
            from matres_pipeline import PipelineConfig, collect_data_source_status

            pipeline_cfg = PipelineConfig.from_dict(
                json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            )
            records = collect_data_source_status(pipeline_cfg)

            if not records:
                return (
                    html.P("No data sources found.",
                           style={"color": "#d97706", "fontWeight": "600"}),
                    "",
                )

            ok_count = sum(1 for r in records if "OK" in r.get("Status", ""))
            missing_count = sum(1 for r in records if "Missing" in r.get("Status", ""))
            status_text = f"Scanned {len(records)} sources: {ok_count} OK, {missing_count} missing"

            table = DataTable(
                id="admin-datasource-result-table",
                columns=[
                    {"name": "Category", "id": "Category"},
                    {"name": "Data Source", "id": "Data Source"},
                    {"name": "File Name", "id": "File Name"},
                    {"name": "Version Date", "id": "Version Date"},
                    {"name": "Modified Time", "id": "Modified Time"},
                    {"name": "Status", "id": "Status"},
                ],
                data=records,
                style_header=PDE_STYLE_HEADER,
                style_cell=PDE_STYLE_CELL,
                style_data_conditional=[
                    *PDE_STYLE_DATA_CONDITIONAL,
                    {
                        "if": {"filter_query": '{Status} contains "OK"'},
                        "color": "#16a34a",
                        "fontWeight": "600",
                    },
                    {
                        "if": {"filter_query": '{Status} contains "Missing"'},
                        "color": "#dc2626",
                        "fontWeight": "700",
                        "backgroundColor": "#fef2f2",
                    },
                    {
                        "if": {"filter_query": '{Category} = "Pipeline Output"'},
                        "color": "#6b7280",
                        "fontSize": "12px",
                    },
                ],
                style_cell_conditional=[
                    {"if": {"column_id": "Category"}, "textAlign": "left", "minWidth": "130px", "width": "160px"},
                    {"if": {"column_id": "Data Source"}, "textAlign": "left", "minWidth": "180px", "width": "220px"},
                    {"if": {"column_id": "File Name"}, "textAlign": "left", "minWidth": "250px", "width": "350px"},
                    {"if": {"column_id": "Version Date"}, "textAlign": "center", "minWidth": "100px", "width": "120px"},
                    {"if": {"column_id": "Modified Time"}, "textAlign": "center", "minWidth": "160px", "width": "180px"},
                    {"if": {"column_id": "Status"}, "textAlign": "center", "minWidth": "80px", "width": "100px"},
                ],
                page_size=25,
                sort_action="native",
                filter_action="native",
                style_table={"overflowX": "auto", "maxHeight": "500px", "overflowY": "auto"},
            )

            return table, status_text
        except Exception:
            logging.exception("Failed to scan data sources")
            return html.P("Scan failed. Check server logs.", style={"color": "#dc2626"}), ""


def create_app() -> Dash:
    cfg = AppConfig.load(CONFIG_PATH)
    app = Dash(
        __name__,
        title="Supply Protection Commander",
        assets_folder=str(Path(__file__).parent / "assets"),
        suppress_callback_exceptions=True,
        url_base_pathname="/",
    )

    # ── Server-side session: set SECRET_KEY ──
    import secrets as _secrets
    app.server.secret_key = os.getenv(
        "MATRES_SECRET_KEY",
        _secrets.token_hex(32),  # random per restart if env var not set
    )
    logging.info("Flask SECRET_KEY configured (server-side sessions enabled).")

    # ── Security response headers ──
    @app.server.after_request
    def add_security_headers(response):
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        return response

    @app.server.route("/docs/user-guide", methods=["GET"])
    def serve_user_guide() -> Response:
        guide_path = _PROJECT_ROOT / "docs" / "user_guide.html"
        if guide_path.exists():
            return Response(guide_path.read_text(encoding="utf-8"), mimetype="text/html; charset=utf-8")
        return Response("User guide not found.", status=404)

    @app.server.route("/data-version", methods=["GET"])
    def serve_data_version() -> Response:
        """Return the current server-side data version as plain text.

        The browser polls this endpoint (see assets/auto_reload.js) and does a
        full page reload when the value changes, so every open tab always shows
        the latest data after the daily/manual pipeline run. Kept independent of
        the Dash callback graph so it keeps working even if the dashboard process
        was restarted while a tab was left open."""
        resp = Response(_read_data_version(), mimetype="text/plain; charset=utf-8")
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    @app.server.route("/mail-preview/latest", methods=["GET"])
    def mail_preview_latest() -> Response:
        try:
            html_file = regenerate_weekly_mail_preview(cfg)
            html_text = html_file.read_text(encoding="utf-8")
            return Response(html_text, mimetype="text/html; charset=utf-8")
        except Exception:
            logging.exception("Failed to regenerate/open weekly mail preview")
            return Response("Failed to refresh weekly mail preview. Please check server logs.", status=500)

    # ── IP-based access control ──────────────────────────────────
    # Disabled: server runs on P&G corporate LAN (143.x.x.x) which is
    # not routable from external networks, so network-layer isolation is
    # sufficient.  The /admin page remains password-protected.
    # To re-enable, set env var MATRES_ALLOWED_SUBNETS to comma-separated
    # CIDRs (e.g. "10.0.0.0/8,143.0.0.0/8,155.0.0.0/8").
    raw_allowed_subnets = os.getenv("MATRES_ALLOWED_SUBNETS", "").strip()
    if raw_allowed_subnets and raw_allowed_subnets.lower() != "disabled":
        allowed_subnets = []
        for subnet_text in [p.strip() for p in raw_allowed_subnets.split(",") if p.strip()]:
            try:
                allowed_subnets.append(ipaddress.ip_network(subnet_text, strict=False))
            except ValueError:
                logging.warning("Invalid subnet ignored in MATRES_ALLOWED_SUBNETS: %s", subnet_text)
        if allowed_subnets:
            @app.server.before_request
            def enforce_internal_access() -> None:
                source_ip = str(request.remote_addr or "").strip()
                try:
                    source_addr = ipaddress.ip_address(source_ip)
                except ValueError:
                    logging.warning("IP access blocked: invalid IP format %r", source_ip)
                    abort(403)
                    return
                if not any(source_addr in subnet for subnet in allowed_subnets):
                    logging.warning("IP access blocked: %s is not in allowed subnets", source_ip)
                    abort(403)
            logging.info("IP access guard enabled with subnets: %s", raw_allowed_subnets)
    else:
        logging.info("IP access guard disabled (relying on network-layer isolation).")

    # ── Pre-build both page layouts ──
    dashboard_layout = build_layout(app, cfg)
    admin_layout = build_admin_layout(cfg)

    # ── Root layout with URL routing ──
    app.layout = html.Div([
        dcc.Location(id="url", refresh=False),
        html.Div(id="page-content"),
    ])

    @app.callback(
        Output("page-content", "children"),
        Input("url", "pathname"),
    )
    def route_page(pathname):
        if pathname == "/admin":
            return admin_layout
        return dashboard_layout

    # Register all callbacks
    register_callbacks(app, cfg)
    register_admin_callbacks(app, cfg)

    # ── Auto-run pipeline on restart if flag exists ──
    flag_file = _PROJECT_ROOT / "data" / "processed" / ".run_pipeline_on_start"
    if flag_file.exists():
        try:
            flag_file.unlink()
            logging.info("Auto-running pipeline after restart ...")
            _start_pipeline_subprocess("all")

            # Monitor pipeline completion in the background and trigger
            # a data-version write so all connected dashboards auto-refresh.
            import threading as _threading

            def _wait_and_notify():
                import time as _time
                for _ in range(600):  # up to ~10 minutes
                    _time.sleep(1)
                    progress = _read_pipeline_progress()
                    if progress and progress.get("status") in ("completed", "error"):
                        if progress["status"] == "completed":
                            logging.info("Auto-pipeline completed. Writing data version to trigger refresh.")
                            _write_data_version()
                        else:
                            logging.warning("Auto-pipeline finished with error: %s", progress.get("error_message"))
                        return
                logging.warning("Auto-pipeline monitor timed out after 600s.")

            _threading.Thread(target=_wait_and_notify, daemon=True).start()
        except Exception:
            logging.exception("Failed to auto-run pipeline on start")

    # ── Start daily auto-refresh scheduler (default 09:00 local) ──
    _start_daily_scheduler()

    return app


app = create_app()


if __name__ == "__main__":
    debug_mode = str(os.getenv("MATRES_DEBUG", "false")).strip().lower() in {"1", "true", "yes", "on"}
    host = str(os.getenv("MATRES_HOST", "0.0.0.0")).strip() or "0.0.0.0"
    port_text = str(os.getenv("MATRES_PORT", "8050")).strip()
    try:
        port = int(port_text)
    except ValueError:
        port = 8050
        logging.warning("Invalid MATRES_PORT: %s. Fallback to 8050.", port_text)

    app.run(debug=debug_mode, host=host, port=port, use_reloader=False)
