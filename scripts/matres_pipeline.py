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
    "td_validation_monthly_compare": "td_version_monthly_comparison.csv",
    "td_validation_gap_detail": "td_version_gap_details.csv",
    "production_data": "production_data_summary.csv",
    "production_data_by_level": "production_data_summary_by_level.csv",
}
PRODUCTION_VOL_ALLOWED_MRP_ELEMENTS = {"2.1plannedorders", "2.2processorders"}
PRODUCTION_VOL_OTHER_EXCLUSION_REASON = (
    "Exclude 'Other' because it contains QM quantities already included in MTD; "
    "keeping Other would double count production data."
)
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
    data_base_dir: Path
    production_data_dir: Path
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

        data_base_dir = Path(raw.get("data_base_dir", "."))
        if not data_base_dir.is_absolute():
            data_base_dir = root / data_base_dir

        production_data_dir = Path(raw.get("production_data_dir", str(data_base_dir / "Production Volume")))
        if not production_data_dir.is_absolute():
            production_data_dir = root / production_data_dir

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
            data_base_dir=data_base_dir,
            production_data_dir=production_data_dir,
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


def read_sufactor_mapping(cfg: PipelineConfig) -> pd.DataFrame:
    workbook = cfg.workbook_path
    if workbook is None or not workbook.exists():
        return pd.DataFrame(columns=["material_key", "numer", "denom"])

    try:
        raw = pd.read_excel(workbook, sheet_name="Sufactor")
    except Exception:
        logging.exception("Failed to read Sufactor sheet from %s", workbook)
        return pd.DataFrame(columns=["material_key", "numer", "denom"])

    if raw.empty:
        return pd.DataFrame(columns=["material_key", "numer", "denom"])

    normalized_map = {str(col).strip().lower(): col for col in raw.columns}

    def pick_column(candidates: list[str], contains: list[str] | None = None) -> Optional[str]:
        for name in candidates:
            if name in normalized_map:
                return normalized_map[name]
        if contains:
            for col in raw.columns:
                key = str(col).strip().lower()
                if all(token in key for token in contains):
                    return col
        return None

    material_col = pick_column(["material"], ["material"])
    numer_col = pick_column(["numer.", "numer"], ["numer"])
    denom_col = pick_column(["denom.", "denom"], ["denom"])
    required_cols = [material_col, numer_col, denom_col]
    if any(col is None for col in required_cols):
        logging.warning("Sufactor sheet missing required columns (Material/Numer./Denom.)")
        return pd.DataFrame(columns=["material_key", "numer", "denom"])

    mapping = raw[[material_col, numer_col, denom_col]].copy()
    mapping.columns = ["Material", "Numer", "Denom"]
    mapping["material_key"] = mapping["Material"].apply(normalize_material_key)
    mapping["numer"] = pd.to_numeric(mapping["Numer"], errors="coerce")
    mapping["denom"] = pd.to_numeric(mapping["Denom"], errors="coerce")

    mapping = mapping[mapping["material_key"].astype(bool)].copy()
    mapping = mapping[mapping["numer"].notna() & mapping["denom"].notna()].copy()
    mapping = mapping[mapping["numer"] != 0].copy()
    mapping = mapping.drop_duplicates(subset=["material_key"], keep="first")
    return mapping[["material_key", "numer", "denom"]]


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


def read_second_level_mapping(cfg: PipelineConfig) -> pd.DataFrame:
    def normalize_level2_label(value: object) -> str:
        text = str(value).strip() if value is not None else ""
        key = text.lower()
        if key in {"o-hot", "o hot"}:
            return "O-Hot"
        if key in {"o-non hot", "o non hot", "o-nonhot", "o nonhot"}:
            return "O-Non Hot"
        return text

    workbook = cfg.level1_workbook_path
    if workbook is None or not workbook.exists():
        return pd.DataFrame(columns=["material_key", "Level2"])

    try:
        mapping = pd.read_excel(workbook, sheet_name=cfg.level1_sheet_name)
    except Exception:
        logging.exception("Failed to read second level mapping from %s", workbook)
        return pd.DataFrame(columns=["material_key", "Level2"])

    material_col = cfg.level1_material_column
    if material_col not in mapping.columns:
        return pd.DataFrame(columns=["material_key", "Level2"])

    second_level_col = None
    normalized_map = {str(col).strip().lower(): col for col in mapping.columns}
    if "second level" in normalized_map:
        second_level_col = normalized_map["second level"]
    else:
        for col in mapping.columns:
            col_name = str(col).strip().lower()
            if "second" in col_name and "level" in col_name:
                second_level_col = col
                break

    if second_level_col is None:
        return pd.DataFrame(columns=["material_key", "Level2"])

    result = mapping[[material_col, second_level_col]].copy()
    result["material_key"] = result[material_col].apply(normalize_material_key)
    result["Level2"] = result[second_level_col].fillna("").apply(normalize_level2_label)
    result = result[result["material_key"].astype(bool)].copy()
    result = result.drop_duplicates(subset=["material_key"], keep="first")
    return result[["material_key", "Level2"]]


def read_xqtc_9su_mapping(root: Path) -> pd.DataFrame:
    candidates = [
        p for p in root.glob("Parameter*.xls*")
        if p.is_file() and not p.name.startswith("~$")
    ]
    if not candidates:
        return pd.DataFrame(columns=["material_key", "su9", "is_bottle_line"])

    parameter_path = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        raw = pd.read_excel(parameter_path)
    except Exception:
        logging.exception("Failed to read Parameter mapping from %s", parameter_path)
        return pd.DataFrame(columns=["material_key", "su9", "is_bottle_line"])

    if raw.empty:
        return pd.DataFrame(columns=["material_key", "su9", "is_bottle_line"])

    normalized_map = {str(col).strip().lower(): col for col in raw.columns}

    code_col = None
    for key in ["code", "material", "material code", "materialcode"]:
        if key in normalized_map:
            code_col = normalized_map[key]
            break
    if code_col is None:
        for col in raw.columns:
            key = str(col).strip().lower()
            if "code" in key or "material" in key:
                code_col = col
                break

    su9_col = None
    for col in raw.columns:
        key = str(col).strip().lower()
        if "9" in key and "su" in key:
            su9_col = col
            break

    technology_col = None
    for key in ["technology", "tech", "packing type", "packingtype", "type"]:
        if key in normalized_map:
            technology_col = normalized_map[key]
            break
    if technology_col is None:
        for col in raw.columns:
            key = str(col).strip().lower()
            if "technology" in key or "tech" in key or "packing" in key or key == "type":
                technology_col = col
                break

    if code_col is None or su9_col is None or technology_col is None:
        logging.warning("Parameter file missing Code/9字头SU/Technology columns: %s", parameter_path)
        return pd.DataFrame(columns=["material_key", "su9", "is_bottle_line"])

    mapping = raw[[code_col, su9_col, technology_col]].copy()
    mapping.columns = ["Code", "SU9", "Technology"]
    mapping["material_key"] = mapping["Code"].apply(normalize_material_key)
    mapping["su9"] = pd.to_numeric(mapping["SU9"], errors="coerce")
    tech_normalized = mapping["Technology"].fillna("").astype(str).str.strip().str.lower()
    mapping["is_bottle_line"] = tech_normalized.str.replace("-", " ", regex=False).str.replace("_", " ", regex=False).str.replace("  ", " ", regex=False).eq("bottle line")
    mapping = mapping[mapping["material_key"].astype(bool)].copy()

    grouped = (
        mapping.groupby("material_key", dropna=False)
        .agg(
            su9=("su9", lambda s: pd.to_numeric(s, errors="coerce").dropna().iloc[0] if pd.to_numeric(s, errors="coerce").dropna().size > 0 else pd.NA),
            is_bottle_line=("is_bottle_line", "max"),
        )
        .reset_index()
    )
    grouped["is_bottle_line"] = grouped["is_bottle_line"].fillna(False).astype(bool)
    return grouped[["material_key", "su9", "is_bottle_line"]]


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
    reports = list_hc_idp_reports_sorted(root)
    if not reports:
        return None
    return reports[-1][1]


def extract_hc_idp_report_date(path: Path) -> Optional[str]:
    name = path.stem
    match = re.search(r"\((\d{8})-[^)]+\)", name)
    if match:
        return match.group(1)
    fallback = re.search(r"(\d{8})", name)
    if fallback:
        return fallback.group(1)
    return None


def infer_hc_idp_report_period(path: Path) -> pd.Period:
    version_date = extract_hc_idp_report_date(path)
    if isinstance(version_date, str) and re.fullmatch(r"\d{8}", version_date):
        try:
            ts = pd.to_datetime(version_date, format="%Y%m%d", errors="coerce")
            if pd.notna(ts):
                return ts.to_period("M")
        except Exception:
            pass
    return pd.Timestamp.today().to_period("M")


def list_hc_idp_reports_sorted(root: Path) -> list[tuple[str, Path]]:
    patterns = [
        "HC IDP HANA TD Report*.xlsx",
        "HC IDP HANA TD Report*.xlsm",
        "HC IDP HANA TD Report*.xls",
    ]
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(root.glob(pattern))

    unique_candidates: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_candidates.append(candidate)

    enriched: list[tuple[str, float, Path]] = []
    for candidate in unique_candidates:
        try:
            mtime = candidate.stat().st_mtime
        except Exception:
            mtime = 0.0
        version_date = extract_hc_idp_report_date(candidate)
        if not version_date:
            version_date = datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y%m%d")
        enriched.append((version_date, mtime, candidate))

    enriched.sort(key=lambda item: (item[0], item[1]))
    return [(version_date, path) for version_date, _, path in enriched]


def extract_date_from_filename(path: Path) -> Optional[str]:
    match = re.search(r"(\d{8})", path.stem)
    if match:
        return match.group(1)
    return None


def list_production_reports_sorted(root: Path) -> list[tuple[str, Path]]:
    patterns = [
        "Detailed Production Scheduling Report*.xls*",
        "Detiald Production Scheduling Report*.xls*",
    ]
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend([p for p in root.glob(pattern) if not p.name.startswith("~$")])

    unique_candidates: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_candidates.append(candidate)

    enriched: list[tuple[str, float, Path]] = []
    for candidate in unique_candidates:
        try:
            mtime = candidate.stat().st_mtime
        except Exception:
            mtime = 0.0
        version_date = extract_date_from_filename(candidate)
        if not version_date:
            version_date = datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y%m%d")
        enriched.append((version_date, mtime, candidate))

    enriched.sort(key=lambda item: (item[0], item[1]))
    return [(version_date, path) for version_date, _, path in enriched]


def find_latest_production_report(root: Path) -> Optional[Path]:
    reports = list_production_reports_sorted(root)
    if not reports:
        return None
    return reports[-1][1]


def infer_report_date_from_filename(path: Path) -> pd.Timestamp:
    text = extract_date_from_filename(path)
    if text and re.fullmatch(r"\d{8}", text):
        ts = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
        if pd.notna(ts):
            return pd.Timestamp(ts).normalize()
    return pd.Timestamp.today().normalize()


def read_production_schedule_report(report_path: Path) -> pd.DataFrame:
    def looks_like_production_table(df: pd.DataFrame) -> bool:
        if df is None or df.empty:
            return False
        cols = [str(col).strip().lower() for col in df.columns]
        has_plant = any(col == "plant" or "plant" in col for col in cols)
        has_material = any(col == "material" or "material" in col for col in cols)
        has_startdate = any(("startdate" in col) or ("start" in col and "date" in col) for col in cols)
        return has_plant and has_material and has_startdate

    def drop_empty_unnamed_columns(df: pd.DataFrame) -> pd.DataFrame:
        cleaned = df.copy()
        unnamed_cols = [
            col for col in cleaned.columns
            if str(col).strip().lower().startswith("unnamed")
        ]
        for col in unnamed_cols:
            series = cleaned[col]
            if series.isna().all() or series.fillna("").astype(str).str.strip().eq("").all():
                cleaned = cleaned.drop(columns=[col])
        return cleaned

    try:
        df_excel = pd.read_excel(report_path)
        if looks_like_production_table(df_excel):
            return df_excel
    except Exception:
        pass

    for encoding in ["utf-16", "utf-8-sig", "latin1"]:
        try:
            df_csv = pd.read_csv(report_path, sep="\t", dtype=str, engine="python", encoding=encoding)
            df_csv = drop_empty_unnamed_columns(df_csv)
            if looks_like_production_table(df_csv):
                return df_csv
        except Exception:
            continue

    for encoding in ["utf-16", "utf-8-sig", "latin1"]:
        try:
            with report_path.open("r", encoding=encoding, errors="ignore") as handle:
                lines = handle.readlines()
        except Exception:
            continue

        header_idx: Optional[int] = None
        for idx, line in enumerate(lines[:300]):
            text = line.strip().lower()
            if "\t" in line and "plant" in text and "material" in text and "startdate" in text:
                header_idx = idx
                break

        if header_idx is None:
            continue

        try:
            df_scanned = pd.read_csv(
                report_path,
                sep="\t",
                dtype=str,
                engine="python",
                encoding=encoding,
                skiprows=header_idx,
            )
            df_scanned = drop_empty_unnamed_columns(df_scanned)
            if looks_like_production_table(df_scanned):
                return df_scanned
        except Exception:
            continue

    raise ValueError(f"Failed to parse production report: {report_path}")


def read_production_volume_report(report_path: Path) -> pd.DataFrame:
    def parse_tab_export(path: Path) -> pd.DataFrame:
        for encoding in ["utf-16", "utf-8-sig", "latin1"]:
            try:
                lines = path.read_text(encoding=encoding, errors="ignore").splitlines()
            except Exception:
                continue

            header_idx: Optional[int] = None
            for idx, line in enumerate(lines[:300]):
                text = line.strip().lower()
                if "\t" in line and "categories" in text and "plant" in text and "material" in text:
                    header_idx = idx
                    break

            if header_idx is None:
                continue

            try:
                df = pd.read_csv(
                    path,
                    sep="\t",
                    dtype=str,
                    engine="python",
                    encoding=encoding,
                    skiprows=header_idx,
                )
            except Exception:
                continue

            df = df.dropna(axis=1, how="all")
            df = df[[col for col in df.columns if not str(col).strip().lower().startswith("unnamed")]]
            if not df.empty:
                return df

        return pd.DataFrame()

    tab_df = parse_tab_export(report_path)
    if not tab_df.empty:
        return standardize_column_names(tab_df)

    try:
        excel_df = pd.read_excel(report_path)
        return standardize_column_names(excel_df)
    except Exception as exc:
        raise ValueError(f"Failed to parse production volume report: {report_path}") from exc


def build_production_data_summary(root: Path, cfg: PipelineConfig) -> pd.DataFrame:
    base_columns = ["Plant", "Level1", "Level2", "MTD", "Left Production", "Current Month Total"]

    def parse_numeric(series: pd.Series) -> pd.Series:
        cleaned = (
            series.fillna("")
            .astype(str)
            .str.replace("\u00a0", "", regex=False)
            .str.replace(",", "", regex=False)
            .str.replace(" ", "", regex=False)
            .str.strip()
        )
        return pd.to_numeric(cleaned, errors="coerce").fillna(0.0)

    def sort_month_values(values: Iterable[str]) -> list[str]:
        def key_func(value: str):
            parsed = pd.Period(str(value), freq="M")
            return parsed.start_time

        return sorted({str(v) for v in values if str(v).strip()}, key=key_func)

    def normalize_month_label(raw_label: str) -> Optional[str]:
        text = str(raw_label).strip()
        dot_match = re.fullmatch(r"(\d{2})\.(\d{4})", text)
        if dot_match:
            month, year = dot_match.groups()
            return f"{year}-{month}"

        dash_match = re.fullmatch(r"(\d{4})-(\d{2})", text)
        if dash_match:
            return text

        return None

    def pick_column(df: pd.DataFrame, candidates: list[str], contains: list[str] | None = None) -> Optional[str]:
        normalized_map = {str(col).strip().lower(): col for col in df.columns}
        for name in candidates:
            if name in normalized_map:
                return normalized_map[name]
        if contains:
            for col in df.columns:
                key = str(col).strip().lower()
                if all(token in key for token in contains):
                    return col
        return None

    all_reports = [
        p for p in root.glob("*.xls*")
        if p.is_file() and not p.name.startswith("~$")
    ]
    mtd_reports = sorted([
        p for p in all_reports
        if "mtd" in p.name.lower() and "production vol" not in p.name.lower()
    ])
    production_vol_reports = sorted([
        p for p in all_reports
        if "production vol" in p.name.lower()
    ])

    if not mtd_reports:
        logging.warning("No MTD report found under %s", root)
    if not production_vol_reports:
        logging.warning("No Production Vol report found under %s", root)

    if not mtd_reports and not production_vol_reports:
        return pd.DataFrame(columns=base_columns)

    mtd_monthly_frames: list[pd.DataFrame] = []
    for report_path in mtd_reports:
        try:
            raw = read_production_schedule_report(report_path)
        except Exception:
            logging.exception("Failed to read MTD report from %s", report_path)
            continue

        if raw.empty:
            continue

        raw = standardize_column_names(raw)
        start_date_col = pick_column(raw, ["startdate", "start date"], ["start", "date"])
        plant_col = pick_column(raw, ["plant"], ["plant"])
        deliv_col = pick_column(raw, ["deliv. quantity", "delivery quantity"], ["deliv", "quantity"])
        required_cols = [start_date_col, plant_col, deliv_col]
        if any(col is None for col in required_cols):
            logging.warning("MTD report missing required columns in %s", report_path)
            continue

        working = raw[[start_date_col, plant_col, deliv_col]].copy()
        working.columns = ["StartDate", "Plant", "Deliv. Quantity"]

        working["StartDateParsed"] = pd.to_datetime(working["StartDate"], errors="coerce")
        working = working[working["StartDateParsed"].notna()].copy()
        if working.empty:
            continue

        deliv_qty = parse_numeric(working["Deliv. Quantity"])
        working["mtd_qty"] = deliv_qty / 1000.0
        working["Plant"] = working["Plant"].fillna("").astype(str).str.strip()
        working = working[working["Plant"] != ""].copy()
        if working.empty:
            continue

        working["Month"] = working["StartDateParsed"].dt.to_period("M").astype(str)

        monthly = (
            working.groupby(["Plant", "Month"], dropna=False)["mtd_qty"]
            .sum(min_count=1)
            .reset_index()
            .rename(columns={"mtd_qty": "MTD_VALUE"})
        )
        mtd_monthly_frames.append(monthly)

    if mtd_monthly_frames:
        mtd_monthly = (
            pd.concat(mtd_monthly_frames, ignore_index=True)
            .groupby(["Plant", "Month"], dropna=False)["MTD_VALUE"]
            .sum(min_count=1)
            .reset_index()
        )
    else:
        mtd_monthly = pd.DataFrame(columns=["Plant", "Month", "MTD_VALUE"])

    production_vol_frames: list[pd.DataFrame] = []
    month_labels: set[str] = set()
    xqtc_9su_mapping = read_xqtc_9su_mapping(root)

    for report_path in production_vol_reports:
        try:
            raw = read_production_volume_report(report_path)
        except Exception:
            logging.exception("Failed to read Production Vol report from %s", report_path)
            continue

        if raw.empty:
            continue

        category_col = pick_column(raw, ["categories / members"], ["categories"])
        plant_col = pick_column(raw, ["plant"], ["plant"])
        material_col = pick_column(raw, ["material"], ["material"])
        mrp_elements_col = pick_column(raw, ["mrp elements", "mrp element"], ["mrp", "element"])
        prev_perd_col = pick_column(raw, ["prev.perd", "prev perd"], ["prev", "perd"])
        d_filter_col = raw.columns[2] if len(raw.columns) > 2 else None
        if category_col is None or plant_col is None or material_col is None or mrp_elements_col is None or prev_perd_col is None:
            logging.warning("Production Vol report missing required columns (Category/Plant/Material/MRP Elements): %s", report_path)
            continue

        month_col_map: Dict[str, str] = {}
        passed_prev_perd = False
        for col in raw.columns:
            if col == prev_perd_col:
                passed_prev_perd = True
                continue
            if not passed_prev_perd:
                continue
            normalized = normalize_month_label(str(col).strip())
            if normalized:
                month_col_map[col] = normalized

        if not month_col_map:
            logging.warning("Production Vol report has no monthly columns: %s", report_path)
            continue

        select_cols = [category_col, plant_col, material_col, mrp_elements_col, *month_col_map.keys()]
        if d_filter_col is not None and d_filter_col not in select_cols:
            select_cols.append(d_filter_col)
        working = raw[select_cols].copy()
        rename_base = {
            category_col: "Category",
            plant_col: "Plant",
            material_col: "Material",
            mrp_elements_col: "MRP Elements",
        }
        if d_filter_col is not None and d_filter_col in working.columns and d_filter_col != plant_col:
            rename_base[d_filter_col] = "_D_FILTER"
        working = working.rename(columns=rename_base)

        working["Category"] = working["Category"].fillna("").astype(str).str.strip()
        working["Plant"] = working["Plant"].fillna("").astype(str).str.strip()
        working["Material"] = working["Material"].fillna("").astype(str).str.strip()
        working["MRP Elements"] = working["MRP Elements"].fillna("").astype(str).str.strip()
        if "_D_FILTER" in working.columns:
            working["_D_FILTER"] = working["_D_FILTER"].fillna("").astype(str).str.strip()

        before_mrp_rows = len(working)
        working = working[
            working["Category"].str.replace(" ", "", regex=False).str.lower().eq("2.0production/receipts")
            & working["MRP Elements"].str.replace(" ", "", regex=False).str.lower().isin(PRODUCTION_VOL_ALLOWED_MRP_ELEMENTS)
            & working["Plant"].ne("")
            & working["Material"].ne("")
            & (working["_D_FILTER"].ne("") if "_D_FILTER" in working.columns else True)
        ].copy()
        removed_by_mrp = before_mrp_rows - len(working)
        if removed_by_mrp > 0:
            logging.info(
                "Production Vol file %s removed %s rows by MRP Elements filter (%s). %s",
                report_path,
                removed_by_mrp,
                sorted(PRODUCTION_VOL_ALLOWED_MRP_ELEMENTS),
                PRODUCTION_VOL_OTHER_EXCLUSION_REASON,
            )
        if working.empty:
            continue

        rename_map = {source_col: month_label for source_col, month_label in month_col_map.items()}
        working = working.rename(columns=rename_map)

        normalized_month_cols = sorted({label for label in rename_map.values()})
        report_name_lower = report_path.name.lower()
        is_xqtc_report = "xqtc" in report_name_lower
        is_xqtc_wip = is_xqtc_report and "wip" in report_name_lower

        if is_xqtc_report:
            working["material_key"] = working["Material"].apply(normalize_material_key)
            working = working[working["material_key"].astype(bool)].copy()
            if working.empty:
                continue

            working = working.merge(xqtc_9su_mapping, on="material_key", how="left")
            bottle_mask = working.get("is_bottle_line", False)
            if not isinstance(bottle_mask, pd.Series):
                bottle_mask = pd.Series(False, index=working.index)
            removed_non_bottle = int((~bottle_mask.fillna(False)).sum())
            working = working[bottle_mask.fillna(False)].copy()
            if removed_non_bottle > 0:
                logging.info(
                    "XQTC file %s removed %s non-Bottle-Line rows by Parameter Technology",
                    report_path,
                    removed_non_bottle,
                )
            if working.empty:
                continue

        if is_xqtc_wip:
            missing_su9 = working.get("su9", pd.Series(dtype=float)).isna().sum()
            if missing_su9 > 0:
                logging.warning(
                    "XQTC WIP file %s has %s materials without 9字头SU mapping; treated as 0",
                    report_path,
                    int(missing_su9),
                )

        for month_col in normalized_month_cols:
            month_values = parse_numeric(working[month_col])
            if is_xqtc_wip:
                su9_values = pd.to_numeric(working.get("su9", 0.0), errors="coerce").fillna(0.0)
                working[month_col] = month_values * su9_values / 1000.0
            elif is_xqtc_report:
                working[month_col] = month_values / 1000.0
            else:
                working[month_col] = month_values
            month_labels.add(month_col)

        grouped = (
            working.groupby("Plant", dropna=False)[normalized_month_cols]
            .sum(min_count=1)
            .reset_index()
        )
        production_vol_frames.append(grouped)

    if production_vol_frames:
        production_vol = (
            pd.concat(production_vol_frames, ignore_index=True)
            .groupby("Plant", dropna=False)
            .sum(min_count=1)
            .reset_index()
        )
    else:
        production_vol = pd.DataFrame(columns=["Plant"])

    sorted_months = sort_month_values(month_labels)
    if not sorted_months:
        return pd.DataFrame(columns=base_columns)

    month_window = sorted_months[:6]
    current_month = month_window[0]
    future_months = month_window[1:]

    if current_month not in production_vol.columns:
        production_vol[current_month] = 0.0

    mtd_current = mtd_monthly[mtd_monthly["Month"].astype(str) == current_month].copy()
    if not mtd_current.empty:
        mtd_current = (
            mtd_current.groupby("Plant", dropna=False)["MTD_VALUE"]
            .sum(min_count=1)
            .reset_index()
        )
    else:
        mtd_current = pd.DataFrame(columns=["Plant", "MTD_VALUE"])

    result = production_vol.merge(mtd_current, on="Plant", how="outer")
    result["Plant"] = result["Plant"].fillna("").astype(str).str.strip()
    result = result[result["Plant"] != ""].copy()
    if result.empty:
        return pd.DataFrame(columns=base_columns)

    result["MTD_VALUE"] = pd.to_numeric(result.get("MTD_VALUE", 0.0), errors="coerce").fillna(0.0)
    result["Left Production_VALUE"] = pd.to_numeric(result.get(current_month, 0.0), errors="coerce").fillna(0.0)
    result["Current Month Total_VALUE"] = result["MTD_VALUE"] + result["Left Production_VALUE"]

    for month in month_window:
        result[month] = pd.to_numeric(result.get(month, 0.0), errors="coerce").fillna(0.0)

    result["MTD"] = result["MTD_VALUE"]
    result["Left Production"] = result["Left Production_VALUE"]
    result["Current Month Total"] = result["Current Month Total_VALUE"]
    result[current_month] = result["Left Production"]
    result["Level1"] = "ALL"
    result["Level2"] = "ALL"

    ordered_cols = [
        "Plant",
        "Level1",
        "Level2",
        "MTD",
        "Left Production",
        "Current Month Total",
        *month_window,
        "MTD_VALUE",
        "Left Production_VALUE",
        "Current Month Total_VALUE",
    ]

    result = result[ordered_cols].copy()
    result = result.sort_values(["Plant", "Level1", "Level2"], ascending=[True, True, True]).reset_index(drop=True)
    return result


def build_production_data_summary_by_level(root: Path, cfg: PipelineConfig) -> pd.DataFrame:
    base_columns = ["Plant", "Level1", "Level2", "MTD", "Left Production", "Current Month Total"]

    def parse_numeric(series: pd.Series) -> pd.Series:
        cleaned = (
            series.fillna("")
            .astype(str)
            .str.replace("\u00a0", "", regex=False)
            .str.replace(",", "", regex=False)
            .str.replace(" ", "", regex=False)
            .str.strip()
        )
        return pd.to_numeric(cleaned, errors="coerce").fillna(0.0)

    def sort_month_values(values: Iterable[str]) -> list[str]:
        def key_func(value: str):
            parsed = pd.Period(str(value), freq="M")
            return parsed.start_time

        return sorted({str(v) for v in values if str(v).strip()}, key=key_func)

    def normalize_month_label(raw_label: str) -> Optional[str]:
        text = str(raw_label).strip()
        dot_match = re.fullmatch(r"(\d{2})\.(\d{4})", text)
        if dot_match:
            month, year = dot_match.groups()
            return f"{year}-{month}"

        dash_match = re.fullmatch(r"(\d{4})-(\d{2})", text)
        if dash_match:
            return text

        return None

    def pick_column(df: pd.DataFrame, candidates: list[str], contains: list[str] | None = None) -> Optional[str]:
        normalized_map = {str(col).strip().lower(): col for col in df.columns}
        for name in candidates:
            if name in normalized_map:
                return normalized_map[name]
        if contains:
            for col in df.columns:
                key = str(col).strip().lower()
                if all(token in key for token in contains):
                    return col
        return None

    all_reports = [
        p for p in root.glob("*.xls*")
        if p.is_file() and not p.name.startswith("~$")
    ]
    mtd_reports = sorted([
        p for p in all_reports
        if "mtd" in p.name.lower() and "production vol" not in p.name.lower()
    ])
    production_vol_reports = sorted([
        p for p in all_reports
        if "production vol" in p.name.lower()
    ])

    if not production_vol_reports:
        logging.warning("No Production Vol report found under %s", root)
        return pd.DataFrame(columns=base_columns)

    level1_mapping = read_level1_mapping(cfg)
    level1_col = cfg.level1_first_level_column
    if level1_col not in level1_mapping.columns:
        level1_mapping = pd.DataFrame(columns=["material_key", "Level1"])
    else:
        level1_mapping = level1_mapping.rename(columns={level1_col: "Level1"})
    level2_mapping = read_second_level_mapping(cfg)

    mtd_monthly_frames: list[pd.DataFrame] = []
    for report_path in mtd_reports:
        try:
            raw = read_production_schedule_report(report_path)
        except Exception:
            logging.exception("Failed to read MTD report from %s", report_path)
            continue

        if raw.empty:
            continue

        raw = standardize_column_names(raw)
        start_date_col = pick_column(raw, ["startdate", "start date"], ["start", "date"])
        plant_col = pick_column(raw, ["plant"], ["plant"])
        material_col = pick_column(raw, ["material", "material number"], ["material"])
        deliv_col = pick_column(raw, ["deliv. quantity", "delivery quantity"], ["deliv", "quantity"])
        required_cols = [start_date_col, plant_col, material_col, deliv_col]
        if any(col is None for col in required_cols):
            logging.warning("MTD report missing required columns in %s", report_path)
            continue

        working = raw[[start_date_col, plant_col, material_col, deliv_col]].copy()
        working.columns = ["StartDate", "Plant", "Material", "Deliv. Quantity"]

        working["StartDateParsed"] = pd.to_datetime(working["StartDate"], errors="coerce")
        working = working[working["StartDateParsed"].notna()].copy()
        if working.empty:
            continue

        working["Plant"] = working["Plant"].fillna("").astype(str).str.strip()
        working["Material"] = working["Material"].fillna("").astype(str).str.strip()
        working = working[working["Plant"] != ""].copy()
        working = working[working["Material"] != ""].copy()
        if working.empty:
            continue

        deliv_qty = parse_numeric(working["Deliv. Quantity"])
        working["MTD_VALUE"] = deliv_qty / 1000.0
        working["Month"] = working["StartDateParsed"].dt.to_period("M").astype(str)

        working["material_key"] = working["Material"].apply(normalize_material_key)
        working = working[working["material_key"].astype(bool)].copy()
        if working.empty:
            continue

        working = working.merge(level1_mapping, on="material_key", how="left")
        working = working.merge(level2_mapping, on="material_key", how="left")
        working["Level1"] = working.get("Level1", "").fillna("").astype(str).str.strip()
        working["Level2"] = working.get("Level2", "").fillna("").astype(str).str.strip()
        working.loc[working["Level1"] == "", "Level1"] = "未映射"
        working.loc[working["Level2"] == "", "Level2"] = "未映射"

        monthly = (
            working.groupby(["Plant", "Level1", "Level2", "Month"], dropna=False)["MTD_VALUE"]
            .sum(min_count=1)
            .reset_index()
        )
        mtd_monthly_frames.append(monthly)

    if mtd_monthly_frames:
        mtd_monthly = (
            pd.concat(mtd_monthly_frames, ignore_index=True)
            .groupby(["Plant", "Level1", "Level2", "Month"], dropna=False)["MTD_VALUE"]
            .sum(min_count=1)
            .reset_index()
        )
    else:
        mtd_monthly = pd.DataFrame(columns=["Plant", "Level1", "Level2", "Month", "MTD_VALUE"])

    detail_frames: list[pd.DataFrame] = []
    month_labels: set[str] = set()
    xqtc_9su_mapping = read_xqtc_9su_mapping(root)

    for report_path in production_vol_reports:
        try:
            raw = read_production_volume_report(report_path)
        except Exception:
            logging.exception("Failed to read Production Vol report from %s", report_path)
            continue

        if raw.empty:
            continue

        category_col = pick_column(raw, ["categories / members"], ["categories"])
        plant_col = pick_column(raw, ["plant"], ["plant"])
        material_col = pick_column(raw, ["material"], ["material"])
        mrp_elements_col = pick_column(raw, ["mrp elements", "mrp element"], ["mrp", "element"])
        prev_perd_col = pick_column(raw, ["prev.perd", "prev perd"], ["prev", "perd"])
        d_filter_col = raw.columns[2] if len(raw.columns) > 2 else None
        if any(col is None for col in [category_col, plant_col, material_col, mrp_elements_col, prev_perd_col]):
            logging.warning("Production Vol detail report missing required columns (Category/Plant/Material/MRP Elements) in %s", report_path)
            continue

        month_col_map: Dict[str, str] = {}
        passed_prev_perd = False
        for col in raw.columns:
            if col == prev_perd_col:
                passed_prev_perd = True
                continue
            if not passed_prev_perd:
                continue
            normalized = normalize_month_label(str(col).strip())
            if normalized:
                month_col_map[col] = normalized

        if not month_col_map:
            continue

        select_cols = [category_col, plant_col, material_col, mrp_elements_col, *month_col_map.keys()]
        if d_filter_col is not None and d_filter_col not in select_cols:
            select_cols.append(d_filter_col)

        working = raw[select_cols].copy()
        rename_base = {
            category_col: "Category",
            plant_col: "Plant",
            material_col: "Material",
            mrp_elements_col: "MRP Elements",
        }
        if d_filter_col is not None and d_filter_col in working.columns and d_filter_col != plant_col:
            rename_base[d_filter_col] = "_D_FILTER"
        working = working.rename(columns=rename_base)

        working["Category"] = working["Category"].fillna("").astype(str).str.strip()
        working["Plant"] = working["Plant"].fillna("").astype(str).str.strip()
        working["Material"] = working["Material"].fillna("").astype(str).str.strip()
        working["MRP Elements"] = working["MRP Elements"].fillna("").astype(str).str.strip()
        if "_D_FILTER" in working.columns:
            working["_D_FILTER"] = working["_D_FILTER"].fillna("").astype(str).str.strip()

        before_mrp_rows = len(working)
        working = working[
            working["Category"].str.replace(" ", "", regex=False).str.lower().eq("2.0production/receipts")
            & working["MRP Elements"].str.replace(" ", "", regex=False).str.lower().isin(PRODUCTION_VOL_ALLOWED_MRP_ELEMENTS)
            & working["Plant"].ne("")
            & working["Material"].ne("")
            & (working["_D_FILTER"].ne("") if "_D_FILTER" in working.columns else True)
        ].copy()
        removed_by_mrp = before_mrp_rows - len(working)
        if removed_by_mrp > 0:
            logging.info(
                "Production Vol detail file %s removed %s rows by MRP Elements filter (%s). %s",
                report_path,
                removed_by_mrp,
                sorted(PRODUCTION_VOL_ALLOWED_MRP_ELEMENTS),
                PRODUCTION_VOL_OTHER_EXCLUSION_REASON,
            )
        if working.empty:
            continue

        working["material_key"] = working["Material"].apply(normalize_material_key)
        working = working[working["material_key"].astype(bool)].copy()
        if working.empty:
            continue

        rename_map = {source_col: month_label for source_col, month_label in month_col_map.items()}
        working = working.rename(columns=rename_map)

        normalized_month_cols = sorted({label for label in rename_map.values()})
        report_name_lower = report_path.name.lower()
        is_xqtc_report = "xqtc" in report_name_lower
        is_xqtc_wip = is_xqtc_report and "wip" in report_name_lower

        if is_xqtc_report:
            working = working.merge(xqtc_9su_mapping, on="material_key", how="left")
            bottle_mask = working.get("is_bottle_line", False)
            if not isinstance(bottle_mask, pd.Series):
                bottle_mask = pd.Series(False, index=working.index)
            removed_non_bottle = int((~bottle_mask.fillna(False)).sum())
            working = working[bottle_mask.fillna(False)].copy()
            if removed_non_bottle > 0:
                logging.info(
                    "XQTC file %s removed %s non-Bottle-Line rows by Parameter Technology",
                    report_path,
                    removed_non_bottle,
                )
            if working.empty:
                continue

        if is_xqtc_wip:
            missing_su9 = working.get("su9", pd.Series(dtype=float)).isna().sum()
            if missing_su9 > 0:
                logging.warning(
                    "XQTC WIP file %s has %s materials without 9字头SU mapping; treated as 0",
                    report_path,
                    int(missing_su9),
                )

        for month_col in normalized_month_cols:
            month_values = parse_numeric(working[month_col])
            if is_xqtc_wip:
                su9_values = pd.to_numeric(working.get("su9", 0.0), errors="coerce").fillna(0.0)
                working[month_col] = month_values * su9_values / 1000.0
            elif is_xqtc_report:
                working[month_col] = month_values / 1000.0
            else:
                working[month_col] = month_values
            month_labels.add(month_col)

        working = working.merge(level1_mapping, on="material_key", how="left")
        working = working.merge(level2_mapping, on="material_key", how="left")
        working["Level1"] = working.get("Level1", "").fillna("").astype(str).str.strip()
        working["Level2"] = working.get("Level2", "").fillna("").astype(str).str.strip()
        working.loc[working["Level1"] == "", "Level1"] = "未映射"
        working.loc[working["Level2"] == "", "Level2"] = "未映射"

        grouped = (
            working.groupby(["Plant", "Level1", "Level2"], dropna=False)[normalized_month_cols]
            .sum(min_count=1)
            .reset_index()
        )
        detail_frames.append(grouped)

    if not detail_frames:
        return pd.DataFrame(columns=base_columns)

    result = (
        pd.concat(detail_frames, ignore_index=True)
        .groupby(["Plant", "Level1", "Level2"], dropna=False)
        .sum(min_count=1)
        .reset_index()
    )

    sorted_months = sort_month_values(month_labels)
    month_window = sorted_months[:6]
    if not month_window:
        return pd.DataFrame(columns=base_columns)

    current_month = month_window[0]

    for month in month_window:
        result[month] = pd.to_numeric(result.get(month, 0.0), errors="coerce").fillna(0.0)

    mtd_current = mtd_monthly[mtd_monthly["Month"].astype(str) == current_month].copy()
    if not mtd_current.empty:
        mtd_current = (
            mtd_current.groupby(["Plant", "Level1", "Level2"], dropna=False)["MTD_VALUE"]
            .sum(min_count=1)
            .reset_index()
        )
    else:
        mtd_current = pd.DataFrame(columns=["Plant", "Level1", "Level2", "MTD_VALUE"])

    result = result.merge(mtd_current, on=["Plant", "Level1", "Level2"], how="outer")
    result["Plant"] = result["Plant"].fillna("").astype(str).str.strip()
    result["Level1"] = result["Level1"].fillna("").astype(str).str.strip()
    result["Level2"] = result["Level2"].fillna("").astype(str).str.strip()
    result = result[result["Plant"] != ""].copy()
    result.loc[result["Level1"] == "", "Level1"] = "未映射"
    result.loc[result["Level2"] == "", "Level2"] = "未映射"
    result["MTD_VALUE"] = pd.to_numeric(result.get("MTD_VALUE", 0.0), errors="coerce").fillna(0.0)
    result["Left Production_VALUE"] = pd.to_numeric(result.get(current_month, 0.0), errors="coerce").fillna(0.0)
    result["Current Month Total_VALUE"] = result["MTD_VALUE"] + result["Left Production_VALUE"]

    result["MTD"] = result["MTD_VALUE"]
    result["Left Production"] = result["Left Production_VALUE"]
    result["Current Month Total"] = result["Current Month Total_VALUE"]
    result[current_month] = result["Left Production"]

    ordered_cols = [
        "Plant",
        "Level1",
        "Level2",
        "MTD",
        "Left Production",
        "Current Month Total",
        *month_window,
        "MTD_VALUE",
        "Left Production_VALUE",
        "Current Month Total_VALUE",
    ]
    result = result[ordered_cols].copy()
    total_check_cols = ["MTD", "Left Production", "Current Month Total", *month_window]
    result = result[result[total_check_cols].abs().sum(axis=1) > 0].copy()
    result = result.sort_values(["Plant", "Level1", "Level2"], ascending=[True, True, True]).reset_index(drop=True)
    return result


def normalize_hc_idp_prod_line_bucket(value: object) -> Optional[str]:
    text = str(value).strip().lower()
    if "base" in text:
        return "Base"
    if "promotion" in text or "pp" in text:
        return "Promotion"
    return None


def detect_weekly_tp_layout(raw: pd.DataFrame) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    if raw is None or raw.empty:
        return None, None, None, None

    max_scan_rows = min(80, len(raw))
    header_row: Optional[int] = None
    lbe_col_idx: Optional[int] = None
    prod_line_col_idx: Optional[int] = None
    apo_col_idx: Optional[int] = None

    for row_idx in range(max_scan_rows):
        row_values = raw.iloc[row_idx].fillna("").astype(str).str.strip()
        row_lower = row_values.str.lower()

        lbe_hits = [int(i) for i, text in enumerate(row_lower.tolist()) if text == "lbe"]
        prod_hits = [int(i) for i, text in enumerate(row_lower.tolist()) if text == "prod line"]

        if lbe_hits and prod_hits:
            header_row = row_idx
            lbe_col_idx = lbe_hits[0]
            prod_line_col_idx = prod_hits[0]

            apo_hits = [int(i) for i, text in enumerate(row_lower.tolist()) if text == "apo product"]
            if apo_hits:
                apo_col_idx = apo_hits[0]
            break

    if header_row is None:
        return None, None, None, None

    if apo_col_idx is None and raw.shape[1] > 7:
        apo_col_idx = 7

    return header_row, lbe_col_idx, prod_line_col_idx, apo_col_idx


def summarize_hc_idp_weekly_current_month(report_path: Path) -> pd.DataFrame:
    try:
        raw = pd.read_excel(report_path, sheet_name="Weekly(TP)", header=None)
    except Exception:
        logging.exception("Failed to read Weekly(TP) sheet from %s", report_path)
        return pd.DataFrame(columns=["Prod Line AS"])

    header_row, lbe_col_idx, prod_line_col_idx, _ = detect_weekly_tp_layout(raw)
    if header_row is None or lbe_col_idx is None or prod_line_col_idx is None:
        logging.warning("Failed to detect Weekly(TP) header layout in %s", report_path)
        return pd.DataFrame(columns=["Prod Line AS"])

    data_start_row = header_row + 1
    if raw.shape[1] <= max(lbe_col_idx, prod_line_col_idx) or raw.shape[0] <= data_start_row:
        logging.warning("Weekly(TP) sheet layout is smaller than expected in %s", report_path)
        return pd.DataFrame(columns=["Prod Line AS"])

    current_month_label = infer_hc_idp_report_period(report_path).strftime("%Y-%m")
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


def summarize_hc_idp_weekly_current_month_detail(report_path: Path, current_month_label: str) -> pd.DataFrame:
    try:
        raw = pd.read_excel(report_path, sheet_name="Weekly(TP)", header=None)
    except Exception:
        logging.exception("Failed to read Weekly(TP) sheet from %s", report_path)
        return pd.DataFrame(columns=["Prod Line", "APO Product", "Month", "Value"])

    header_row, lbe_col_idx, prod_line_col_idx, apo_col_idx = detect_weekly_tp_layout(raw)
    if header_row is None or lbe_col_idx is None or prod_line_col_idx is None:
        logging.warning("Failed to detect Weekly(TP) header layout in %s", report_path)
        return pd.DataFrame(columns=["Prod Line", "APO Product", "Month", "Value"])

    if apo_col_idx is None:
        apo_col_idx = 7

    data_start_row = header_row + 1
    if raw.shape[1] <= max(lbe_col_idx, prod_line_col_idx, apo_col_idx) or raw.shape[0] <= data_start_row:
        logging.warning("Weekly(TP) sheet layout is smaller than expected in %s", report_path)
        return pd.DataFrame(columns=["Prod Line", "APO Product", "Month", "Value"])

    working = raw.iloc[data_start_row:, [prod_line_col_idx, apo_col_idx, lbe_col_idx]].copy()
    working.columns = ["prod_line", "apo_product", "lbe"]
    working["Prod Line"] = working["prod_line"].apply(normalize_hc_idp_prod_line_bucket)
    working = working[working["Prod Line"].notna()].copy()
    if working.empty:
        return pd.DataFrame(columns=["Prod Line", "APO Product", "Month", "Value"])

    working["Prod Line"] = working["Prod Line"].replace({"Promotion": "PP"})
    working["APO Product"] = working["apo_product"].apply(normalize_material_key)
    working["lbe"] = pd.to_numeric(working["lbe"], errors="coerce").fillna(0.0) / 1000.0

    grouped = (
        working.groupby(["Prod Line", "APO Product"], dropna=False)["lbe"]
        .sum(min_count=1)
        .reset_index()
        .rename(columns={"lbe": "Value"})
    )
    grouped["Month"] = current_month_label
    return grouped[["Prod Line", "APO Product", "Month", "Value"]]


def summarize_hc_idp_monthly_from_report(report_path: Path, apply_past_shipment_filter: bool = False) -> pd.DataFrame:
    if report_path is None:
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
    report_period = infer_hc_idp_report_period(report_path)
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
        # Column L in source file is used for Shipment filtering on past months.
        shipment_col = columns[11] if len(columns) > 11 else None

        selected_cols: list[Any] = [category_col] + date_columns
        if shipment_col is not None and shipment_col not in selected_cols:
            selected_cols.append(shipment_col)

        working = raw[selected_cols].copy()
        working["Prod Line AS"] = working[category_col].apply(normalize_hc_idp_prod_line_bucket)
        working = working[working["Prod Line AS"].notna()].copy()

        if not working.empty:
            shipment_mask = pd.Series(True, index=working.index)
            if shipment_col is not None and shipment_col in working.columns:
                shipment_mask = (
                    working[shipment_col]
                    .fillna("")
                    .astype(str)
                    .str.strip()
                    .str.lower()
                    .isin({"shipment", "shipments"})
                )

            for col in date_columns:
                ts = pd.to_datetime(str(col), errors="coerce")
                if pd.isna(ts):
                    continue
                month_label = ts.strftime("%Y-%m")
                if month_label not in grouped.columns:
                    continue

                month_rows = working
                if apply_past_shipment_filter and ts.to_period("M") < report_period:
                    month_rows = working[shipment_mask].copy()

                if month_rows.empty:
                    continue

                # Source is SU; convert to MSU for dashboard display.
                month_values = pd.to_numeric(month_rows[col], errors="coerce").fillna(0.0) / 1000.0
                month_grouped = (
                    month_rows.assign(_month_value=month_values)
                    .groupby("Prod Line AS", dropna=False)["_month_value"]
                    .sum(min_count=1)
                    .reindex(["Base", "Promotion"], fill_value=0.0)
                    .fillna(0.0)
                )
                grouped[month_label] = grouped[month_label] + month_grouped

    weekly_current = summarize_hc_idp_weekly_current_month(report_path)
    if not weekly_current.empty:
        weekly_by_bucket = weekly_current.set_index("Prod Line AS")
        current_month_label = report_period.strftime("%Y-%m")
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


def summarize_hc_idp_monthly(root: Path) -> pd.DataFrame:
    report_path = find_latest_hc_idp_report(root)
    if report_path is None:
        logging.warning("No HC IDP HANA TD Report file found under %s", root)
        return pd.DataFrame(columns=["Prod Line AS", "Overall Result"])
    return summarize_hc_idp_monthly_from_report(report_path, apply_past_shipment_filter=True)


def build_td_validation_monthly_comparison(root: Path) -> pd.DataFrame:
    reports = list_hc_idp_reports_sorted(root)
    if len(reports) < 2:
        return pd.DataFrame(columns=["Version", "Prod Line", "Total"])

    previous_date, previous_path = reports[-2]
    current_date, current_path = reports[-1]

    current_df = summarize_hc_idp_monthly_from_report(current_path, apply_past_shipment_filter=True)
    previous_df = summarize_hc_idp_monthly_from_report(previous_path, apply_past_shipment_filter=True)
    if current_df.empty or previous_df.empty:
        return pd.DataFrame(columns=["Version", "Prod Line", "Total"])

    current_period = pd.Timestamp.today().to_period("M")
    quarter_start_month = ((current_period.month - 1) // 3) * 3 + 1
    quarter_start = pd.Period(f"{current_period.year}-{quarter_start_month:02d}", freq="M")
    target_months = [(quarter_start + i).strftime("%Y-%m") for i in range(6)]

    def to_full_date(version_date: str) -> str:
        if isinstance(version_date, str) and re.fullmatch(r"\d{8}", version_date):
            return f"{version_date[:4]}-{version_date[4:6]}-{version_date[6:8]}"
        return str(version_date)

    def normalize_version_frame(df: pd.DataFrame) -> pd.DataFrame:
        if "Prod Line AS" not in df.columns:
            return pd.DataFrame(0.0, index=["Base", "PP", "Total"], columns=target_months)

        working = df.copy()
        for month in target_months:
            if month not in working.columns:
                working[month] = 0.0

        for month in target_months:
            working[month] = pd.to_numeric(working[month], errors="coerce").fillna(0.0)

        mapping = {
            "base": "Base",
            "promotion": "PP",
            "total": "Total",
        }
        working["Prod Line"] = working["Prod Line AS"].astype(str).str.strip().str.lower().map(mapping)
        working = working[working["Prod Line"].notna()].copy()
        if working.empty:
            return pd.DataFrame(0.0, index=["Base", "PP", "Total"], columns=target_months)

        normalized = (
            working.groupby("Prod Line", dropna=False)[target_months]
            .sum(min_count=1)
            .reindex(["Base", "PP", "Total"], fill_value=0.0)
            .fillna(0.0)
        )
        return normalized

    current_norm = normalize_version_frame(current_df)
    previous_norm = normalize_version_frame(previous_df)
    gap_norm = current_norm - previous_norm

    version_rows = [
        (f"Current ({to_full_date(current_date)})", "Current", current_norm),
        (f"Previous ({to_full_date(previous_date)})", "Previous", previous_norm),
        ("Gap", "Gap", gap_norm),
    ]

    records: list[dict[str, Any]] = []
    for idx_version, (version_label, version_group, frame) in enumerate(version_rows):
        for idx, prod_line in enumerate(["Base", "PP", "Total"]):
            row: dict[str, Any] = {
                "Version": version_label if idx == 0 else "",
                "Version Group": version_group,
                "Prod Line": prod_line,
            }
            total_value = 0
            for month in target_months:
                value = float(frame.loc[prod_line, month]) if month in frame.columns else 0.0
                int_value = int(round(value))
                total_value += int_value
                row[month] = int_value
            row["Total"] = total_value
            records.append(row)

        if idx_version < len(version_rows) - 1:
            spacer: dict[str, Any] = {"Version": "", "Version Group": "", "Prod Line": ""}
            for month in target_months:
                spacer[month] = ""
            spacer["Total"] = ""
            records.append(spacer)

    return pd.DataFrame(records)


def summarize_hc_idp_monthly_detail_from_report(
    report_path: Path,
    target_months: list[str],
    apply_past_shipment_filter: bool = False,
) -> pd.DataFrame:
    if report_path is None:
        return pd.DataFrame(columns=["Prod Line", "APO Product", "Des", "Month", "Value"])

    try:
        preview = pd.read_excel(report_path, sheet_name="Monthly", header=None)
    except Exception:
        logging.exception("Failed to read Monthly sheet from %s", report_path)
        return pd.DataFrame(columns=["Prod Line", "APO Product", "Des", "Month", "Value"])

    header_row = None
    max_scan = min(300, len(preview))
    for row_idx in range(max_scan):
        row_values = [str(v).strip().lower() for v in preview.iloc[row_idx].tolist()]
        if "overall result" in row_values:
            header_row = row_idx
            break

    if header_row is None:
        logging.warning("Failed to detect header row in Monthly sheet for %s", report_path)
        return pd.DataFrame(columns=["Prod Line", "APO Product", "Des", "Month", "Value"])

    try:
        raw = pd.read_excel(report_path, sheet_name="Monthly", header=header_row)
    except Exception:
        logging.exception("Failed to re-read Monthly sheet with detected header from %s", report_path)
        return pd.DataFrame(columns=["Prod Line", "APO Product", "Des", "Month", "Value"])

    if raw.empty:
        return pd.DataFrame(columns=["Prod Line", "APO Product", "Des", "Month", "Value"])

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
        return pd.DataFrame(columns=["Prod Line", "APO Product", "Des", "Month", "Value"])

    apo_col = None
    for key in ["apo product", "apo_product", "apo product code", "apo"]:
        if key in normalized_map:
            apo_col = normalized_map[key]
            break
    if not apo_col:
        for col in columns:
            col_name = str(col).strip().lower()
            if "apo" in col_name and "product" in col_name:
                apo_col = col
                break

    des_col = None
    for key in ["des", "description", "material description"]:
        if key in normalized_map:
            des_col = normalized_map[key]
            break
    if not des_col:
        for col in columns:
            col_name = str(col).strip().lower()
            if col_name == "des" or "description" in col_name:
                des_col = col
                break

    if (apo_col is None or raw[apo_col].dropna().empty) and len(columns) > 7:
        apo_col = columns[7]
    if (des_col is None or raw[des_col].dropna().empty) and len(columns) > 8:
        des_col = columns[8]

    overall_col = None
    for col in columns:
        if str(col).strip().lower() == "overall result":
            overall_col = col
            break

    start_idx = 12 if len(columns) >= 13 else 0
    overall_idx = columns.index(overall_col) if overall_col in columns else len(columns)
    date_candidates = columns[start_idx:overall_idx]

    target_set = set(target_months)
    month_columns: list[Any] = []
    month_label_map: dict[Any, str] = {}
    for col in date_candidates:
        ts = pd.to_datetime(str(col), errors="coerce")
        if pd.isna(ts):
            continue
        month_label = ts.strftime("%Y-%m")
        if month_label in target_set:
            month_columns.append(col)
            month_label_map[col] = month_label

    if not month_columns:
        return pd.DataFrame(columns=["Prod Line", "APO Product", "Des", "Month", "Value"])

    shipment_col = columns[11] if len(columns) > 11 else None

    selected_cols: list[Any] = [category_col] + month_columns
    if apo_col is not None:
        selected_cols.append(apo_col)
    if des_col is not None and des_col not in selected_cols:
        selected_cols.append(des_col)
    if shipment_col is not None and shipment_col not in selected_cols:
        selected_cols.append(shipment_col)

    working = raw[selected_cols].copy()
    working["Prod Line"] = working[category_col].apply(normalize_hc_idp_prod_line_bucket)
    working = working[working["Prod Line"].notna()].copy()
    if working.empty:
        return pd.DataFrame(columns=["Prod Line", "APO Product", "Des", "Month", "Value"])

    working["Prod Line"] = working["Prod Line"].replace({"Promotion": "PP"})
    working["APO Product"] = (
        working[apo_col].apply(normalize_material_key) if apo_col is not None else ""
    )
    working["Des"] = (
        working[des_col].astype(str).str.strip() if des_col is not None else ""
    )

    shipment_mask = pd.Series(True, index=working.index)
    if shipment_col is not None and shipment_col in working.columns:
        shipment_mask = (
            working[shipment_col]
            .fillna("")
            .astype(str)
            .str.strip()
            .str.lower()
            .isin({"shipment", "shipments"})
        )

    detail_frames: list[pd.DataFrame] = []
    current_period = pd.Timestamp.today().to_period("M")
    report_period = infer_hc_idp_report_period(report_path)
    for col in month_columns:
        month_label = month_label_map.get(col)
        if not month_label:
            continue

        month_rows = working
        month_period = pd.Period(month_label, freq="M")
        if apply_past_shipment_filter and month_period < report_period:
            month_rows = working[shipment_mask].copy()

        if month_rows.empty:
            continue

        month_values = pd.to_numeric(month_rows[col], errors="coerce").fillna(0.0) / 1000.0
        month_frame = month_rows[["Prod Line", "APO Product", "Des"]].copy()
        month_frame["Month"] = month_label
        month_frame["Value"] = month_values
        detail_frames.append(month_frame)

    if detail_frames:
        melted = pd.concat(detail_frames, ignore_index=True, sort=False)
    else:
        melted = pd.DataFrame(columns=["Prod Line", "APO Product", "Des", "Month", "Value"])

    grouped = (
        melted.groupby(["Prod Line", "APO Product", "Des", "Month"], dropna=False)["Value"]
        .sum(min_count=1)
        .reset_index()
    )

    current_month_label = report_period.strftime("%Y-%m")
    if current_month_label in target_set:
        weekly_detail = summarize_hc_idp_weekly_current_month_detail(report_path, current_month_label)
        if not weekly_detail.empty:
            monthly_lookup = grouped.copy()
            monthly_lookup["apo_norm"] = monthly_lookup["APO Product"].apply(normalize_material_key)
            monthly_lookup["Des"] = monthly_lookup["Des"].fillna("").astype(str).str.strip()
            monthly_lookup = monthly_lookup[monthly_lookup["Des"] != ""].copy()
            if not monthly_lookup.empty:
                monthly_lookup["abs_value"] = monthly_lookup["Value"].abs()
                monthly_lookup = monthly_lookup.sort_values(["abs_value"], ascending=[False])
                des_lookup = monthly_lookup.drop_duplicates(subset=["Prod Line", "apo_norm"], keep="first")[["Prod Line", "apo_norm", "Des"]]
            else:
                des_lookup = pd.DataFrame(columns=["Prod Line", "apo_norm", "Des"])

            weekly_detail["apo_norm"] = weekly_detail["APO Product"].apply(normalize_material_key)
            if not des_lookup.empty:
                weekly_detail = weekly_detail.merge(des_lookup, on=["Prod Line", "apo_norm"], how="left")
            else:
                weekly_detail["Des"] = ""
            weekly_detail["Des"] = weekly_detail["Des"].fillna("")
            weekly_detail = weekly_detail.drop(columns=["apo_norm"])

            grouped = grouped[grouped["Month"] != current_month_label].copy()
            grouped = pd.concat(
                [grouped, weekly_detail[["Prod Line", "APO Product", "Des", "Month", "Value"]]],
                ignore_index=True,
                sort=False,
            )
            grouped = (
                grouped.groupby(["Prod Line", "APO Product", "Des", "Month"], dropna=False)["Value"]
                .sum(min_count=1)
                .reset_index()
            )
    return grouped


def build_td_validation_gap_details(root: Path, cfg: PipelineConfig) -> pd.DataFrame:
    reports = list_hc_idp_reports_sorted(root)
    if len(reports) < 2:
        return pd.DataFrame(
            columns=[
                "Month",
                "Prod Line",
                "APO Product",
                "Des",
                "Current",
                "Previous",
                "Gap",
                "Current Version",
                "Previous Version",
            ]
        )

    previous_date, previous_path = reports[-2]
    current_date, current_path = reports[-1]

    current_period = pd.Timestamp.today().to_period("M")
    quarter_start_month = ((current_period.month - 1) // 3) * 3 + 1
    quarter_start = pd.Period(f"{current_period.year}-{quarter_start_month:02d}", freq="M")
    target_months = [(quarter_start + i).strftime("%Y-%m") for i in range(6)]

    current_detail = summarize_hc_idp_monthly_detail_from_report(
        current_path,
        target_months,
        apply_past_shipment_filter=True,
    )
    previous_detail = summarize_hc_idp_monthly_detail_from_report(
        previous_path,
        target_months,
        apply_past_shipment_filter=True,
    )

    for frame in [current_detail, previous_detail]:
        if frame.empty:
            continue
        if "APO Product" in frame.columns:
            frame["APO Product"] = frame["APO Product"].apply(normalize_material_key)
        if "Des" in frame.columns:
            frame["Des"] = frame["Des"].fillna("").astype(str).str.strip()
        if "Month" in frame.columns:
            frame["Month"] = frame["Month"].fillna("").astype(str).str.strip()
        if "Prod Line" in frame.columns:
            frame["Prod Line"] = frame["Prod Line"].fillna("").astype(str).str.strip()
    if current_detail.empty and previous_detail.empty:
        return pd.DataFrame(
            columns=[
                "Month",
                "Prod Line",
                "APO Product",
                "Des",
                "Current",
                "Previous",
                "Gap",
                "Current Version",
                "Previous Version",
            ]
        )

    merged = current_detail.merge(
        previous_detail,
        on=["Prod Line", "APO Product", "Des", "Month"],
        how="outer",
        suffixes=("_current", "_previous"),
    )
    merged["Value_current"] = pd.to_numeric(merged.get("Value_current"), errors="coerce").fillna(0.0)
    merged["Value_previous"] = pd.to_numeric(merged.get("Value_previous"), errors="coerce").fillna(0.0)

    totals = (
        merged.groupby(["APO Product", "Des", "Month"], dropna=False)[["Value_current", "Value_previous"]]
        .sum(min_count=1)
        .reset_index()
    )
    totals["Prod Line"] = "Total"
    merged = pd.concat([merged, totals], ignore_index=True, sort=False)

    merged["Current"] = pd.to_numeric(merged["Value_current"], errors="coerce").fillna(0.0)
    merged["Previous"] = pd.to_numeric(merged["Value_previous"], errors="coerce").fillna(0.0)
    merged["Gap"] = merged["Current"] - merged["Previous"]

    result = merged[["Month", "Prod Line", "APO Product", "Des", "Current", "Previous", "Gap"]].copy()

    result["material_key"] = result["APO Product"].apply(normalize_material_key)
    second_level_mapping = read_second_level_mapping(cfg)
    if not second_level_mapping.empty:
        result = result.merge(second_level_mapping, on="material_key", how="left")
    else:
        result["Level2"] = ""
    result["Level2"] = result["Level2"].fillna("").astype(str).str.strip()
    result.loc[result["Level2"] == "", "Level2"] = "未映射"
    result = result.drop(columns=["material_key"])

    result["Current Version"] = str(current_date)
    result["Previous Version"] = str(previous_date)
    result = result.sort_values(["Month", "Prod Line", "Gap", "APO Product", "Des"], ascending=[True, True, False, True, True])
    return result.reset_index(drop=True)


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

    if "Item Text" in df.columns:
        item_text = df["Item Text"]
        normalized_item = item_text.where(item_text.isna(), item_text.astype(str).str.strip())
        normalized_item = normalized_item.mask(normalized_item.astype(str).str.lower().isin({"nan", "none"}), "")
        df["Item Text"] = normalized_item

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

    hc_idp_monthly = summarize_hc_idp_monthly(cfg.data_base_dir)
    write_processed_csv(hc_idp_monthly, cfg.processed_dir, PROCESSED_FILES["hc_idp_monthly"])

    production_data = build_production_data_summary(cfg.production_data_dir, cfg)
    write_processed_csv(production_data, cfg.processed_dir, PROCESSED_FILES["production_data"])

    production_data_by_level = build_production_data_summary_by_level(cfg.production_data_dir, cfg)
    write_processed_csv(production_data_by_level, cfg.processed_dir, PROCESSED_FILES["production_data_by_level"])

    td_validation = build_td_validation_monthly_comparison(cfg.data_base_dir)
    write_processed_csv(td_validation, cfg.processed_dir, PROCESSED_FILES["td_validation_monthly_compare"])

    td_validation_detail = build_td_validation_gap_details(cfg.data_base_dir, cfg)
    write_processed_csv(td_validation_detail, cfg.processed_dir, PROCESSED_FILES["td_validation_gap_detail"])


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
