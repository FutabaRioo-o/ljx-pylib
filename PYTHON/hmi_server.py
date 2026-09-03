#!/usr/bin/env python3
"""Raspberry Pi local web server for the integrated PCB HMI.

The server keeps the browser UI independent from the vendor library process:
- read_ljx_status.py supplies the laser controller health check.
- read_one_profile.py supplies single-frame acquisition.
- read_moving_profiles.py runs as a managed background acquisition process.
- main_controller.py supplies the migrated PC_MAIN motion and relay control.

Run on the Raspberry Pi:
    python3 hmi_server.py
Then open:
    http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import atexit
import csv
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request

from calibration_store import CalibrationStore
from integrated_config import load_config, public_config
from main_controller import ControllerConfig, ControllerError, MainController
from workflow_controller import WorkflowConfig, WorkflowController, WorkflowError


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = SCRIPT_DIR.parents[1]
HTML_FILENAME = "一体化上位机界面V3.html"
DEFAULT_HTML_PATH = next(
    (candidate for candidate in (SCRIPT_DIR / HTML_FILENAME, WORKSPACE_DIR / HTML_FILENAME) if candidate.exists()),
    SCRIPT_DIR / HTML_FILENAME,
)
BRIDGE_PATH = SCRIPT_DIR / "hmi_bridge.js"

STATUS_SCRIPT = SCRIPT_DIR / "read_ljx_status.py"
ONE_PROFILE_SCRIPT = SCRIPT_DIR / "read_one_profile.py"
MOVING_PROFILES_SCRIPT = SCRIPT_DIR / "read_moving_profiles.py"

DATA_ROOT = Path(
    os.environ.get(
        "LJX_HMI_DATA_DIR",
        str(Path.home() / "Desktop" / "LJX上位机数据"),
    )
).expanduser().resolve()
SINGLE_PROFILE_DIR = Path.home() / "Desktop" / "LJX轮廓数据"
APP_CONFIG = load_config()
MAIN_CONTROLLER_CONFIG = ControllerConfig.from_mapping(APP_CONFIG["main_controller"])
MAIN_CONTROLLER = MainController(MAIN_CONTROLLER_CONFIG)
CALIBRATION_STORE = CalibrationStore(SCRIPT_DIR / "data" / "integrated_hmi.sqlite3")


def _workflow_start_laser(_: dict[str, int | float | str]) -> dict[str, Any]:
    """Start Pi-side laser capture only after PCB positioning was accepted."""
    workflow_settings = APP_CONFIG["workflow"]
    result, status_code = start_laser_acquisition(
        {
            "profiles": workflow_settings["scan_profiles"],
            "timeout": workflow_settings["scan_timeout_seconds"],
            "sample_interval": workflow_settings["scan_sample_interval_seconds"],
        }
    )
    if status_code >= 400 or not result.get("ok"):
        raise WorkflowError(result.get("error", "无法启动激光扫描"))
    return result


def _workflow_stop_laser() -> bool:
    """Resolve the laser-stop helper lazily; it is defined below the process code."""
    return stop_acquisition_process()


WORKFLOW_CONFIG = WorkflowConfig.from_mapping(APP_CONFIG["workflow"], APP_CONFIG["motion"])
WORKFLOW = WorkflowController(
    MAIN_CONTROLLER,
    WORKFLOW_CONFIG,
    on_scan_start=_workflow_start_laser,
    on_stop=_workflow_stop_laser,
)

app = Flask(__name__)
command_lock = threading.Lock()
state_lock = threading.Lock()

html_path = Path(os.environ.get("LJX_HMI_HTML", DEFAULT_HTML_PATH)).expanduser().resolve()
status_cache: dict[str, Any] = {"checked_at_monotonic": 0.0, "payload": None}
acquisition: dict[str, Any] = {
    "process": None,
    "status": "idle",
    "requested": 0,
    "received": 0,
    "started_at": None,
    "finished_at": None,
    "output_dir": None,
    "error": None,
    "stop_requested": False,
    "log_tail": deque(maxlen=40),
}


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def completed_command(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=SCRIPT_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def output_tail(process: subprocess.CompletedProcess[str], lines: int = 12) -> str:
    combined = "\n".join(part for part in (process.stdout, process.stderr) if part)
    return "\n".join(combined.strip().splitlines()[-lines:])


def parse_status_output(text: str) -> dict[str, Any]:
    model_match = re.search(r"型号[^：:]*[：:]\s*([^\r\n]+)", text)
    temperatures = re.findall(r"(-?\d+(?:\.\d+)?)\s*(?:°C|℃)", text)
    error_section = text.split("系统错误", 1)[-1] if "系统错误" in text else ""
    error_codes = sorted(set(re.findall(r"0x(?!0000)[0-9A-Fa-f]{4}", error_section)))
    return {
        "model": model_match.group(1).strip() if model_match else "LJ-X8000A",
        "sensor_temperature": float(temperatures[0]) if temperatures else None,
        "processor_temperature": float(temperatures[1]) if len(temperatures) > 1 else None,
        "case_temperature": float(temperatures[2]) if len(temperatures) > 2 else None,
        "error_codes": error_codes,
    }


def acquisition_public_state() -> dict[str, Any]:
    with state_lock:
        return {
            "status": acquisition["status"],
            "running": acquisition["status"] == "running",
            "requested": acquisition["requested"],
            "received": acquisition["received"],
            "started_at": acquisition["started_at"],
            "finished_at": acquisition["finished_at"],
            "output_dir": acquisition["output_dir"],
            "error": acquisition["error"],
            "log_tail": list(acquisition["log_tail"]),
        }


def read_laser_status(force: bool = False) -> dict[str, Any]:
    running = acquisition_public_state()["running"]
    if running:
        return {
            "ok": True,
            "online": True,
            "status": "acquiring",
            "model": "LJ-X8000A",
            "error_code": "—",
            "message": "连续采集中",
            "checked_at": iso_now(),
        }

    with state_lock:
        cached = status_cache["payload"]
        cache_age = time.monotonic() - status_cache["checked_at_monotonic"]
    if cached is not None and cache_age < 2.0 and not force:
        return cached

    with command_lock:
        try:
            process = completed_command([sys.executable, str(STATUS_SCRIPT)], timeout=8.0)
            online = process.returncode == 0
            details = parse_status_output(process.stdout)
            payload = {
                "ok": online,
                "online": online,
                "status": "online" if online else "offline",
                "model": details["model"] if online else "未检测到",
                "sensor_temperature": details["sensor_temperature"],
                "processor_temperature": details["processor_temperature"],
                "case_temperature": details["case_temperature"],
                "system_errors": details["error_codes"],
                "error_code": "、".join(details["error_codes"]) if details["error_codes"] else ("—" if online else "LJX_OFFLINE"),
                "message": "激光控制器在线" if online else (output_tail(process) or "未检测到激光控制器"),
                "checked_at": iso_now(),
            }
        except subprocess.TimeoutExpired:
            payload = {
                "ok": False,
                "online": False,
                "status": "timeout",
                "model": "未检测到",
                "error_code": "LJX_TIMEOUT",
                "message": "激光控制器状态检测超时",
                "checked_at": iso_now(),
            }
        except Exception as exc:  # keep UI alive when the vendor library is unavailable
            payload = {
                "ok": False,
                "online": False,
                "status": "error",
                "model": "未检测到",
                "error_code": "LJX_STATUS_ERROR",
                "message": str(exc),
                "checked_at": iso_now(),
            }

    with state_lock:
        status_cache["payload"] = payload
        status_cache["checked_at_monotonic"] = time.monotonic()
    return payload


def newest_file(directory: Path, pattern: str, newer_than: float) -> Path | None:
    if not directory.exists():
        return None
    candidates = [path for path in directory.glob(pattern) if path.is_file() and path.stat().st_mtime >= newer_than - 1]
    return max(candidates, key=lambda path: path.stat().st_mtime, default=None)


def summarize_profile_csv(csv_path: Path) -> dict[str, Any]:
    point_count = 0
    valid_count = 0
    heights: list[float] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            point_count += 1
            value = (row.get("height_mm") or "").strip()
            if value:
                try:
                    heights.append(float(value))
                    valid_count += 1
                except ValueError:
                    pass
    return {
        "point_count": point_count,
        "valid_count": valid_count,
        "invalid_count": point_count - valid_count,
        "min_height_mm": min(heights) if heights else None,
        "max_height_mm": max(heights) if heights else None,
    }


def monitor_acquisition(process: subprocess.Popen[str], existing_dirs: set[Path]) -> None:
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.rstrip()
            if not line:
                continue
            with state_lock:
                acquisition["log_tail"].append(line)
                match = re.search(r"(?:received|saved)\s+(\d+)/(\d+)", line, flags=re.IGNORECASE)
                if match:
                    acquisition["received"] = int(match.group(1))

        return_code = process.wait()
        output_dirs = set(DATA_ROOT.glob("moving_profiles_*")) if DATA_ROOT.exists() else set()
        new_dirs = [path for path in output_dirs - existing_dirs if path.is_dir()]
        output_dir = max(new_dirs, key=lambda path: path.stat().st_mtime, default=None)

        with state_lock:
            stop_requested = acquisition["stop_requested"]
            acquisition["process"] = None
            acquisition["finished_at"] = iso_now()
            acquisition["output_dir"] = str(output_dir) if output_dir else None
            if stop_requested:
                acquisition["status"] = "stopped"
                acquisition["error"] = None
            elif return_code == 0:
                acquisition["status"] = "succeeded"
                acquisition["received"] = acquisition["requested"]
                acquisition["error"] = None
            else:
                acquisition["status"] = "failed"
                acquisition["error"] = "\n".join(list(acquisition["log_tail"])[-10:]) or f"采集进程退出码 {return_code}"
    finally:
        if command_lock.locked():
            command_lock.release()


def stop_acquisition_process() -> bool:
    with state_lock:
        process: subprocess.Popen[str] | None = acquisition["process"]
        if process is None or process.poll() is not None:
            return False
        acquisition["stop_requested"] = True

    try:
        if os.name != "nt":
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    return True


@app.after_request
def disable_browser_cache(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.get("/")
def index() -> Response:
    if not html_path.exists():
        return Response(f"找不到前端文件：{html_path}", status=500, content_type="text/plain; charset=utf-8")
    html = html_path.read_text(encoding="utf-8")
    if "hmi_bridge.js" not in html and "hmi-bridge.js" not in html:
        bridge_tag = '<script src="/hmi_bridge.js"></script>'
        html = html.replace("</body>", f"{bridge_tag}</body>")
    return Response(html, content_type="text/html; charset=utf-8")


@app.get("/hmi-bridge.js")
@app.get("/hmi_bridge.js")
def bridge_script() -> Response:
    if not BRIDGE_PATH.exists():
        return Response("console.error('hmi_bridge.js missing')", status=500, content_type="application/javascript")
    return Response(BRIDGE_PATH.read_text(encoding="utf-8"), content_type="application/javascript; charset=utf-8")


@app.get("/api/health")
def health() -> Response:
    return jsonify(
        ok=True,
        service="integrated-hmi",
        html=str(html_path),
        time=iso_now(),
        main_controller_enabled=MAIN_CONTROLLER_CONFIG.enabled,
    )


@app.get("/api/system/about")
def system_about() -> Response:
    """Migrates PC_MAIN's About dialog into data for the existing web HMI."""
    return jsonify(
        ok=True,
        product="一体化设备上位机",
        version="2.0.0",
        runtime="Raspberry Pi / Python",
        migrated_from="PC_MAIN_260811",
        modules=["LJ-X8000A 激光轮廓仪", "整机运动控制", "继电器控制", "校准参数管理"],
    )


@app.get("/api/main-controller/config")
def main_controller_config() -> Response:
    return jsonify(ok=True, **public_config(APP_CONFIG))


@app.get("/api/main-controller/status")
def main_controller_status() -> Response:
    return jsonify(ok=True, **MAIN_CONTROLLER.status())


@app.post("/api/main-controller/connect")
def main_controller_connect() -> Response:
    return _main_controller_response(MAIN_CONTROLLER.connect)


@app.post("/api/main-controller/disconnect")
def main_controller_disconnect() -> Response:
    return jsonify(ok=True, **MAIN_CONTROLLER.disconnect())


@app.post("/api/main-controller/counters/reset")
def main_controller_reset_counters() -> Response:
    return jsonify(ok=True, **MAIN_CONTROLLER.clear_counters())


@app.post("/api/main-controller/motors/<int:motor>")
def main_controller_move_stepper(motor: int) -> Response:
    payload = request.get_json(silent=True) or {}
    return _main_controller_response(
        lambda: MAIN_CONTROLLER.move_stepper(motor, payload.get("steps"), payload.get("speed"))
    )


@app.post("/api/main-controller/motors/rs485/position")
def main_controller_move_rs485_position() -> Response:
    payload = request.get_json(silent=True) or {}
    return _main_controller_response(lambda: MAIN_CONTROLLER.move_rs485_position(payload.get("position")))


@app.post("/api/main-controller/motors/rs485/speed")
def main_controller_set_rs485_speed() -> Response:
    payload = request.get_json(silent=True) or {}
    return _main_controller_response(lambda: MAIN_CONTROLLER.set_rs485_speed(payload.get("speed")))


@app.post("/api/main-controller/motors/rs485/run")
def main_controller_set_rs485_running() -> Response:
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload.get("running"), bool):
        return jsonify(ok=False, error="running 必须是布尔值"), 400
    return _main_controller_response(lambda: MAIN_CONTROLLER.set_rs485_running(payload["running"]))


@app.post("/api/main-controller/relays")
def main_controller_set_relays() -> Response:
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload.get("relay_1"), bool) or not isinstance(payload.get("relay_2"), bool):
        return jsonify(ok=False, error="relay_1 和 relay_2 必须是布尔值"), 400
    return _main_controller_response(
        lambda: MAIN_CONTROLLER.set_relays(payload["relay_1"], payload["relay_2"])
    )


@app.post("/api/main-controller/safe-stop")
def main_controller_safe_stop() -> Response:
    return _main_controller_response(MAIN_CONTROLLER.safe_stop)


@app.get("/api/workflow")
def workflow_status() -> Response:
    return jsonify(ok=True, **WORKFLOW.status())


@app.post("/api/workflow/preflight")
def workflow_preflight() -> Response:
    return _workflow_response(WORKFLOW.preflight)


@app.post("/api/workflow/scan/start")
def workflow_scan_start() -> Response:
    payload = request.get_json(silent=True) or {}
    return _workflow_response(lambda: WORKFLOW.start_scan(payload))


@app.post("/api/workflow/scan/complete")
def workflow_scan_complete() -> Response:
    return _workflow_response(WORKFLOW.complete_scan)


@app.post("/api/workflow/stress/start")
def workflow_stress_start() -> Response:
    payload = request.get_json(silent=True) or {}
    return _workflow_response(lambda: WORKFLOW.start_stress(payload))


@app.post("/api/workflow/stop")
def workflow_stop() -> Response:
    payload = request.get_json(silent=True) or {}
    reason = str(payload.get("reason", "操作员停止"))[:80]
    # Stop remains available even when PCB communication has not been
    # configured: it must still terminate any Pi-side laser acquisition.
    return jsonify(ok=True, **WORKFLOW.safe_stop(reason))


@app.get("/api/calibration")
def calibration_list() -> Response:
    return jsonify(ok=True, items=CALIBRATION_STORE.list())


@app.put("/api/calibration")
def calibration_replace() -> Response:
    payload = request.get_json(silent=True) or {}
    items = payload.get("items")
    if not isinstance(items, list):
        return jsonify(ok=False, error="items 必须是校准参数数组"), 400
    try:
        return jsonify(ok=True, items=CALIBRATION_STORE.replace(items))
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400


@app.post("/api/calibration/uniform")
def calibration_set_uniform() -> Response:
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(
            ok=True,
            items=CALIBRATION_STORE.set_uniform(payload.get("gain"), payload.get("offset")),
        )
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400


def _main_controller_response(action: Any) -> tuple[Response, int] | Response:
    if not MAIN_CONTROLLER_CONFIG.enabled:
        return jsonify(
            ok=False,
            error="整机控制器未启用；请在 integrated_hmi_config.json 中填写 Wi-Fi 地址并设置 enabled=true",
        ), 503
    try:
        return jsonify(ok=True, **action())
    except (ControllerError, ValueError) as exc:
        return jsonify(ok=False, error=str(exc)), 400


def _workflow_response(action: Any) -> tuple[Response, int] | Response:
    if not MAIN_CONTROLLER_CONFIG.enabled:
        return jsonify(
            ok=False,
            error="PCB 通信未启用；请在 integrated_hmi_config.json 中填写 PCB 的 Wi-Fi 地址并设置 enabled=true",
        ), 503
    try:
        return jsonify(ok=True, **action())
    except (ControllerError, WorkflowError, ValueError) as exc:
        return jsonify(ok=False, error=str(exc)), 400


@app.get("/api/laser/status")
def laser_status() -> Response:
    force = request.args.get("force") == "1"
    payload = read_laser_status(force=force)
    return jsonify(payload), (200 if payload["online"] else 503)


@app.post("/api/laser/profile/one")
def laser_profile_one() -> Response:
    if acquisition_public_state()["running"]:
        return jsonify(ok=False, error="连续采集正在运行，不能同时读取单帧"), 409

    started_at = time.time()
    with command_lock:
        try:
            process = completed_command([sys.executable, str(ONE_PROFILE_SCRIPT)], timeout=30.0)
        except subprocess.TimeoutExpired:
            return jsonify(ok=False, error="单帧读取超时"), 504
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 500

    if process.returncode != 0:
        return jsonify(ok=False, error=output_tail(process) or "单帧读取失败"), 502

    csv_path = newest_file(SINGLE_PROFILE_DIR, "profile_*.csv", started_at)
    if csv_path is None:
        return jsonify(ok=False, error="读取成功，但未找到新生成的轮廓 CSV 文件"), 500

    summary = summarize_profile_csv(csv_path)
    return jsonify(ok=True, output_file=str(csv_path), read_at=iso_now(), **summary)


def start_laser_acquisition(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Start managed Pi-side laser capture for a manual or automatic task."""
    try:
        profiles = int(payload.get("profiles", 100))
        timeout = float(payload.get("timeout", 15.0))
        sample_interval = float(payload.get("sample_interval", 0.1))
    except (TypeError, ValueError):
        return {"ok": False, "error": "采集参数格式错误"}, 400

    if not 1 <= profiles <= 10000:
        return {"ok": False, "error": "profiles 必须在 1～10000 之间"}, 400
    if not 1 <= timeout <= 3600:
        return {"ok": False, "error": "timeout 必须在 1～3600 秒之间"}, 400
    if not 0 <= sample_interval <= 60:
        return {"ok": False, "error": "sample_interval 必须在 0～60 秒之间"}, 400

    with state_lock:
        current: subprocess.Popen[str] | None = acquisition["process"]
        if current is not None and current.poll() is None:
            return {"ok": False, "error": "连续采集已经在运行"}, 409

    if not command_lock.acquire(blocking=False):
        return {"ok": False, "error": "激光控制器正在执行其他指令，请稍后重试"}, 409

    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    existing_dirs = set(DATA_ROOT.glob("moving_profiles_*"))
    command = [
        sys.executable,
        str(MOVING_PROFILES_SCRIPT),
        "--mode", "auto",
        "--profiles", str(profiles),
        "--timeout", str(timeout),
        "--sample-interval", str(sample_interval),
        "--output-dir", str(DATA_ROOT),
        "--print-every", "1",
    ]

    try:
        process = subprocess.Popen(
            command,
            cwd=SCRIPT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=(os.name != "nt"),
        )
    except Exception as exc:
        command_lock.release()
        return {"ok": False, "error": str(exc)}, 500

    with state_lock:
        acquisition.update({
            "process": process,
            "status": "running",
            "requested": profiles,
            "received": 0,
            "started_at": iso_now(),
            "finished_at": None,
            "output_dir": None,
            "error": None,
            "stop_requested": False,
            "log_tail": deque(maxlen=40),
        })

    threading.Thread(target=monitor_acquisition, args=(process, existing_dirs), daemon=True).start()
    return {"ok": True, **acquisition_public_state()}, 202


@app.post("/api/laser/acquisition/start")
def laser_acquisition_start() -> Response:
    result, status_code = start_laser_acquisition(request.get_json(silent=True) or {})
    return jsonify(result), status_code


@app.get("/api/laser/acquisition")
def laser_acquisition_status() -> Response:
    return jsonify(ok=True, **acquisition_public_state())


@app.post("/api/laser/acquisition/stop")
def laser_acquisition_stop() -> Response:
    stopped = stop_acquisition_process()
    return jsonify(ok=True, stopped=stopped, **acquisition_public_state())


@atexit.register
def shutdown_acquisition() -> None:
    # A service restart must never leave the shock-gun relays energized.
    if MAIN_CONTROLLER.status().get("connected"):
        try:
            MAIN_CONTROLLER.safe_stop()
        except (ControllerError, ValueError):
            pass
    stop_acquisition_process()
    MAIN_CONTROLLER.disconnect()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the integrated LJ-X8000A HMI on Raspberry Pi")
    parser.add_argument(
        "--host",
        default=APP_CONFIG["network"]["hmi_host"],
        help="listen address; default is the configured Wi-Fi LAN address (normally 0.0.0.0)",
    )
    parser.add_argument("--port", type=int, default=APP_CONFIG["network"]["hmi_port"], help="HTTP port")
    parser.add_argument("--html", type=Path, default=html_path, help="path to 一体化上位机界面V3.html")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    html_path = args.html.expanduser().resolve()
    print(f"HMI: http://{args.host}:{args.port}")
    print(f"HTML: {html_path}")
    print(f"Data: {DATA_ROOT}")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
