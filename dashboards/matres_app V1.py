"""Plotly Dash application for the MatRes dashboard MVP."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import dash
from dash import Dash, Input, Output, dcc, html
from dash.dash_table import DataTable
import pandas as pd
import plotly.express as px
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
    stacked = (
        frame.groupby(["availability_month", "item_bucket"], as_index=False)["total_msu"].sum()
    )
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


def build_requester_view(df: pd.DataFrame) -> go.Figure:
    if df.empty:
        fig = px.bar(title="暂无数据")
        fig.update_layout(template="plotly_dark", plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
        return fig

    frame = df.copy()
    frame["month_order"] = pd.to_datetime(frame["availability_month"], format="%Y-%m", errors="coerce")
    frame = frame.sort_values("month_order")

    top_requesters = (
        frame.groupby("Requester Email")["total_msu"].sum().sort_values(ascending=False).head(6).index.tolist()
    )
    frame = frame[frame["Requester Email"].isin(top_requesters)]

    fig = px.bar(
        frame,
        x="availability_month",
        y="total_msu",
        color="Item Text",
        facet_col="Requester Email",
        facet_col_wrap=3,
        category_orders={"availability_month": frame["availability_month"].dropna().unique().tolist()},
        title="Requester × Item Text 月度 MSU",
        hover_data={"total_msu": ":,.0f"},
    )
    fig.update_layout(
        barmode="stack",
        legend_title_text="Item Text",
        template="plotly_dark",
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    for axis in fig.select_yaxes():
        axis.update(title="MSU")
    for axis in fig.select_xaxes():
        axis.update(title="月份")
    return fig


PDE_COLUMNS = [
    {"name": "Requester", "id": "Requester Email"},
    {"name": "Availability Date", "id": "availability_date"},
    {"name": "Month", "id": "availability_month"},
    {"name": "MSU Due", "id": "msu_due"},
    {"name": "Open Items", "id": "open_items"},
    {"name": "Max PDE", "id": "max_pde"},
    {"name": "Avg PDE", "id": "avg_pde"},
    {"name": "Closest Availability", "id": "closest_availability"},
]

PDE_STYLE_HEADER = {"backgroundColor": "rgba(18,18,18,0.8)", "color": "#00f5ff", "border": "none"}
PDE_STYLE_CELL = {
    "backgroundColor": "rgba(10,10,10,0.6)",
    "color": "#e6f7ff",
    "border": "none",
    "textAlign": "center",
}
PDE_STYLE_DATA_COND = [
    {
        "if": {"filter_query": "{max_pde} >= 7"},
        "backgroundColor": "rgba(255,77,77,0.15)",
        "color": "#ff6b6b",
    }
]

MONTH_TOTAL_LABEL = "汇总"


def format_pde_records(df: pd.DataFrame) -> List[Dict]:
    display_df = df.copy()
    if display_df.empty:
        return []

    if "msu_due" in display_df.columns:
        display_df["msu_due"] = display_df["msu_due"].map(lambda v: f"{v:,.0f}")
    for column in ("availability_date", "closest_availability"):
        if column in display_df.columns:
            display_df[column] = (
                pd.to_datetime(display_df[column], errors="coerce").dt.strftime("%Y-%m-%d")
            )
    return display_df.to_dict("records")


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


def build_monthly_matrix(df: pd.DataFrame) -> Tuple[List[Dict], List[Dict]]:
    if df.empty:
        columns = [
            {"name": "Item Text", "id": "Item Text"},
            {"name": "月份", "id": "无数据"},
        ]
        return columns, []

    months = sort_month_labels(df["availability_month"].dropna().tolist())
    pivot = (
        df.pivot_table(index="Item Text", columns="availability_month", values="total_msu", aggfunc="sum", fill_value=0)
        .reindex(columns=months)
        .sort_index()
    )

    pivot[MONTH_TOTAL_LABEL] = pivot.sum(axis=1)
    total_row = pivot.sum(axis=0).to_frame().T
    total_row.index = [MONTH_TOTAL_LABEL]
    pivot = pd.concat([pivot, total_row])

    columns = [{"name": "Item Text", "id": "Item Text"}]
    for month in months:
        columns.append({"name": month, "id": month})
    columns.append({"name": MONTH_TOTAL_LABEL, "id": MONTH_TOTAL_LABEL})

    records: List[Dict] = []
    for item_text, row in pivot.iterrows():
        record = {"Item Text": item_text}
        for column in months + [MONTH_TOTAL_LABEL]:
            value = row.get(column, 0)
            record[column] = f"{value:,.0f}" if pd.notna(value) else "-"
        records.append(record)

    return columns, records


def build_layout(app: Dash, cfg: AppConfig) -> html.Div:
    data_bundle = load_data_bundle(cfg)
    monthly_item = pd.DataFrame(data_bundle["monthly_item"])
    monthly_requester = pd.DataFrame(data_bundle["monthly_requester"])
    pde_alerts = pd.DataFrame(data_bundle["pde_alerts"])
    metrics = compute_metrics(monthly_item, pde_alerts)
    pde_records = format_pde_records(pde_alerts)
    matrix_columns, matrix_data = build_monthly_matrix(monthly_item)

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
                    dcc.Loading(dcc.Graph(id="requester-view", figure=build_requester_view(monthly_requester))),
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
                        columns=PDE_COLUMNS,
                        data=pde_records,
                        style_header=PDE_STYLE_HEADER,
                        style_cell=PDE_STYLE_CELL,
                        style_data_conditional=PDE_STYLE_DATA_COND,
                        page_size=10,
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
        Output("requester-view", "figure"),
        Output("monthly-summary", "columns"),
        Output("monthly-summary", "data"),
        Output("pde-table", "data"),
        Input("data-store", "data"),
    )
    def update_visuals(data):
        monthly_item = pd.DataFrame(data.get("monthly_item", []))
        monthly_requester = pd.DataFrame(data.get("monthly_requester", []))
        pde_alerts = pd.DataFrame(data.get("pde_alerts", []))
        item_fig = build_item_trend(monthly_item)
        requester_fig = build_requester_view(monthly_requester)
        summary_columns, monthly_summary = build_monthly_matrix(monthly_item)
        pde_records = format_pde_records(pde_alerts)
        return item_fig, requester_fig, summary_columns, monthly_summary, pde_records


def create_app() -> Dash:
    cfg = AppConfig.load(CONFIG_PATH)
    app = Dash(__name__, title="MatRes Command Center", assets_folder=str(Path(__file__).parent / "assets"))
    app.layout = build_layout(app, cfg)
    register_callbacks(app, cfg)
    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
