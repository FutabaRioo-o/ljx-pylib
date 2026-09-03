import csv
import ctypes
import sys
from datetime import datetime
from pathlib import Path

import LJXAwrap


DEVICE_ID = 0
CONTROLLER_IP = [192, 168, 0, 1]
COMMAND_PORT = 24691

# 为兼容不同测头，按最多 3200 点并包含亮度数据预留空间。
MAX_PROFILE_POINTS = 3200
HEADER_WORDS = ctypes.sizeof(
    LJXAwrap.LJX8IF_PROFILE_HEADER
) // ctypes.sizeof(ctypes.c_uint)

FOOTER_WORDS = ctypes.sizeof(
    LJXAwrap.LJX8IF_PROFILE_FOOTER
) // ctypes.sizeof(ctypes.c_uint)

MAX_BUFFER_WORDS = (
    HEADER_WORDS
    + MAX_PROFILE_POINTS * 2
    + FOOTER_WORDS
)

INVALID_VALUES = {
    -2147483648: "无效数据",
    -2147483647: "无效数据",
    -2147483646: "死角数据",
    -2147483645: "判断待机数据",
}


def code_text(result):
    return f"0x{result & 0xFFFF:04X}"


def to_signed32(value):
    """将无符号 32 位数据解释为有符号 32 位数据。"""
    return ctypes.c_int32(value).value


def raw_to_mm(value):
    """
    原始高度和坐标单位为 0.01 μm。
    1 mm = 100000 × 0.01 μm。
    """
    return value / 100000.0


def main():
    config = LJXAwrap.LJX8IF_ETHERNET_CONFIG()

    for index, value in enumerate(CONTROLLER_IP):
        config.abyIpAddress[index] = value

    config.wPortNo = COMMAND_PORT

    connected = False

    print("正在连接 LJ-X8000A……")

    result = LJXAwrap.LJX8IF_EthernetOpen(
        DEVICE_ID,
        config
    )

    if result != 0:
        print(
            "连接失败，返回码：",
            code_text(result)
        )
        return 1

    connected = True
    print("连接成功")

    try:
        request = LJXAwrap.LJX8IF_GET_PROFILE_REQUEST()

        # 0：活动区域，即当前程序使用的存储区域
        request.byTargetBank = 0

        # 0：从当前最新轮廓开始读取
        request.byPositionMode = 0

        # 当前模式下此编号不会使用
        request.dwGetProfileNo = 0

        # 只读取一条轮廓
        request.byGetProfileCount = 1

        # 0：读取后不删除控制器中的数据
        request.byErase = 0

        response = LJXAwrap.LJX8IF_GET_PROFILE_RESPONSE()
        profile_info = LJXAwrap.LJX8IF_PROFILE_INFO()

        profile_buffer = (
            ctypes.c_int * MAX_BUFFER_WORDS
        )()

        buffer_size_bytes = ctypes.sizeof(
            profile_buffer
        )

        print("正在读取当前单条轮廓……")

        result = LJXAwrap.LJX8IF_GetProfile(
            DEVICE_ID,
            request,
            response,
            profile_info,
            profile_buffer,
            buffer_size_bytes
        )

        if result != 0:
            print(
                "轮廓读取失败，返回码：",
                code_text(result)
            )

            if (result & 0xFFFF) == 0x80A0:
                print(
                    "说明：控制器中目前没有可读取的轮廓数据。"
                )

            if (result & 0xFFFF) == 0x8081:
                print(
                    "说明：当前控制器启用了批处理测量，"
                    "需要改用批处理轮廓读取函数。"
                )

            return 1

        profile_count = response.byGetProfileCount
        point_count = profile_info.wProfileDataCount
        luminance_enabled = (
            profile_info.byLuminanceOutput == 1
        )

        print("轮廓读取成功")
        print(f"  实际读取轮廓数：{profile_count}")
        print(f"  当前最新轮廓号：{response.dwCurrentProfileNo}")
        print(f"  本次读取轮廓号：{response.dwGetTopProfileNo}")
        print(f"  高度数据点数：{point_count}")
        print(
            "  是否包含亮度数据："
            + ("是" if luminance_enabled else "否")
        )
        print(
            f"  X起点：{raw_to_mm(profile_info.lXStart):.6f} mm"
        )
        print(
            f"  X点间距：{raw_to_mm(profile_info.lXPitch):.6f} mm"
        )

        if profile_count < 1:
            print("控制器返回的轮廓数量为 0。")
            return 1

        if point_count < 1 or point_count > MAX_PROFILE_POINTS:
            print(
                f"异常的数据点数：{point_count}"
            )
            return 1

        # 标题占 6 个 32 位数据。
        trigger_count = profile_buffer[1]
        encoder_count = to_signed32(
            profile_buffer[2]
        )

        print(f"  触发计数：{trigger_count}")
        print(f"  编码器计数：{encoder_count}")

        height_start = HEADER_WORDS
        luminance_start = height_start + point_count

        timestamp = datetime.now().strftime(
            "%Y%m%d_%H%M%S"
        )

        output_dir = Path.home() / "Desktop" / "LJX轮廓数据"
        output_dir.mkdir(parents=True, exist_ok=True)

        output_path = output_dir / f"profile_{timestamp}.csv"

        valid_heights = []
        valid_count = 0
        invalid_count = 0

        with output_path.open(
            "w",
            newline="",
            encoding="utf-8-sig"
        ) as csv_file:
            writer = csv.writer(csv_file)

            writer.writerow([
                "point_index",
                "x_raw_0.01um",
                "x_mm",
                "height_raw_0.01um",
                "height_mm",
                "status",
                "luminance"
            ])

            for index in range(point_count):
                x_raw = (
                    profile_info.lXStart
                    + index * profile_info.lXPitch
                )

                height_raw = to_signed32(
                    profile_buffer[
                        height_start + index
                    ]
                )

                status = INVALID_VALUES.get(
                    height_raw,
                    "有效"
                )

                if status == "有效":
                    height_mm = raw_to_mm(
                        height_raw
                    )
                    valid_heights.append(
                        height_mm
                    )
                    valid_count += 1
                else:
                    height_mm = ""
                    invalid_count += 1

                if luminance_enabled:
                    luminance = profile_buffer[
                        luminance_start + index
                    ]
                else:
                    luminance = ""

                writer.writerow([
                    index,
                    x_raw,
                    f"{raw_to_mm(x_raw):.6f}",
                    height_raw,
                    (
                        f"{height_mm:.6f}"
                        if height_mm != ""
                        else ""
                    ),
                    status,
                    luminance
                ])

        print()
        print("数据统计：")
        print(f"  有效点：{valid_count}")
        print(f"  无效点：{invalid_count}")

        if valid_heights:
            print(
                f"  最低高度：{min(valid_heights):.6f} mm"
            )
            print(
                f"  最高高度：{max(valid_heights):.6f} mm"
            )
            print(
                "  高度范围："
                f"{max(valid_heights) - min(valid_heights):.6f} mm"
            )
        else:
            print("  没有读取到有效高度点")

        print()
        print(
            "CSV 已保存：",
            output_path.resolve()
        )

        return 0

    except Exception as error:
        print(
            "程序运行异常：",
            repr(error)
        )
        return 1

    finally:
        if connected:
            close_result = (
                LJXAwrap.LJX8IF_CommunicationClose(
                    DEVICE_ID
                )
            )

            print(
                "断开控制器：",
                (
                    "成功"
                    if close_result == 0
                    else "失败，返回码 "
                    + code_text(close_result)
                )
            )


if __name__ == "__main__":
    sys.exit(main())
