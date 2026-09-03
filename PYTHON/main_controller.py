"""Thread-safe migration of PC_MAIN's TCP control protocol.

The old WinForms program treated each TCP read as a complete packet.  TCP is
a byte stream, so this implementation accumulates bytes and extracts only
valid 16-byte frames.  Commands retain the original wire format while the
web HMI talks to this class through validated JSON endpoints.
"""

from __future__ import annotations

import socket
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from pc_main_math import bytes_to_hex


FRAME_HEADER = 0xAA
FRAME_TAIL = 0xFF
FRAME_LENGTH = 16
TELEMETRY_FUNCTION = 0x01

FUNCTION_RELAY = 0xB1
FUNCTION_MOTOR_1 = 0xB2
FUNCTION_MOTOR_2 = 0xB3
FUNCTION_RS485_POSITION = 0xB4
FUNCTION_RS485_SPEED = 0xB5
FUNCTION_RS485_RUN = 0xB6


class ControllerError(RuntimeError):
    """A recoverable controller configuration or connection failure."""


@dataclass(frozen=True)
class ControllerConfig:
    enabled: bool
    host: str
    port: int
    mode: str = "client"
    connect_timeout_seconds: float = 2.0

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "ControllerConfig":
        try:
            enabled = bool(value.get("enabled", False))
            host = str(value.get("host", "")).strip()
            port = int(value.get("port", 10000))
            mode = str(value.get("mode", "client")).strip().lower()
            timeout = float(value.get("connect_timeout_seconds", 2.0))
        except (TypeError, ValueError) as exc:
            raise ControllerError("整机控制器配置格式错误") from exc
        if mode not in {"client", "server"}:
            raise ControllerError("整机控制器 mode 只能是 client 或 server")
        if not 1 <= port <= 65535:
            raise ControllerError("整机控制器端口必须在 1～65535")
        if not 0.1 <= timeout <= 30:
            raise ControllerError("连接超时必须在 0.1～30 秒")
        return cls(enabled, host, port, mode, timeout)


@dataclass
class Telemetry:
    pitch: float | None = None
    roll: float | None = None
    yaw: float | None = None
    battery_adc: int | None = None
    received_at: str | None = None


@dataclass
class ControllerState:
    enabled: bool
    mode: str
    host: str
    port: int
    connected: bool = False
    listening: bool = False
    tx_bytes: int = 0
    rx_bytes: int = 0
    rx_frames: int = 0
    discarded_bytes: int = 0
    last_error: str | None = None
    last_tx_frame: str | None = None
    last_rx_frame: str | None = None
    relay_1: bool = False
    relay_2: bool = False
    rs485_running: bool = False
    rs485_speed: int | None = None
    telemetry: Telemetry = field(default_factory=Telemetry)


class FrameDecoder:
    """Extract legacy AA...FF fixed-width frames from fragmented TCP data."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.discarded_bytes = 0

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer.extend(data)
        frames: list[bytes] = []
        while self._buffer:
            try:
                frame_start = self._buffer.index(FRAME_HEADER)
            except ValueError:
                self.discarded_bytes += len(self._buffer)
                self._buffer.clear()
                break
            if frame_start:
                self.discarded_bytes += frame_start
                del self._buffer[:frame_start]
            if len(self._buffer) < FRAME_LENGTH:
                break
            candidate = bytes(self._buffer[:FRAME_LENGTH])
            if candidate[-1] != FRAME_TAIL:
                self.discarded_bytes += 1
                del self._buffer[0]
                continue
            frames.append(candidate)
            del self._buffer[:FRAME_LENGTH]
        return frames


def build_frame(function: int, payload: bytes = b"") -> bytes:
    if not 0 <= function <= 0xFF:
        raise ValueError("功能码必须是一个字节")
    if len(payload) > FRAME_LENGTH - 3:
        raise ValueError("参数不能超过 13 个字节")
    frame = bytearray(FRAME_LENGTH)
    frame[0] = FRAME_HEADER
    frame[1] = function
    frame[2 : 2 + len(payload)] = payload
    frame[-1] = FRAME_TAIL
    return bytes(frame)


def decode_telemetry(frame: bytes) -> Telemetry | None:
    """Decode PC_MAIN's upload frame, correcting its C# operator precedence bug."""
    if len(frame) != FRAME_LENGTH or frame[0] != FRAME_HEADER or frame[-1] != FRAME_TAIL:
        raise ValueError("不是完整的旧协议帧")
    if frame[1] != TELEMETRY_FUNCTION:
        return None
    # The original intention is ((high << 8) | low) - 3600, in 0.01 degrees.
    values = [int.from_bytes(frame[index : index + 2], "big") for index in (2, 4, 6)]
    return Telemetry(
        pitch=(values[0] - 3600) * 0.01,
        roll=(values[1] - 3600) * 0.01,
        yaw=(values[2] - 3600) * 0.01,
        battery_adc=frame[8],
        received_at=_now(),
    )


class MainController:
    """One migrated PC_MAIN controller connection.

    Client mode connects to the controller address.  Server mode retains the
    legacy optional listener capability and waits for one controller client.
    Neither mode performs I/O until ``connect()`` is explicitly requested.
    """

    def __init__(self, config: ControllerConfig) -> None:
        self.config = config
        self._state = ControllerState(
            enabled=config.enabled,
            mode=config.mode,
            host=config.host,
            port=config.port,
        )
        self._state_lock = threading.RLock()
        self._connection_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._server_socket: socket.socket | None = None
        self._stop = threading.Event()
        self._decoder = FrameDecoder()
        self._events: deque[dict[str, str]] = deque(maxlen=80)

    def connect(self) -> dict[str, Any]:
        if not self.config.enabled:
            raise ControllerError("整机控制器尚未启用；请在 integrated_hmi_config.json 填写地址后设置 enabled=true")
        with self._connection_lock:
            if self._socket is not None:
                return self.status()
            self._stop.clear()
            if self.config.mode == "server":
                self._start_server()
            else:
                self._connect_client()
        return self.status()

    def disconnect(self) -> dict[str, Any]:
        self._stop.set()
        with self._connection_lock:
            for item in (self._socket, self._server_socket):
                if item is None:
                    continue
                try:
                    item.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    item.close()
                except OSError:
                    pass
            self._socket = None
            self._server_socket = None
            self._set_state(connected=False, listening=False)
            self._event("info", "整机控制器已断开")
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            state = asdict(self._state)
            state["events"] = list(self._events)
        return state

    def move_stepper(self, motor: int, steps: int, speed: int) -> dict[str, Any]:
        if motor not in (1, 2):
            raise ValueError("电机编号只能是 1 或 2")
        steps = _integer(steps, "steps", -65535, 65535, nonzero=True)
        speed = _integer(speed, "speed", 0, 65535)
        direction = 0x01 if steps > 0 else 0x02
        payload = bytes((direction,)) + abs(steps).to_bytes(2, "big") + speed.to_bytes(2, "big")
        return self._send(FUNCTION_MOTOR_1 if motor == 1 else FUNCTION_MOTOR_2, payload)

    def move_rs485_position(self, position: int) -> dict[str, Any]:
        position = _integer(position, "position", -2147483648, 2147483647)
        direction = 0x01 if position > 0 else 0x02 if position < 0 else 0x00
        payload = bytes((direction,)) + position.to_bytes(4, "big", signed=True)
        return self._send(FUNCTION_RS485_POSITION, payload)

    def set_rs485_running(self, running: bool) -> dict[str, Any]:
        self._send(FUNCTION_RS485_RUN, bytes((0x01 if running else 0x00,)))
        self._set_state(rs485_running=running)
        return self.status()

    def set_rs485_speed(self, speed: int) -> dict[str, Any]:
        """Set the A-axis RS485 motor speed before a position command.

        PC_MAIN declares 0xB5 as the RS485 speed function but never exposed
        the corresponding button.  The integration document maps the V1 task
        parameter to this command, using the same big-endian unsigned layout
        used by the two push-rod motor speed fields.
        """
        speed = _integer(speed, "speed", 0, 65535)
        self._send(FUNCTION_RS485_SPEED, speed.to_bytes(2, "big"))
        self._set_state(rs485_speed=speed)
        return self.status()

    def set_relays(self, relay_1: bool, relay_2: bool) -> dict[str, Any]:
        self._send(FUNCTION_RELAY, bytes((int(relay_1), int(relay_2))))
        self._set_state(relay_1=relay_1, relay_2=relay_2)
        return self.status()

    def safe_stop(self) -> dict[str, Any]:
        """Stop A-axis motion and de-energize both external relay outputs."""
        self.set_rs485_running(False)
        self.set_relays(False, False)
        self._event("warning", "已执行整机安全停机：A 轴停止、两路继电器关闭")
        return self.status()

    def clear_counters(self) -> dict[str, Any]:
        with self._state_lock:
            self._state.tx_bytes = 0
            self._state.rx_bytes = 0
            self._state.rx_frames = 0
            self._state.discarded_bytes = 0
            self._decoder.discarded_bytes = 0
        self._event("info", "通信计数已清零")
        return self.status()

    def _connect_client(self) -> None:
        if not self.config.host:
            raise ControllerError("整机控制器 host 为空")
        try:
            connection = socket.create_connection(
                (self.config.host, self.config.port), timeout=self.config.connect_timeout_seconds
            )
        except OSError as exc:
            self._set_state(last_error=str(exc), connected=False)
            raise ControllerError(f"无法连接整机控制器 {self.config.host}:{self.config.port}：{exc}") from exc
        self._attach_connection(connection, f"已连接 {self.config.host}:{self.config.port}")

    def _start_server(self) -> None:
        bind_host = self.config.host or "0.0.0.0"
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((bind_host, self.config.port))
            listener.listen(1)
            listener.settimeout(0.5)
        except OSError as exc:
            self._set_state(last_error=str(exc), listening=False)
            raise ControllerError(f"无法监听整机控制器端口 {bind_host}:{self.config.port}：{exc}") from exc
        self._server_socket = listener
        self._set_state(listening=True, last_error=None)
        self._event("info", f"等待整机控制器连接：{bind_host}:{self.config.port}")
        threading.Thread(target=self._accept_loop, name="main-controller-accept", daemon=True).start()

    def _accept_loop(self) -> None:
        listener = self._server_socket
        if listener is None:
            return
        while not self._stop.is_set():
            try:
                connection, address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._connection_lock:
                if self._socket is not None:
                    connection.close()
                    continue
                self._attach_connection(connection, f"整机控制器已接入：{address[0]}:{address[1]}")
                return

    def _attach_connection(self, connection: socket.socket, message: str) -> None:
        connection.settimeout(0.5)
        self._socket = connection
        self._set_state(connected=True, last_error=None)
        self._event("info", message)
        threading.Thread(target=self._receive_loop, name="main-controller-recv", daemon=True).start()

    def _receive_loop(self) -> None:
        while not self._stop.is_set():
            connection = self._socket
            if connection is None:
                return
            try:
                data = connection.recv(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                self._connection_lost(str(exc))
                return
            if not data:
                self._connection_lost("远端已关闭连接")
                return
            with self._state_lock:
                self._state.rx_bytes += len(data)
            for frame in self._decoder.feed(data):
                self._handle_frame(frame)
            with self._state_lock:
                self._state.discarded_bytes = self._decoder.discarded_bytes

    def _handle_frame(self, frame: bytes) -> None:
        with self._state_lock:
            self._state.rx_frames += 1
            self._state.last_rx_frame = bytes_to_hex(frame)
        telemetry = decode_telemetry(frame)
        if telemetry is not None:
            with self._state_lock:
                self._state.telemetry = telemetry
            self._event("info", "收到姿态与电量数据")

    def _send(self, function: int, payload: bytes) -> dict[str, Any]:
        frame = build_frame(function, payload)
        with self._send_lock:
            connection = self._socket
            if connection is None:
                raise ControllerError("整机控制器未连接")
            try:
                connection.sendall(frame)
            except OSError as exc:
                self._connection_lost(str(exc))
                raise ControllerError(f"整机控制指令发送失败：{exc}") from exc
        with self._state_lock:
            self._state.tx_bytes += len(frame)
            self._state.last_tx_frame = bytes_to_hex(frame)
        self._event("info", f"已发送功能码 0x{function:02X}")
        return self.status()

    def _connection_lost(self, reason: str) -> None:
        with self._connection_lock:
            connection = self._socket
            self._socket = None
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass
            self._set_state(connected=False, last_error=reason)
            self._event("error", f"整机控制器连接断开：{reason}")

    def _set_state(self, **values: Any) -> None:
        with self._state_lock:
            for key, value in values.items():
                setattr(self._state, key, value)

    def _event(self, level: str, message: str) -> None:
        with self._state_lock:
            self._events.appendleft({"time": _now(), "level": level, "message": message})


def _integer(value: Any, name: str, minimum: int, maximum: int, *, nonzero: bool = False) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if number < minimum or number > maximum or (nonzero and number == 0):
        suffix = "且不能为 0" if nonzero else ""
        raise ValueError(f"{name} 必须在 {minimum}～{maximum} {suffix}")
    return number


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
