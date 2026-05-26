"""Simulate the update_visuals callback to find errors."""
import sys, traceback, pandas as pd, logging
logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, "dashboards")
import re

from pathlib import Path
from matres_app import (
    AppConfig, load_data_bundle, load_request_details,
    build_monthly_matrix, build_hc_idp_monthly_table,
    build_demand_hs_dataframe, build_demand_iya_table,
    build_demand_iya_by_quarter_table, split_quarter_iya_tables,
    build_td_validation_table_from_detail, build_td_validation_style_data_conditional,
    build_production_data_table_by_plant, build_production_data_table_by_plant_level,
    build_production_version_comparison_table, build_production_version_style_data_conditional,
    build_first_level_summary, build_pde_tables,
    build_role_item_project_summary, extract_role_item_project_total_row,
    build_requester_email_options, build_mrp_indicator_options, build_item_text_options,
    build_role_trend, ROLE_ALL_VALUE, TOTAL_LABEL,
    normalize_requester_values, normalize_mrp_values,
)

cfg = AppConfig.load(Path("config/config.json"))
data = load_data_bundle(cfg)

try:
    monthly_requester = pd.DataFrame(data.get("monthly_requester", []))
    monthly_level1 = pd.DataFrame(data.get("monthly_level1", []))
    hc_idp_monthly = pd.DataFrame(data.get("hc_idp_monthly", []))
    production_data_df = pd.DataFrame(data.get("production_data", []))
    production_data_by_level_df = pd.DataFrame(data.get("production_data_by_level", []))
    td_validation_detail = pd.DataFrame(data.get("td_validation_detail", []))
    historical_shipment = pd.DataFrame(data.get("historical_shipment", []))
    pde_alerts = pd.DataFrame(data.get("pde_alerts", []))
    request_details = load_request_details(cfg)
    print("Step 0: data loaded")

    selected_role = ROLE_ALL_VALUE
    table_columns, table_data = build_monthly_matrix(monthly_requester, selected_role)
    print("Step 1: monthly_matrix")

    drill_requester_options = build_requester_email_options(request_details, selected_role)
    valid_requesters = []
    drill_mrp_options = build_mrp_indicator_options(request_details, selected_role, valid_requesters)
    valid_mrp_indicators = []
    drill_item_text_options = build_item_text_options(request_details, selected_role, valid_requesters, valid_mrp_indicators)
    valid_item_texts = []
    print("Step 2: drill filters")

    role_fig = build_role_trend(monthly_requester, selected_role)
    pde_columns, pde_records, pde_fg_columns, pde_fg_records = build_pde_tables(pde_alerts, request_details)
    drill_columns, drill_rows = build_role_item_project_summary(request_details, selected_role, valid_requesters, valid_mrp_indicators, valid_item_texts)
    drill_total_columns, drill_total_rows, drill_rows = extract_role_item_project_total_row(drill_columns, drill_rows)
    print("Step 3: drill tables")

    hc_idp_columns, hc_idp_rows = build_hc_idp_monthly_table(hc_idp_monthly)
    print("Step 4: hc_idp", len(hc_idp_rows), "rows")

    hc_idp_hs_df = build_demand_hs_dataframe(hc_idp_monthly, monthly_level1)
    hc_idp_iya_columns, hc_idp_iya_rows = build_demand_iya_table(hc_idp_monthly, historical_shipment)
    hc_idp_hs_columns, hc_idp_hs_rows = build_hc_idp_monthly_table(hc_idp_hs_df)
    hc_idp_hs_iya_columns, hc_idp_hs_iya_rows = build_demand_iya_table(hc_idp_hs_df, historical_shipment)
    print("Step 5: demand tables")

    hc_idp_quarter_iya_columns, hc_idp_quarter_iya_rows = build_demand_iya_by_quarter_table(
        hc_idp_monthly, hc_idp_hs_df, historical_shipment)
    (_, quarter1_columns, quarter1_rows), (_, quarter2_columns, quarter2_rows) = split_quarter_iya_tables(
        hc_idp_quarter_iya_columns, hc_idp_quarter_iya_rows)
    print("Step 6: quarter")

    td_validation_columns, td_validation_rows = build_td_validation_table_from_detail(td_validation_detail)
    td_validation_styles = build_td_validation_style_data_conditional(td_validation_columns)
    print("Step 7: td_validation")

    production_group_1 = ["0386", "1864", "A868"]
    production_plant_columns_1, production_plant_rows_1 = build_production_data_table_by_plant(
        production_data_df, plant_order=production_group_1, include_segment_totals=False)
    print("Step 8: prod_plant")

    production_version_df = pd.DataFrame(data.get("production_version_compare", []))
    production_version_columns, production_version_rows = build_production_version_comparison_table(production_version_df)
    production_version_styles = build_production_version_style_data_conditional(production_version_columns)
    print("Step 9: prod_version", len(production_version_rows), "rows")

    production_level_columns_1, production_level_rows_1 = build_production_data_table_by_plant_level(
        production_data_by_level_df, plant_order=production_group_1,
        include_segment_totals=True,
        segment_totals_after={"0386": ("HP Total", ["0386", "C810"]), "1864": ("XQ Total", ["1864", "D352"]), "A868": ("TC Total", ["A868", "A673"])})
    print("Step 10: prod_level")

    level1_core_columns, level1_core_rows = build_first_level_summary(
        monthly_level1, source_level_column="First Level", display_level_column="Level 1", include_levels=["Base", "PP"])
    level1_hktw_ess_columns, level1_hktw_ess_rows = build_first_level_summary(
        monthly_level1, source_level_column="First Level", display_level_column="Level 1", include_levels=["HKTW", "ESS"])
    print("Step 11: level1")

    print("\n=== ALL STEPS OK ===")

except Exception:
    traceback.print_exc()
