import csv
import statistics
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


ACTUAL_STEP_MM = 13.0

DATA_DIR = Path.home() / "Desktop" / "LJX轮廓数据"
OUTPUT_DIR = DATA_DIR / "台阶测试分析"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def estimate_step_height(values):
    """使用两组聚类估算两个平台之间的高度差。"""
    if len(values) < 100:
        return None

    values = sorted(values)
    low_center = values[len(values) // 4]
    high_center = values[len(values) * 3 // 4]

    for _ in range(30):
        low_group = []
        high_group = []

        for value in values:
            if abs(value - low_center) <= abs(value - high_center):
                low_group.append(value)
            else:
                high_group.append(value)

        if len(low_group) < 20 or len(high_group) < 20:
            return None

        new_low = statistics.median(low_group)
        new_high = statistics.median(high_group)

        if abs(new_low - low_center) < 1e-8 and abs(new_high - high_center) < 1e-8:
            break

        low_center = new_low
        high_center = new_high

    return abs(high_center - low_center)


files = sorted(
    DATA_DIR.glob("profile_*.csv"),
    key=lambda path: path.stat().st_mtime,
    reverse=True,
)[:10]

if len(files) < 10:
    raise RuntimeError(f"只找到 {len(files)} 个轮廓文件，至少需要 10 个。")

files.reverse()
summary = []
step_results = []

plt.figure(figsize=(11, 6))

for number, file_path in enumerate(files, start=1):
    x_values = []
    heights = []

    with file_path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)

        for row in reader:
            if row["status"] != "有效" or not row["height_mm"]:
                continue

            x_values.append(float(row["x_mm"]))
            heights.append(float(row["height_mm"]))

    step_height = estimate_step_height(heights)

    if step_height is not None:
        step_results.append(step_height)
        error_mm = step_height - ACTUAL_STEP_MM
        error_percent = error_mm / ACTUAL_STEP_MM * 100
    else:
        error_mm = None
        error_percent = None

    summary.append({
        "test_number": number,
        "filename": file_path.name,
        "valid_points": len(heights),
        "invalid_points": 800 - len(heights),
        "estimated_step_mm": step_height,
        "actual_step_mm": ACTUAL_STEP_MM,
        "error_mm": error_mm,
        "error_percent": error_percent,
    })

    if heights:
        plt.plot(x_values, heights, linewidth=1, label=f"Test {number}")


plot_path = OUTPUT_DIR / "latest_10_profiles_overlay.png"

plt.xlabel("X position (mm)")
plt.ylabel("Height Z (mm)")
plt.title("Latest 10 LJ-V7080 Step Profiles")
plt.grid(True)
plt.legend()
plt.tight_layout()
plt.savefig(plot_path, dpi=180)
plt.close()


summary_path = OUTPUT_DIR / "latest_10_profiles_summary.csv"

with summary_path.open("w", encoding="utf-8-sig", newline="") as file:
    writer = csv.DictWriter(file, fieldnames=summary[0].keys())
    writer.writeheader()
    writer.writerows(summary)


for row in summary:
    step = row["estimated_step_mm"]

    if step is None:
        step_text = "无法识别"
    else:
        step_text = f"{step:.4f} mm"

    print(
        f"{row['test_number']:2d}. {row['filename']}，"
        f"有效点 {row['valid_points']}，"
        f"估算台阶 {step_text}"
    )

print()
print(f"叠加曲线图：{plot_path}")
print(f"统计表：{summary_path}")

if step_results:
    average = statistics.mean(step_results)

    print(f"实际厚度：{ACTUAL_STEP_MM:.4f} mm")
    print(f"平均测量厚度：{average:.4f} mm")
    print(f"平均误差：{average - ACTUAL_STEP_MM:.4f} mm")

    if len(step_results) >= 2:
        print(
            "十次测量标准差："
            f"{statistics.stdev(step_results):.6f} mm"
        )
else:
    print("十条数据均未识别出两个稳定平台。")
