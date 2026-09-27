# Qwen 严格 promotion 配对补跑计划

- baseline result SHA256：`bdbcdf0ac465281878eed804320476f1c57184b37195dbd5989c425e002e68a0`
- candidate result SHA256：`ae6c20c2a6a386e0da569c130bd01b9df4cda090cce4ed5320b28a4abb1c4cbb`

## 当前配对完成度

- 双方有效：18 / 128
- 仅 baseline 无效：32
- 仅 candidate 无效：5
- 双方均无效：73
- 待补跑 cell：183
- 原因分布：dependency=1、inconsistent_result=1、missing_reward=1、other=1、provider=152、timeout=27

## 分阶段执行

| 阶段 | 目的 | Cells | 最多模型尝试 | 配置上界 |
|---|---|---:|---:|---:|
| Phase 0 | 每侧一个固定基础设施 canary；只检查是否得到数值 verifier 结果 | 2 | 6 | 1.62 h |
| Phase 1 | 补齐单侧缺失配对 | 37 | 111 | 30.06 h |
| Phase 2 | 补齐双方均缺失配对 | 146 | 438 | 118.62 h |

Phase 0 与后续阶段重叠，成功后必须重新生成计划以自动排除已完成 cell。任何阶段均不得依据 pass/fail 决定是否继续；只能依据是否恢复稳定的基础设施结果决定暂停或继续。确认性分析仍要求全部 183 个待补跑 cell 完成且最终为零 invalid。
