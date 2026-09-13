# 公开错误诊断

SDK 0.2.3 支持以下安全公开分类，并保留请求身份与重试保护。发行状态以官方 GitHub Release 为准，不代表 PyPI 可用。具体原因需要兼容的服务端响应；现网可能仍返回旧版通用错误。SDK 不补造缺失原因，也不重新推断历史 `service_busy` 记录的真实原因。

| 分类 | 错误码 | 含义与处理 |
| --- | --- | --- |
| 用户输入 | invalid_request、strategy_group_invalid、unsupported_difficulty、strategy_capability_mismatch | 按安全约束修正明确指出的公开字段或选择。 |
| 生成结果 | model_invalid_result；历史 model_invalid_response | 服务生成的结果不合格，不是要求用户修复生成内容。可选 reason 区分结构、类型、范围、格式。 |
| 生成策略/隐私 | model_output_policy_conflict、model_output_privacy_rejected | 服务生成结果未满足任务要求或安全检查，不公开私有内容或字段路径。 |
| 模型服务 | service_busy | 模型供应方超时、不可用或实际繁忙。 |
| 服务端内部 | request_failed | 安全的服务端失败，不表示用户输入有误或用户账户欠费。 |
| 容量/进行中 | capacity_exceeded、request_in_progress | 服务准入容量或原请求仍在处理，不据此推断模型供应方故障。 |
| 历史原因不明 | operation_failed | 任务失败，但历史记录无法确定细分原因。使用 ServiceError，不归咎用户或猜测模型故障。 |

鉴权、权限、额度、幂等和不存在错误仍有各自分类。程序应依据 code/retryable，
不要解析翻译文案。SDK 使用固定中文文案，不信任任意远端文本。
历史 upstream_unavailable/upload_not_configured 改用 ServiceError，但维持此前
不自动重试的行为。已完成失败保留原 code/retryable，不重写历史记录。

禁止自动重试也覆盖从原通用繁忙响应拆出的全部类别：capacity_exceeded、
request_failed、operation_failed、model_invalid_result（含 model_invalid_response）、
model_output_policy_conflict、model_output_privacy_rejected、request_in_progress、
strategy_group_invalid、invalid_request、idempotency_conflict、resource_not_found。
连同 service_busy 和上述两个历史别名，即使 retryable=true，也只发起一次 HTTP
尝试或一次失败的 poll 就交还调用方。retryable 元数据不变，不意味着 SDK 可以
自动重复计费请求。调用方显式恢复时保留原 key/operation，已知 operation 只 GET。
既有 network_error 重试、退避与预算保持不变。

## 完整公开错误展示

服务冻结的公开错误目录中，每个错误码都有明确的 SDK 异常类或校验分类及固定
文案，不使用任意远端 message。尤其包括：

| 错误码 | SDK 异常类 | 含义 |
| --- | --- | --- |
| invalid_manifest、invalid_case_file、invalid_metadata、invalid_batch、invalid_log_parts、invalid_log_count、duplicate_log_name | ValidationError | 指出清单、Case、元数据、批次或附件关联/数量的问题。 |
| invalid_case_id、invalid_case_reference、invalid_client_request_id、invalid_idempotency_key、invalid_content_length | ValidationError | 修正明确指出的公开引用或请求字段。 |
| runtime_evidence_unsupported、unsupported_log_type、tool_capabilities_invalid | ValidationError | 证据格式/能力不支持，或工具声明不合法。 |
| rate_limited | LimitExceededError | 请求频率限制，不是账户额度不足。保留收到的 retryable；框架限流可以为 false，专用限流可以为 true。 |
| api_key_service_unavailable、strategy_catalog_unavailable | ServiceError | 鉴权基础服务或策略目录不可用，不表示密钥或用户输入有误。 |
| strategy_group_forbidden | PermissionDeniedError | 此密钥不能使用所选策略组。 |
| strategy_catalog_changed | ValidationError | 先刷新目录，再重新选择。 |
| sensitive_data_blocked、sensitive_content_detected | SensitiveDataError | 移除或脱敏敏感输入。 |

HTTP 与异步错误分类一致，异步 error 没有 HTTP status 也不会误分类。只读密钥、
不存在、经过验证的 Case 步数上限保留专用提示。未来未知错误码仍安全兜底，不公开
私有别名或服务端原文。

## 静态输入约束

每条诊断必填 field/reason。expected_type 是固定类型名称，minimum/maximum 是
静态约束而不是观测到的用户值。字符串按字符、数组按项、整数按数值计量。
可选约束省略时不编造，最多接受 16 条记录。

| 公开字段 | 类型 | 静态约束 |
| --- | --- | --- |
| strategy_id、strategy_version | string | maximum 40 |
| count、max_steps | integer | minimum 1 |
| repo_meta、behavior_spec、manifest | object | 无 |
| agent_description | string | maximum 2000 |
| behavior_spec.production_scenario、behavior_spec.behaviors_to_test、behavior_spec.prohibited_behaviors | string | maximum 4000 |
| tool_list | array | maximum 100 |
| evidence_capabilities | array | minimum 1、maximum 7 |
| status | string | allowed_values 精确为 completed、failed、timeout、aborted |
| force、allow_sensitive | boolean | 无 |
| agent_name、agent_version | string | maximum 160 |
| repo_fingerprint | string | maximum 71 |
| case_sha256 | string | 无 |
| case_signature | string | maximum 255 |

例如 invalid_request 返回 max_steps、reason=min_value、expected_type=integer、
minimum=1，会显示 max_steps 应为整数且最小为 1，不回显实际提交值。
畸形或未知详情触发 invalid_response；异步解析失败保留待恢复身份。
旧服务未提供详情时使用安全通用文案，不猜造事实。
仅对 invalid_request、model_invalid_result 和历史 model_invalid_response，兼容旧服务
返回精确空 JSON 对象 `details: {}`，视为没有已知详情。null、列表和非空未知结构
仍严格拒绝；不放宽 Case 步数上限的必填详情或其它错误码规则。

## 变更说明

- 区分调用方错误、服务生成失败和模型可用性。
- 显示有界静态输入约束和四种安全生成失败原因。
- 保留封闭 schema、原 retryable 和待恢复请求身份。
- 不意味着自动重试、再次计费或改写服务端历史记录。
