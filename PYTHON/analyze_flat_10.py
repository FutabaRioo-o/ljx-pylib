import csv
import math
import statistics
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


DATA_DIR = (
    Path.home()
    / "Desktop"
    / "LJX轮廓数据"
    / "桌面平面测试10次"
)

OUTPUT_DIR = DATA_DIR / "分析结果"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def linear_fit(xs, ys):
    """最小二乘拟合直线：y = slope*x + intercept。"""
    mean_x = statistics.mean(xs)
    mean_y = statistics.mean(ys)

    denominator = sum(
        (x - mean_x) ** 2
        for x in xs
    )

    if denominator == 0:
        return 0.0, mean_y

    slope = sum(
        (x - mean_x) * (y - mean_y)
        for x, y in zip(xs, ys)
    ) / denominator

    intercept = mean_y - slope * mean_x
    return slope, intercept


files = sorted(DATA_DIR.glob("profile_*.csv"))

if len(files) != 10:
    raise RuntimeError(
        f"文件夹中应有10个CSV，实际找到 {len(files)} 个。"
    )

summary = []
profiles = []
point_values = {}

plt.figure(figsize=(11, 6))

for number, file_path in enumerate(files, start=1):
    xs = []
    heights = []
    indices = []

    with file_path.open(
        "r",
        encoding="utf-8-sig",
        newline=""
    ) as file:
        reader = csv.DictReader(file)

        for row in reader:
            if row["status"] != "有效":
                continue

            if not row["height_mm"]:
                continue

            point_index = int(row["point_index"])
            x = float(row["x_mm"])
            height = float(row["height_mm"])

            indices.append(point_index)
            xs.append(x)
            heights.append(height)

            point_values.setdefault(
                point_index,
                []
            ).append(height)

    valid_points = len(heights)
    invalid_points = 800 - valid_points

    if valid_points >= 2:
        slope, intercept = linear_fit(xs, heights)

        fitted = [
            slope * x + intercept
            for x in xs
        ]

        residuals = [
            height - fit
            for height, fit in zip(heights, fitted)
        ]

        raw_range = max(heights) - min(heights)
        detrended_range = max(residuals) - min(residuals)
        detrended_std = statistics.stdev(residuals)

        scan_width = max(xs) - min(xs)
        tilt_height = abs(slope * scan_width)
        tilt_angle = math.degrees(math.atan(slope))
        median_height = statistics.median(heights)

        plt.plot(
            xs,
            heights,
            linewidth=1,
            label=f"Test {number}"
        )
    else:
        slope = None
        residuals = []
        raw_range = None
        detrended_range = None
        detrended_std = None
        tilt_height = None
        tilt_angle = None
        median_height = None

    profiles.append({
        "xs": xs,
        "residuals": residuals,
    })

    summary.append({
        "test_number": number,
        "filename": file_path.name,
        "valid_points": valid_points,
        "invalid_points": invalid_points,
        "median_height_mm": median_height,
        "raw_height_range_mm": raw_range,
        "tilt_height_mm": tilt_height,
        "tilt_angle_deg": tilt_angle,
        "detrended_range_mm": detrended_range,
        "detrended_std_mm": detrended_std,
    })


raw_plot = OUTPUT_DIR / "桌面10次原始轮廓叠加.png"

plt.xlabel("X position (mm)")
plt.ylabel("Height Z (mm)")
plt.title("10 Flat-Surface Profiles")
plt.grid(True)
plt.legend()
plt.tight_layout()
plt.savefig(raw_plot, dpi=180)
plt.close()


plt.figure(figsize=(11, 6))

for number, profile in enumerate(profiles, start=1):
    if profile["residuals"]:
        plt.plot(
            profile["xs"],
            profile["residuals"],
            linewidth=1,
            label=f"Test {number}"
        )

detrended_plot = OUTPUT_DIR / "桌面10次去倾斜轮廓.png"

plt.xlabel("X position (mm)")
plt.ylabel("Residual height (mm)")
plt.title("Flat Profiles After Tilt Removal")
plt.grid(True)
plt.legend()
plt.tight_layout()
plt.savefig(detrended_plot, dpi=180)
plt.close()


point_indices = []
point_stds = []

for point_index in sorted(point_values):
    values = point_values[point_index]

    if len(values) == 10:
        point_indices.append(point_index)
        point_stds.append(statistics.stdev(values))

plt.figure(figsize=(11, 5))
plt.plot(point_indices, point_stds, linewidth=1)
plt.xlabel("Point index")
plt.ylabel("Standard deviation (mm)")
plt.title("Repeatability at Each Profile Point")
plt.grid(True)
plt.tight_layout()

repeatability_plot = OUTPUT_DIR / "各点十次测量标准差.png"
plt.savefig(repeatability_plot, dpi=180)
plt.close()


summary_file = OUTPUT_DIR / "桌面10次分析统计.csv"

with summary_file.open(
    "w",
    encoding="utf-8-sig",
    newline=""
) as file:
    writer = csv.DictWriter(
        file,
        fieldnames=summary[0].keys()
    )
    writer.writeheader()
    writer.writerows(summary)


median_values = [
    row["median_height_mm"]
    for row in summary
    if row["median_height_mm"] is not None
]

detrended_stds = [
    row["detrended_std_mm"]
    for row in summary
    if row["detrended_std_mm"] is not None
]


print("桌面10次测试分析完成：")
print()

for row in summary:
    print(
        f"{row['test_number']:2d}. "
        f"有效点 {row['valid_points']}，"
        f"去倾斜标准差 "
        f"{row['detrended_std_mm']}"
    )

print()
print(f"原始轮廓图：{raw_plot}")
print(f"去倾斜轮廓图：{detrended_plot}")
print(f"各点重复性图：{repeatability_plot}")
print(f"统计表：{summary_file}")

if len(median_values) >= 2:
    print(
        "十次整体高度标准差："
        f"{statistics.stdev(median_values):.6f} mm"
    )

if detrended_stds:
    print(
        "平均单条轮廓去倾斜标准差："
        f"{statistics.mean(detrended_stds):.6f} mm"
    )

if point_stds:
    print(
        "同一点十次测量平均标准差："
        f"{statistics.mean(point_stds):.6f} mm"
    )
    print(
        "同一点十次测量最大标准差："
        f"{max(point_stds):.6f} mm"
    )
