"""Pure calculation helpers for the weekly email supply summary."""
from __future__ import annotations

import html
from typing import Dict
from urllib.parse import urlsplit

import pandas as pd


def safe_dashboard_url(value: str, fallback: str) -> str:
    """Return an escaped HTTP(S) dashboard URL or a known-safe fallback."""
    try:
        parsed = urlsplit(str(value).strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return html.escape(fallback, quote=True)
        return html.escape(str(value).strip(), quote=True)
    except (TypeError, ValueError):
        return html.escape(fallback, quote=True)


def calculate_supply_inventory(
    monthly_item: pd.DataFrame,
    hppp_series: pd.Series,
) -> Dict[str, float]:
    """Return FG, material, HPPP and total MSU for the email summary."""
    if monthly_item is not None and not monthly_item.empty:
        work = monthly_item.copy()
        if "total_msu" not in work.columns:
            work["total_msu"] = 0.0
        work["total_msu"] = pd.to_numeric(work["total_msu"], errors="coerce").fillna(0.0)
        if "Item Text" in work.columns:
            item_sum = work.groupby("Item Text", dropna=False)["total_msu"].sum(min_count=1).to_dict()
        else:
            item_sum = {}
    else:
        item_sum = {}

    fg = float(sum(item_sum.get(name, 0.0) for name in ("FG Rolling", "R Quotation", "R Component")))
    material = float(sum(item_sum.get(name, 0.0) for name in ("R Material", "RM Material")))
    hppp = (
        float(pd.to_numeric(hppp_series, errors="coerce").fillna(0.0).sum())
        if hppp_series is not None and not hppp_series.empty
        else 0.0
    )
    return {"fg": fg, "material": material, "hppp": hppp, "total": fg + material + hppp}


def format_supply_inventory(inventory: Dict[str, float]) -> str:
    """Format the auditable Supply Summary component breakdown."""
    return (
        f"Total: {inventory.get('total', 0.0):,.0f} msu; "
        f"FG: {inventory.get('fg', 0.0):,.0f} msu; "
        f"Material: {inventory.get('material', 0.0):,.0f} msu; "
        f"HPPP: {inventory.get('hppp', 0.0):,.0f} msu."
    )