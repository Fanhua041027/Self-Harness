#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "eval" / "configs" / "harbor_local_clean64.toml"
OUTPUT = ROOT / "runs" / "clean64-live-report"
RUNS = {
    "DeepSeek V4 Flash": ROOT / "runs" / "clean64-deepseek-baseline",
    "Qwen3.7 Plus": ROOT / "runs" / "clean64-qwen-baseline",
}


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    expected = load_expected_cases(CONFIG)
    records = collect_records()
    provenance = build_provenance(expected=expected, records=records)
    write_json(OUTPUT / "provenance.json", provenance)
    configure_style()
    draw_dashboard(expected=expected, records=records)
    draw_completion(expected=expected, records=records, path=OUTPUT / "completion.png")
    draw_observed_rates(records=records, path=OUTPUT / "observed_pass_rates.png")
    draw_retry_audit(records=records, path=OUTPUT / "retry_audit.png")
    write_report(expected=expected, records=records, provenance=provenance)
    write_html()
    expected_total = provenance["expected"]["trials_per_model"]
    if all(len(model_records(records, model)) == expected_total for model in RUNS):
        final_output = ROOT / "runs" / "clean64-baseline-final-report"
        shutil.copytree(OUTPUT, final_output, dirs_exist_ok=True)
    print(f"wrote Clean64 live report to {OUTPUT}")
    return 0


def load_expected_cases(path: Path) -> dict[str, list[str]]:
    import tomllib

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    result = {"train": [], "heldout": []}
    for item in raw["cases"]:
        result[item["split"]].append(item["case_id"])
    return result


def collect_records() -> list[dict[str, Any]]:
    records = []
    for model, run_dir in RUNS.items():
        if not run_dir.exists():
            continue
        for path in sorted(run_dir.glob("splits/*/repeat-*/cases/*/result.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            records.append(
                {
                    "model": model,
                    "split": payload["split"],
                    "repeat": int(payload["repeat"]),
                    "case_id": payload["case_id"],
                    "status": payload["status"],
                    "passed": bool(payload["passed"]),
                    "reward": payload.get("reward"),
                    "attempt_count": int(payload.get("attempt_count", 1)),
                    "duration_s": float(payload.get("duration_s", 0)),
                    "infrastructure_error": payload.get("infrastructure_error"),
                    "source_path": str(path.resolve()),
                    "source_sha256": sha256_file(path),
                }
            )
    return records


def build_provenance(*, expected: dict[str, list[str]], records: list[dict[str, Any]]) -> dict[str, Any]:
    manifest = ROOT / "runs" / "terminal-bench-2-reliable-v2" / "SELF_HARNESS_BOOTSTRAP_MANIFEST.json"
    return {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "report_type": "live partial baseline report; becomes complete when all expected trials exist",
        "config_path": str(CONFIG.resolve()),
        "config_sha256": sha256_file(CONFIG),
        "bootstrap_manifest_path": str(manifest.resolve()),
        "bootstrap_manifest_sha256": sha256_file(manifest),
        "expected": {
            "train_cases": len(expected["train"]),
            "heldout_cases": len(expected["heldout"]),
            "repeats": 2,
            "trials_per_model": 2 * (len(expected["train"]) + len(expected["heldout"])),
        },
        "aggregation": {
            "completed": "number of case checkpoint result.json files",
            "valid": "completed records whose status is not invalid",
            "observed_pass_rate": "passed valid completed records / valid completed records",
            "invalid_handling": "included in retry/infrastructure audit; excluded from observed pass rate",
            "final_repeat_metric": "mean of per-repeat pass rates after all expected trials complete",
        },
        "sources": records,
    }


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


def model_records(records: list[dict[str, Any]], model: str) -> list[dict[str, Any]]:
    return [item for item in records if item["model"] == model]


def draw_completion(*, expected: dict[str, list[str]], records: list[dict[str, Any]], path: Path | None = None, ax=None) -> None:
    own_figure = ax is None
    if own_figure:
        _, ax = plt.subplots(figsize=(10, 5.5))
    total = 2 * (len(expected["train"]) + len(expected["heldout"]))
    models = list(RUNS)
    completed = [len(model_records(records, model)) for model in models]
    pending = [total - value for value in completed]
    ax.barh(models, completed, color="#4cb782", label="已完成", zorder=3)
    ax.barh(models, pending, left=completed, color="#e5e8f0", label="待完成", zorder=2)
    for index, value in enumerate(completed):
        ax.text(value + 1, index, f"{value} / {total}", va="center", fontweight="bold")
    ax.set_xlim(0, total + 16)
    ax.set_xlabel("Trial 数")
    ax.set_title("Clean64 Baseline 完成度")
    ax.legend(frameon=False, loc="lower right")
    ax.grid(axis="x", zorder=0)
    if own_figure:
        plt.tight_layout()
        plt.savefig(path, dpi=180, bbox_inches="tight")
        plt.close()


def observed_rate(records: list[dict[str, Any]], model: str, split: str) -> tuple[float | None, int]:
    valid = [item for item in records if item["model"] == model and item["split"] == split and item["status"] != "invalid"]
    if not valid:
        return None, 0
    return sum(item["passed"] for item in valid) / len(valid), len(valid)


def draw_observed_rates(*, records: list[dict[str, Any]], path: Path | None = None, ax=None) -> None:
    own_figure = ax is None
    if own_figure:
        _, ax = plt.subplots(figsize=(10, 5.5))
    models = list(RUNS)
    splits = ("train", "heldout")
    colors = ("#5b6ee1", "#20a486")
    x = np.arange(len(models))
    width = 0.32
    for offset, (split, color) in enumerate(zip(splits, colors)):
        for index, model in enumerate(models):
            rate, count = observed_rate(records, model, split)
            position = x[index] + (offset - 0.5) * width
            if rate is None:
                ax.text(position, 4, "等待数据", ha="center", color="#8b92a5", fontsize=9)
                continue
            bar = ax.bar(position, rate * 100, width, color=color, label=split if index == 0 else None, zorder=3)
            ax.text(position, rate * 100 + 2, f"{rate * 100:.1f}%\nn={count}", ha="center", fontsize=9, fontweight="bold")
    ax.set_xticks(x, models)
    ax.set_ylim(0, 116)
    ax.set_ylabel("已完成有效 Trial 的观察通过率")
    ax.set_title("阶段性通过率（完成度不足时不代表最终结果）")
    ax.legend(frameon=False)
    ax.grid(axis="y", zorder=0)
    if own_figure:
        plt.tight_layout()
        plt.savefig(path, dpi=180, bbox_inches="tight")
        plt.close()


def draw_retry_audit(*, records: list[dict[str, Any]], path: Path | None = None, ax=None) -> None:
    own_figure = ax is None
    if own_figure:
        _, ax = plt.subplots(figsize=(10, 5.5))
    models = list(RUNS)
    one = [sum(item["attempt_count"] == 1 for item in model_records(records, model)) for model in models]
    retried = [sum(item["attempt_count"] > 1 for item in model_records(records, model)) for model in models]
    ax.bar(models, one, color="#5b6ee1", label="一次完成", zorder=3)
    ax.bar(models, retried, bottom=one, color="#f29e4c", label="发生重试", zorder=3)
    for index, (a, b) in enumerate(zip(one, retried)):
        ax.text(index, a + b + 0.15, f"{a + b} 个 checkpoint", ha="center", fontweight="bold")
    ax.set_ylabel("已完成 Case 数")
    ax.set_title("依赖引导与重试审计")
    ax.legend(frameon=False)
    ax.grid(axis="y", zorder=0)
    if own_figure:
        plt.tight_layout()
        plt.savefig(path, dpi=180, bbox_inches="tight")
        plt.close()


def draw_dashboard(*, expected: dict[str, list[str]], records: list[dict[str, Any]]) -> None:
    fig, axes = plt.subplots(2, 2, figsize=(17, 11))
    draw_completion(expected=expected, records=records, ax=axes[0, 0])
    draw_observed_rates(records=records, ax=axes[0, 1])
    draw_retry_audit(records=records, ax=axes[1, 0])
    axes[1, 1].axis("off")
    axes[1, 1].set_title("数据血缘", pad=15)
    axes[1, 1].text(0.5, 0.68, "result.json", ha="center", fontsize=24, fontweight="bold", transform=axes[1, 1].transAxes)
    axes[1, 1].text(0.5, 0.52, "→ 校验 status / split / repeat", ha="center", fontsize=14, transform=axes[1, 1].transAxes)
    axes[1, 1].text(0.5, 0.40, "→ SHA-256 固定输入", ha="center", fontsize=14, transform=axes[1, 1].transAxes)
    axes[1, 1].text(0.5, 0.28, "→ 聚合公式生成图表", ha="center", fontsize=14, transform=axes[1, 1].transAxes)
    axes[1, 1].text(0.5, 0.12, "详情见 provenance.json", ha="center", color="#4358c8", transform=axes[1, 1].transAxes)
    fig.suptitle("Self-Harness Clean64 — 动态复现报告", fontsize=22, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.96), h_pad=3, w_pad=2.5)
    fig.savefig(OUTPUT / "dashboard.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def write_report(*, expected: dict[str, list[str]], records: list[dict[str, Any]], provenance: dict[str, Any]) -> None:
    total = provenance["expected"]["trials_per_model"]
    rows = []
    for model in RUNS:
        items = model_records(records, model)
        valid = [item for item in items if item["status"] != "invalid"]
        passed = sum(item["passed"] for item in valid)
        rate = f"{passed / len(valid) * 100:.2f}%" if valid else "等待数据"
        rows.append(f"| {model} | {len(items)} / {total} | {len(valid)} | {passed} | {rate} |")
    report = f"""# Self-Harness Clean64 动态复现报告

生成时间：{provenance['generated_at']}

> 这是随后台实验更新的阶段性报告。只有每个模型的 {total} 个 baseline trial 全部完成后，才是完整基线结果。

## 当前进度

| 模型 | 已完成 / 计划 | 有效 Trial | 已通过 | 阶段观察通过率 |
|---|---:|---:|---:|---:|
{chr(10).join(rows)}

固定划分：{len(expected['train'])} train、{len(expected['heldout'])} heldout；每题两次重复。

## 图表是怎么来的

1. 从 `runs/clean64-*/splits/<split>/repeat-*/cases/<case>/result.json` 读取已完成 checkpoint。
2. 配置分母来自 `{CONFIG.relative_to(ROOT).as_posix()}`，不是根据已有结果反推。
3. 每个输入文件的绝对路径和 SHA-256 写入 `provenance.json`。
4. `status=invalid` 的记录进入基础设施与重试审计，但不进入观察通过率。
5. 阶段观察通过率 = 已通过的有效完成 trial ÷ 有效完成 trial。
6. 最终论文指标将在所有 trial 完成后，先计算每次 repeat 的 split pass rate，再对两次 repeat 取平均。

## 实验完整性

- 任务版本：Terminal-Bench 2.0 固定归档 commit `69671fbaac6d67a7ef0dfec016cc38a64ef7a77c`。
- 可靠任务根只修改依赖引导，任务说明和测试断言哈希审计无差异。
- 诊断和 proposer 只能读取 train 轨迹；heldout 仅用于 acceptance gate。
- 当前图表不替代最终论文复现结论。
"""
    (OUTPUT / "REPORT.md").write_text(report, encoding="utf-8")


def write_html() -> None:
    html = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>Clean64 动态复现报告</title>
<style>body{margin:0;background:#f2f4f9;color:#24293a;font-family:'Microsoft YaHei',sans-serif}main{max-width:1300px;margin:auto;padding:36px}section{background:white;border-radius:16px;padding:16px;margin:22px 0;box-shadow:0 8px 25px #28304a12}img{width:100%}a{color:#4358c8}</style></head><body><main>
<h1>Self-Harness Clean64 动态复现报告</h1><p>报告由 case checkpoint 自动聚合；完整数据血缘见 provenance.json。</p>
<section><img src="dashboard.png"></section><section><img src="completion.png"></section><section><img src="observed_pass_rates.png"></section><section><img src="retry_audit.png"></section>
<p><a href="REPORT.md">文字报告</a> · <a href="provenance.json">数据血缘</a></p></main></body></html>"""
    (OUTPUT / "index.html").write_text(html, encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
