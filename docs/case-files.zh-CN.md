# 保存并复用完整 Case

[English](case-files.md) | 简体中文

生成一次后，显式保存完整公共 Case，随后可在新 Run 或新进程执行，不再次请求 CaseGen：

```python
from kuma import create_run

run = create_run(repo_path=".", agent_profile_path="agent-profile.md")
saved = run.save_case("case.json")  # 新文件；父目录须已存在
run.cancel()  # 启动另一个 Run 前释放当前 Run

# 可在另一个进程执行，不需要 Profile 或 Case Provider。
reused = create_run(repo_path=".", case_path="case.json")
print(reused.case_origin)  # "official"
# 将 reused.get_input() 交给 Agent，再调用 reused.submit(agent_output)。
reused.cancel()  # 如果继续执行，省略这行
```

原有 Docker/API Key 要求不变。可信本地开发可传 `allow_local=True`；
`judge=False` 可不带官方服务凭据加载。加载不调用 CaseGen 或策略目录。
官方 Judge 仍可能协商 Evidence 并评价保存的 Case，这是独立 Judge 请求，
不表示评价免费，也不承诺零计费。

## 文件与身份

UTF-8 文件是 closed object，仅含 `schema_version`、`origin`、`case`、`integrity`。
`schema_version` 固定 `kuma.case_artifact.v1`，`origin` 为 `official` 或 `custom`。
整文件最多 5,242,880 bytes；JSON 必须有限，根深度 0、最大深度 32。
拒绝重复 key 和未知字段。不包含运行 ID、执行 history、Evidence、Agent 输出、
凭据或私有 Rubric；`allow_sensitive=True` 也不能绕过隐私拒绝。

官方文件只保存一份原始公共 schema-2 Case：`schema_version`、`batch_id`、
`case_id`、`strategy_id`、`strategy_version`、`repo_fingerprint`、`title`、
`description`、`steps`、`signature`。每步仅含 `step_id`、`prompt`。
integrity 保留既有七个公共引用字段，可额外包含已校验的
`executed_strategy_group`；没有第二份可独立编辑的规范化 Inputs。

自定义文件的 case 仅含 `case_id`、`input_type`、`input_schema`、`inputs`、
`extensions`，`integrity: null`。每个 input 仅含 `input_id`、`payload_type`、
`payload`、`public_constraints`、`extensions`。使用官方 Judge 也不会把自定义
Case 变成官方 Case；SDK 不导入或生成 Rubric。

加载保留 Case ID、Input ID/顺序和内容，只绑定新 Run ID。
`max_steps=None` 使用完整保存步数；显式值较小则拒绝，较大也不补步骤。
不得同时提供 Profile、Case Provider 或非默认 strategy。自动选组仍禁用。

## 完整性与官方 Judge

`signature` 是 `sha256:` 加未含 signature 的公共 Case canonical SHA-256；
`case_sha256` 对包含 signature 的完整 Case 计算。Canonical JSON 使用 sorted
keys、紧凑分隔符、`ensure_ascii=False`、有限值和 UTF-8。
**这两个是公开 checksum，不是证明服务端发行身份的数字签名。** 重算 hash
不会让被改动的官方 Case 获得授权。

新官方 Judge 请求仅把原始 schema-2 object 放进 multipart `case_file`，
文件名 `kuma-official-case.json`、MIME `application/json`，不再同时发送顶层
`case_id`。既有 integrity metadata 绑定同一对象；Case 字节计入协商的单文件/
总预算。Backend 在新 Judge reservation 前核对当前租户原件及原 Rubric；
已接受请求的恢复继续遵守原幂等规则。

缺少原始公共记录的旧 SDK 对象、只有 inputs 的 JSON 不能作为官方 artifact
加载。旧 Backend 可能拒绝新官方文件；SDK 原样报告安全错误，不改为 custom
或仅传 case ID 重试。想编写另一份 Case，请明确使用自定义 Provider，再保存其
自定义 Case；不要删除 origin 或伪造官方 integrity。

## 错误与文件安全

`case_artifact_invalid` 表示格式、大小或内容无效/改变；`case_origin_invalid`
表示来源缺失或冲突。敏感数据拒绝不回显命中值。文件缺失、不可读、路径不安全
及保存目标已存在时抛安全 `ConfigurationError`。相对路径基于 `repo_path`，
不基于 cwd。父目录须存在；拒绝链接、跨 mount 逃逸和覆盖。
保存使用临时文件、fsync、固定父目录下的原子不覆盖 hard link；文件系统不支持
时直接失败，不改用覆盖。保存/加载不执行工具，不上传本地配置。
