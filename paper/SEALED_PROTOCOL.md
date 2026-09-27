# Sealed21 独立泛化实验协议

## 目的

Clean64 的 Heldout 参与了 promotion gate，因此不能作为无偏最终测试。Sealed21 从本地 89 个 dependency-bootstrap 任务中选择从未进入 Clean64 的任务，并在任何 sealed 模型调用前冻结，用于检验严格接受后的 Qwen harness 是否能泛化到完全未参与诊断、提案和版本选择的任务。

## 冻结划分

- 任务全集：`runs/terminal-bench-2-reliable-v2` 中的 89 个目录。
- 排除：Clean64 的 64 个任务。
- 能力边界排除：含本地图片、视频、音频或 PDF 输入的任务。
- 最终 Sealed21：剩余全部 21 个纯文本任务，不进行人工挑选。
- 历史检查：冻结时现有 `runs/**/result.json` 中没有任何 Sealed21 case ID。
- 完整性：每个任务目录计算 SHA256；manifest 本身另有 SHA256 lock；Sealed TOML 必须逐字节等于由 manifest 任务顺序生成的规范配置。
- 覆盖差异：结果盲元数据审计显示 Clean64 与 Sealed21 的难度分布接近（TV=0.062），但类别分布差异明显（TV=0.480，Jensen–Shannon=0.320 bits）。Sealed21 不是类别分层随机样本。

权威文件：

- `configs/splits/sealed21.json`
- `configs/splits/sealed21.json.sha256`
- `eval/configs/harbor_local_sealed21.toml`
- `configs/experiments/ei_confirmation_v1.json`

## 执行前置条件

1. Qwen Clean64 baseline 当前识别出的 105 个有效 invalid 必须在 Sealed 执行前全部解决（当前尚未解决）。
2. `anti_workaround_execution` 当前识别出的 78 个有效 invalid 必须在 Sealed 执行前全部解决（当前尚未解决）。
3. `finalize_clean64_strict.ps1` 生成 `acceptance.strict.json` 且决策为 `accepted`。
4. materialized candidate harness 计算并冻结 SHA256。
5. `build_sealed_split.py --verify` 通过。

任一条件不满足时，sealed 启动器必须退出且不得调用模型。

API key 门控使用 Windows User 环境变量 `SELF_HARNESS_QWEN_API_KEY`；空字符串或仅含空白字符均视为不可用，不能进入付费执行。冻结环境快照还必须包含带时区时间戳、OS/架构、Python、Harbor、Docker client/server、Git HEAD/dirty 状态、模型和 HTTP(S) endpoint；字段缺失、空白或类型不符时，分析器拒绝生成确认性统计。

## 执行约束

- baseline 与 frozen candidate 各运行 21 个任务 × 2 attempts，共 84 个评测单元。
- 两侧使用相同模型、任务、attempt 数、预算、Harbor/Docker 环境和 evaluator。
- 每侧结果绑定由 Sealed manifest SHA256、预注册文件 SHA256、Sealed TOML SHA256、baseline/candidate 角色和 harness surface SHA256 组成的 `run_identity`。运行器在解析配置前先核验原始 TOML 哈希，并把该哈希写入 marker 和最终结果。`--reuse-existing` 仅在身份与配置哈希均完全一致时允许复用 checkpoint；有旧结果但无身份标记时拒绝复用。
- `run_identity` 还绑定实际生成结果的 runner、Harbor wrapper 和 backend bridge 源码 bundle SHA256。freeze 另行记录分析器、统计核心、划分验证器和有效性分类器的逐文件/组合哈希。
- 执行环境快照记录 UTC 时间、OS/架构、Python、Harbor、Docker client/server、Git HEAD/dirty 状态、模型别名和 endpoint；不记录 API key 内容。
- 项目 Python 与 Harbor 自身 Python 分别执行排序后的 `pip freeze --all`，同时记录解释器和 Harbor CLI 可执行文件 SHA256；稳定 dependency bundle SHA256 纳入两侧 `run_identity`。捕获时间和机器路径保留在审计记录中，但不影响恢复同一依赖环境时的身份稳定性。
- 付费执行默认要求严格验收、Harbor、Docker、API key 和 clean Git worktree 同时就绪。dirty worktree 默认阻断；显式 `-AllowDirtyWorktree` 例外会写入计划和 freeze。
- baseline 与 candidate 必须由同一启动器连续执行；两侧完成前不得读取或发布单侧结果。
- Sealed21 结果不得回流到 diagnosis、proposal、acceptance 或 harness 修改。
- 基础设施 invalid 最多补跑 2 次，不替换任务、不插补分数。
- 仍有 invalid 时，结果状态为 incomplete，不计算确认性结论。
- 按 outer timeout、并发数和每单元最多 3 次模型尝试计算，84 个评测单元的配置墙钟上界为 52.5 wall-clock hours；该值是资源规划上界，不是 API 费用估计。
- execution plan 的评测单元数、timeout、并发、基础设施重试数和 52.5 小时上界必须从签入的 Sealed TOML 重新计算并与计划一致，不能只依赖启动器中的摘要常量。

## 预注册主分析

- 主效应：candidate pass rate − baseline pass rate。
- 分析单位：任务；同一任务的两次 attempt 保持为一个 cluster。
- 95% CI：按任务聚类 Bootstrap，20,000 次；分位数采用排序样本上的 `q*(n-1)` 线性插值（Hyndman–Fan type 7）。
- 主检验：双侧任务级配对符号置换检验，`α=0.05`。
- 主检验使用有理数任务效应的动态规划枚举精确零分布，不在 21 个非零任务时降级为 Monte Carlo 近似。
- 成功标准：`delta > 0`、95% CI 下界大于 0、`p < 0.05`。
- 辅助检验：attempt 级精确 McNemar，仅作诊断。
- 主假设仅一个，不做多重比较校正。

若成功，可表述为“冻结后的 Qwen harness 在独立 Sealed21 上取得显著正向泛化效应”。若失败，只保留 Clean64 描述性结果，不声称独立泛化。

这里的“泛化”严格限定为结果盲、互斥的本地纯文本 Sealed21：不能外推为完整 Terminal-Bench 类别总体、媒体输入任务或跨模型普适性。类别差异不用于事后重加权、替换任务或改变主终点。覆盖表和图见 `paper/generated/split-coverage/`。

## 容器镜像限制

结果盲静态审计覆盖本地 89 个任务及 90 个 `FROM` 引用。所有引用均为显式版本 tag，但没有任何 `@sha256:` digest，因此 0/89 个任务能从 Dockerfile 静态证明基础镜像内容不可变。任务目录哈希能证明 Dockerfile 未变，不能证明 registry tag 在不同时间解析到同一镜像。由于 Sealed21 已冻结，本实验不事后改写任务 Dockerfile；正式结果必须把这一点作为复现限制，并在实际执行产物中保留 Docker/Harbor 版本和时间。完整审计见 `paper/generated/container-audit/`。

baseline 与 candidate 均完成后、统计分析前，启动器调用 `capture_container_resolution.py` 对 Sealed21 使用的基础镜像 tag 执行只读 `docker image inspect`。该步骤明确不 pull 镜像，记录本地 image ID、RepoDigest、RepoTag、创建时间、OS/架构以及引用这些镜像的任务，并生成稳定 resolution bundle。分析器会从冻结任务目录重新解析 Dockerfile，并要求快照中的基础镜像引用集合与之完全相等；随后核验引用唯一性、引用总数与条目数、已解析数与 `found` 标志、任务 ID 排序唯一性以及 `complete` 一致性。解析完整度单独报告：缺失本地 tag 不改变预注册统计成功规则，但必须标记容器内容来源证据为 incomplete，不能隐去。

分析前还必须重新计算 21 个任务目录内容哈希与全部冻结执行/分析源码哈希，并核验所有 JSON 对象 key 唯一、manifest 为 `self_harness.sealed_split.v1`、恰有 21 个唯一任务 ID、`task_count=21`、结果盲分类且 `prior_result_references=0`；结果必须包含且仅包含外层 repeat=1、repeat=2 两条记录，每个 repeat 的 `passed/total` 必须与其 `case_results` 逐项重算一致，且每个 case 的 repeat 必须与外层记录一致；84 个结果单元的 `passed` 必须是 JSON 布尔值且必须存在非空有限数值 verifier reward，不能用字符串真值替代，也不能把缺失 reward、`NaN` 或 `Infinity` 当作行为失败。随后核验 freeze 中的 Sealed manifest、规范 TOML、预注册文件、严格验收产物和两份 harness surface 哈希。baseline/candidate 结果中的配置哈希与角色化 `run_identity` 必须分别完全匹配。PowerShell 写出的 execution plan 和 freeze 使用无 BOM UTF-8，分析器同时兼容旧版带 BOM JSON，避免跨运行时编码差异造成分析阶段失败。缺少任一证据时不生成确认性结论。

分析器不能只依赖预注册文件 SHA256：进入正式统计前还必须解析预注册内容，重新验证模型、candidate、21-task/42-attempt 设计、结果盲属性、Bootstrap seed 以及 missingness policy；语义不一致时即使文件哈希正确也必须拒绝分析。
预注册的 primary endpoint 还必须与实现一致：每任务两次 attempt 的 verifier pass fraction、20,000 次任务聚类 Bootstrap、双侧任务级配对符号置换、`α=0.05` 以及 `delta > 0`、CI 下界大于 0、`p < 0.05` 的成功规则均不得漂移。

统计完成后还必须再次核验预注册、规范 TOML、严格验收产物、两侧 harness surface、执行/分析源码 bundle 以及 21 个任务目录哈希；任一冻结来源在分析期间变化都必须拒绝写出结果。
strict acceptance artifact 还必须是 `self_harness.acceptance_gate.v1`，其 `accepted` 与 `decision` 一致、`source_hashes_stable=true`，且内部 baseline/candidate 路径和 SHA256 必须分别匹配 freeze 中绑定的 Clean64 源结果。
分析器不会只信任 artifact 中的摘要：它会使用 freeze 绑定的两份 Clean64 `result.json`，通过同一个 `run_acceptance_gate.py` 重新计算 `rule`、Train/Heldout comparison、delta、reason 和 decision，并逐字段比较；该 acceptance gate 源码也纳入 Sealed analysis code bundle。
同一 verifier 还会在 launcher 设置 `strictReady`、调用任何模型前执行；重算失败只会生成阻断状态，不会启动付费 Sealed21 运行。
其 `rule` 必须声明 train/Heldout、每侧 2 次 repeat、pass-rate 指标和“无 split drop 且至少一个 split 改善”的判定规则；两个 split 的 comparison 必须各包含 repeat=1/2 的 baseline/candidate 汇总和有限 delta。

正式输出的 `sealed_analysis.v6` / `sealed_statistics.json` 必须在验证前后锁定并比较 baseline/candidate 原始 `result.json`、Sealed manifest、freeze 和容器解析快照的 SHA256，任一输入在分析期间改变则拒绝分析；同时记录这些输入哈希、21×2×2 的评测单元数、Bootstrap 重采样次数、seed 和 `q*(n-1)`（Hyndman–Fan type 7）分位数方法、精确任务级置换方法、两侧 invalid 计数及容器解析状态。`SEALED_REPORT.md` 还必须重复设计规模、完整性摘要和全部输入哈希，避免只保留难以审计的单个比例或 p 值。
报告中的极小精确 p 值必须使用科学计数法保留非零数值，不得将可达的最小 p 值四舍五入显示为 `0.0000`。

## 设计分辨率审计

在不读取任何 Sealed21 结果的前提下，`paper/audit_confirmation_design.py` 对一个透明的规范模式进行分辨率审计：每个有利任务仅有一次 repeat 从 fail→pass，另一次不变，其余任务不变。这不是功效模型或预期结果。在该模式下，至少需要 6 个同方向有利任务簇，才会同时得到 `p=0.03125`、正的 Bootstrap CI 下界和 14.29 pp 的 attempt-level 差值。该边界说明 Sealed21 能确认分布在多个任务上的中等改善，但对更稀疏或相互抵消的效应可能没有足够分辨率。完整表见 `paper/generated/design-audit/CONFIRMATION_DESIGN_AUDIT.md`。

## 5-repeat 的定位

原论文每个 harness candidate 使用 2 次 repeated attempts。本复现的确认性主路径同样采用 2 次，并通过任务聚类推断处理 repeat 相关性。Clean64 的 5-repeat 扩展保留为计算预算允许时的精度/稳定性分析，不再作为 EI 投稿完成的硬性门槛；它不能替代独立 Sealed21。
