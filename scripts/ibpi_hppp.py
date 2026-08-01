"""IBPI safety incremental (HPPP) reader — Databricks (Azure China).

Pulls weekly IBPI safety-stock figures for *all* FG codes (prdid) over the next
~half year (weekly Mondays) from the ODS full-horizon table, and writes a flat
CSV snapshot to ``data/processed/ibpi_hppp_weekly.csv``.

Connection parameters are read from environment variables (loaded from a local
``.env`` file — never hard-coded). See ``.env.example`` for the required keys.

Notes on Azure China + corporate proxy (learned on sibling projects):
  * The Azure China workspace enforces an IP allow-list. The corporate HTTP proxy
    egresses from a non-allow-listed IP, so proxied connects return
    "Unauthorized network access to workspace". We therefore bypass the proxy for
    the duration of the connect/query (``DATABRICKS_BYPASS_PROXY=1``).
  * ``dbsql.connect`` has no default timeout and can hang forever under a bad
    proxy/network. We run the whole query in a daemon thread and give up after
    ``DATABRICKS_QUERY_TIMEOUT`` seconds.

Run directly:
    python scripts/ibpi_hppp.py
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is expected to be installed
    load_dotenv = None

from databricks import sql as dbsql

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
TABLE = "cdl_ps_hana_prd.ods.psdh_pdp_ibpi_ibp_sap_full_horizon_weekly_cn"
UOM_TABLE = "cdl_ps_hana_prd.dwd.tb_mdm_prod_material_uom_dim"
OUTPUT_CSV = Path("data/processed/ibpi_hppp_weekly.csv")
OUTPUT_MONTHLY_CSV = Path("data/processed/ibpi_hppp_monthly.csv")
OUTPUT_OWNER_SUMMARY_CSV = Path("data/processed/ibpi_hppp_owner_summary.csv")

# Seg (segmentation) mapping workbook: FG code -> Level1/2/3.
DEFAULT_SEG_WORKBOOK = Path("0.Data Base/HairCare Code List By Seg_Update Version.xlsx")
DEFAULT_SEG_SHEET = "Seg summary by code_New Version"
SEG_MATERIAL_COLUMN = "Material"
SEG_LEVEL_COLUMNS = {
    "Level1": "First Level",
    "Level2": "Second Level",
    "Level3": "Third Level",  # matched case/space-insensitively
}

OUTPUT_COLUMNS = [
    "Owner",
    "plant",
    "FPC",
    "Level1",
    "Level2",
    "Level3",
    "keyfiguredate",
    "HPPP_SU",
    "HPPP_CS",
    "MSU",
    "zpublishindicator",
]

DEFAULT_WEEKS_AHEAD = 26          # ~half a year of weekly buckets
DEFAULT_BATCH_SIZE = 5            # max keyfiguredate values per query batch
DEFAULT_SU_BATCH_SIZE = 200       # max FG codes per UOM-factor query batch
DEFAULT_CATEGORY = "Hair Care"
DEFAULT_CONNECT_TIMEOUT = 30      # seconds (socket)
DEFAULT_QUERY_TIMEOUT = 120       # seconds (overall, per run)
PUBLISH_INDICATORS = (1, 2)
SU_ALTER_UOM = "SU"

# These plants (locid) belong to the BU; every other DC is treated as DSTC.
# NB: this list is only used for Owner classification, NOT as a SQL filter —
# all plants with HPPP data are pulled.
BU_PLANTS = frozenset(
    ("A672", "A715", "A716", "A673", "A680", "A668", "C810", "C816", "D352")
)
HKTW_PLANTS = frozenset(("E810", "D594"))


def _owner_of(plant: object) -> str:
    """Classify a plant/DC into an Owner bucket: BU / HKTW / DSTC/SaDC."""
    code = str(plant).strip().upper()
    if code in BU_PLANTS:
        return "BU"
    if code in HKTW_PLANTS:
        return "HKTW"
    return "DSTC/SaDC"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
class IbpiConfig:
    """Runtime configuration sourced entirely from environment variables."""

    def __init__(self) -> None:
        self.host = _require_env("DATABRICKS_HOST")
        self.http_path = _require_env("DATABRICKS_HTTP_PATH")
        self.token = _require_env("DATABRICKS_TOKEN")

        self.category = os.getenv("IBPI_CATEGORY", DEFAULT_CATEGORY)
        self.weeks_ahead = _int_env("IBPI_WEEKS_AHEAD", DEFAULT_WEEKS_AHEAD)
        self.batch_size = max(1, _int_env("IBPI_BATCH_SIZE", DEFAULT_BATCH_SIZE))
        self.su_batch_size = max(1, _int_env("IBPI_SU_BATCH_SIZE", DEFAULT_SU_BATCH_SIZE))
        self.seg_workbook = Path(
            os.getenv("IBPI_SEG_WORKBOOK", str(DEFAULT_SEG_WORKBOOK))
        )
        self.seg_sheet = os.getenv("IBPI_SEG_SHEET", DEFAULT_SEG_SHEET)
        self.connect_timeout = _int_env(
            "DATABRICKS_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT
        )
        self.query_timeout = _int_env(
            "DATABRICKS_QUERY_TIMEOUT", DEFAULT_QUERY_TIMEOUT
        )
        self.bypass_proxy = os.getenv("DATABRICKS_BYPASS_PROXY", "1") == "1"


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        raise RuntimeError(
            f"Missing required environment variable '{name}'. "
            f"Copy .env.example to .env and fill in the Databricks credentials."
        )
    return value.strip()


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logging.warning("Env %s=%r is not an int; using default %s", name, raw, default)
        return default


# --------------------------------------------------------------------------- #
# Date helpers
# --------------------------------------------------------------------------- #
def future_mondays(weeks_ahead: int, today: Optional[date] = None) -> List[str]:
    """Return ``weeks_ahead`` weekly Monday key-figure dates as ``YYYYMMDD``.

    Starts from the Monday of the current week (inclusive) and steps forward one
    week at a time.
    """
    today = today or date.today()
    this_monday = today - timedelta(days=today.weekday())
    return [
        (this_monday + timedelta(weeks=i)).strftime("%Y%m%d")
        for i in range(max(0, weeks_ahead))
    ]


def _chunks(items: List[str], size: int) -> List[List[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _norm_code(value: Any) -> str:
    """Normalise a material/FG code to a bare integer string (strip padding).

    Handles zero-padded SAP material numbers ('000000000080754073'), floats
    ('80754073.0') and plain strings, so ODS ``prdid``, the UOM ``material_num``
    and the seg workbook ``Material`` column all reconcile on one key.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if text.isdigit():
        stripped = text.lstrip("0")
        return stripped if stripped else "0"
    return text


def _week_to_month(keyfiguredate: str) -> str:
    """Assign a weekly (Monday) key-figure date to a calendar month (YYYYMM).

    A cross-month week is assigned to whichever month holds the majority of the
    week's seven days (Mon..Sun). E.g. the week of 2026-08-31 has one day in Aug
    and six in Sep, so it maps to 202609. For a 7-day span across two months a
    tie is impossible (splits range 6-1 .. 4-3), so the majority is well defined.
    """
    try:
        monday = datetime.strptime(str(keyfiguredate).strip(), "%Y%m%d").date()
    except (ValueError, TypeError):
        return str(keyfiguredate)[:6]
    counts: Dict[str, int] = {}
    for i in range(7):
        month = (monday + timedelta(days=i)).strftime("%Y%m")
        counts[month] = counts.get(month, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


# --------------------------------------------------------------------------- #
# Query building
# --------------------------------------------------------------------------- #
def _build_query(date_batch: List[str]) -> tuple[str, Dict[str, Any]]:
    """Build a parameterised query for one batch of key-figure dates.

    All values are bound as native parameters (``:name``) — no string
    interpolation of values into SQL.
    """
    date_markers = ", ".join(f":d{i}" for i in range(len(date_batch)))
    publish_markers = ", ".join(f":p{i}" for i in range(len(PUBLISH_INDICATORS)))

    query = f"""
        SELECT
            locid   AS plant,
            prdid   AS FPC,
            keyfiguredate,
            CAST(zincrhppptotalsafetystockqty AS DOUBLE) AS HPPP,
            zpublishindicator
        FROM {TABLE}
        WHERE category = :category
          AND keyfiguredate IN ({date_markers})
          AND zpublishindicator IN ({publish_markers})
          AND extractiondate = (
                SELECT MAX(extractiondate)
                FROM {TABLE}
              )
    """

    params: Dict[str, Any] = {"category": None}
    # category filled by caller so we keep a single source of truth
    for i, d in enumerate(date_batch):
        params[f"d{i}"] = d
    for i, p in enumerate(PUBLISH_INDICATORS):
        params[f"p{i}"] = p
    return query, params


def _build_su_query(fpc_batch: List[int]) -> tuple[str, Dict[str, Any]]:
    """Build a parameterised query for SU unit-of-measure conversion factors.

    Matches ``material_num`` (zero-padded) to the bare integer FG codes via
    ``try_cast(... AS BIGINT)`` so no wildcard string interpolation is needed.
    """
    code_markers = ", ".join(f":c{i}" for i in range(len(fpc_batch)))
    query = f"""
        SELECT
            material_num,
            base_uom_convert_numerator,
            base_uom_convert_denominator
        FROM {UOM_TABLE}
        WHERE alter_uom_for_sku = :alter_uom
          AND try_cast(material_num AS BIGINT) IN ({code_markers})
    """
    params: Dict[str, Any] = {"alter_uom": SU_ALTER_UOM}
    for i, code in enumerate(fpc_batch):
        params[f"c{i}"] = code
    return query, params


# --------------------------------------------------------------------------- #
# Databricks access
# --------------------------------------------------------------------------- #
def _fetch_all(cfg: IbpiConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Connect + run every batch, returning ``(hppp_df, su_factors_df)``.

    Both the HPPP horizon query and the SU conversion-factor lookup run over the
    same connection, inside a daemon thread guarded by an overall timeout so a
    hung connect/query can never block the process indefinitely.
    """
    result: Dict[str, Any] = {"df": None, "su": None, "error": None}
    date_batches = _chunks(future_mondays(cfg.weeks_ahead), cfg.batch_size)

    def worker() -> None:
        saved_proxy: Dict[str, Optional[str]] = {}
        try:
            if cfg.bypass_proxy:
                saved_proxy = _pop_proxy_env()

            logging.info(
                "Connecting to Databricks %s (bypass_proxy=%s)",
                cfg.host,
                cfg.bypass_proxy,
            )
            frames: List[pd.DataFrame] = []
            with dbsql.connect(
                server_hostname=cfg.host,
                http_path=cfg.http_path,
                access_token=cfg.token,
                _socket_timeout=cfg.connect_timeout,
            ) as conn:
                for idx, batch in enumerate(date_batches, start=1):
                    query, params = _build_query(batch)
                    params["category"] = cfg.category
                    logging.info(
                        "Batch %d/%d: %d weeks (%s .. %s)",
                        idx,
                        len(date_batches),
                        len(batch),
                        batch[0],
                        batch[-1],
                    )
                    with conn.cursor() as cur:
                        cur.execute(query, params)
                        rows = cur.fetchall()
                        cols = [c[0] for c in cur.description]
                    frames.append(pd.DataFrame(rows, columns=cols))

                if frames and any(not f.empty for f in frames):
                    combined = pd.concat(
                        [f for f in frames if not f.empty], ignore_index=True
                    )
                else:
                    combined = pd.DataFrame(columns=_BASE_COLUMNS)
                result["df"] = combined

                # SU conversion factors for the FG codes we actually retrieved
                # (same open connection).
                fpc_codes = _distinct_fpc_ints(combined)
                result["su"] = _fetch_su_factors(conn, cfg, fpc_codes)
        except Exception as exc:  # noqa: BLE001 - surfaced to caller
            result["error"] = exc
        finally:
            if cfg.bypass_proxy:
                _restore_proxy_env(saved_proxy)

    thread = threading.Thread(target=worker, name="ibpi-databricks", daemon=True)
    thread.start()
    thread.join(cfg.query_timeout)

    if thread.is_alive():
        raise TimeoutError(
            f"Databricks query exceeded {cfg.query_timeout}s and was abandoned. "
            f"Check network/proxy access to {cfg.host}."
        )
    if result["error"] is not None:
        raise result["error"]
    su = result["su"]
    if su is None:
        su = pd.DataFrame(columns=["FPC", "su_numerator", "su_denominator"])
    return result["df"], su


def _distinct_fpc_ints(hppp_df: pd.DataFrame) -> List[int]:
    """Distinct FG codes (as ints) present in the HPPP result."""
    if hppp_df.empty or "FPC" not in hppp_df.columns:
        return []
    codes: set[int] = set()
    for raw in hppp_df["FPC"].tolist():
        norm = _norm_code(raw)
        if norm.isdigit():
            codes.add(int(norm))
    return sorted(codes)


def _fetch_su_factors(conn, cfg: IbpiConfig, fpc_codes: List[int]) -> pd.DataFrame:
    """Fetch SU conversion factors for the given FG codes over an open conn."""
    empty = pd.DataFrame(columns=["FPC", "su_numerator", "su_denominator"])
    if not fpc_codes:
        return empty

    frames: List[pd.DataFrame] = []
    batches = _chunks([str(c) for c in fpc_codes], cfg.su_batch_size)
    for idx, batch in enumerate(batches, start=1):
        int_batch = [int(c) for c in batch]
        query, params = _build_su_query(int_batch)
        logging.info("SU factors batch %d/%d: %d codes", idx, len(batches), len(int_batch))
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            cols = [c[0] for c in cur.description]
        frames.append(pd.DataFrame(rows, columns=cols))

    raw = pd.concat([f for f in frames if not f.empty], ignore_index=True) if any(
        not f.empty for f in frames
    ) else empty.copy()
    if raw.empty:
        return empty

    raw["FPC"] = raw["material_num"].apply(_norm_code)
    raw["su_numerator"] = pd.to_numeric(raw["base_uom_convert_numerator"], errors="coerce")
    raw["su_denominator"] = pd.to_numeric(raw["base_uom_convert_denominator"], errors="coerce")
    raw = raw[raw["FPC"].astype(bool)].copy()
    raw = raw.drop_duplicates(subset=["FPC"], keep="first")
    return raw[["FPC", "su_numerator", "su_denominator"]]



_PROXY_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)


def _pop_proxy_env() -> Dict[str, Optional[str]]:
    saved: Dict[str, Optional[str]] = {}
    for name in _PROXY_VARS:
        saved[name] = os.environ.pop(name, None)
    saved["NO_PROXY"] = os.environ.get("NO_PROXY")
    saved["no_proxy"] = os.environ.get("no_proxy")
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    return saved


def _restore_proxy_env(saved: Dict[str, Optional[str]]) -> None:
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


# --------------------------------------------------------------------------- #
# Normalisation / enrichment / output
# --------------------------------------------------------------------------- #
_BASE_COLUMNS = ["plant", "FPC", "keyfiguredate", "HPPP", "zpublishindicator"]


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=_BASE_COLUMNS)

    # Keep only the expected columns and order (guard against driver casing).
    lower_map = {c.lower(): c for c in df.columns}
    ordered: Dict[str, pd.Series] = {}
    for col in _BASE_COLUMNS:
        src = lower_map.get(col.lower())
        if src is not None:
            ordered[col] = df[src]
        else:
            ordered[col] = pd.Series([pd.NA] * len(df))
    out = pd.DataFrame(ordered)

    out["HPPP"] = pd.to_numeric(out["HPPP"], errors="coerce")
    out["keyfiguredate"] = out["keyfiguredate"].astype(str)
    out["FPC"] = out["FPC"].astype(str)
    out["plant"] = out["plant"].astype(str)
    out = out[out["HPPP"].notna()]
    return out.sort_values(["keyfiguredate", "FPC", "plant"]).reset_index(drop=True)


def _read_seg_mapping(cfg: IbpiConfig) -> pd.DataFrame:
    """Read the seg workbook -> DataFrame [FPC, Level1, Level2, Level3]."""
    cols = ["FPC", "Level1", "Level2", "Level3"]
    workbook = cfg.seg_workbook
    if not workbook.exists():
        logging.warning("Seg workbook %s not found; levels will be blank", workbook)
        return pd.DataFrame(columns=cols)

    logging.info("Reading seg mapping sheet '%s' from %s", cfg.seg_sheet, workbook)
    mapping = pd.read_excel(workbook, sheet_name=cfg.seg_sheet)

    normalized_cols = {str(c).strip().lower(): c for c in mapping.columns}
    material_col = normalized_cols.get(SEG_MATERIAL_COLUMN.lower())
    if material_col is None:
        logging.warning("Seg workbook missing '%s' column; levels will be blank", SEG_MATERIAL_COLUMN)
        return pd.DataFrame(columns=cols)

    out = pd.DataFrame()
    out["FPC"] = mapping[material_col].apply(_norm_code)
    for out_name, src_label in SEG_LEVEL_COLUMNS.items():
        src = normalized_cols.get(src_label.strip().lower())
        out[out_name] = (
            mapping[src].fillna("").astype(str).str.strip() if src is not None else ""
        )
    out = out[out["FPC"].astype(bool)].copy()
    out = out.drop_duplicates(subset=["FPC"], keep="first")
    return out[cols]


def _enrich(df: pd.DataFrame, seg: pd.DataFrame, su: pd.DataFrame) -> pd.DataFrame:
    """Merge seg levels + SU factors, compute HPPP_CS and MSU."""
    if df.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    work = df.copy()
    work["_key"] = work["FPC"].apply(_norm_code)

    seg_idx = seg.rename(columns={"FPC": "_key"}) if not seg.empty else pd.DataFrame(
        columns=["_key", "Level1", "Level2", "Level3"]
    )
    work = work.merge(seg_idx, on="_key", how="left")
    for level in ("Level1", "Level2", "Level3"):
        if level not in work.columns:
            work[level] = ""
        work[level] = work[level].fillna("")

    su_idx = su.rename(columns={"FPC": "_key"}) if not su.empty else pd.DataFrame(
        columns=["_key", "su_numerator", "su_denominator"]
    )
    work = work.merge(su_idx, on="_key", how="left")

    numer = pd.to_numeric(work.get("su_numerator"), errors="coerce")
    denom = pd.to_numeric(work.get("su_denominator"), errors="coerce")
    valid = numer.notna() & denom.notna() & (numer != 0)

    # HPPP_SU = raw SU value (rounded)
    work["HPPP_SU"] = work["HPPP"].round(2)

    # HPPP_CS = SU * numer / denom (convert to Case units)
    work["HPPP_CS"] = pd.NA
    work.loc[valid, "HPPP_CS"] = (work.loc[valid, "HPPP"] * numer[valid] / denom[valid]).round(2)
    work["HPPP_CS"] = pd.to_numeric(work["HPPP_CS"], errors="coerce")

    # MSU = SU / 1000
    work["MSU"] = (work["HPPP"] / 1000).round(2)

    missing = int((~valid).sum())
    if missing:
        logging.warning("%d rows had no SU conversion factor; HPPP_CS left blank", missing)

    work["Owner"] = work["plant"].apply(_owner_of)
    ordered = {col: work[col] for col in OUTPUT_COLUMNS}
    return pd.DataFrame(ordered).reset_index(drop=True)


def _aggregate_monthly(df: pd.DataFrame) -> pd.DataFrame:
    """Roll weekly HPPP/MSU up to a monthly figure per FG code + plant.

    The monthly value is the mean of that month's weekly buckets
    (a month's figure == average of its weekly figures). Seg levels are carried.
    """
    monthly_columns = [
        "Owner", "FPC", "plant", "Level1", "Level2", "Level3", "month",
        "HPPP_SU", "HPPP_CS", "MSU", "weeks",
    ]
    if df.empty:
        return pd.DataFrame(columns=monthly_columns)

    work = df.copy()
    work["month"] = work["keyfiguredate"].apply(_week_to_month)
    work["Owner"] = work["plant"].apply(_owner_of)
    for level in ("Level1", "Level2", "Level3"):
        if level not in work.columns:
            work[level] = ""
        work[level] = work[level].fillna("")
    grouped = (
        work.groupby(["Owner", "FPC", "plant", "Level1", "Level2", "Level3", "month"], as_index=False)
        .agg(HPPP_SU=("HPPP_SU", "mean"), HPPP_CS=("HPPP_CS", "mean"), MSU=("MSU", "mean"), weeks=("HPPP_SU", "size"))
    )
    grouped["HPPP_SU"] = grouped["HPPP_SU"].round(2)
    grouped["HPPP_CS"] = grouped["HPPP_CS"].round(2)
    grouped["MSU"] = grouped["MSU"].round(2)
    grouped = grouped[monthly_columns]
    return grouped.sort_values(["month", "FPC", "plant"]).reset_index(drop=True)


def _aggregate_owner_summary(monthly: pd.DataFrame) -> pd.DataFrame:
    """Owner x Level1 x Level2 summary of monthly MSU, months as columns.

    Pivots months horizontally so each month becomes its own column. Adds
    Total (sum) and Avg columns at the end. Rows are grouped by
    Owner / Level1 / Level2 with subtotal rows per Owner and a grand Total
    row at the bottom.
    """
    if monthly.empty:
        return pd.DataFrame(columns=["Owner", "Level1", "Level2"])

    work = monthly.copy()
    work["Owner"] = work["plant"].apply(_owner_of)
    for level in ("Level1", "Level2"):
        work[level] = work[level].fillna("")
    work["month"] = work["month"].astype(str).str.strip()
    work["MSU"] = pd.to_numeric(work["MSU"], errors="coerce").fillna(0.0)

    grouped = (
        work.groupby(["Owner", "Level1", "Level2", "month"], as_index=False)
        .agg(MSU=("MSU", "sum"))
    )

    # Pivot months into columns
    months = sorted(grouped["month"].unique())
    pivoted = grouped.pivot_table(
        index=["Owner", "Level1", "Level2"],
        columns="month",
        values="MSU",
        aggfunc="sum",
        fill_value=0.0,
    ).reset_index()
    pivoted.columns.name = None  # remove multi-index name

    # Round month columns and compute Total / Avg
    for m in months:
        if m in pivoted.columns:
            pivoted[m] = pivoted[m].round(2)
    pivoted["Total"] = pivoted[months].sum(axis=1).round(2)
    pivoted["Avg"] = (pivoted[months].mean(axis=1)).round(2)

    # Build output with subtotals
    sections: List[pd.DataFrame] = []
    for owner in ("BU", "HKTW", "DSTC/SaDC"):
        part = pivoted[pivoted["Owner"] == owner].sort_values(["Level1", "Level2"]).reset_index(drop=True)
        if part.empty:
            continue
        subtotal: Dict[str, Any] = {"Owner": f"Total {owner}", "Level1": "", "Level2": ""}
        for col in months + ["Total", "Avg"]:
            subtotal[col] = round(part[col].sum(), 2) if col != "Avg" else round(part[months].sum().mean(), 2)
        sections.append(part)
        sections.append(pd.DataFrame([subtotal]))

    grand: Dict[str, Any] = {"Owner": "Total", "Level1": "", "Level2": ""}
    for col in months + ["Total", "Avg"]:
        if col == "Avg":
            grand[col] = round(pivoted[months].sum().mean(), 2)
        else:
            grand[col] = round(pivoted[col].sum(), 2) if col in pivoted.columns else 0.0
    sections.append(pd.DataFrame([grand]))

    result = pd.concat(sections, ignore_index=True)
    return result


def run(output_path: Optional[Path] = None) -> pd.DataFrame:
    """Fetch IBPI HPPP data and write the CSV snapshots. Returns the DataFrame."""
    cfg = IbpiConfig()
    raw, su = _fetch_all(cfg)
    base = _normalise(raw)
    seg = _read_seg_mapping(cfg)
    df = _enrich(base, seg, su)

    out_path = output_path or OUTPUT_CSV
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    logging.info("Wrote %d rows to %s", len(df), out_path)

    monthly = _aggregate_monthly(df)
    OUTPUT_MONTHLY_CSV.parent.mkdir(parents=True, exist_ok=True)
    monthly.to_csv(OUTPUT_MONTHLY_CSV, index=False)
    logging.info("Wrote %d monthly rows to %s", len(monthly), OUTPUT_MONTHLY_CSV)

    owner_summary = _aggregate_owner_summary(monthly)
    owner_summary.to_csv(OUTPUT_OWNER_SUMMARY_CSV, index=False)
    logging.info("Wrote %d owner-summary rows to %s", len(owner_summary), OUTPUT_OWNER_SUMMARY_CSV)
    return df


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    if load_dotenv is not None:
        load_dotenv()  # loads .env from CWD / repo root if present
    else:
        logging.warning("python-dotenv not installed; relying on process environment only")

    df = run()
    if df.empty:
        logging.warning("No IBPI HPPP rows returned for the requested horizon.")
    else:
        logging.info(
            "Done: %d rows, %d FG codes, %d weeks.",
            len(df),
            df["FPC"].nunique(),
            df["keyfiguredate"].nunique(),
        )


if __name__ == "__main__":
    main()
