# Qwen 机制证据链审计

- 诊断簇：`disabled_local_recipient_maps / root_cause / workaround_instead_of_fix`
- 修改 hook：`build_execution_instruction`
- 机制：Add explicit anti-workaround guidance to execution instruction to prevent disabling validation features as shortcuts
- 直接目标是否确认：**否**

## 预声明直接目标

| 目标 | Split | 配对转移 | 无效原因 |
|---|---|---|---|
| `mailman#repeat-02` | train | invalid-involved | runtime failure: BadRequestError: Error code: 400 - {'code': 'Arrearage', 'message': 'Access denied, please make sure your account is in good standing. For details, see: https://help.aliyun.com/zh/model-studio/error-code#overdue-payment', 'request_id': '2f3bdf44-6b1c-9c45-a01b-d0d18b5cb782'} |

直接机制确认要求预声明目标出现 baseline 有效失败、candidate 有效通过。当前目标未满足该条件，不能用总体固定分母差值替代直接机制证据。

## 全部配对单元审计

- 有效 fail→pass：3
- 有效 pass→fail：0
- 有效 pass→pass：13
- 有效 fail→fail：2
- 任一侧 invalid：110

这些总体转移仅用于审计；只有 Clean64 补跑至零 invalid 后，才能重新判断机制与总体效应。
