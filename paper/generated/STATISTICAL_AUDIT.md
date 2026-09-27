# Clean64 实验统计审计

历史固定分母数值把基础设施无效尝试按未通过计入，只用于说明旧流水线为何产生相应决策。评测单元有效性判定同时检查显式 status、verifier reward、runtime failure 和字段一致性。当前所有比较均含 unresolved invalid，因此置信区间和显著性不作确认性解释；共同有效样本也仅作为高度受选择影响的诊断。

| 模型 | Split | Baseline | Final | Δ | 无效样本 B→F | 历史改善/退化对 | 95% CI | 任务级置换 p |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| DeepSeek V4 Flash | train | 12.79% | 12.79% | 0.00% | 45→45 | 0/0 | incomplete | incomplete |
| DeepSeek V4 Flash | heldout | 64.29% | 64.29% | 0.00% | 6→6 | 0/0 | incomplete | incomplete |
| Qwen3.7 Plus | train | 1.16% | 12.79% | 11.63% | 84→64 | 11/1 | incomplete | incomplete |
| Qwen3.7 Plus | heldout | 33.33% | 57.14% | 23.81% | 21→14 | 11/1 | incomplete | incomplete |

## 共同有效样本敏感性分析

| 模型 | Split | 共同有效 n | Baseline | Final | Δ |
|---|---|---:|---:|---:|---:|
| DeepSeek V4 Flash | train | 41 | 26.83% | 26.83% | 0.00% |
| DeepSeek V4 Flash | heldout | 36 | 75.00% | 75.00% | 0.00% |
| Qwen3.7 Plus | train | 0 | - | - | - |
| Qwen3.7 Plus | heldout | 18 | 72.22% | 88.89% | 16.67% |

## 候选门控结果

| 模型 | 候选 | 机制 | Train Δ | Heldout Δ | 决策 | 来源 | Invalid B→C | 严格门控可复现 |
|---|---|---|---:|---:|---|---|---:|---|
| DeepSeek V4 Flash | `git_proxy_url_rewrite` | `prompt_instruction` | 3.49% | -2.38% | rejected | historical | 51→72 | 否 |
| DeepSeek V4 Flash | `required_output_file_exists` | `prompt_instruction` | -12.79% | -64.29% | rejected | historical | 51→128 | 否 |
| DeepSeek V4 Flash | `missing_runtime_install` | `prompt_instruction` | -12.79% | -64.29% | rejected | historical | 51→128 | 否 |
| Qwen3.7 Plus | `anti_workaround_execution` | `prompt_instruction` | 11.63% | 23.81% | accepted | historical | 105→78 | 否 |
| Qwen3.7 Plus | `validation_bypass_verification` | `prompt_instruction` | -1.16% | -33.33% | rejected | historical | 105→128 | 否 |
| Qwen3.7 Plus | `dangerous_config_interrupt` | `permission_interrupt` | -1.16% | -33.33% | rejected | historical | 105→128 | 否 |
