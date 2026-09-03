import ctypes
import sys
import LJXAwrap

DEVICE_ID = 0
IP = [192, 168, 0, 1]
PORT = 24691


def result_text(result):
    if result == 0:
        return "成功"
    return f"失败，返回码 0x{result & 0xFFFF:04X}"


def temperature_text(value):
    # LJ-V 系列不支持的温度项目可能返回 0xFFFF，即 -1
    if value == -1:
        return "不支持 / 无数据"
    return f"{value / 100:.2f} °C"


def main():
    config = LJXAwrap.LJX8IF_ETHERNET_CONFIG()

    for index, value in enumerate(IP):
        config.abyIpAddress[index] = value

    config.wPortNo = PORT

    print("正在连接 LJ-X8000A……")
    result = LJXAwrap.LJX8IF_EthernetOpen(DEVICE_ID, config)
    print("连接控制器：", result_text(result))

    if result != 0:
        sys.exit(1)

    try:
        # 读取感测头型号
        head_model = ctypes.create_string_buffer(32)
        result = LJXAwrap.LJX8IF_GetHeadModel(
            DEVICE_ID,
            head_model
        )

        print("读取感测头型号：", result_text(result))
        if result == 0:
            model = head_model.value.decode(
                "ascii",
                errors="replace"
            )
            print("  型号：", model)

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

        print("读取感测头温度：", result_text(result))
        if result == 0:
            print(
                "  CMOS温度：",
                temperature_text(sensor_temperature.value)
            )
            print(
                "  处理器温度：",
                temperature_text(processor_temperature.value)
            )
            print(
                "  外壳温度：",
                temperature_text(case_temperature.value)
            )

        # 读取当前程序号
        active_program = ctypes.c_ubyte()

        result = LJXAwrap.LJX8IF_GetActiveProgram(
            DEVICE_ID,
            active_program
        )

        print("读取当前程序号：", result_text(result))
        if result == 0:
            print("  当前程序：", active_program.value)

        # 读取系统错误
        maximum_errors = 10
        error_count = ctypes.c_ubyte()
        error_codes = (ctypes.c_ushort * maximum_errors)()

        result = LJXAwrap.LJX8IF_GetError(
            DEVICE_ID,
            maximum_errors,
            error_count,
            error_codes
        )

        print("读取系统错误：", result_text(result))
        if result == 0:
            if error_count.value == 0:
                print("  当前没有系统错误")
            else:
                print("  错误数量：", error_count.value)
                for index in range(error_count.value):
                    print(
                        f"  错误 {index + 1}："
                        f"0x{error_codes[index]:04X}"
                    )

    finally:
        result = LJXAwrap.LJX8IF_CommunicationClose(DEVICE_ID)
        print("断开控制器：", result_text(result))


if __name__ == "__main__":
    main()
