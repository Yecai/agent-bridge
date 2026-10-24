# AgentBridge 协议 v1

本机 CLI 是宿主边界。宿主不导入 AgentBridge 包，也不共享 Python 虚拟环境。VPS Claude 通过本次执行独立的 HTTP MCP 连接访问本机；SSH 反向端口及 MCP 服务只监听回环。实际监听地址也会核验，拒绝 sshd 将端口改成公网监听的配置。

## RunSpec

完整字段和校验以 `src/agentbridge/protocol.py` 为准。请求必须是当前用户拥有的普通文件，权限 0600，不能使用符号链接。

| 字段 | 约束 |
|---|---|
| `schema_version` | 固定为 1 |
| `execution_id`、`trace_id` | UUID；前者标识一次执行，后者关联宿主流程 |
| `node_id`、`control_revision` | 节点身份及非负控制版本，工具和业务回执都核验 |
| `client_id`、`machine_id`、`workspace_id` | 稳定绑定；远端会话工作目录由客户端和工作区身份派生 |
| `cwd`、`read_roots`、`write_roots` | 本机绝对目录；只读执行不允许写目录及业务回调 |
| `log_dir` | 本机私有诊断目录；与含凭据的请求文件分开 |
| `target` | SSH host/port/user/known_hosts，以及可选的私有 password |
| `mode` | `print` 或 `interactive` |
| `session_id`、`resume` | 续接必须指定原会话；续接使用新的 execution_id |
| `model`、`effort` | 可选，按值转发给远端 Claude；模型为空时使用远端默认值 |
| `prompt`、`result_schema` | 正文通过私有文件、stdin 或 PTY 传递；print 可要求结构化结果 |
| `callbacks`、`callback_token` | 仅宿主生成的固定回环业务接口及独立短期凭据 |

同一 execution_id 的配置指纹不可改变。重复连接已有执行只恢复控制或读取缓存，不重新执行命令。停止未确认时不能通过换一个 execution_id 重试原副作用。

## MCP 工具

| 工具 | 权限及行为 |
|---|---|
| `workspace_context` | 真实本机平台、cwd、授权根、项目规则和技能索引 |
| `read_file` | 分页读取文本或支持的图片，返回内容版本 |
| `list_directory`、`glob`、`grep` | 本机查找和分页游标；游标绑定查询 |
| `write_file` | 创建必须声明 create；覆盖核验 expected_version |
| `apply_patch` | 核验版本及唯一精确片段，拒绝静默覆盖 |
| `exec` | 本机 Git、构建、测试及命令；返回 process_id |
| `process_output` | 按游标读取本次命令的完整 stdout/stderr |
| `process_stop` | 停止本次拥有的命令进程组并确认终态 |
| `dev_handoff` | 仅计划角色及已授权宿主回调可用 |
| `dev_finish` | 仅执行角色及已授权宿主回调可用 |

只读执行只暴露前五项。全权 shell 具有本机用户权限，文件工具根目录限制不构成 shell 沙箱。原生 Bash/Read/Write、其它 MCP 和插件被禁用；要求 JSON schema 时单独允许无文件副作用的 `StructuredOutput` 格式化工具。

受控 PreToolUse 从执行身份和真实 tool_use_id 派生 op_id，覆盖模型提供的操作身份。持久账本保存参数指纹和结果：相同操作返回原结果，参数变化拒绝，未知结果不重放。命令先持久化归属，再释放启动门禁。

## 事件与完成

stdout 为 JSONL，包含宿主身份、trace、execution、node、revision、source_seq、event_id、时间、事件、状态和附件引用。未知 Claude 事件仍保留。完整工具请求/响应、hook、原始双流和 transcript 放在脱敏附件，界面可分页预览及导出。

`claude.event` 内的原生 result 只代表模型返回，宿主不能据此释放池容量。最终 `bridge.result.data` 包含 `result/session_id/structured_output/usage/num_turns/is_error/stop_confirmed`。只有两端停止确认后才可发布节点完成；要求 schema 但缺少 structured_output 会失败。CLI 先读取终态，再排空最后一页事件，避免漏掉最终结果。

日志读取报告序号缺口、非法行和未完成行。CLI 显式输出 `log.gap`，结束的执行若只剩损坏尾行会以非零退出，避免永久等待或伪装日志完整。

## 业务回执

1. 校验角色、任务、执行、节点、revision、独立凭据和原业务门禁，持久化 intent。
2. 相同 op_id/参数返回原 accepted receipt；不同参数拒绝。后续写操作被冻结。
3. 只有匹配该操作的受控 PostToolUse 才发送 ACK，证明工具结果已被 Claude 收到。
4. 宿主独立 finalizer 确认两端停止，重新核验文件或交付证明，然后推进原业务状态。
5. 业务转换和 action_receipt 在同一次任务原子更新中提交；重启不会重复交接或完成。

`accepted` 不等于任务完成。ACK 丢失保留待确认状态；停止未知保留容量；未推送或远端祖先证明失败仍不能 finish。
