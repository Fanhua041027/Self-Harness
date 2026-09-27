# 实验优化日志

## 2026-09-03：静态预检与有界进程清理

- 发现：部分静态非法输入仍会先创建 work directory 与持久 lock；`run_command()` 接受 NaN/Infinity，Windows `taskkill` 和最终 `wait()` 没有 deadline，process-group 操作失败时缺少 direct-child fallback；lock sentinel/metadata 还假设单次 `os.write()` 完整写入。
- 修正：在目录创建前统一验证 eval config/显式输入文件、route count、candidate env name 和 surface 语法/重复名/regular-file 状态；新 run 缺少 surface 时无文件系统副作用。timeout/grace 必须 finite，异常清理使用 monotonic graceful/force deadline，tree termination 失败时回退 direct terminate/kill，所有 helper 与 reap 均有界，清理错误继续作为主异常 note。
- 锁 I/O：新增 short-write/EINTR 安全的 write-all，并统一识别 POSIX `EWOULDBLOCK` 与 Windows sharing/lock violation；未知 I/O 错误仍原样抛出，metadata 继续仅含受限 owner 信息。
- 边界：已有 work directory 的初始化、queue/recovery/reuse 等状态相关判断仍在 orchestrator lock 内；process cleanup 是 deadline-bounded best effort，而非对 detached descendant 的 Job Object 级强制 containment。
- 测试：增加非有限数拒绝、taskkill/signal 失败与 direct fallback、有界 final reap、short/interrupt/zero-progress write、跨平台 contention 映射及静态非法 CLI 无目录副作用测试。

## 2026-09-03：候选晋升的崩溃一致性

- 发现：候选接受后，旧实现先替换 `candidate_queue.json`、再替换 `branch_state.json`，两份独立原子写并不构成事务。中途崩溃可能留下指向不存在分支的终态 queue；反向残留状态重放时会生成 `-2` 后缀分支，并可能错误 supersede 与声明 parent 无关的当前 active 分支。
- 修正：新增单操作 write-ahead journal `finalization.intent.json`（`self_harness.finalization_intent.v1`）。由 parent、proposal bundle 哈希、候选 manifest 哈希和排序后的候选身份生成确定性 `finalization_id`；journal 在状态变更前冻结完整 child payload 与 queue updates。提交顺序固定为 prepared journal → branch state → queue → completed journal，启动时在持有 orchestrator lock 的条件下幂等 roll-forward。
- 不变量：重放复用原 branch ID 与 `created_at`，不再通过后缀规避冲突；首次应用必须确认声明 parent 是唯一 active branch，并只 supersede 该 parent。重复 finalization ID、branch ID 占用、queue 身份缺失/重复、冲突终态和 dangling accepted reference 均 fail closed，且应用先在深拷贝上完整验证，拒绝时不修改原状态。
- 兼容与边界：内部一致的 legacy queue/branch 记录仍可读取；缺少 journal 的 legacy split-brain 不做猜测式修复。该协议覆盖进程崩溃和普通写失败，但单文件 `fsync` 与原子替换不等价于缺少父目录 flush 时的完整断电持久性。
- 测试：加入 branch 写后失败、两份状态写完但 journal 未完成、queue-first 反向状态、重复重放、stale parent、冲突/重复 binding 和 dangling reference 的故障注入测试。

## 2026-09-03：工作目录独占编排锁

- 发现：最初的 `O_EXCL` marker 虽能阻止正常并发，但崩溃会留下永久 stale 文件；Windows 必须先关闭句柄再删除，仍存在路径替换竞态。原始 argv 预扫描还会在完整参数验证前产生目录/锁副作用，非 timeout 中断则可能释放锁但遗留仍在写入的子进程树。
- 修正：改为持久化 OS-backed lock 文件；POSIX 使用 advisory `flock`，Windows 使用 `msvcrt` sentinel-byte lock，释放时只 unlock/close、不再删除，进程崩溃由内核自动释放。CLI 先由唯一的 `argparse` 入口完整验证，再创建目录和加锁；外部命令在 timeout 及所有逃逸 `BaseException` 下终止并回收整棵进程树，同时保留原始异常。
- 状态恢复：expired candidate lease 统一清除 `claim_id`、`worker_id`、`lease_expires_at`、`claimed_at` 和 `stage`，恢复状态在新工作开始前持久化；候选 lease 继续作为独立的 crash-recovery 记录。
- 安全边界：冲突诊断只输出受限的非敏感 owner 字段；锁用于合作进程协调，不作为恶意本地账户的安全边界，Windows DACL 与网络文件系统限制在复现文档中明确说明。
- 测试：覆盖真实独立进程竞争、`os._exit` 崩溃后自动重获、持久化 metadata、异常/中断释放、主异常保留、畸形 CLI 无副作用、BaseException 子进程清理和 lease 字段完整清理。


## 2026-08-12：第136轮 - dependency lock schema 完整性边界

- 位置：`eval/scripts/capture_environment_lock.py`、`tests/test_environment_lock.py`、`tests/test_experiment_document_consistency.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：第135轮已经核对了三个 runtime binary 的版本和 SHA-256，但 `validate_lock()` 仍只验证格式字符串与 bundle 哈希；在重算 bundle 后，错误的路径类型、时间戳、包计数、包列表顺序/重复项或非法 digest 仍可能被接受，形成“哈希一致但锁结构不可解释”的边界。
- 修改：新增严格 lock schema 校验：要求 `captured_at` 为带时区 ISO-8601；三个 runtime 记录必须包含非空 executable/version 和 64 位 SHA-256；Python 快照的 `package_count` 必须为非负整数且与包列表长度一致，包名必须为非空字符串、大小写不敏感排序且无重复；CLI 记录明确走无包列表分支。校验先验证结构再验证 bundle，避免把结构损坏误报为普通内容漂移。
- 测试：新增包计数与 bundle 重算后的负向测试、未排序包列表测试、非法 digest 测试，以及 Sealed plan 级别的 schema 漂移测试；所有测试均不读取或写入 API key 内容。
- 验证：定向测试通过 66 项；刷新 Sealed21 计划后，执行代码 bundle 哈希同步更新；待完成全量 pytest、论文一致性审计、Split/容器/设计审计、compileall 与 `git diff --check`。Sealed21 仍保持 dry-run，`ready_to_execute=False`，阻断原因不变。

## 2026-08-12：第 135 轮 - runtime version 命令重算

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`、`paper/generated/sealed21-execution-plan.json`。
- 发现：第134轮已核对项目 Python、Harbor Python 和 Harbor CLI 的可执行文件 SHA256，但版本字段仍主要信任冻结字符串；binary 被替换后若伪造版本文本，单独字段比较无法发现。
- 修改：审计现在对当前磁盘上的三个 runtime binary 重新执行 `--version`，将命令 stdout 与 dependency lock 的冻结版本逐项比较；命令不可执行时得到明确不匹配而不是静默通过。新增 runtime version 篡改负向测试，继续不读取 API key 内容。
- 验证：论文一致性审计通过 1809 项；全量 pytest 通过 221 项；Split coverage、容器复现性、结果盲设计分辨率、compileall 和 `git diff --check` 通过；Sealed21 dry-run 仍为 `ready_to_execute=False`、84 个评测单元、未调用模型，阻断原因保持为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 2026-08-12：第 134 轮 - runtime binary SHA256 provenance

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`、`paper/generated/sealed21-execution-plan.json`。
- 发现：第133轮已绑定项目/Harbor Python 路径与版本，但可执行文件本身仍可能被替换而保持同一路径和版本字符串；dependency lock 已记录三个 runtime binary 的 SHA256，却没有与当前磁盘文件逐项比对。
- 修改：环境身份审计现在重新计算项目 Python、Harbor Python 和 Harbor CLI 当前磁盘文件 SHA256，并分别与 dependency lock 的 `executable_sha256` 比较；任一 binary 替换都会使审计失败。新增项目 Python runtime hash 篡改负向测试。
- 验证：论文一致性审计通过 1806 项；全量 pytest 通过 220 项；Split coverage、容器复现性、结果盲设计分辨率、compileall 和 `git diff --check` 通过；Sealed21 dry-run 仍为 `ready_to_execute=False`、84 个评测单元、未调用模型，阻断原因保持为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 2026-08-12：第 133 轮 - 项目 Python 身份与快照时间窗口绑定

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`、`paper/generated/sealed21-execution-plan.json`。
- 发现：第132轮已绑定 Harbor CLI/Python，但项目 Python executable/version 以及 dependency lock 与 execution environment 的具体捕获间隔仍未验证；两份独立合法快照可能被拼成一次环境证据。
- 修改：环境身份审计现在要求 dependency lock 的 `project_python.executable/version` 与 execution environment 的 `python_executable/python_version` 完全一致；Harbor CLI/Python 绑定继续保留；两份快照的 `captured_at` 时间差必须不超过 300 秒，并要求时间戳可解析。新增项目 Python 版本篡改负向测试。
- 验证：论文一致性审计通过 1803 项；全量 pytest 通过 219 项；Split coverage、容器复现性、结果盲设计分辨率、compileall 和 `git diff --check` 通过；Sealed21 dry-run 仍为 `ready_to_execute=False`、84 个评测单元、未调用模型，阻断原因保持为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 2026-08-12：第 132 轮 - dependency/environment 跨快照身份绑定

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`、`paper/generated/sealed21-execution-plan.json`。
- 发现：第131轮已校验 execution environment 的 Git/时间/模型身份，但 dependency lock 与 environment snapshot 仍可来自不同捕获时点或不同 Harbor 安装；单独验证两个 bundle 不能证明它们属于同一次 dry-run。
- 修改：环境身份审计现在要求 dependency lock 与 execution environment 具有同一捕获日期、相同 Harbor CLI 路径和版本，并要求 dependency lock 的 Harbor Python 路径由同一 Harbor 安装派生；依赖锁仍通过完整 `validate_environment_lock()` 和 bundle SHA256 校验。新增 Harbor 版本篡改负向测试。
- 验证：论文一致性审计通过 1801 项；全量 pytest 通过 218 项；Split coverage、容器复现性、结果盲设计分辨率、compileall 和 `git diff --check` 通过；Sealed21 dry-run 仍为 `ready_to_execute=False`、84 个评测单元、未调用模型，阻断原因保持为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 2026-08-12：第 131 轮 - execution environment 身份新鲜度审计

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`、`paper/generated/sealed21-execution-plan.json`。
- 发现：第130轮已把环境 availability 绑定到快照证据，但快照中的 `git_head`、`git_dirty`、`model` 和 `captured_at` 仍未与当前工作区/计划身份独立比较；过期或篡改的环境身份可能与 readiness 布尔值同时存在。
- 修改：新增 `environment_identity_checks()`：通过只读 `git rev-parse HEAD` 与 `git status --porcelain` 比较当前 Git 身份和 dirty 状态，检查快照模型与 plan 模型一致，并要求带时区的 `captured_at` 不是未来时间。新增 Git HEAD 篡改负向测试。历史快照不会因自然变旧被拒绝，代码/工作区变化仍由 dirty 与源码哈希门禁处理。
- 验证：论文一致性审计通过 1797 项；全量 pytest 通过 217 项；Split coverage、容器复现性、结果盲设计分辨率、compileall 和 `git diff --check` 通过；Sealed21 dry-run 仍为 `ready_to_execute=False`、84 个评测单元、未调用模型，阻断原因保持为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 2026-08-12：第 130 轮 - execution environment readiness 证据绑定

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`、`paper/generated/sealed21-execution-plan.json`。
- 发现：第129轮已从磁盘重算 strict acceptance readiness，但 `source_snapshot_ready`、Harbor/Docker、依赖锁等环境状态仍主要读取 plan 的嵌套字段，未逐项与 `execution_environment` 和 dependency lock 证据绑定；过期环境快照可能被误当作当前可执行状态。
- 修改：新增 `derive_environment_readiness()`：从 dirty worktree 与 override 重算 source snapshot，从 Harbor executable/version、Docker client/server evidence 和 `validate_environment_lock()` 重算环境可用性；审计逐项比较这些重算值与 plan readiness。API key 继续只保留非敏感布尔状态，不读取或写入凭据内容。新增 source snapshot 篡改负向测试。
- 验证：论文一致性审计通过 1793 项；全量 pytest 通过 216 项；Split coverage、容器复现性、结果盲设计分辨率、compileall 和 `git diff --check` 通过；Sealed21 dry-run 仍为 `ready_to_execute=False`、84 个评测单元、未调用模型，阻断原因保持为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 2026-08-12：第 129 轮 - strict acceptance readiness 从磁盘证据重算

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`、`paper/generated/sealed21-execution-plan.json`。
- 发现：第128轮已要求 readiness 由多个条件组成，但 `strict_acceptance_artifact_present`、`strict_acceptance_artifact_malformed`、boolean consistency、源哈希稳定和结果绑定状态仍主要读取 execution plan 自身；若只修改 plan 字段，审计未必能从磁盘事实发现。
- 修改：新增 `derive_acceptance_readiness()`，从 strict artifact 实际路径重新判断文件存在性、JSON 可解析性、decision/accepted 一致性、源结果 SHA256、baseline/candidate 路径绑定及 acceptance gate 重算结果；审计逐项比较嵌套 readiness 与磁盘重算值，并保留顶层字段存在时的同步检查。新增 readiness 标志与磁盘 artifact 不一致的负向测试。
- 验证：论文一致性审计通过 1789 项；全量 pytest 通过 215 项；Split coverage、容器复现性、结果盲设计分辨率、compileall 和 `git diff --check` 通过；Sealed21 dry-run 仍为 `ready_to_execute=False`、84 个评测单元、未调用模型，阻断原因保持为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 2026-08-12：第 128 轮 - Sealed execution readiness 派生闭环

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`、`paper/generated/sealed21-execution-plan.json`。
- 发现：execution plan 顶层 `strict_acceptance` 字段实际是 artifact 路径，而布尔 readiness 位于嵌套 `readiness.strict_acceptance`；审计此前计算 `ready_to_execute` 时虽检查了部分环境条件，却没有把嵌套 strict readiness 的组成条件完整纳入派生式，可能让篡改后的 readiness 组合被误判为可执行。
- 修改：审计现在从 strict decision、artifact boolean 一致性、源哈希稳定、结果绑定和 acceptance 重算共同重建 `expected_strict_ready`，并要求嵌套 `readiness.strict_acceptance` 与之相等；`ready_to_execute` 同时要求 strict readiness、重算状态、源快照、Harbor、Docker、API key 和依赖锁全部为真。新增“伪造 strict acceptance 后置 ready”负向测试，同时明确不把 artifact 路径字段当作布尔值。
- 验证：论文一致性审计通过 1780 项；全量 pytest 通过 214 项；Split coverage、容器复现性、结果盲设计分辨率、compileall 和 `git diff --check` 通过；Sealed21 dry-run 仍为 `ready_to_execute=False`、84 个评测单元、未调用模型，阻断原因保持为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 2026-08-12：第 127 轮 - Sealed 报告状态感知与论文消费审计

- 位置：`paper/analyze_sealed.py`、`paper/audit_paper_consistency.py`、`tests/test_sealed_analysis.py`。
- 发现：第126轮已验证 `sealed_statistics.json` 的状态闭环，但报告渲染层此前没有专门测试，未来 incomplete 产物可能把 `null` 渲染成 `None`，或缺少状态/阻断原因标记，导致 EI 读者把缺失推断误读成数值结论。
- 修改：报告渲染使用 `fmt_optional_pct/fmt_optional_p` 输出 `NA` 而非字面量 `None`；报告明确写入 `analysis_status` 和 `inference_blocked_reason`。论文审计在发现 `sealed21/SEALED_REPORT.md` 时，检查状态标记、incomplete 的 `NA`、阻断原因和无 `None` 污染。新增 incomplete 报告渲染回归测试。
- 验证：论文一致性审计通过 1779 项；全量 pytest 通过 213 项；Split coverage、容器复现性、结果盲设计分辨率、compileall 和 `git diff --check` 通过；Sealed21 dry-run 仍为 84 个评测单元且未调用模型，阻断原因保持为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 2026-08-12：第 126 轮 - Sealed21 统计产物状态不变量审计

- 位置：`paper/analyze_sealed.py`、`paper/audit_paper_consistency.py`、`tests/test_sealed_analysis.py`、`tests/test_experiment_document_consistency.py`。
- 发现：第125轮已写入 `analysis_status` 与 invalid keys，但若只篡改状态字段、primary 的 `inference_valid`、invalid 计数或 CI/p 值，旧审计没有一个专门的产物级闭环校验器来统一拒绝这种语义漂移。
- 修改：新增 `validate_sealed_statistics_payload()`，强制检查 `complete/incomplete` 状态、双方 invalid keys 数量与 `primary` 计数一致、incomplete 必须有阻断原因/`pre_registered_success=false`/确认性字段全为 null，complete 必须零 invalid/推断有效/确认性字段齐全。分析器写出前调用该校验；论文审计新增 `sealed_statistics_status_checks()`，对生成产物重新执行同一不变量。新增状态篡改负向测试。
- 验证：论文一致性审计通过 1779 项；全量 pytest 通过 212 项；Split coverage、容器复现性、结果盲设计分辨率、compileall 和 `git diff --check` 通过；Sealed21 dry-run 仍未调用模型，阻断原因保持为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 2026-08-12：第 125 轮 - Sealed21 incomplete 状态与确认性门禁分层

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、中英文实验章节。
- 发现：协议规定合法结果若仍有 infrastructure-invalid，应标记为 `incomplete` 并不计算确认性结论；此前 `validate_result()` 遇到 invalid 直接抛错，无法在机器产物中区分“合法但未完成”与“结果格式损坏”，也无法记录具体 invalid 单元。
- 修改：`validate_result()` 现在继续硬拒绝非布尔 `passed`、缺失/非有限 reward、repeat 聚合不一致和 cell 集合错误，但对合法结果返回 rows 与 invalid key 列表。分析器根据 invalid 门禁写入 `analysis_status`（`complete`/`incomplete`）、`inference_blocked_reason`、双方 unresolved invalid 数及具体 key；incomplete 时确认性 CI、McNemar p 和任务级 p 保持 null，报告支持 NA 格式，不再把基础设施问题表述为行为失败。新增状态函数和 invalid 列表回归测试。
- 文档：中英文实验章节明确合法 incomplete 产物与 malformed 硬失败的区别，并说明 `SEALED_REPORT.md` 会重复状态和 invalid keys。
- 验证：论文一致性审计通过 1779 项；全量 pytest 通过 210 项；Split coverage、容器复现性、结果盲设计分辨率、compileall 和 `git diff --check` 通过；Sealed21 dry-run 仍未调用模型，阻断原因为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 2026-08-12：第 124 轮 - 预注册结构化统计设计冻结

- 位置：`configs/experiments/ei_confirmation_v1.json`、`paper/analyze_sealed.py`、`paper/audit_paper_consistency.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`。
- 发现：第123轮已让 Clean64 与 Sealed21 使用共享统计构造器，但冻结预注册仍主要通过多个自然语言字段表达统计设计；分析器虽然能校验部分 seed、Bootstrap 和 endpoint 字段，却没有要求预注册完整保存比较单元、观测单元、repeat、角色和算法聚合语义。
- 修改：在预注册中新增完整 `statistical_design` 对象，记录 21 个 sealed 任务、baseline/candidate 两角色、repeat 1/2、每任务 2 次 attempt、20,000 次任务 Bootstrap、固定 seed/分位数方法及精确有理数置换算法。`analyze_sealed.validate_preregistration()` 现在按共享构造器逐字段比较该对象；论文审计同步核对预注册对象，并新增结构化设计篡改负向测试。预注册变化后重新生成统计和 Sealed execution plan，刷新身份哈希。
- 验证：论文一致性审计通过 1779 项；全量 pytest 通过 209 项；Split coverage、容器复现性、结果盲设计分辨率、compileall 和 `git diff --check` 通过；Sealed21 dry-run 仍为 84 个评测单元、52.5 小时预算上界，未调用模型，阻断原因仍为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 2026-08-12：第 123 轮 - Clean64/Sealed21 统计契约统一

- 位置：`paper/analyze_experiments.py`、`paper/analyze_sealed.py`、`paper/audit_paper_consistency.py`、`tests/test_sealed_analysis.py`、`tests/test_experiment_document_consistency.py`。
- 发现：Clean64 已有结构化 `statistical_design`，但 Sealed21 仍在分析输出中单独拼装 `design` 字典，字段名称和统计语义不完全相同；预注册校验只覆盖部分常量，存在两条分析路径发生统计口径分叉的风险。
- 修改：新增共享 `build_statistical_design_metadata()`；Sealed21 通过 `sealed_statistical_design_metadata()` 复用比较单元、观测单元、repeat、任务级 Bootstrap、分位数方法和精确任务级置换字段，并显式绑定 baseline/candidate 角色和 21 个 sealed 任务。Sealed 分析常量改为引用共享分析常量，避免 seed/重采样次数独立漂移。论文审计新增跨路径统计设计检查，逐项核对 Sealed 预注册、共享 Clean64 设计及成功规则；新增共享契约正向测试和预注册 alpha 篡改负向测试。
- 验证：论文一致性审计通过 1778 项；全量 pytest 通过 208 项；Split coverage、容器复现性、结果盲设计分辨率、compileall 和 `git diff --check` 通过；Sealed21 dry-run 仍为 84 个评测单元、52.5 小时预算上界，未调用模型，阻断原因保持为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 2026-08-12：第 122 轮 - 统计设计元数据结构化与重算审计

- 位置：`paper/analyze_experiments.py`、`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`、中英文实验章节。
- 发现：统计实现已经按任务聚类 Bootstrap 和任务级精确置换检验计算，但 `statistics.json` 只分散记录重采样次数、种子等字段，未将比较单元、观测单元、repeat 边界、划分规模和算法语义组成一个可逐项比较的设计对象；这会增加实现与 EI 正文描述逐渐漂移的风险。
- 修改：新增 `statistical_design_metadata()`，集中记录 43/21 任务、repeat 1/2、每任务 2 次 attempt、20,000 次任务 Bootstrap、分位数方法、种子派生、双侧精确任务级置换及有理数动态规划算法；统计生成器写入 `statistics.json`，论文审计按当前分析源码重建并逐字段比较，同时核对设计元数据中的划分任务数与 summary 行一致。新增篡改 `attempts_per_task` 的负向回归测试。
- 文档：中英文“统计结论有效性”段落明确说明结构化统计设计元数据及其防漂移作用。
- 验证：重新生成统计产物和结果盲设计分辨率包，Sealed21 dry-run 刷新分析代码哈希；论文一致性审计通过，当前仍阻断于 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`，未调用模型。

## 2026-08-12：第 121 轮 - execution provenance 计数与 post-result 原始重算绑定

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`
- 发现：第120轮已记录执行前后 result 哈希，但 `execution_provenance.selected_invalid_after` 和 `global_invalid_after` 仍可能只作为 JSON 字段被读取；如果手工修改计数而不改 post hash，审计未必发现。
- 修改：审计器现在读取 `post_source_result_sha256` 对应的当前 `result.json`，按 strict reward 规则逐 cell 重算 invalid 集合，并分别核对全局 invalid 数和执行计划 `cases` 交集的 selected invalid 数；结果解析失败直接使该 provenance 检查失败。
- 测试：新增执行后 invalid 计数漂移负向测试；保留前后哈希绑定、稳定快照和 canary 判定测试。
- 意义：rerun 结果进入统计前，执行状态、结果文件、哈希和 invalid 计数必须来自同一份可重算证据，避免补跑报告与论文统计之间出现“计数正确但来源错误”的断链。

## 2026-08-12：第 120 轮 - rerun 执行前后 provenance 分离

- 位置：`eval/scripts/rerun_invalid_cases.py`、`paper/audit_paper_consistency.py`、`tests/test_invalid_rerun_planner.py`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_EN.md`、`paper/EXPERIMENTS_ZH.md`
- 发现：Phase 0 执行后若直接把 `canary_outcome` 写回原 rerun plan，`source_result_sha256` 会同时承担“执行前输入”和“执行后结果”两个语义；rerun 修改 aggregate `result.json` 后，无法证明新结果来自本次执行且读取期间稳定。
- 修改：新增 `execution_provenance`，保留执行前 `pre_source_result_sha256`，并记录执行后 `post_source_result_sha256`、evaluator return code、post-read stability、selected/global invalid 数和执行状态；执行失败也写入失败状态，归档目录中的计划更新为最终执行记录。论文审计对 completed/failed 两种状态分别核对前后哈希和稳定性。
- 文档：中英文实验章节和复现手册明确区分计划输入身份与执行后结果身份，避免把 rerun 结果误写成历史输入。
- 测试：新增前后哈希绑定测试；保留 canary 行为失败/基础设施失败及 stale provenance 负向测试。

## 2026-08-12：第 119 轮 - Phase 0 canary 执行后健康判定结构化

- 位置：`eval/scripts/rerun_invalid_cases.py`、`tests/test_invalid_rerun_planner.py`、`paper/audit_paper_consistency.py`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_EN.md`、`paper/EXPERIMENTS_ZH.md`
- 发现：此前 Phase 0 文档声明“只检查是否得到数值 verifier 结果”，但执行器完成后只打印 selected/global invalid 数，没有写出独立的 canary 判定；行为失败、provider 错误和缺失 reward 无法在机器可读计划中区分。
- 修改：新增 `evaluate_canary_outcomes()`。它按严格 reward/status/passed 一致性逐个重算选中 cell，写入 `numeric_verifier_outcome_count`、`infrastructure_invalid_count`、行为通过/失败计数、明细和 `canary_ready`。行为失败但 reward=0 的结果仍算有效基础设施结果；provider、timeout、missing reward、非有限或不一致结果保持未恢复。计划同时记录 `selection_phase`，审计器校验 phase、side 与执行入口一致。
- 测试：新增“行为失败但 numeric reward 可用”和“provider/reward 缺失导致 canary 未恢复”两类测试；Phase 0 baseline/candidate dry-run 重新生成计划，未调用模型。
- 验证：定向 rerun 测试通过，论文一致性审计通过；本轮完整验证随后执行全量 pytest、辅助审计、Sealed21 dry-run、`compileall` 和 `git diff --check`。
- 追加审计：当执行后计划包含 `canary_outcome` 时，论文审计会验证各计数非负、明细数等于 selected cells、numeric/invalid/missing 三者求和一致，以及 `canary_ready` 与 numeric outcome 数量一致；当前 dry-run 不虚构该字段。

## 2026-08-12：第 118 轮 - 分阶段 rerun 清单与实际 harness 角色绑定

- 位置：`eval/scripts/rerun_invalid_cases.py`、`tests/test_invalid_rerun_planner.py`、`paper/audit_paper_consistency.py`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_EN.md`、`paper/EXPERIMENTS_ZH.md`
- 发现：`load_cell_filter()` 之前只验证清单格式、源结果哈希和 cell 仍为 invalid，没有验证 manifest 的 `side` 是否匹配本次 baseline 或 candidate 执行入口；合法清单理论上可以被错误地用于另一 harness 角色。
- 修改：清单加载现在强制要求 `side` 为 `baseline/candidate`，并由命令入口按是否提供 candidate workspace 传入 expected side；cell-based rerun plan 新增 `selection_side`。论文审计同步要求该字段与执行角色一致，并将规则写入复现手册和中英文实验章节。
- 测试：新增 candidate 清单用于 baseline 执行的负向测试；现有 stale hash、valid cell、重复 cell 和阶段 manifest 测试继续通过。
- 结果：Phase 0 baseline/candidate dry-run 均成功生成计划，分别选择 1 个历史 provider failure canary，未调用模型；错误角色清单会在生成计划前拒绝。随后修正 `build_plan()` 直接调用也写入 `selection_side` 的结构一致性遗漏。
- 验证：定向 rerun 测试通过；论文一致性审计通过；全量 pytest 通过 198 项；`compileall` 与 `git diff --check` 通过。

## 2026-08-12：第 117 轮 - summary 与原始 result.json 独立重算绑定

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`
- 发现：上一轮已把 `inference_valid` 与 summary 内的 invalid 计数绑定，但审计仍主要依赖 `statistics.json` 自身字段；如果有人同步篡改 `baseline_passed`、`delta` 或共同有效子集字段，统计 JSON 可能内部自洽而脱离原始 `result.json`。
- 修改：新增 `statistics_summary_rebuild_checks()`，直接读取两个模型的 baseline/final 原始结果，调用与生成器相同的 `analyze_split()`，按模型、split、随机种子逐字段重算并比较四行 summary。该校验覆盖通过数、invalid 数、比例、delta、共同有效样本、推断字段和重复种子等关键统计量。
- 测试：新增 summary 数值被篡改但仍满足比例公式时的负向测试，确认原始结果级重算能够拒绝该篡改。
- 当前意义：EI 论文中的历史结果不再只证明“统计 JSON 内部一致”，还必须证明“统计 JSON 与源 result.json 一致”；任何源结果变化都要求重新生成统计产物并通过输入哈希门禁。

## 2026-08-12：第 116 轮 - 确认性推断状态与 invalid 门禁双向绑定

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`、`paper/generated/statistics.json`
- 发现：统计生成器会按 `baseline_invalid == 0 且 final_invalid == 0` 决定 `inference_valid`，但论文审计此前只验证“blocked 时推断字段为空”，没有反向重算该布尔状态；手工将零 invalid 行标成 blocked，或将含 invalid 行标成 valid，存在绕过确认性门禁的风险。
- 修改：审计器现在从每个 summary 行的 `baseline_invalid`/`final_invalid` 重新推导 `inference_valid`，并同时重算 `inference_blocked_reason`；计数类型、状态、阻断原因三者必须一致。统计源重新生成，保持当前所有 unresolved invalid 行的确认性字段为 `null`。
- 测试：新增“零 invalid 却标记 blocked”和“存在 invalid 却标记 valid”两类负向测试；原有非空推断字段测试继续保留。
- 验证：定向测试通过；重新运行 `paper/analyze_experiments.py` 和论文一致性审计通过；下一步执行全量 pytest、Sealed21 dry-run、`compileall` 与 `git diff --check`。

## 2026-08-12：第 115 轮 - malformed strict artifact 与论文双语显式绑定

- 位置：`paper/audit_paper_consistency.py`、`paper/EXPERIMENTS_EN.md`、`paper/EXPERIMENTS_ZH.md`、`tests/test_experiment_document_consistency.py`
- 发现：strict artifact 变为 malformed 时，launcher 只报 `strict_acceptance_artifact_malformed`，但审计器的 blocker 重算仍会继续推出 boolean/source/result-binding 三类次级阻断，与真实 launcher 分支不一致；同时，中英文实验章节未明确列出这一可能的动态诊断码。
- 修改：审计器按 `artifact_present=true` 且 `artifact_malformed=true` 的规则只生成 `strict_acceptance_artifact_malformed`，并保持 `strict_acceptance_not_recomputed` 与其他环境门禁的独立重算；中英文实验章节同步列出并解释 malformed blocker。
- 测试：新增测试确认异常 artifact 不会被解析为 3 个并列的其他 strict blocker；保留原有 stale narrative 与 blocker list 改写负向测试。
- 验证：论文一致性审计通过 1629 项，全量 pytest 通过 195 项；Sealed21 dry-run 打印 21 个任务、84 个评估单元，`ready_to_execute: False`，当前仍为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`，因此未调用模型；`compileall` 和 `git diff --check` 通过。

## 2026-08-12：第 114 轮 - malformed strict artifact readiness 诊断

- 位置：`workflow/scripts/run_qwen_sealed21.ps1`、`paper/audit_paper_consistency.py`、`tests/test_sealed_launcher.py`。
- 发现：strict artifact 缺失时 launcher 能正常生成 blocker，但 artifact 存在且 JSON 截断、重复 key 或根对象非法时，`Read-StrictJsonObject` 会直接抛错，无法写出 execution plan；这会丢失“artifact 已存在但不可验证”的机器可读诊断。
- 修改：launcher 捕获 strict artifact 读取异常，将 `strict_decision` 标为 `malformed`，新增顶层/readiness 字段 `strict_acceptance_artifact_malformed=true`，并写入 `strict_acceptance_artifact_malformed` blocker；所有 strict readiness 条件保持 false，不会放行模型执行。论文审计新增该字段类型和顶层/readiness 一致性检查。
- 测试：扩展 Sealed launcher 静态测试，锁定 malformed 状态变量和 blocker 代码。
- 验证：论文一致性审计通过 1629 项，全量 pytest 通过 194 项；Sealed21 dry-run 验证 21 个任务、84 个 evaluation cells，当前正常状态仍输出 `ready_to_execute: False` 且未调用模型；`compileall` 和 `git diff --check` 通过。

## 2026-08-12：第 113 轮 - strict acceptance artifact 原子写出

- 位置：`acceptance/scripts/run_acceptance_gate.py`、`tests/test_acceptance_invalid_trials.py`。
- 发现：acceptance CLI 原先直接向目标路径 `write_text()`；如果进程在写 JSON 中途终止，可能留下截断的 `acceptance.strict.json`。下一次 launcher 会看到 artifact 存在，再进入部分字段诊断，增加陈旧/半写入状态的歧义。
- 修改：`write_json()` 改为同目录临时文件 + `flush/fsync` + `os.replace` 原子替换；异常时清理临时文件。旧 artifact 只有在新结果完整序列化后才会被替换。
- 测试：新增原子写出测试，验证目标内容完整更新且 `.tmp` 文件不残留。
- 额外验证：本轮修改使 acceptance gate 源码哈希发生变化，论文审计先拒绝旧 execution-plan bundle；重新运行 Sealed21 dry-run 后刷新 bundle，最终审计通过。
- 最终结果：论文一致性审计通过 1626 项，全量 pytest 通过 194 项；Sealed21 dry-run 验证 21 个任务、84 个 evaluation cells，`ready_to_execute: False`，未调用模型；`compileall` 和 `git diff --check` 通过。

## 2026-08-12：第 112 轮 - Sealed freeze candidate provenance 绑定

- 位置：`workflow/scripts/run_qwen_sealed21.ps1`、`paper/audit_paper_consistency.py`、`tests/test_sealed_launcher.py`、`tests/test_experiment_document_consistency.py`。
- 发现：execution plan 已能从 queue/manifest 重算 candidate provenance，但 freeze 复用检查此前只比较 candidate surface hash，不比较 candidate_id、candidate_dir 和 surface 路径；旧 freeze 可能在哈希巧合或路径替换时与当前 plan 语义不一致。
- 修改：launcher 将 `candidate_dir` 写入 `sealed_freeze.v3`，复用 freeze 时同时核对 `candidate_id`、`candidate_dir`、`candidate_surface` 及既有哈希；论文审计在 freeze 存在时重核对三项，执行前 freeze 尚不存在时记录明确的允许状态。
- 测试：扩展 launcher 静态测试并增加 execution plan candidate directory 篡改负向测试。
- 验证：论文一致性审计通过 1626 项，全量 pytest 通过 193 项；Sealed21 dry-run 验证 21 个任务、84 个 evaluation cells，`ready_to_execute: False`，未调用模型；`compileall` 和 `git diff --check` 通过。

## 2026-08-12：第 111 轮 - execution plan candidate provenance 重算

- 位置：`paper/audit_paper_consistency.py`、`workflow/scripts/run_qwen_sealed21.ps1`、`tests/test_experiment_document_consistency.py`、`tests/test_sealed_launcher.py`。
- 发现：launcher 已把 candidate 身份和路径写入 execution plan，但论文审计此前只核对 `strict_candidate_result` 与 surface SHA256，没有从 queue/manifest 独立重建 candidate_dir、candidate_id、eval_result 和 materialized surface 关系；手工修改 plan 可能保留输入哈希却改变 provenance 语义。
- 修改：execution plan 现在记录 `candidate_dir`；论文审计读取 Qwen candidate queue，要求 candidate ID 唯一，plan candidate_dir 与 canonical branch 一致，strict candidate result 与 queue 的 eval_result 一致，manifest candidate_id 与 plan 一致，并从 manifest surface_files 重新解析 candidate surface。
- 测试：新增 candidate directory 被改写到工作区外的负向测试；Sealed launcher 静态测试同步要求 plan 写出 `candidate_dir`。
- 验证：论文一致性审计通过 1625 项，全量 pytest 通过 193 项；Sealed21 dry-run 验证 21 个任务、84 个 evaluation cells，`ready_to_execute: False`，未调用模型；`compileall` 和 `git diff --check` 通过。

## 2026-08-12：第 110 轮 - Sealed21 launcher candidate 路径安全对齐

- 位置：`workflow/scripts/run_qwen_sealed21.ps1`、`tests/test_sealed_launcher.py`。
- 发现：第109轮只加固了 strict finalizer；Sealed21 launcher 仍直接解析 queue 的 `candidate_dir`/`eval_result`，未验证 manifest candidate ID，也未限制 candidate_dir 必须位于 Qwen candidate branches 工作区，导致 readiness 入口与 finalizer 使用不同安全口径。
- 修改：launcher 新增 `Resolve-QueuePath` 和 `Assert-PathUnder`，相对 queue 路径按仓库根目录解析；candidate_dir 必须位于 `runs\\clean64-qwen-self-harness\\branches`；manifest ID 必须匹配 `-CandidateId`；eval_result 必须严格绑定到 candidate 目录下的 `eval\\result.json`。
- 测试：扩展 Sealed launcher 静态测试，锁定根目录解析、工作区边界、manifest ID 和结果路径绑定。
- 验证：论文一致性审计通过 1618 项，全量 pytest 通过 192 项；Sealed21 dry-run 验证 21 个任务、84 个 evaluation cells，`ready_to_execute: False`，未调用模型；`compileall` 和 `git diff --check` 通过。

## 2026-08-12：第 109 轮 - strict finalizer 相对路径与工作区边界

- 位置：`workflow/scripts/finalize_clean64_strict.ps1`、`tests/test_finalize_launcher.py`。
- 发现：candidate queue 当前写入绝对路径，但 finalizer 的路径解析函数此前直接依赖 PowerShell 当前工作目录；若 queue 使用相对路径或从其他目录调用，可能解析到不同文件。仅验证“路径相等”也无法阻止 candidate_dir 指向实验工作区之外。
- 修改：新增 `Resolve-QueuePath`，所有 queue 路径按仓库根目录解析；新增 `Assert-PathUnder`，要求 candidate_dir 位于对应 label 的 `runs\\clean64-*-self-harness\\branches` 下。标准 `eval\\result.json` 绑定继续在规范化绝对路径上执行。
- 测试：扩展 finalizer 静态测试，锁定根目录解析函数和 workspace 边界检查。
- 验证：论文一致性审计通过 1618 项，全量 pytest 通过 192 项；Sealed21 dry-run 验证 21 个任务、84 个 evaluation cells，`ready_to_execute: False`，未调用模型；`compileall` 和 `git diff --check` 通过。

## 2026-08-12：第 108 轮 - strict finalizer candidate 路径绑定

- 位置：`workflow/scripts/finalize_clean64_strict.ps1`、`tests/test_finalize_launcher.py`。
- 发现：strict finalizer 按 candidate ID 从 queue 取出 `candidate_dir` 和 `eval_result`，但此前没有验证 manifest 内的 candidate ID，也没有确认 `eval_result` 位于该 candidate 目录的标准 `eval/result.json`；队列路径被替换后，理论上可能用另一分支结果生成合法 strict artifact。
- 修改：finalizer 现在要求 candidate `manifest.json` 存在且 `candidate_id` 与参数一致，并要求 queue 的 `eval_result` 解析后严格等于 `<candidate_dir>\eval\result.json`；不满足即在 strict gate 前终止。
- 测试：扩展 finalizer 静态测试，锁定 manifest ID 检查和 result 路径绑定错误信息。
- 验证：论文一致性审计通过 1618 项，全量 pytest 通过 192 项；Sealed21 dry-run 验证 21 个任务、84 个 evaluation cells，`ready_to_execute: False`，未调用模型；`compileall` 和 `git diff --check` 通过。

## 2026-08-12：第 107 轮 - strict finalizer 与 Sealed readiness 闭环

- 位置：`workflow/scripts/finalize_clean64_strict.ps1`、`tests/test_finalize_launcher.py`、`paper/EXPERIMENTS_EN.md`、`paper/EXPERIMENTS_ZH.md`。
- 发现：strict finalizer 在生成 `acceptance.strict.json` 和重建 Clean64 统计后直接结束，未自动刷新 `sealed21-execution-plan.json` 或运行论文一致性审计；因此 strict artifact 到位后，execution plan 可能仍保留旧的 `strict_decision_missing`，需要人工额外执行 launcher，容易造成“已验收但未就绪”状态漂移。
- 修改：finalizer 现在依次执行统计重建、Sealed21 launcher dry-run（刷新 readiness plan）和 `audit_paper_consistency.py`；任一步失败都会中止并报告明确阶段。历史 `acceptance.json` 仍不被覆盖。
- 文档：中英文实验章节同步说明 finalizer 会重建统计、刷新 readiness plan 并运行论文审计。
- 测试：扩展 finalizer 静态测试，要求存在 readiness 刷新和论文审计调用。
- 验证：论文一致性审计通过 1618 项，全量 pytest 通过 192 项；Sealed21 dry-run 验证 21 个任务、84 个 evaluation cells，当前仍因 strict artifact 缺失而 `ready_to_execute: False`，未调用模型；`compileall` 和 `git diff --check` 通过。

## 2026-08-12：第 106 轮 - 候选 acceptance 来源语义与 gate 口径绑定

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`。
- 发现：第105轮已能重算历史 acceptance 摘要，但候选行的 `decision_source` 仍主要依赖统计生成器写入；若手工把历史 acceptance 标为 strict，或以后引入 `acceptance.strict.json` 却继续使用历史 v0 格式，数值可能正确但论文语义标签与实际 gate 口径不一致。
- 修改：候选 provenance 审计现在按 artifact 文件名绑定 `decision_source`（`acceptance.json` 必须是 `historical`，`acceptance.strict.json` 必须是 `strict`），并分别要求 v0/v1 格式；历史 v0 继续使用 fixed-denominator 重算，严格 v1 则调用统一 `verify_acceptance_artifact()`。
- 测试：新增 decision source 标签漂移负向测试；已有历史 acceptance 摘要篡改测试继续覆盖 v0 重算路径。
- 验证：论文一致性审计通过 1618 项，全量 pytest 通过 192 项；Sealed21 dry-run 验证 21 个任务、84 个 evaluation cells，`ready_to_execute: False`，未调用模型；`compileall` 和 `git diff --check` 通过。

## 2026-08-12：第 105 轮 - 历史 acceptance 摘要从原始结果重算

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`。
- 发现：候选 provenance 审计此前确认候选表的 decision/delta 与 acceptance 文件一致，并确认结果路径存在，但历史 `self_harness.acceptance_gate.v0` 摘要自身没有经过统一重算；如果同时篡改 acceptance 的 decision、reason 或 repeat 汇总，论文表格可能继续沿用伪造摘要。
- 修改：新增 fixed-denominator 历史 acceptance 重算器：从 acceptance 指向的 baseline/candidate 原始 `result.json` 重新按 JSON boolean `passed`、两次 repeat、Train/Heldout 和 pass-rate gate 计算 comparisons、delta、status、reason、decision 与 rule，并逐字段比对 acceptance 文件。
- 说明：历史结果包含基础设施无效单元，不能调用 strict reward gate；本轮重算明确保留历史固定分母口径，不把它误写成严格 promotion 结果。
- 测试：新增 acceptance 摘要被伪造为 accepted 的负向测试；即使源结果没有任何改善，审计也必须拒绝。
- 验证：论文一致性审计通过 1606 项，全量 pytest 通过 191 项；Sealed21 dry-run 验证 21 个任务、84 个 evaluation cells，`ready_to_execute: False`，未调用模型；`compileall` 和 `git diff --check` 通过。

## 2026-08-12：第 104 轮 - Sealed primary endpoint 统计契约审计

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`、`paper/generated/PAPER_CONSISTENCY_AUDIT.json`。
- 发现：`analyze_sealed.py` 和 `validate_preregistration()` 已经在正式分析入口检查 primary endpoint，但论文总审计的 `sealed_protocol_checks()` 此前只核对任务规模、重复次数、缺失处理和正文描述，没有逐项锁定预注册中的指标、置信区间、检验、α 与三条件 success rule；正文审计可能在 endpoint 语义漂移后仍通过。
- 修改：新增 endpoint 契约检查，要求预注册中的 `metric`、`confidence_interval`、`hypothesis_test`、`alpha=0.05` 和 `success_rule` 与冻结协议完全一致。
- 测试：新增 primary endpoint 被改写为仅 `delta > 0` 的负向测试，确认论文审计拒绝不完整成功标准。
- 验证：论文一致性审计通过 1576 项，全量 pytest 通过 190 项；Sealed21 dry-run 验证 21 个任务、84 个 evaluation cells，`ready_to_execute: False`，未调用模型；`compileall` 和 `git diff --check` 通过。

## 2026-08-12：第 103 轮 - 统计方法配置与 summary seed 审计

- 位置：`paper/analyze_experiments.py`、`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`、`paper/generated/design-audit/`。
- 发现：统计 JSON 虽记录 Bootstrap 次数、分位数规则、置换方法和全局 seed，但这些字段此前未与分析器常量逐项重算；summary 行也未记录其实际使用的分 split seed，统计产物存在“参数文字与实现漂移”的审计盲区。
- 修改：集中定义 `ANALYSIS_VERSION`、`BOOTSTRAP_SAMPLES`、`ANALYSIS_SEED`、`PERMUTATION_METHOD`；summary 为每个 model/split 记录 `bootstrap_seed`；论文审计新增 `statistical_configuration_checks()`，逐项核对方法参数与每行 seed。
- 测试：新增统计配置篡改负向测试，将 `bootstrap_samples` 改为 10000 后要求审计失败；同时重建设计分辨率审计和 Sealed execution plan 的源码 bundle 哈希。
- 验证：论文一致性审计通过 1571 项，全量 pytest 通过 189 项；Sealed21 dry-run 验证 21 个任务、84 个 evaluation cells，未调用模型；`compileall` 和 `git diff --check` 通过。

## 2026-08-12：第 102 轮 - 统计输入清单集合级审计

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`。
- 发现：统计产物会校验 `source_files` 中每个文件的 SHA256，但此前没有把该清单与 `analyze_experiments.analysis_input_paths()` 的实际发现结果做集合比较；替换一个真实输入为无关但存在的文件，可能保持数量与单文件哈希检查通过。
- 修改：审计现在重建统计分析应追踪的输入集合（Clean64 baseline/final/queue、候选 acceptance 及其引用结果、分析脚本和 validity 脚本），并要求与 `statistics.json.source_files` 的规范化绝对路径集合完全相等。
- 测试：新增清单替换负向测试，将一个统计来源替换为 `README_REPRODUCTION.md` 并同步其哈希，确认集合级检查拒绝该伪造清单。
- 验证：论文一致性审计通过 1561 项；Sealed21 dry-run 验证 21 个任务、84 个 evaluation cells，输出 `ready_to_execute: False`，报告 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready` 四个 blocker，未调用模型；全量 pytest 通过 188 项，`compileall` 和 `git diff --check` 通过。

## 2026-08-12：第 101 轮 - dependency_lock bundle 内容审计

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`。
- 发现：execution plan 虽已包含 `dependency_lock.bundle_sha256`，但论文审计此前只比对有效字段的 bundle hash，未按统一 lock 协议重新计算 canonical payload；手工更改包清单可能保留原哈希并通过审计。
- 修改：新增 `validate_environment_lock()` 审计，按 `capture_environment_lock.validate_lock()` 公式检查依赖 lock format 和 `bundle_sha256`；无效时失败 `sealed_plan.dependency_lock_valid`，与 launcher 生成规则保持一致。
- 测试：新增 dependency lock 内容被更改的负向测试，模拟在 `project_python.packages` 插入 `tampered==0` 而不改 bundle hash，审计必须拒绝。
- 验证：Sealed21 dry-run 验证 21 个任务、84 个 evaluation cells，输出 `ready_to_execute: False`，并报告 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready` 四个 blocker，未调用模型；论文一致性审计通过 1560 项，全量 pytest 通过 187 项，`compileall` 和 `git diff --check` 通过。正式 Sealed21 仍因 strict decision 缺失而不执行模型调用。

## 2026-08-12：第 100 轮 - Sealed run_identity 重算审计

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`。
- 发现：launcher 已用 manifest、预注册、TOML、execution code bundle、dependency bundle、角色和 harness surface 形成 baseline/candidate `run_identity`，但论文审计此前只分别检查输入哈希，未核对最终身份字符串。
- 修改：审计按 launcher 公式重算两侧 identity，要求 baseline/candidate 的角色片段和 surface hash 正确，防止输入哈希看似完整但身份被替换或角色错绑。
- 测试：新增 baseline identity 被替换为 candidate identity 的负向测试。
- 验证：最终 dry-run 输出四个 blocker（`strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`），`ready_to_execute: False`，未调用模型；论文一致性审计通过 1559 项；全量 pytest 通过 186 项；`compileall` 与 `git diff --check` 通过。

## 2026-08-12：第 99 轮 - readiness blocker 与中英文实验正文动态绑定

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`。
- 发现：第98轮已将四个 blocker 写入 execution plan 和中英文实验章节，但论文审计尚未检查正文是否仍报告当前 plan 状态；后续状态变化可能导致正文滞后。
- 修改：新增 `sealed_readiness_narrative_checks()`，从当前 execution plan 读取 blocker 列表，逐项要求每个代码同时出现在 `EXPERIMENTS_EN.md` 和 `EXPERIMENTS_ZH.md`；列表形状异常或正文缺失任一代码都会失败。
- 测试：新增 stale blocker narrative 负向测试。
- 验证：最终 dry-run 输出四个当前 blocker（`strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`），`ready_to_execute: False`，未调用模型；论文一致性审计通过 1557 项；全量 pytest 通过 185 项；`compileall` 与 `git diff --check` 通过。

## 2026-08-12：第 98 轮 - 区分 acceptance artifact 缺失与内容失配

- 位置：`workflow/scripts/run_qwen_sealed21.ps1`、`paper/audit_paper_consistency.py`、`tests/test_sealed_launcher.py`。
- 发现：第97轮 blocker 列表在 strict artifact 尚未生成时，会同时报告 boolean/hash/binding 失败；这些条件实际上是“尚未检查”，不是已发现的错误，容易误导 EI 实验排障。
- 修改：新增 `strict_acceptance_artifact_present`；artifact 缺失时只报告 `strict_acceptance_artifact_missing` 与 `strict_acceptance_not_recomputed`，artifact 存在但内容异常时才报告 boolean、source-hash 或 result-binding 失配。该字段同时写入顶层 plan 和 readiness，并由论文审计核对。
- 文档：中英文实验章节同步记录当前四个 readiness blocker，并明确它们是执行诊断而不是模型结果。
- 验证：最终 dry-run 明确输出 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed` 和 `source_snapshot_not_ready` 四个 blocker，`ready_to_execute: False`，未调用模型；论文一致性审计通过 1549 项；全量 pytest 通过 184 项；`compileall` 与 `git diff --check` 通过。

## 2026-08-12：第 97 轮 - 结构化 readiness blockers

- 位置：`workflow/scripts/run_qwen_sealed21.ps1`、`paper/audit_paper_consistency.py`、`tests/test_sealed_launcher.py`、`tests/test_experiment_document_consistency.py`。
- 发现：execution plan 只有多个 readiness 布尔值，无法直接、机器可读地解释为什么当前不能执行；人工判断容易遗漏 strict、环境或源码快照条件。
- 修改：launcher 按固定顺序写入顶层和 readiness 内的 `readiness_blockers`，包括 `strict_decision_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`、`harbor_unavailable`、`docker_unavailable`、`api_key_unavailable` 和 `dependency_lock_unavailable` 等代码；终端 dry-run 同步打印列表。
- 修改：论文审计按相同 readiness 字段和 strict decision 重算 blocker 列表，核对顶层/嵌套列表一致性，防止手工删减阻断原因。
- 验证：最终 dry-run 输出 6 个确定性 blocker（包含 `strict_decision_missing`、`strict_acceptance_not_recomputed` 和 `source_snapshot_not_ready`），`ready_to_execute: False`，未调用模型；论文一致性审计通过 1546 项；全量 pytest 通过 184 项；`compileall` 与 `git diff --check` 通过。

## 2026-08-12：第 96 轮 - 规范化 malformed strict decision 状态

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`。
- 发现：动态读取 acceptance artifact 时，缺失或非法 `decision` 可能被字符串化为 `"None"`，不利于审计报告和排障。
- 修改：缺失、非字符串或不属于 `accepted/rejected` 的 decision 统一标记为 `malformed`；verifier 失败时 recomputed 保持 `false`，不允许进入执行就绪状态。
- 测试：新增 malformed decision 负向测试，并保留合法 accepted decision 的动态读取测试。
- 验证：最终 dry-run 输出 `strict_decision: missing`、`strict_acceptance_recomputed: False`、`ready_to_execute: False`，未调用模型；论文一致性审计通过 1544 项；全量 pytest 通过 183 项；`compileall` 与 `git diff --check` 通过。

## 2026-08-12：第 95 轮 - 论文审计复用 acceptance artifact 稳定性校验

- 位置：`paper/audit_paper_consistency.py`。
- 发现：第94轮已让 CLI 和 Sealed analyzer 对 acceptance artifact 做前后哈希核对，但论文审计的动态 decision 分支仍未把 artifact 路径/哈希传给 verifier，主要依赖审计末尾的整体输入快照。
- 修改：`sealed_acceptance_plan_state()` 现在在解析 artifact 前记录 SHA256，并将路径和哈希传入统一 `verify_acceptance_artifact()`；decision 重算期间的 artifact 变化会立即使计划审计失败。
- 验证：最终 dry-run 输出 `strict_decision: missing`、`strict_acceptance_recomputed: False`、`ready_to_execute: False`，未调用模型；论文一致性审计通过 1544 项；全量 pytest 通过 182 项；`compileall` 与 `git diff --check` 通过。

## 2026-08-12：第 94 轮 - acceptance artifact 自身 TOCTOU 门禁

- 位置：`acceptance/scripts/run_acceptance_gate.py`、`paper/analyze_sealed.py`、`tests/test_acceptance_invalid_trials.py`。
- 发现：verifier 已对两份 Clean64 源结果执行读取前后哈希核对，但 acceptance artifact 自身在解析后、重算完成前被替换时缺少同等级的稳定性检查。
- 修改：CLI 和 Sealed analyzer 在解析 artifact 前记录 SHA256；统一 `verify_acceptance_artifact()` 在 gate 重算后重新核对 artifact 哈希，发生变化即拒绝。launcher 通过 CLI 自动继承该门禁。
- 测试：新增竞态篡改测试，在 gate 重算期间替换 artifact，必须报 `acceptance artifact changed during acceptance evaluation`。
- 验证：最终 dry-run 输出 `strict_decision: missing`、`strict_acceptance_recomputed: False`、`ready_to_execute: False`，未调用模型；论文一致性审计通过 1544 项；全量 pytest 通过 182 项；`compileall` 与 `git diff --check` 通过。

## 2026-08-12：第 93 轮 - strict acceptance artifact 哈希纳入计划审计

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`。
- 发现：第92轮已核对 strict Clean64 源结果路径和 SHA256，但 execution plan 的 `strict_acceptance_sha256` 尚未独立与磁盘核对；旧或替换的 acceptance artifact 可能因此缺少审计证据。
- 修改：审计现在按计划中的 strict artifact 路径重新计算 `strict_acceptance_sha256`；文件不存在时要求计划字段为 `null`，文件存在时必须匹配实际 SHA256。
- 测试：新增 strict artifact 哈希篡改负向测试。
- 验证：最终 dry-run 输出 `strict_decision: missing`、`strict_acceptance_recomputed: False`、`ready_to_execute: False`，未调用模型；论文一致性审计通过 1544 项；全量 pytest 通过 181 项；`compileall` 与 `git diff --check` 通过。

## 2026-08-12：第 92 轮 - strict Clean64 源绑定纳入 execution plan 审计

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`。
- 发现：launcher/freeze 已记录 strict baseline/candidate Clean64 `result.json` 的路径与 SHA256，但论文一致性审计只核对 strict artifact 路径，未独立核对这四个计划字段。
- 修改：execution plan 审计现在要求 strict 两侧源路径分别等于当前 Clean64 canonical 结果，并重新计算 `strict_baseline_result_sha256`、`strict_candidate_result_sha256` 与磁盘一致；这些路径若存在也由输入快照保护。
- 测试：新增篡改 candidate strict 源 SHA256 的负向测试。
- 验证：最终 dry-run 输出 `strict_decision: missing`、`strict_acceptance_recomputed: False`、`ready_to_execute: False`，未调用模型；论文一致性审计通过 1543 项；全量 pytest 通过 180 项；`compileall` 与 `git diff --check` 通过。

## 2026-08-12：第 91 轮 - 动态审计 ready_to_execute 语义

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`。
- 发现：第90轮已将 `strict_decision` 改为动态读取，但审计仍固定要求 `ready_to_execute=false`；正式 acceptance 完成且环境就绪后，合法的可执行计划会被错误拒绝。
- 修改：审计按 launcher 实际公式重算 `ready_to_execute`：`strict_acceptance`、`source_snapshot_ready`、`harbor_available`、`docker_available`、`api_key_available` 和 `dependency_lock_available` 必须全部为 true；同时保留“ready 必须有 acceptance 重算”的约束。
- 测试：新增 ready flag 与 readiness 不一致的负向测试。
- 验证：最终 dry-run 输出 `strict_decision: missing`、`strict_acceptance_recomputed: False`、`ready_to_execute: False`，未调用模型；论文一致性审计通过 1539 项；全量 pytest 通过 179 项；`compileall` 与 `git diff --check` 通过。

## 2026-08-12：第 90 轮 - 论文审计动态核对 strict acceptance 状态

- 位置：`paper/audit_paper_consistency.py`、`README_REPRODUCTION.md`、`tests/test_experiment_document_consistency.py`。
- 发现：execution plan 审计此前固定要求 `strict_decision=missing`；Clean64 严格 acceptance 完成后，即使 artifact 合法，论文审计也会误报失败，无法支持正式 EI 收口。
- 修改：审计现在读取 plan 指向的 acceptance artifact：不存在时要求 `strict_decision=missing` 且 `strict_acceptance_recomputed=false`；存在时动态读取其 decision，并调用统一 `verify_acceptance_artifact()` 验证真实重算状态。同时，存在的 strict artifact 被加入审计输入快照，防止审计期间发生 TOCTOU 漂移。
- 文档：复现手册说明当前缺失状态只是“尚未完成”的事实，不会被误写成实验成功或失败结论。
- 验证：当前无 strict artifact 时，dry-run/审计仍保持 `missing/false`；新增动态 decision 测试通过。论文一致性审计通过 1538 项，全量 pytest 通过 178 项，`compileall` 与 `git diff --check` 通过；正式 Sealed21 仍未调用模型。

## 2026-08-12：第 89 轮 - execution plan 门禁语义审计

### 1. 让论文审计核对新 acceptance 门禁

- 位置：`workflow/scripts/run_qwen_sealed21.ps1`、`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`。
- 发现：第 87–88 轮已在 launcher 中计算并输出 `strict_acceptance_recomputed`，但 execution plan 顶层和论文审计尚未把该状态作为不变量核对；手工修改计划可能造成 readiness 与实际验收状态脱节。
- 修改：计划新增顶层 `strict_acceptance_recomputed`；审计现在检查该字段和 `readiness.strict_acceptance_recomputed` 都是布尔值、二者一致，并要求 `ready_to_execute=true` 时验收必须已重算通过。
- 测试：新增计划篡改负向测试，篡改顶层重算状态后审计必须失败。

### 2. 本轮验证

- Sealed21 dry-run 输出 `strict_decision: missing`、`strict_acceptance_recomputed: False`、`ready_to_execute: False`，未调用模型。
- 验证：Sealed21 dry-run 输出 `strict_decision: missing`、`strict_acceptance_recomputed: False`、`ready_to_execute: False`，未调用模型；论文一致性审计通过 1537 项；全量 pytest 通过 177 项；`compileall` 与 `git diff --check` 通过。

## 2026-08-12：第 88 轮 - 执行前 acceptance 状态可观测性与复现手册同步

- 位置：`workflow/scripts/run_qwen_sealed21.ps1`、`README_REPRODUCTION.md`、`tests/test_sealed_launcher.py`。
- 修改：dry-run 现在显式输出 `strict_acceptance_recomputed`，使“严格决策缺失”和“摘要未通过重算”在执行计划与终端输出中可区分；重算失败的 verifier 输出被捕获为阻断状态，不会因 stderr 直接中断计划写出。
- 修改：复现手册补充 launcher 在模型调用前执行 `--verify-artifact`、要求 `strict_acceptance_recomputed=true`，并说明 launcher/analyzer 共享 `verify_acceptance_artifact()`。
- 验证：dry-run 通过并明确输出 `strict_decision: missing`、`strict_acceptance_recomputed: False`、`ready_to_execute: False`；未调用模型。论文一致性审计通过 1533 项，全量 pytest 通过 176 项，`compileall` 与 `git diff --check` 通过。

## 2026-08-12：第 87 轮 - 将 acceptance 重算门禁前移到模型执行前

### 1. 消除“先付费执行、后发现验收摘要漂移”的窗口

- 位置：`acceptance/scripts/run_acceptance_gate.py`、`paper/analyze_sealed.py`、`workflow/scripts/run_qwen_sealed21.ps1`。
- 发现：第 86 轮已要求分析器在统计前从 Clean64 源结果重算 acceptance，但旧 launcher 仍可能仅凭 artifact 的 `accepted`、路径和 SHA256 进入模型执行；若 `reason` 或 split 摘要被篡改，错误会延迟到 Sealed 运行结束才暴露。
- 修改：新增 `verify_acceptance_artifact()` 和 `--verify-artifact` CLI 模式，统一检查 artifact 结构、Clean64 源绑定、源结果稳定性，并逐字段比较 gate 重算结果。Sealed launcher 在设置 `strictReady` 前调用该模式，重算失败时只写入 `strict_acceptance_recomputed=false` 并阻止模型调用。
- 修改：Sealed analyzer 改为复用同一个 verifier，避免 launcher 与统计分析维护两套 acceptance 语义。

### 2. 回归测试与验证

- 新增 CLI 正向验证和 `reason` 摘要篡改负向测试；启动器静态测试要求 `--verify-artifact`、`--expected-repeats 2` 和 `strict_acceptance_recomputed` 门禁存在。
- 验证：Sealed21 dry-run 通过（21 tasks、84 evaluation cells、52.5 h 配置上界，未调用模型）；论文一致性审计通过 1533 项；全量 pytest 通过 176 项；`compileall`、`git diff --check` 通过。正式 Sealed21 仍因 strict decision 缺失而未执行。

## 2026-08-12：第 86 轮 - strict acceptance 摘要可重算与 gate 源码绑定

### 1. 将验收摘要升级为可重算证据

- 位置：`paper/analyze_sealed.py`、`acceptance/scripts/run_acceptance_gate.py`、`workflow/scripts/run_qwen_sealed21.ps1`。
- 发现：第 85 轮已修正 strict artifact 必须绑定 freeze 中的 Clean64 源结果，但仅检查 artifact 内部结构、路径和哈希，仍不能排除摘要字段被独立篡改后继续通过。
- 修改：分析器加载 freeze 绑定的 Clean64 baseline/candidate `result.json`，调用同一 `run_acceptance_gate()` 重新计算 `accepted`、`decision`、`reason`、`rule` 和 Train/Heldout comparison，并逐字段与 artifact 比较；任一重算漂移立即拒绝确认性分析。
- 修改：Sealed launcher 将 `acceptance/scripts/run_acceptance_gate.py` 的路径和 SHA256 纳入 analysis code bundle 及 bundle canonical hash，使验收规则实现与分析身份共同冻结。

### 2. 测试与文档

- 测试夹具改为提供完整的 Clean64 两 split、两 repeat、带 verifier reward 的最小结果，并覆盖 gate 摘要可重算路径；启动器静态测试检查 gate 源码纳入 bundle。
- `SEALED_PROTOCOL.md`、中英文实验章节补充“从冻结 Clean64 源重算 acceptance，且 gate 源码纳入 analysis bundle”的审计说明。
- 验证：Sealed21 dry-run 通过（21 tasks、84 evaluation cells、52.5 h 配置上界，未调用模型）；论文一致性审计通过 1533 项；全量 pytest 通过 176 项；`compileall`、`git diff --check` 通过。正式 Sealed21 仍因 strict decision 缺失而未执行。

## 2026-08-12：第 1 轮 - 严格验收与投稿级统计

### 1. 恢复基础设施完整性门控

- 位置：`acceptance/scripts/run_acceptance_gate.py`
- 修改：存在 unresolved `status=invalid` 的 baseline 或 candidate 时直接终止验收，不再把基础设施故障静默当作普通失败。
- 原因：环境故障与模型行为不可混合用于 promotion，否则候选可能因不完整数据被错误接受。
- 验证：`tests/test_acceptance_invalid_trials.py` 通过。

### 2. 修复 Harbor 配置向后兼容性

- 位置：`eval/scripts/run_harbor_eval.py`
- 修改：`Config.agent_timeout_multiplier` 保留为可选配置，并提供默认值 `None`；旧调用无需显式传参。
- 原因：新增 Harbor 参数后，既有测试和调用方出现构造函数缺参错误。
- 验证：Harbor retry/checkpoint 测试全部通过。

### 3. 增加严格决策可复现审计

- 位置：`paper/analyze_experiments.py`
- 修改：逐候选读取 baseline/candidate 结果，统计 invalid 数量并输出 `strict_gate_reproducible`。
- 发现：当前 6 个历史候选决策均无法在严格门控下直接复现；Qwen 的历史接受候选为 `3→25` 个 invalid。
- 输出：`paper/generated/statistics.json`、`candidates.csv` 和 `STATISTICAL_AUDIT.md`。

### 4. 增加投稿级图表

- 位置：`paper/generated/effect_sizes.svg`、`paper/generated/invalid_runs.svg`
- 修改：新增任务聚类 95% CI 效应量图和基础设施无效单元审计图，同时提供 PNG 预览。
- 原因：原性能柱状图没有置信区间，也没有展示 invalid 的可靠性风险。
- 验证：已渲染并人工检查标题、标签、误差线、图例和数值，无重叠或裁切。

### 5. 降低论文结论强度并明确补跑条件

- 位置：`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 修改：将 `final/promoted harness` 改为 `candidate/provisional`；区分历史固定分母描述性结果与当前严格验收；明确 Qwen 需补跑 baseline 3 个、candidate 25 个 invalid 后才能确认 promotion。
- 原因：统计显著不等于验收有效，未解决的基础设施故障会影响内部效度。

### 6. 增加统计回归测试

- 位置：`tests/test_experiment_analysis.py`
- 修改：覆盖精确 McNemar、任务级 repeat 聚类和严格门控可复现状态。
- 验证：完整测试套件 `43 passed`，`git diff --check` 通过。

## 下一轮优先级

1. 使用现有 checkpoint 只补跑 unresolved invalid，不重复有效单元。
2. 补跑完成后重新执行严格 acceptance gate，并自动更新论文表格与结论。
3. 对 baseline 与严格通过的候选增加至少 5 次 repeat。
4. 增加不参与 proposal 和 promotion 的 sealed split，给出无偏最终泛化结果。

## 2026-08-12：第 2 轮 - 可审计补跑与严格收口

### 1. 将 invalid 补跑改为显式执行

- 位置：`eval/scripts/rerun_invalid_cases.py`
- 修改：默认只生成 JSON plan；只有传入 `--execute` 才清除 checkpoint 并调用模型。
- 原因：旧工具默认执行付费补跑，且缺少可审计的运行前清单。

### 2. 保留原始 Harbor 证据

- 位置：`eval/scripts/rerun_invalid_cases.py`
- 修改：不再删除 invalid case 的 `harbor/` 目录；旧 aggregate、repeat 和 case JSON 先归档到 `rerun_history/<timestamp>/`。
- 原因：原始 job、异常和 verifier 产物是实验审计证据，不能在补跑前销毁。

### 3. 支持候选 harness 的正确补跑

- 位置：`workflow/scripts/rerun_clean64_invalid.ps1`
- 修改：增加 `-CandidateId`，从 `candidate_queue.json` 精确解析 candidate output 和 materialized workspace；执行时显式传递 `SELF_HARNESS_CANDIDATE_WORKSPACE`。
- 原因：旧脚本只能补跑 baseline；直接用于候选会错误加载 baseline harness。

### 4. 生成精确范围与上界

- 位置：`paper/generated/rerun-plans/`、`paper/generated/RERUN_PLAN.md`
- 结果：DeepSeek baseline 9 个、三个候选分别 46/127/123 个，Qwen baseline 3 个、接受候选 25 个、两个拒绝候选均为 0 个 invalid。Qwen 严格 promotion 最小补跑范围为 28 个单元、最多 84 次模型尝试、配置墙钟上界 22.75 h；完整跨模型候选消融上界约 270.57 h。

### 5. 增加一键严格收口

- 位置：`workflow/scripts/finalize_clean64_strict.ps1`
- 修改：补跑完成后重新运行 strict acceptance，写入独立的 `acceptance.strict.json`，随后重建统计表；不覆盖历史 acceptance。
- 位置：`paper/analyze_experiments.py`
- 修改：存在 strict artifact 时优先读取，并记录决策来源为 `strict` 或 `historical`。

## 2026-08-12：第 3 轮 - 独立 Sealed21 确认实验

### 1. 构建真正未参与选择的冻结测试集

- 位置：`eval/scripts/build_sealed_split.py`、`configs/splits/sealed21.json`。
- 修改：从本地 89 个任务中确定性排除 Clean64 的 64 个任务和 4 个含本地媒体输入的任务，纳入剩余全部 21 个纯文本任务；不做人工结果导向挑选。
- 完整性：冻结逐任务目录 SHA256、Clean64 case 集合哈希及 manifest lock；冻结时扫描现有 `runs/**/result.json`，确认 Sealed21 case ID 的历史引用数为 0。
- 验证：构建/校验、任务篡改检测、历史结果引用拒绝测试通过。

### 2. 在模型调用前冻结确认性统计协议

- 位置：`configs/experiments/ei_confirmation_v1.json`、`paper/SEALED_PROTOCOL.md`。
- 修改：预注册唯一主对比、任务聚类 Bootstrap 95% CI、双侧任务级配对符号置换检验、`alpha=0.05`、严格缺失规则和禁止结果回流规则。
- 成功标准：`delta > 0`、CI 下界大于 0、`p < 0.05`，且两侧均无 unresolved invalid。
- 原因：避免在观察 Sealed21 结果后改变终点、缺失处理或成功阈值。

### 3. 增加有前置门控的成对启动器

- 位置：`workflow/scripts/run_qwen_sealed21.ps1`。
- 修改：默认 dry-run，只验证划分并写出 84 个统计评测单元的执行计划；只有显式 `-Execute` 且 `acceptance.strict.json` 为 `accepted` 时才允许调用模型。每个单元最多 2 次基础设施重试，与预注册缺失策略一致；最大尝试口径的配置墙钟上界为 52.5 h。
- 冻结：执行前记录 baseline、candidate surface 和 Sealed21 manifest 的 SHA256；baseline/candidate 连续完成后才运行分析。
- 验证：PowerShell 语法通过；dry-run 未调用模型；当前显式执行因严格验收缺失而按预期阻断。

### 4. 增加确认性结果分析与回归测试

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`。
- 修改：要求精确匹配 21 任务 × 2 attempts 的配对键，拒绝 invalid、不同模型和缺失/额外单元；输出 JSON、Markdown 和类别 CSV。
- 验证：合成显著改善、invalid 拒绝和键不匹配三类测试通过。

### 5. 调整计算预算优先级

- 位置：中英文实验章节、`README_REPRODUCTION.md`。
- 修改：原论文使用每候选 2 次 repeated attempts，因此确认性主路径保留 2 次，并优先完成独立 Sealed21；Clean64 5-repeat 降为可选精度/稳定性扩展，不再作为 EI 实验完成的硬性门槛。
- 当前状态：Sealed21 已冻结但未执行；必须先补跑 Qwen 的 28 个 invalid 单元并通过严格 promotion。

## 2026-08-12：第 4 轮 - 历史结果有效性重审与机制证据链

> 本轮发现表明前 1-3 轮仅按字面 `status=invalid` 得到的 3/25、9/46/127/123 等数量不完整；以下有效 invalid 口径取代此前补跑估计。

### 1. 依据原论文补做实验差距审计

- 来源：`Self-Harness.pdf` 第 4.3 节和附录 A.1，并完成相关页面渲染核对。
- 发现：原论文除总体分数外，还要求演化/保留修改和代表性前后轨迹形成机制证据；当前复现缺少统一的“诊断→修改→直接目标结果”链路。

### 2. 修复历史基础设施故障误标

- 位置：`eval/scripts/result_validity.py`。
- 修改：有效性判定同时检查显式 status、verifier reward、runtime failure，以及 reward/passed/status 一致性；兼容不含 Harbor reward 字段的离线 fixture。
- 发现：旧运行器把大量 API 连接失败、账户欠费、wrapper 异常和缺失 verifier reward 写成普通 `failed`。Qwen baseline 有效 invalid 从 3 修正为 105，历史接受候选从 25 修正为 78；DeepSeek baseline 从 9 修正为 51。
- 原因：没有 verifier 结果的运行既不是行为失败，也不能进入 promotion 或显著性检验。

### 3. 统一所有门控与报告入口

- 位置：`rerun_invalid_cases.py`、`run_acceptance_gate.py`、`finalize_clean64_strict.ps1`、`analyze_experiments.py`、`analyze_sealed.py`、Clean64 live/final report。
- 修改：所有入口调用同一有效 invalid 判定；最终报告遇到有效 invalid 直接拒绝生成，不再仅打印 warning。
- 验证：严格收口当前准确阻断并报告 `baseline invalid=105, candidate invalid=78`。

### 4. 重建真实补跑计划与论文结论

- 位置：`paper/generated/rerun-plans/`、`RERUN_PLAN.md`、中英文实验章节。
- 修改：Qwen 严格 promotion 最小范围修正为 183 个单元、最多 549 次模型尝试、配置上界 148.69 h；全部八组消融为 818 个单元、上界 664.63 h。
- 结论调整：Qwen Train 共同有效配对为 0，Heldout 仅 18，因此撤回旧 CI、置换检验和 McNemar 显著性解释，统一标记为 incomplete。

### 5. 增加机制级配对证据审计

- 位置：`paper/audit_mechanism_evidence.py`、`paper/generated/mechanism/`。
- 修改：连接诊断簇、候选 hook、预声明目标和全部配对转移，区分 valid fail→pass 与 invalid-involved。
- 结果：`mailman#repeat-02` 的 candidate 侧为账户欠费导致的 runtime failure，直接机制未确认；全部 128 对中 110 对涉及 invalid，仅 18 对双方有效。
- 验证：合成直接确认/无效目标测试通过，JSON、CSV、Markdown 和 SVG 均可重建。

## 2026-08-12：第 5 轮 - 防止新增隐性无效样本与可执行分阶段补跑

### 1. 修复 Harbor 运行异常的判定顺序

- 位置：`eval/scripts/run_harbor_eval.py`、`tests/test_harbor_infrastructure_retry.py`。
- 修改：先检查 `exception_info`，再解释 reward；任意 Harbor 异常（含 `AgentTimeoutError`、provider/API 异常和 Docker compose 异常）均进入基础设施重试。缺失或非数值 reward 同样判为 invalid；只有不存在异常且 reward 为数值时才作为行为结果。
- 原因：旧逻辑先用 `reward != 0` 判断通过，使 `None` 或异常后残留的正 reward 绕过基础设施分类，产生新的隐性 invalid。
- 验证：覆盖“异常优先于残留正 reward”“APIConnectionError 无 reward”“已有 trial 缺失 reward”三类回归测试。

### 2. 将无效原因结构化

- 位置：`eval/scripts/result_validity.py`、`eval/scripts/rerun_invalid_cases.py`。
- 修改：统一归类 `timeout/provider/missing_reward/missing_trial/dependency/docker/inconsistent_result/other`，并在重跑计划写入 `reason_counts`。
- 结果：Qwen 最小路径 183 个待补单元中，provider 152、timeout 27，其余 dependency、missing_reward、inconsistent_result、other 各 1。该分布支持先做基础设施 canary，而不是直接启动全量补跑。

### 3. 生成可执行的配对分阶段清单

- 位置：`paper/build_paired_rerun_plan.py`、`paper/generated/paired-rerun/`、`workflow/scripts/rerun_clean64_invalid.ps1`。
- 修改：按配对缺失结构生成 6 个精确 JSON manifest：Phase 0 为 1+1 canary，Phase 1 为 baseline 32 + candidate 5，Phase 2 为 73+73；启动器新增 `-CellsFile`，只归档和补跑清单指定的无效单元。
- 防偏规则：canary 固定且只判断是否产生数值 verifier 结果；每阶段后必须重建计划，已变为有效的单元会令旧 manifest 失败，禁止结果驱动的清单调整。
- 安全性：阶段 dry-run 使用独立计划文件名，不覆盖八组完整计划；选定阶段清零即可正常结束，即使尚有后续全局 invalid。

### 4. 本轮验证

- 聚焦回归测试：26 项通过。
- 实际 manifest 数量：`1/1`、`32/5`、`73/73`，总计 183 个唯一待补单元（Phase 0 与后续阶段重叠，需阶段后重建）。
- baseline 与 candidate canary 均完成 dry-run，分别生成 1-cell、最多 3 次尝试、0.81 h 配置上界的计划；未读取 API key，未发生模型调用。

## 2026-08-12：第 6 轮 - Sealed21 证据身份与精确确认性统计

### 1. 将冻结身份绑定到评测结果

- 位置：`run_harbor_eval.py`、`run_qwen_sealed21.ps1`、`analyze_sealed.py`。
- 修改：baseline/candidate 分别记录由 Sealed manifest、预注册文件、实验角色和 harness surface SHA256 构成的 `run_identity`；输出目录写入不可混用的身份 marker。
- 原因：此前 freeze 只能说明计划冻结了什么，结果文件不能证明自身来自该冻结状态；无身份的 `--reuse-existing` 也可能复用来源不明的旧 checkpoint。
- 门控：身份不一致、存在旧 checkpoint 但缺少 marker、角色交换、严格验收或 harness/划分/预注册哈希变化均拒绝复用或分析。

### 2. 将任务级置换检验改为完全精确

- 位置：`paper/analyze_experiments.py`、`tests/test_experiment_analysis.py`。
- 修改：使用 `Fraction` 表示任务聚合效应，通过动态规划累计全部符号组合的精确零分布。
- 原因：旧实现超过 20 个非零任务时自动使用 100,000 次 Monte Carlo，但预注册未声明近似，极小 p 值还受随机精度下限影响。
- 验证：21 个任务全部同方向时精确得到 `2 / 2^21 = 0.000000953674...`。

### 3. 增加结果盲设计分辨率审计

- 位置：`paper/audit_confirmation_design.py`、`paper/generated/design-audit/`。
- 修改：不读取任何 Sealed21 结果，枚举 0–21 个规范有利任务簇并同时计算 20,000 次任务 Bootstrap CI 和精确置换 p。
- 结果：在每个有利任务仅一个 repeat 改善且无退化的规范模式下，成功规则的首次可达点为 6 个任务簇、+14.29 pp、95% CI `[4.76, 23.81]` pp、`p=0.03125`。
- 限定：这是样本量分辨率边界，不是功效模型、期望效果或对尚未运行的 Sealed21 结果的推断。

### 4. 修复论文脚本独立入口

- 位置：`paper/analyze_sealed.py`、`paper/audit_confirmation_design.py`。
- 修改：显式将仓库根目录加入模块搜索路径，使 `python paper/<script>.py` 在未 editable-install 的干净 checkout 中也可执行。

### 5. 本轮验证

- 全量测试：76 项通过；Python 编译、预注册 JSON 解析、PowerShell 语法和 `git diff --check` 通过。
- Sealed21 dry-run：21 个任务哈希验证通过，`ready_to_execute=False`、严格验收仍缺失；未创建 freeze、baseline/candidate 输出目录，模型调用为 0。
- 产物不变量：计划中的预注册哈希与当前文件一致，baseline/candidate 身份不同；设计审计首次成功边界固定为 6 个任务簇、`p=0.03125`；统计 JSON 明确记录 exact permutation 方法。

## 2026-08-12：第 7 轮 - Sealed21 任务覆盖与外部效度边界

### 1. 新增结果盲划分覆盖审计

- 位置：`paper/audit_split_coverage.py`、`paper/generated/split-coverage/`、`tests/test_split_coverage_audit.py`。
- 修改：仅从任务 `task.toml` 和冻结 manifest 读取 category/difficulty，不读取模型输出；完整核对 89 个任务的互斥归属，输出 JSON、CSV、Markdown 和 SVG。
- 原因：任务互斥只能证明未泄漏，不能证明 Sealed21 对目标任务类别总体有代表性。

### 2. 量化划分差异

- 任务记账：Clean64 64 + Sealed21 21 + 媒体能力边界排除 4 = 本地全集 89；Clean64/Sealed21 重叠为 0。
- 难度：分布接近，total variation=0.062，Jensen–Shannon=0.032 bits；hard 比例为 32.8%/33.3%。
- 类别：差异明显，total variation=0.480，Jensen–Shannon=0.320 bits。Sealed21 的 data-science 为 23.8%，Clean64 为 3.1%；Sealed21 缺少 7 个 Clean64 类别并新增 games/optimization。

### 3. 收紧论文泛化表述

- 位置：中英文实验章节、`SEALED_PROTOCOL.md`、预注册文件和复现手册。
- 修改：Sealed21 成功只支持对“结果盲、互斥的本地纯文本任务集合”泛化，不支持完整 Terminal-Bench 类别总体、媒体任务或跨模型普适性；禁止依据类别差异事后重加权或替换任务。
- 原因：类别分布偏移较大，若继续使用无边界的“独立泛化”表述会高估外部效度。

### 4. 本轮验证

- 全量测试：79 项通过；新增覆盖测试验证任务记账、分布距离、漏记拒绝和 SVG 几何边界。
- 产物不变量：`89=64+21+4`、Clean64/Sealed21 overlap=0、category TV=0.480、difficulty TV=0.062；JSON、CSV、Markdown、SVG 可重复生成。
- 最新 Sealed21 dry-run 已重新绑定更新后的预注册 SHA256，仍为 `ready_to_execute=False`；未创建 freeze 或结果目录，模型调用为 0。

## 2026-08-12：第 8 轮 - 执行配置与任务内容的端到端冻结证明

### 1. 修复运行身份未覆盖 TOML 的缺口

- 位置：`run_harbor_eval.py`、`run_qwen_sealed21.ps1`、`analyze_sealed.py`。
- 修改：`run_identity` 新增 Sealed TOML SHA256；运行器在解析配置前核验 `--expected-config-sha256`，并把实际哈希写入身份 marker、resolved config 和最终 `result.json`。
- 原因：此前相同任务和 harness 可以在 repeats、超时、重试、并发或 agent 参数被修改后仍携带相同冻结身份，无法证明实验条件一致。

### 2. 将规范配置和任务内容纳入分析门控

- 位置：`build_sealed_split.py`、`analyze_sealed.py`。
- 修改：Sealed TOML 必须逐字节等于根据冻结任务顺序生成的规范配置；分析前重新哈希 21 个任务目录，并核对两侧结果记录的配置哈希。
- 拒绝条件：配置字节变化、配置哈希不匹配、任一任务内容变化、身份/角色或其他 freeze 输入变化。

### 3. 升级证据格式版本

- `run_identity.v2`：新增配置哈希。
- `sealed_execution_plan.v2`、`sealed_freeze.v2`：冻结规范 TOML 路径及 SHA256。
- `sealed_analysis.v3`：输出已核验任务数量、配置路径/哈希和完整来源证据。

### 4. 本轮验证

- 聚焦篡改测试：25 项通过，覆盖规范配置字节变化、身份相同但配置哈希不同、任务内容变化和角色交换。
- 全量测试：82 项通过；Python 编译、预注册 JSON、PowerShell 语法与 `git diff --check` 通过。
- 实际 dry-run：规范 TOML 验证通过，计划为 `sealed_execution_plan.v2`，配置 SHA256 已包含在两侧身份中；`ready_to_execute=False`，未创建 freeze/结果目录，模型调用为 0。

## 2026-08-12：第 9 轮 - 执行源码冻结与环境可复现性

### 1. 冻结结果生成与分析源码

- 位置：`run_qwen_sealed21.ps1`、`analyze_sealed.py`。
- 修改：结果身份新增 runner/Harbor wrapper/backend bridge 的规范 bundle SHA256；freeze 另存分析器、统计核心、划分构建器和有效性分类器的逐文件哈希与 bundle 哈希。
- 原因：相同任务、TOML 和 harness 在 evaluator/wrapper 代码变化后仍可能产生不同结果，不能把实现变化归因于 harness。
- 门控：分析前重新哈希全部源码；单文件或组合哈希不一致均拒绝确认性结论。

### 2. 记录执行环境快照

- 字段：UTC 时间、OS/架构、Python executable/version、Harbor executable/version、Docker client/server、Git HEAD/dirty、模型别名和 endpoint。
- 隐私：只记录 API key 是否可用，不写入 key 内容。
- 当前 dry-run：Python 3.13.12、Harbor 0.20.0、Docker client/server 29.3.1、Git HEAD `bb2924aec2e285f34dc8cd5711cb2d3d6478cff2`，worktree dirty。

### 3. 将 readiness 改为组合门控

- `ready_to_execute` 现在同时要求：严格验收 accepted、Harbor 可用、Docker client/server 可用、API key 存在、源码 snapshot 就绪。
- dirty worktree 默认阻止付费执行；`-AllowDirtyWorktree` 是显式且会被记录的例外，不建议用于最终论文运行。
- 原因：旧字段只反映严格验收，可能在环境尚不可运行时错误显示 ready。

### 4. 本轮验证

- 全量测试：83 项通过；新增执行源码篡改拒绝测试通过。
- 跨语言不变量：PowerShell 生成的两个 bundle SHA256 与 Python 分析器按规范序列化重算值完全一致，逐文件哈希均匹配。
- dry-run readiness：strict=false、clean Git=false、Harbor=true、Docker=true、API key=true，因此总体 false；未创建 freeze 或结果目录，模型调用为 0。
- Python 编译、预注册 JSON、PowerShell 语法和 `git diff --check` 通过。

## 2026-08-12：第 10 轮 - Python 依赖锁与容器镜像漂移审计

### 1. 将双 Python 环境依赖锁纳入运行身份

- 位置：`eval/scripts/capture_environment_lock.py`、`run_qwen_sealed21.ps1`、`analyze_sealed.py`。
- 修改：分别捕获项目解释器和 Harbor 自身解释器的排序 `pip freeze --all`、Python executable SHA256/version，以及 Harbor CLI SHA256/version；规范 JSON 生成稳定 bundle SHA256。
- 稳定性：捕获时间和机器路径保留用于审计，但不进入 bundle，因此相同依赖环境在重试时身份保持稳定。
- 当前结果：项目环境 127 个包、Harbor 环境 312 个包，bundle=`aab646f95ff4fcdfbcf20ffcfe9eb76e2fbf00fc9518806821c789db57eb2da1`。

### 2. 审计任务基础镜像不可变性

- 位置：`paper/audit_container_reproducibility.py`、`paper/generated/container-audit/`。
- 结果：89 个任务、90 个 `FROM` 引用；0 个 digest-pinned，90 个显式 tag，0 个 floating/latest；0/89 个任务的全部基础镜像可由 Dockerfile 静态证明内容不可变。
- 主要引用：`python:3.13-slim-bookworm` 41 次、`ubuntu:24.04` 40 次，其余 9 次分布于 5 个显式 tag。
- 原因：任务目录哈希只能固定 Dockerfile 文本，registry 仍可能将相同 tag 重新指向不同镜像内容。

### 3. 收紧复现结论

- 修改：论文明确区分“显式版本 tag”与“不可变 digest”，不把任务哈希误写为完整容器内容固定。
- 决策：Sealed21 已冻结，不事后修改 Dockerfile；保留静态审计并把基础镜像 tag 与远程模型别名共同列为时间相关复现限制。

### 4. 证据格式升级

- `sealed_execution_plan.v3`、`sealed_freeze.v3`：新增完整 dependency lock。
- `sealed_analysis.v4`：输出 dependency bundle 及项目/Harbor 包数量，并在分析前验证锁的规范哈希。

### 5. 本轮验证

- 全量测试：87 项通过；依赖锁时间/路径稳定性、包变更拒绝、Docker 多阶段/标签分类测试通过。
- 重复捕获相同环境后 bundle 保持 `aab646f95ff4fcdfbcf20ffcfe9eb76e2fbf00fc9518806821c789db57eb2da1`，并已进入 baseline/candidate 身份。
- 容器产物不变量：89 tasks、90 FROM、`explicit_tag=90`、`digest_pinned=0`、全 digest 任务数 0。
- Python 编译、预注册 JSON、PowerShell 语法、`git diff --check` 通过；dry-run 未调用模型。

## 2026-08-12：第 11 轮 - 基础镜像实际解析证据

### 1. 增加无 pull 的动态镜像解析快照

- 位置：`eval/scripts/capture_container_resolution.py`、`tests/test_container_resolution.py`。
- 修改：从 Sealed manifest 精确提取 21 个任务使用的基础镜像引用，调用只读 `docker image inspect`，记录 image ID、RepoDigest/RepoTag、创建时间、OS/架构和任务引用位置；明确 `pull_performed=false`。
- 稳定 bundle：规范化 case IDs、Docker 版本和逐引用内容身份，排除捕获时间与 executable 路径。

### 2. 接入正式成对运行与分析

- 位置：`run_qwen_sealed21.ps1`、`analyze_sealed.py`。
- 顺序：baseline 完成 → candidate 完成 → 捕获本地镜像解析 → 分析。
- 门控：分析器要求快照 bundle 有效且 case IDs 精确等于 Sealed21；输出 resolved/total、complete 和快照 SHA256。解析不完整单独报告，不事后改变预注册统计成功规则。
- 格式：执行计划升级为 `sealed_execution_plan.v4`，Sealed 分析升级为 `sealed_analysis.v5`。

### 3. 当前结果盲预览

- Sealed21 使用 5 个唯一基础镜像 tag。
- 当前 Docker 缓存解析 0/5；这是正式任务镜像尚未构建前的预期状态。
- 预览没有 pull、没有运行任务、没有读取模型输出；正式解析文件只在两侧运行都结束后生成。

### 4. 本轮验证

- 全量测试：90 项通过；新增测试覆盖“仅检查本地镜像、不执行 pull”、镜像解析身份变化导致 bundle 变化，以及 Sealed21 任务集合不匹配时拒绝分析。
- 静态检查：Python 编译、确认性实验 JSON 解析、PowerShell 语法和 `git diff --check` 均通过。
- 产物不变量：dry-run 重新生成 `sealed_execution_plan.v4`；执行代码 bundle 可按规范重算，且已包含动态镜像解析脚本。
- 预览证据：21 个 Sealed 任务共引用 5 个唯一基础镜像，当前本地解析 0/5，`complete=false`、`pull_performed=false`，bundle 为 `38070cb1ab1eecc96afdbb0288345ad37e44ba5670dc3f5be39383ede4efba84`。
- 执行安全：正式 `runs/sealed21-qwen-container-resolution.json` 仍不存在；本轮未创建正式结果、未拉取镜像、未发生模型调用。

## 2026-08-12：第 12 轮 - 跨运行时 JSON 编码兼容性

### 1. 修复 PowerShell→Python 的 BOM 交接风险

- 位置：`workflow/scripts/run_qwen_sealed21.ps1`、`paper/analyze_sealed.py`。
- 原因：Windows PowerShell 5 的 `Set-Content -Encoding UTF8` 会写入 UTF-8 BOM，而 Python 的严格 `utf-8` 读取可能在正式任务结束后的分析阶段失败。
- 修改：启动器新增 `Write-Utf8NoBom`，以无 BOM UTF-8 写出 execution plan 与 freeze；分析器改用 `utf-8-sig`，兼容标准 UTF-8 和历史 BOM JSON，不改变任何统计或身份字段。

### 2. 增加编码回归保护并同步协议

- 位置：`tests/test_sealed_analysis.py`、`tests/test_sealed_launcher.py`、`configs/experiments/ei_confirmation_v1.json`、`paper/SEALED_PROTOCOL.md`、`README_REPRODUCTION.md`、中英文实验章节。
- 修改：增加旧版 BOM JSON 读取测试、启动器无 BOM 写入静态回归测试；将跨运行时编码规则写入确认性配置、协议和论文的实现效度/复现性说明。

### 3. 本轮验证

- 聚焦测试：9 项通过；覆盖 BOM 兼容性和启动器写入规则。
- dry-run：通过，明确 `no model calls`；计划仍为 `self_harness.sealed_execution_plan.v4`，`ready_to_execute=false`。
- 字节级产物检查：`paper/generated/sealed21-execution-plan.json` 不以 `EF BB BF` 开头，可直接用标准 UTF-8 解析；正式 freeze 与容器解析文件均未创建。
- 安全边界：未执行模型调用、未拉取 Docker 镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 13 轮 - 动态镜像证据结构校验

### 1. 收紧快照语义不变量

- 位置：`eval/scripts/capture_container_resolution.py`、`tests/test_container_resolution.py`。
- 原因：此前 bundle 哈希正确并不保证 `reference_count` 等于实际条目数；空镜像列表也可能被错误标记为 `complete=true`，会削弱容器来源证据的可审计性。
- 修改：校验 case IDs 必须为非空、排序且唯一；镜像引用必须唯一；引用计数必须等于条目数；`found`、image ID、resolved count 和 `complete` 必须相互一致。
- 边界保护：新增“计数篡改”和“空证据伪 complete”两个负向测试。

### 2. 同步实验协议表述

- 位置：`configs/experiments/ei_confirmation_v1.json`、`paper/SEALED_PROTOCOL.md`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`。
- 修改：明确动态镜像证据除了 bundle 和任务集合，还要通过引用唯一性、计数一致性和完整度一致性校验；不改变预注册统计成功规则。

### 3. 本轮验证

- 聚焦测试：12 项通过。
- 全量测试：94 项通过。
- 预览快照：5 个引用、0 个已解析、`complete=false`，通过新增结构校验。
- 正式安全边界：正式 freeze、正式镜像解析和正式结果均不存在；本轮未调用模型、未拉取镜像。

## 2026-08-12：第 14 轮 - 镜像引用集合与冻结 Dockerfile 绑定

### 1. 修复引用集合遗漏风险

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`。
- 原因：此前分析器只比较容器快照的 21 个 `case_ids`，没有证明快照中的镜像引用集合覆盖并且仅覆盖冻结任务 Dockerfile 的 `FROM` 引用。
- 修改：分析前从 manifest 指向的 Sealed21 任务目录重新解析 Dockerfile，要求快照引用集合与冻结内容解析出的集合完全相等；任务内容哈希和引用证据因此形成闭环。
- 测试：新增引用集合不匹配的拒绝测试，并让分析正向夹具包含 Dockerfile，验证正常路径仍可生成确认性统计。

### 2. 同步协议和论文

- 位置：`configs/experiments/ei_confirmation_v1.json`、`paper/SEALED_PROTOCOL.md`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`。
- 修改：明确“重新解析冻结 Dockerfile并精确匹配引用集合”是正式分析前提；不改变统计成功标准或容器完整度的独立报告规则。

### 3. 本轮验证

- 聚焦分析与容器测试：13 项通过。
- 正向 Sealed 分析夹具：通过；引用集合完全匹配时继续生成统计结果。
- 负向夹具：任务集合或镜像引用集合不匹配时均拒绝。
- 正式实验安全边界：未调用模型、未拉取镜像、未生成正式结果。

## 2026-08-12：第 15 轮 - Sealed21 固定设计结构门控

### 1. 防止错误规模 manifest 进入确认性分析

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`。
- 原因：仅依赖 manifest 中现有任务列表会把“格式合法但规模错误”的 split 当作 Sealed21，导致 42 个 attempt、21 个 task 的统计前提未被显式验证。
- 修改：新增 `validate_sealed_manifest()`，强制要求 `self_harness.sealed_split.v1`、恰好 21 个任务、`task_count=21`、唯一非空任务 ID、`frozen-before-sealed-evaluation` 结果盲分类和 `prior_result_references=0`。
- 测试：新增错误任务数和重复 ID 两个负向测试；正向确认性分析夹具补齐 manifest 结构字段。

### 2. 同步确认性协议与论文

- 位置：`configs/experiments/ei_confirmation_v1.json`、`paper/SEALED_PROTOCOL.md`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`。
- 修改：把固定设计规模与结果盲结构列为分析前提，明确统计结论只能来自冻结的 21 任务设计。

### 3. 本轮验证

- Sealed 分析聚焦测试：11 项通过。
- 设计门控负向测试：错误规模和重复任务 ID 均被拒绝。
- 正式模型调用、Docker pull 和 Sealed21 结果生成：均未发生。

## 2026-08-12：第 16 轮 - Sealed 结果布尔字段类型门控

### 1. 防止 truthiness 污染确认性统计

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`。
- 原因：如果结果中的 `passed` 被错误写成字符串（例如 `"false"`），Python 的 `bool(...)` 会将其解释为真值，可能把格式损坏的单元计入通过数。
- 修改：`validate_result()` 在 invalid 分类和统计前要求所有 84 个 Sealed 单元的 `passed` 必须是 JSON 布尔值；字符串、数字和空值均拒绝。
- 测试：新增字符串 `"false"` 负向夹具，确认不会进入统计路径。

### 2. 同步结果 schema 与论文

- 位置：`configs/experiments/ei_confirmation_v1.json`、`paper/SEALED_PROTOCOL.md`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`。
- 修改：将 `passed` 的 JSON 布尔类型列为确认性分析前提，明确不使用隐式真值转换。

### 3. 本轮验证

- 聚焦测试：18 项通过。
- 非布尔 `passed`：被拒绝；正常布尔结果分析路径不变。
- 本轮未调用模型、未拉取 Docker 镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 17 轮 - Sealed verifier reward 证据门控

### 1. 防止缺失 reward 被误计为行为失败

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`。
- 原因：历史有效性分类器为了兼容旧 Clean64 产物允许缺失 reward；如果同一宽松规则直接用于 Sealed21，`passed=false/status=failed` 但没有 verifier reward 的基础设施失败可能进入确认性统计。
- 修改：Sealed `validate_result()` 在 invalid 分类前要求每个结果单元存在非空 reward；后续仍由统一分类器检查 reward 类型和 passed/reward 一致性。
- 测试：新增删除全部 reward 的负向夹具；正向分析夹具补齐 `reward=0.0/1.0`。

### 2. 同步协议与论文

- 位置：`configs/experiments/ei_confirmation_v1.json`、`paper/SEALED_PROTOCOL.md`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`。
- 修改：明确 Sealed21 的零 invalid 前提要求数值 verifier reward；历史 Clean64 的兼容逻辑不再隐式扩展到确认性分析。

### 3. 本轮验证

- 聚焦测试：19 项通过。
- 缺失 reward：在统计前被拒绝。
- 正常 reward 结果：确认性分析路径保持通过。
- 本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 42 轮 - 评测单元 repeat 身份不变量

### 1. 收紧结果展开器的单元身份校验

- 位置：`paper/analyze_experiments.py:64`
- 修改：`flatten_result()` 现在要求 `splits`/repeat/case_results 为正确容器类型、外层 repeat 与 case 内 repeat 可转换且完全一致、`case_id` 为非空字符串，并继续拒绝重复 `(split, repeat, case_id)`。
- 原因：此前只按 case 内 repeat 建键；若外层 repeat 标签错误，统计和配对计划可能静默把不同运行配错，导致重复实验结构失真。

### 2. 统一影响统计与补跑计划

- 位置：`paper/build_paired_rerun_plan.py`、`paper/analyze_experiments.py`
- 修改：paired plan 继续复用同一个 `flatten_result()`，因此身份错误会在生成补跑范围前失败，而不是生成可执行但错误的 manifest。
- 原因：统计分析和补跑计划必须共享完全相同的 cell identity，避免一侧接受、另一侧拒绝不同的结果结构。

### 3. 测试、文档与产物

- 位置：`tests/test_experiment_analysis.py`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 验证：新增外层/case repeat 不一致和空 `case_id` 测试；双语实验章节记录身份不变量；Clean64 统计和配对计划重建成功。

## 2026-08-12：第 85 轮 - 修正 strict acceptance 的 Clean64 源绑定

### 1. 修正分析输入角色混淆

- 发现：Sealed analyzer 的 strict acceptance 校验若直接将 artifact 内 baseline/candidate 路径与 Sealed21 结果比较，会错误拒绝真实正式运行；strict acceptance 实际绑定的是 Clean64 源结果。
- 修改：`workflow/scripts/run_qwen_sealed21.ps1` 将 Clean64 baseline/candidate 源路径及 SHA256 写入 freeze；`paper/analyze_sealed.py` 改为对照 freeze 中的严格验收源验证 artifact，并在后置稳定性复核中再次检查这两份 Clean64 文件。
- 影响：保留 artifact 内部绑定门禁，同时恢复与真实启动器角色模型一致的正式执行路径。

### 2. 更新夹具、启动器测试和论文说明

- 位置：`tests/test_sealed_analysis.py`、`tests/test_sealed_launcher.py`、`SEALED_PROTOCOL.md`、README 及中英文实验章节。
- 修改：测试夹具增加 freeze 绑定的 Clean64 源结果；文档明确 strict acceptance 绑定 freeze 中的 Clean64 源，而 Sealed21 baseline/candidate 是待分析结果。
- 验证：Sealed21 dry-run 通过（21 tasks、84 cells、52.5 h，仍未就绪）；论文审计通过 1532 项检查、稳定输入 241 个；全量测试 174 项通过，`compileall` 与 `git diff --check` 通过。本轮未调用模型、未拉取镜像。

## 2026-08-12：第 84 轮 - strict acceptance rule/splits 结构门禁

### 1. 校验验收摘要的完整结构

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`。
- 修改：strict acceptance artifact 除格式、决策和输入哈希外，还必须包含 Train/Heldout 两个 split、每侧 repeat=1/2、pass-rate 指标、无 drop 且至少一个 split 改善的 rule，以及每个 comparison 的有限 delta。
- 原因：仅有 `accepted=true` 和输入哈希不足以证明 acceptance gate 的比较摘要完整；缺失或伪造 split 结构不应成为 Sealed 解锁证据。

### 2. 增加结构篡改测试并同步论文

- 测试：删除 heldout comparison 后，strict artifact 校验必须失败；完整 acceptance 夹具仍能通过正式 Sealed 分析。
- 文档：同步 `SEALED_PROTOCOL.md`、README 及中英文实验章节，明确 acceptance rule/splits 结构是确认性前置条件。
- 验证：Sealed21 dry-run 通过（21 tasks、84 cells、52.5 h，仍未就绪）；论文审计通过 1532 项检查、稳定输入 241 个；全量测试 174 项通过，`compileall` 与 `git diff --check` 通过。本轮未调用模型、未拉取镜像。

## 2026-08-12：第 83 轮 - strict acceptance artifact 内部绑定门禁

### 1. 在 Sealed 分析入口验证验收 artifact 语义

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`。
- 修改：新增 `validate_strict_acceptance_artifact()`，要求 `self_harness.acceptance_gate.v1`、`accepted` 与 `decision` 一致、`source_hashes_stable=true`，并重新计算 artifact 内 baseline/candidate 路径对应的 SHA256。
- 原因：此前 Sealed 只验证 freeze 指向的 acceptance 文件哈希和 `decision=accepted`；artifact 内部路径或 source hash 错绑时，外层文件仍可能满足 readiness。

### 2. 增加内部绑定漂移测试并同步论文

- 测试：篡改 artifact 的 `accepted`/`decision` 一致性，分析器必须拒绝；正式主分析夹具同时验证完整 acceptance artifact 能顺利进入统计。
- 文档：同步 `SEALED_PROTOCOL.md`、README 及中英文实验章节，明确验收 artifact 的格式、语义和输入绑定是 Sealed 前置条件。
- 验证：Sealed21 dry-run 通过（21 tasks、84 cells、52.5 h，仍未就绪）；论文审计通过 1532 项检查、稳定输入 241 个；全量测试 173 项通过，`compileall` 与 `git diff --check` 通过。本轮未调用模型、未拉取镜像。

## 2026-08-12：第 82 轮 - execution plan 与 Sealed TOML 预算绑定

### 1. 从实际 TOML 重算评测规模和墙钟上界

- 位置：`workflow/scripts/run_qwen_sealed21.ps1`、`paper/audit_paper_consistency.py`。
- 修改：启动器和 `sealed_execution_plan_checks()` 都直接解析 `harbor_local_sealed21.toml`，核对 21 个 case、2 repeats、2 次 infrastructure retry、并发 4、timeout 3000 s，并按 `tasks × repeats × roles × (retries+1) × timeout / concurrency / 3600` 重算 52.5 wall-clock hours；计划同时记录 retries/concurrency/timeout 字段。
- 原因：此前启动器的 84 cells/52.5 h 是摘要常量；若 TOML 的 timeout、并发或重试配置被改动，计划可能仍显示旧预算，造成 EI 资源和可复现性描述失真。

### 2. 同步协议、论文和验证范围

- 位置：`paper/SEALED_PROTOCOL.md`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_EN.md`、`paper/EXPERIMENTS_ZH.md`。
- 修改：明确 execution plan 的设计规模和预算必须从签入 TOML 重算，不能只信任 launcher 摘要字段。
- 验证：启动器真实 dry-run 已从 TOML 动态得到 21 tasks、84 cells、52.5 h；论文审计通过 1532 项检查（其中 execution plan 40 项）、稳定输入 241 个；全量测试 172 项通过，`compileall` 与 `git diff --check` 通过。本轮未调用模型、未拉取镜像。

## 2026-08-12：第 81 轮 - 精确置换 p 值的无损报告格式

### 1. 修复极小 p 值显示为 0.0000 的风险

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`。
- 修改：新增 `fmt_p()`；当精确 p 值小于 `1e-4` 时使用科学计数法，否则保留四位小数；同时拒绝非有限或不在 `[0,1]` 的 p 值。
- 原因：21 个任务的双侧精确符号置换检验最小可达 p 值为 `2/2^21≈9.53674e-07`，旧的 `:.4f` 会把它错误显示为 `0.0000`，不符合 EI 论文的可审计数值要求。

### 2. 增加边界回归测试并同步论文

- 测试：确认 `2/2^21` 输出为 `9.53674e-07`，普通 `0.03125` 输出为 `0.0312`，非法 p 值会被拒绝。
- 文档：同步协议、README 及中英文实验章节，明确极小精确 p 值不得显示为零。
- 验证：Sealed21 dry-run 通过（21 tasks、84 cells、52.5 h，仍未就绪）；论文审计通过 1523 项检查、稳定输入 241 个；全量测试 172 项通过，`compileall` 与 `git diff --check` 通过。本轮未调用模型、未拉取镜像。

## 2026-08-12：第 80 轮 - Sealed 主终点统计契约绑定

### 1. 将预注册主终点与统计实现绑定

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`。
- 修改：预注册语义校验新增 verifier pass fraction、每任务两次 attempt、20,000 次任务聚类 Bootstrap、双侧任务级配对符号置换、`alpha=0.05` 和三项成功规则的精确检查，并确认 permutation 实现为有理数动态规划。
- 原因：此前只校验任务规模、seed 和 missingness；若主终点文本中的 Bootstrap 次数、检验或成功阈值发生漂移，文件哈希仍可能通过但统计实现已不再对应预注册。

### 2. 增加统计契约漂移测试并同步论文

- 测试：将预注册 `primary_endpoint.alpha` 从 0.05 改为 0.10，分析器必须拒绝。
- 文档：同步 `SEALED_PROTOCOL.md`、README 及中英文实验章节，明确主终点契约不得脱离实现独立变化。
- 验证：Sealed21 dry-run 通过（21 tasks、84 cells、52.5 h，仍未就绪）；论文审计通过 1523 项检查、稳定输入 241 个；全量测试 171 项通过，`compileall` 与 `git diff --check` 通过。本轮未调用模型、未拉取镜像。

## 2026-08-12：第 79 轮 - Sealed 全冻结输入的前后稳定性复核

### 1. 扩大分析期间的 TOCTOU 防护范围

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`。
- 修改：新增 `require_stable_frozen_inputs()`；统计完成后再次核验预注册、规范 TOML、严格验收、两侧 harness surface、执行/分析源码 bundle，以及 21 个任务目录内容哈希。
- 原因：此前分析末尾只比较 raw results、manifest、freeze 和容器快照；其它已经通过前置校验的冻结来源若在 Bootstrap/置换计算期间变化，仍可能被旧哈希带入输出。

### 2. 加入预注册 TOCTOU 负向测试

- 测试：在 `analyze_split()` 执行期间修改 preregistration 文件，分析必须报 `preregistration changed during analysis`，且不写出结果文件。
- 输出元数据：`analysis_input_integrity` 新增 `frozen_provenance_rechecked` 和 `task_content_hashes_rechecked`，明确记录后置复核已执行。

### 3. 同步协议和论文说明

- 位置：`paper/SEALED_PROTOCOL.md`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_EN.md`、`paper/EXPERIMENTS_ZH.md`。
- 修改：明确所有冻结来源及 21 个任务目录都必须在统计后再次核验，任何中途变化都阻止结果写出。
- 验证：Sealed21 dry-run 通过（21 tasks、84 cells、52.5 h，仍未就绪）；论文审计通过 1523 项检查、稳定输入 241 个；全量测试 170 项通过，`compileall` 与 `git diff --check` 通过。本轮未调用模型、未拉取镜像。

## 2026-08-12：第 78 轮 - Sealed 预注册语义与完整 provenance 输出

### 1. 将预注册内容语义纳入正式分析入口

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`。
- 修改：新增 `validate_preregistration()`，除 SHA256 外还验证预注册格式、冻结状态、模型/candidate、manifest/config 路径与哈希、21 tasks、42 attempts、outcome-blind、Bootstrap seed，以及 2 次重试且不替换/不插补的 missingness policy。
- 原因：仅验证预注册文件哈希只能证明“读到的是冻结文件”，不能证明冻结文件本身仍满足 EI 确认实验设计。

### 2. 补全 `SEALED_REPORT.md` 的输入哈希集合

- 修改：报告现在重复记录 preregistration、Sealed TOML、strict acceptance、baseline/candidate harness surface、execution/analysis source bundle 和 dependency bundle 的 SHA256，不再只显示结果、manifest、freeze 和容器快照哈希。
- 影响：审稿人或复现实验者仅查看报告即可核对完整的运行身份和分析来源，而无需解析嵌套 JSON 才能发现关键 provenance。

### 3. 协议与论文同步

- 位置：`paper/SEALED_PROTOCOL.md`、`configs/experiments/ei_confirmation_v1.json`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_EN.md`、`paper/EXPERIMENTS_ZH.md`。
- 修改：将“预注册语义必须在分析入口再次验证”和“报告重复完整输入哈希”写入确认性分析约束。

## 2026-08-12：第 77 轮 - Sealed repeat 汇总一致性门禁

### 1. 拒绝外层 repeat 汇总与逐 case 数据不一致

- 位置：`paper/analyze_sealed.py`、`eval/scripts/result_validity.py`。
- 修改：Sealed 分析在展开 84 个 cell 前调用共享的 `repeat_aggregate_consistency_error()`，逐 repeat 重算 `passed/total`；任何汇总字段与 `case_results` 不一致都会终止分析。
- 原因：此前 Sealed 只依赖扁平化 case 结果，错误或篡改的外层聚合元数据仍可能进入分析产物，削弱结果文件的完整性证据。

### 2. 同步预注册、协议和论文说明

- 位置：`configs/experiments/ei_confirmation_v1.json`、`paper/SEALED_PROTOCOL.md`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_EN.md`、`paper/EXPERIMENTS_ZH.md`。
- 修改：将 repeat `passed/total` 一致性列为 Sealed schema 和正式分析前置条件，并明确不一致时不生成确认性统计。

### 3. 增加负向测试

- 位置：`tests/test_sealed_analysis.py`。
- 测试：篡改 repeat=1 的外层 `passed` 后，`validate_result()` 必须失败；原有非布尔 passed、缺失 reward、repeat 标签和精确 cell 集合测试继续保留。
- 验证：Sealed 专项测试与论文一致性定向测试通过；重新生成 dry-run 后，论文审计通过 1523 项检查、稳定输入 241 个；全量测试 168 项通过，`compileall`、`git diff --check` 通过。本轮未调用模型、未拉取镜像。

## 2026-08-12：第 76 轮 - 协议、预注册与机器计划三方交叉核对

### 1. 将协议文字纳入稳定审计输入

- 位置：`paper/audit_paper_consistency.py`、`paper/SEALED_PROTOCOL.md`。
- 修改：新增 `sealed_protocol_checks()`，把 `SEALED_PROTOCOL.md` 加入论文审计的稳定输入集合，并核对 21 个任务、2 次 repeat、84 个评测单元、52.5 h 上界、结果盲、最多 2 次 invalid 重试及不替换/不插补规则。
- 原因：仅校验预注册 JSON 和 execution plan 仍可能遗漏协议文字被手工改写后与机器执行参数分叉的问题。

### 2. 绑定预注册主设计字段

- 修改：预注册的 `primary_model`、`primary_candidate`、manifest/config 路径与 SHA256、`attempts_per_harness`、missingness policy 必须分别与 execution plan 和协议中的固定前置条件一致；协议仍明确记录 invalid 尚未解决和“不满足前置条件不得调用模型”。
- 影响：论文协议、机器计划和预注册文件形成同一组可失败的证据链，任何一方的设计漂移都会阻断总审计。

### 3. 增加负向回归测试并同步说明

- 测试：将协议中的 84 个评测单元篡改为 83 后，`sealed_protocol_checks()` 必须失败。
- 文档：同步 `README_REPRODUCTION.md`、`paper/EXPERIMENTS_EN.md`、`paper/EXPERIMENTS_ZH.md`，明确三方交叉核对范围。
- 验证：串行重建 dry-run 通过（21 tasks、84 cells、52.5 h，仍未就绪）；论文审计通过 1523 项检查（其中协议 18 项、execution plan 31 项）；全量测试 167 项通过，`compileall` 与 `git diff --check` 通过。本轮不调用模型、不拉取镜像。

## 2026-08-12：第 75 轮 - Sealed21 execution plan 纳入执行前审计

### 1. 锁定机器执行计划与冻结输入

- 位置：`paper/audit_paper_consistency.py`、`paper/generated/sealed21-execution-plan.json`
- 原因：Sealed21 启动器实际使用 execution plan，但此前总论文审计只验证 manifest 和容器预览，计划中的 84 个单元、52.5 h 上界、源码 bundle 或 ready 状态仍可能独立漂移。
- 修改：新增 `sealed_execution_plan_checks()`，重新核对 v4 格式、模型/candidate、21×2×2 规模、manifest/preregistration/TOML SHA256、两侧 surface SHA256、执行/分析源码文件及 bundle SHA256、`strict_decision=missing` 和 `ready_to_execute=false`；计划及相关原始文件纳入 TOCTOU 快照。

### 2. 增加执行计划漂移测试并同步论文说明

- 位置：`tests/test_experiment_document_consistency.py`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 测试：将 execution plan 的 `evaluation_cells` 从 84 改为 83 后，论文审计必须失败；论文说明明确执行计划不是可独立编辑的预算摘要。

### 3. 本轮验证

- 执行计划审计：通过（31 项 plan checks）；论文审计 v3 通过 1505 项检查，审计输入数量为 240；全量测试 `166` 项通过，Sealed21 dry-run 通过且仍未就绪。本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 74 轮 - Sealed21 容器解析预览纳入执行前审计

### 1. 校验执行前的 no-pull 容器证据

- 位置：`paper/audit_paper_consistency.py`、`eval/scripts/capture_container_resolution.py`、`paper/generated/container-audit/sealed21_container_resolution.preview.json`
- 原因：论文明确报告 Sealed21 尚未执行、预览为 0/5，但此前总审计没有验证预览是否仍对应冻结 21 个任务和 5 个 Dockerfile 基础镜像引用。
- 修改：新增 `container_resolution_preview_checks()`，调用快照结构校验，重新解析冻结任务的引用集合和 `used_by` 绑定，强制 `pull_performed=false`、case ID 完全匹配、引用数和 `resolved_count/complete` 一致；预览 JSON 纳入稳定输入快照。

### 2. 统一容器预览 manifest 的 JSON 解析

- 位置：`eval/scripts/capture_container_resolution.py`、`tests/test_container_resolution.py`
- 修改：manifest 读取改用共享严格 JSON loader，兼容 BOM、拒绝重复 key；增加正向 BOM 和重复 key 负向测试。

### 3. 本轮验证

- 预览校验：通过（21 tasks、5 references、0 resolved、`pull_performed=false`）。论文审计 v3 通过 1474 项检查，审计输入数量为 227；全量测试 `165` 项通过；本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 73 轮 - Sealed21 设计分辨率审计纳入 EI 证据链

### 1. 锁定结果盲设计审计的统计代码与派生产物

- 位置：`paper/audit_confirmation_design.py`、`paper/audit_paper_consistency.py`、`paper/generated/design-audit/`
- 原因：论文说明引用 `CONFIRMATION_DESIGN_AUDIT.md` 的“至少 6 个有利任务簇”分辨率边界，但此前总审计没有验证设计 JSON、Markdown 或其统计代码身份。
- 修改：设计 JSON 新增设计脚本、统计脚本、有效性脚本的 SHA256；总审计按固定 `tasks=21`、`bootstrap_samples=20,000` 重新构建 JSON，并逐字节比较 Markdown。生成器改用显式 UTF-8 bytes 写出 JSON/Markdown，固定 LF 换行，消除 Windows CRLF 与跨平台复现差异。

### 2. 增加设计 provenance 漂移测试并同步论文说明

- 位置：`tests/test_experiment_document_consistency.py`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 测试：将设计审计源脚本 SHA256 改为全零后，论文审计必须失败；设计输出的换行和字节身份纳入稳定快照。

### 3. 本轮验证

- 设计审计重建：通过；论文审计 v3 通过 1466 项检查，审计输入数量为 226；设计/容器/论文专项测试和全量测试最终分别为 25 项与 163 项通过。本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 72 轮 - 容器复现审计纳入 EI 论文证据链

### 1. 绑定容器审计的原始任务文件

- 位置：`paper/audit_container_reproducibility.py`、`paper/audit_paper_consistency.py`、`paper/generated/container-audit/`
- 原因：论文实现效度段落引用容器基础镜像审计，但此前总论文审计没有验证其 JSON、CSV、Markdown 或原始 Dockerfile 是否发生漂移。
- 修改：容器 JSON 新增 89 个 `task.toml` 与 89 个 Dockerfile 的规范路径和 SHA256；总审计从当前任务目录重新构建容器审计，逐项核对 provenance，并重建 `CONTAINER_REPRODUCIBILITY_AUDIT.md` 与 `container_base_images.csv` 逐字节比较。

### 2. 增加容器 provenance 漂移测试并同步论文说明

- 位置：`tests/test_experiment_document_consistency.py`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 测试：将容器审计 JSON 的任一 Dockerfile/manifest SHA256 改为全零后，论文审计必须失败；容器原始输入也纳入 TOCTOU 稳定快照。

### 3. 本轮验证

- 容器审计重建：通过；论文审计 v3 通过 1451 项检查，审计输入数量为 221。
- 容器/论文一致性专项测试：23 项通过；全量测试：162 项通过；本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 71 轮 - 补充审计生成器统一严格 JSON 语义

### 1. 消除补充生成器的 JSON 解析分叉

- 位置：`paper/audit_mechanism_evidence.py`、`paper/audit_split_coverage.py`
- 原因：主统计、Sealed 和补跑路径已拒绝重复 JSON key，但机制证据生成器仍使用普通 `json.loads`，划分覆盖生成器也直接解析 Sealed manifest；同一输入可能在生成阶段和总审计阶段得到不同语义。
- 修改：两个生成器统一调用 `paper.analyze_experiments.load_json` 使用的严格共享 loader，兼容 BOM、拒绝嵌套重复 key，并要求根节点为 object。

### 2. 增加解析语义负向测试并同步论文说明

- 位置：`tests/test_mechanism_evidence_audit.py`、`tests/test_split_coverage_audit.py`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 测试：BOM JSON 正向读取通过；嵌套重复 key 在两组生成器中均被拒绝，避免歧义输入进入审计或图表。

### 3. 本轮验证

- 机制/划分/论文一致性专项测试：27 项通过；全量测试 `161` 项通过，论文审计保持 735 项检查、129 个稳定输入。
- 本轮继续未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 70 轮 - 补充 JSON 原始输入 provenance 与确定性修复

### 1. 将补充 JSON 绑定到规范原始输入

- 位置：`paper/audit_mechanism_evidence.py`、`paper/audit_split_coverage.py`、`paper/audit_paper_consistency.py`
- 原因：第69轮只保证 JSON 与 Markdown/CSV/SVG 一致；如果同时替换 JSON 和派生文件，仍可能绕过总审计。
- 修改：机制 JSON 新增 proposal、baseline result、candidate result 的路径及 SHA256；划分覆盖 JSON 新增 Clean64 配置、Sealed manifest 和 89 个任务 `task.toml` 的路径及 SHA256。论文审计按规范路径重新读取这些输入、重建 JSON、逐项核对 provenance，并将原始文件纳入 TOCTOU 快照。

### 2. 修复划分距离的跨进程浮点漂移

- 位置：`paper/audit_split_coverage.py` 的 `distribution_distance()`
- 原因：无序 `set` 的标签遍历顺序受 Python hash 随机化影响，Jensen–Shannon divergence 的最后浮点位可能跨进程变化，导致同一输入的 JSON provenance 重建失败。
- 修改：对联合标签排序后再求和，并以回归验证确保生成 JSON 可稳定重建。

### 3. 增加 provenance 漂移测试并同步论文说明

- 位置：`tests/test_experiment_document_consistency.py`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 测试：将机制或划分覆盖 JSON 的任一源 SHA256 改为全零后，总审计必须失败；论文说明补充原始输入绑定和跨进程确定性规则。

### 4. 本轮验证

- 论文一致性专项测试：25 项通过；论文审计 v3 通过 735 项检查，审计输入数量为 129（新增补充 JSON 的原始输入 provenance）。
- 本轮继续未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 69 轮 - 机制证据与划分覆盖图表纳入论文审计

### 1. 锁定正文引用的补充图表

- 位置：`paper/audit_paper_consistency.py`、`paper/generated/mechanism/`、`paper/generated/split-coverage/`
- 原因：第68轮已锁定统计主图，但 EI 实验章节还引用机制证据链和 Clean64–Sealed21 覆盖图；这些 Markdown/CSV/SVG 若被单独替换，主统计审计仍可能通过。
- 修改：新增 `supplementary_artifact_checks()`，从 `mechanism_evidence.json` 和 `split_coverage.json` 重新生成两组 Markdown、CSV、SVG，并对 6 个派生文件逐字节比较；机制/覆盖 JSON 格式和 8 个补充文件同时加入审计 TOCTOU 输入集合。

### 2. 增加补充图表漂移负向测试并同步论文说明

- 位置：`tests/test_experiment_document_consistency.py`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 测试：分别替换机制证据 SVG 和划分覆盖 SVG 的标题，审计必须失败；双语实验章节明确这些图表不是手工维护的独立证据。

### 3. 本轮验证

- 论文一致性专项测试：18 项通过；论文审计 v3 通过 355 项检查，审计输入数量为 35（新增 8 个补充产物）。
- 本轮继续未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 68 轮 - EI 统计图形绑定 JSON 汇总

### 1. 锁定论文统计 SVG 图形

- 位置：`paper/audit_paper_consistency.py`、`paper/generated/effect_sizes.svg`、`paper/generated/invalid_runs.svg`
- 原因：第67轮已锁定统计 CSV/Markdown，但 SVG 仍可能被单独替换而不影响 `statistics.json`、表格和数值审计。
- 修改：新增 `statistics_figure_checks()`，使用 `write_effect_svg()` 和 `write_invalid_svg()` 从当前 `statistics.json` 的 summary 临时重建两个图形，并逐字节比较；两个 SVG 同时纳入审计开始/结束的 TOCTOU 输入快照。

### 2. 增加图形漂移负向测试并同步论文说明

- 位置：`tests/test_experiment_document_consistency.py`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 测试：将效应量 SVG 的标题替换为过期文本后，图形审计必须失败；双语实验章节和复现说明明确两个 SVG 均由 `statistics.json` 派生，不能手工脱离统计表修改。

### 3. 本轮验证

- 论文一致性专项测试：16 项通过；论文审计 v3 通过 343 项检查，审计输入数量为 27（新增两个统计 SVG）。
- 本轮继续未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 67 轮 - 统计 CSV/Markdown 派生产物绑定 JSON

### 1. 锁定论文制表使用的统计导出物

- 位置：`paper/audit_paper_consistency.py`、`paper/generated/summary.csv`、`paper/generated/candidates.csv`、`paper/generated/STATISTICAL_AUDIT.md`
- 原因：前几轮已锁定双语表格和配对计划派生文件，但论文制表工具直接使用的统计 CSV/Markdown 仍可能被独立编辑。
- 修改：新增 `statistics_derived_artifact_checks()`，从 `statistics.json` 内存重建 summary/candidates CSV 行和 `STATISTICAL_AUDIT.md`，逐行或逐字节比较；三个文件也加入审计 TOCTOU 输入集合。

### 2. 增加统计派生产物漂移负向测试并同步说明

- 位置：`tests/test_experiment_document_consistency.py`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 测试：将统计 Markdown 标题改为“过期”并提供空 CSV，审计必须失败；论文说明明确 JSON 是统计导出唯一来源。

### 3. 本轮验证

- 论文一致性专项测试：15 项通过；论文审计 v3 通过 339 项检查，审计输入数量为 25（新增 3 个统计导出物）。
- 本轮继续未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 66 轮 - 配对计划 CSV/Markdown 派生产物一致性

### 1. 锁定 paired plan 的发布与执行辅助文件

- 位置：`paper/audit_paper_consistency.py`、`paper/generated/paired-rerun/PAIRED_RERUN_PLAN.md`、`paper/generated/paired-rerun/paired_rerun_cells.csv`
- 原因：第65轮已锁定 phase manifest，但 JSON plan 的 Markdown 摘要和 CSV cell 清单仍可能被单独编辑，导致论文预算或执行输入与 JSON 不同。
- 修改：新增 `paired_derived_artifact_checks()`，用 `render_markdown()` 重建并逐字比较 Markdown，同时解析 CSV 并逐行比较 phase/side/split/repeat/case/reason/reason_category；这两个文件也加入审计 TOCTOU 输入集合。

### 2. 增加派生产物漂移负向测试并同步说明

- 位置：`tests/test_experiment_document_consistency.py`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 测试：将 Markdown 中待补跑 183 改为 182，并提供空 CSV，审计必须失败；双语章节明确 JSON、Markdown 和 CSV 共享同一来源。

### 3. 本轮验证

- 论文一致性专项测试：14 项通过；论文审计 v3 通过 333 项检查，审计输入数量为 22（新增 Markdown/CSV）。
- 本轮继续未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 65 轮 - phase manifest 与配对计划逐 cell 绑定

### 1. 防止执行清单脱离重建计划

- 位置：`paper/audit_paper_consistency.py`、`paper/generated/PAPER_CONSISTENCY_AUDIT.json`
- 原因：第64轮已从源结果重建 paired plan，但 phase manifest 仍只检查格式、稳定标志和源哈希；单独修改 manifest 的 cells 可能让执行器运行错误单元。
- 修改：新增 `phase_manifest_semantic_checks()`，对全部 6 个 manifest 逐项比较 phase/side、`split/repeat/case_id` cells、baseline/candidate 路径和 SHA256 与 paired plan 的对应值。

### 2. 增加 manifest 漂移负向测试并同步执行说明

- 位置：`tests/test_experiment_document_consistency.py`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 测试：将一个 manifest 的 `cells` 清空后审计必须失败；双语说明明确六个执行清单均须逐 cell 对齐重建计划。

### 3. 本轮验证

- 论文一致性专项测试：13 项通过；论文审计 v3 通过 329 项检查，其中 6 项为 manifest cell 级检查。
- 本轮继续未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 64 轮 - 配对补跑计划纳入语义重建门禁

### 1. 从源结果重新计算 paired plan

- 位置：`paper/audit_paper_consistency.py`、`paper/generated/PAPER_CONSISTENCY_AUDIT.json`
- 原因：第63轮已验证 paired plan 的源文件哈希，但手工修改 pair counts、reason counts、phase cell 清单或预算仍可能保留相同源哈希。
- 修改：新增 `paired_plan_semantic_checks()`，调用 `build_paired_rerun_plan.build()` 从绑定的 baseline/candidate `result.json` 重新生成计划，并逐项比较 pair counts、incomplete/rerun cells、原因分布、limits、total budget 和全部 phases。

### 2. 增加计划语义漂移负向测试并同步复现说明

- 位置：`tests/test_experiment_document_consistency.py`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 测试：将 paired plan 的 `both_valid` 人工增加 1 后审计必须失败；双语章节明确 183 cells 和 148.69 h 预算来自源结果重建，而非手工复制。

### 3. 本轮验证

- 论文一致性专项测试：12 项通过；论文审计 v3 通过 287 项检查。
- 本轮继续未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 63 轮 - 候选表绑定 acceptance 与原始结果 provenance

### 1. 建立候选决策到原始证据的完整链路

- 位置：`paper/audit_paper_consistency.py`、`paper/generated/PAPER_CONSISTENCY_AUDIT.json`
- 原因：前几轮已验证统计源文件哈希和论文表格，但候选表的 `decision`、split delta 与 invalid 数仍可能只存在于展开后的 JSON；若 acceptance 路径漂移，表格仍可能看起来一致。
- 修改：新增 `candidate_provenance_checks()`；逐候选解析 `acceptance_path`，要求 acceptance 及其 baseline/candidate `result.json` 存在并被 `statistics.json.source_files` 覆盖，重新读取 acceptance 比较 decision/delta，并从两侧原始结果重算 invalid 与 strict-gate 状态。

### 2. 增加候选 provenance 负向测试并同步论文说明

- 位置：`tests/test_experiment_document_consistency.py`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 测试：将一条候选的 `acceptance_path` 改为不存在文件后，审计必须失败；中英文章节明确候选结论必须可回溯到 acceptance 和两侧原始结果。

### 3. 本轮验证

- 论文一致性专项测试：11 项通过；论文审计 v3 通过 279 项检查，其中候选 provenance 检查 72 项。
- 本轮继续未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 62 轮 - 论文一致性审计增加自身 TOCTOU 门禁

### 1. 锁定审计器读取期间的全部输入

- 位置：`paper/audit_paper_consistency.py`、`paper/generated/PAPER_CONSISTENCY_AUDIT.json`
- 原因：第61轮已重新计算源结果哈希，但统计 JSON、双语 Markdown、paired plan 或 manifest 若在审计过程中被替换，仍可能产生混合版本的审计报告。
- 修改：新增 `audit_input_paths()` 和前后 `stable_hash_snapshot()`；审计覆盖两份实验章节、`statistics.json`、paired plan、6 个 phase manifest 和 10 个单侧计划，任何输入变化都拒绝写出结果。新增清单数量门禁，并将输出 schema 升级为 `self_harness.paper_consistency_audit.v3`，记录 `audit_input_integrity`。

### 2. 增加 TOCTOU/清单缺失负向测试并同步说明

- 位置：`tests/test_experiment_document_consistency.py`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 测试：新增缺失 phase/single 清单数量失败夹具；双语说明明确审计自身也必须保持输入稳定。

### 3. 本轮验证

- 论文一致性专项测试：10 项通过；论文审计输出 v3，206 项检查全部通过，`tracked_input_count=20`。
- 本轮继续未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 61 轮 - provenance 哈希从格式校验升级为实际重算

### 1. 对论文输入 provenance 执行字节级重算

- 位置：`paper/audit_paper_consistency.py`、`paper/generated/PAPER_CONSISTENCY_AUDIT.json`
- 原因：此前审计只检查 `source_hashes_stable`、`source_result_stable` 和 SHA256 长度；如果统计生成后源 `result.json` 被替换，旧哈希字段仍可能看起来“格式正确”。
- 修改：`provenance_checks()` 现在解析并重新哈希 `statistics.json.source_files` 的全部 20 个文件，以及 paired plan、6 个 phase manifest、10 个单侧 rerun plan 绑定的源结果；路径缺失、哈希不匹配或数量不一致都会失败。

### 2. 增加哈希漂移负向测试并同步论文说明

- 位置：`tests/test_experiment_document_consistency.py`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 测试：将统计产物中的一个 source SHA256 改为全零后，审计必须拒绝；论文复现说明明确 provenance 是实际字节匹配而非格式占位。

### 3. 本轮验证

- 论文一致性专项测试：9 项通过；论文审计通过 204 项检查，其中 44 项为实际 SHA256 匹配检查。
- 本轮继续未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 60 轮 - 统计汇总锁定 task/attempt 单位边界

### 1. 防止 task-level 推断混入错误分母

- 位置：`paper/audit_paper_consistency.py`、`paper/generated/PAPER_CONSISTENCY_AUDIT.json`
- 原因：统计函数虽然会校验 Clean64 布局，但论文审计此前没有验证输出汇总行的 `tasks`、`attempts`、invalid/pass 上限及比例公式；部分产物替换可能让 task-level 推断和 attempt-level 分母脱节。
- 修改：新增 `statistical_unit_checks()`，逐行要求 Train=43 tasks/86 attempts、Heldout=21 tasks/42 attempts、attempts=tasks×2，invalid/pass/common-valid 计数有界，并重算 baseline/final rate、delta 及共同有效敏感性比例。

### 2. 增加单位漂移负向测试并同步统计方法说明

- 位置：`tests/test_experiment_document_consistency.py`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 测试：新增将汇总行 attempts 从 86 改为 43 后审计失败的夹具；双语章节明确 task 与 attempt 的单位关系。

### 3. 本轮验证

- 论文一致性专项测试：8 项通过；论文审计通过 138 项检查。
- 本轮继续未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 59 轮 - 共同有效敏感性分析绑定统计产物

### 1. 校验敏感性子集的比例、分母和任务覆盖

- 位置：`paper/audit_paper_consistency.py`、`paper/generated/PAPER_CONSISTENCY_AUDIT.json`
- 原因：第58轮已锁定总体 invalid 和配对缺失数字，但共同有效敏感性分析仍可能出现比例或分母漂移；这类选择性子集不能被误写成完整确认性结果。
- 修改：`narrative_claim_checks()` 新增 Qwen Heldout 共同有效 n、baseline/final 比例及 pp 差值检查，并核对 DeepSeek Train/Heldout 共同有效单元数；所有值均从 `statistics.json` 动态格式化后匹配中英文正文。

### 2. 同步敏感性分析的论文限定语

- 位置：`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 修改：明确 72.22%→88.89% 只来自选择性共同有效子集，不能替代零 invalid 的完整配对分析。

### 3. 本轮验证

- 论文一致性专项测试：7 项通过；论文审计通过 85 项检查。
- 本轮继续未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 58 轮 - 论文正文关键数字自动回溯统计来源

### 1. 将叙述性实验结论纳入一致性审计

- 位置：`paper/audit_paper_consistency.py`、`paper/generated/PAPER_CONSISTENCY_AUDIT.json`
- 原因：前几轮已校验表格行、输入 provenance 和推断字段，但正文仍可能手工保留过期的 Qwen/DeepSeek invalid 总数、共同有效样本数或配对缺失结构。
- 修改：新增 `narrative_claim_checks()`，从 `statistics.json` 与配对计划动态计算 Qwen baseline/candidate 总 invalid、DeepSeek baseline 和候选 invalid 列表、Qwen Heldout 共同有效数，以及双方/单侧缺失 pair counts，并逐项要求中英文正文包含当前值。

### 2. 增加叙述漂移负向测试并同步说明

- 位置：`tests/test_experiment_document_consistency.py`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 测试：新增将英文正文中的 `105/128` 改为 `104/128` 后审计失败的夹具，确保段落级数字不能脱离机器统计独立维护。

### 3. 本轮验证

- 论文一致性专项测试：7 项通过；审计检查已覆盖表格、provenance、推断门禁和关键叙述数字。
- 本轮继续未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 57 轮 - 论文一致性审计纳入推断门禁

### 1. 防止 invalid 结果产生伪确认性结论

- 位置：`paper/audit_paper_consistency.py`、`paper/generated/PAPER_CONSISTENCY_AUDIT.json`
- 原因：第56轮已锁定输入 provenance，但论文审计仍未验证统计门禁本身；如果有人手工填入 CI 或 p 值，单纯的表格数值一致性检查可能无法识别“未完成补跑却报告显著性”的错误。
- 修改：新增 `confirmatory_inference_checks()`；它要求 `validity_policy.confirmatory_fields_null_when_inference_blocked=true`，所有当前被阻断行的 McNemar p、任务级 permutation p、Bootstrap CI 均为 `null`，每行有非空阻断原因，并检查中英文正文明确写出不报告确认性置信区间/显著性。若未来推断门禁通过，则要求四个推断字段均为有限数值且不再保留阻断原因。

### 2. 增加负向测试并同步 EI 实验章节

- 位置：`tests/test_experiment_document_consistency.py`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 测试：新增“invalid 门禁阻断但 p 值非 null”负向夹具；双语论文和复现手册明确推断字段的 null 规则。

### 3. 本轮验证

- 论文一致性专项测试：6 项通过；审计通过 70 项检查。
- 本轮继续未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 56 轮 - 论文一致性审计纳入 provenance 门禁

### 1. 将输入稳定性证据纳入论文发布门禁

- 位置：`paper/audit_paper_consistency.py`、`paper/generated/PAPER_CONSISTENCY_AUDIT.json`
- 原因：前一版一致性审计主要比较表格数字、invalid 计数和补跑预算；即使这些数字未变，统计输入、配对 phase manifest 或单侧补跑计划仍可能来自旧文件或缺少完整哈希。
- 修改：审计输出升级为 `self_harness.paper_consistency_audit.v2`，新增 `provenance_checks`：要求 `statistics.json.analysis_input_integrity.stable_before_after_analysis=true`，配对计划和全部 phase manifest 使用 v3 且 `source_hashes_stable=true`，并逐项验证 baseline/candidate SHA256；同时检查所有单侧计划的 `source_result_stable=true` 和 64 位 `source_result_sha256`。

### 2. 增加负向回归测试并同步论文说明

- 位置：`tests/test_experiment_document_consistency.py`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 测试：新增缺失或不稳定 provenance 时审计失败的夹具，双语实验章节和复现手册明确 provenance 是表格发布的必要条件。

### 3. 本轮验证

- 全量 pytest、统计重建、配对补跑计划重建、论文一致性审计、Python 编译检查和 `git diff --check` 均通过；当前共 144 项测试。
- 本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 55 轮 - 单侧 invalid rerun 输入稳定性校验

### 1. 补齐单侧补跑入口的 TOCTOU 门禁

- 位置：`eval/scripts/rerun_invalid_cases.py`
- 原因：第53–54轮只覆盖 paired plan 和 phase manifest；单侧 planner 仍在 invalid 分类结束后才计算 `source_result_sha256`，且 execute 分支在归档 checkpoint 前没有重新核对结果。
- 修改：新增 `read_json_with_stable_hash()` 与 `require_stable_source_hash()`；单侧计划执行“哈希→严格读取→哈希”并在分类完成后再次核对，写入 `source_result_stable=true`；`--execute` 在归档或启动 Harbor 前再次要求该字段为 true 且当前结果哈希匹配。

### 2. 回归测试与复现说明

- 位置：`tests/test_invalid_rerun_planner.py`、`README_REPRODUCTION.md`
- 测试：新增结果在读取期间变化的负向测试，并验证正常 dry-run 计划的稳定 provenance；复现手册明确 execute 前的最后一次哈希检查。

### 3. 本轮验证

- 预期：单侧/配对 rerun 专项、全量 pytest、计划/统计重建、论文一致性审计和 Sealed21 dry-run 均通过；不调用模型、不执行付费补跑。

## 2026-08-12：第 54 轮 - 阶段 manifest 强制稳定性 provenance

### 1. 防止旧 phase manifest 绕过新稳定快照门禁

- 位置：`paper/build_paired_rerun_plan.py`、`eval/scripts/rerun_invalid_cases.py`
- 原因：第53轮的 paired plan 已写入 `source_hashes_stable=true`，但阶段 manifest 尚未携带该字段，executor 只校验两侧 SHA256；旧 manifest 可能因此继续进入 dry-run/execute。
- 修改：cell manifest schema 从 `self_harness.rerun_cell_manifest.v2` 升级为 `v3`，每个阶段 manifest 写入 `source_hashes_stable=true`；`load_cell_filter()` 在任何执行路径中要求该字段为真实 JSON Boolean `true`，缺失或非 true 直接拒绝。

### 2. 回归测试与复现说明

- 位置：`tests/test_paired_rerun_plan.py`、`tests/test_invalid_rerun_planner.py`、`README_REPRODUCTION.md`
- 测试：新增 manifest 缺少稳定 provenance 的负向测试，并验证新 manifest 正常通过；复现手册明确旧/不完整 manifest 不得进入付费执行。

### 3. 本轮验证

- 预期：paired planner/executor 专项、全量 pytest、计划/统计重建、论文一致性审计和 Sealed21 dry-run 均通过；不调用模型、不执行付费补跑。

## 2026-08-12：第 53 轮 - 配对补跑计划输入稳定性校验

### 1. 防止 paired rerun plan 与源结果分叉

- 位置：`paper/build_paired_rerun_plan.py`
- 原因：计划生成器此前先 flatten 两侧结果、计算缺失配对，最后才写 SHA256；补跑或合并进程若在此期间更新结果，阶段 manifest 可能把旧配对结构与新文件哈希绑定在一起。
- 修改：`build()` 在 flatten 前后对 baseline/candidate 做稳定哈希快照，路径/哈希变化即拒绝生成计划；计划新增 `source_hashes_stable=true`，其两侧 SHA256使用读取前快照值。

### 2. 回归测试与复现说明

- 位置：`tests/test_paired_rerun_plan.py`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 修改：新增模拟结果变化的负向测试；中英文补跑说明明确计划生成阶段的前后哈希门禁。

### 3. 本轮验证

- 预期：配对计划专项、全量 pytest、统计重建、论文一致性审计和 Sealed21 dry-run 均通过；不调用模型、不执行付费补跑。

## 2026-08-12：第 52 轮 - Clean64 统计输入稳定性校验

### 1. 将 TOCTOU 防护扩展到论文统计入口

- 位置：`paper/analyze_experiments.py`
- 原因：acceptance gate 已能保证比较期间两侧结果稳定，但统计重建此前只在写出 provenance 时计算哈希；补跑或队列合并若在统计期间改写输入，可能出现表格内容与 `source_files` 哈希不一致。
- 修改：新增 `analysis_input_paths()`、`stable_hash_snapshot()` 和 `require_stable_analysis_inputs()`；统计开始前和所有 summary/candidate 计算后重新收集并哈希 baseline/final、queue、acceptance、strict artifact 及其引用结果和分析代码，路径集合或哈希任一变化即拒绝写出统计产物。`statistics.json` 新增 `analysis_input_integrity`。

### 2. 回归测试与论文说明

- 位置：`tests/test_experiment_analysis.py`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 修改：新增 changed/added input 的稳定性负向测试；中英文复现段落明确 `stable_before_after_analysis=true` 与输入变更拒绝规则。

### 3. 本轮验证

- 预期：统计专项、全量 pytest、统计与配对计划重建、论文一致性审计、PowerShell parser 和 Sealed21 dry-run 均通过；不调用模型、不执行付费补跑。

## 2026-08-12：第 51 轮 - acceptance gate 结果文件稳定性校验

### 1. 修复 acceptance 统计与源文件哈希的 TOCTOU 风险

- 位置：`acceptance/scripts/run_acceptance_gate.py`
- 原因：此前 gate 先读取 baseline/candidate JSON，完成比较后才计算 SHA256；补跑进程若在读取期间更新 `result.json`，artifact 可能记录与统计内容不同的文件版本。
- 修改：CLI 现在对每侧结果执行“哈希→严格读取→哈希”校验，并在比较完成后再次核对哈希；任一侧变化即拒绝生成 acceptance。artifact schema 升级为 `self_harness.acceptance_gate.v1`，并写入 `source_hashes_stable=true`。

### 2. 将稳定性标志纳入 Sealed 解锁门禁

- 位置：`workflow/scripts/run_qwen_sealed21.ps1`
- 修改：Sealed launcher 要求 `source_hashes_stable` 为真实 JSON Boolean `true`，与 `accepted=true`、`decision=accepted`、两侧路径和 SHA256 共同满足后才设置 `strictReady`。

### 3. 回归测试与验证

- 位置：`tests/test_acceptance_invalid_trials.py`、`tests/test_sealed_launcher.py`
- 测试：新增稳定读取遇到哈希变化时的负向测试，并锁定 v1 schema 与 Sealed readiness 字段；全量测试、统计重建、论文一致性审计和 Sealed dry-run 继续作为本轮验收标准。

## 2026-08-12：第 50 轮 - Clean64 strict finalizer 解析链闭环

### 1. 收口入口复用 Sealed 的严格 JSON loader

- 位置：`workflow/scripts/finalize_clean64_strict.ps1`
- 原因：第49轮已统一 Sealed launcher，但 Clean64 strict finalizer 仍直接用 `ConvertFrom-Json` 读取 `candidate_queue.json` 和最终 `acceptance.strict.json`；重复 key 可能在 acceptance 生成前改变候选选择，或在输出展示时读取出不同 decision。
- 修改：新增 `Read-StrictJsonObject`，queue 与生成后的 acceptance artifact 均先经过 `result_validity.py --emit-object` 的 BOM、重复 key 和根 object 校验；PowerShell 只消费已验证对象。

### 2. 回归测试与复现说明

- 位置：`tests/test_finalize_launcher.py`、`README_REPRODUCTION.md`
- 修改：静态测试禁止 finalizer 的 queue 旧解析路径，并要求 `--emit-object`；README 明确 Clean64 finalizer 与 Sealed launcher 共享 JSON 语义。

### 3. 本轮验证

- 预期：finalizer/JSON 专项测试、PowerShell parser、全量 pytest、统计与论文一致性重建、Sealed21 dry-run 均通过；不调用模型、不执行付费补跑。

## 2026-08-12：第 49 轮 - Sealed 关键 JSON 输入统一严格解析

### 1. 消除 PowerShell 与 Python JSON 语义分叉

- 位置：`eval/scripts/result_validity.py`、`workflow/scripts/run_qwen_sealed21.ps1`
- 原因：Sealed launcher 原先用 PowerShell `ConvertFrom-Json` 直接读取 queue、candidate manifest、strict acceptance、dependency lock 和已有 freeze；该路径不会拒绝重复 object key，可能与统计/验收 Python loader 得到不同字段。
- 修改：`result_validity.py` 增加 `--emit-object`，先用共享 `load_json_object()` 完成 BOM 兼容、重复 key 拒绝和根 object 校验，再把规范化对象交回 PowerShell；Sealed launcher 的五类关键输入全部改走 `Read-StrictJsonObject`。

### 2. 测试与复现说明

- 位置：`tests/test_result_validity.py`、`tests/test_sealed_launcher.py`、`README_REPRODUCTION.md`
- 修改：新增 BOM JSON 的 CLI 正向测试，并静态禁止 queue 继续走旧 `ConvertFrom-Json` 路径；README 明确 launcher 的严格 JSON 输入边界。

### 3. 本轮验证

- 预期：JSON/Sealed 专项测试、PowerShell parser、全量 pytest、统计与论文一致性重建、Sealed21 dry-run 均通过；不调用模型、不执行付费补跑。

## 2026-08-12：第 48 轮 - Sealed 解锁决策绑定 Clean64 源结果

### 1. 发现旧 acceptance artifact 可被误复用

- 位置：`workflow/scripts/run_qwen_sealed21.ps1`、`acceptance/scripts/run_acceptance_gate.py`
- 原因：Sealed 启动器此前只读取 `acceptance.strict.json` 的 `decision` 字段；若候选 `result.json` 更新、旧 artifact 被保留或复制，`accepted` 字符串本身不能证明它对应当前两侧结果。
- 修改：acceptance artifact 新增 baseline/candidate 源结果绝对路径及 SHA256；Sealed dry-run/execute 入口重新解析路径并计算当前文件哈希，同时要求 `accepted=true` 与 `decision=accepted` 一致，只有两侧路径/哈希和决策布尔值都匹配时才设置 `strictReady`。

### 2. 增加身份绑定回归测试与可审计 readiness 字段

- 位置：`tests/test_acceptance_invalid_trials.py`、`tests/test_sealed_launcher.py`
- 修改：新增 artifact 源结果哈希正向测试，以及启动器静态测试；执行计划的 `readiness.strict_acceptance_result_binding` 单独报告源结果绑定状态，区分“决策不 accepted”和“artifact 与当前结果不匹配”。

### 3. 本轮验证

- 预期：acceptance/Sealed 专项测试、PowerShell parser、全量 pytest、统计与论文一致性重建、Sealed21 dry-run 均通过；不调用模型、不执行付费补跑。

## 2026-08-12：第 47 轮 - Clean64 严格收口入口口径统一

### 1. 修复 PowerShell 收口脚本的 strict reward 漏洞

- 位置：`workflow/scripts/finalize_clean64_strict.ps1`
- 原因：前置 `result_validity.py --count` 仍使用兼容模式；虽然后续 acceptance gate 会严格检查，但入口审计与最终门禁的 reward 口径不一致。
- 修改：前置 invalid 盘点显式追加 `--require-reward`，使缺失 verifier reward 的单元在收口开始时即被阻断。

### 2. 固化三段式收口链

- 位置：`workflow/scripts/finalize_clean64_strict.ps1`、`acceptance/scripts/run_acceptance_gate.py`、`paper/analyze_experiments.py`
- 说明：脚本继续将 acceptance 输出写入独立的 `acceptance.strict.json`；acceptance gate 负责逐 case strict reward 与 repeat aggregate 一致性，统计重建负责 Clean64 固定 Train/Heldout 布局校验，历史 acceptance 不被覆盖。
- 测试：新增 `tests/test_finalize_launcher.py`，静态锁定 strict 参数、独立产物路径和统计重建调用。

### 3. 本轮验证

- 预期：PowerShell parser、收口脚本静态测试、全量 pytest、统计重建、配对补跑计划重建、论文一致性审计和 Sealed21 dry-run 均通过；不调用模型、不执行付费补跑。

## 2026-08-12：第 46 轮 - Harbor 执行器复用结果严格解析

### 1. 统一 Harbor executor 的 JSON 读取

- 位置：`eval/scripts/run_harbor_eval.py`
- 修改：复用 `load_json_object()` 读取 run identity marker、repeat result、case checkpoint 和可选 trial/invoke JSON；写入路径仍保持原有无 BOM JSON。
- 原因：此前统计、验收、补跑和 workflow 已拒绝 BOM/重复 key，但 Harbor 的 `--reuse-existing` 路径仍默认解析旧 result/checkpoint，可能在付费执行前信任歧义产物。

### 2. 回归测试与复现文档

- 位置：`tests/test_harbor_infrastructure_retry.py`、`README_REPRODUCTION.md`
- 验证：新增 Harbor loader 的 BOM/重复 key 测试；已有 run identity、checkpoint reuse 和 infrastructure retry 测试继续通过；README 明确记录 marker/case-result 复用也受同一规则保护。

## 2026-08-12：第 45 轮 - Clean64 固定设计布局校验

### 1. 强制 Clean64 43/21×2 设计

- 位置：`paper/analyze_experiments.py`
- 修改：新增 `validate_clean64_layout()`；统计主入口和候选表入口要求 Train=43 tasks、Heldout=21 tasks、repeat IDs 恰为 1/2，并检查两个 repeat 的 task 集合一致。
- 原因：此前 baseline/candidate 只要 keys 彼此相同即可进入分析；如果两侧同步缺失相同 task，统计仍可能生成看似完整但分母错误的论文表格。

### 2. 回归测试与文档

- 位置：`tests/test_experiment_analysis.py`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 验证：新增固定布局正向/缺失 task 负向测试；现有 candidate fixture 保留可测试的小型单元模式，生产主入口启用强制布局；双语实验章节记录设计约束。

## 2026-08-12：第 44 轮 - proposer/resume 工作流 JSON 语义统一

### 1. 统一工作流状态与候选入口读取器

- 位置：`workflow/scripts/run_self_harness_loop.py`
- 修改：`read_json()` 改为复用 `eval/scripts/result_validity.py::load_json_object()`，对 branch state、candidate queue、proposal bundle、candidate manifest 和 acceptance artifact 统一执行 BOM、重复 key 和根 object 校验；JSON 写入仍保持无 BOM，避免改变既有产物格式。
- 原因：前几轮已统一结果、验收和补跑 JSON，但 proposer/resume 工作流仍默认使用 `json.loads`；重复 key 可能改变候选选择、恢复状态或合并分支。

### 2. 回归测试与文档

- 位置：`tests/test_workflow_resume.py`、`README_REPRODUCTION.md`
- 验证：新增工作流 JSON loader 的 BOM/重复 key 测试；工作流恢复和机制测试继续通过；README 明确记录 queue/state/proposal/checkpoint 的统一解析规则。

## 2026-08-12：第 43 轮 - repeat 汇总与逐单元结果一致性

### 1. 新增 repeat aggregate 一致性分类器

- 位置：`eval/scripts/result_validity.py`
- 修改：新增 `repeat_aggregate_consistency_error()`，要求 `case_results` 存在、`total == len(case_results)`、所有 case 的 `passed` 为 JSON boolean，且 aggregate `passed` 等于逐 case true 数量。
- 原因：仅校验 aggregate 的范围和 baseline/candidate 分母相同，无法发现被篡改或旧脚本错误写出的 `passed/total`；这会直接影响 acceptance 的平均通过率。

### 2. 接入严格验收和最终报告

- 位置：`acceptance/scripts/run_acceptance_gate.py`、`eval/scripts/build_clean64_final_report.py`
- 修改：严格 acceptance 和 Clean64 最终报告在 invalid 检查后重新核对 aggregate；不一致时拒绝继续。
- 原因：论文最终表格和 promotion decision 必须由逐单元 verifier 结果重算，不能信任可独立修改的汇总字段。

### 3. 回归测试与文档

- 位置：`tests/test_acceptance_invalid_trials.py`、`tests/test_clean64_reporting_validity.py`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 验证：新增 aggregate passed mismatch 测试；现有 invalid、provider failure 和 final report 测试继续通过。

## 2026-08-12：第 41 轮 - Sealed21 确认性统计下游断言

### 1. 防止 Sealed21 消费不完整 CI/p

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`
- 修改：新增 `require_confirmatory_stats()`；Sealed21 在渲染报告和 success 判定前强制要求 `inference_valid` 及全部 CI/p 非空。
- 原因：第40轮将不完整 Clean64 推断字段置为 `null` 后，所有下游确认性消费者都必须显式区分 `null` 与有效统计，不能依赖隐含的数值比较。

### 2. 验证

- Sealed21 validate_result 仍先要求 84 个单元零 invalid，因此正常完整运行不会被新断言阻断；不完整夹具会得到明确错误而非 `TypeError`。

## 2026-08-12：第 40 轮 - invalid 门禁下隐藏确认性推断数值

### 1. 阻断无效样本下的 CI/p 输出

- 位置：`paper/analyze_experiments.py`
- 修改：只有 baseline 和 final 的 effective-invalid 均为零时才计算 Bootstrap CI、精确 McNemar p 和任务级 permutation p；否则三个 CI/p 字段写为 JSON `null`，并新增 `inference_blocked_reason`。
- 原因：此前 Markdown 虽显示 `incomplete`，但 `statistics.json` 仍保存把 invalid 单元当失败计算出的 CI/p，下游制表或脚本可能误把它们当确认性结果。

### 2. 修正效果图的 incomplete 表达

- 位置：`paper/analyze_experiments.py` 的 `write_effect_svg()`
- 修改：CI 为 `null` 时不绘制误导性的区间线，仅保留描述性点估计并标注 `incomplete`。
- 原因：图形产物也必须遵守零 invalid 门禁，不能通过视觉元素重新引入被正文禁止的确认性区间。

### 3. 测试、统计策略与双语论文说明

- 位置：`tests/test_experiment_analysis.py`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 修改：新增 invalid 门禁下 CI/p 为 `None` 的回归测试；`statistics.json.validity_policy` 新增 `confirmatory_fields_null_when_inference_blocked`；双语实验章节记录机器可读产物的 null 规则。

## 2026-08-12：第 39 轮 - EI 论文表格数值一致性审计

### 1. 新增论文数值一致性审计器

- 位置：`paper/audit_paper_consistency.py`
- 修改：逐行解析中英文 4.5 历史结果表和 4.6 候选表，将百分比、pp 差值、invalid 转移、决策和 strict 状态与 `paper/generated/statistics.json` 对比；同时核对 Qwen 配对计划的 183 cells、549 attempts 和 148.69 h 是否仍出现在两种语言文本中。
- 原因：论文实验章节此前依赖人工复制数字；结果文件或补跑计划更新后，表格可能保持旧值而测试仍然通过。

### 2. 生成机器可读审计产物

- 位置：`paper/generated/PAPER_CONSISTENCY_AUDIT.json`
- 修改：记录审计格式、每项实际值/期望值和总体 `passed` 状态；审计失败时命令返回非零退出码。
- 原因：EI 投稿前需要可复核证据证明正文表格由当前统计产物支持，而不是只检查文件存在。

### 3. 测试与文档接入

- 位置：`tests/test_experiment_document_consistency.py`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 验证：新增文档级回归测试，并把 `python paper/audit_paper_consistency.py` 纳入重建命令。

## 2026-08-12：第 38 轮 - 严格路径强制 verifier reward

### 1. 区分历史兼容模式与严格验收模式

- 位置：`eval/scripts/result_validity.py`
- 修改：`effective_invalid_reason`、`is_effectively_invalid` 和 `effective_invalid_cases` 新增 `require_reward` 参数；默认保留旧历史夹具兼容，strict 模式将缺失 `reward` 字段直接判为 `missing verifier reward`。CLI 增加 `--require-reward`。
- 原因：不能为了修复未来严格运行而改变历史固定分母口径，但 acceptance 和付费补跑不能允许没有 verifier reward 的单元伪装成普通行为失败。

### 2. 将 strict 模式接入关键实验路径

- 位置：`acceptance/scripts/run_acceptance_gate.py`、`eval/scripts/rerun_invalid_cases.py`、`paper/build_paired_rerun_plan.py`、`eval/scripts/build_clean64_live_report.py`、`eval/scripts/build_clean64_final_report.py`、`paper/audit_mechanism_evidence.py`
- 修改：验收、补跑选择、配对缺失计划、实时/最终报告和机制证据审计均传入 `require_reward=True`。
- 原因：严格实验路径必须以 verifier 结果为准，不能依赖默认的 legacy 兼容行为。

### 3. 真实数据审计与回归测试

- 位置：`tests/test_result_validity.py`、`tests/test_acceptance_invalid_trials.py`、`tests/test_invalid_rerun_planner.py`
- 结果：当前 512 个 Clean64 baseline/final 单元全部带有 `reward`，历史 invalid 计数保持不变；新增 legacy/strict 分支、acceptance 缺失 reward、补跑 fixture 和 CLI 边界测试。

## 2026-08-12：第 37 轮 - 全部结果入口共享严格 JSON loader

### 1. 统一结果文件读取语义

- 位置：`eval/scripts/result_validity.py`、`acceptance/scripts/run_acceptance_gate.py`、`eval/scripts/rerun_invalid_cases.py`、`paper/analyze_experiments.py`、`paper/analyze_sealed.py`
- 修改：新增 `load_json_object()`，统一使用 `utf-8-sig`、嵌套重复 key 拒绝和根 object 校验；统计、Sealed、acceptance gate、invalid rerun 与 `result_validity` CLI 通过同一实现读取结果/验收 JSON。
- 原因：第36轮虽然统一了类型校验，但不同入口仍各自维护 JSON parser，入口之间可能因 BOM 或重复 key 得出不同结果；验收 gate 的输入尤其不能继续使用默认 `json.loads`。

### 2. 加固有限数值边界

- 位置：`eval/scripts/result_validity.py`
- 修改：对超大整数 reward 捕获 `OverflowError`，与 `NaN`、`Infinity` 一样判为非有限 reward，而不是让分类器崩溃。
- 原因：结果文件来自外部运行产物，异常数值必须转化为可审计的 invalid，而不是导致统计或 gate 进程非预期退出。

### 3. 入口级回归测试与论文说明

- 位置：`tests/test_result_validity.py`、`tests/test_acceptance_invalid_trials.py`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 验证：新增 BOM/重复 key 共享 loader、acceptance gate、超大 reward 测试；双语实验章节说明所有结果入口使用同一 JSON 语义。

## 2026-08-12：第 36 轮 - 统计结果类型校验与输入 provenance 固化

### 1. 收紧结果有效性分类

- 位置：`eval/scripts/result_validity.py`
- 修改：`passed` 必须是 JSON boolean；若存在 `reward`，必须是有限的数值，`NaN` 与 `Infinity` 归为 `inconsistent_result`。
- 原因：Python 的 `bool("false")` 和 `bool(1)` 都为真，可能把格式错误记录计为通过；非有限 reward 也不应进入行为结果统计。

### 2. 统一描述性统计的通过判定

- 位置：`paper/analyze_experiments.py`
- 修改：新增 `passed_for_descriptive_rate`，固定分母统计只把 JSON boolean `true` 计为通过，结构无效单元按失败计入；共同有效敏感性分析也使用同一判定。
- 原因：统计聚合必须与有效性分类器一致，不能因为结果文件类型错误而产生不同口径。

### 3. 固化统计输入 provenance 与门禁策略

- 位置：`paper/analyze_experiments.py`、`paper/generated/statistics.json`
- 修改：`analysis_version` 升至 `1.1`；新增 `validity_policy` 和 `source_files`，记录分析器、有效性分类器、baseline/final/queue/acceptance 输入文件及其 SHA256。
- 原因：EI 论文表格需要能够从明确的输入身份重建；仅保存比例、seed 和样本数不足以审计统计代码或输入是否被替换。

### 4. 回归测试与文档

- 位置：`tests/test_result_validity.py`、`tests/test_experiment_analysis.py`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`
- 验证：新增非布尔 `passed`、非有限 `reward`、描述性计数和 source manifest 测试；双语实验章节同步记录新门禁与 provenance 字段。

## 2026-08-12：第 35 轮 - 共享统计读取器 JSON 语义统一

### 1. 覆盖 Clean64 历史统计和配对计划共同读取路径

- 位置：`paper/analyze_experiments.py`、`tests/test_experiment_analysis.py`。
- 原因：第34轮只修复了 invalid rerun planner；共享 `flatten_result()` 使用的 `load_json()` 仍默认接受重复 key，因此 Clean64 历史统计和 paired plan 可能读取与 rerun planner 不同的 JSON 语义。
- 修改：共享统计 loader 改用 `utf-8-sig`，并通过 `object_pairs_hook` 拒绝嵌套重复 JSON key，同时验证根节点必须是 object。
- 测试：新增标准 BOM 文件正向读取和嵌套重复 `status` 负向测试。

### 2. 同步 EI 实验说明

- 位置：`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`。
- 修改：明确历史统计、配对计划和 invalid rerun 共用 UTF-8/BOM 兼容、重复 key 拒绝的解析规则。

### 3. 本轮验证

- 统计/Sealed/rerun 专项测试通过。
- 本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 34 轮 - Clean64 rerun JSON 重复键门控

### 1. 统一补跑 planner 的 JSON 解析策略

- 位置：`eval/scripts/rerun_invalid_cases.py`、`tests/test_invalid_rerun_planner.py`。
- 原因：Sealed analyzer 已拒绝重复 JSON key，但 Clean64 invalid rerun planner 仍使用默认 `json.loads`；重复 key 可能静默覆盖 `status`、`case_id` 或 checkpoint 字段，改变待补跑集合。
- 修改：`read_json()` 使用 `utf-8-sig` 兼容 BOM，并通过 `object_pairs_hook` 拒绝所有嵌套重复 key；错误发生在 invalid 分类和计划生成之前。
- 测试：新增带 BOM 的嵌套重复 `status` 负向夹具。

### 2. 同步复现实验材料

- 位置：`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`。
- 修改：明确 rerun planner 对 UTF-8/BOM JSON 使用重复键拒绝规则，与 Sealed 分析链保持一致。

### 3. 本轮验证

- JSON/invalid rerun 专项测试通过。
- 本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 33 轮 - Clean64 rerun 空白 API key 门控

### 1. 统一两个付费入口的凭据判定

- 位置：`workflow/scripts/rerun_clean64_invalid.ps1`、`tests/test_rerun_launcher.py`。
- 原因：Sealed21 启动器已使用 `[string]::IsNullOrWhiteSpace()`，但 Clean64 invalid rerun wrapper 仍用 `if (-not $env:OPENAI_API_KEY)`；仅含空格的 Windows User key 可能被当作可用凭据并进入付费执行。
- 修改：前台和 detached PowerShell 分支均先读取 User 环境变量到 `$apiKey`，使用 `IsNullOrWhiteSpace` 拒绝缺失或空白值，再设置 `OPENAI_API_KEY`。
- 测试：新增静态测试确认两条路径都包含非空白检查、明确错误信息和用户环境变量读取。

### 2. 同步复现材料

- 位置：`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`。
- 修改：明确 Clean64 rerun 的 foreground/detached 两条路径共享非空白 API key 门控。

### 3. 本轮验证

- Clean64 rerun/Sealed launcher 静态专项测试通过。
- 本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 32 轮 - 执行器拒绝过期 phase manifest

### 1. 让 SHA256 身份真正参与付费执行门控

- 位置：`eval/scripts/rerun_invalid_cases.py`、`paper/build_paired_rerun_plan.py`、阶段 manifest、相关测试。
- 原因：第31轮已把 baseline/candidate SHA256 写入计划，但 rerun 执行器原先只读取 manifest 中的 cell 坐标，未验证源结果身份；过期清单仍可能进入 `--execute`。
- 修改：manifest schema 升级为 `self_harness.rerun_cell_manifest.v2`，包含两侧结果路径和 SHA256；`load_cell_filter()` 在任何 dry-run/execute 计划构建前同时校验两侧文件存在且哈希匹配，任一不符即抛出 `stale cell manifest`。单侧 rerun plan 也记录当前 `source_result_sha256`。
- 测试：新增修改 candidate 源结果后拒绝 phase manifest 的负向夹具；更新正向 manifest schema 测试。

### 2. 同步复现说明和生成材料

- 位置：`paper/generated/paired-rerun/`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`。
- 修改：重新生成 v2 计划与 6 个阶段清单，并明确执行器会在付费前拒绝源结果哈希不匹配的过期清单。

### 3. 本轮验证

- 配对计划、invalid rerun 和文档专项测试：11 项通过。
- 本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 31 轮 - 配对补跑计划结果身份绑定

### 1. 防止同计数不同结果时误复用计划

- 位置：`paper/build_paired_rerun_plan.py`、`paper/generated/paired-rerun/paired_rerun_plan.json`、阶段 cell manifests、`tests/test_paired_rerun_plan.py`。
- 原因：第30轮只比较了 pair counts、原因分布和预算；若结果文件内容改变但 invalid 汇总恰好不变，旧的具体 phase cell 清单仍可能被复用。
- 修改：计划新增 baseline/candidate 原始 `result.json` SHA256；每个阶段 manifest 继承这两个哈希；一致性测试同时比较两个输入哈希和完整 `phases` cell 清单。重新生成了配对计划及 6 个阶段清单。

### 2. 同步复现说明

- 位置：`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`。
- 修改：明确结果文件即使只发生内容变化、invalid 计数不变，也必须重新生成计划和 manifest。

### 3. 本轮验证

- 配对计划/文档专项测试：4 项通过。
- 当前计划仍为 183 cells、549 次最大尝试、148.69 h 配置上界，且与实际结果哈希和具体 phase 清单一致。
- 本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 30 轮 - Clean64 补跑计划与论文计数一致性

### 1. 将配对补跑计划绑定到实际结果

- 位置：`tests/test_experiment_document_consistency.py`、`paper/generated/paired-rerun/paired_rerun_plan.json`。
- 原因：论文中的 105/78 invalid、183 个待补跑 cell、549 次最大尝试和 148.69 h 预算来自历史结果复制；结果更新后若只手工修改文档，计划、清单和论文可能漂移。
- 修改：新增一致性回归测试，使用当前 baseline/candidate `result.json` 重新调用 `build_paired_rerun_plan.build()`，逐项比较 checked-in JSON 的 pair counts、原因分布、限制、待补跑数量和预算，并从权威计划反向核对中英文实验表述。

### 2. 本轮验证

- 文档/补跑计划专项测试：3 项通过。
- 当前权威配对计数：双方有效 18、baseline-only 32、candidate-only 5、双方均无效 73；baseline invalid=105、candidate invalid=78、待补跑 cells=183。
- 本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 29 轮 - Sealed 分析输入 TOCTOU 门控

### 1. 锁定全部关键分析输入

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`。
- 原因：此前只对 baseline/candidate 原始结果做读取前后 SHA256 校验；manifest、freeze 或容器解析快照若在分析期间被替换，可能出现验证内容与最终报告输入不一致。
- 修改：分析开始时记录 Sealed manifest、freeze、container-resolution snapshot 的 SHA256；完成 provenance、结果校验、统计和类别汇总后重新比较，任一输入变化即拒绝输出。`sealed_statistics.json` 新增 `analysis_input_integrity` 和三类输入哈希，`SEALED_REPORT.md` 重复完整哈希集合。
- 测试：新增在统计函数期间修改 manifest 的回归夹具，确认分析器抛出 `sealed manifest changed during analysis`。

### 2. 同步复现实验协议与论文材料

- 位置：`configs/experiments/ei_confirmation_v1.json`、`paper/SEALED_PROTOCOL.md`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`。
- 修改：明确五类输入（两侧结果、manifest、freeze、容器快照）均须稳定，并由 JSON/Markdown 产物共同记录。

### 3. 本轮验证

- Sealed 分析专项测试：21 项通过。
- 本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 28 轮 - Sealed 前置条件文档一致性

### 1. 修正误导性的 invalid 状态表述

- 位置：`paper/SEALED_PROTOCOL.md`、`paper/EXPERIMENTS_ZH.md`。
- 原因：协议执行前置条件曾把 Qwen baseline 的 105 个和 candidate 的 78 个有效 invalid 写成“已解决”，与 dry-run 的 `strict_decision: missing`、启动器阻断状态以及中英文实验章节相矛盾。
- 修改：统一改为“必须在 Sealed 执行前全部解决（当前尚未解决）”和“待全部解决”，明确当前没有 Sealed 执行资格。

### 2. 增加文档一致性回归测试

- 位置：`tests/test_experiment_document_consistency.py`。
- 修改：检查前置条件不得出现“已解决”而必须标注当前未解决状态；同时校验配置、协议和中英文实验章节的 21-task/42-attempt/84-cell 设计计数一致。

### 3. 本轮验证

- 文档一致性专项测试：2 项通过。
- 本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 27 轮 - Bootstrap 分位数规则显式化

### 1. 固定 CI 分位数重建规则

- 位置：`paper/analyze_experiments.py`、`paper/analyze_sealed.py`、`paper/audit_confirmation_design.py`、相关测试。
- 原因：原实现使用排序样本位置 `q*(n-1)` 的线性插值，但 Sealed 输出只记录 Bootstrap 次数和 seed；不同统计软件若采用不同 quantile convention，可能重建出不同 CI。
- 修改：声明 `BOOTSTRAP_PERCENTILE_METHOD` 为 Hyndman–Fan type 7，Sealed21 统计输出和设计审计 JSON 均记录该方法；同时拒绝空样本、越界分位数和非正 Bootstrap 次数。
- 测试：增加 type-7 插值数值回归测试与非正样本数负向测试。

### 2. 同步 EI 复现材料

- 位置：`configs/experiments/ei_confirmation_v1.json`、`paper/SEALED_PROTOCOL.md`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`。
- 修改：将 `q*(n-1)` 线性分位数规则写入确认性统计协议和论文实验说明；重新生成设计审计与历史统计审计产物。

### 3. 本轮验证

- 统计专项、Sealed 分析和设计审计测试全部通过。
- 重新生成 `paper/generated/design-audit/` 与 `paper/generated/` 统计产物；未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 26 轮 - 冻结环境快照 schema 门控

### 1. 拒绝不完整的 execution environment 证据

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`。
- 原因：此前分析器只把 freeze 中的 `execution_environment` 原样写入输出，没有验证时间戳、Docker client/server、Git 状态、模型和 endpoint 等预注册要求的字段是否存在、非空且类型正确。
- 修改：新增 `validate_execution_environment_snapshot()`；要求带时区的 ISO-8601 时间戳、完整运行时字符串字段、三个布尔状态字段、freeze/model 一致性以及 HTTP(S) endpoint。缺失或 malformed snapshot 在 provenance 校验阶段直接拒绝。
- 测试：更新正向 freeze 夹具，新增缺失 Docker server version 的负向测试。

### 2. 同步实验协议与论文材料

- 位置：`configs/experiments/ei_confirmation_v1.json`、`paper/SEALED_PROTOCOL.md`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`。
- 修改：将“记录环境快照”升级为“记录并验证环境快照 schema”，明确缺失、空白、格式错误或类型错误不得进入确认性统计。

### 3. 本轮验证

- Sealed 分析聚焦测试：20 项通过。
- 本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 25 轮 - JSON duplicate-key 解析门控

### 1. 拒绝重复 JSON object key

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`。
- 原因：Python 默认允许重复 JSON key，并静默采用最后一个值；不同解析器可能因此得到不同的 `passed`、`reward` 或身份字段。
- 修改：`load_json()` 使用 `object_pairs_hook` 逐对象检查 key 唯一性，嵌套对象出现重复 key 时在统计和 invalid 分类前直接拒绝。
- 测试：新增嵌套 `passed` 重复 key 负向夹具，确认抛出 `duplicate JSON key`。

### 2. 同步 Sealed schema 与复现文档

- 位置：`configs/experiments/ei_confirmation_v1.json`、`paper/SEALED_PROTOCOL.md`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`。
- 修改：将 JSON object key 唯一性写入结果 schema、分析前提和 EI 复现实验说明。

### 3. 本轮验证

- Sealed 分析聚焦测试：19 项通过；嵌套重复 key 被拒绝。
- 本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 24 轮 - API key 空白值执行门控

### 1. 修复 readiness 的空白凭据漏洞

- 位置：`workflow/scripts/run_qwen_sealed21.ps1`、`tests/test_sealed_launcher.py`。
- 原因：PowerShell 的布尔转换会把空格字符串视为存在，导致 dry-run 显示 API key 可用，付费请求阶段才失败。
- 修改：readiness 和 Execute 分支均使用 `[string]::IsNullOrWhiteSpace()`；空字符串和仅空白字符统一拒绝，并给出明确错误。
- 测试：启动器静态断言确认两处都使用非空白检查，PowerShell parser 通过。

### 2. 同步复现与论文说明

- 位置：`configs/experiments/ei_confirmation_v1.json`、`paper/SEALED_PROTOCOL.md`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`。
- 修改：明确 Windows User 环境变量必须为非空白 API key，空白凭据不满足付费执行门控。

### 3. 本轮验证

- 启动器静态测试：1 项通过。
- PowerShell 语法解析：通过。
- 本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 23 轮 - verifier reward 有限数值门控

### 1. 拒绝 NaN/Infinity 伪数值

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`。
- 原因：Python `json.loads` 默认接受 `NaN`、`Infinity` 和 `-Infinity`，而单纯的 `int/float` 类型检查不能保证 reward 是合法有限数值。
- 修改：Sealed `validate_result()` 要求 reward 为非布尔、非空且 `math.isfinite()` 的数值。
- 测试：新增 NaN、正 Infinity、负 Infinity 三个负向夹具。

### 2. 同步结果 schema 与论文

- 位置：`configs/experiments/ei_confirmation_v1.json`、`paper/SEALED_PROTOCOL.md`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`。
- 修改：明确非有限 reward 不得被解释为行为通过或失败。

### 3. 本轮验证

- Sealed 分析测试：18 项通过。
- NaN/Infinity reward：均在统计前拒绝。
- 本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 22 轮 - 原始结果读取稳定性校验

### 1. 防止 TOCTOU 导致统计内容与哈希不一致

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`。
- 原因：若 `result.json` 在分析器读取和哈希记录之间被替换，统计内容与输出 SHA256 可能来自不同字节版本。
- 修改：分析器在读取前记录 baseline/candidate SHA256，完成 provenance、repeat、reward 和 invalid 校验后再次比较；任一文件发生变化即拒绝分析。
- 测试：新增文件变更后 `require_stable_file_hash()` 拒绝测试。

### 2. 同步复现说明

- 位置：`configs/experiments/ei_confirmation_v1.json`、`paper/SEALED_PROTOCOL.md`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`。
- 修改：把读取期间哈希稳定性列为 v6 分析产物的输入完整性要求。

### 3. 本轮验证

- Sealed 分析测试：15 项通过。
- 结果文件变化负向测试：通过。
- 本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 21 轮 - 分析产物 schema v6 与报告输入哈希

### 1. 防止新旧分析产物混用

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`。
- 修改：含设计元数据、完整性计数和原始结果哈希的分析输出版本升级为 `self_harness.sealed_analysis.v6`。
- 修改：`SEALED_REPORT.md` 直接显示 baseline/candidate 原始 `result.json` SHA256；测试同时断言 JSON schema 版本和报告哈希。

### 2. 同步 EI 复现材料

- 位置：`configs/experiments/ei_confirmation_v1.json`、`paper/SEALED_PROTOCOL.md`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`。
- 修改：明确 v6 输出和报告输入哈希是正式论文表格的可审计来源。

### 3. 本轮验证

- Sealed 正向分析测试：14 项通过。
- `sealed_analysis.v6` 版本断言：通过。
- Markdown 输入哈希断言：通过。
- 本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 20 轮 - 原始结果文件身份绑定

### 1. 将 baseline/candidate 结果哈希写入分析产物

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`。
- 原因：分析 JSON 原先记录了原始结果路径和 run identity，但没有直接绑定实际 `result.json` 字节内容；结果文件被替换后，复核者需要额外手工计算哈希。
- 修改：`sealed_statistics.json` 新增 `baseline_result_sha256` 与 `candidate_result_sha256`，由分析器在读取并验证结果后直接计算；正向夹具断言两者等于原始文件哈希。

### 2. 同步 EI 结果复现规范

- 位置：`configs/experiments/ei_confirmation_v1.json`、`paper/SEALED_PROTOCOL.md`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`。
- 修改：明确论文表格和 `SEALED_REPORT.md` 必须绑定两侧原始结果 SHA256，避免只复制比例或 p 值。

### 3. 本轮验证

- Sealed 正向分析测试：14 项通过。
- 原始 baseline/candidate 结果哈希断言：通过。
- 本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 19 轮 - Sealed 分析输出可复核元数据

### 1. 补齐机器可读输出的设计与完整性字段

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`。
- 修改：`sealed_statistics.json` 新增 `design` 和 `result_integrity`，记录任务数、每侧 repeat 数、baseline/candidate 角色、84 个评测单元、Bootstrap 20,000 次与 seed、精确置换方法、两侧 invalid 计数、repeat/schema 和 reward 要求。
- 修改：`SEALED_REPORT.md` 新增设计与完整性摘要，明确 21×2×2 单元结构、invalid 计数和 Bootstrap 参数。
- 测试：正向分析夹具断言 JSON 元数据和 Markdown 摘要均存在且数值正确。

### 2. 同步 EI 论文复现材料

- 位置：`configs/experiments/ei_confirmation_v1.json`、`paper/SEALED_PROTOCOL.md`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`。
- 修改：明确论文表格应从带设计和完整性元数据的 Sealed 产物重建，而不是只复制比例或 p 值。

### 3. 本轮验证

- 正向 Sealed 分析测试：14 项通过。
- 新增输出字段：设计、invalid、Bootstrap 和容器状态断言通过。
- 本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。

## 2026-08-12：第 18 轮 - Sealed repeat 结构门控

### 1. 固定两次 repeat 的结构证据

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`。
- 原因：扁平化键校验可能掩盖外层 repeat 标签与 case 内 repeat 标签不一致，导致 84 个统计单元的重复结构缺少直接证据。
- 修改：`validate_result()` 强制要求外层 `sealed` 只包含 repeat=1、repeat=2 两条记录，每条有 `case_results`，且每个 case 的 repeat 必须与外层记录一致。
- 测试：新增 outer/case repeat 不一致的负向夹具；正常双 repeat 分析路径保持通过。

### 2. 同步 schema 与论文

- 位置：`configs/experiments/ei_confirmation_v1.json`、`paper/SEALED_PROTOCOL.md`、`README_REPRODUCTION.md`、`paper/EXPERIMENTS_ZH.md`、`paper/EXPERIMENTS_EN.md`。
- 修改：把 repeat=1/2 及标签一致性列为确认性结果 schema 的显式前提。

### 3. 本轮验证

- Sealed 分析测试：14 项通过。
- 本轮未调用模型、未拉取镜像、未生成正式 Sealed21 结果。
# 第137轮 - acceptance 两侧配对键集合门禁

- 位置：`paper/analyze_experiments.py`、`paper/audit_paper_consistency.py`、`tests/test_experiment_analysis.py`、`tests/test_experiment_document_consistency.py`、`paper/generated/statistics.json`、`paper/generated/PAPER_CONSISTENCY_AUDIT.json`
- 发现：候选 acceptance 的历史重算此前只比较每个 split/repeat 的行数和通过数，没有显式要求 baseline 与 candidate 的 `(split, repeat, case_id)` 集合相同；两侧可以在样本数相同的情况下替换任务，仍产生看似合法的平均通过率和 delta。
- 修改：新增 `validate_paired_result_keys()`，要求两侧结果的完整配对键集合逐项一致，并输出缺失于哪一侧的具体键。候选统计生成器在消费 acceptance 时执行该门禁；论文审计在历史 acceptance 重算前、候选 provenance 校验中再次执行同一门禁，避免只依赖统计 JSON 内部一致性。
- 测试：新增同样大小但不同 `case_id` 的生成器负向测试，以及直接的配对键集合错配测试；现有文档审计测试继续覆盖 acceptance、源结果和表格绑定。
- 验证：全量 pytest 通过 226 项；论文一致性审计通过 1815 项，其中 6 个候选配对键检查全部通过；Split coverage、容器复现性、确认性设计审计、compileall 和 `git diff --check` 均通过。统计 JSON、CSV、SVG、设计审计和 Sealed execution plan 已刷新；Sealed21 仍为 dry-run，未调用模型，原有 readiness blockers 不变。

# 第136轮验证计数修订：定向测试 63 项，全量 pytest 224 项，论文一致性审计 1809 项，其他审计与 compileall/git diff --check 均通过。

## 第138轮 - strict acceptance 前置验证与源码 provenance 完整性

- 位置：`paper/analyze_experiments.py`、`paper/audit_paper_consistency.py`、`tests/test_experiment_analysis.py`、`paper/generated/statistics.json`、`paper/generated/PAPER_CONSISTENCY_AUDIT.json`
- 发现：第137轮虽然已在候选统计中检查配对键集合，但统计生成器仍可直接读取一个被篡改的 `acceptance.strict.json` 摘要；strict acceptance 的统一重算只在后置论文审计执行，生成器短暂写出的候选表并非独立可信。同时，统计分析实际调用了 `run_acceptance_gate.py`，但 source manifest 未绑定该源码，源码变更可能不触发统计输入漂移门禁。
- 修改：`candidate_rows()` 发现 strict artifact 时调用统一 `verify_acceptance_artifact()`，复核格式、decision、rule、两侧路径与 SHA256、每个 split 的重算结果及 artifact TOCTOU 稳定性；新增项目根目录导入路径，确保 `python paper/analyze_experiments.py` 命令行入口可直接执行。统计输入快照、`source_files` 和论文审计 expected source set 均加入 `acceptance/scripts/run_acceptance_gate.py`。
- 测试：strict artifact 正向夹具改为真实的双 split/双 repeat/有 reward 结构；新增只篡改 `reason` 的负向测试，确认统计生成器本身拒绝摘要漂移；source manifest 测试要求 gate 源码存在。
- 验证：全量 pytest 通过 226 项；论文一致性审计通过 1817 项；统计 source manifest 共 21 个文件且明确绑定 acceptance gate；Split coverage、容器复现性、确认性设计审计、compileall 和 `git diff --check` 均通过。Sealed21 仍为 84 单元 dry-run，`ready_to_execute=False`，阻断原因不变。

## 第139轮 - historical acceptance 重算前移并共享实现

- 位置：`paper/analyze_experiments.py`、`paper/audit_paper_consistency.py`、`tests/test_experiment_analysis.py`、`paper/generated/statistics.json`、`paper/generated/PAPER_CONSISTENCY_AUDIT.json`
- 发现：第138轮只把 strict v1 acceptance 的重算前移到统计生成器；历史 `acceptance.json`（v0）仍在生成器中直接读取 `decision/reason/splits`，而论文审计器另有一份独立的 historical 重算函数。两套实现可能在规则演进后产生统计表与审计结论分叉。
- 修改：新增公共 `recompute_historical_acceptance()`，从两侧原始 `result.json` 重新计算 fixed-denominator v0 的 repeat 指标、split delta、improved/dropped 状态、decision、reason 和 rule。`candidate_rows()` 对 historical acceptance 逐字段比对该重算结果后才写出候选统计；论文审计改为调用同一公共函数，并删除重复的嵌套算法实现。
- 测试：新增历史 acceptance 只篡改 `reason` 的负向测试；现有配对键集合门禁继续在共享重算函数中执行，保证历史 v0 也不能使用错配任务。
- 验证：全量 pytest 通过 227 项；论文一致性审计通过 1817 项；统计产物含 6 个候选行、21 个源码/输入记录；Split coverage、容器复现性、确认性设计审计、compileall 和 `git diff --check` 均通过。Sealed21 仍为 84 单元 dry-run，`ready_to_execute=False`，阻断原因不变。

## 第140轮 - Sealed 预注册 endpoint、missingness 与 multiplicity 语义门禁

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`、`paper/generated/PAPER_CONSISTENCY_AUDIT.json`、`paper/generated/sealed21-execution-plan.json`
- 发现：Sealed 协议审计此前已核对 alpha、success rule、Bootstrap/置换方法、rerun 次数和不替换/不插补，但 `primary_endpoint.name/contrast/unit`、missingness 的 classification/completion_rule/failure_rule，以及 multiplicity 的 primary_hypotheses/adjustment/rationale 仍只由预注册文件和正文承载；这些字段被篡改时，部分执行计划语义检查仍可能通过。
- 修改：`sealed_protocol_checks()` 现在逐字段绑定上述 11 个预注册语义字段，要求它们与冻结的 EI 协议完全一致；这些检查只约束 Sealed21 确认性协议，不改变 Clean64 历史固定分母描述性统计。
- 测试：新增 primary endpoint、multiplicity adjustment 和 missingness completion rule 的负向回归测试，分别验证协议字段漂移会使审计失败。
- 验证：全量 pytest 通过 229 项；论文一致性审计通过 1826 项，其中 32 项 Sealed protocol checks 全部通过；Split coverage、容器复现性、确认性设计审计、compileall 和 `git diff --check` 均通过。Sealed21 仍为 84 单元 dry-run，`ready_to_execute=False`，阻断原因保持为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 第141轮 - primary endpoint success rule 的 alpha 参数化与产物闭环

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、`tests/test_experiment_document_consistency.py`、`paper/generated/sealed21-execution-plan.json`、`paper/generated/PAPER_CONSISTENCY_AUDIT.json`
- 发现：分析器在 `main()` 中内联使用 `p < 0.05` 计算 `pre_registered_success`；虽然预注册 alpha 会被检查为 0.05，但成功判定没有显式读取该参数，且 `sealed_statistics.json` 没有记录或重算 primary success contract。未来 alpha 或产物中的 delta/CI/p 被篡改时，部分状态检查可能无法证明成功布尔值来自冻结规则。
- 修改：新增 `evaluate_primary_success(stats, alpha)`，从已验证的 preregistration endpoint 读取 alpha，统一计算 `inference_valid`、`delta > 0`、CI 下界 > 0 和 `p < alpha`。`sealed_statistics.json` 新增 `primary_endpoint_alpha` 与 `primary_success_rule`；`validate_sealed_statistics_payload()` 验证 alpha/rule schema 并重算 `pre_registered_success`，complete/incomplete 两种状态均不得与重算值不一致。
- 测试：新增 alpha 参数化测试（同一 p=0.08 在 alpha=0.10 时成功、alpha=0.05 时失败），并扩展状态夹具覆盖新元数据字段。
- 验证：全量 pytest 通过 230 项；论文一致性审计通过 1826 项；Split coverage、容器复现性、确认性设计审计、compileall 和 `git diff --check` 均通过。当前尚无 `sealed_statistics.json` 是预期的，因为 Sealed21 尚未执行；execution plan 仍为 84 单元 dry-run，`ready_to_execute=False`，阻断原因不变。

## 第142轮 - sealed_statistics 与冻结 preregistration 的 success contract 跨文件绑定

- 位置：`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`、`paper/generated/PAPER_CONSISTENCY_AUDIT.json`、`paper/generated/sealed21-execution-plan.json`
- 发现：第141轮的 `validate_sealed_statistics_payload()` 已要求 `primary_endpoint_alpha` 在合法范围内、`primary_success_rule` 文本正确，但正式产物若单独修改 alpha 或规则文本，仍可能与 freeze 绑定的 preregistration 脱离；仅有产物内部自洽不能证明它仍遵循预注册协议。
- 修改：`sealed_statistics_status_checks()` 现在读取冻结 preregistration，逐项比较产物 `primary_endpoint_alpha` 与 `primary_endpoint.success_rule`；产物状态校验和跨文件审计共同形成 success contract 闭环。
- 测试：新增 alpha 脱离 preregistration、success-rule 脱离 preregistration 两个负向测试；不创建或伪造正式 Sealed 结果文件。
- 验证：全量 pytest 通过 232 项；论文一致性审计通过 1826 项，其中 11 项 sealed statistics 状态检查在无正式产物时保持清晰跳过；Split coverage、容器复现性、确认性设计审计、compileall 和 `git diff --check` 均通过。Sealed21 仍为 84 单元 dry-run，`ready_to_execute=False`，阻断原因不变。

## 第143轮 - SEALED_REPORT 显式呈现 alpha 与 primary success rule

- 位置：`paper/analyze_sealed.py`、`paper/audit_paper_consistency.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`、`paper/generated/PAPER_CONSISTENCY_AUDIT.json`
- 发现：`sealed_statistics.json` 已记录 `primary_endpoint_alpha` 和 `primary_success_rule`，但 `SEALED_REPORT.md` 只写自然语言“p < 0.05”成功条件，没有显式展示产物实际采用的 alpha；报告读者和后续制表流程无法直接核对显著性阈值。
- 修改：`render_markdown()` 增加 `primary_endpoint_alpha=<alpha>` 与 `primary_success_rule=<rule>` 两个机器可检索 marker；报告由 `main()` 传入冻结 preregistration 读取的实际字段。论文审计在正式报告存在时核对两个 marker 与 `sealed_statistics.json`，并继续核对产物与 preregistration 的跨文件绑定。
- 测试：扩展 incomplete 报告渲染测试，要求报告出现 alpha 和完整 success rule；现有 payload 篡改测试继续覆盖产物状态和预注册绑定。
- 验证：全量 pytest 通过 232 项；论文一致性审计通过 1826 项；Split coverage、容器复现性、确认性设计审计、compileall 和 `git diff --check` 均通过。由于 Sealed21 尚未执行，正式报告 marker 检查当前不产生实际条目；execution plan 仍为 84 单元 dry-run，`ready_to_execute=False`，阻断原因不变。

## 2026-08-12：第144轮 - execution plan 绑定冻结统计合同

- 位置：`workflow/scripts/run_qwen_sealed21.ps1`、`paper/audit_paper_consistency.py`、`tests/test_experiment_document_consistency.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：Sealed21 execution plan 之前只记录任务规模、重复次数、readiness 及 preregistration 文件哈希，没有把主终点的 alpha/成功规则/统计方法写入计划；因此仅凭文件哈希无法证明启动器实际执行的统计合同与论文审计使用的合同一致。
- 修改：启动器读取冻结预注册中的 `primary_endpoint`、`statistical_design`、`missingness_policy`、`multiplicity`，逐项写入机器可读 execution plan；若任一合同节缺失则在生成计划前失败。论文审计新增四个逐对象一致性检查，直接将计划合同与冻结预注册重算比对。
- 测试：新增主终点 alpha 篡改和 bootstrap seed 篡改两项负向回归测试，均能使审计失败；定向一致性测试通过 65 项，论文一致性审计通过 1826 项。
- 验证：Sealed21 dry-run 成功重新生成计划，`evaluation_cells=84`、外层预算上限 `52.5 h`，未调用模型；`ready_to_execute=False`，阻断原因仍为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。直接调用 `pytest.exe` 的路径导入失败已通过 `python -m pytest` 排除，属于启动方式问题。

## 2026-08-12：第145轮 - sealed freeze 统计合同 provenance 闭环

- 位置：`workflow/scripts/run_qwen_sealed21.ps1`、`paper/analyze_sealed.py`、`paper/audit_paper_consistency.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：第144轮已将统计合同写入 execution plan，但正式分析依赖的 `sealed_freeze.v3` 仍只有 preregistration 路径和 SHA256；freeze 自身被篡改时，分析器只能间接依赖外部预注册文件，无法证明执行时冻结的主终点和统计设计未漂移。
- 修改：启动器将 `primary_endpoint`、`statistical_design`、`missingness_policy`、`multiplicity` 同步写入 freeze；`validate_frozen_provenance()` 对四个区段逐对象比较 freeze 与 preregistration；论文审计进一步比较 freeze 与 execution plan，形成 preregistration → plan → freeze 三段式 provenance 链。
- 测试：新增 freeze 主终点 alpha 篡改负向测试；Sealed 分析/启动器定向测试全部通过。
- 验证：全量 pytest 通过 235 项；论文一致性审计通过 1830 项且失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。dry-run 未调用模型，当前 freeze 尚未生成，`ready_to_execute=False` 的四个前置阻断原因不变。

## 2026-08-12：第146轮 - 确认性设计审计改为消费冻结统计合同

- 位置：`paper/audit_confirmation_design.py`、`paper/audit_paper_consistency.py`、`tests/test_confirmation_design_audit.py`、`paper/generated/design-audit/confirmation_design_audit.json`
- 发现：设计分辨率审计仍硬编码 `alpha=0.05`、Bootstrap seed `20260812` 和成功判定表达式；虽然当前数值与预注册相同，但修改预注册后设计审计可能继续输出旧结论，形成统计合同漂移。
- 修改：设计审计现在读取 `configs/experiments/ei_confirmation_v1.json` 的 `primary_endpoint.alpha`、`primary_endpoint.success_rule` 和 `statistical_design.bootstrap.seed`；生成物显式记录 alpha、seed，并把预注册文件加入 source inventory。论文一致性审计同步要求该 source inventory 与重建结果一致。
- 测试：新增自定义 alpha=0.10 fixture，验证设计审计使用传入的预注册合同，而不是硬编码 0.05；定向文档审计全部通过。
- 验证：全量 pytest 通过 236 项；论文一致性审计通过 1834 项、失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。设计生成物记录 `primary_endpoint_alpha=0.05`、`bootstrap_seed=20260812`，Sealed dry-run 未调用模型且执行阻断状态不变。

## 2026-08-12：第147轮 - 正式分析入口补齐主终点语义与多重性门禁

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：`validate_preregistration()` 已校验 metric、置信区间、检验方法、alpha 和成功规则，但未在正式分析入口强制核对主终点的 `name/contrast/unit`，也未核对 multiplicity 的假设数量、调整方法和理由；论文审计虽可发现漂移，分析器本身仍可能先接受不完整合同。
- 修改：新增固定 endpoint 语义字段校验，以及完整 multiplicity 合同校验；任何字段漂移都会在 provenance 阶段直接拒绝分析。扩展测试 fixture 以包含完整 endpoint 语义，并新增 contrast 漂移和 multiplicity adjustment 漂移两项负向测试。
- 验证：首次全量测试按预期发现分析源码变化导致旧 execution plan 的 analysis-code SHA256 失配，重新执行 Sealed21 dry-run 更新计划后恢复通过；最终全量 pytest 通过 236 项，论文一致性审计通过 1834 项且失败数为 0，确认性设计、Split coverage、容器复现性、compileall 和 `git diff --check` 均通过。未调用模型，`ready_to_execute=False`，阻断原因不变。

## 2026-08-12：第148轮 - 正式分析入口补齐 missingness 完整语义

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：正式分析入口此前只校验 missingness 的 rerun 次数、是否替换任务和是否插补，未校验 invalid 分类、完成规则和失败规则；因此可能接受“invalid 被当作行为失败”或“删除 invalid 单元后继续推断”的语义漂移。
- 修改：`validate_preregistration()` 现在要求 `missingness_policy` 六个字段逐对象匹配冻结合同：classification、reruns、replacement_tasks、imputation、completion_rule、failure_rule。同步补齐 Sealed 测试 fixture 的完整策略结构。
- 测试：新增 classification 漂移和 failure_rule 漂移两项负向测试；测试先暴露并修复旧 fixture 的简化合同，随后全部通过。
- 验证：全量 pytest 通过 238 项；论文一致性审计通过 1834 项且失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。Sealed21 dry-run 未调用模型，`ready_to_execute=False`，四个前置阻断原因不变。

## 2026-08-12：第149轮 - Sealed 统计产物显式记录缺失值与多重性合同

- 位置：`paper/analyze_sealed.py`、`paper/audit_paper_consistency.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：`sealed_statistics.json` 之前直接记录 alpha、success rule 和 statistical design，但 missingness policy 与 multiplicity 只存在于 provenance 输入链；EI 读者必须额外追溯预注册才能确认“不插补/不替换任务/单一主假设”等关键规则。
- 修改：统计产物新增完整 `missingness_policy` 和 `multiplicity` 对象；`validate_sealed_statistics_payload()` 对两者执行严格合同校验；`SEALED_REPORT.md` 增加机器可检索的 JSON 合同行；论文一致性审计逐对象比较产物与冻结预注册。
- 测试：扩展正式 Sealed 产物 fixture，验证产物包含 `imputation=false` 和 `primary_hypotheses=1`；已有 incomplete 状态 fixture 同步完整合同字段。
- 验证：全量 pytest 通过 238 项；论文一致性审计通过 1834 项、失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。dry-run 未调用模型，`ready_to_execute=False`，阻断原因不变。

## 2026-08-12：第150轮 - SEALED_REPORT 成功标准文本参数化

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：报告已显示机器可检索的 `primary_endpoint_alpha` 和 `primary_success_rule`，但自然语言成功标准行仍硬编码 `p < 0.05`；若未来冻结合同使用其他 alpha，机器字段与论文正文会不一致。
- 修改：`render_markdown()` 的自然语言成功标准现在直接渲染传入的完整 `primary_success_rule`；报告不再独立维护 p 值阈值。新增 alpha=0.10 fixture，要求报告出现 `p < 0.10` 且不出现旧的 `p < 0.05`。
- 验证：全量 pytest 通过 239 项；论文一致性审计通过 1834 项且失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。重新生成 execution plan 后 analysis-code 哈希一致；Sealed dry-run 未调用模型，`ready_to_execute=False`，阻断原因不变。

## 2026-08-12：第151轮 - SEALED_REPORT 设计规模与 Bootstrap 元数据参数化

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：报告成功规则已参数化，但正文的任务数、repeat 数、角色数、评测单元数、Bootstrap 重采样数和 seed 仍读取硬编码常量；设计合同变化时，机器产物与论文正文可能分叉。
- 修改：`render_markdown()` 新增 `design` 参数，按同一 `statistical_design` 对象计算任务数 × repeat × role 的评测单元数，并读取 Bootstrap `resamples/seed`；`main()` 将 payload 的 design 原样传入报告。
- 测试：新增非默认设计 fixture（3 任务、3 repeats、2 roles、123 次 Bootstrap、seed=77），验证报告渲染 `18` 个评测单元和新元数据；更新正式 fixture 的旧文案断言。
- 验证：全量 pytest 通过 240 项；论文一致性审计通过 1834 项且失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。dry-run 仍为 84 cells、52.5 h、未调用模型，readiness 阻断原因不变。

## 2026-08-12：第152轮 - SEALED_REPORT 角色标签与容器证据去硬编码

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：报告设计规模已参数化，但表头、invalid 计数标签和类别表仍固定写 `Baseline/Candidate`；这会掩盖角色合同变化。容器计数虽然已读取快照，但报告其他设计标签仍不是完全由 metadata 驱动。
- 修改：报告从 `design.roles` 读取两个角色标签，动态生成主表和类别表表头及 unresolved-invalid 标签，并在渲染前要求恰有两个角色，避免角色数量异常时生成误导性报告。
- 测试：非默认设计 fixture 使用 `control/treatment` 角色，验证报告不再出现隐含的 baseline/candidate 表头。
- 验证：全量 pytest 通过 240 项；论文一致性审计通过 1834 项且失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。重新生成 execution plan 后哈希一致，dry-run 未调用模型，readiness 阻断原因不变。

## 2026-08-12：第153轮 - SEALED_REPORT 置换检验方法元数据化

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：报告正文的置换检验说明仍固定写“任务级、精确、有理数符号置换”，没有展示 `unit/sidedness/exact/aggregation/algorithm`；统计 design 发生变化时，EI 方法描述可能继续保留旧算法。
- 修改：`render_markdown()` 现在从 `design.permutation` 渲染完整检验元数据；主表、设计规模、Bootstrap 和置换方法均由同一 design 对象驱动。
- 测试：非默认 design fixture 使用 `unit=item`、`sidedness=one-sided`、`exact=False`、自定义 aggregation/algorithm，验证报告输出这些值。
- 验证：全量 pytest 通过 240 项；论文一致性审计通过 1834 项且失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。dry-run 未调用模型，`evaluation_cells=84`、`52.5 h` 上界和 readiness 阻断原因不变。

## 2026-08-12：第154轮 - 统计表头的分析单位元数据化

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：置换方法正文已读取 design metadata，但主统计表的 CI 和 p 值列标题仍固定写“任务聚类/任务级”；若 Bootstrap 或 permutation 的 unit 改变，表头会与方法合同不一致。
- 修改：主表列标题现在分别读取 `design.bootstrap.unit` 和 `design.permutation.unit`；例如 `item 聚类 95% CI`、`item 置换 p`。测试 fixture 同步加入 `bootstrap.unit`，避免不完整设计对象被误当作有效合同。
- 测试：非默认 `item` unit 设计验证表头、角色标签和方法正文均使用新单位。
- 验证：全量 pytest 通过 240 项；论文一致性审计通过 1834 项且失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。dry-run 未调用模型，84 cells、52.5 h 上界和 readiness 阻断原因不变。

## 2026-08-12：第155轮 - 类别描述性结果绑定角色映射

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：类别表头已使用动态角色标签，但类别数据行仍固定读取 `baseline_rate/final_rate`；角色合同变化时可能出现表头与数据列错位。
- 修改：`category_rows()` 新增 `role_rates` 映射；报告优先按 `design.roles` 从该映射读取类别数据，并保留旧字段回退以兼容历史 fixture。自定义角色数量仍由报告层严格限制为两个。
- 测试：新增 `control/treatment` 类别行，验证类别表输出 25.00%/75.00% 与动态角色列一致。
- 验证：全量 pytest 通过 240 项；论文一致性审计通过 1834 项且失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。dry-run 未调用模型，readiness 阻断原因不变。

## 2026-08-12：第156轮 - 类别结果区分任务簇与重复 attempts

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：类别统计此前只输出 `attempts`，没有显示对应 task-cluster 数；在每任务多次 repeat 的设计下，EI 读者可能把 attempts 误当作独立任务样本。
- 修改：`category_rows()` 新增 `task_clusters` 和 `comparison_unit=task`；类别表改为同时显示 Task clusters 与 Attempts，明确重复观测与聚类分析单位的区别。
- 测试：类别 fixture 验证 1 个 task cluster、2 个 attempts 的表格输出，并继续验证角色映射数据列。
- 验证：全量 pytest 通过 240 项；论文一致性审计通过 1834 项且失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。dry-run 未调用模型，84 cells、52.5 h 上界和 readiness 阻断原因不变。

## 2026-08-12：第157轮 - sealed_categories 结构与数值不变量校验

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：类别 CSV/Markdown 共享 `categories` rows，但此前没有统一 schema 校验；类别名重复、task/attempt 计数异常、role rate 越界或 delta 被篡改都可能进入报告。
- 修改：`validate_sealed_statistics_payload()` 现在校验类别列表：类别名唯一非空；`task_clusters`、`attempts` 为正整数且 attempts 不少于 task clusters；`comparison_unit=task`；两角色比例有限且在 [0,1]；delta 必须等于 candidate rate−baseline rate。分析器写出统计产物前执行该校验。
- 测试：新增类别 delta 篡改负向测试；正式分析 fixture 保持通过并继续生成 `sealed_categories.csv`。
- 验证：全量 pytest 通过 241 项；论文一致性审计通过 1834 项且失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。dry-run 未调用模型，执行阻断原因不变。

## 2026-08-12：第158轮 - 类别统计跨行总量守恒校验

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：第157轮已校验每个类别行的结构和 delta，但类别漏行或重复计数仍可能在单行校验下通过；尚未要求类别 task_clusters/attempts 与主分析总任务/总 attempts 守恒。
- 修改：统计产物校验现在在主分析提供 `tasks/attempts` 时，要求所有类别 `task_clusters` 和 `attempts` 分别精确求和到主分析总数；不一致即拒绝写出/消费产物。
- 测试：新增类别总量篡改负向测试，构造主分析 2 tasks/4 attempts 而类别仅覆盖 1/2，确认审计拒绝。
- 验证：全量 pytest 通过 242 项；论文一致性审计通过 1834 项且失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。dry-run 未调用模型，84 cells、52.5 h 上界和 readiness 阻断原因不变。

## 2026-08-12：第159轮 - 类别通过数与比例的双向一致性校验

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：第157—158轮已校验类别行的 `role_rates`、`delta` 及跨行总量，但仍可能出现 `baseline_passed/candidate_passed` 被篡改而比例保持在合法范围内的情况；这会让类别表的通过计数与展示比例失配。
- 修改：`validate_sealed_statistics_payload()` 现在要求两个角色的通过数均为 `[0, attempts]` 内的整数，并逐项验证 `role_rates == passed / attempts`；保留 `delta == candidate_rate - baseline_rate` 和跨行总量守恒校验。生产统计行已包含对应通过计数，因此正式产物可被同一合约闭环验证。
- 测试：修正 delta 漂移和类别总量漂移 fixture，使其先满足通过数—比例一致性后再命中目标错误；新增比例篡改负向测试，验证比例与通过数不一致时被拒绝。
- 验证：定向测试通过 46 项；全量 pytest 通过 245 项；论文一致性审计通过 1834 项且失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。重新生成 Sealed21 execution plan 后，dry-run 仍未调用模型，`ready_to_execute=False`，阻断原因仍为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 2026-08-12：第160轮 - 类别级 invalid 计数与通过数互斥校验

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：类别统计此前只记录通过数、attempts 和比例，无法直接展示某类别中有多少基础设施无效单元；仅校验通过数范围仍可能把 invalid 单元与通过数重叠计入，削弱类别分析的可解释性。
- 修改：`category_rows()` 现在分别统计 `baseline_invalid` 和 `candidate_invalid`；类别 Markdown/CSV 行同步输出两侧 invalid 数。`validate_sealed_statistics_payload()` 要求 invalid 计数为合法整数，并强制 `passed + invalid <= attempts`，从而避免把同一 attempt 同时当作行为通过和基础设施无效。
- 测试：更新类别统计 fixture；新增“通过数与 invalid 数重叠”的负向测试；报告 fixture 增加两侧 invalid 列断言。
- 验证：定向测试通过 47 项；全量 pytest 通过 246 项；论文一致性审计通过 1834 项且失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。重新生成 Sealed21 execution plan 后，dry-run 未调用模型，`ready_to_execute=False`，阻断原因仍为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 2026-08-12：第161轮 - 类别通过数与 invalid 数跨行守恒

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：第160轮已约束每个类别行内部的通过数、invalid 数和 attempts，但类别行整体仍可能与主分析的 `baseline_passed/final_passed/baseline_invalid/final_invalid` 脱节；这种替换会使类别表与主结果的总计不一致。
- 修改：`validate_sealed_statistics_payload()` 在类别总 task/attempt 守恒之外，新增四项跨行守恒：类别 baseline/candidate 通过数分别等于主分析 `baseline_passed/final_passed`，类别两侧 invalid 数分别等于主分析 `baseline_invalid/final_invalid`。只有主分析字段存在且为整数时才启用对应比较，兼容不含这些汇总字段的历史不完整 fixture。
- 测试：新增“类别通过总量漂移”负向测试，验证类别行内部完全合法但与主分析通过总数不一致时仍被拒绝；既有类别 task/attempt 总量、比例篡改和 passed-invalid 重叠测试继续保留。
- 验证：定向测试通过 48 项；全量 pytest 通过 247 项；论文一致性审计通过 1834 项且失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。重新生成 Sealed21 execution plan 后，dry-run 未调用模型，`ready_to_execute=False`，阻断原因仍为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 2026-08-12：第162轮 - 正主分析总量下禁止类别整表缺失

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：即使已完成类别行内校验及跨行守恒，若把 `categories` 整体替换为空列表，旧逻辑会跳过所有类别总量检查；当主分析仍有正数任务/attempts 时，报告可能丢失完整的类别分解而不被拒绝。
- 修改：`validate_sealed_statistics_payload()` 现在在主分析 `tasks` 或 `attempts` 为正数时强制要求 `categories` 非空；零总量或历史最小 fixture 仍允许空类别列表。
- 测试：新增“正主分析总量但类别整表缺失”的负向测试，确认空类别列表被拒绝；既有类别行内、跨行和统计总量测试继续通过。
- 验证：定向测试通过 49 项；全量 pytest 通过 248 项；论文一致性审计通过 1834 项且失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。重新生成 Sealed21 execution plan 后，dry-run 未调用模型，`ready_to_execute=False`，阻断原因仍为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 2026-08-12：第163轮 - 类别统计复用主分析描述性通过计数语义

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：类别统计原先使用 `bool(row["passed"])`，而主分析使用 `passed_for_descriptive_rate()`；非布尔值（例如字符串 `"true"`）会被 Python truthiness 错误计为通过，造成类别表与主分析统计语义不一致。
- 修改：`category_rows()` 现在直接复用 `passed_for_descriptive_rate()`；JSON boolean 才能计入描述性通过数，非布尔值按失败计入固定分母，同时继续单独记录 invalid 数。这样类别、主分析和报告共享同一计数定义。
- 测试：新增类别统计非布尔 `passed` 负向测试；测试确认字符串值不会被隐式转换为通过，且 baseline/candidate 比例、delta、invalid 计数均按统一语义生成。
- 验证：定向测试通过 50 项；全量 pytest 通过 249 项；论文一致性审计通过 1834 项且失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。重新生成 Sealed21 execution plan 后，dry-run 未调用模型，`ready_to_execute=False`，阻断原因仍为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 2026-08-12：第164轮 - 类别 attempts 绑定预注册 attempts-per-task 设计

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：类别校验原先只要求 `attempts >= task_clusters`，但 Sealed21 预注册设计规定每个任务固定 2 次 repeat；例如 1 个任务配 3 个 attempts 会改变分母，却可能通过原有结构校验。
- 修改：`validate_sealed_statistics_payload()` 现在读取统计产物 `design.attempts_per_task`，并要求每个类别满足 `attempts == task_clusters × attempts_per_task`；缺失 design 的历史 fixture 默认按 Sealed21 的2次 repeat 处理，非法 design 会被拒绝。
- 测试：新增类别 attempts 与预注册设计漂移的负向测试，验证 1 task/3 attempts 配置被拒绝；正常正式产物和历史最小 fixture 继续通过。
- 验证：定向测试通过 51 项；全量 pytest 通过 250 项；论文一致性审计通过 1834 项且失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。重新生成 Sealed21 execution plan 后，dry-run 未调用模型，`ready_to_execute=False`，阻断原因仍为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 2026-08-12：第165轮 - 类别角色映射绑定统计设计元数据

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：类别校验此前将 `role_rates` 固定解释为 `baseline/candidate`，但未检查统计产物 `design.roles`；若角色标签被改写，类别数值可能内部自洽却与实验语义错位。
- 修改：类别校验现在要求 `design.roles` 明确为预注册的 `["baseline", "candidate"]`，并用该元数据验证 `role_rates` 的键集合、通过数比例和 delta 方向；缺失 design 的历史 fixture 使用相同的默认角色合同。
- 测试：新增角色元数据漂移负向测试，验证 `control/treatment` 等非预注册角色会被拒绝；attempts-per-task、类别守恒和非布尔 passed 测试继续通过。
- 验证：定向测试通过 52 项；全量 pytest 通过 251 项；论文一致性审计通过 1834 项且失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。重新生成 Sealed21 execution plan 后，dry-run 未调用模型，`ready_to_execute=False`，阻断原因仍为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 2026-08-12：第166轮 - 类别 comparison_unit 绑定预注册统计设计

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：类别行此前硬编码 `comparison_unit=task`，但没有验证统计产物 `design.comparison_unit`；若设计元数据被改为 attempt 等其他单位，类别表仍可能继续声称 task-level 分析。
- 修改：类别校验现在读取并严格验证 `design.comparison_unit`，要求其为预注册的 `task`，并要求每个类别行与该元数据完全一致；非法设计单位在读取类别前即被拒绝。
- 测试：新增 comparison unit 漂移负向测试，验证 `design.comparison_unit=attempt` 会被拒绝；角色、attempts-per-task、类别守恒和非布尔 passed 测试继续通过。
- 验证：定向测试通过 53 项；全量 pytest 通过 252 项；论文一致性审计通过 1834 项且失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。重新生成 Sealed21 execution plan 后，dry-run 未调用模型，`ready_to_execute=False`，阻断原因仍为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 2026-08-12：第167轮 - 类别 observation_unit 绑定 attempts 分母语义

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：第166轮已绑定类别 `comparison_unit`，但 `design.observation_unit` 尚未校验；若被改写为 task，类别 attempts 仍可能被解释成任务级观测，论文中的分母语义会与预注册协议不一致。
- 修改：统计产物校验现在要求 `design.observation_unit` 明确为预注册的 `attempt`；非法 observation unit 在类别行校验前直接拒绝。缺失 design 的历史 fixture 默认使用 attempt。
- 测试：新增 observation unit 漂移负向测试，验证 `design.observation_unit=task` 会被拒绝；comparison unit、角色、attempts-per-task 和类别守恒测试继续通过。
- 验证：定向测试通过 54 项；全量 pytest 通过 253 项；论文一致性审计通过 1834 项且失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。重新生成 Sealed21 execution plan 后，dry-run 未调用模型，`ready_to_execute=False`，阻断原因仍为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。

## 2026-08-12：第168轮 - Bootstrap/置换方法元数据绑定预注册合同

- 位置：`paper/analyze_sealed.py`、`tests/test_sealed_analysis.py`、`paper/generated/sealed21-execution-plan.json`
- 发现：统计产物此前只校验单位、角色和 repeat 分母，没有强制核对 Bootstrap 的 resamples/seed/百分位方法以及置换检验的 exact、sidedness、aggregation、algorithm；方法字段被改写后，数值可能仍看似合理但不再对应预注册分析。
- 修改：对存在完整 design 的正式产物，`validate_sealed_statistics_payload()` 逐字段锁定 Bootstrap（task、20,000、固定 seed、Type-7 百分位插值）和 permutation（task、双侧、exact、任务内 attempts 均值、rational 动态规划）合同；旧版缺少子字段的最小 fixture 保持兼容。
- 测试：新增 Bootstrap 方法元数据漂移负向测试；验证错误 resamples、seed 或 percentile method 会被拒绝。
- 验证：定向测试通过 55 项；全量 pytest 通过 254 项；论文一致性审计通过 1834 项且失败数为 0；确认性设计、Split coverage、容器复现性、compileall、`git diff --check` 均通过。重新生成 Sealed21 execution plan 后，dry-run 未调用模型，`ready_to_execute=False`，阻断原因仍为 `strict_decision_missing`、`strict_acceptance_artifact_missing`、`strict_acceptance_not_recomputed`、`source_snapshot_not_ready`。
