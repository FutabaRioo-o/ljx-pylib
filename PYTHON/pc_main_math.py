"""Math helpers migrated from PC_MAIN's Class_Math and Class_Linear."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence


def crc16_modbus(data: bytes, *, exclude_trailing_bytes: int = 0) -> int:
    """Return the Modbus/IBM CRC-16 used by the legacy utility class."""
    if exclude_trailing_bytes < 0 or exclude_trailing_bytes > len(data):
        raise ValueError("exclude_trailing_bytes 超出数据范围")
    crc = 0xFFFF
    payload = data[: len(data) - exclude_trailing_bytes or None]
    for byte in payload:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc & 0xFFFF


def bytes_to_hex(data: bytes) -> str:
    return data.hex().upper()


def hex_to_bits(value: str) -> str:
    try:
        return "".join(f"{int(char, 16):04b}" for char in value)
    except ValueError as exc:
        raise ValueError("输入不是十六进制字符串") from exc


def is_unsigned_number(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9]+", value or ""))


def signed_24bit(value: int | float) -> int | float:
    return value - 0x1000000 if value >= 0x800000 else value


def linear_fit(x: Sequence[float], y: Sequence[float]) -> tuple[float, float]:
    if len(x) != len(y) or not x:
        raise ValueError("x 和 y 必须等长且非空")
    count = len(x)
    x_sum = sum(x)
    y_sum = sum(y)
    xy_sum = sum(left * right for left, right in zip(x, y))
    x2_sum = sum(value * value for value in x)
    denominator = count * x2_sum - x_sum * x_sum
    if math.isclose(denominator, 0.0, abs_tol=1e-12):
        raise ValueError("无法拟合：x 没有足够变化")
    slope = (count * xy_sum - x_sum * y_sum) / denominator
    return slope, (y_sum - slope * x_sum) / count


def linear_values(x: Sequence[float], slope: float, intercept: float) -> list[float]:
    return [slope * value + intercept for value in x]


def correlation_coefficient(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("两组数据必须等长且非空")
    count = len(left)
    left_sum = sum(left)
    right_sum = sum(right)
    sum_x = sum((count * value - left_sum) ** 2 for value in left)
    sum_y = sum((count * value - right_sum) ** 2 for value in right)
    if math.isclose(sum_x, 0.0) or math.isclose(sum_y, 0.0):
        raise ValueError("常量序列没有定义相关系数")
    sum_xy = sum(
        (count * x_value - left_sum) * (count * y_value - right_sum)
        for x_value, y_value in zip(left, right)
    )
    return abs(sum_xy) / math.sqrt(sum_x * sum_y)
