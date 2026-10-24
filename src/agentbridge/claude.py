"""Claude CLI contract; prompts travel through private files/stdin, never shell."""
import json


def build_argv(spec, mcp_path, settings_path):
    args = [*spec.claude_exe, '--tools', '', '--strict-mcp-config', '--mcp-config', str(mcp_path),
            '--settings', str(settings_path), '--setting-sources', '', '--disable-slash-commands',
            '--allowedTools', 'mcp__local__*', '--dangerously-skip-permissions']
    if spec.session_id:
        args += ['--resume' if spec.resume else '--session-id', spec.session_id]
    if spec.model:
        args += ['--model', spec.model]
    if spec.effort:
        args += ['--effort', spec.effort]
    if spec.mode == 'print':
        args += ['-p', '--verbose', '--output-format', 'stream-json']
        if spec.result_schema:
            args += ['--json-schema', json.dumps(spec.result_schema, ensure_ascii=False)]
    return args


def context_prompt(spec):
    return ('你正在通过 AgentBridge 操作用户本机项目。Claude 进程所在 VPS 不是项目机器。'
            '所有文件、Git、测试、命令只能用 mcp__local__ 工具。先调用 workspace_context，'
            '读取其列出的本机项目规则，按需读取授权的技能。终端路径与操作系统以该工具为准。\n'
            + spec.prompt)
