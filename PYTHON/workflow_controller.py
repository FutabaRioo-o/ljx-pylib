"""Safe automatic-workflow coordinator for the integrated HMI.

The PCB protocol inherited from ``PC_MAIN`` only exposes relative commands
for the two push-rod motors and does not report their positions or completion
acks.  This module therefore keeps *estimated* B/C positions in software and
never presents them as measured feedback.  It also keeps every automatic
operation cancellable: an explicit stop always sends the A-axis stop command
and turns both external relays off.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from main_controller import ControllerError, MainController


class WorkflowError(RuntimeError):
    """A workflow request cannot be run safely in the current state."""


@dataclass(frozen=True)
class WorkflowConfig:
    startup_wait_seconds: float = 1.0
    a_target_position: int = 180
    a_speed: int = 30000
    b_scan_steps: int = 0
    c_scan_steps: int = 0
    b_stress_steps: int = 0
    c_stress_steps: int = 0
    b_speed: int = 8000
    c_speed: int = 8000

    @classmethod
    def from_mapping(cls, value: dict[str, Any], motion: dict[str, Any]) -> "WorkflowConfig":
        def integer(key: str, default: int, minimum: int, maximum: int) -> int:
            raw = value.get(key, default)
            if isinstance(raw, bool):
                raise WorkflowError(f"工作流配置 {key} 必须是整数")
            try:
                result = int(raw)
            except (TypeError, ValueError) as exc:
                raise WorkflowError(f"工作流配置 {key} 必须是整数") from exc
            if not minimum <= result <= maximum:
                raise WorkflowError(f"工作流配置 {key} 必须在 {minimum}～{maximum} 之间")
            return result

        try:
            wait = float(value.get("startup_wait_seconds", 1.0))
        except (TypeError, ValueError) as exc:
            raise WorkflowError("工作流配置 startup_wait_seconds 必须是数字") from exc
        if not 0 <= wait <= 60:
            raise WorkflowError("工作流配置 startup_wait_seconds 必须在 0～60 秒之间")
        return cls(
            startup_wait_seconds=wait,
            a_target_position=integer("a_target_position", 180, -2147483648, 2147483647),
            a_speed=integer("a_speed", 30000, 0, 65535),
            b_scan_steps=integer("b_scan_steps", 0, -65535, 65535),
            c_scan_steps=integer("c_scan_steps", 0, -65535, 65535),
            b_stress_steps=integer("b_stress_steps", 0, -65535, 65535),
            c_stress_steps=integer("c_stress_steps", 0, -65535, 65535),
            b_speed=integer("b_speed", motion.get("b_default_speed", 8000), 0, 65535),
            c_speed=integer("c_speed", motion.get("c_default_speed", 8000), 0, 65535),
        )


@dataclass
class WorkflowState:
    phase: str = "idle"
    run_id: int = 0
    last_error: str | None = None
    started_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None
    estimated_a_position: int | None = None
    estimated_b_steps: int = 0
    estimated_c_steps: int = 0
    active_task: dict[str, int | float | str] | None = None
    events: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=80))


class WorkflowController:
    """Run scan and stress-elimination tasks from existing HMI controls."""

    def __init__(
        self,
        controller: MainController,
        config: WorkflowConfig,
        *,
        on_scan_start: Callable[[dict[str, int | float | str]], Any] | None = None,
        on_stop: Callable[[], Any] | None = None,
    ) -> None:
        self.controller = controller
        self.config = config
        self._on_scan_start = on_scan_start
        self._on_stop = on_stop
        self._lock = threading.RLock()
        self._state = WorkflowState()

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = asdict(self._state)
            state["events"] = list(self._state.events)
            state["position_note"] = "B/C 为软件估算脉冲位置，PCB 旧协议未提供实际位置反馈"
            return state

    def preflight(self) -> dict[str, Any]:
        self._ensure_controller_connected()
        with self._lock:
            if self._state.phase in {"scan_waiting", "scan_positioning", "scanning", "stress_positioning", "stress_running"}:
                raise WorkflowError("自动任务正在运行，不能重新自检")
            self._state.phase = "ready"
            self._state.last_error = None
            self._state.updated_at = _now()
            self._event_locked("info", "整机控制器在线，自动任务可以启动")
        return self.status()

    def start_scan(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        parameters = self._scan_parameters(payload or {})
        self._ensure_controller_connected()
        run_id = self._begin("scan_waiting", parameters, "已接受扫描任务，等待启动联锁")
        threading.Thread(
            target=self._run_scan,
            args=(run_id, parameters),
            name=f"integrated-scan-{run_id}",
            daemon=True,
        ).start()
        return self.status()

    def start_stress(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        parameters = self._stress_parameters(payload or {})
        self._ensure_controller_connected()
        run_id = self._begin("stress_positioning", parameters, "已接受应力消除任务，正在定位双枪")
        threading.Thread(
            target=self._run_stress,
            args=(run_id, parameters),
            name=f"integrated-stress-{run_id}",
            daemon=True,
        ).start()
        return self.status()

    def complete_scan(self) -> dict[str, Any]:
        with self._lock:
            if self._state.phase != "scanning":
                return self.status()
            self._state.phase = "scan_complete"
            self._state.completed_at = _now()
            self._state.updated_at = self._state.completed_at
            self._event_locked("info", "扫描采集已结束；可进入应力消除或复测")
        return self.status()

    def safe_stop(self, reason: str = "操作员停止") -> dict[str, Any]:
        with self._lock:
            self._state.run_id += 1  # cancels all waiting/background workers
            self._state.phase = "stopping"
            self._state.updated_at = _now()
            self._event_locked("warning", reason)

        errors: list[str] = []
        try:
            self.controller.safe_stop()
        except (ControllerError, ValueError) as exc:
            errors.append(str(exc))
        if self._on_stop is not None:
            try:
                self._on_stop()
            except Exception as exc:  # stopping must continue even if laser exit fails
                errors.append(f"激光停止失败：{exc}")

        with self._lock:
            # The stop request succeeded for every component that was
            # reachable.  Keep a stopped state even if an already-offline PCB
            # could not acknowledge its frame, so Pi-side laser stopping is
            # never reported as a failed safety action.
            self._state.phase = "stopped"
            self._state.last_error = "; ".join(errors) if errors else None
            self._state.completed_at = _now()
            self._state.updated_at = self._state.completed_at
            self._event_locked("warning" if not errors else "error", "安全停机完成" if not errors else self._state.last_error)
        return self.status()

    def _run_scan(self, run_id: int, parameters: dict[str, int | float | str]) -> None:
        try:
            wait_seconds = float(parameters["startup_wait_seconds"])
            if wait_seconds and not self._wait_until_ready(run_id, wait_seconds):
                return
            if not self._transition(run_id, "scan_positioning", "开始执行扫描定位"):
                return
            self._move_push_rods(run_id, parameters, "b_scan_steps", "c_scan_steps")
            if not self._still_active(run_id):
                return
            self._set_rs485_speed_and_position(run_id, parameters)
            if not self._still_active(run_id):
                return
            if self._on_scan_start is not None:
                self._on_scan_start(parameters)
            self._transition(run_id, "scanning", "扫描运动与激光采集已启动")
        except Exception as exc:
            self._fail(run_id, f"扫描任务失败：{exc}")

    def _run_stress(self, run_id: int, parameters: dict[str, int | float | str]) -> None:
        try:
            self._move_push_rods(run_id, parameters, "b_stress_steps", "c_stress_steps")
            if not self._still_active(run_id):
                return
            self.controller.set_relays(True, True)
            self._transition(run_id, "stress_running", "双路超声冲击枪已开启")
        except Exception as exc:
            self._fail(run_id, f"应力消除任务失败：{exc}")

    def _set_rs485_speed_and_position(self, run_id: int, parameters: dict[str, int | float | str]) -> None:
        if not self._still_active(run_id):
            return
        self.controller.set_rs485_speed(int(parameters["a_speed"]))
        if not self._still_active(run_id):
            return
        position = int(parameters["a_target_position"])
        self.controller.move_rs485_position(position)
        with self._lock:
            if self._state.run_id == run_id:
                self._state.estimated_a_position = position
                self._state.updated_at = _now()

    def _move_push_rods(
        self,
        run_id: int,
        parameters: dict[str, int | float | str],
        b_key: str,
        c_key: str,
    ) -> None:
        values = ((1, b_key, "b_speed", "estimated_b_steps"), (2, c_key, "c_speed", "estimated_c_steps"))
        for motor, steps_key, speed_key, state_key in values:
            if not self._still_active(run_id):
                return
            steps = int(parameters[steps_key])
            if not steps:
                continue
            self.controller.move_stepper(motor, steps, int(parameters[speed_key]))
            with self._lock:
                if self._state.run_id == run_id:
                    setattr(self._state, state_key, getattr(self._state, state_key) + steps)
                    self._state.updated_at = _now()

    def _begin(self, phase: str, parameters: dict[str, int | float | str], message: str) -> int:
        with self._lock:
            if self._state.phase in {"scan_waiting", "scan_positioning", "scanning", "stress_positioning", "stress_running", "stopping"}:
                raise WorkflowError("已有自动任务正在运行；请先使用现有的正常停止或急停按钮")
            self._state.run_id += 1
            self._state.phase = phase
            self._state.last_error = None
            self._state.started_at = _now()
            self._state.completed_at = None
            self._state.updated_at = self._state.started_at
            self._state.active_task = dict(parameters)
            self._event_locked("info", message)
            return self._state.run_id

    def _scan_parameters(self, payload: dict[str, Any]) -> dict[str, int | float | str]:
        return self._parameters(
            payload,
            {
                "task": "scan",
                "startup_wait_seconds": self.config.startup_wait_seconds,
                "a_target_position": self.config.a_target_position,
                "a_speed": self.config.a_speed,
                "b_scan_steps": self.config.b_scan_steps,
                "c_scan_steps": self.config.c_scan_steps,
                "b_speed": self.config.b_speed,
                "c_speed": self.config.c_speed,
            },
        )

    def _stress_parameters(self, payload: dict[str, Any]) -> dict[str, int | float | str]:
        return self._parameters(
            payload,
            {
                "task": "stress",
                "b_stress_steps": self.config.b_stress_steps,
                "c_stress_steps": self.config.c_stress_steps,
                "b_speed": self.config.b_speed,
                "c_speed": self.config.c_speed,
            },
        )

    @staticmethod
    def _parameters(payload: dict[str, Any], defaults: dict[str, int | float | str]) -> dict[str, int | float | str]:
        values = dict(defaults)
        for key, default in defaults.items():
            if key == "task" or key not in payload:
                continue
            raw = payload[key]
            if isinstance(default, float):
                try:
                    value = float(raw)
                except (TypeError, ValueError) as exc:
                    raise WorkflowError(f"{key} 必须是数字") from exc
                if not 0 <= value <= 60:
                    raise WorkflowError(f"{key} 必须在 0～60 之间")
            else:
                if isinstance(raw, bool):
                    raise WorkflowError(f"{key} 必须是整数")
                try:
                    value = int(raw)
                except (TypeError, ValueError) as exc:
                    raise WorkflowError(f"{key} 必须是整数") from exc
                if key in {"a_speed", "b_speed", "c_speed"} and not 0 <= value <= 65535:
                    raise WorkflowError(f"{key} 必须在 0～65535 之间")
                if key.endswith("steps") and not -65535 <= value <= 65535:
                    raise WorkflowError(f"{key} 必须在 -65535～65535 之间")
                if key == "a_target_position" and not -2147483648 <= value <= 2147483647:
                    raise WorkflowError("a_target_position 超出范围")
            values[key] = value
        return values

    def _ensure_controller_connected(self) -> None:
        if not self.controller.status().get("connected"):
            raise WorkflowError("PCB 控制器未连接；请先使用现有的“连接设备”按钮")

    def _wait_until_ready(self, run_id: int, seconds: float) -> bool:
        deadline = threading.Event()
        # Wait in short chunks so a normal-stop click cancels pending motion.
        remaining = seconds
        while remaining > 0:
            interval = min(remaining, 0.1)
            deadline.wait(interval)
            if not self._still_active(run_id):
                return False
            remaining -= interval
        return True

    def _still_active(self, run_id: int) -> bool:
        with self._lock:
            return self._state.run_id == run_id and self._state.phase not in {"stopping", "stopped", "fault"}

    def _transition(self, run_id: int, phase: str, message: str) -> bool:
        with self._lock:
            if self._state.run_id != run_id or self._state.phase in {"stopping", "stopped", "fault"}:
                return False
            self._state.phase = phase
            self._state.updated_at = _now()
            self._event_locked("info", message)
            return True

    def _fail(self, run_id: int, message: str) -> None:
        with self._lock:
            if self._state.run_id != run_id:
                return
            self._state.phase = "fault"
            self._state.last_error = message
            self._state.completed_at = _now()
            self._state.updated_at = self._state.completed_at
            self._event_locked("error", message)
        try:
            self.controller.safe_stop()
        except (ControllerError, ValueError):
            pass

    def _event_locked(self, level: str, message: str) -> None:
        self._state.events.appendleft({"time": _now(), "level": level, "message": message})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
