"""Plotly Dash application for the MatRes dashboard MVP."""
from __future__ import annotations

import json
import logging
import os
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


def load_data_bundle(cfg: AppConfig) -> Dict[str, Any]:
    monthly_item = load_dataset(cfg.processed_dir, "monthly_msu_by_item_text.csv")
    monthly_requester = load_dataset(cfg.processed_dir, "monthly_msu_by_requester_item.csv")
    pde_alerts = load_dataset(cfg.processed_dir, "pde_alerts.csv")
    request_details_path = cfg.processed_dir / "matres_request_details.csv"
    details_version = request_details_path.stat().st_mtime if request_details_path.exists() else None

    return {
        "monthly_item": monthly_item.to_dict("records"),
        "monthly_requester": monthly_requester.to_dict("records"),
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

PDE_STYLE_HEADER = {"backgroundColor": "rgba(18,18,18,0.8)", "color": "#00f5ff", "border": "none"}
PDE_STYLE_CELL = {
    "backgroundColor": "rgba(10,10,10,0.6)",
    "color": "#e6f7ff",
    "border": "none",
    "textAlign": "center",
}
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
        columns.append({"name": month, "id": month})
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
    if role and role != ROLE_ALL_VALUE:
        role_order = [role]
    else:
        preferred_roles = ["IOL", "CSP", "CROSS REGION", UNKNOWN_ROLE]
        role_order = [r for r in preferred_roles if r in seen_roles]
        role_order.extend(r for r in seen_roles if r not in role_order)

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
        columns.append({"name": month, "id": month})
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
                hovertemplate="%{x}<br>%{y:,.0f} MSU<extra>%{fullData.name}</extra>",
            )

    totals = pivot.sum(axis=0).reindex(all_months, fill_value=0)
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

    title_role = "全部角色" if not role or role == ROLE_ALL_VALUE else role
    fig.update_layout(
        title=f"{title_role} · Role by Monthly MSU",
        barmode="stack",
        legend=dict(orientation="h", x=0.5, xanchor="center", y=1.12),
        yaxis_title="MSU",
        xaxis_title="月份",
        template="plotly_dark",
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    fig.update_xaxes(categoryorder="array", categoryarray=all_months)
    return fig


def build_layout(app: Dash, cfg: AppConfig) -> html.Div:
    data_bundle = load_data_bundle(cfg)
    monthly_item = pd.DataFrame(data_bundle["monthly_item"])
    monthly_requester = pd.DataFrame(data_bundle["monthly_requester"])
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

    overview_tab = dcc.Tab(
        label="Dashboard 总览",
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
                            html.Div(
                                className="matrix-header",
                                children=[
                                    html.H3("Role × Item Monthly Matrix"),
                                    html.P(" " ),
                                ],
                            ),
                            dcc.Loading(
                                DataTable(
                                    id="role-item-table",
                                    columns=role_matrix_columns,
                                    data=role_matrix_data,
                                    style_header=PDE_STYLE_HEADER,
                                    style_cell=PDE_STYLE_CELL,
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
                        page_size=10,
                        style_table={"overflowX": "auto"},
                    ),
                ],
            ),
        ],
    )

    drill_tab = dcc.Tab(
        label="Project Drill",
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
            dcc.Tabs(id="page-tabs", value="overview", children=[overview_tab, drill_tab]),
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
        Input("data-store", "data"),
        Input("role-filter", "value"),
    )
    def update_visuals(data, role_value):
        monthly_requester = pd.DataFrame(data.get("monthly_requester", []))
        pde_alerts = pd.DataFrame(data.get("pde_alerts", []))
        request_details = load_request_details(cfg)
        table_columns, table_data = build_monthly_matrix(monthly_requester, ROLE_ALL_VALUE)
        selected_role = role_value or ROLE_ALL_VALUE
        summary_columns, summary_data = build_item_summary(monthly_requester, selected_role)
        role_fig = build_role_trend(monthly_requester, selected_role)
        pde_columns, pde_records = build_pde_matrix(pde_alerts)
        drill_columns, drill_rows = build_role_item_project_summary(request_details, selected_role)
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
        )

    @app.callback(
        Output("drill-detail-title", "children"),
        Output("drill-detail-table", "columns"),
        Output("drill-detail-table", "data"),
        Input("role-item-mrp-summary", "active_cell"),
        Input("details-version-store", "data"),
        State("role-item-mrp-summary", "data"),
    )
    def update_drill_details(active_cell, _version_data, summary_rows):
        details = load_request_details(cfg)
        default_columns = [{"name": field, "id": field} for field in DETAIL_VIEW_FIELDS]
        if not active_cell or not summary_rows:
            return "请选择上方 Role × Item × Project 组合", default_columns, []

        row_index = active_cell.get("row")
        if row_index is None or row_index < 0 or row_index >= len(summary_rows):
            return "请选择上方 Role × Item × Project 组合", default_columns, []

        selected_row = summary_rows[row_index]
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
