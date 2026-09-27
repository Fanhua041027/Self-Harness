# Clean64 严格补跑计划

生成依据：`paper/generated/rerun-plans/*.json`。以下时间是按 `timeout_s=3900`、`infrastructure_retries=2`、`case_concurrency=4` 计算的配置上界，不是实际 API 费用或预计完成时间。

| 目标 | Invalid | 最多模型尝试 | 配置墙钟上界 | 主要原因 |
|---|---:|---:|---:|---|
| DeepSeek baseline | 51 | 153 | 41.44 h | timeout、余额不足、缺失 reward、字段不一致 |
| DeepSeek `git_proxy_url_rewrite` | 72 | 216 | 58.50 h | timeout、连接失败、缺失 trial/reward、依赖错误 |
| DeepSeek `required_output_file_exists` | 128 | 384 | 104.00 h | 无有效评测单元 |
| DeepSeek `missing_runtime_install` | 128 | 384 | 104.00 h | 无有效评测单元 |
| Qwen baseline | 105 | 315 | 85.31 h | 大量连接失败、欠费、wrapper 异常及 3 个显式 invalid |
| Qwen `anti_workaround_execution` | 78 | 234 | 63.38 h | 52 个历史 failed 无 reward、25 个显式 invalid、1 个字段不一致 |
| Qwen `validation_bypass_verification` | 128 | 384 | 104.00 h | 无有效评测单元 |
| Qwen `dangerous_config_interrupt` | 128 | 384 | 104.00 h | 无有效评测单元 |
| **完整消融合计** | **818** | **2454** | **664.63 h** | - |

## Qwen 严格 promotion 的最小范围

只需补跑 Qwen baseline 和 `anti_workaround_execution` candidate，共 183 个有效 invalid evaluation cells，最多 549 次模型尝试，配置墙钟上界合计 148.69 h。DeepSeek 补跑用于重新确认其拒绝结论，但不阻塞 Qwen 的严格判断。

从投入产出比看，优先完成 Qwen baseline 与历史接受候选；两个 Qwen 拒绝候选和两个 DeepSeek 候选均没有有效评测单元，不能再称为“已经完整”。DeepSeek 全量补跑上界为 307.94 h；全部八组消融的配置上界为 664.63 h。若论文不要求跨模型完整候选消融，应将未完成候选保留在审计附录而不是效应表。

### 配对分阶段执行

- 当前双方有效：18/128 对。
- Phase 0 固定基础设施 canary：baseline 1 + candidate 1；只检查数值 verifier 结果，不依据 pass/fail 决策。
- Phase 1 单侧补齐：baseline 32 + candidate 5。
- Phase 2 双侧补齐：baseline 73 + candidate 73。
- 每阶段后重新生成计划；过期 manifest 会被拒绝。阶段划分不减少 183 个确认性补跑单元。

精确阶段清单见 `paper/generated/paired-rerun/PAIRED_RERUN_PLAN.md` 及同目录 6 个 JSON manifest。

## 安全属性

- 默认命令仅生成计划，不读取 API key、不启动 Docker、不调用模型。
- `--execute` 前验证结果模型与 `SELF_HARNESS_MODEL` 完全一致。
- candidate 补跑显式设置并验证 `SELF_HARNESS_CANDIDATE_WORKSPACE`。
- 原 Harbor job 目录不删除；旧 JSON checkpoint 归档至 `rerun_history/<timestamp>/`。
- `--reuse-existing` 保留全部有效单元，仅运行清除 checkpoint 的 invalid 单元。
- 补跑后仍存在 invalid 时返回非零状态，严格 acceptance 继续阻断。

## 命令

```powershell
# 仅生成计划（零模型调用）
.\workflow\scripts\rerun_clean64_invalid.ps1 -Label qwen
.\workflow\scripts\rerun_clean64_invalid.ps1 -Label qwen -CandidateId anti_workaround_execution

# 审核计划后显式执行
.\workflow\scripts\rerun_clean64_invalid.ps1 -Label qwen -Execute -Foreground
.\workflow\scripts\rerun_clean64_invalid.ps1 -Label qwen -CandidateId anti_workaround_execution -Execute -Foreground

# 推荐先运行固定 canary；下列命令仍是 dry-run，付费执行需显式追加 -Execute -Foreground
.\workflow\scripts\rerun_clean64_invalid.ps1 -Label qwen -CellsFile .\paper\generated\paired-rerun\phase_0_infrastructure_canary.baseline.json
.\workflow\scripts\rerun_clean64_invalid.ps1 -Label qwen -CandidateId anti_workaround_execution -CellsFile .\paper\generated\paired-rerun\phase_0_infrastructure_canary.candidate.json

# 两侧 invalid 均为 0 后执行严格验收并重建论文表格
.\workflow\scripts\finalize_clean64_strict.ps1 -Label qwen -CandidateId anti_workaround_execution
```

逐单元清单见：

- `paper/generated/rerun-plans/deepseek-baseline.json`
- `paper/generated/rerun-plans/qwen-baseline.json`
- `paper/generated/rerun-plans/qwen-anti_workaround_execution.json`
- `paper/generated/rerun-plans/qwen-validation_bypass_verification.json`
- `paper/generated/rerun-plans/qwen-dangerous_config_interrupt.json`
- `paper/generated/rerun-plans/deepseek-git_proxy_url_rewrite.json`
- `paper/generated/rerun-plans/deepseek-required_output_file_exists.json`
- `paper/generated/rerun-plans/deepseek-missing_runtime_install.json`
