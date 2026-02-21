"""Plotly Dash application for the MatRes dashboard MVP."""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

import dash
from dash import Dash, Input, Output, State, dcc, html
from dash.dash_table import DataTable
import pandas as pd
import plotly.graph_objects as go

CONFIG_PATH = Path(os.getenv("MATRES_CONFIG", "config/config.json"))


@dataclass
class AppConfig:
    processed_dir: Path

    @staticmethod
    def load(path: Path) -> "AppConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        processed_dir = Path(raw["processed_dir"])
        if not processed_dir.is_absolute():
            processed_dir = path.parent.parent / processed_dir
        return AppConfig(processed_dir=processed_dir)


def load_dataset(processed_dir: Path, filename: str) -> pd.DataFrame:
    csv_path = processed_dir / filename
    if not csv_path.exists():
        return pd.DataFrame()
    return pd.read_csv(csv_path)


def load_historical_shipment_dataset(cfg: AppConfig) -> pd.DataFrame:
    root = cfg.processed_dir.parent.parent
    candidates = [
        p for p in root.glob("Historical Shipment Data_FY2425*.xls*")
        if not p.name.startswith("~$")
    ]
    if not candidates:
        return pd.DataFrame()

    file_path = max(candidates, key=lambda p: p.stat().st_mtime)
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
        elif "promotion" in label:
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


def load_data_bundle(cfg: AppConfig) -> Dict[str, Any]:
    monthly_item = load_dataset(cfg.processed_dir, "monthly_msu_by_item_text.csv")
    monthly_requester = load_dataset(cfg.processed_dir, "monthly_msu_by_requester_item.csv")
    monthly_level1 = load_dataset(cfg.processed_dir, "monthly_msu_by_level1.csv")
    hc_idp_monthly = load_dataset(cfg.processed_dir, "hc_idp_monthly_summary.csv")
    historical_shipment = load_historical_shipment_dataset(cfg)
    pde_alerts = load_dataset(cfg.processed_dir, "pde_alerts.csv")
    request_details_path = cfg.processed_dir / "matres_request_details.csv"
    details_version = request_details_path.stat().st_mtime if request_details_path.exists() else None

    return {
        "monthly_item": monthly_item.to_dict("records"),
        "monthly_requester": monthly_requester.to_dict("records"),
        "monthly_level1": monthly_level1.to_dict("records"),
        "hc_idp_monthly": hc_idp_monthly.to_dict("records"),
        "historical_shipment": historical_shipment.to_dict("records"),
        "pde_alerts": pde_alerts.to_dict("records"),
        "request_details_version": details_version,
    }


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

PDE_STYLE_HEADER = {
    "backgroundColor": "#eef4fb",
    "color": "#1f3b6d",
    "border": "1px solid #d6e2f0",
    "fontWeight": "600",
}
PDE_STYLE_CELL = {
    "backgroundColor": "#ffffff",
    "color": "#1f2937",
    "border": "1px solid #e2e8f0",
    "textAlign": "center",
}
PDE_STYLE_DATA_CONDITIONAL = [
    {"if": {"row_index": "odd"}, "backgroundColor": "#f8fbff"},
    {"if": {"state": "active"}, "backgroundColor": "#e8f1ff", "border": "1px solid #93c5fd"},
    {"if": {"state": "selected"}, "backgroundColor": "#dbeafe", "border": "1px solid #60a5fa"},
]
TOTAL_LABEL = "汇总"
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


def compute_metrics(monthly_item: pd.DataFrame, pde_alerts: pd.DataFrame) -> Dict[str, str]:
    total_msu = monthly_item["total_msu"].sum() if not monthly_item.empty else 0
    unique_items = monthly_item["Item Text"].nunique() if not monthly_item.empty else 0
    if not pde_alerts.empty:
        if "msu_due" in pde_alerts.columns:
            pde_open = pde_alerts["msu_due"].sum(min_count=1)
        elif "open_items" in pde_alerts.columns:
            pde_open = pde_alerts["open_items"].sum(min_count=1)
        else:
            pde_open = 0
    else:
        pde_open = 0
    return {
        "total_msu": f"{total_msu:,.0f}",
        "unique_items": str(unique_items),
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
    working["item_label"] = working["Item Text"].astype(str).str.strip()

    months = sort_month_labels(working["availability_month"].dropna().tolist())
    if not months:
        columns.append({"name": TOTAL_LABEL, "id": TOTAL_LABEL})
        return columns, []

    for month in months:
        columns.append({"name": format_month_label_slash(month), "id": month})
    columns.append({"name": TOTAL_LABEL, "id": TOTAL_LABEL})

    preferred_items = ["R Material", "RM Material", "R Quotation", "FG Rolling"]
    unique_items = [item for item in preferred_items if item in working["item_label"].unique()]
    remaining_items = [
        item
        for item in sorted(working["item_label"].unique())
        if item not in unique_items
    ]
    item_order = unique_items + remaining_items
    if not item_order:
        item_order = ["Other"]

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

    for current_role in role_order:
        role_subset = working[working["requester_role"] == current_role]
        if role_subset.empty:
            pivot = pd.DataFrame(0, index=item_order, columns=months)
        else:
            grouped = (
                role_subset.groupby(["item_label", "availability_month"], dropna=False)["total_msu"]
                .sum(min_count=1)
                .reset_index()
            )
            pivot = (
                grouped.pivot_table(
                    index="item_label",
                    columns="availability_month",
                    values="total_msu",
                    aggfunc="sum",
                    fill_value=0,
                )
                .reindex(index=item_order, fill_value=0)
                .reindex(columns=months, fill_value=0)
            )

        for idx, item_name in enumerate(item_order):
            row = {
                "Role": current_role if idx == 0 else "",
                "Item Text": item_name,
            }
            row_values = pivot.loc[item_name] if item_name in pivot.index else pd.Series(0, index=months)
            row_total = row_values.sum()
            for month in months:
                row[month] = format_value(row_values.get(month, 0))
            row[TOTAL_LABEL] = format_value(row_total)
            records.append(row)

    totals = (
        scope_df.groupby("availability_month", dropna=False)["total_msu"].sum(min_count=1)
        if not scope_df.empty
        else pd.Series(dtype=float)
    )
    total_record = {"Role": TOTAL_LABEL, "Item Text": ""}
    total_value = totals.sum() if not totals.empty else 0
    for month in months:
        total_record[month] = format_value(totals.get(month, 0))
    total_record[TOTAL_LABEL] = format_value(total_value)
    records.append(total_record)

    return columns, records


def build_item_summary(df: pd.DataFrame, role: str) -> Tuple[List[Dict], List[Dict]]:
    columns = [{"name": "Item Text", "id": "Item Text"}]
    if df.empty:
        columns.append({"name": TOTAL_LABEL, "id": TOTAL_LABEL})
        return columns, []

    scope_df = df.copy()
    scope_df["Item Text"] = scope_df["Item Text"].astype(str).str.strip()
    if role and role != ROLE_ALL_VALUE:
        scope_df = scope_df[scope_df["requester_role"] == role]

    months = sort_month_labels(scope_df["availability_month"].dropna().tolist())
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
    unique_items = [item for item in preferred_items if item in scope_df["Item Text"].unique()]
    remaining_items = [
        item
        for item in sorted(scope_df["Item Text"].unique())
        if item not in unique_items
    ]
    item_order = unique_items + remaining_items
    if not item_order:
        item_order = ["未定义"]

    grouped = (
        scope_df.groupby(["Item Text", "availability_month"], dropna=False)["total_msu"]
        .sum(min_count=1)
        .reset_index()
    )
    if grouped.empty:
        pivot = pd.DataFrame(0, index=item_order, columns=months)
    else:
        pivot = (
            grouped.pivot_table(
                index="Item Text",
                columns="availability_month",
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
        for month in months:
            row[month] = format_value(row_values.get(month, 0))
        row[TOTAL_LABEL] = format_value(row_values.sum())
        records.append(row)

    totals = (
        scope_df.groupby("availability_month", dropna=False)["total_msu"].sum(min_count=1)
        if not scope_df.empty
        else pd.Series(dtype=float)
    )
    total_record = {"Item Text": TOTAL_LABEL}
    total_value = totals.sum() if not totals.empty else 0
    for month in months:
        total_record[month] = format_value(totals.get(month, 0))
    total_record[TOTAL_LABEL] = format_value(total_value)
    records.append(total_record)

    return columns, records


def build_first_level_summary(
    df: pd.DataFrame,
    source_level_column: str = "First Level",
    display_level_column: str = "Level 1",
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
    for requester, row in pivot.iterrows():
        record = {
            "Requester Email": requester,
            "Project": project_map.get(requester, "未定义"),
        }
        for label in date_labels + [TOTAL_LABEL]:
            value = row.get(label, 0)
            record[label] = f"{value:,.0f}" if pd.notna(value) else "-"
        records.append(record)

    return columns, records


def build_hc_idp_monthly_table(df: pd.DataFrame, as_percent: bool = False) -> Tuple[List[Dict], List[Dict]]:
    if df.empty:
        return [{"name": "Prod Line", "id": "Prod Line"}, {"name": "Overall Result", "id": "Overall Result"}], []

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

    columns = [{"name": str(col), "id": str(col)} for col in working.columns]
    return columns, working.to_dict("records")


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
        return [{"name": "Prod Line", "id": "Prod Line"}, {"name": "Overall Result", "id": "Overall Result"}], []

    current = current_df.copy()
    if "Prod Line AS" not in current.columns:
        first_col = current.columns[0]
        current = current.rename(columns={first_col: "Prod Line AS"})
    for col in [c for c in current.columns if c != "Prod Line AS"]:
        current[col] = pd.to_numeric(current[col], errors="coerce").fillna(0)

    history = historical_df.copy()
    if "Prod Line AS" not in history.columns:
        return [{"name": "Prod Line", "id": "Prod Line"}, {"name": "Overall Result", "id": "Overall Result"}], []
    for col in [c for c in history.columns if c != "Prod Line AS"]:
        history[col] = pd.to_numeric(history[col], errors="coerce")

    month_cols = sorted([c for c in current.columns if re.fullmatch(r"\d{4}-\d{2}", str(c))])
    if not month_cols:
        return [{"name": "Prod Line", "id": "Prod Line"}, {"name": "Overall Result", "id": "Overall Result"}], []

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

    current_period = pd.Timestamp.today().to_period("M")
    quarter_start_month = ((current_period.month - 1) // 3) * 3 + 1
    quarter_months = [
        pd.Period(f"{current_period.year}-{quarter_start_month + i:02d}", freq="M")
        for i in range(3)
    ]
    quarter_month_labels = [p.strftime("%Y-%m") for p in quarter_months]
    prev_quarter_month_labels = [(p - 12).strftime("%Y-%m") for p in quarter_months]

    month_letter = {
        1: "J", 2: "F", 3: "M", 4: "A", 5: "M", 6: "J",
        7: "J", 8: "A", 9: "S", 10: "O", 11: "N", 12: "D",
    }
    quarter_tag = "".join(month_letter.get(p.month, "") for p in quarter_months)

    col_lbe = f"{quarter_tag} LBE"
    col_hs = f"{quarter_tag} HS"
    col_lbe_iya = f"{quarter_tag} LBE IYA"
    col_hs_iya = f"{quarter_tag} HS IYA"

    columns = base_columns + [
        {"name": col_lbe, "id": col_lbe},
        {"name": col_hs, "id": col_hs},
        {"name": col_lbe_iya, "id": col_lbe_iya},
        {"name": col_hs_iya, "id": col_hs_iya},
    ]

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
        lbe_quarter = quarter_sum(lbe, bucket, quarter_month_labels)
        hs_quarter = quarter_sum(hs, bucket, quarter_month_labels)
        lbe_prev = sum(history_lookup.get((bucket.lower(), month), 0.0) for month in prev_quarter_month_labels)
        hs_prev = lbe_prev

        lbe_iya = (lbe_quarter / lbe_prev * 100.0) if lbe_prev else None
        hs_iya = (hs_quarter / hs_prev * 100.0) if hs_prev else None

        records.append(
            {
                "Prod Line": bucket,
                col_lbe: f"{lbe_quarter:,.0f}" if lbe_quarter else "-",
                col_hs: f"{hs_quarter:,.0f}" if hs_quarter else "-",
                col_lbe_iya: f"{lbe_iya:,.1f}%" if lbe_iya is not None else "-",
                col_hs_iya: f"{hs_iya:,.1f}%" if hs_iya is not None else "-",
            }
        )

    return columns, records


def build_role_item_project_summary(df: pd.DataFrame, role: str) -> Tuple[List[Dict], List[Dict]]:
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

    if "availability_month" not in working.columns:
        working["availability_month"] = working.get("Availability Date", pd.NaT)
        working["availability_month"] = pd.to_datetime(working["availability_month"], errors="coerce").dt.to_period("M").astype(str)

    if role and role != ROLE_ALL_VALUE:
        working = working[working["requester_role"] == role]

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
        return f"{value:,.2f}"

    records_with_totals: List[Tuple[float, Dict]] = []
    for _, row in pivot.iterrows():
        record = {
            "Role": row.get("Role", UNKNOWN_ROLE) or UNKNOWN_ROLE,
            "Item Text": row.get("Item Text", "未定义") or "未定义",
            "MRP Element Indicator": row.get("MRP Element Indicator", "未定义") or "未定义",
        }
        for month in months:
            record[month] = fmt(row.get(month, 0))
        total_value = row.get(TOTAL_LABEL, 0)
        record[TOTAL_LABEL] = fmt(total_value)
        records_with_totals.append((total_value if pd.notna(total_value) else 0, record))

    sorted_records = [rec for _, rec in sorted(records_with_totals, key=lambda item: item[0], reverse=True)]
    return columns, sorted_records


def build_modal_detail_rows(df: pd.DataFrame, role: str, item_text: str, mrp_indicator: str) -> Tuple[List[Dict], List[Dict]]:
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
    options.extend(make_option(role, role) for role in roles)
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
            font=dict(color="#7f8c8d", size=14),
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
        font=dict(color="#334155"),
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
    historical_shipment = pd.DataFrame(data_bundle.get("historical_shipment", []))
    pde_alerts = pd.DataFrame(data_bundle["pde_alerts"])
    request_details = load_request_details(cfg)
    details_version = data_bundle.get("request_details_version")
    metrics = compute_metrics(monthly_item, pde_alerts)
    role_options = build_role_options(monthly_requester)
    default_role = role_options[0]["value"] if role_options else ROLE_ALL_VALUE
    role_matrix_columns, role_matrix_data = build_monthly_matrix(monthly_requester, ROLE_ALL_VALUE)
    summary_columns, summary_data = build_item_summary(monthly_requester, default_role)
    pde_columns, pde_data = build_pde_matrix(pde_alerts)
    summary_drill_columns, summary_drill_rows = build_role_item_project_summary(request_details, default_role)

    level1_columns, level1_rows = build_first_level_summary(monthly_level1, source_level_column="First Level", display_level_column="Level 1")
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
                        html.H4("Item Text 数量"),
                        html.Span(metrics["unique_items"], id="metric-item-count", className="metric-value"),
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
                                    style_data_conditional=PDE_STYLE_DATA_CONDITIONAL,
                                    style_cell_conditional=[
                                        {"if": {"column_id": "Role"}, "textAlign": "left"},
                                        {"if": {"column_id": "Item Text"}, "textAlign": "left"},
                                    ],
                                    page_size=20,
                                    style_table={"overflowX": "auto"},
                                )
                            ),
                        ],
                    ),
                ],
            ),
            html.Div(
                className="summary-panel",
                children=[
                    html.H3("Monthly Summary"),
                    DataTable(
                        id="monthly-summary",
                        columns=summary_columns,
                        data=summary_data,
                        style_header=PDE_STYLE_HEADER,
                        style_cell=PDE_STYLE_CELL,
                        style_data_conditional=PDE_STYLE_DATA_CONDITIONAL,
                        page_size=6,
                        style_table={"overflowX": "auto"},
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
                            html.H3("Role × Item × Project 汇总"),
                            html.P("点击任意组合即可查看对应的所有物料请求明细"),
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
                                style_data_conditional=PDE_STYLE_DATA_CONDITIONAL,
                                page_size=10,
                                style_table={"overflowX": "auto"},
                                sort_action="native",
                                filter_action="native",
                            )
                        )
                    ),
                    html.Div(
                        className="drill-detail-panel",
                        children=[
                            html.Div(
                                className="drill-detail-header",
                                children=[
                                    html.H4("明细列表"),
                                    html.Span("请选择上方 Role × Item × Project 组合", id="drill-detail-title"),
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
                            html.H3("Demand LBE"),
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
                            html.H3("Demand LBE IYA"),
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
                            html.H3("Demand HS"),
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
                            html.H3("Demand HS IYA"),
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
                            html.H3("Supply Protection"),
                            dcc.Loading(
                                DataTable(
                                    id="first-level-table",
                                    columns=level1_columns,
                                    data=level1_rows,
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
                            html.H3("Demand IYA by quarter"),
                            dcc.Loading(
                                DataTable(
                                    id="hc-idp-quarter-iya-table",
                                    columns=hc_idp_quarter_iya_columns,
                                    data=hc_idp_quarter_iya_rows,
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

    return html.Div(
        className="page",
        children=[
            dcc.Interval(id="refresh-interval", interval=15 * 60 * 1000, n_intervals=0),
            dcc.Store(id="data-store", data=data_bundle),
            dcc.Store(id="details-version-store", data={"version": details_version}),
            html.Div(
                className="hero",
                children=[
                    html.Div(
                        [html.H1("Hair Care Protection Command Center"), html.P("(MVP)")]
                    )
                ],
            ),
            dcc.Tabs(id="page-tabs", value="demand-assumption", children=[demand_assumption_tab, overview_tab, drill_tab]),
        ],
    )


def register_callbacks(app: Dash, cfg: AppConfig) -> None:
    @app.callback(
        Output("data-store", "data"),
        Output("details-version-store", "data"),
        Input("refresh-interval", "n_intervals"),
    )
    def refresh_data(_):
        bundle = load_data_bundle(cfg)
        return bundle, {"version": bundle.get("request_details_version")}

    @app.callback(
        Output("metric-total-msu", "children"),
        Output("metric-item-count", "children"),
        Output("metric-pde-open", "children"),
        Input("data-store", "data"),
    )
    def update_metrics(data):
        monthly_item = pd.DataFrame(data.get("monthly_item", []))
        pde_alerts = pd.DataFrame(data.get("pde_alerts", []))
        metrics = compute_metrics(monthly_item, pde_alerts)
        return metrics["total_msu"], metrics["unique_items"], metrics["pde_open"]

    @app.callback(
        Output("role-item-table", "columns"),
        Output("role-item-table", "data"),
        Output("monthly-summary", "columns"),
        Output("monthly-summary", "data"),
        Output("role-trend", "figure"),
        Output("pde-table", "columns"),
        Output("pde-table", "data"),
        Output("role-item-mrp-summary", "columns"),
        Output("role-item-mrp-summary", "data"),
        Output("hc-idp-monthly-table", "columns"),
        Output("hc-idp-monthly-table", "data"),
        Output("hc-idp-monthly-iya-table", "columns"),
        Output("hc-idp-monthly-iya-table", "data"),
        Output("hc-idp-hs-table", "columns"),
        Output("hc-idp-hs-table", "data"),
        Output("hc-idp-hs-iya-table", "columns"),
        Output("hc-idp-hs-iya-table", "data"),
        Output("hc-idp-quarter-iya-table", "columns"),
        Output("hc-idp-quarter-iya-table", "data"),
        Output("first-level-table", "columns"),
        Output("first-level-table", "data"),
        Input("data-store", "data"),
        Input("role-filter", "value"),
    )
    def update_visuals(data, role_value):
        monthly_requester = pd.DataFrame(data.get("monthly_requester", []))
        monthly_level1 = pd.DataFrame(data.get("monthly_level1", []))
        hc_idp_monthly = pd.DataFrame(data.get("hc_idp_monthly", []))
        historical_shipment = pd.DataFrame(data.get("historical_shipment", []))
        pde_alerts = pd.DataFrame(data.get("pde_alerts", []))
        request_details = load_request_details(cfg)
        table_columns, table_data = build_monthly_matrix(monthly_requester, ROLE_ALL_VALUE)
        selected_role = role_value or ROLE_ALL_VALUE
        summary_columns, summary_data = build_item_summary(monthly_requester, selected_role)
        role_fig = build_role_trend(monthly_requester, selected_role)
        pde_columns, pde_records = build_pde_matrix(pde_alerts)
        drill_columns, drill_rows = build_role_item_project_summary(request_details, selected_role)
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
        level1_columns, level1_rows = build_first_level_summary(monthly_level1, source_level_column="First Level", display_level_column="Level 1")
        return (
            table_columns,
            table_data,
            summary_columns,
            summary_data,
            role_fig,
            pde_columns,
            pde_records,
            drill_columns,
            drill_rows,
            hc_idp_columns,
            hc_idp_rows,
            hc_idp_iya_columns,
            hc_idp_iya_rows,
            hc_idp_hs_columns,
            hc_idp_hs_rows,
            hc_idp_hs_iya_columns,
            hc_idp_hs_iya_rows,
            hc_idp_quarter_iya_columns,
            hc_idp_quarter_iya_rows,
            level1_columns,
            level1_rows,
        )

    @app.callback(
        Output("drill-detail-title", "children"),
        Output("drill-detail-table", "columns"),
        Output("drill-detail-table", "data"),
        Input("role-item-mrp-summary", "active_cell"),
        Input("details-version-store", "data"),
        State("role-item-mrp-summary", "data"),
        State("role-item-mrp-summary", "derived_viewport_data"),
    )
    def update_drill_details(active_cell, _version_data, summary_rows, viewport_rows):
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
        role = selected_row.get("Role") or ROLE_ALL_VALUE
        item_text = selected_row.get("Item Text") or ""
        mrp_indicator = selected_row.get("MRP Element Indicator") or ""

        try:
            columns, rows = build_modal_detail_rows(details, role, item_text, mrp_indicator)
        except Exception:
            logging.exception("Failed to build drill detail rows: role=%s item=%s mrp=%s", role, item_text, mrp_indicator)
            return "加载明细失败，请稍后重试", [{"name": "错误", "id": "error"}], [{"error": "加载明细失败"}]

        title_parts = [part for part in [role, item_text, mrp_indicator] if part]
        title = " / ".join(title_parts) if title_parts else "明细列表"
        if not rows:
            return f"{title} · 暂无数据", columns or default_columns, rows
        return title, columns, rows


def create_app() -> Dash:
    cfg = AppConfig.load(CONFIG_PATH)
    app = Dash(__name__, title="MatRes Command Center", assets_folder=str(Path(__file__).parent / "assets"))
    app.layout = build_layout(app, cfg)
    register_callbacks(app, cfg)
    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
