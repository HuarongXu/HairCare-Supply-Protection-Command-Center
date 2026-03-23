from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboards.matres_app import AppConfig, CONFIG_PATH, create_dashboard_snapshot


if __name__ == "__main__":
    cfg = AppConfig.load(CONFIG_PATH)
    snapshot_dir, snapshot_excel, exported_tables = create_dashboard_snapshot(cfg)
    print(f"snapshot_dir={snapshot_dir}")
    print(f"snapshot_excel={snapshot_excel}")
    print(f"exported_tables={exported_tables}")
