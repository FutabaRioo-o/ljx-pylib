import argparse
import csv
import ctypes
import json
import statistics
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import LJXAwrap


DEFAULT_DEVICE_ID = 0
DEFAULT_CONTROLLER_IP = "192.168.0.1"
DEFAULT_COMMAND_PORT = 24691
DEFAULT_HIGH_SPEED_PORT = 24692
DEFAULT_OUTPUT_DIR = Path.home() / "Desktop" / "LJX_moving_profiles"
SCRIPT_VERSION = "2026-07-19.5"

# Observed when the controller is already producing profiles and rejects a
# second batch-start request. In this case high-speed receiving can continue.
START_MEASURE_ALREADY_RUNNING_CODES = {0x8080}

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

INVALID_PROFILE_VALUES = {
    -2147483648: "invalid",
    -2147483647: "invalid",
    -2147483646: "dead_zone",
    -2147483645: "judgment_standby",
}


class ZUnitUnavailableError(RuntimeError):
    pass


def code_text(result):
    return f"0x{result & 0xFFFF:04X}"


def to_signed32(value):
    return ctypes.c_int32(value).value


def raw_001um_to_mm(value):
    return value / 100000.0


def parse_ip(ip_text):
    parts = ip_text.split(".")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("IP must have 4 octets")

    values = []
    for part in parts:
        try:
            value = int(part)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"Invalid IP octet: {part}"
            ) from exc

        if value < 0 or value > 255:
            raise argparse.ArgumentTypeError(
                f"IP octet out of range: {part}"
            )

        values.append(value)

    return values


def make_ethernet_config(ip_values, command_port):
    config = LJXAwrap.LJX8IF_ETHERNET_CONFIG()

    for index, value in enumerate(ip_values):
        config.abyIpAddress[index] = value

    config.wPortNo = command_port
    return config


def make_output_dir(base_dir):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(base_dir) / f"moving_profiles_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def get_z_unit(device_id):
    z_unit = ctypes.c_ushort()
    result = LJXAwrap.LJX8IF_GetZUnitSimpleArray(device_id, z_unit)
    if result != 0:
        raise ZUnitUnavailableError(
            "LJX8IF_GetZUnitSimpleArray failed: "
            + code_text(result)
        )

    return z_unit.value


def make_record(
    profile_index,
    captured_at,
    height_format,
    heights_raw,
    luminance_raw,
    x_start_raw,
    x_pitch_raw,
    luminance_enabled,
    trigger_count=None,
    encoder_count=None,
    profile_no=None,
    z_unit_raw=None,
):
    return {
        "profile_index": profile_index,
        "profile_no": profile_no,
        "captured_at": captured_at,
        "trigger_count": trigger_count,
        "encoder_count": encoder_count,
        "height_format": height_format,
        "heights_raw": heights_raw,
        "luminance_raw": luminance_raw,
        "x_start_raw": int(x_start_raw),
        "x_pitch_raw": int(x_pitch_raw),
        "luminance_enabled": bool(luminance_enabled),
        "z_unit_raw": z_unit_raw,
    }


def height_status_and_mm(record, raw_value):
    if record["height_format"] == "simple_array_u16":
        if raw_value == 0:
            return "invalid", None

        z_unit_raw = record["z_unit_raw"]
        if z_unit_raw is None:
            raise ValueError("z_unit_raw is required for simple array data")

        height_mm = (int(raw_value) - 32768) * z_unit_raw / 100000.0
        return "valid", height_mm

    signed_value = to_signed32(raw_value)
    status = INVALID_PROFILE_VALUES.get(signed_value, "valid")
    if status != "valid":
        return status, None

    return "valid", raw_001um_to_mm(signed_value)


def record_height_values(record):
    heights = []

    for raw_value in record["heights_raw"]:
        status, height_mm = height_status_and_mm(record, raw_value)
        if status == "valid":
            heights.append(height_mm)

    return heights


def summarize_record(record):
    heights = record_height_values(record)
    point_count = len(record["heights_raw"])
    valid_count = len(heights)
    invalid_count = point_count - valid_count

    if not heights:
        return {
            "valid_points": 0,
            "invalid_points": invalid_count,
            "min_height_mm": "",
            "max_height_mm": "",
            "height_range_mm": "",
            "mean_height_mm": "",
            "median_height_mm": "",
        }

    return {
        "valid_points": valid_count,
        "invalid_points": invalid_count,
        "min_height_mm": f"{min(heights):.6f}",
        "max_height_mm": f"{max(heights):.6f}",
        "height_range_mm": f"{max(heights) - min(heights):.6f}",
        "mean_height_mm": f"{statistics.mean(heights):.6f}",
        "median_height_mm": f"{statistics.median(heights):.6f}",
    }


class HighSpeedCollector:
    def __init__(
        self,
        target_profile_count,
        profile_info,
        z_unit_raw,
        sample_interval,
    ):
        self.target_profile_count = target_profile_count
        self.profile_info = profile_info
        self.z_unit_raw = z_unit_raw
        self.sample_interval = sample_interval
        self.next_sample_at = None
        self.received_profile_count = 0
        self.skipped_profile_count = 0
        self.records = []
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.error = None
        self.notify_counts = {}

    def callback(
        self,
        p_header,
        p_height,
        p_luminance,
        luminance_enable,
        xpoint_count,
        profile_count,
        notify,
        user,
    ):
        try:
            self.notify_counts[notify] = self.notify_counts.get(notify, 0) + 1

            if notify not in (0, 0x10000):
                return None

            profile_count = int(profile_count)
            if profile_count == 0:
                return None

            with self.lock:
                self.received_profile_count += profile_count
                remaining = self.target_profile_count - len(self.records)

                if self.sample_interval > 0:
                    callback_at = time.monotonic()
                    if (
                        self.next_sample_at is not None
                        and callback_at < self.next_sample_at
                    ):
                        self.skipped_profile_count += profile_count
                        return None

                    self.next_sample_at = callback_at + self.sample_interval
                    profile_offsets = [profile_count - 1]
                    self.skipped_profile_count += profile_count - 1
                else:
                    copy_count = min(profile_count, remaining)
                    profile_offsets = range(copy_count)

            if remaining <= 0:
                self.done.set()
                return None

            xpoint_count = int(xpoint_count)
            new_records = []

            for offset_profile in profile_offsets:
                start = offset_profile * xpoint_count
                captured_at = datetime.now().isoformat(timespec="milliseconds")

                heights_raw = [
                    int(p_height[start + point_index])
                    for point_index in range(xpoint_count)
                ]

                if luminance_enable == 1:
                    luminance_raw = [
                        int(p_luminance[start + point_index])
                        for point_index in range(xpoint_count)
                    ]
                else:
                    luminance_raw = []

                header = p_header[offset_profile]

                new_records.append({
                    "captured_at": captured_at,
                    "trigger_count": int(header.dwTriggerCount),
                    "encoder_count": int(header.lEncoderCount),
                    "heights_raw": heights_raw,
                    "luminance_raw": luminance_raw,
                })

            with self.lock:
                for item in new_records:
                    profile_index = len(self.records) + 1
                    self.records.append(
                        make_record(
                            profile_index=profile_index,
                            captured_at=item["captured_at"],
                            height_format="simple_array_u16",
                            heights_raw=item["heights_raw"],
                            luminance_raw=item["luminance_raw"],
                            x_start_raw=self.profile_info.lXStart,
                            x_pitch_raw=self.profile_info.lXPitch,
                            luminance_enabled=luminance_enable == 1,
                            trigger_count=item["trigger_count"],
                            encoder_count=item["encoder_count"],
                            z_unit_raw=self.z_unit_raw,
                        )
                    )

                if len(self.records) >= self.target_profile_count:
                    self.done.set()

        except Exception as exc:
            self.error = repr(exc)
            self.done.set()

        return None

    def count(self):
        with self.lock:
            return len(self.records)

    def snapshot(self):
        with self.lock:
            return list(self.records)

    def sampling_stats(self):
        with self.lock:
            return {
                "profiles_received_from_controller": self.received_profile_count,
                "profiles_skipped_by_sampling": self.skipped_profile_count,
            }


def read_profiles_high_speed(args, config):
    connected = False
    high_speed_initialized = False
    high_speed_started = False
    measure_started = False
    callback_func = None
    collector = None

    print("Opening command connection...")
    result = LJXAwrap.LJX8IF_EthernetOpen(args.device_id, config)
    if result != 0:
        raise RuntimeError("LJX8IF_EthernetOpen failed: " + code_text(result))

    connected = True

    try:
        pre_start_req = LJXAwrap.LJX8IF_HIGH_SPEED_PRE_START_REQ()
        pre_start_req.bySendPosition = args.send_position

        profile_info = LJXAwrap.LJX8IF_PROFILE_INFO()

        print("Preparing high-speed communication...")

        collector = HighSpeedCollector(
            args.profiles,
            profile_info,
            None,
            args.sample_interval,
        )
        callback_func = LJXAwrap.LJX8IF_CALLBACK_SIMPLE_ARRAY(
            collector.callback
        )

        profiles_per_callback = min(args.callback_profiles, args.profiles)

        result = LJXAwrap.LJX8IF_InitializeHighSpeedDataCommunicationSimpleArray(
            args.device_id,
            config,
            args.high_speed_port,
            callback_func,
            profiles_per_callback,
            0,
        )
        if result != 0:
            raise RuntimeError(
                "LJX8IF_InitializeHighSpeedDataCommunicationSimpleArray "
                "failed: "
                + code_text(result)
            )

        high_speed_initialized = True

        result = LJXAwrap.LJX8IF_PreStartHighSpeedDataCommunication(
            args.device_id,
            pre_start_req,
            profile_info,
        )
        if result != 0:
            raise RuntimeError(
                "LJX8IF_PreStartHighSpeedDataCommunication failed: "
                + code_text(result)
            )

        if profile_info.wProfileDataCount < 1:
            raise RuntimeError("Controller returned zero X points")

        if profile_info.wProfileDataCount > MAX_PROFILE_POINTS:
            raise RuntimeError(
                "X point count exceeds local limit: "
                + str(profile_info.wProfileDataCount)
            )

        z_unit_raw = get_z_unit(args.device_id)
        collector.z_unit_raw = z_unit_raw

        print(
            "Profile info: "
            f"x_points={profile_info.wProfileDataCount}, "
            f"luminance={profile_info.byLuminanceOutput}, "
            f"x_start_mm={raw_001um_to_mm(profile_info.lXStart):.6f}, "
            f"x_pitch_mm={raw_001um_to_mm(profile_info.lXPitch):.6f}, "
            f"z_unit_raw={z_unit_raw}"
        )

        result = LJXAwrap.LJX8IF_StartHighSpeedDataCommunication(
            args.device_id
        )
        if result != 0:
            raise RuntimeError(
                "LJX8IF_StartHighSpeedDataCommunication failed: "
                + code_text(result)
            )

        high_speed_started = True

        if not args.external_start:
            result = LJXAwrap.LJX8IF_StartMeasure(args.device_id)
            result_code = result & 0xFFFF
            if result == 0:
                measure_started = True
                print("Measurement started by this script.")
            elif result_code in START_MEASURE_ALREADY_RUNNING_CODES:
                print(
                    "Controller is already measuring; "
                    "continuing without restarting it."
                )
            else:
                raise RuntimeError(
                    "LJX8IF_StartMeasure failed: " + code_text(result)
                )
        else:
            print("Using measurement already started outside this script.")

        print(
            f"Collecting {args.profiles} profiles "
            f"(timeout {args.timeout:.1f}s)..."
        )
        if args.sample_interval > 0:
            minimum_duration = (args.profiles - 1) * args.sample_interval
            print(
                "Saving at most one profile every "
                f"{args.sample_interval:.3f}s "
                f"(~{1.0 / args.sample_interval:.2f} profiles/s)."
            )
            print(
                "Expected minimum collection time: "
                f"{minimum_duration:.1f}s"
            )
        print("Collection stops at the profile target or at the timeout.")

        deadline = time.monotonic() + args.timeout
        last_printed = -1

        while not collector.done.is_set():
            current_count = collector.count()
            should_print = (
                args.print_every > 0
                and current_count != last_printed
                and (
                    current_count == args.profiles
                    or (current_count == 0 and last_printed < 0)
                    or (
                        current_count > 0
                        and current_count % args.print_every == 0
                    )
                )
            )

            if should_print:
                print(f"  received {current_count}/{args.profiles}")
                last_printed = current_count

            if time.monotonic() >= deadline:
                break

            time.sleep(0.01)

        records = collector.snapshot()

        if len(records) >= args.profiles:
            print(f"Profile target reached: {len(records)}/{args.profiles}")
        else:
            print(
                "Collection timeout reached: "
                f"{len(records)}/{args.profiles} profiles received"
            )

        if collector.error:
            raise RuntimeError("Callback failed: " + collector.error)

        if not records:
            raise RuntimeError("No profiles were received before timeout")

        if len(records) < args.profiles:
            print(
                "Warning: requested "
                f"{args.profiles}, received {len(records)} before timeout"
            )

        return records, {
            "mode": "high-speed",
            "x_points": int(profile_info.wProfileDataCount),
            "luminance_enabled": bool(profile_info.byLuminanceOutput == 1),
            "x_start_raw_0.01um": int(profile_info.lXStart),
            "x_pitch_raw_0.01um": int(profile_info.lXPitch),
            "x_start_mm": raw_001um_to_mm(profile_info.lXStart),
            "x_pitch_mm": raw_001um_to_mm(profile_info.lXPitch),
            "z_unit_raw": int(z_unit_raw),
            "z_unit_mm_per_count": z_unit_raw / 100000.0,
            "sample_interval_seconds": args.sample_interval,
            "notify_counts": collector.notify_counts,
            **collector.sampling_stats(),
        }

    finally:
        if measure_started and not args.leave_measure_running:
            result = LJXAwrap.LJX8IF_StopMeasure(args.device_id)
            print("LJX8IF_StopMeasure:", code_text(result))

        if high_speed_started:
            result = LJXAwrap.LJX8IF_StopHighSpeedDataCommunication(
                args.device_id
            )
            print(
                "LJX8IF_StopHighSpeedDataCommunication:",
                code_text(result),
            )

        if high_speed_initialized:
            result = LJXAwrap.LJX8IF_FinalizeHighSpeedDataCommunication(
                args.device_id
            )
            print(
                "LJX8IF_FinalizeHighSpeedDataCommunication:",
                code_text(result),
            )

        if connected:
            result = LJXAwrap.LJX8IF_CommunicationClose(args.device_id)
            print("LJX8IF_CommunicationClose:", code_text(result))

        callback_func = None


def read_current_profile_once(args):
    request = LJXAwrap.LJX8IF_GET_PROFILE_REQUEST()
    request.byTargetBank = 0
    request.byPositionMode = 0
    request.dwGetProfileNo = 0
    request.byGetProfileCount = 1
    request.byErase = 0

    response = LJXAwrap.LJX8IF_GET_PROFILE_RESPONSE()
    profile_info = LJXAwrap.LJX8IF_PROFILE_INFO()
    profile_buffer = (ctypes.c_int * MAX_BUFFER_WORDS)()

    result = LJXAwrap.LJX8IF_GetProfile(
        args.device_id,
        request,
        response,
        profile_info,
        profile_buffer,
        ctypes.sizeof(profile_buffer),
    )
    if result != 0:
        return result, None

    if response.byGetProfileCount < 1:
        return 0, None

    point_count = int(profile_info.wProfileDataCount)
    if point_count < 1 or point_count > MAX_PROFILE_POINTS:
        raise RuntimeError("Unexpected X point count: " + str(point_count))

    height_start = HEADER_WORDS
    luminance_start = height_start + point_count
    luminance_enabled = profile_info.byLuminanceOutput == 1

    heights_raw = [
        to_signed32(profile_buffer[height_start + point_index])
        for point_index in range(point_count)
    ]

    if luminance_enabled:
        luminance_raw = [
            int(profile_buffer[luminance_start + point_index])
            for point_index in range(point_count)
        ]
    else:
        luminance_raw = []

    record = make_record(
        profile_index=0,
        profile_no=int(response.dwGetTopProfileNo),
        captured_at=datetime.now().isoformat(timespec="milliseconds"),
        height_format="profile_i32_0.01um",
        heights_raw=heights_raw,
        luminance_raw=luminance_raw,
        x_start_raw=profile_info.lXStart,
        x_pitch_raw=profile_info.lXPitch,
        luminance_enabled=luminance_enabled,
        trigger_count=int(profile_buffer[1]),
        encoder_count=to_signed32(profile_buffer[2]),
    )

    metadata = {
        "x_points": point_count,
        "luminance_enabled": bool(luminance_enabled),
        "x_start_raw_0.01um": int(profile_info.lXStart),
        "x_pitch_raw_0.01um": int(profile_info.lXPitch),
        "x_start_mm": raw_001um_to_mm(profile_info.lXStart),
        "x_pitch_mm": raw_001um_to_mm(profile_info.lXPitch),
        "current_profile_no": int(response.dwCurrentProfileNo),
        "oldest_profile_no": int(response.dwOldestProfileNo),
        "get_top_profile_no": int(response.dwGetTopProfileNo),
    }

    return 0, (record, metadata)


def read_profiles_polling(args, config):
    print("Opening command connection...")
    result = LJXAwrap.LJX8IF_EthernetOpen(args.device_id, config)
    if result != 0:
        raise RuntimeError("LJX8IF_EthernetOpen failed: " + code_text(result))

    records = []
    last_profile_no = None
    duplicate_count = 0
    poll_interval = max(args.poll_interval, args.sample_interval)
    metadata = {
        "mode": "poll",
        "poll_interval_seconds": poll_interval,
        "sample_interval_seconds": args.sample_interval,
    }

    try:
        print(
            f"Polling latest profile until {args.profiles} unique samples "
            f"are saved..."
        )

        deadline = time.monotonic() + args.timeout

        while len(records) < args.profiles:
            if time.monotonic() >= deadline:
                break

            result, payload = read_current_profile_once(args)

            if result != 0:
                if (result & 0xFFFF) == 0x80A0:
                    time.sleep(poll_interval)
                    continue

                raise RuntimeError(
                    "LJX8IF_GetProfile failed: " + code_text(result)
                )

            if payload is None:
                time.sleep(poll_interval)
                continue

            record, latest_metadata = payload
            metadata.update(latest_metadata)

            profile_no = record["profile_no"]
            if (
                not args.allow_duplicates
                and profile_no == last_profile_no
            ):
                duplicate_count += 1
                time.sleep(poll_interval)
                continue

            record["profile_index"] = len(records) + 1
            records.append(record)
            last_profile_no = profile_no

            if (
                args.print_every > 0
                and (
                    len(records) == args.profiles
                    or len(records) % args.print_every == 0
                )
            ):
                print(f"  saved {len(records)}/{args.profiles}")

            time.sleep(poll_interval)

    finally:
        result = LJXAwrap.LJX8IF_CommunicationClose(args.device_id)
        print("LJX8IF_CommunicationClose:", code_text(result))

    if not records:
        raise RuntimeError("No profiles were saved before timeout")

    if len(records) < args.profiles:
        print(
            "Warning: requested "
            f"{args.profiles}, saved {len(records)} before timeout"
        )

    metadata["duplicates_skipped"] = duplicate_count
    return records, metadata


def format_float(value):
    if value is None:
        return ""

    return f"{value:.6f}"


def write_points_csv(records, output_path):
    fieldnames = [
        "profile_index",
        "profile_no",
        "captured_at",
        "trigger_count",
        "encoder_count",
        "point_index",
        "x_raw_0.01um",
        "x_mm",
        "height_raw_value",
        "height_raw_format",
        "height_mm",
        "status",
        "luminance",
    ]

    with output_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()

        for record in records:
            for point_index, raw_value in enumerate(record["heights_raw"]):
                x_raw = record["x_start_raw"] + point_index * record["x_pitch_raw"]
                status, height_mm = height_status_and_mm(record, raw_value)

                if record["luminance_enabled"]:
                    luminance = record["luminance_raw"][point_index]
                else:
                    luminance = ""

                writer.writerow({
                    "profile_index": record["profile_index"],
                    "profile_no": (
                        ""
                        if record["profile_no"] is None
                        else record["profile_no"]
                    ),
                    "captured_at": record["captured_at"],
                    "trigger_count": record["trigger_count"],
                    "encoder_count": record["encoder_count"],
                    "point_index": point_index,
                    "x_raw_0.01um": x_raw,
                    "x_mm": f"{raw_001um_to_mm(x_raw):.6f}",
                    "height_raw_value": raw_value,
                    "height_raw_format": record["height_format"],
                    "height_mm": format_float(height_mm),
                    "status": status,
                    "luminance": luminance,
                })


def write_summary_csv(records, output_path):
    fieldnames = [
        "profile_index",
        "profile_no",
        "captured_at",
        "trigger_count",
        "encoder_count",
        "point_count",
        "valid_points",
        "invalid_points",
        "min_height_mm",
        "max_height_mm",
        "height_range_mm",
        "mean_height_mm",
        "median_height_mm",
    ]

    with output_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()

        for record in records:
            summary = summarize_record(record)
            writer.writerow({
                "profile_index": record["profile_index"],
                "profile_no": (
                    ""
                    if record["profile_no"] is None
                    else record["profile_no"]
                ),
                "captured_at": record["captured_at"],
                "trigger_count": record["trigger_count"],
                "encoder_count": record["encoder_count"],
                "point_count": len(record["heights_raw"]),
                **summary,
            })


def write_individual_profile_csvs(records, output_dir):
    individual_dir = output_dir / "individual_profiles"
    individual_dir.mkdir(parents=True, exist_ok=True)

    for record in records:
        file_path = individual_dir / (
            f"profile_{record['profile_index']:06d}.csv"
        )
        write_points_csv([record], file_path)

    return individual_dir


def write_metadata(metadata, args, output_path):
    payload = dict(metadata)
    payload.update({
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "requested_profiles": args.profiles,
        "timeout_seconds": args.timeout,
        "sample_interval_seconds": args.sample_interval,
        "device_id": args.device_id,
        "controller_ip": args.ip,
        "command_port": args.command_port,
        "high_speed_port": args.high_speed_port,
    })

    with output_path.open("w", encoding="utf-8") as json_file:
        json.dump(payload, json_file, indent=2, sort_keys=True)


def write_outputs(records, metadata, args):
    output_dir = make_output_dir(args.output_dir)
    points_path = output_dir / "profiles_points.csv"
    summary_path = output_dir / "profiles_summary.csv"
    metadata_path = output_dir / "metadata.json"

    write_points_csv(records, points_path)
    write_summary_csv(records, summary_path)
    write_metadata(metadata, args, metadata_path)

    individual_dir = None
    if args.save_individual:
        individual_dir = write_individual_profile_csvs(records, output_dir)

    return {
        "output_dir": output_dir,
        "points": points_path,
        "summary": summary_path,
        "metadata": metadata_path,
        "individual_dir": individual_dir,
    }


def print_run_summary(records):
    total_points = sum(len(record["heights_raw"]) for record in records)
    total_valid = 0
    all_heights = []

    for record in records:
        heights = record_height_values(record)
        total_valid += len(heights)
        all_heights.extend(heights)

    print()
    print("Collection summary")
    print(f"  profiles: {len(records)}")
    print(f"  points: {total_points}")
    print(f"  valid points: {total_valid}")
    print(f"  invalid points: {total_points - total_valid}")

    if all_heights:
        print(f"  min height: {min(all_heights):.6f} mm")
        print(f"  max height: {max(all_heights):.6f} mm")
        print(f"  height range: {max(all_heights) - min(all_heights):.6f} mm")


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Read multiple LJ-X8000A profiles while the target is moving."
        )
    )
    parser.add_argument(
        "--mode",
        choices=("auto", "high-speed", "poll"),
        default="auto",
        help=(
            "auto tries high-speed and falls back to GetProfile polling; "
            "high-speed and poll force one acquisition method"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {SCRIPT_VERSION}",
    )
    parser.add_argument(
        "--profiles",
        type=int,
        default=100,
        help="number of profiles to save",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="maximum acquisition time in seconds",
    )
    parser.add_argument(
        "--sample-interval",
        type=float,
        default=0.1,
        help=(
            "minimum seconds between saved profiles; "
            "0 saves every received profile"
        ),
    )
    parser.add_argument(
        "--ip",
        default=DEFAULT_CONTROLLER_IP,
        help="controller IP address",
    )
    parser.add_argument(
        "--command-port",
        type=int,
        default=DEFAULT_COMMAND_PORT,
        help="command communication port",
    )
    parser.add_argument(
        "--high-speed-port",
        type=int,
        default=DEFAULT_HIGH_SPEED_PORT,
        help="high-speed communication port",
    )
    parser.add_argument(
        "--device-id",
        type=int,
        default=DEFAULT_DEVICE_ID,
        help="device ID used by the KEYENCE library",
    )
    parser.add_argument(
        "--callback-profiles",
        type=int,
        default=1,
        help="profiles per high-speed callback",
    )
    parser.add_argument(
        "--send-position",
        type=int,
        default=2,
        help="bySendPosition for high-speed pre-start",
    )
    parser.add_argument(
        "--external-start",
        action="store_true",
        help="do not call LJX8IF_StartMeasure; wait for external start",
    )
    parser.add_argument(
        "--leave-measure-running",
        action="store_true",
        help="do not call LJX8IF_StopMeasure after this script started it",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.05,
        help="seconds between GetProfile calls in poll mode",
    )
    parser.add_argument(
        "--allow-duplicates",
        action="store_true",
        help="poll mode: save repeated profile numbers too",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="base output directory",
    )
    parser.add_argument(
        "--save-individual",
        action="store_true",
        help="also write one CSV per profile",
    )
    parser.add_argument(
        "--print-every",
        type=int,
        default=10,
        help="progress print interval; 0 disables progress lines",
    )
    return parser


def validate_args(args):
    if args.profiles < 1:
        raise ValueError("--profiles must be at least 1")

    if args.timeout <= 0:
        raise ValueError("--timeout must be greater than 0")

    if args.sample_interval < 0:
        raise ValueError("--sample-interval cannot be negative")

    if args.callback_profiles < 1:
        raise ValueError("--callback-profiles must be at least 1")

    if args.poll_interval < 0:
        raise ValueError("--poll-interval cannot be negative")

    parse_ip(args.ip)


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        print(f"read_moving_profiles {SCRIPT_VERSION}")
        validate_args(args)
        ip_values = parse_ip(args.ip)
        config = make_ethernet_config(ip_values, args.command_port)

        if args.mode == "high-speed":
            records, metadata = read_profiles_high_speed(args, config)
        elif args.mode == "poll":
            records, metadata = read_profiles_polling(args, config)
        else:
            try:
                records, metadata = read_profiles_high_speed(args, config)
            except ZUnitUnavailableError as exc:
                print()
                print(
                    "High-speed Z unit is unavailable; "
                    "falling back to GetProfile polling."
                )
                print("  reason:", exc)
                records, metadata = read_profiles_polling(args, config)
                metadata["high_speed_fallback_reason"] = str(exc)

        print_run_summary(records)
        outputs = write_outputs(records, metadata, args)

        print()
        print("Files written")
        print(f"  output dir: {outputs['output_dir'].resolve()}")
        print(f"  points CSV: {outputs['points'].resolve()}")
        print(f"  summary CSV: {outputs['summary'].resolve()}")
        print(f"  metadata: {outputs['metadata'].resolve()}")

        if outputs["individual_dir"] is not None:
            print(
                "  individual profiles: "
                f"{outputs['individual_dir'].resolve()}"
            )

        return 0

    except KeyboardInterrupt:
        print()
        print("Interrupted by user")
        return 130

    except Exception as exc:
        print("Error:", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
