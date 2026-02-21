"""Plotly Dash application for the MatRes dashboard MVP."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

import dash
from dash import Dash, Input, Output, dcc, html
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


def load_data_bundle(cfg: AppConfig) -> Dict[str, List[Dict]]:
    monthly_item = load_dataset(cfg.processed_dir, "monthly_msu_by_item_text.csv")
    monthly_requester = load_dataset(cfg.processed_dir, "monthly_msu_by_requester_item.csv")
    pde_alerts = load_dataset(cfg.processed_dir, "pde_alerts.csv")

    return {
        "monthly_item": monthly_item.to_dict("records"),
        "monthly_requester": monthly_requester.to_dict("records"),
        "pde_alerts": pde_alerts.to_dict("records"),
    }


def build_item_trend(df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    if df.empty:
        fig.update_layout(title="暂无数据")
        return fig

    frame = df.copy()
    frame["month_order"] = pd.to_datetime(frame["availability_month"], format="%Y-%m", errors="coerce")
    frame = frame.sort_values("month_order")
    top_items = (
        frame.groupby("Item Text")["total_msu"].sum().sort_values(ascending=False).head(6).index.tolist()
    )
    frame["item_bucket"] = frame["Item Text"].where(frame["Item Text"].isin(top_items), "其他")
    stacked = frame.groupby(["availability_month", "item_bucket"], as_index=False)["total_msu"].sum()
    month_order = [m for m in frame["availability_month"].dropna().unique().tolist() if isinstance(m, str)]

    for item in stacked["item_bucket"].unique():
        subset = stacked[stacked["item_bucket"] == item]
        fig.add_bar(
            name=item,
            x=subset["availability_month"],
            y=subset["total_msu"],
            text=subset["total_msu"].map(lambda v: f"{v:,.0f}"),
            textposition="inside",
            hovertemplate="%{x}<br>%{y:,.0f} MSU<extra>%{fullData.name}</extra>",
        )

    totals = stacked.groupby("availability_month")["total_msu"].sum().reset_index()
    fig.add_scatter(
        name="月度总计",
        x=totals["availability_month"],
        y=totals["total_msu"],
        mode="lines+markers+text",
        text=totals["total_msu"].map(lambda v: f"{v:,.0f}"),
        textposition="top center",
        line=dict(color="#00f5ff", width=3),
    )

    fig.update_layout(
        title="Item Text × 月度 MSU 堆叠",
        barmode="stack",
        legend=dict(orientation="h", x=0.5, xanchor="center", y=1.12),
        yaxis_title="MSU",
        xaxis_title="月份",
        template="plotly_dark",
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    fig.update_xaxes(categoryorder="array", categoryarray=month_order)
    return fig


PDE_STYLE_HEADER = {"backgroundColor": "rgba(18,18,18,0.8)", "color": "#00f5ff", "border": "none"}
PDE_STYLE_CELL = {
    "backgroundColor": "rgba(10,10,10,0.6)",
    "color": "#e6f7ff",
    "border": "none",
    "textAlign": "center",
}
TOTAL_LABEL = "汇总"
ROLE_ALL_VALUE = "ALL"


def compute_metrics(monthly_item: pd.DataFrame, pde_alerts: pd.DataFrame) -> Dict[str, str]:
    total_msu = monthly_item["total_msu"].sum() if not monthly_item.empty else 0
    unique_items = monthly_item["Item Text"].nunique() if not monthly_item.empty else 0
    pde_open = int(pde_alerts["open_items"].sum()) if not pde_alerts.empty else 0
    return {
        "total_msu": f"{total_msu:,.0f}",
        "unique_items": str(unique_items),
        "pde_open": str(pde_open),
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
    if df.empty:
        columns = [
            {"name": "Item Text", "id": "Item Text"},
            {"name": "月份", "id": "无数据"},
        ]
        return columns, []

    grouped_all = (
        df.groupby(["Item Text", "availability_month"], dropna=False)["total_msu"]
        .sum(min_count=1)
        .reset_index()
    )

    if grouped_all.empty:
        columns = [
            {"name": "Item Text", "id": "Item Text"},
            {"name": "月份", "id": "无数据"},
        ]
        return columns, []

    months = sort_month_labels(grouped_all["availability_month"].dropna().tolist())
    base_pivot = (
        grouped_all.pivot_table(
            index="Item Text",
            columns="availability_month",
            values="total_msu",
            aggfunc="sum",
            fill_value=0,
        )
        .reindex(columns=months)
        .sort_index()
    )
    base_index = base_pivot.index

    working = df.copy()
    if role and role != ROLE_ALL_VALUE and "requester_role" in working.columns:
        working = working[working["requester_role"] == role]

    grouped_role = (
        working.groupby(["Item Text", "availability_month"], dropna=False)["total_msu"]
        .sum(min_count=1)
        .reset_index()
    )

    if grouped_role.empty:
        pivot = pd.DataFrame(0, index=base_index, columns=months)
    else:
        pivot = (
            grouped_role.pivot_table(
                index="Item Text",
                columns="availability_month",
                values="total_msu",
                aggfunc="sum",
                fill_value=0,
            )
            .reindex(columns=months, fill_value=0)
            .reindex(index=base_index, fill_value=0)
        )

    pivot[TOTAL_LABEL] = pivot.sum(axis=1)
    total_row = pivot.sum(axis=0).to_frame().T
    total_row.index = [TOTAL_LABEL]
    pivot = pd.concat([pivot, total_row])

    columns = [{"name": "Item Text", "id": "Item Text"}]
    for month in months:
        columns.append({"name": month, "id": month})
    columns.append({"name": TOTAL_LABEL, "id": TOTAL_LABEL})

    records: List[Dict] = []
    for item_text, row in pivot.iterrows():
        record = {"Item Text": item_text}
        for column in months + [TOTAL_LABEL]:
            value = row.get(column, 0)
            record[column] = f"{value:,.0f}" if pd.notna(value) else "-"
        records.append(record)

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


def build_role_options(df: pd.DataFrame) -> List[Dict[str, Any]]:
    if "requester_role" in df.columns:
        roles = sorted({role for role in df["requester_role"].dropna().tolist() if role})
    else:
        roles = []

    def make_option(label: str, value: str) -> Dict[str, Any]:
        return {"label": html.Span(label, className="role-chip-label"), "value": value}

    options: List[Dict[str, Any]] = [make_option("全部角色", ROLE_ALL_VALUE)]
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
        title=f"{title_role} · Role 月度 MSU",
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
    metrics = compute_metrics(monthly_item, pde_alerts)
    role_options = build_role_options(monthly_requester)
    default_role = role_options[0]["value"] if role_options else ROLE_ALL_VALUE
    matrix_columns, matrix_data = build_monthly_matrix(monthly_requester, default_role)
    pde_columns, pde_data = build_pde_matrix(pde_alerts)

    return html.Div(
        className="page",
        children=[
            dcc.Interval(id="refresh-interval", interval=15 * 60 * 1000, n_intervals=0),
            dcc.Store(id="data-store", data=data_bundle),
            html.Div(
                className="hero",
                children=[
                    html.Div(
                        [html.H1("MatRes Command Center"), html.P("科技风 MatRes 数据驾驶舱 (MVP)")]
                    )
                ],
            ),
            html.Div(
                className="metrics",
                children=[
                    html.Div([
                        html.H4("月度总 MSU"),
                        html.Span(metrics["total_msu"], id="metric-total-msu", className="metric-value"),
                    ]),
                    html.Div([
                        html.H4("Item Text 数量"),
                        html.Span(metrics["unique_items"], id="metric-item-count", className="metric-value"),
                    ]),
                    html.Div([
                        html.H4("PDE 告警"),
                        html.Span(metrics["pde_open"], id="metric-pde-open", className="metric-value warning"),
                    ]),
                ],
            ),
            html.Div(
                className="charts-grid",
                children=[
                    dcc.Loading(dcc.Graph(id="item-trend", figure=build_item_trend(monthly_item))),
                    html.Div(
                        className="role-card",
                        children=[
                            html.Div(
                                className="role-filter",
                                children=[
                                    html.Span("Role 选择"),
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
                ],
            ),
            html.Div(
                className="summary-panel",
                children=[
                    html.H3("月度 MSU 汇总"),
                    DataTable(
                        id="monthly-summary",
                        columns=matrix_columns,
                        data=matrix_data,
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
                    html.H3("PDE Past Due 提醒"),
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


def register_callbacks(app: Dash, cfg: AppConfig) -> None:
    @app.callback(Output("data-store", "data"), Input("refresh-interval", "n_intervals"))
    def refresh_data(_):
        return load_data_bundle(cfg)

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
        Output("item-trend", "figure"),
        Output("role-trend", "figure"),
        Output("monthly-summary", "columns"),
        Output("monthly-summary", "data"),
        Output("pde-table", "columns"),
        Output("pde-table", "data"),
        Input("data-store", "data"),
        Input("role-filter", "value"),
    )
    def update_visuals(data, role_value):
        monthly_item = pd.DataFrame(data.get("monthly_item", []))
        monthly_requester = pd.DataFrame(data.get("monthly_requester", []))
        pde_alerts = pd.DataFrame(data.get("pde_alerts", []))
        item_fig = build_item_trend(monthly_item)
        selected_role = role_value or ROLE_ALL_VALUE
        role_fig = build_role_trend(monthly_requester, selected_role)
        summary_columns, monthly_summary = build_monthly_matrix(monthly_requester, selected_role)
        pde_columns, pde_records = build_pde_matrix(pde_alerts)
        return item_fig, role_fig, summary_columns, monthly_summary, pde_columns, pde_records


def create_app() -> Dash:
    cfg = AppConfig.load(CONFIG_PATH)
    app = Dash(__name__, title="MatRes Command Center", assets_folder=str(Path(__file__).parent / "assets"))
    app.layout = build_layout(app, cfg)
    register_callbacks(app, cfg)
    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
