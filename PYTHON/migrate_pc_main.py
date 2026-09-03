#!/usr/bin/env python3
"""One-time PC_MAIN data migration helpers.

The executable logic is already migrated to Python.  Use this script once to
bring the 35 calibration records from the legacy Access database into the new
SQLite store.  Access is deliberately not required on the Raspberry Pi.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from calibration_store import CalibrationStore


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE = SCRIPT_DIR / "data" / "integrated_hmi.sqlite3"


def import_csv(csv_path: Path, database_path: Path) -> int:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    if not rows:
        raise ValueError("校准 CSV 没有数据")
    CalibrationStore(database_path).import_pc_main_rows(rows)
    return len(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import PC_MAIN calibration data into the integrated HMI")
    parser.add_argument("--calibration-csv", type=Path, required=True, help="Table_Measurement 导出的 CSV")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help="目标 SQLite 文件")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    imported = import_csv(args.calibration_csv.expanduser().resolve(), args.database.expanduser().resolve())
    print(f"已迁移 {imported} 条校准记录到 {args.database}")
