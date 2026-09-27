# 4 实验

> 本节依据 `Self-Harness.pdf` 的实验协议撰写，但当前仓库只完成了 Clean64 历史流水线与有效性审计，尚未完成可解释的替代模型效应复现。文中的固定分母数值不能表述为原论文结果的直接复现，也不能在大规模有效 invalid 补跑前表述为候选效应。

## 4.1 研究问题

本实验围绕以下三个问题展开：

- **RQ1：有效性。** Self-Harness 能否在保持模型参数、评测器和任务集合不变的条件下提高智能体的任务通过率？
- **RQ2：非退化与迁移。** 基于 Train 轨迹提出的 harness 修改能否同时改善未向 proposer 暴露轨迹的 Heldout 集合，且不引入可测量的性能回退？
- **RQ3：模型特异性。** 不同模型能否提出并筛选出不同的 harness 修改，而不是稳定地产生同一种通用提示词？

## 4.2 数据集与划分

实验采用 Terminal-Bench-2.0 的 Clean64 子集，共 64 个可在当前环境稳定启动的终端任务。任务集合在实验开始前固定划分为 43 个 Train 任务和 21 个 Heldout 任务。Train 任务的执行轨迹、验证器结果及失败证据可用于 weakness mining 和 harness proposal；Heldout 轨迹不提供给 proposer，只用于候选 promotion gate。所有 harness 变体使用完全相同的任务划分，每个任务均从新的 Harbor/Docker 环境启动。

每个 harness 状态在每个任务上独立运行 2 次。因此，一个完整状态包含 86 个 Train 评测单元和 42 个 Heldout 评测单元，共 128 个评测单元。两个模型的 baseline 及各 3 个候选共形成 1,024 个预定评测单元；基础设施重试不作为新的统计样本。

需要强调，本文的 Heldout 表示“对 proposer 隐藏的回归验证集”，但其得分参与候选选择，因此不是完全独立的最终测试集。本文将其解释为 out-of-evidence validation，而不将其表述为无偏的最终泛化估计。

## 4.3 对比模型与实现细节

受原论文模型服务和算力可用性限制，本实验选用 `deepseek-v4-flash`（文中记为 DeepSeek V4 Flash）和 `qwen3.7-plus`（文中记为 Qwen3.7 Plus）作为替代模型。对每个实验分支，执行任务、诊断失败和生成候选均使用同一个目标模型，模型权重在整个循环中保持不变。该设置检验的是“模型能否改善自身 harness”的机制，而不是不同强度模型之间的蒸馏。

初始 harness 基于 DeepAgent，仅保留文件读写、文本编辑和 shell 执行等基础能力。允许的修改被限制在声明的 harness surface 内，每个候选至多修改一个组件和一个文件。复现配置设 proposal width `K=3`、最大演化轮数 `T=2`，每个状态运行 2 次。Harbor 以 Docker 为执行环境，任务并发数为 4，单次 agent hard timeout 为 1,800 s，外层任务 timeout 为 3,000 s。实验使用 Windows 11、Docker Desktop/WSL2 和 Harbor 0.20.0；API 凭据仅从环境变量读取，不写入运行产物。

候选接受规则与原论文一致。当前实现首先要求 baseline 与 candidate 不含 unresolved infrastructure-invalid 单元；否则立即中止验收。通过该完整性检查后，设候选在 Train 和 Heldout 上相对父 harness 的通过率变化分别为 $\Delta_{tr}$ 和 $\Delta_{ho}$，仅当

$$
\Delta_{tr}\geq 0,\quad \Delta_{ho}\geq 0,\quad
\max(\Delta_{tr},\Delta_{ho})>0
$$

时接受候选。否则保留父分支，并记录拒绝原因和完整评测产物。

## 4.4 指标与统计方法

主要指标为 Pass (%)，即通过 Terminal-Bench-2.0 最终容器状态验证器的评测单元比例。为复核已经生成的历史分数，描述性主表沿用当时的固定分母口径，将基础设施无效单元按未通过计入；但当前严格 acceptance gate 不再允许这些结果用于 promotion。作为敏感性分析，本文另外报告 baseline 与 candidate 均有效的共同样本结果。

有效性审计不只读取历史 `status`，还要求存在数值 verifier reward、没有 runtime failure，并检查 reward、passed 与 status 的一致性。统计、Sealed、acceptance gate、invalid rerun 和 `result_validity` CLI 现在共用同一个严格 JSON object loader：兼容 BOM、拒绝任意嵌套重复 object key，并要求根节点为 object。历史 Clean64 审计保留无 `reward` 字段旧夹具的兼容模式；acceptance、付费补跑、配对计划和实时报告显式启用 strict reward 模式，缺失字段直接作为 invalid。当前真实 Clean64 的 512 个 baseline/final 单元均包含 `reward`，因此该模式不会改变历史计数。只有 baseline 和 candidate 均为零 unresolved invalid 时，才执行任务聚类 Bootstrap（20,000 次）和双侧任务级配对符号置换检验；未通过门禁时，`statistics.json` 中的 CI、McNemar p 和任务级 permutation p 均写为 `null`，并记录阻断原因，避免下游工具误用不具确认性的数值。当前比较均不满足此前提，因此下表只报告历史固定分母分数，不报告确认性置信区间或显著性。

## 4.5 历史固定分母结果

| 模型 | Split | Baseline | Candidate | 历史 $\Delta$ | 有效 invalid B→C | 推断状态 |
|---|---|---:|---:|---:|---:|---|
| DeepSeek V4 Flash | Train | 12.79% | 12.79% | +0.00 pp | 45→45 | incomplete |
| DeepSeek V4 Flash | Heldout | 64.29% | 64.29% | +0.00 pp | 6→6 | incomplete |
| Qwen3.7 Plus | Train | 1.16% | 12.79% | +11.63 pp | 84→64 | incomplete |
| Qwen3.7 Plus | Heldout | 33.33% | 57.14% | +23.81 pp | 21→14 | incomplete |

旧流水线曾把 Qwen3.7 Plus 的 `anti_workaround_execution` 记录为 Train +11.63 pp、Heldout +23.81 pp，但该数字主要处在大规模基础设施缺失背景下：baseline 有效 invalid 为 105/128，candidate 为 78/128。典型历史误标包括 API 连接失败、账户欠费、wrapper 异常或缺失 verifier reward 被写为普通 `failed`。Qwen Train 没有任何一对 baseline/candidate 同时有效的评测单元，Heldout 仅有 18 对共同有效单元，因此原先的 CI、置换检验和 McNemar 显著性均不可解释，历史接受决定只能作为流水线审计记录。

DeepSeek V4 Flash baseline 同样包含 51 个有效 invalid。其 3 个候选的历史拒绝决定不能证明修改无效，只能说明旧固定分母流水线没有接受它们；完整补跑前不进行跨模型效应比较。

主结果图可直接采用 [`runs/clean64-final-report/01_performance.png`](../runs/clean64-final-report/01_performance.png)，建议图注为：

> **图 4.** Clean64 上 baseline 与历史 active candidate 的固定分母流水线分数。Qwen 的表面差值伴随 baseline 105 个、candidate 78 个有效 invalid，不构成候选效应或严格 promotion 证据。

## 4.6 候选门控与模型特异性分析

| 模型 | 候选修改 | 类型 | Train $\Delta$ | Heldout $\Delta$ | 历史决策 | Invalid B→C | 严格复现 |
|---|---|---|---:|---:|---|---:|---|
| DeepSeek V4 Flash | `git_proxy_url_rewrite` | prompt instruction | +3.49 pp | -2.38 pp | 拒绝 | 51→72 | 否 |
| DeepSeek V4 Flash | `required_output_file_exists` | prompt instruction | -12.79 pp | -64.29 pp | 拒绝 | 51→128 | 否 |
| DeepSeek V4 Flash | `missing_runtime_install` | prompt instruction | -12.79 pp | -64.29 pp | 拒绝 | 51→128 | 否 |
| Qwen3.7 Plus | `anti_workaround_execution` | prompt instruction | +11.63 pp | +23.81 pp | 接受（历史） | 105→78 | 否 |
| Qwen3.7 Plus | `validation_bypass_verification` | prompt instruction | -1.16 pp | -33.33 pp | 拒绝 | 105→128 | 否 |
| Qwen3.7 Plus | `dangerous_config_interrupt` | permission interrupt | -1.16 pp | -33.33 pp | 拒绝 | 105→128 | 否 |

历史运行的 6 个候选中仅 `anti_workaround_execution` 被标记为接受。该修改针对 Qwen3.7 Plus 通过禁用校验规避配置问题的诊断，但直接目标 `mailman#repeat-02` 在 baseline 和 candidate 下均未通过；`repeat-01` 则是 baseline API 失败、candidate 通过，不能作为机制改善证据。因此当前只能确认“诊断产生了相应修改”，不能确认该机制导致了任务改善。

DeepSeek 三个候选分别含 72、128 和 128 个有效 invalid，其中后两个没有任何有效评测单元。其 $\Delta$ 只能视为历史流水线记录，不能解释为候选真实效应，也不能用来证明 gate 成功拒绝了低质量修改。

候选决策图可采用 [`runs/clean64-final-report/03_candidate_decisions.png`](../runs/clean64-final-report/03_candidate_decisions.png)。

诊断到候选再到直接目标结果的证据链见 [`paper/generated/mechanism/MECHANISM_EVIDENCE.md`](generated/mechanism/MECHANISM_EVIDENCE.md) 和 [`mechanism_evidence.svg`](generated/mechanism/mechanism_evidence.svg)。该审计将“修改与诊断一致”与“修改机制已被结果确认”分开，避免把全局相关性误写成因果机制证据。

## 4.7 稳健性与无效样本分析

Qwen baseline 与 candidate 的有效 invalid 分别为：Train `84→64`，Heldout `21→14`。共同有效分析在 Train 上为 $n=0$，无法计算；Heldout 仅保留 18/42 个配对单元，观察到 72.22%→88.89%（+16.67 pp），但这是由基础设施可用性共同决定的选择子集，不能替代完整配对估计。DeepSeek 共同有效样本为 Train 41、Heldout 36，但 active branch 未发生变化，不能提供候选效应证据。

因此当前不存在支持结果方向的完整敏感性分析。正式 promotion 的必要条件是补跑 Qwen baseline 的 105 个、candidate 的 78 个有效 invalid，并得到零 unresolved invalid 的完整配对结果。

两次 repeat 的离散度较大，尤其 Qwen final 的 Train 为 25.58% 与 0%，Heldout 为 71.43% 与 42.86%。这表明仅进行两次重复不足以精确估计绝对通过率。确认性主路径沿用原论文每个候选 2 次 repeated attempts 的设置，并优先把预算用于完全独立的 Sealed21；Clean64 的 5-repeat 扩展仅作为预算允许时的精度与稳定性分析，不作为独立泛化结论的替代证据。

## 4.8 有效性威胁

**外部效度。** 本实验使用两个替代模型和 Clean64 子集，而非原论文的三个模型及完整 Terminal-Bench-2.0，故不能据此声称复现原论文绝对分数。仅两个模型中一个获得提升，也不足以建立跨模型普适性。结果盲元数据审计完整记账本地 89 个任务：Clean64 64 个、Sealed21 21 个、媒体能力边界排除 4 个，二者任务重叠为 0。难度分布接近（total variation 0.062；Clean64/Sealed21 hard 为 32.8%/33.3%），但类别分布差异明显（total variation 0.480，Jensen–Shannon divergence 0.320 bits）：Sealed21 的 data-science 为 23.8%（Clean64 3.1%），且缺少 Clean64 的 7 个类别。因此即使 Sealed21 成功，也只能声称对该结果盲、互斥的本地纯文本集合泛化，不能外推到完整 Terminal-Bench 类别总体或媒体输入任务。完整审计见 [`paper/generated/split-coverage/SPLIT_COVERAGE_AUDIT.md`](generated/split-coverage/SPLIT_COVERAGE_AUDIT.md) 和 [`split_coverage.svg`](generated/split-coverage/split_coverage.svg)。

**内部效度。** Qwen baseline 的 105/128 个单元和 candidate 的 78/128 个单元无有效 verifier 结果；这不是轻微缺失，而是足以使现有候选效应不可识别的系统性中断。API 服务状态、账户状态、网络、wrapper 和 Docker 调度均是已观察到的混杂来源。

**实现与后端版本效度。** 确认实验将 runner、wrapper、bridge、分析器、有效性分类器和规范配置的 SHA256 纳入 freeze，并记录 Python、Harbor、Docker、OS、Git 和运行时间；分析器还会拒绝缺少带时区时间戳、模型、endpoint、Docker client/server 或 Git 状态等字段的环境快照。项目与 Harbor Python 的完整排序包清单、解释器及 Harbor CLI 可执行文件哈希组成 dependency bundle，并进入运行身份。容器静态审计发现 89 个任务的 90 个 `FROM` 引用全部使用显式版本 tag，但没有 `@sha256:` digest；因此 Dockerfile 可冻结，registry tag 实际解析内容仍可能跨时间漂移。正式两侧运行完成后会在不 pull 的条件下，对 Sealed21 的 5 个基础镜像引用记录本地 image ID/RepoDigest；分析器会从冻结 Dockerfile 重新解析引用集合并要求完全匹配，同时核验引用唯一性、引用与解析计数、任务 ID 和 `complete` 一致性。解析完整度作为独立来源指标报告，不改变预注册统计规则。当前运行前预览为 0/5，符合尚未构建任务镜像的状态。运行器把 execution plan 和 freeze 以无 BOM UTF-8 写出，分析器兼容旧版带 BOM JSON，避免 PowerShell 与 Python 交接时的编码差异造成分析阶段失败。远程模型也仍通过供应商别名 `qwen3.7-plus` 调用；若供应商在别名下更新权重或推理栈，本地无法独立固定或验证该变化。因此论文结论同时受基础镜像 tag 和模型别名在执行时刻稳定性的限制。容器审计见 [`paper/generated/container-audit/CONTAINER_REPRODUCIBILITY_AUDIT.md`](generated/container-audit/CONTAINER_REPRODUCIBILITY_AUDIT.md)。

**选择偏差。** Heldout 分数参与 promotion gate，因此它是回归验证集而非严格未触碰测试集。对最终泛化能力的无偏估计需要在 harness 冻结后增加 sealed split，且该 split 不得回流到诊断、提案或版本选择。

正式执行门控还要求 Windows User 环境中的 `SELF_HARNESS_QWEN_API_KEY` 非空白；空白凭据不会被当作可用 API key。Clean64 invalid rerun 启动器的前台和 detached 两条路径同样拒绝空白凭据。

**统计结论效度。** 在零 invalid 前不报告确认性 CI 或显著性。补跑完成后才按 43/21 个任务、每状态 2 次 repeat 执行任务聚类 bootstrap 和任务级置换检验。生成的 `statistics.json` 以结构化元数据记录该设计：`comparison_unit=task`、`observation_unit=attempt`、repeat ID 为 1/2、20,000 次任务级重采样及全局种子和确定性的模型/划分种子派生规则，并记录基于有理数任务效应的精确置换算法，防止正文、样本边界与实际估计器发生漂移。

## 4.9 可复现性

所有 baseline、候选、历史验收决定和分支均保存在 `runs/` 下，输入文件哈希记录于 `runs/clean64-final-report/provenance.json`。效应量图见 [`paper/generated/effect_sizes.svg`](generated/effect_sizes.svg)，无效样本审计图见 [`paper/generated/invalid_runs.svg`](generated/invalid_runs.svg)。运行

```powershell
python paper/analyze_experiments.py
python paper/audit_split_coverage.py
python paper/audit_container_reproducibility.py
python paper/audit_paper_consistency.py
```

可重新生成统计审计表、候选表及机器可读 JSON。生成文件位于 `paper/generated/`，其中 `statistics.json` 保存全部统计量、固定随机种子、有效性门禁策略、分析器和所有输入结果文件的 SHA256，并记录 `analysis_input_integrity.stable_before_after_analysis=true`；统计入口在开始和结束时重新哈希全部结果、队列及 acceptance 输入，期间任何文件变化都会拒绝生成新表。`summary.csv` 和 `candidates.csv` 可直接导入论文制表软件。描述性固定分母统计只把 JSON boolean `true` 计为通过，非布尔 `passed` 和非有限 `reward` 会先被标为无效，避免 Python truthiness 将格式错误伪装成模型通过。
`audit_paper_consistency.py` 会逐行比较中英文历史结果表、候选表与 `statistics.json`/配对补跑计划，并生成 [`paper/generated/PAPER_CONSISTENCY_AUDIT.json`](generated/PAPER_CONSISTENCY_AUDIT.json)，任何手工数字漂移都会使审计失败；同时要求统计输入稳定、配对 phase manifest 为 v3 且含完整源结果哈希，并要求每个单侧补跑计划记录 `source_result_stable=true` 和 64 位 SHA256。即使表格数字未变，缺失或过期 provenance 也会使审计失败。
审计还会验证推断门禁：只要任一汇总行仍有 unresolved invalid，McNemar p、任务级 permutation p 和 Bootstrap 区间字段必须为 `null`，每行必须记录阻断原因，且中英文正文都必须明确不报告确认性显著性结论。
此外，审计会从 `statistics.json` 和配对计划重新计算 Qwen/DeepSeek 总体 invalid、共同有效样本及配对缺失结构，并要求这些关键叙述数字同时出现在中英文正文中，避免表格未变但段落数字过期。
共同有效敏感性分析的比例和样本数也会被重新核对，包括 Qwen Heldout 的 72.22%→88.89% 以及 DeepSeek Train/Heldout 的共同有效单元数；该子集不能脱离生成统计单独修改。
审计同时锁定统计单位边界：每个 Clean64 汇总行必须对应 Train 43 个或 Heldout 21 个任务、每任务恰好 2 次 attempt，且 invalid/通过数有界、比例和差值能由同一分母重新计算。
对于 provenance，审计不会只相信布尔标志或哈希长度，而是解析 `statistics.json.source_files` 及配对/phase/单侧补跑计划中的源文件路径，重新计算 SHA256 并逐项比对。
审计器自身也会在读取前后锁定两份实验章节和全部生成计划的哈希，输出 `audit_input_integrity.stable_before_after_audit=true`；期间输入发生变化时不会写出 v3 报告。
对每个候选行，审计还会解析 `acceptance_path`，确认 acceptance 及其 baseline/candidate `result.json` 均属于 `statistics.json.source_files`，并从原始产物重新计算 decision、分 split delta、invalid 数和严格门控状态。
配对补跑计划也会在审计期间根据其绑定的两侧结果重新构建；pair counts、原因分类、各阶段 cells、重试限制、183 个待补单元和 148.69 h 预算必须逐项一致。
六个 phase manifest 还会逐 cell 与重建计划比对，并核对 phase/side 标签及源结果路径和哈希；即使总体计划未变，手工修改执行清单也不能通过。
生成的 `PAIRED_RERUN_PLAN.md` 和 `paired_rerun_cells.csv` 也会与同一 JSON 计划逐项比对，避免论文预算或 CSV 执行行单独漂移。
统计导出物也采用同一规则：审计从 `statistics.json` 内存重建 `summary.csv`、`candidates.csv` 和 `STATISTICAL_AUDIT.md`，并在论文审计通过前逐行/逐字节比对。`effect_sizes.svg` 与 `invalid_runs.svg` 也由同一 JSON 汇总重新生成并逐字节比较，禁止图形脱离表格单独修改。
机制证据链的 `MECHANISM_EVIDENCE.md`、`mechanism_transitions.csv`、`mechanism_evidence.svg`，以及结果盲划分覆盖的 `SPLIT_COVERAGE_AUDIT.md`、`split_coverage_counts.csv`、`split_coverage.svg`，也分别从各自 JSON 审计对象重建并逐字节比较；这 8 个补充产物同时纳入论文审计的稳定输入快照。
两个补充 JSON 还记录并重新核对规范 proposal/result、Clean64/Sealed manifest 及全部任务元数据的 SHA256；划分距离计算按排序后的标签求和，保证不同 Python 进程的浮点序列化稳定。
两个补充生成器与主统计、Sealed 路径共用严格 JSON object loader：兼容 BOM，拒绝嵌套重复 key，并要求根节点为 object，解析歧义不会进入图表或审计。
容器复现包 `container_reproducibility.json`、`CONTAINER_REPRODUCIBILITY_AUDIT.md` 和 `container_base_images.csv` 也由 89 个任务 manifest 与 89 个 Dockerfile 重建；其原始 SHA256 与派生产物均纳入同一论文审计。
结果盲设计分辨率包 `confirmation_design_audit.json` 与 `CONFIRMATION_DESIGN_AUDIT.md` 由固定设计脚本及精确的统计/有效性源码重建；其源哈希和 LF 规范化 UTF-8 输出也纳入审计。
执行前的 `sealed21_container_resolution.preview.json` 会与冻结的 21 个任务及 Dockerfile 引用集合重新核对；审计强制 `pull_performed=false`、恰好 5 个引用、排序后的使用绑定，并显式记录 `resolved_count/complete`，当前为 `0/5`、`complete=false`。
签入的 `sealed21-execution-plan.json` 也会重新核对 manifest、预注册文件、Sealed TOML、当前两侧 harness surface、执行/分析源码 bundle 及严格就绪状态；84 个评测单元和 52.5 h 配置上界不能脱离冻结输入独立漂移。审计还会把机器计划与 `SEALED_PROTOCOL.md` 及预注册缺失处理策略交叉核对，防止 repeat 数、重试上限或“不替换任务/不插补”规则静默分叉。
审计会根据签入 Sealed TOML 的任务数、repeat、timeout、并发和基础设施重试字段重新计算评测单元数与墙钟上界，启动器中的摘要常量不能低估实际配置预算。
结果展开器还会校验每个 case 的 `repeat` 与外层 repeat 标签一致、`case_id` 非空且单元键唯一；身份不一致的结果在统计和配对补跑计划生成前直接拒绝。
严格 acceptance 和 Clean64 最终报告还会从 `case_results` 重新计算每个 repeat 的 `passed/total`，汇总字段与逐单元结果不一致时拒绝生成验收或最终表格。
Sealed 分析器同样执行这一汇总一致性门禁：每个外层 repeat 的 `passed/total` 必须与逐 case 重算值一致，否则不生成确认性统计。
分析入口还会解析冻结的预注册内容，重新核对模型、candidate、21-task/42-attempt 设计、结果盲属性、Bootstrap seed 和 missingness policy；仅有正确文件哈希不能授权语义已改变的协议。`SEALED_REPORT.md` 会重复记录预注册、TOML、严格验收、harness surface、源码 bundle 和依赖 bundle 的哈希。
同时会校验主终点契约：每任务两次 attempt、20,000 次任务聚类 Bootstrap、双侧配对符号置换、α=0.05，以及三项成功标准必须与实现一致。
严格验收 artifact 也会按 `self_harness.acceptance_gate.v1` 解析：`accepted` 必须与 `decision` 一致，`source_hashes_stable` 必须为 true，内部两侧结果路径和 SHA256 必须与 freeze 绑定的 Clean64 源结果完全匹配。
分析器随后调用同一个 `run_acceptance_gate.py`，从这两份冻结的 Clean64 `result.json` 重新计算 `rule`、Train/Heldout comparison、delta、reason 和 decision，并逐字段比对；该 gate 源码同时纳入 Sealed analysis-code bundle，防止只替换验收摘要而不改变分析实现。
Sealed launcher 在任何模型调用前也执行同一 verifier；若重算失败，`strict_acceptance_recomputed=false` 且执行被阻断，不会先产生付费评测结果再发现验收漂移。
其 `rule` 还必须声明 Train/Heldout、每侧两次 repeat、pass-rate 指标及“无 split drop 且至少一个 split 改善”的判定规则；两个 split comparison 都必须包含 repeat=1/2 汇总和有限 delta。
统计计算完成后，分析器还会再次核验这些冻结来源及 21 个任务目录哈希；分析期间任何 provenance 或任务内容变化都会阻止结果文件写出。
极小的精确 p 值使用科学计数法输出，不会被四舍五入成 `0.0000`，从而区分非零概率和零概率。
Clean64 统计入口还强制要求 Train 43 个 task、Heldout 21 个 task、每个 task 恰有 repeat 1/2，且两个 repeat 的 task 集合一致；两侧同时缺失单元时也不会生成论文统计。

Sealed21 正式分析生成 `sealed_analysis.v6`，在验证前后比较 baseline/candidate 原始 `result.json`、Sealed manifest、freeze 和容器解析快照的 SHA256，任一输入在读取或统计期间发生变化则拒绝分析；`sealed_statistics.json` 同时记录全部输入哈希、21 个任务、2 次 repeat、两侧共 84 个评测单元、Bootstrap 次数与 seed、`q*(n-1)` 线性分位数规则（Hyndman–Fan type 7）、精确任务级置换方法、两侧 unresolved invalid 数及具体 key、显式 `analysis_status`（`complete` 或 `incomplete`）和容器解析状态。结果格式合法但仍有基础设施 invalid 时生成可审计的 `incomplete` 产物并将确认性统计保持为 null，不会误标为模型行为失败；格式损坏仍直接失败。`SEALED_REPORT.md` 重复这些设计、状态、完整性摘要和完整输入哈希集合，便于直接生成 EI 论文表格。

## 4.10 投稿完成度与补跑计划

| 检查项 | 当前状态 | 完成条件 |
|---|---|---|
| 实验代码与离线测试 | 已完成 | 完整 `pytest` 测试套件通过 |
| 历史结果与有效性审计 | 已完成 | 固定分母记录可重建；确认性推断保持 incomplete |
| Qwen 严格 promotion | **待补跑** | baseline 105 个、candidate 78 个有效 invalid 待全部解决 |
| DeepSeek 严格拒绝确认 | **高成本、待决策** | baseline 51 个及三个候选 72/128/128 个有效 invalid；完整上界 307.94 h |
| 5 次独立 repeat | **可选稳健性扩展** | 预算允许时扩展 baseline 与严格通过候选，不是确认性主路径的硬门槛 |
| Sealed21 泛化测试 | **已冻结、待执行** | 21 个未参与 Clean64 的纯文本任务；严格 promotion 通过后运行 baseline/candidate 各 2 次 |
| 执行代码与环境快照 | **已实现、待执行冻结** | clean Git snapshot、源码 bundle 哈希及 Python/Harbor/Docker 环境写入 freeze |

精确补跑清单见 [`paper/generated/RERUN_PLAN.md`](generated/RERUN_PLAN.md) 和 `paper/generated/rerun-plans/*.json`。Qwen 严格 promotion 的最小补跑范围是 183 个评测单元，最多 549 次模型尝试，配置墙钟上界合计 148.69 h。仅 DeepSeek 全量补跑需处理 379 个有效 invalid、上界 307.94 h；全部八组消融合计 818 个、上界 664.63 h。上述数值均不是预计 API 费用。补跑工具默认 dry-run，只有显式指定 `-Execute` 才会调用模型；完成后由 `finalize_clean64_strict.ps1` 以 `--require-reward` 预检，再由 acceptance gate 校验逐 case reward/aggregate 一致性并把两侧源 `result.json` 路径及 SHA256 写入 `acceptance.strict.json`，重建固定 Clean64 统计、刷新 Sealed21 readiness plan 并运行论文一致性审计；Sealed21 启动器还会重新核对这些路径/哈希，历史 acceptance 不会被覆盖。

为降低再次发生账户、连接或 wrapper 故障时的计算浪费，Qwen 的 183 个单元按预先生成的配对缺失结构分阶段执行：当前已有双方有效 18/128 对；单侧缺失为 baseline 32、candidate 5；双方均缺失 73 对。配对计划生成器会在读取前后重新哈希两侧源 `result.json`，只有 `source_hashes_stable=true` 才写出计划；计划和每个阶段清单均绑定两侧 SHA256 并记录目标 `side`，rerun 执行器会要求清单的 `side` 与实际 baseline 或 candidate workspace 一致后才允许生成计划或付费执行；cell 计划还显式记录 `selection_side` 和 `selection_phase`，避免把合法清单错用于另一 harness 角色或错误阶段。执行后，单侧计划会把执行前的源哈希与 `execution_provenance.post_source_result_sha256` 分开记录，并保存 evaluator return code 和执行后读取稳定性；因此更新后的结果不会被误认为计划的原始输入。Phase 0 执行后，计划会写入结构化 `canary_outcome`：只有具有有限数值 verifier reward 且 `passed/status` 一致的结果才算基础设施恢复；行为失败仍可作为有效 canary，provider 错误、超时、缺少 reward 或字段不一致则保持 `canary_ready=false`。因此只要结果文件变化或执行角色变化，就必须重新生成或拒绝旧计划。Phase 0 固定抽取每侧 1 个历史基础设施故障单元作为 canary（2 cells，最多 6 次尝试），仅检查能否得到数值 verifier 结果，不查看 pass/fail 决定后续；Phase 1 补齐 37 个单侧缺失单元；Phase 2 补齐双方缺失的 146 个单元。每阶段完成后必须从更新后的结果重新生成清单，过期清单会被执行器拒绝。分阶段只降低运行风险，不改变零 invalid、完整 128 个配对分析或 183 个待补单元的确认性要求。机器可读清单与阶段预算见 [`paper/generated/paired-rerun/PAIRED_RERUN_PLAN.md`](generated/paired-rerun/PAIRED_RERUN_PLAN.md)。

Sealed21 的结果文件进一步绑定 Sealed manifest、预注册文件、逐字节规范执行 TOML、结果生成源码 bundle、项目/Harbor Python 依赖 bundle、实验角色和 harness surface 的 SHA256 身份。运行器在解析 TOML 前核验原始配置哈希并写入 marker 与聚合结果；分析器拒绝重复 JSON key，首先要求 manifest 为 `self_harness.sealed_split.v1`、恰有 21 个唯一结果盲任务、`task_count=21` 且无既有结果引用，并要求外层 repeat 只有 1、2 且每个 case 的 repeat 标签匹配，再要求全部 84 个结果单元的 `passed` 为 JSON 布尔值且存在非空有限数值 verifier reward，再重新计算任务目录及执行/分析源码哈希，验证依赖锁，并要求两侧运行后的只读容器解析快照与 Sealed 任务集合完全匹配。缺失 verifier reward、`NaN` 或 `Infinity` 的基础设施失败不会被当作行为失败。PowerShell 生成的计划和 freeze 使用无 BOM UTF-8，分析器对旧版带 BOM JSON 保持兼容。freeze 同时记录 OS、Python、Harbor、Docker、Git、模型别名、endpoint 和 UTC 时间；付费执行默认要求 clean Git worktree。任何无身份 marker 的旧 checkpoint、依赖/配置/代码/任务内容变化、角色交换、harness/预注册/严格验收产物变化都会在复用或分析前被拒绝。任务级双侧符号置换检验采用有理数动态规划计算精确零分布，不因 21 个任务均为非零效应而退化为 Monte Carlo 近似。结果盲设计审计显示：在“每个有利任务仅一次 repeat 从 fail→pass、无退化”的规范稀疏模式下，至少 6 个有利任务簇（attempt-level +14.29 pp）才能同时满足正 CI 下界与精确 `p=0.03125`；这不是功效假设，而是对当前样本量可识别效应的透明边界。详见 [`paper/generated/design-audit/CONFIRMATION_DESIGN_AUDIT.md`](generated/design-audit/CONFIRMATION_DESIGN_AUDIT.md)。

Sealed21 已在任何 sealed 模型调用前按确定性规则冻结：从本地 89 个任务中排除 Clean64 的 64 个任务和 4 个含本地媒体输入的任务，纳入剩余全部 21 个纯文本任务；冻结时现有结果中没有这些 case ID。manifest、逐任务 SHA256、预注册主终点、缺失处理和禁止结果回流规则见 [`SEALED_PROTOCOL.md`](SEALED_PROTOCOL.md)。确认实验共 84 个统计评测单元；每个单元最多允许 2 次基础设施重试，因此按 outer timeout、并发数和最大 3 次模型尝试计算的配置墙钟上界为 52.5 h。启动器当前会因严格 promotion 尚未完成而阻止执行，因此本节尚不报告 Sealed21 结果。机器可读的 execution plan 可能记录 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_artifact_malformed`、`strict_acceptance_not_recomputed` 或 `source_snapshot_not_ready` 等执行阻断原因；这些是执行就绪诊断，不是模型结果。
