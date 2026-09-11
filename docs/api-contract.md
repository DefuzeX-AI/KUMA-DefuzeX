# KUMA SDK API Contract

## 保存的 Case 与官方 Judge 原件

`Run.save_case(path)` / `create_run(case_path=...)` 使用本地 closed
`kuma.case_artifact.v1`；完整字段、5 MiB/深度限制和来源规则见
[Case 文件](case-files.zh-CN.md)。新官方 Judge 上传 raw schema `2` 的完整十字段
公共 Case，part 为 `case_file`、filename `kuma-official-case.json`、MIME
`application/json`；不上传本地 artifact 外壳，不同时传顶层 `case_id`。
metadata 仍为既有 `repo_fingerprint/case_sha256/case_signature`，必须与 raw 一致。
batch 使用既有 `case_file_part` 指向各条目的 Case part。Case 文件字节计入原有
动态单文件和总预算；custom Case 仍用 `defuzex.custom_case.v1`。
公开 checksum 不是认证：Backend 在新 reservation 前核验租户原件和原 CaseGen
control 的唯一 Rubric 引用。SDK 不导入 Rubric，不把被编辑的官方 Case 降级为
custom，也不在旧 Backend 拒绝时退回 case_id-only。历史客户端引用路径不作为
新保存/加载流程的隐式兼容路径。

Base URL：`https://defuzex.ai/api/agentdefuze`

所有 URL 使用 trailing slash。SDK 请求使用：

```http
Authorization: Bearer dfx_<public-id>.<secret>
Accept: application/json
```

## Entitlements

`GET /sdk/entitlements/`

返回用户 ID、API Key 元数据、scopes、订阅等级和本周额度。不会返回完整 API Key 或 hash。

## Error semantics

- `401`：Key 缺失、格式错误、无效、过期或已撤销。
- `403`：用户、订阅或 scope 不允许该操作。
- `429`：账户当前额度已耗尽。

## Protected services

- `cases:generate`：Case generation。
- `judge:run`：LLM-as-Judge。

这些服务继承 Django backend 的统一 API Key authentication、subscription、scope 和 quota permission，并要求幂等键。SDK 只接受真实服务结果，不提供模拟成功回退。

## Official Case/Judge v2 operations

官方单 Case 和单 Judge 分别提交到：

- `POST /sdk/v2/cases/generate/`
- `POST /sdk/v2/judge/`

二者在接受请求或幂等回放时必须返回 HTTP `202`：

```json
{"operation_id":"...","status":"queued","poll_after_ms":1000}
```

`status` 可为 `queued`、`running`、`succeeded` 或 `failed`；`poll_after_ms` 是 `100..60000` 的权威毫秒间隔。SDK 使用同一 `Idempotency-Key` 重试完全相同的 POST，不回退到 v1。

SDK 通过 `GET /sdk/v2/operations/{operation_id}/` 获取终态。活动响应只含 `operation_id` 和 `status`；成功响应加入 `result`（既有 Case 或 Judgment payload）；失败响应加入 `error: {code, retryable}`。未知 operation 返回稳定的 HTTP `404 operation_not_found`。失败 operation 本身是 HTTP `200` wrapper。

单次 HTTP `timeout` 与总 `operation_wait_timeout` 相互独立。v2 首次 POST 可带
`X-Kuma-Client-Request-Id: kreq_<32位小写十六进制>`。Backend 将它与创建者、
精确 API Key、scope、endpoint、幂等键和请求 hash 绑定。SDK 可通过
`GET /sdk/requests/{client_request_id}/` 找回已经接受但响应丢失的公开
operation；已知 `operation_id` 只继续 GET。不同请求复用同一身份会稳定 409，
跨用户或跨 Key 查询表现为 404。

本地 `.kuma/requests/` 只保存有界身份与状态元数据，不保存 API Key、请求、
Evidence、Rubric 或 Provider 正文。Python `list_requests`、`show_request`、
`resume_request` 及对应 `kuma requests` CLI 可跨进程恢复。Backend 查不到 prepared
记录时，SDK 返回 `request_not_started`，不会发送无正文 POST。恢复成功的 Judge
公开报告保存到 `.kuma/reports/<run_id>.json`。

自定义 Case 上传禁止 `rubric`、`private_rubric` 和 `rubric_context`。官方 Judge
接收 closed 公共 Case，不接收调用方 Rubric ID、私有 revision ID 或 criteria。

`POST /sdk/judge/batch/` 仍是既有同步批量接口。
