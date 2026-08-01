#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "runs" / "clean64-final-report"
MODELS = {
    "DeepSeek V4 Flash": {
        "baseline": ROOT / "runs" / "clean64-deepseek-baseline" / "result.json",
        "work": ROOT / "runs" / "clean64-deepseek-self-harness",
    },
    "Qwen3.7 Plus": {
        "baseline": ROOT / "runs" / "clean64-qwen-baseline" / "result.json",
        "work": ROOT / "runs" / "clean64-qwen-self-harness",
    },
}


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    data = {model: load_model(paths) for model, paths in MODELS.items()}
    configure_style()
    draw_performance(data)
    draw_deltas(data)
    draw_candidate_decisions(data)
    draw_repeat_stability(data)
    draw_dashboard(data)
    provenance = build_provenance(data)
    write_json(OUTPUT / "provenance.json", provenance)
    write_report(data, provenance)
    write_html()
    print(f"wrote Clean64 final report to {OUTPUT}")
    return 0


def load_model(paths: dict[str, Path]) -> dict[str, Any]:
    baseline_path = paths["baseline"]
    state_path = paths["work"] / "branch_state.json"
    queue_path = paths["work"] / "candidate_queue.json"
    for path in (baseline_path, state_path, queue_path):
        if not path.is_file():
            raise RuntimeError(f"最终报告缺少输入文件: {path}")
    baseline = read_json(baseline_path)
    state = read_json(state_path)
    queue = read_json(queue_path)
    active_id = state["active_branch_id"]
    active = next((item for item in state["branches"] if item.get("branch_id") == active_id), None)
    if active is None:
        raise RuntimeError(f"branch_state 找不到 active branch: {active_id}")
    final_path = Path(active["baseline_result"]).resolve()
    if not final_path.is_file():
        raise RuntimeError(f"active branch 结果不存在: {final_path}")
    final = read_json(final_path)
    validate_complete(baseline, label=str(baseline_path))
    validate_complete(final, label=str(final_path))
    return {
        "baseline": baseline,
        "final": final,
        "queue": queue,
        "state": state,
        "paths": [baseline_path.resolve(), state_path.resolve(), queue_path.resolve(), final_path],
    }


def validate_complete(payload: dict[str, Any], *, label: str) -> None:
    total = 0
    for split, expected_total in (("train", 43), ("heldout", 21)):
        repeats = payload.get("splits", {}).get(split)
        if not isinstance(repeats, list) or len(repeats) != 2:
            raise RuntimeError(f"{label}: {split} 必须有两次 repeat")
        for repeat in repeats:
            cases = repeat.get("case_results", [])
            if any(item.get("status") == "invalid" for item in cases):
                raise RuntimeError(f"{label}: {split} 含 invalid trial")
            if int(repeat.get("total", -1)) != expected_total:
                raise RuntimeError(f"{label}: {split} 分母不完整")
            total += int(repeat["total"])
    if total != 128:
        raise RuntimeError(f"{label}: 总 trial 数必须为 128")


def split_rates(payload: dict[str, Any], split: str) -> list[float]:
    return [int(item["passed"]) / int(item["total"]) for item in payload["splits"][split]]


def metric(payload: dict[str, Any], split: str) -> float:
    rates = split_rates(payload, split)
    return sum(rates) / len(rates)


def overall(payload: dict[str, Any]) -> float:
    passed = sum(int(item["passed"]) for repeats in payload["splits"].values() for item in repeats)
    total = sum(int(item["total"]) for repeats in payload["splits"].values() for item in repeats)
    return passed / total


def configure_style() -> None:
    plt.rcParams.update({
        "font.sans-serif": ["Microsoft YaHei", "SimHei", "DejaVu Sans"],
        "axes.unicode_minus": False,
        "figure.facecolor": "#f5f7fb",
        "axes.facecolor": "white",
        "axes.edgecolor": "#dce1ec",
        "axes.titleweight": "bold",
        "text.color": "#24293a",
        "grid.color": "#e8ebf3",
    })


def draw_performance(data: dict[str, dict[str, Any]], *, ax=None) -> None:
    own = ax is None
    if own:
        _, ax = plt.subplots(figsize=(11, 6))
    labels = list(data)
    x = np.arange(len(labels))
    width = 0.12
    series = (("Baseline Train", "baseline", "train", "#9ba5d9"),
              ("Final Train", "final", "train", "#4f63d8"),
              ("Baseline Heldout", "baseline", "heldout", "#8fd3c0"),
              ("Final Heldout", "final", "heldout", "#20a486"))
    for index, (name, stage, split, color) in enumerate(series):
        values = [metric(item[stage], split) * 100 for item in data.values()]
        bars = ax.bar(x + (index - 1.5) * width, values, width, label=name, color=color, zorder=3)
        for bar, value in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, value + 1, f"{value:.1f}", ha="center", fontsize=8)
    ax.set_xticks(x, labels)
    ax.set_ylim(0, 108)
    ax.set_ylabel("两次 repeat 平均通过率（%）")
    ax.set_title("Baseline 与最终 active branch")
    ax.grid(axis="y", zorder=0)
    ax.legend(frameon=False, ncol=2)
    if own:
        plt.tight_layout(); plt.savefig(OUTPUT / "01_performance.png", dpi=180, bbox_inches="tight"); plt.close()


def draw_deltas(data: dict[str, dict[str, Any]], *, ax=None) -> None:
    own = ax is None
    if own:
        _, ax = plt.subplots(figsize=(10, 5.5))
    labels = list(data)
    y = np.arange(len(labels))
    width = 0.24
    for offset, (split, color) in enumerate((("train", "#4f63d8"), ("heldout", "#20a486"))):
        values = [(metric(item["final"], split) - metric(item["baseline"], split)) * 100 for item in data.values()]
        bars = ax.barh(y + (offset - .5) * width, values, width, label=split, color=color, zorder=3)
        for bar, value in zip(bars, values):
            ax.text(value + (0.15 if value >= 0 else -0.15), bar.get_y() + bar.get_height()/2,
                    f"{value:+.2f} pp", va="center", ha="left" if value >= 0 else "right", fontsize=9)
    ax.axvline(0, color="#596079", linewidth=1)
    ax.set_yticks(y, labels)
    ax.set_xlabel("最终 active branch 相对 baseline 的变化（百分点）")
    ax.set_title("Self-Harness 改进量与无退化检查")
    ax.grid(axis="x", zorder=0)
    ax.legend(frameon=False)
    if own:
        plt.tight_layout(); plt.savefig(OUTPUT / "02_deltas.png", dpi=180, bbox_inches="tight"); plt.close()


def decision_counts(item: dict[str, Any]) -> tuple[int, int, int]:
    candidates = item["queue"].get("candidates", [])
    accepted = sum(str(c.get("status", "")).startswith("accepted") and c.get("status") != "accepted_merge_rejected" for c in candidates)
    rejected = sum(c.get("status") in {"rejected", "accepted_merge_rejected"} for c in candidates)
    return accepted, rejected, len(candidates)


def draw_candidate_decisions(data: dict[str, dict[str, Any]], *, ax=None) -> None:
    own = ax is None
    if own:
        _, ax = plt.subplots(figsize=(9.5, 5.5))
    labels = list(data)
    counts = [decision_counts(item) for item in data.values()]
    accepted = [item[0] for item in counts]
    rejected = [item[1] for item in counts]
    ax.bar(labels, accepted, color="#4cb782", label="接受", zorder=3)
    ax.bar(labels, rejected, bottom=accepted, color="#e36a6a", label="拒绝", zorder=3)
    for index, (_, _, total) in enumerate(counts):
        ax.text(index, total + .12, f"实际入队 {total}", ha="center", fontweight="bold")
    ax.set_ylabel("候选数")
    ax.set_title("候选评测与 acceptance gate 决策")
    ax.grid(axis="y", zorder=0)
    ax.legend(frameon=False)
    if own:
        plt.tight_layout(); plt.savefig(OUTPUT / "03_candidate_decisions.png", dpi=180, bbox_inches="tight"); plt.close()


def draw_repeat_stability(data: dict[str, dict[str, Any]], *, ax=None) -> None:
    own = ax is None
    if own:
        _, ax = plt.subplots(figsize=(10, 5.5))
    labels, values, colors = [], [], []
    for model, item in data.items():
        for split, color in (("train", "#4f63d8"), ("heldout", "#20a486")):
            labels.append(f"{model}\n{split}")
            values.append([value * 100 for value in split_rates(item["final"], split)])
            colors.append(color)
    positions = np.arange(len(labels))
    for position, pair, color in zip(positions, values, colors):
        ax.plot([position, position], pair, color=color, linewidth=3, zorder=2)
        ax.scatter([position, position], pair, color=color, s=55, zorder=3)
        for repeat, value in enumerate(pair, 1):
            ax.text(position + .04, value, f"R{repeat} {value:.1f}%", va="center", fontsize=8)
    ax.set_xticks(positions, labels)
    ax.set_ylim(0, 105)
    ax.set_ylabel("最终通过率（%）")
    ax.set_title("最终结果的 repeat 稳定性")
    ax.grid(axis="y", zorder=0)
    if own:
        plt.tight_layout(); plt.savefig(OUTPUT / "04_repeat_stability.png", dpi=180, bbox_inches="tight"); plt.close()


def draw_dashboard(data: dict[str, dict[str, Any]]) -> None:
    fig, axes = plt.subplots(2, 2, figsize=(18, 12))
    draw_performance(data, ax=axes[0, 0]); draw_deltas(data, ax=axes[0, 1])
    draw_candidate_decisions(data, ax=axes[1, 0]); draw_repeat_stability(data, ax=axes[1, 1])
    fig.suptitle("Self-Harness Clean64 最终复现报告", fontsize=22, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, .96), h_pad=3, w_pad=2.5)
    fig.savefig(OUTPUT / "00_dashboard.png", dpi=180, bbox_inches="tight"); plt.close(fig)


def build_provenance(data: dict[str, dict[str, Any]]) -> dict[str, Any]:
    sources = []
    for model, item in data.items():
        for path in dict.fromkeys(item["paths"]):
            sources.append({"model": model, "path": str(path), "sha256": sha256_file(path)})
    return {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "method": "Terminal-Bench 2.0 Clean64; 43 train + 21 heldout; 2 repeats; K=3; T=2",
        "metric": "先计算每个 split/repeat 的 pass_rate，再对两个 repeat 取算术平均",
        "acceptance_rule": "所有 split 均不下降，且至少一个 split 改善",
        "sources": sources,
    }


def write_report(data: dict[str, dict[str, Any]], provenance: dict[str, Any]) -> None:
    rows = []
    decisions = []
    for model, item in data.items():
        bt, bh = metric(item["baseline"], "train"), metric(item["baseline"], "heldout")
        ft, fh = metric(item["final"], "train"), metric(item["final"], "heldout")
        accepted, rejected, total = decision_counts(item)
        rows.append(f"| {model} | {bt:.2%} | {ft:.2%} | {ft-bt:+.2%} | {bh:.2%} | {fh:.2%} | {fh-bh:+.2%} |")
        decisions.append(f"| {model} | {total} | {accepted} | {rejected} | {item['state']['active_branch_id']} |")
    text = f"""# Self-Harness Clean64 最终复现报告

生成时间：{provenance['generated_at']}

## 最终结果

| 模型 | Train 基线 | Train 最终 | Train Δ | Heldout 基线 | Heldout 最终 | Heldout Δ |
|---|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

| 模型 | 候选总数 | 接受 | 拒绝 | 最终 active branch |
|---|---:|---:|---:|---|
{chr(10).join(decisions)}

## 图表与数值来源

1. baseline 来自每个模型的 `clean64-*-baseline/result.json`。
2. 最终结果来自 `branch_state.json` 指向的 active branch `baseline_result`，不是人工挑选候选。
3. Train/Heldout 指标先按 repeat 计算 `passed / total`，再对两次 repeat 取算术平均。
4. 候选决策来自 `candidate_queue.json`；接受门控为“所有 split 均不下降，且至少一个改善”。
5. 每个输入文件的绝对路径与 SHA256 均记录在 `provenance.json`，可逐项复核。

## 复现范围说明

本实验严格复现论文流程与 Clean64 划分，但使用 DeepSeek V4 Flash 与 Qwen3.7 Plus 作为可替代模型，因此属于流程级替代模型复现，不宣称复现论文原模型的数值。
"""
    (OUTPUT / "REPORT.md").write_text(text, encoding="utf-8")


def write_html() -> None:
    cards = "".join(f'<section><img src="{name}"></section>' for name in (
        "00_dashboard.png", "01_performance.png", "02_deltas.png", "03_candidate_decisions.png", "04_repeat_stability.png"))
    html = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>Self-Harness Clean64 最终复现报告</title>
<style>body{{margin:0;background:#f2f4f9;color:#24293a;font-family:"Microsoft YaHei",sans-serif}}main{{max-width:1300px;margin:auto;padding:36px}}section{{background:white;border-radius:16px;padding:16px;margin:22px 0;box-shadow:0 8px 25px #28304a12}}img{{width:100%}}a{{color:#4358c8}}</style></head><body><main><h1>Self-Harness Clean64 最终复现报告</h1>{cards}<p><a href="REPORT.md">文字报告</a> · <a href="provenance.json">数据血缘</a></p></main></body></html>'''
    (OUTPUT / "index.html").write_text(html, encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
