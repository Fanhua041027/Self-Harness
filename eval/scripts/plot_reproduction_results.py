#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import ListedColormap


RUNS = (
    ("DeepSeek baseline", Path("runs/pilot3-deepseek-baseline/result.json")),
    ("Qwen baseline", Path("runs/pilot3-qwen-baseline/result.json")),
    ("Qwen candidate", Path("runs/pilot3-qwen-candidate-eval/result.json")),
)
TASKS = ("log-summary-date-ranges", "fix-git", "break-filter-js-from-html")


def main() -> int:
    parser = argparse.ArgumentParser(description="绘制 Self-Harness pilot 复现图表。")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output-dir", type=Path, default=Path("runs/visualizations"))
    args = parser.parse_args()

    root = args.root.resolve()
    output_dir = (root / args.output_dir).resolve() if not args.output_dir.is_absolute() else args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    data = {label: load_result(root / path) for label, path in RUNS}

    configure_style()
    chart_pass_rates(data, output_dir / "01_pass_rates.png")
    chart_task_matrix(data, output_dir / "02_task_matrix.png")
    chart_acceptance_delta(data, output_dir / "03_acceptance_delta.png")
    chart_trace_events(data, output_dir / "04_trace_events.png")
    chart_heldout_status(data, output_dir / "05_heldout_status.png")
    chart_dashboard(data, output_dir / "00_dashboard.png")
    write_html(output_dir)
    print(f"wrote charts to {output_dir}")
    return 0


def load_result(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def configure_style() -> None:
    plt.rcParams.update(
        {
            "font.sans-serif": ["Microsoft YaHei", "SimHei", "Arial Unicode MS", "DejaVu Sans"],
            "axes.unicode_minus": False,
            "figure.facecolor": "#f7f8fc",
            "axes.facecolor": "#ffffff",
            "axes.edgecolor": "#d8dce8",
            "axes.titleweight": "bold",
            "axes.titlesize": 15,
            "axes.labelcolor": "#30364a",
            "text.color": "#24293a",
            "xtick.color": "#596079",
            "ytick.color": "#596079",
            "grid.color": "#e8eaf2",
            "grid.linewidth": 0.8,
        }
    )


def split_rate(result: dict, split: str) -> float:
    repeats = result["splits"][split]
    passed = sum(item["passed"] for item in repeats)
    total = sum(item["total"] for item in repeats)
    return passed / total if total else 0.0


def outcomes(result: dict) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for repeats in result["splits"].values():
        for repeat in repeats:
            for item in repeat["case_results"]:
                found[item["case_id"]] = item
    return found


def bar_label(ax, bars) -> None:
    for bar in bars:
        value = bar.get_height()
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            value + 2.0,
            f"{value:.0f}%",
            ha="center",
            va="bottom",
            fontsize=10,
            fontweight="bold",
        )


def draw_pass_rates(ax, data: dict[str, dict]) -> None:
    labels = list(data)
    groups = ("Train", "Heldout", "Overall")
    colors = ("#5b6ee1", "#20a486", "#f29e4c")
    x = np.arange(len(labels))
    width = 0.23
    for index, (group, color) in enumerate(zip(groups, colors)):
        values = []
        for result in data.values():
            if group == "Train":
                value = split_rate(result, "train")
            elif group == "Heldout":
                value = split_rate(result, "heldout")
            else:
                value = result["correctness"]
            values.append(value * 100)
        bars = ax.bar(x + (index - 1) * width, values, width, label=group, color=color, zorder=3)
        bar_label(ax, bars)
        if group == "Heldout":
            status_labels = ("TIMEOUT", "INFRA", "TIMEOUT")
            for position, status in zip(x, status_labels):
                ax.scatter(position, 0, marker="D", s=72, color=color, edgecolor="white", linewidth=1.2, zorder=5)
                ax.text(position, 6.5, status, ha="center", va="bottom", fontsize=8.5, color="#187963", fontweight="bold")
    ax.set_xticks(x, labels)
    ax.set_ylim(0, 116)
    ax.set_ylabel("通过率")
    ax.set_title("真实 API pilot：按 split 的原始通过率")
    ax.grid(axis="y", zorder=0)
    ax.legend(frameon=False, ncol=3, loc="upper left")


def chart_pass_rates(data: dict[str, dict], path: Path) -> None:
    fig, ax = plt.subplots(figsize=(11, 6.2))
    draw_pass_rates(ax, data)
    fig.tight_layout()
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def task_state(item: dict, label: str, task: str) -> tuple[int, str]:
    if item["passed"]:
        return 3, "PASS"
    metadata = item.get("trace_metadata", {})
    if metadata.get("timed_out"):
        return 1, "TIMEOUT"
    if label == "Qwen baseline" and task == "break-filter-js-from-html":
        return 0, "INFRA"
    return 2, "FAIL"


def draw_task_matrix(ax, data: dict[str, dict]) -> None:
    labels = list(data)
    matrix = np.zeros((len(TASKS), len(labels)))
    texts: list[list[str]] = []
    for row, task in enumerate(TASKS):
        row_text = []
        for col, label in enumerate(labels):
            state, text = task_state(outcomes(data[label])[task], label, task)
            matrix[row, col] = state
            row_text.append(text)
        texts.append(row_text)
    cmap = ListedColormap(["#9aa1b5", "#f2b366", "#e36a6a", "#4cb782"])
    ax.imshow(matrix, cmap=cmap, vmin=-0.5, vmax=3.5, aspect="auto")
    ax.set_xticks(range(len(labels)), labels)
    ax.set_yticks(range(len(TASKS)), ["日志日期汇总\n(train)", "Git 恢复\n(train)", "HTML 过滤绕过\n(heldout)"])
    for row in range(len(TASKS)):
        for col in range(len(labels)):
            ax.text(col, row, texts[row][col], ha="center", va="center", color="white", fontweight="bold")
    ax.set_title("逐任务结果矩阵（区分功能失败、超时和基础设施失败）")
    ax.tick_params(length=0)


def chart_task_matrix(data: dict[str, dict], path: Path) -> None:
    fig, ax = plt.subplots(figsize=(11, 5.4))
    draw_task_matrix(ax, data)
    fig.tight_layout()
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def draw_acceptance_delta(ax, data: dict[str, dict]) -> None:
    baseline = data["Qwen baseline"]
    candidate = data["Qwen candidate"]
    values = [
        (split_rate(candidate, "train") - split_rate(baseline, "train")) * 100,
        (split_rate(candidate, "heldout") - split_rate(baseline, "heldout")) * 100,
        (candidate["correctness"] - baseline["correctness"]) * 100,
    ]
    labels = ["Train", "Heldout", "Overall"]
    colors = ["#4cb782" if value > 0 else "#9aa1b5" for value in values]
    bars = ax.barh(labels, values, color=colors, zorder=3)
    for bar, value in zip(bars, values):
        ax.text(value + 1.2, bar.get_y() + bar.get_height() / 2, f"{value:+.1f} pp", va="center", fontweight="bold")
    ax.axvline(0, color="#596079", linewidth=1)
    ax.set_xlim(-5, 58)
    ax.set_xlabel("候选相对 baseline 的通过率变化（百分点）")
    ax.set_title("Qwen 候选的 acceptance 增量")
    ax.grid(axis="x", zorder=0)
    ax.text(0.98, 0.03, "pilot 数学门控：accepted", transform=ax.transAxes, ha="right", color="#24845f", fontweight="bold")


def chart_acceptance_delta(data: dict[str, dict], path: Path) -> None:
    fig, ax = plt.subplots(figsize=(10, 5.3))
    draw_acceptance_delta(ax, data)
    fig.tight_layout()
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def draw_trace_events(ax, data: dict[str, dict]) -> None:
    labels = list(data)
    tasks = ("log-summary-date-ranges", "fix-git")
    names = ("日志日期汇总", "Git 恢复")
    x = np.arange(len(labels))
    width = 0.33
    for index, (task, name, color) in enumerate(zip(tasks, names, ("#5b6ee1", "#f29e4c"))):
        values = [outcomes(result)[task].get("trace_metadata", {}).get("event_count", 0) for result in data.values()]
        bars = ax.bar(x + (index - 0.5) * width, values, width, label=name, color=color, zorder=3)
        for bar, value in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, value + 0.7, str(value), ha="center", fontsize=10)
    ax.set_xticks(x, labels)
    ax.set_ylabel("Agent trace 事件数")
    ax.set_ylim(0, 34)
    ax.set_title("Train 任务的轨迹长度（操作成本代理）")
    ax.grid(axis="y", zorder=0)
    ax.legend(frameon=False)


def chart_trace_events(data: dict[str, dict], path: Path) -> None:
    fig, ax = plt.subplots(figsize=(10.5, 5.5))
    draw_trace_events(ax, data)
    fig.tight_layout()
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def chart_heldout_status(data: dict[str, dict], path: Path) -> None:
    labels = list(data)
    status_labels = ("TIMEOUT", "INFRA", "TIMEOUT")
    details = (
        "Agent 超过 600 秒",
        "Verifier Debian 源 502",
        "Agent 超过 600 秒",
    )
    colors = ("#f2b366", "#9aa1b5", "#f2b366")
    fig, ax = plt.subplots(figsize=(11, 5.5))
    ax.set_xlim(-0.6, 2.6)
    ax.set_ylim(-0.8, 1.25)
    ax.axis("off")
    ax.set_title("Pilot Heldout 状态：任务存在，但没有有效通过结果", pad=18)
    for index, (label, status, detail, color) in enumerate(zip(labels, status_labels, details, colors)):
        ax.text(
            index,
            0.55,
            status,
            ha="center",
            va="center",
            fontsize=18,
            color="white",
            fontweight="bold",
            bbox={"boxstyle": "round,pad=1.2", "facecolor": color, "edgecolor": "none"},
        )
        ax.text(index, -0.05, label, ha="center", va="center", fontsize=12, fontweight="bold")
        ax.text(index, -0.28, detail, ha="center", va="center", fontsize=10, color="#596079")
        ax.text(index, -0.5, "原始通过率：0%", ha="center", va="center", fontsize=10, color="#a04a3f")
    ax.text(
        1,
        1.02,
        "Heldout 任务：break-filter-js-from-html（1 个）",
        ha="center",
        fontsize=11,
        color="#596079",
    )
    ax.text(
        1,
        -0.72,
        "TIMEOUT 与 INFRA 单独呈现，不解释为可靠的模型能力得分。完整 Clean64 将包含 21 个 heldout × 2 次重复。",
        ha="center",
        fontsize=10,
        color="#596079",
    )
    fig.tight_layout()
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def chart_dashboard(data: dict[str, dict], path: Path) -> None:
    fig, axes = plt.subplots(2, 2, figsize=(18, 12))
    draw_pass_rates(axes[0, 0], data)
    draw_task_matrix(axes[0, 1], data)
    draw_acceptance_delta(axes[1, 0], data)
    draw_trace_events(axes[1, 1], data)
    fig.suptitle("Self-Harness 真实 API 复现 — Pilot 可视化总览", fontsize=22, fontweight="bold", y=0.995)
    fig.text(
        0.5,
        0.008,
        "范围：2 train + 1 heldout，1 次重复。该图验证闭环，不代表 Clean64 论文级最终数值。",
        ha="center",
        fontsize=11,
        color="#596079",
    )
    fig.tight_layout(rect=(0, 0.025, 1, 0.97), h_pad=3.2, w_pad=2.6)
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def write_html(output_dir: Path) -> None:
    cards = "\n".join(
        f'<section><img src="{name}" alt="{name}"></section>'
        for name in (
            "01_pass_rates.png",
            "02_task_matrix.png",
            "03_acceptance_delta.png",
            "04_trace_events.png",
            "05_heldout_status.png",
        )
    )
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Self-Harness 复现图表</title>
<style>
body{{margin:0;background:#f2f4f9;color:#24293a;font-family:'Microsoft YaHei',sans-serif}}
main{{max-width:1280px;margin:auto;padding:36px}} h1{{margin:0 0 8px}} p{{color:#596079}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin-top:28px}}
section{{background:white;border:1px solid #e0e3ec;border-radius:16px;padding:12px;box-shadow:0 8px 25px #28304a12}}
img{{display:block;width:100%;height:auto;border-radius:10px}} @media(max-width:900px){{.grid{{grid-template-columns:1fr}}}}
</style></head><body><main><h1>Self-Harness 真实 API 复现图表</h1>
<p>2 train + 1 heldout、1 次重复的 pilot。Heldout 中包含网络失败/超时，已在图中单独标注。</p>
<div class="grid">{cards}</div></main></body></html>"""
    (output_dir / "index.html").write_text(html, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
