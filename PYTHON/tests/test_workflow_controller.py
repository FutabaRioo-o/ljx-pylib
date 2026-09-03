from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workflow_controller import WorkflowConfig, WorkflowController


class FakeMainController:
    def __init__(self) -> None:
        self.actions: list[tuple[object, ...]] = []
        self.connected = True

    def status(self) -> dict[str, bool]:
        return {"connected": self.connected}

    def move_stepper(self, motor: int, steps: int, speed: int) -> dict[str, bool]:
        self.actions.append(("stepper", motor, steps, speed))
        return self.status()

    def set_rs485_speed(self, speed: int) -> dict[str, bool]:
        self.actions.append(("rs485_speed", speed))
        return self.status()

    def move_rs485_position(self, position: int) -> dict[str, bool]:
        self.actions.append(("rs485_position", position))
        return self.status()

    def set_relays(self, relay_1: bool, relay_2: bool) -> dict[str, bool]:
        self.actions.append(("relays", relay_1, relay_2))
        return self.status()

    def safe_stop(self) -> dict[str, bool]:
        self.actions.append(("safe_stop",))
        return self.status()


def wait_for_phase(workflow: WorkflowController, expected: str) -> dict[str, object]:
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        state = workflow.status()
        if state["phase"] == expected:
            return state
        time.sleep(0.01)
    raise AssertionError(f"workflow did not reach {expected}: {workflow.status()}")


class WorkflowControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = FakeMainController()
        self.callbacks: list[object] = []
        self.workflow = WorkflowController(
            self.controller,  # type: ignore[arg-type]
            WorkflowConfig(startup_wait_seconds=0, b_speed=7000, c_speed=7100),
            on_scan_start=lambda values: self.callbacks.append(("laser", values["task"])),
            on_stop=lambda: self.callbacks.append("laser_stop"),
        )

    def test_scan_positions_push_rods_before_a_axis_and_starts_laser(self) -> None:
        self.workflow.start_scan(
            {
                "b_scan_steps": 120,
                "c_scan_steps": -80,
                "a_target_position": 180,
                "a_speed": 30000,
            }
        )
        state = wait_for_phase(self.workflow, "scanning")
        self.assertEqual(
            self.controller.actions,
            [
                ("stepper", 1, 120, 7000),
                ("stepper", 2, -80, 7100),
                ("rs485_speed", 30000),
                ("rs485_position", 180),
            ],
        )
        self.assertEqual(self.callbacks, [("laser", "scan")])
        self.assertEqual(state["estimated_b_steps"], 120)
        self.assertEqual(state["estimated_c_steps"], -80)
        self.assertEqual(state["estimated_a_position"], 180)

    def test_stress_enables_both_relays_after_positioning(self) -> None:
        self.workflow.start_stress({"b_stress_steps": 5, "c_stress_steps": 6})
        wait_for_phase(self.workflow, "stress_running")
        self.assertEqual(
            self.controller.actions,
            [("stepper", 1, 5, 7000), ("stepper", 2, 6, 7100), ("relays", True, True)],
        )

    def test_stop_cancels_delayed_scan_and_uses_safe_stop(self) -> None:
        delayed = WorkflowController(
            self.controller,  # type: ignore[arg-type]
            WorkflowConfig(startup_wait_seconds=0.25),
            on_stop=lambda: self.callbacks.append("laser_stop"),
        )
        delayed.start_scan({"b_scan_steps": 9})
        delayed.safe_stop("测试停止")
        time.sleep(0.35)
        self.assertEqual(delayed.status()["phase"], "stopped")
        self.assertEqual(self.controller.actions, [("safe_stop",)])
        self.assertEqual(self.callbacks, ["laser_stop"])


if __name__ == "__main__":
    unittest.main()
