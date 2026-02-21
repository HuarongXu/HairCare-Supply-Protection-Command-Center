"""MatRes data pipeline.

Reads the configured Excel workbook, cleans the MatRes Record sheet, produces the
aggregations required by the dashboard, and optionally appends the raw snapshot
into a historical store.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import pandas as pd
from zoneinfo import ZoneInfo

DEFAULT_CONFIG_PATH = Path("config/config.json")
DATE_COLUMNS = [
    "Availability Date",
    "Request Date",
    "Upload Date",
    "DeleteDate",
]
NUMERIC_COLUMNS = ["MSU", "Quantity", "PDE Checking"]
PROCESSED_FILES = {
    "monthly_item": "monthly_msu_by_item_text.csv",
    "monthly_requester": "monthly_msu_by_requester_item.csv",
    "monthly_level1": "monthly_msu_by_level1.csv",
    "pde_alerts": "pde_alerts.csv",
    "request_details": "matres_request_details.csv",
    "level1_unmapped": "level1_unmapped_materials.csv",
    "hc_idp_monthly": "hc_idp_monthly_summary.csv",
}
UNKNOWN_ROLE = "Others"
DETAIL_COLUMNS = [
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
]


@dataclass
class PipelineConfig:
    workbook_path: Path
    sheet_name: str
    history_path: Path
    processed_dir: Path
    level1_workbook_path: Optional[Path] = None
    level1_sheet_name: str = "Seg summary by code_New Version"
    level1_material_column: str = "material_num"
    level1_first_level_column: str = "First level"
    time_zone: str = "UTC"
    refresh_opts: Optional[Dict] = None
    role_lookup: Dict[str, str] = None

    @staticmethod
    def from_dict(raw: Dict) -> "PipelineConfig":
        root = Path.cwd()
        workbook_path = Path(raw["workbook_path"])
        if not workbook_path.is_absolute():
            workbook_path = root / workbook_path

        level1_workbook_path: Optional[Path] = None
        level1_raw = raw.get("level1_workbook_path")
        if isinstance(level1_raw, str) and level1_raw.strip():
            candidate = Path(level1_raw.strip())
            if not candidate.is_absolute():
                candidate = root / candidate
            level1_workbook_path = candidate

        history_path = Path(raw["history_path"])
        if not history_path.is_absolute():
            history_path = root / history_path

        processed_dir = Path(raw["processed_dir"])
        if not processed_dir.is_absolute():
            processed_dir = root / processed_dir

        roles_path = raw.get("requester_roles_path")
        role_lookup = load_role_lookup(root / roles_path) if roles_path else {}

        return PipelineConfig(
            workbook_path=workbook_path,
            sheet_name=raw.get("sheet_name", "MatRes Record"),
            level1_workbook_path=level1_workbook_path,
            level1_sheet_name=raw.get("level1_sheet_name", "Seg summary by code_New Version"),
            level1_material_column=raw.get("level1_material_column", "material_num"),
            level1_first_level_column=raw.get("level1_first_level_column", "First level"),
            history_path=history_path,
            processed_dir=processed_dir,
            time_zone=raw.get("time_zone", "UTC"),
            refresh_opts=raw.get("refresh", {}),
            role_lookup=role_lookup,
        )


def normalize_material_key(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            as_int = int(value)
            if float(as_int) == float(value):
                return str(as_int)
        except Exception:
            pass

    text = str(value).strip()
    if not text:
        return ""
    if re.fullmatch(r"\d+\.0", text):
        return text[:-2]

    if re.fullmatch(r"\d+", text):
        stripped = text.lstrip("0")
        return stripped if stripped else "0"
    return text


def read_level1_mapping(cfg: PipelineConfig) -> pd.DataFrame:
    workbook = cfg.level1_workbook_path
    if workbook is None:
        logging.info("Level1 mapping workbook not configured; skipping Level1 summary")
        return pd.DataFrame(columns=["material_key", cfg.level1_first_level_column])
    if not workbook.exists():
        logging.warning("Level1 mapping workbook %s not found; skipping Level1 summary", workbook)
        return pd.DataFrame(columns=["material_key", cfg.level1_first_level_column])

    sheet = cfg.level1_sheet_name
    logging.info("Reading Level1 mapping sheet '%s' from %s", sheet, workbook)
    mapping = pd.read_excel(workbook, sheet_name=sheet)

    material_col = cfg.level1_material_column
    level_col = cfg.level1_first_level_column
    if material_col not in mapping.columns or level_col not in mapping.columns:
        logging.warning(
            "Level1 mapping sheet missing required columns: need '%s' and '%s'", material_col, level_col
        )
        return pd.DataFrame(columns=["material_key", level_col])

    mapping = mapping[[material_col, level_col]].copy()
    mapping["material_key"] = mapping[material_col].apply(normalize_material_key)
    mapping[level_col] = mapping[level_col].astype(str).str.strip()
    mapping = mapping[mapping["material_key"].astype(bool)].copy()
    mapping = mapping.drop_duplicates(subset=["material_key"], keep="first")
    return mapping[["material_key", level_col]]


def summarize_monthly_by_first_level(df: pd.DataFrame, mapping: pd.DataFrame, cfg: PipelineConfig) -> pd.DataFrame:
    level_col = cfg.level1_first_level_column
    if (
        df.empty
        or "MSU" not in df.columns
        or "availability_month" not in df.columns
        or "Material Number" not in df.columns
    ):
        return pd.DataFrame(columns=["availability_month", level_col, "total_msu"])

    selected_cols = ["availability_month", "Material Number", "MSU"]
    if "Item Text" in df.columns:
        selected_cols.append("Item Text")
    working = df[selected_cols].copy()
    working["material_key"] = working["Material Number"].apply(normalize_material_key)
    working["MSU"] = pd.to_numeric(working["MSU"], errors="coerce")

    if mapping is not None and not mapping.empty and "material_key" in mapping.columns and level_col in mapping.columns:
        working = working.merge(mapping, on="material_key", how="left")
    else:
        working[level_col] = None

    raw_level = working[level_col].fillna("").astype(str).str.strip()
    missing_material = working["material_key"].fillna("").astype(str).str.strip().eq("")
    raw_level = raw_level.mask(raw_level.str.lower().isin({"nan", "none"}), "")
    working[level_col] = raw_level
    item_text = working["Item Text"].fillna("").astype(str).str.strip().str.lower() if "Item Text" in working.columns else ""
    is_rm_material = item_text.eq("rm material") if isinstance(item_text, pd.Series) else pd.Series(False, index=working.index)
    working.loc[~missing_material & (working[level_col] == "") & is_rm_material, level_col] = "Base"
    working.loc[missing_material, level_col] = "物料号缺失"
    working.loc[~missing_material & (working[level_col] == ""), level_col] = "未映射"

    agg = (
        working.groupby(["availability_month", level_col], dropna=False)["MSU"]
        .sum(min_count=1)
        .reset_index()
        .rename(columns={"MSU": "total_msu"})
    )
    return agg.sort_values(["availability_month", "total_msu"], ascending=[True, False])


def find_latest_hc_idp_report(root: Path) -> Optional[Path]:
    patterns = [
        "HC IDP HANA TD Report*.xlsx",
        "HC IDP HANA TD Report*.xlsm",
        "HC IDP HANA TD Report*.xls",
    ]
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(root.glob(pattern))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def normalize_hc_idp_prod_line_bucket(value: object) -> Optional[str]:
    text = str(value).strip().lower()
    if "base" in text:
        return "Base"
    if "promotion" in text:
        return "Promotion"
    return None


def summarize_hc_idp_weekly_current_month(report_path: Path) -> pd.DataFrame:
    try:
        raw = pd.read_excel(report_path, sheet_name="Weekly(TP)", header=None)
    except Exception:
        logging.exception("Failed to read Weekly(TP) sheet from %s", report_path)
        return pd.DataFrame(columns=["Prod Line AS"])

    # In current report layout, ER -> LBE is column 147 and Prod Line is column 153.
    lbe_col_idx = 147
    prod_line_col_idx = 153
    data_start_row = 15
    if raw.shape[1] <= max(lbe_col_idx, prod_line_col_idx) or raw.shape[0] <= data_start_row:
        logging.warning("Weekly(TP) sheet layout is smaller than expected in %s", report_path)
        return pd.DataFrame(columns=["Prod Line AS"])

    current_month_label = pd.Timestamp.today().strftime("%Y-%m")
    working = raw.iloc[data_start_row:, [prod_line_col_idx, lbe_col_idx]].copy()
    working.columns = ["prod_line", "lbe"]
    working["Prod Line AS"] = working["prod_line"].apply(normalize_hc_idp_prod_line_bucket)
    working = working[working["Prod Line AS"].notna()].copy()
    if working.empty:
        return pd.DataFrame(columns=["Prod Line AS", current_month_label])

    # Source is SU; convert to MSU for dashboard display.
    working["lbe"] = pd.to_numeric(working["lbe"], errors="coerce").fillna(0) / 1000.0
    grouped = (
        working.groupby("Prod Line AS", dropna=False)["lbe"]
        .sum(min_count=1)
        .reindex(["Base", "Promotion"], fill_value=0)
        .reset_index()
        .rename(columns={"lbe": current_month_label})
    )
    return grouped


def summarize_hc_idp_monthly(root: Path) -> pd.DataFrame:
    report_path = find_latest_hc_idp_report(root)
    if report_path is None:
        logging.warning("No HC IDP HANA TD Report file found under %s", root)
        return pd.DataFrame(columns=["Prod Line AS", "Overall Result"])

    try:
        preview = pd.read_excel(report_path, sheet_name="Monthly", header=None)
    except Exception:
        logging.exception("Failed to read Monthly sheet from %s", report_path)
        return pd.DataFrame(columns=["Prod Line AS", "Overall Result"])

    header_row = None
    max_scan = min(300, len(preview))
    for row_idx in range(max_scan):
        row_values = [str(v).strip().lower() for v in preview.iloc[row_idx].tolist()]
        if "overall result" in row_values:
            header_row = row_idx
            break

    if header_row is None:
        logging.warning("Failed to detect header row in Monthly sheet for %s", report_path)
        return pd.DataFrame(columns=["Prod Line AS", "Overall Result"])

    try:
        raw = pd.read_excel(report_path, sheet_name="Monthly", header=header_row)
    except Exception:
        logging.exception("Failed to re-read Monthly sheet with detected header from %s", report_path)
        return pd.DataFrame(columns=["Prod Line AS", "Overall Result"])

    if raw.empty:
        return pd.DataFrame(columns=["Prod Line AS", "Overall Result"])

    columns = list(raw.columns)
    normalized_map = {str(col).strip().lower(): col for col in columns}
    category_col = normalized_map.get("prod line as") or normalized_map.get("prod line")
    if not category_col:
        for col in columns:
            col_name = str(col).strip().lower()
            if "prod line" in col_name:
                category_col = col
                break
    if not category_col:
        logging.warning("Monthly sheet missing product line column in %s", report_path)
        return pd.DataFrame(columns=["Prod Line AS", "Overall Result"])

    overall_col = None
    for col in columns:
        if str(col).strip().lower() == "overall result":
            overall_col = col
            break

    # Monthly numeric/date columns start from column M (0-based index 12).
    start_idx = 12 if len(columns) >= 13 else 0
    overall_idx = columns.index(overall_col) if overall_col in columns else len(columns)
    date_candidates = columns[start_idx:overall_idx]

    if not date_candidates and len(columns) > 12:
        date_candidates = columns[12:overall_idx]

    date_columns: list[Any] = []
    for col in date_candidates:
        ts = pd.to_datetime(str(col), errors="coerce")
        if pd.notna(ts):
            date_columns.append(col)

    if not date_columns:
        logging.warning("No date columns detected in Monthly sheet for %s", report_path)
        return pd.DataFrame(columns=["Prod Line AS", "Overall Result"])

    current_period = pd.Timestamp.today().to_period("M")
    quarter_start_month = ((current_period.month - 1) // 3) * 3 + 1
    quarter_start = pd.Period(f"{current_period.year}-{quarter_start_month:02d}", freq="M")
    quarter_end_next = quarter_start + 5
    target_periods = [quarter_start + i for i in range(6)]
    target_month_labels = [p.strftime("%Y-%m") for p in target_periods]

    quarter_window_columns: list[Any] = []
    for col in date_columns:
        ts = pd.to_datetime(str(col), errors="coerce")
        if pd.isna(ts):
            continue
        period = ts.to_period("M")
        if quarter_start <= period <= quarter_end_next:
            quarter_window_columns.append(col)

    if not quarter_window_columns:
        logging.warning(
            "No monthly columns within quarter window (%s to %s) for %s",
            str(quarter_start),
            str(quarter_end_next),
            report_path,
        )
    date_columns = quarter_window_columns

    grouped = pd.DataFrame(
        0.0,
        index=pd.Index(["Base", "Promotion"], name="Prod Line AS"),
        columns=target_month_labels,
    )
    if date_columns:
        working = raw[[category_col] + date_columns].copy()
        working["Prod Line AS"] = working[category_col].apply(normalize_hc_idp_prod_line_bucket)
        working = working[working["Prod Line AS"].notna()].copy()

        if not working.empty:
            for col in date_columns:
                # Source is SU; convert to MSU for dashboard display.
                working[col] = pd.to_numeric(working[col], errors="coerce").fillna(0) / 1000.0

            monthly_grouped = (
                working.groupby("Prod Line AS", dropna=False)[date_columns]
                .sum(min_count=1)
                .reindex(["Base", "Promotion"], fill_value=0)
            )

            renamed_cols: list[str] = []
            for col in monthly_grouped.columns:
                ts = pd.to_datetime(str(col), errors="coerce")
                renamed_cols.append(ts.strftime("%Y-%m") if pd.notna(ts) else str(col))
            monthly_grouped.columns = renamed_cols
            monthly_grouped = monthly_grouped.T.groupby(level=0).sum().T
            monthly_grouped = monthly_grouped.reindex(columns=target_month_labels, fill_value=0)
            grouped.loc[:, target_month_labels] = monthly_grouped.reindex(
                index=["Base", "Promotion"], fill_value=0
            )

    weekly_current = summarize_hc_idp_weekly_current_month(report_path)
    if not weekly_current.empty:
        weekly_by_bucket = weekly_current.set_index("Prod Line AS")
        current_month_label = current_period.strftime("%Y-%m")
        if current_month_label in grouped.columns and current_month_label in weekly_by_bucket.columns:
            grouped[current_month_label] = (
                weekly_by_bucket[current_month_label]
                .reindex(["Base", "Promotion"], fill_value=0)
                .astype(float)
            )

    grouped = grouped.reindex(["Base", "Promotion"], fill_value=0).fillna(0)
    grouped = grouped.reindex(columns=target_month_labels, fill_value=0)

    if grouped.empty:
        return pd.DataFrame(columns=["Prod Line AS", "Overall Result"])

    grouped["Overall Result"] = grouped.sum(axis=1)
    total_row = grouped.sum(axis=0)
    grouped.loc["Total"] = total_row
    result = grouped.reset_index()
    result = result.rename(columns={"index": "Prod Line AS"})
    return result


def build_level1_unmapped_report(df: pd.DataFrame, mapping: pd.DataFrame, cfg: PipelineConfig) -> pd.DataFrame:
    """List material numbers that failed to map to First level, ranked by MSU impact."""
    level_col = cfg.level1_first_level_column
    if df.empty or "MSU" not in df.columns or "availability_month" not in df.columns or "Material Number" not in df.columns:
        return pd.DataFrame(columns=["material_key", "Material Number", "unmapped_msu", "row_count"])

    selected_cols = ["availability_month", "Material Number", "MSU"]
    if "Item Text" in df.columns:
        selected_cols.append("Item Text")
    working = df[selected_cols].copy()
    working["material_key"] = working["Material Number"].apply(normalize_material_key)
    working["MSU"] = pd.to_numeric(working["MSU"], errors="coerce")

    if mapping is not None and not mapping.empty and "material_key" in mapping.columns and level_col in mapping.columns:
        working = working.merge(mapping, on="material_key", how="left")
    else:
        working[level_col] = None

    raw_level = working[level_col].fillna("").astype(str).str.strip()
    raw_level = raw_level.mask(raw_level.str.lower().isin({"nan", "none"}), "")
    missing_material = working["material_key"].fillna("").astype(str).str.strip().eq("")
    item_text = working["Item Text"].fillna("").astype(str).str.strip().str.lower() if "Item Text" in working.columns else ""
    is_rm_material = item_text.eq("rm material") if isinstance(item_text, pd.Series) else pd.Series(False, index=working.index)
    unmapped = (~missing_material) & (raw_level == "") & (~is_rm_material)

    if not unmapped.any():
        return pd.DataFrame(columns=["material_key", "Material Number", "unmapped_msu", "row_count"])

    report = (
        working.loc[unmapped]
        .groupby(["material_key", "Material Number"], dropna=False)["MSU"]
        .agg(unmapped_msu="sum", row_count="count")
        .reset_index()
        .sort_values(["unmapped_msu", "row_count"], ascending=[False, False])
    )
    report["unmapped_msu"] = report["unmapped_msu"].round(4)
    return report


def load_role_lookup(path: Path) -> Dict[str, str]:
    if not path.exists():
        logging.warning("Role mapping file %s not found; defaulting to '%s'", path, UNKNOWN_ROLE)
        return {}
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    lookup: Dict[str, str] = {}
    for entry in raw:
        email = entry.get("email", "").strip().lower()
        role = entry.get("role", UNKNOWN_ROLE).strip()
        if email:
            lookup[email] = role or UNKNOWN_ROLE
    logging.info("Loaded %s requester role entries", len(lookup))
    return lookup


def normalize_requester_email_value(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip().lower()
    if not text:
        return ""
    # Fix common domain typos like @pg,com while keeping separators intact.
    text = re.sub(r"@pg\s*,\s*com\b", "@pg.com", text)
    text = re.sub(r"@pg\s*,\s*cn\b", "@pg.cn", text)
    text = re.sub(r",\s*com\b", ".com", text)
    text = re.sub(r",\s*cn\b", ".cn", text)
    return text


def infer_role(value: Optional[str], lookup: Optional[Dict[str, str]]) -> str:
    if not value:
        return UNKNOWN_ROLE
    if not lookup:
        return UNKNOWN_ROLE
    normalized = normalize_requester_email_value(value)
    tokens = re.split(r"[;,]", normalized)
    for token in tokens:
        email = token.strip().lower()
        if not email:
            continue
        if email in lookup:
            return lookup[email]
    return UNKNOWN_ROLE


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def load_config(path: Optional[Path]) -> PipelineConfig:
    env_override = os.getenv("MATRES_CONFIG")
    if env_override:
        config_path = Path(env_override)
    elif path:
        config_path = path
    else:
        config_path = DEFAULT_CONFIG_PATH

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    logging.info("Loaded config from %s", config_path)
    return PipelineConfig.from_dict(raw)


def read_workbook(cfg: PipelineConfig) -> pd.DataFrame:
    logging.info("Reading sheet '%s' from %s", cfg.sheet_name, cfg.workbook_path)
    df = pd.read_excel(cfg.workbook_path, sheet_name=cfg.sheet_name)
    logging.info("Loaded %s rows", len(df))
    return df


def standardize_column_names(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    rename_map: Dict[str, str] = {}
    for col in df.columns:
        cleaned = re.sub(r"\s+", " ", str(col)).strip()
        rename_map[col] = cleaned
    return df.rename(columns=rename_map)


def infer_material_number_column(df: pd.DataFrame) -> Optional[str]:
    if df.empty:
        return None

    if "Material Number" in df.columns:
        return "Material Number"

    exclude = {
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
        "snapshot_extracted",
        "availability_month",
        "requester_role",
    }

    best_col: Optional[str] = None
    best_score = 0.0
    for col in df.columns:
        if col in exclude:
            continue
        series = df[col]
        numeric = pd.to_numeric(series, errors="coerce")
        score = float(numeric.notna().mean())
        if score > best_score:
            best_score = score
            best_col = col

    if best_col and best_score >= 0.6:
        return best_col

    first = df.columns[0]
    if first not in exclude:
        numeric = pd.to_numeric(df[first], errors="coerce")
        if float(numeric.notna().mean()) >= 0.6:
            return first

    return None


def clean_dataframe(df: pd.DataFrame, cfg: PipelineConfig) -> pd.DataFrame:
    df = standardize_column_names(df)
    material_col = infer_material_number_column(df)
    if material_col and material_col != "Material Number":
        logging.info("Renaming material number column '%s' -> 'Material Number'", material_col)
        df = df.rename(columns={material_col: "Material Number"})

    df = df.copy()
    for column in DATE_COLUMNS:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], errors="coerce")

    for column in NUMERIC_COLUMNS:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    if "Requester Email" in df.columns:
        df["Requester Email"] = df["Requester Email"].apply(normalize_requester_email_value)
        df["requester_role"] = df["Requester Email"].apply(lambda val: infer_role(val, cfg.role_lookup))
    else:
        df["requester_role"] = UNKNOWN_ROLE

    if "Availability Date" in df.columns:
        df["availability_month"] = df["Availability Date"].dt.to_period("M").astype(str)
        df.loc[df["availability_month"].isna(), "availability_month"] = "unknown"
    else:
        logging.warning("Availability Date column missing; tagging month as unknown")
        df["availability_month"] = "unknown"

    if "DeleteDate" in df.columns:
        before = len(df)
        df = df[df["DeleteDate"].isna()].copy()
        removed = before - len(df)
        if removed:
            logging.info("Filtered %s deleted rows via DeleteDate", removed)

    df["snapshot_extracted"] = datetime.now(timezone.utc)
    return df


def write_processed_csv(df: pd.DataFrame, path: Path, name: str) -> None:
    output_path = path / name
    df.to_csv(output_path, index=False)
    logging.info("Wrote %s rows to %s", len(df), output_path)


def summarize_monthly_by_item(df: pd.DataFrame) -> pd.DataFrame:
    if "MSU" not in df.columns:
        raise ValueError("MSU column missing from dataset")
    agg = (
        df.groupby(["availability_month", "Item Text"], dropna=False)["MSU"]
        .sum(min_count=1)
        .reset_index()
        .rename(columns={"MSU": "total_msu"})
    )
    return agg.sort_values(["availability_month", "total_msu"], ascending=[True, False])


def summarize_monthly_by_requester_item(df: pd.DataFrame) -> pd.DataFrame:
    required = {"MSU", "Requester Email", "Item Text"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns for requester summary: {missing}")
    group_cols = ["availability_month", "Requester Email", "Item Text"]
    agg = (
        df.groupby(group_cols, dropna=False)
        .agg(
            total_msu=("MSU", "sum"),
            requester_role=("requester_role", "first"),
        )
        .reset_index()
    )
    return agg.sort_values(["availability_month", "total_msu"], ascending=[True, False])


def summarize_pde_alerts(df: pd.DataFrame) -> pd.DataFrame:
    if "PDE Checking" not in df.columns:
        raise ValueError("PDE Checking column missing from dataset")
    pde_df = df[df["PDE Checking"].notna()].copy()
    if pde_df.empty:
        return pd.DataFrame(
            columns=[
                "Requester Email",
                "availability_date",
                "availability_month",
                "msu_due",
                "open_items",
                "max_pde",
                "avg_pde",
                "closest_availability",
                "project_label",
                "requester_role",
            ]
        )

    if "MRP Element Indicator" not in pde_df.columns:
        pde_df["MRP Element Indicator"] = None

    def combine_project(values: pd.Series) -> str:
        cleaned = sorted({str(v).strip() for v in values if pd.notna(v) and str(v).strip()})
        return " / ".join(cleaned) if cleaned else "未定义"

    pde_df["availability_date"] = pde_df["Availability Date"].dt.date

    summary = (
        pde_df.groupby(["Requester Email", "availability_date"], dropna=False)
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

    return summary.sort_values(["closest_availability", "max_pde"], ascending=[True, False])


def prepare_request_details(df: pd.DataFrame) -> pd.DataFrame:
    available_columns = [col for col in DETAIL_COLUMNS if col in df.columns]
    if not available_columns:
        return pd.DataFrame()

    meta_columns = [col for col in ["availability_month", "requester_role"] if col in df.columns]
    details = df[available_columns + meta_columns].copy()

    sort_fields = [col for col in ["Availability Date", "Request Date", "Material Number"] if col in details.columns]
    if sort_fields:
        details = details.sort_values(by=sort_fields, ascending=[True] * len(sort_fields), na_position="last")

    for column in DATE_COLUMNS:
        if column in details.columns:
            details[column] = details[column].dt.strftime("%Y-%m-%d")

    return details


def append_history_snapshot(df: pd.DataFrame, cfg: PipelineConfig) -> None:
    if not cfg.refresh_opts or not cfg.refresh_opts.get("append_history", True):
        logging.info("History append disabled; skipping history write")
        return

    history_file = cfg.history_path
    history_file.parent.mkdir(parents=True, exist_ok=True)

    tz = cfg.time_zone
    try:
        tzinfo = ZoneInfo(tz)
    except Exception:  # fallback when timezone missing
        logging.warning("Invalid time zone '%s'; defaulting to UTC", tz)
        tzinfo = ZoneInfo("UTC")

    snapshot_ts = datetime.now(tzinfo)
    history_df = df.copy()
    history_df["snapshot_ts"] = snapshot_ts.isoformat()
    history_df["snapshot_date"] = snapshot_ts.date().isoformat()

    history_keys: Iterable[str] = cfg.refresh_opts.get("history_keys", [])
    for key in history_keys:
        if key not in history_df.columns:
            logging.warning("History key '%s' not found; it will be skipped", key)

    if history_file.exists():
        existing = pd.read_csv(history_file)
        combined = pd.concat([existing, history_df], ignore_index=True)
    else:
        combined = history_df

    dedupe_keys = ["snapshot_date"] + [k for k in history_keys if k in history_df.columns]
    if dedupe_keys:
        combined = combined.drop_duplicates(subset=dedupe_keys, keep="last")

    combined.to_csv(history_file, index=False)
    logging.info("History snapshot updated (%s rows)", len(combined))


def run_pipeline(cfg: PipelineConfig) -> None:
    cfg.processed_dir.mkdir(parents=True, exist_ok=True)
    df_raw = read_workbook(cfg)
    df_clean = clean_dataframe(df_raw, cfg)

    append_history_snapshot(df_clean, cfg)

    monthly_item = summarize_monthly_by_item(df_clean)
    write_processed_csv(monthly_item, cfg.processed_dir, PROCESSED_FILES["monthly_item"])

    monthly_requester = summarize_monthly_by_requester_item(df_clean)
    write_processed_csv(
        monthly_requester,
        cfg.processed_dir,
        PROCESSED_FILES["monthly_requester"],
    )

    mapping = read_level1_mapping(cfg)
    monthly_level1 = summarize_monthly_by_first_level(df_clean, mapping, cfg)
    write_processed_csv(monthly_level1, cfg.processed_dir, PROCESSED_FILES["monthly_level1"])

    unmapped_report = build_level1_unmapped_report(df_clean, mapping, cfg)
    write_processed_csv(unmapped_report, cfg.processed_dir, PROCESSED_FILES["level1_unmapped"])

    pde_alerts = summarize_pde_alerts(df_clean)
    write_processed_csv(pde_alerts, cfg.processed_dir, PROCESSED_FILES["pde_alerts"])

    request_details = prepare_request_details(df_clean)
    write_processed_csv(request_details, cfg.processed_dir, PROCESSED_FILES["request_details"])

    hc_idp_monthly = summarize_hc_idp_monthly(Path.cwd())
    write_processed_csv(hc_idp_monthly, cfg.processed_dir, PROCESSED_FILES["hc_idp_monthly"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MatRes pipeline runner")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to pipeline config JSON",
    )
    return parser.parse_args()


def main() -> None:
    configure_logging()
    args = parse_args()
    cfg = load_config(args.config)
    run_pipeline(cfg)


if __name__ == "__main__":
    main()
