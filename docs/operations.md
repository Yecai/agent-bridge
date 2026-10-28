# 操作与排障

## 安装和主机登记

本机运行 `uv sync --group dev`，宿主配置独立 CLI 的绝对路径。当前支持 macOS/Linux 的进程组和文件锁语义；VPS 需要 Python 3.11+ 及可运行的 Claude Code。本机需要 uv、Git 和 ripgrep。

请求文件置于工作区授权范围以外并设置 0600。先运行普通 doctor；未知主机会返回一次性 challenge 和 SHA256 指纹。通过独立渠道核对后执行：

```sh
agentbridge trust-host --request-file /absolute/private/request.json --challenge CHALLENGE_UUID --fingerprint SHA256:FINGERPRINT
agentbridge doctor --request-file /absolute/private/request.json --test-tools
```

挑战十分钟内有效，只能使用一次，登记前会再次获取并核验主机密钥。主机密钥变化不得通过关闭校验解决。doctor 不执行登录或修改上游认证；认证配置存在不代表实际模型调用成功。显式工具测试在独立本机目录执行，会产生模型用量，诊断目录保留。

远端部署按本机 wheel 摘要建立私有运行目录和 venv，监督器只使用标准库；不依赖系统 pip/ensurepip，不安装系统软件包。Claude 的模型密钥由 VPS 自己管理，不写入宿主池的 SSH 连接配置。

VPS 保存 Claude 会话缓存和监督元数据，用于续接；MCP 返回的项目上下文会进入远端会话。本机保留完整审计镜像，项目文件本身及 Git、构建、测试的执行位置仍在本机。

## 运行、停止与续接

```sh
agentbridge run --request-file /absolute/private/request.json
agentbridge status --execution EXECUTION_UUID
agentbridge cancel --execution EXECUTION_UUID
```

CLI、本机监督器和远端监督器分别运行。CLI 心跳停止后，本机租约约 60 秒到期并发起清理；远端也有独立租约。本机命令还有归属管道 watchdog，拥有者被 SIGKILL 后也会停止所属进程组。外部故意脱离进程组的程序不属于完整 OS 沙箱保证。

短时 SSH 断开时先撤销工具权限，再在约 40 秒内按原主机身份、执行和控制版本重连。反向隧道恢复原端口并核验回环监听后才恢复权限。交互输入回执丢失不能重放，返回 input_outcome_unknown。

停止后续接需创建新的 execution_id，并保留原 session_id、machine/client/workspace 身份，设置 resume=true。远端稳定工作目录保证 Claude 找到原 transcript。不能将原 session_id 静默带到另一台机器；身份缺失、控制版本变化或停止未知都禁止重复拉起。

`stop_unconfirmed/outcome_unknown` 时保留池占用。先保留诊断，再核对两端执行状态、PGID 和启动标记。不要清空状态文件、重复原命令或随意 kill 一个复用的 PID。远端监督器不可达时，须核验原执行进程是否仍存在；没有证据不能手动标为停止成功。

`cancel` 在本机监督器退出后会自动恢复停止：读取私有缓存中的原始请求，校验执行身份、控制版本和请求指纹，通过已登记 SSH 主机核验远端，并按进程启动标记清理两端所属进程组。请求缓存放在调用方日志目录的同级私有目录，保持原业务数据域并排除在日志导出之外；全局执行状态只存路径指针与指纹。远端恢复只发送固定的标准库程序，不重新部署、不启动模型。证据写入本机执行状态，原始失败退出码保持不变。普通 `status` 查询不会触发 SSH、发信号或恢复。

旧执行尚无请求缓存时，可传 `agentbridge cancel --execution EXECUTION_UUID --request-file /absolute/original/request.json`；宿主会自动传入任务保留的原始私有请求。请求缺失、指纹或控制版本不符、PID 归属不明、远端不可达时继续保留停止未知状态与容量，恢复连接后再次终止即可重试。

## 日志与导出

```sh
agentbridge logs --execution EXECUTION_UUID --cursor 0
agentbridge export --execution EXECUTION_UUID --output /absolute/private/diagnostics.zip
```

日志为独立序号的 JSONL，完整请求/响应、stdout/stderr、输入、hooks、transcript 使用分片附件及摘要清单。原始业务正文和命令输出可能很大，界面按游标取页；早期输出仍保存在附件中。附件预览和 ZIP 只读取已登记的诊断文件，不包含私有请求、回调凭据或操作账本。

工作任务日志位于公司数据域，个人任务位于个人数据域，项目验收队列和预览日志也按项目数据域存放。新目录先确保 Git 忽略；Git 索引变化后重新检查是否误跟踪。旧日志来源保留，活动调用不会中途换文件。删除任务默认保留诊断，不自动清理。

已知 SSH 密码、回调 bearer、环境中的密钥/令牌在持久化前脱敏；Claude 原生新密钥确认界面显示的密钥尾部也会遮盖。未知业务秘密和自行编码的内容无法保证自动识别。诊断目录权限 0700，文件权限 0600，分享前仍应按实际业务内容检查。

发现 log.gap 时记录 cursor、source_seq、文件和原因。部分尾行不会被伪造为完整事件；不要为让界面“正常”而删除损坏行。日志开始写入失败会阻止新副作用；清理流程在日志失败时仍尝试停止已拥有的进程。

## 常见失败

| 错误或现象 | 检查方向 |
|---|---|
| host_untrusted / host_changed | 核对当前 SSH 指纹和已登记 known_hosts |
| ssh_unavailable | VPS、SSH、认证前网络及回环转发能力 |
| forwarding_exposed | sshd 的 GatewayPorts 实际监听地址 |
| auth / authentication_failed | VPS 的上游地址、密钥、账户权限；不要只看 auth status |
| schema_result_missing | 模型是否返回 StructuredOutput、hook 是否放行格式化工具 |
| 交互启动没有任务 | 查看原生初始化界面、MCP 握手和 hooks；不会在未知界面自动输入确认 |
| 新版 Claude 启动后声称没有 MCP 工具 | 首条任务须同时等待会话就绪和 `tools/list` 成功；`SessionStart` 不代表工具已接入。新版 `server/discover` 探测收到旧 SDK 的 400 后，可正常回退到 `2025-11-25` 握手，不应据此宣称不兼容，也不能假报支持新版协议 |
| stop_unconfirmed | 保留容量，核对两端监督器、进程组和原始执行身份 |
| log_path_tracked / audit_unavailable | Git 跟踪状态、磁盘空间和目录权限；先保留现场 |

grep 单次超时十秒、每页最多 500 条；目前先收集 rg 输出再分页，超大搜索应按目录或模式缩小范围。图片、文件和输出的单页读取也有上限，继续读取使用返回的游标。

## Local tool supervisor recovery

The tools service uses the existing private execution state root (or explicit AGENTBRIDGE_STATE). Its immutable tools-request.json, request fingerprint and exact results/ cache are private control data, not public audit attachments. Never put the state root inside an authorized project directory or include it in diagnostic ZIPs.

Use tools status/cancel for an execution even if its original CLI observer is gone. The supervisor outlives the observer until lease expiry or explicit cancellation. Recovery checks durable PID/start-marker ownership and process-group absence; a missing observer alone is not stop evidence. Reused or unprovable process identity remains stop_unconfirmed. Recovery never fabricates a successful exit code for a command whose result was lost.

A host must stop and reconcile the old execution before replacing it. Pending tool events from an old model turn must not be replayed on a new execution. Full commands run with local user privileges; file-root checks are not a shell sandbox.

For `tools call` / `process_output`, an integer `wait_seconds` is a polling upper bound. The CLI caps values above 5 to 5 seconds before contacting the supervisor, including an already-running older supervisor. Continue with `next_cursor` while the command runs; the polling limit does not change its execution timeout or repeat the command.

Argument rejections include the missing required field names and the allowed fields from the actual tool schema. Input values and file contents are not echoed. For example, an `apply_patch` call using `new_string` is told that the required field is `new`; aliases are not silently applied to mutations.

String commands run in Bash with `pipefail`: a failed test before `| tail` remains a failed process. Prefer argv arrays for verification and retain the complete log or structured report. If a pipeline intentionally ignores a stage failure, handle that explicitly in the command. This default applies to newly started supervisors; running supervisors retain their loaded code.
