# Email Summary HPPP Integration Design

## Goal

Make the generated weekly email use the same HPPP-inclusive supply-protection calculations as the live dashboard, without changing recipients, sending behavior, or visual styling.

## Scope

- Load `ibpi_hppp_monthly.csv` from the configured processed-data directory.
- Fold non-HKTW HPPP into Base/PP before calculating monthly and quarterly `System LBE + Supply System Protection` values and IYA.
- Include non-HKTW HPPP in the Supply Summary total.
- Display HPPP as a separate component in the Supply Summary breakdown so the total is auditable.
- Preserve existing `Demand System LBE` values because HPPP is supply protection, not demand.

## Data flow

1. The email generator loads the existing dashboard data bundle.
2. It separately loads the monthly HPPP dataset using the existing restricted processed-directory loader.
3. It calls the dashboard's existing HPPP helpers:
   - `_with_hppp_level1()` for Base/PP integration.
   - `compute_hppp_monthly_series()` for the standalone HPPP total.
4. The HPPP-enriched Level1 frame is passed to the existing demand-plus-protection monthly, quarterly, and IYA builders.
5. The email Supply Summary total becomes `FG + Material + HPPP`, with all three components shown.

## Business rules

- Exclude `Owner == HKTW`, matching the dashboard.
- Only HPPP mapped to Base or PP affects the Demand Assumption protection calculations.
- The Supply Summary HPPP component uses all non-HKTW HPPP, matching the dashboard's all-protection metric.
- HPPP must be added exactly once.
- Missing or empty HPPP input contributes zero; email generation remains available.

## Error handling and security

- Read only the fixed server-controlled file name under the configured processed-data directory.
- Do not expose internal paths or stack traces in generated email content.
- Preserve existing HTML escaping/sanitization behavior and introduce no raw user-controlled HTML.
- Log missing optional HPPP data without logging credentials or sensitive row data.

## Verification

- Add a focused automated test with synthetic LBE, MR, and HPPP data proving:
  - HKTW is excluded.
  - Base/PP HPPP changes monthly and quarterly protection totals and IYA.
  - Supply Summary total includes HPPP once and displays its component.
- Generate the current weekly email preview and compare its 2026-09 protection value with the live dashboard.
- Confirm the preview still renders and no Python diagnostics are introduced.

## Ownership and exposure

- Owner: MatRes dashboard maintainers.
- Data sensitivity: internal supply-chain planning data.
- Exposure: authenticated/internal dashboard email-preview flow; no new endpoint or network boundary.
