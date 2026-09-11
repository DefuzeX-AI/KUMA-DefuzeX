# 发送给 Judge 的 Runtime Trace

KUMA 可以发送**同进程实际采集的 OTel 观测**，不再只有 Trace hash。
安装可选 `otel` extra 并使用兼容 Provider：可以自动附着全局 Provider，
也可以显式 `configure_trace_evidence`。启用采集后，官方 Judge 会在服务端
广告 `runtime_trace` 时接收 Trace；没有第二个隐藏上传开关。
服务端不支持时，在 Judge POST 前明确报 `runtime_evidence_unsupported`，
不会静默退回 hash。未启用 Trace 的 Run 保持原兼容路径。

## 采集内容

真实 `gen_ai.operation.name="execute_tool"` span 可以携带
`gen_ai.tool.name`、`gen_ai.tool.type`、`gen_ai.tool.call.id`、
`gen_ai.tool.call.arguments` 和 `gen_ai.tool.call.result`。
这些值必须由实际 instrumentation 记录：KUMA 不执行工具、不解析 stdout、
不凭 span 名猜行为，也不把模型建议的工具调用当成已经执行。

例如，为真实本地函数埋点，无网络、无模型：

```python
import json
from opentelemetry.sdk.trace import TracerProvider
from kuma.otel import configure_trace_evidence

provider = TracerProvider()
capture = configure_trace_evidence(provider)
tracer = provider.get_tracer("example-agent")

def add(left, right):
    with tracer.start_as_current_span("execute_tool add") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", "add")
        span.set_attribute("gen_ai.tool.call.id", "call-add-1")
        span.set_attribute("gen_ai.tool.call.arguments", json.dumps({"left": left, "right": right}))
        result = left + right
        span.set_attribute("gen_ai.tool.call.result", json.dumps(result))
        return result
```

将 `trace_evidence=capture` 传给 `create_run(...)`，在 `run.get_input()`
与 `run.submit(output)` 之间调用该函数。span 必须在本步采集窗口内开始，
并在 Submission 准备前结束。跨窗口、未结束、采样或过滤损失会明确记账，
不会移到别的步骤；没有埋点就不会编造观测。

## 正文、预算与隐私

- OTel JSON 字符串最多解析一次；普通非 JSON 文本结果保留字符串。
  JSON null 与没有记录字段是两种情况。
- 每个参数／结果最多 **4 MiB canonical JSON**：键排序、ASCII 转义、紧凑格式、
  有限数值；深度最多 32，根为零。循环和不支持的对象会拒绝。
  默认 256 字符元数据限制不切割工具正文。Trace 默认仍是每 Run 8 MiB，
  完整 Runtime Evidence envelope 5 MiB，官方 multipart 合计 8 MiB
  （含其他 part 和 framing）；动态服务端限制还可能更小。
- 超限、非法或敏感正文**整字段省略**，提供状态并标记 partial。
  复用现有敏感扫描，`allow_sensitive=True` 不能绕过。
  上传总量超限报错，不静默降级成 hash，不截断 JSON。
- 在采集预算内保留 Trace/span/parent ID、时间、kind、原始
  `unset|ok|error` 状态、安全 attributes/events/resource/scope，以及最多
  128 个真实 links。传输安全上限为 10,000 spans，不是提高默认采集数量；
  默认仍为 200。

## 完整性与判断边界

每个工具 span 的 `tool_content_status.arguments`／`.result` 为
`present`、`not_recorded`、`sensitive_content`、`size_limit` 或 `invalid`。
正文缺失不代表工具没有执行。`not_recorded` 只增加 reason／partial，
不伪造丢弃计数；实际省略才计数。OTel `UNSET` 不等于失败；采集 complete
也不证明 Agent 所有操作都被埋点。日志与 Trace 是观测，不是授权或外部状态证明。

只有协商 `runtime_trace` 时，既有 Trace artifact 才携带
`trace_evidence`、`capture_status`、`capture_summary`。
SHA-256 与 size 绑定同一份 canonical Trace bytes。Judge 把它作为
**不可信遥测**处理，不把它冒充已验证的工具执行事实，也不复制到公开报告。
历史 hash-only 记录不能凭空恢复正文。

## 升级与埋点前提

此改动进入公开 main 后，在运行 Agent 的同一 Python 环境升级，并重启 Agent 进程：

```sh
python -m pip install --upgrade "kuma-defuzex[otel] @ git+https://github.com/DefuzeX-AI/KUMA-DefuzeX.git@main"
python -c "import kuma; print(kuma.__file__)"
```

为可复现可把 main 替换为已发布 commit。版本字符串未必能区分 Git 安装。
安装 OTel 不会替工具埋点；普通 span 可能没有参数/结果，既有 instrumentation
必须在当前 step 内记录真实的语义属性。

SDK 没有自动生成 typed `command_result`/`test_result` 的入口。已记录的命令、
测试观测可以作为工具 Trace 正文传输，但 KUMA 不运行测试、不解析 stdout、
不虚构组件或执行成功。缺少观测不能证明没有执行。

## 可选文件 diff

使用 `create_run(..., track_files=True, upload_diff=True)`，以独立 `file_diff`
能力将安全 unified patch 与 Trace 一起发送。默认 False 仍仅传文件哈希。
服务端不支持时在 Judge POST 前报错，不静默丢弃已请求的补丁。
每个 diff 最多 32,768 UTF-8 字节，每个 envelope 内总 diff 最多 65,536 字节。
不截断、不上传整文件；二进制、超限、敏感、无文本变化或采集不完整时，
保留哈希元数据并给出明确 omission reason。详见 [Runtime Evidence](runtime-evidence.md)。

```sh
PYTHONPATH=src python tools/verify_runtime_trace.py
```

该 verifier 使用真实本地 OTel 和模拟外部 transport，不访问服务、不用凭据或模型。
