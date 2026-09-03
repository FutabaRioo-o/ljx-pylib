import ctypes
import threading
from datetime import datetime

from flask import Flask, render_template_string

import LJXAwrap


DEVICE_ID = 0
CONTROLLER_IP = [192, 168, 0, 1]
COMMAND_PORT = 24691

app = Flask(__name__)

# 官方通信库不支持多线程同时调用，因此使用锁保护
communication_lock = threading.Lock()


def result_code_text(result):
    return f"0x{result & 0xFFFF:04X}"


def temperature_text(value):
    # LJ-V 系列不支持的项目可能返回 0xFFFF，在 c_short 中为 -1
    if value == -1:
        return "不支持"
    return f"{value / 100:.2f} ℃"


def decode_text(buffer):
    return buffer.raw.rstrip(b"\x00 ").decode(
        "ascii",
        errors="replace"
    )


def read_controller_status():
    status = {
        "online": False,
        "model": "未读取",
        "sensor_temperature": "未读取",
        "processor_temperature": "未读取",
        "case_temperature": "未读取",
        "program": "未读取",
        "system_errors": [],
        "message": "",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    with communication_lock:
        config = LJXAwrap.LJX8IF_ETHERNET_CONFIG()

        for index, value in enumerate(CONTROLLER_IP):
            config.abyIpAddress[index] = value

        config.wPortNo = COMMAND_PORT

        result = LJXAwrap.LJX8IF_EthernetOpen(
            DEVICE_ID,
            config
        )

        if result != 0:
            status["message"] = (
                "连接控制器失败，返回码 "
                + result_code_text(result)
            )
            return status

        status["online"] = True

        try:
            # 读取感测头型号
            head_model = ctypes.create_string_buffer(32)

            result = LJXAwrap.LJX8IF_GetHeadModel(
                DEVICE_ID,
                head_model
            )

            if result == 0:
                status["model"] = decode_text(head_model)
            else:
                status["model"] = (
                    "读取失败 " + result_code_text(result)
                )

            # 读取感测头温度
            sensor_temperature = ctypes.c_short()
            processor_temperature = ctypes.c_short()
            case_temperature = ctypes.c_short()

            result = LJXAwrap.LJX8IF_GetHeadTemperature(
                DEVICE_ID,
                sensor_temperature,
                processor_temperature,
                case_temperature
            )

            if result == 0:
                status["sensor_temperature"] = temperature_text(
                    sensor_temperature.value
                )
                status["processor_temperature"] = temperature_text(
                    processor_temperature.value
                )
                status["case_temperature"] = temperature_text(
                    case_temperature.value
                )
            else:
                error_text = (
                    "读取失败 " + result_code_text(result)
                )
                status["sensor_temperature"] = error_text
                status["processor_temperature"] = error_text
                status["case_temperature"] = error_text

            # 读取当前程序号
            active_program = ctypes.c_ubyte()

            result = LJXAwrap.LJX8IF_GetActiveProgram(
                DEVICE_ID,
                active_program
            )

            if result == 0:
                status["program"] = str(active_program.value)
            else:
                status["program"] = (
                    "读取失败 " + result_code_text(result)
                )

            # 读取系统错误
            maximum_errors = 10
            error_count = ctypes.c_ubyte()
            error_codes = (
                ctypes.c_ushort * maximum_errors
            )()

            result = LJXAwrap.LJX8IF_GetError(
                DEVICE_ID,
                maximum_errors,
                error_count,
                error_codes
            )

            if result == 0:
                status["system_errors"] = [
                    f"0x{error_codes[index]:04X}"
                    for index in range(error_count.value)
                ]
            else:
                status["system_errors"] = [
                    "读取失败 " + result_code_text(result)
                ]

            status["message"] = "控制器状态读取成功"

        except Exception as error:
            status["message"] = f"程序异常：{error}"

        finally:
            LJXAwrap.LJX8IF_CommunicationClose(DEVICE_ID)

    return status


PAGE = """
<!doctype html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport"
          content="width=device-width, initial-scale=1">
    <title>LJ-X8000A 控制面板</title>

    <style>
        body {
            margin: 0;
            padding: 24px;
            background: #f3f5f7;
            color: #202124;
            font-family:
                Arial,
                "Microsoft YaHei",
                sans-serif;
        }

        .panel {
            max-width: 680px;
            margin: 30px auto;
            background: white;
            border-radius: 14px;
            padding: 28px;
            box-shadow: 0 8px 30px rgba(0, 0, 0, 0.08);
        }

        h1 {
            margin-top: 0;
            font-size: 26px;
        }

        .subtitle {
            color: #666;
            margin-bottom: 24px;
        }

        .status-row {
            display: flex;
            justify-content: space-between;
            gap: 20px;
            padding: 14px 0;
            border-bottom: 1px solid #eeeeee;
        }

        .label {
            color: #666;
        }

        .value {
            font-weight: 600;
            text-align: right;
        }

        .online {
            color: #138a36;
        }

        .offline {
            color: #c62828;
        }

        .dot {
            display: inline-block;
            width: 10px;
            height: 10px;
            margin-right: 8px;
            border-radius: 50%;
            background: currentColor;
        }

        button {
            width: 100%;
            margin-top: 24px;
            padding: 13px;
            border: 0;
            border-radius: 9px;
            font-size: 16px;
            cursor: pointer;
            background: #1a73e8;
            color: white;
        }

        button:hover {
            opacity: 0.9;
        }

        .message {
            margin-top: 18px;
            padding: 12px;
            border-radius: 8px;
            background: #f6f8fa;
        }

        .time {
            margin-top: 14px;
            color: #777;
            font-size: 13px;
            text-align: right;
        }
    </style>
</head>

<body>
<div class="panel">
    <h1>LJ-X8000A 激光控制器</h1>

    <div class="subtitle">
        树莓派 CM5 只读状态面板 v0.1
    </div>

    <div class="status-row">
        <span class="label">连接状态</span>

        {% if status.online %}
        <span class="value online">
            <span class="dot"></span>在线
        </span>
        {% else %}
        <span class="value offline">
            <span class="dot"></span>离线
        </span>
        {% endif %}
    </div>

    <div class="status-row">
        <span class="label">感测头型号</span>
        <span class="value">{{ status.model }}</span>
    </div>

    <div class="status-row">
        <span class="label">CMOS 温度</span>
        <span class="value">
            {{ status.sensor_temperature }}
        </span>
    </div>

    <div class="status-row">
        <span class="label">处理器温度</span>
        <span class="value">
            {{ status.processor_temperature }}
        </span>
    </div>

    <div class="status-row">
        <span class="label">外壳温度</span>
        <span class="value">
            {{ status.case_temperature }}
        </span>
    </div>

    <div class="status-row">
        <span class="label">当前程序号</span>
        <span class="value">{{ status.program }}</span>
    </div>

    <div class="status-row">
        <span class="label">系统错误</span>

        <span class="value">
        {% if status.system_errors %}
            {{ status.system_errors | join("、") }}
        {% else %}
            无
        {% endif %}
        </span>
    </div>

    <form method="get">
        <button type="submit">刷新控制器状态</button>
    </form>

    <div class="message">
        {{ status.message }}
    </div>

    <div class="time">
        最后刷新：{{ status.updated_at }}
    </div>
</div>
</body>
</html>
"""


@app.route("/")
def index():
    status = read_controller_status()
    return render_template_string(
        PAGE,
        status=status
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
        threaded=False
    )
