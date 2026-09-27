# Clean64–Sealed21 划分覆盖审计

本审计只读取冻结任务元数据，不读取模型输出、分数、轨迹或 verifier 结果。

任务记账：全集 89 = Clean64 64 + Sealed21 21 + 媒体能力边界排除 4；Clean64/Sealed21 重叠为 0。

## 分布距离

| 维度 | Total variation | Jensen–Shannon divergence (bits) |
|---|---:|---:|
| category | 0.480 | 0.320 |
| difficulty | 0.062 | 0.032 |

- 共同类别：data-science, model-training, scientific-computing, security, software-engineering, system-administration。
- Clean64 有但 Sealed21 缺失：data-processing, data-querying, debugging, file-operations, machine-learning, mathematics, personal-assistant。
- Sealed21 新增类别：games, optimization。

## 类别构成

| Category | Clean64 | Sealed21 |
|---|---:|---:|
| data-processing | 3 (4.7%) | 0 (0.0%) |
| data-querying | 1 (1.6%) | 0 (0.0%) |
| data-science | 2 (3.1%) | 5 (23.8%) |
| debugging | 5 (7.8%) | 0 (0.0%) |
| file-operations | 5 (7.8%) | 0 (0.0%) |
| games | 0 (0.0%) | 1 (4.8%) |
| machine-learning | 3 (4.7%) | 0 (0.0%) |
| mathematics | 4 (6.2%) | 0 (0.0%) |
| model-training | 2 (3.1%) | 2 (9.5%) |
| optimization | 0 (0.0%) | 1 (4.8%) |
| personal-assistant | 1 (1.6%) | 0 (0.0%) |
| scientific-computing | 7 (10.9%) | 1 (4.8%) |
| security | 5 (7.8%) | 3 (14.3%) |
| software-engineering | 20 (31.2%) | 5 (23.8%) |
| system-administration | 6 (9.4%) | 3 (14.3%) |

## 难度构成

| Difficulty | Clean64 | Sealed21 |
|---|---:|---:|
| easy | 4 (6.2%) | 0 (0.0%) |
| hard | 21 (32.8%) | 7 (33.3%) |
| medium | 39 (60.9%) | 14 (66.7%) |

解释边界：Sealed21 是结果盲、互斥的本地测试集，但不是从目标任务总体随机抽样，也未按 Clean64 类别比例分层。分布距离用于描述覆盖差异，不用于对结果加权、事后选择任务或修改预注册主终点。
