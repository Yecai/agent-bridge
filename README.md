# AgentBridge

让 VPS 上的 Claude Code 通过 SSH 回环隧道调用本机 MCP 工具。项目文件、Git、构建、测试在本机执行。

```sh
uv sync --group dev
uv run agentbridge --version
uv run agentbridge doctor --request-file /absolute/path/request.private.json
uv run agentbridge run --request-file /absolute/path/request.private.json
uv run agentbridge status --execution EXECUTION_UUID
uv run agentbridge cancel --execution EXECUTION_UUID
uv run agentbridge logs --execution EXECUTION_UUID --cursor 0
uv run agentbridge export --execution EXECUTION_UUID --output /absolute/path/diagnostics.zip
```

请求格式见 `src/agentbridge/protocol.py` 的 `RunSpec`。请求文件必须属于当前用户，权限 0600；凭据只在本机私有请求和内存中传递，不放进 Git。运行结果与事件使用 JSONL，最终事件为 `bridge.result`。每个 execution_id 只允许启动一次；未知结果不能通过重复运行命令“恢复”。

本机需要 Python 3.11+、uv、Git 和 ripgrep。开发验证使用 Python 3.12。VPS 需要 Python 3.11+、venv 和已配置的 Claude Code；远端监督器只使用标准库，部署至用户私有 venv，不依赖系统 pip/ensurepip，不安装系统包。主机密钥必须已经存在于显式 `known_hosts` 文件，未知或变化会拒绝连接。

MCP 仅绑定回环地址，每次执行有独立 bearer。只读执行仅开放 context/read/list/glob/grep；全权执行再开放写入、补丁和受管命令。全权 shell 具有本机用户权限，文件工具的根目录限制不构成 shell 沙箱。Claude 禁用原生工具及其他 MCP 配置。

诊断目录和控制目录为 0700；事件、附件和账本为私有文件。命令输出按流脱敏并分片，事件只引用完整附件，不丢弃早期输出。已知密码和令牌在持久化前替换；对未知业务秘密无法保证自动识别。诊断不自动删除。

`run` 的短命 CLI 连接独立本机监督器；远端监督器独立拥有 Claude 进程。控制端停止心跳后租约到期回收所属进程。只在两端停止确认后标记 stopped；停止未知必须保留容量。交互模式使用 PTY，结构化事件另行存储。

当前开发及验收进展见 [验证记录](docs/validation-current.md)。完整宿主节点接入、故障恢复及真实模型验收仍在进行。

CLI 参数及 hooks 依据：[Claude CLI](https://code.claude.com/docs/en/cli-reference)、[Claude hooks](https://code.claude.com/docs/en/hooks)。部署前还需检查实际安装版本。
