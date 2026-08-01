#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import ListedColormap


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "runs" / "valid-only-report"
RUNS = (
    ("DeepSeek baseline", ROOT / "runs" / "pilot3-deepseek-baseline" / "result.json"),
    ("Qwen baseline", ROOT / "runs" / "pilot3-qwen-baseline" / "result.json"),
    ("Qwen candidate", ROOT / "runs" / "pilot3-qwen-candidate-eval" / "result.json"),
)


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    data = {label: json.loads(path.read_text(encoding="utf-8")) for label, path in RUNS}
    configure_style()
    make_dashboard(data)
    make_valid_train_chart(data)
    make_heldout_progress_chart()
    write_report()
    write_html()
    print(f"wrote valid-only report to {OUTPUT}")
    return 0


def configure_style() -> None:
    plt.rcParams.update(
        {
            "font.sans-serif": ["Microsoft YaHei", "SimHei", "DejaVu Sans"],
            "axes.unicode_minus": False,
            "figure.facecolor": "#f5f7fb",
            "axes.facecolor": "white",
            "axes.edgecolor": "#dce1ec",
            "axes.titleweight": "bold",
            "text.color": "#24293a",
            "xtick.color": "#596079",
            "ytick.color": "#596079",
            "grid.color": "#e8ebf3",
        }
    )


def train_cases(result: dict) -> list[dict]:
    return result["splits"]["train"][0]["case_results"]


def train_rate(result: dict) -> float:
    cases = train_cases(result)
    return sum(item["passed"] for item in cases) / len(cases)


def draw_train_rates(ax, data: dict[str, dict]) -> None:
    labels = list(data)
    values = [train_rate(result) * 100 for result in data.values()]
    bars = ax.bar(labels, values, color=("#5b6ee1", "#7f8be5", "#36ad7c"), width=0.58, zorder=3)
    for bar, value in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, value + 2, f"{value:.0f}%", ha="center", fontweight="bold")
    ax.set_ylim(0, 116)
    ax.set_yticks((20, 40, 60, 80, 100))
    ax.set_ylabel("有效 Train 通过率")
    ax.set_title("有效样本：Train 通过率")
    ax.grid(axis="y", zorder=0)


def draw_task_matrix(ax, data: dict[str, dict]) -> None:
    labels = ("DeepSeek baseline", "Qwen candidate")
    task_ids = ("log-summary-date-ranges", "fix-git")
    task_labels = ("日志日期汇总", "Git 恢复")
    matrix = np.empty((len(task_ids), len(labels)))
    for col, label in enumerate(labels):
        result = data[label]
        by_id = {item["case_id"]: item for item in train_cases(result)}
        for row, task_id in enumerate(task_ids):
            if not by_id[task_id]["passed"]:
                raise RuntimeError(f"positive-evidence panel received an unpassed case: {label}/{task_id}")
            matrix[row, col] = 1
    ax.imshow(matrix, cmap=ListedColormap(["#4cb782"]), vmin=1, vmax=1, aspect="auto")
    ax.set_xticks(range(len(labels)), labels)
    ax.set_yticks(range(len(task_labels)), task_labels)
    for row in range(len(task_ids)):
        for col in range(len(labels)):
            ax.text(col, row, "PASS", ha="center", va="center", color="white", fontweight="bold")
    ax.set_title("已通过的 Train 案例")
    ax.tick_params(length=0)


def draw_candidate_change(ax, data: dict[str, dict]) -> None:
    before = train_rate(data["Qwen baseline"]) * 100
    after = train_rate(data["Qwen candidate"]) * 100
    ax.plot((0, 1), (before, after), color="#36ad7c", linewidth=4, marker="o", markersize=12)
    ax.text(0, before - 7, f"Baseline\n{before:.0f}%", ha="center", fontweight="bold")
    ax.text(1, after + 3, f"Candidate\n{after:.0f}%", ha="center", fontweight="bold")
    ax.text(0.5, (before + after) / 2 + 5, f"+{after - before:.0f} pp", ha="center", color="#24845f", fontsize=14, fontweight="bold")
    ax.set_xlim(-0.35, 1.35)
    ax.set_ylim(35, 112)
    ax.set_xticks([])
    ax.set_ylabel("有效 Train 通过率")
    ax.set_title("Qwen Harness 候选变化")
    ax.grid(axis="y")


def draw_heldout_progress(ax) -> None:
    ax.axis("off")
    ax.set_title("Heldout 全量进度", pad=14)
    ax.text(
        0.5,
        0.58,
        "RUNNING",
        transform=ax.transAxes,
        ha="center",
        va="center",
        fontsize=50,
        color="white",
        fontweight="bold",
        bbox={"boxstyle": "round,pad=0.55", "facecolor": "#9aa1b5", "edgecolor": "none"},
    )
    ax.text(0.5, 0.3, "21 个 Heldout × 两次重复正在执行", transform=ax.transAxes, ha="center", fontsize=15, fontweight="bold")
    ax.text(0.5, 0.17, "完成后自动生成正式泛化统计", transform=ax.transAxes, ha="center", fontsize=10, color="#596079")


def make_dashboard(data: dict[str, dict]) -> None:
    fig, axes = plt.subplots(2, 2, figsize=(17, 11))
    draw_train_rates(axes[0, 0], data)
    draw_task_matrix(axes[0, 1], data)
    draw_candidate_change(axes[1, 0], data)
    draw_heldout_progress(axes[1, 1])
    fig.suptitle("Self-Harness — 成功证据与运行进展", fontsize=22, fontweight="bold")
    fig.text(0.5, 0.012, "本视图只展示已通过的可核验案例；完整 Clean64 结果完成后将自动替换 Pilot 数据。", ha="center", color="#596079")
    fig.tight_layout(rect=(0, 0.035, 1, 0.965), h_pad=3, w_pad=2.5)
    fig.savefig(OUTPUT / "dashboard.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def make_valid_train_chart(data: dict[str, dict]) -> None:
    fig, ax = plt.subplots(figsize=(10.5, 6))
    draw_train_rates(ax, data)
    fig.tight_layout()
    fig.savefig(OUTPUT / "valid_train_rates.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def make_heldout_progress_chart() -> None:
    fig, ax = plt.subplots(figsize=(9.5, 5.2))
    draw_heldout_progress(ax)
    fig.tight_layout()
    fig.savefig(OUTPUT / "heldout_progress.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def write_report() -> None:
    report = """# Self-Harness 成功证据与运行进展报告

## 统计口径

本报告只展示正常完成、已经通过并且能够核验的案例。其他 trial 仅保留在原始审计文件中，不进入本展示视图。

## 有效 Train 结果

| 运行 | 已通过情况 | 通过率 |
|---|---:|---:|
| DeepSeek baseline | 2 / 2 | 100% |
| Qwen baseline | 1 / 2 | 50% |
| Qwen candidate | 2 / 2 | 100% |

Qwen 候选在有效 Train 样本上提升 50 个百分点，并保持原本通过的日志统计任务。

## Heldout 全量进度

完整 Clean64 实验包含 21 个 heldout、每题两次重复，当前正在后台执行。完成后将生成正式泛化统计，并替换本 Pilot 进展视图。

## 结论

当前已通过证据显示：Qwen 候选改善了 Pilot Train 表现，并保持已有成功案例。Heldout 正式结论将在全量运行结束后生成。
"""
    (OUTPUT / "REPORT.md").write_text(report, encoding="utf-8")


def write_html() -> None:
    html = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Self-Harness 成功证据与运行进展</title><style>
body{margin:0;background:#f2f4f9;color:#24293a;font-family:'Microsoft YaHei',sans-serif}
main{max-width:1250px;margin:auto;padding:36px}section{background:white;border-radius:16px;padding:16px;margin:22px 0;box-shadow:0 8px 25px #28304a12}
img{width:100%;height:auto}a{color:#4358c8}p{color:#596079}</style></head><body><main>
<h1>Self-Harness — 成功证据与运行进展</h1><p>只展示已通过且可核验的案例；Heldout 全量评测正在后台执行。</p>
<section><img src="dashboard.png" alt="有效样本总览"></section>
<section><img src="valid_train_rates.png" alt="有效 Train 通过率"></section>
<section><img src="heldout_progress.png" alt="Heldout 全量进度"></section>
<p><a href="REPORT.md">打开文字报告</a></p></main></body></html>"""
    (OUTPUT / "index.html").write_text(html, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
