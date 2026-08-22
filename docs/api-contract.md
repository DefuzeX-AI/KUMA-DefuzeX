# KUMA SDK API Contract

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

单次 HTTP `timeout` 与总 `operation_wait_timeout` 相互独立。总等待超时保留本地恢复元数据；再次执行同一 Case 请求或同一 Run Judge 时，已知 `operation_id` 只继续 GET，未知 `operation_id` 则用原幂等键重发同一 POST。恢复文件只含 operation ID/type、幂等键、Backend identity 和时间戳，不保存 API Key、请求、Evidence 或结果内容。当前高层 Python API 不能在整个进程丢失 `Run` 对象后仅凭 `run_id` 重建 Run；Judge 恢复仍要求原 Run/History 可用。

`POST /sdk/judge/batch/` 仍是既有同步批量接口。
