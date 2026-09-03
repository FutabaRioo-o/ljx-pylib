#!/usr/bin/env python3
"""Idempotently add the Raspberry Pi HMI bridge reference to the bundled HTML."""

from pathlib import Path


HTML_PATH = Path(__file__).resolve().with_name("一体化上位机界面V3.html")
BRIDGE_TAG = '<script src="./hmi_bridge.js"></script>'
BUTTON_REPLACEMENTS = {
    '<el-button>单帧读取</el-button>': '<el-button id="laser-single-read">单帧读取</el-button>',
    '<el-button type="primary">连续采集</el-button>': '<el-button id="laser-continuous-start" type="primary">连续采集</el-button>',
    '<el-button type="warning">停止采集</el-button>': '<el-button id="laser-continuous-stop" type="warning">停止采集</el-button>',
}


def main() -> int:
    html = HTML_PATH.read_text(encoding="utf-8")
    for original, replacement in BUTTON_REPLACEMENTS.items():
        if replacement in html:
            continue
        if original not in html:
            raise RuntimeError(f"HTML 中没有找到待绑定按钮：{original}")
        html = html.replace(original, replacement, 1)

    # Bundled libraries may contain literal </body> strings. Remove any older
    # insertion and target the final closing body tag of the real document.
    html = html.replace(BRIDGE_TAG, "")
    insertion_index = html.rfind("</body>")
    if insertion_index < 0:
        raise RuntimeError("HTML 中没有找到 </body>，无法插入绑定脚本")
    html = html[:insertion_index] + BRIDGE_TAG + html[insertion_index:]
    HTML_PATH.write_text(html, encoding="utf-8")
    print(f"Embedded: {HTML_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
