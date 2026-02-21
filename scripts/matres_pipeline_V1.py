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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional

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
    "pde_alerts": "pde_alerts.csv",
}


@dataclass
class PipelineConfig:
    workbook_path: Path
    sheet_name: str
    history_path: Path
    processed_dir: Path
    time_zone: str = "UTC"
    refresh_opts: Optional[Dict] = None

    @staticmethod
    def from_dict(raw: Dict) -> "PipelineConfig":
        root = Path.cwd()
        workbook_path = Path(raw["workbook_path"])
        if not workbook_path.is_absolute():
            workbook_path = root / workbook_path

        history_path = Path(raw["history_path"])
        if not history_path.is_absolute():
            history_path = root / history_path

        processed_dir = Path(raw["processed_dir"])
        if not processed_dir.is_absolute():
            processed_dir = root / processed_dir

        return PipelineConfig(
            workbook_path=workbook_path,
            sheet_name=raw.get("sheet_name", "MatRes Record"),
            history_path=history_path,
            processed_dir=processed_dir,
            time_zone=raw.get("time_zone", "UTC"),
            refresh_opts=raw.get("refresh", {}),
        )


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


def clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for column in DATE_COLUMNS:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], errors="coerce")

    for column in NUMERIC_COLUMNS:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    if "Requester Email" in df.columns:
        df["Requester Email"] = df["Requester Email"].astype(str).str.strip().str.lower()

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
    agg = (
        df.groupby(["availability_month", "Requester Email", "Item Text"], dropna=False)["MSU"]
        .sum(min_count=1)
        .reset_index()
        .rename(columns={"MSU": "total_msu"})
    )
    return agg.sort_values(["availability_month", "total_msu"], ascending=[True, False])


def summarize_pde_alerts(df: pd.DataFrame) -> pd.DataFrame:
    if "PDE Checking" not in df.columns:
        raise ValueError("PDE Checking column missing from dataset")
    pde_df = df[df["PDE Checking"].notna() & (df["PDE Checking"] > 0)].copy()
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
            ]
        )

    pde_df["availability_date"] = pde_df["Availability Date"].dt.date

    summary = (
        pde_df.groupby(["Requester Email", "availability_date"], dropna=False)
        .agg(
            msu_due=("MSU", "sum"),
            open_items=("PDE Checking", "count"),
            max_pde=("PDE Checking", "max"),
            avg_pde=("PDE Checking", "mean"),
            closest_availability=("Availability Date", "min"),
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
    df_clean = clean_dataframe(df_raw)

    append_history_snapshot(df_clean, cfg)

    monthly_item = summarize_monthly_by_item(df_clean)
    write_processed_csv(monthly_item, cfg.processed_dir, PROCESSED_FILES["monthly_item"])

    monthly_requester = summarize_monthly_by_requester_item(df_clean)
    write_processed_csv(
        monthly_requester,
        cfg.processed_dir,
        PROCESSED_FILES["monthly_requester"],
    )

    pde_alerts = summarize_pde_alerts(df_clean)
    write_processed_csv(pde_alerts, cfg.processed_dir, PROCESSED_FILES["pde_alerts"])


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
