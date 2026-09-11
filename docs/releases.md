# Versions and releases / 版本与发布

## 0.2.1

Availability is determined by the corresponding official GitHub Release, not
this document or a development branch. 是否可用以对应正式 Release 为准。

- Corrects Judge file-count preflight: Case plus Evidence count toward the
  advertised per-item limit for both official and custom Cases. Batch counts
  each item independently rather than rejecting their combined file count.
- The matching Backend derives the file count as twice its supported maximum
  step count (currently 20 files); clients consume that value, not a hardcoded
  20 or a limit based on this Run's actual steps. Deploy the Backend first.
- 修复官方/自定义 Judge 文件计数；Case 与 Evidence 都占名额，batch 按每项
  分别计算。服务端按支持的最大步数 × 2 返回上限，客户端直接遵守配置。
- Existing byte budgets, privacy checks and request identities are unchanged.
  Combined bytes for custom single Judge remain enforced by the Backend; this
  patch does not add a new local combined-byte preflight for that path.
- 字节预算、敏感检查与请求身份不变；自定义 Case 单次 Judge 的合计字节仍由
  Backend 强制检查，本补丁不新增该路径的本地合计预检。
- This is an optional patch update from 0.2.0. It does not publish to PyPI or
  automatically install itself. 0.2.0 → 0.2.1 是可选补丁，不自动安装。

## 0.2.0

Release availability is determined by the corresponding official
[GitHub Release](https://github.com/DefuzeX-AI/KUMA-DefuzeX/releases), not by a
development branch or each new main commit. 发行状态以对应正式 Release 为准。

- Consolidates public source improvements since 0.1.0: Agent Profile naming,
  reusable complete Case files, bounded Agent output and Trace Evidence, optional
  unified file diffs and captured tool arguments/results. See the existing
  [Trace and file-diff guide](runtime-trace.md) for opt-in and privacy limits.
- Adds anonymous, nonblocking update reminders plus explicit Python/CLI checks.
- 独立版本汇总上述已有公开能力，并新增更新提醒。0.1.0 用户需手动升级一次并重启
  Agent，旧安装无法自行获得提醒；更新器不执行 pip、不自动安装。
- A GitHub Release does not imply PyPI publication or a service deployment.
  GitHub 发行版不代表已经发布 PyPI 包或变更服务部署。

## Version rules / 版本规则

| Installed → latest stable | Status | Meaning / 含义 |
| --- | --- | --- |
| 0.2.0 → 0.2.1 | optional | Patch-only: optional / 补丁升级，可选 |
| 0.1.9 → 0.2.0 | required | Higher minor: upgrade required reminder / 次版本，必须升级提醒 |
| 0.2.9 → 1.0.0 | required | Higher major: upgrade required reminder / 主版本，必须升级提醒 |
| 0.2.0 → 0.2.0 or 0.1.9 | up_to_date | Equal or ahead: silent / 相等或领先，不提醒 |

These are KUMA's rules, including pre-1.0. Required is a strong reminder, not a
denied API request, interrupted task, changed billing or automatic install.
主/次版本提示必须升级，但不阻断已有业务、不收费重试、不自动安装。
Only strict stable `vX.Y.Z` GitHub Release tags are compared; drafts/prereleases,
main commits and PyPI are not update sources. 不将草稿或每次提交当作新正式版。

## Check explicitly / 手动检查

```bash
kuma updates check
```

```python
from kuma import check_for_updates

result = check_for_updates()
print(result["status"], result["latest_version"], result["release_url"])
```

No options/arguments. Both interfaces provide the same detached JSON-compatible
object; the CLI prints JSON to stdout and returns zero for all these statuses:

| Field | Type / Meaning |
| --- | --- |
| status | disabled / checking / unavailable / up_to_date / optional / required |
| current_version | Installed package version / 本地包版本字符串 |
| latest_version | Validated stable version, or null / 已校验正式版本或 null |
| release_url | Official tag URL derived from that version, or null |
| cached | Boolean; true for a cached success/failure / 是否命中进程缓存 |

无需参数；离线/限流/畸形响应返回 unavailable，不输出原始错误或响应正文。
已有并发检查时立即返回 checking，不等待或重复联网。Required 也不会使 CLI 失败。

## Background checks and privacy / 后台检查与隐私

Successful real official Python/CLI transport schedules a daemon check centrally.
It never waits for GitHub; import/help/local/custom workflows do not check.
The cache contains one result for 24 hours per process, including failures, with
one request in flight and a nonblocking reservation lock. There is no disk cache.
Short-lived commands may exit before a reminder appears; use the explicit check
when you need its result. Reminders go to stderr, leaving business JSON intact.

官方实际请求成功后后台检查，不等待 GitHub；import/help/local/custom 不检查。
成功/失败均在当前进程缓存 24 小时、并发去重、不写磁盘；新进程重新开始。
短命进程可能先退出而没有提醒，可用显式命令；提醒仅写 stderr。

Set `KUMA_DISABLE_UPDATE_CHECK=1` to disable both explicit and automatic checks,
including cached reminders. Other values leave checks enabled. Set it before a
workflow; a request already sent cannot be unsent. 设置此开关可全部禁用，但已发出的
请求无法撤回。检测到禁用后不会再打印提醒。

The fixed official GitHub releases/latest HTTPS endpoint receives no credentials,
Backend headers, Agent/Evidence/repository content or local paths. No proxies or
redirects are followed; TLS uses Python's default verification. GitHub receives
ordinary connection metadata such as the source IP. Only validated version fields
are retained; arbitrary release text, URLs or commands are never executed.

固定官方 GitHub HTTPS 源；不发送 API Key/用户业务数据，不跟随代理或重定向。
只保留已校验版本，不执行远端返回的 URL/命令。每次响应最多 64 KiB、socket 超时
1 秒、不重试；socket 超时不是 DNS/操作系统调度的硬总时限，但业务不等待检查。
Each explicit fetch has a one-second socket timeout, 64 KiB limit and no retry;
socket timeout is not a hard DNS/OS wall-clock deadline. Ordinary failures are
safe unavailable results and never change the original business result.

## Publication / 发布约定

Every formal release has its own version, immutable tag and independent Release
notes, created after its accepted source is merged to public main. Existing tags
are never moved; new functionality must not be presented as a new release by
merely appending to old notes. 每次正式发布独立版本/tag/Release，不改旧 tag 指向。

Package metadata reads the single source `kuma._version.__version__`. Version,
docs and the tagged source must agree. Install a pinned release using its tag
only once it exists; installing main gets the latest source, not a promise of a
new formal release. 元数据、源码、文档版本必须一致；main 最新源码不等于新正式版。
