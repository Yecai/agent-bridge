"""Claude CLI contract; prompts travel through private files/stdin, never shell."""
import json

from .hooks import SERVER_TOOLS


def build_argv(spec, mcp_path, settings_path):
    # Skills, plugins and memory live on the VPS (user settings); file/shell built-ins stay off.
    args = [*spec.claude_exe, '--tools', ','.join(SERVER_TOOLS), '--strict-mcp-config', '--mcp-config', str(mcp_path),
            '--settings', str(settings_path), '--setting-sources', 'user',
            '--allowedTools', ','.join(['mcp__local__*', *SERVER_TOOLS]), '--dangerously-skip-permissions']
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
            '读取其列出的本机项目规则，按需读取授权的技能。终端路径与操作系统以该工具为准。'
            'VPS 上安装的技能可通过 Skill 工具使用；技能里提到的 Read、Write、Edit、Bash、Grep、Glob 等操作，'
            '一律改用对应的 mcp__local__ 工具完成。\n'
            + spec.prompt)
