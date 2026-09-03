"""SQLite replacement for PC_MAIN's Access calibration table."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import re


CHANNEL_COUNT = 35


class CalibrationStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _session(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._session() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS measurement_calibration (
                    channel INTEGER PRIMARY KEY,
                    purpose TEXT NOT NULL DEFAULT '',
                    gain REAL NOT NULL DEFAULT 1.0,
                    offset REAL NOT NULL DEFAULT 0.0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            timestamp = _now()
            for channel in range(1, CHANNEL_COUNT + 1):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO measurement_calibration
                    (channel, purpose, gain, offset, updated_at)
                    VALUES (?, ?, 1.0, 0.0, ?)
                    """,
                    (channel, f"CH{channel}", timestamp),
                )

    def list(self) -> list[dict[str, Any]]:
        with self._lock, self._session() as connection:
            rows = connection.execute(
                """
                SELECT channel, purpose, gain, offset, updated_at
                FROM measurement_calibration ORDER BY channel
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def replace(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(items) != CHANNEL_COUNT:
            raise ValueError(f"必须一次提交 {CHANNEL_COUNT} 个通道")
        normalized = [_normalize_item(item) for item in items]
        channels = {item["channel"] for item in normalized}
        expected = set(range(1, CHANNEL_COUNT + 1))
        if channels != expected:
            raise ValueError("通道必须完整覆盖 CH1～CH35，且不能重复")
        timestamp = _now()
        with self._lock, self._session() as connection:
            connection.executemany(
                """
                UPDATE measurement_calibration
                SET purpose = ?, gain = ?, offset = ?, updated_at = ?
                WHERE channel = ?
                """,
                [
                    (item["purpose"], item["gain"], item["offset"], timestamp, item["channel"])
                    for item in normalized
                ],
            )
        return self.list()

    def set_uniform(self, gain: float, offset: float) -> list[dict[str, Any]]:
        gain = _finite_number(gain, "gain")
        offset = _finite_number(offset, "offset")
        timestamp = _now()
        with self._lock, self._session() as connection:
            connection.execute(
                "UPDATE measurement_calibration SET gain = ?, offset = ?, updated_at = ?",
                (gain, offset, timestamp),
            )
        return self.list()

    def import_pc_main_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Import rows exported from PC_MAIN's Table_Measurement Access table."""
        items: list[dict[str, Any]] = []
        for row in rows:
            channel_text = str(row.get("通道", row.get("channel", ""))).strip().upper()
            match = re.fullmatch(r"CH?(\d+)", channel_text)
            if not match:
                raise ValueError(f"无法识别旧数据库通道：{channel_text or '空值'}")
            items.append(
                {
                    "channel": int(match.group(1)),
                    "purpose": row.get("系数用途", row.get("purpose", "")),
                    "gain": row.get("通道增益", row.get("gain")),
                    "offset": row.get("通道漂移", row.get("offset")),
                }
            )
        return self.replace(items)


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    try:
        channel = int(item["channel"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("校准项缺少有效 channel") from exc
    purpose = str(item.get("purpose", "")).strip()
    if len(purpose) > 120:
        raise ValueError("系数用途不能超过 120 个字符")
    return {
        "channel": channel,
        "purpose": purpose,
        "gain": _finite_number(item.get("gain"), "gain"),
        "offset": _finite_number(item.get("offset"), "offset"),
    }


def _finite_number(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{name} 必须是有限数字")
    return number


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
