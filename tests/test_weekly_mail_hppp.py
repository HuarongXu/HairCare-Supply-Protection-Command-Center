import os
from pathlib import Path
import unittest

import pandas as pd

os.environ.setdefault(
    "MATRES_CONFIG",
    str(Path(__file__).resolve().parents[1] / "config" / "config.example.json"),
)

from dashboards.matres_app import (  # noqa: E402
    _with_hppp_level1,
    build_demand_hs_dataframe,
    compute_hppp_monthly_series,
)
from scripts.weekly_mail_summary import (
    calculate_supply_inventory,
    format_supply_inventory,
    safe_dashboard_url,
)


class WeeklyMailHpppTests(unittest.TestCase):
    def setUp(self):
        self.mr_level1 = pd.DataFrame([
            {"availability_month": "2026-09", "First Level": "Base", "total_msu": 20.0},
            {"availability_month": "2026-09", "First Level": "PP", "total_msu": 5.0},
        ])
        self.hppp = pd.DataFrame([
            {"Owner": "BU", "Level1": "Base", "month": 202609, "MSU": 15.0},
            {"Owner": "BU", "Level1": "PP", "month": 202609, "MSU": 7.0},
            {"Owner": "DSTC/SaDC", "Level1": "Base", "month": "2026-09", "MSU": 10.0},
            {"Owner": "HKTW", "Level1": "Base", "month": 202609, "MSU": 99.0},
        ])

    def test_enrichment_folds_base_and_pp_hppp_but_excludes_hktw(self):
        demand = pd.DataFrame([
            {"Prod Line AS": "Base", "2026-09": 100.0},
            {"Prod Line AS": "Promotion", "2026-09": 50.0},
        ])
        enriched = _with_hppp_level1(self.mr_level1, self.hppp)
        result = build_demand_hs_dataframe(demand, enriched).set_index("Prod Line AS")

        self.assertEqual(result.loc["Base", "2026-09"], 145.0)
        self.assertEqual(result.loc["Promotion", "2026-09"], 62.0)
        self.assertEqual(result.loc["Total", "2026-09"], 207.0)

    def test_inventory_total_adds_non_hktw_hppp_once(self):
        monthly_item = pd.DataFrame([
            {"Item Text": "FG Rolling", "total_msu": 10.0},
            {"Item Text": "R Quotation", "total_msu": 5.0},
            {"Item Text": "R Material", "total_msu": 7.0},
        ])

        values = calculate_supply_inventory(
            monthly_item,
            compute_hppp_monthly_series(self.hppp),
        )

        self.assertEqual(
            values,
            {"fg": 15.0, "material": 7.0, "hppp": 32.0, "total": 54.0},
        )

    def test_missing_hppp_contributes_zero(self):
        monthly_item = pd.DataFrame([
            {"Item Text": "FG Rolling", "total_msu": 10.0},
        ])

        values = calculate_supply_inventory(monthly_item, pd.Series(dtype=float))

        self.assertEqual(
            values,
            {"fg": 10.0, "material": 0.0, "hppp": 0.0, "total": 10.0},
        )

    def test_inventory_summary_displays_hppp_component(self):
        line = format_supply_inventory(
            {"fg": 15.0, "material": 7.0, "hppp": 32.0, "total": 54.0}
        )

        self.assertEqual(
            line,
            "Total: 54 msu; FG: 15 msu; Material: 7 msu; HPPP: 32 msu.",
        )

    def test_dashboard_url_rejects_unsafe_scheme_and_escapes_attributes(self):
        fallback = "http://127.0.0.1:8050/"

        self.assertEqual(safe_dashboard_url("javascript:alert(1)", fallback), fallback)
        self.assertEqual(
            safe_dashboard_url('https://example.test/a?x="quoted"', fallback),
            "https://example.test/a?x=&quot;quoted&quot;",
        )


if __name__ == "__main__":
    unittest.main()
