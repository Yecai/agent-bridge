import json
from uuid import uuid4

from agentbridge.claude import build_argv
from agentbridge.hooks import operation_id, decide
from agentbridge.sessions import JsonLines


def test_argv_modes_secrets_and_prompt(spec, tmp_path):
    spec.session_id = str(uuid4())
    spec.prompt = "中文 '$HOME' `echo bad`"
    args = build_argv(spec, tmp_path / 'mcp.json', tmp_path / 'settings.json')
    assert args[args.index('--tools') + 1] == ''
    assert args[args.index('--setting-sources') + 1] == ''
    assert '--strict-mcp-config' in args
    assert '--output-format' in args
    assert spec.prompt not in args
    assert 'fixture-secret' not in ' '.join(args)
    spec.mode = 'interactive'
    assert '--output-format' not in build_argv(spec, 'm', 's')
    spec.resume = True
    assert '--resume' in build_argv(spec, 'm', 's')


def test_hook_uses_trusted_tool_identity(spec):
    event = {'hook_event_name': 'PreToolUse', 'tool_name': 'mcp__local__write_file',
             'tool_use_id': 'use-1', 'tool_input': {'op_id': 'malicious', 'path': 'a'}}
    result = decide(event, spec.execution_id)
    output = result['hookSpecificOutput']
    assert output['updatedInput']['op_id'] == operation_id(spec.execution_id, 'use-1')
    assert output['permissionDecision'] == 'allow'
    assert decide({**event, 'tool_name': 'Bash'}, spec.execution_id)['hookSpecificOutput']['permissionDecision'] == 'deny'


def test_json_lines_retains_partial_and_reports_invalid():
    lines = JsonLines()
    assert lines.feed(b'{"type":"init"}\n{"t') == [{'type': 'init'}]
    assert lines.feed(b'ype":"result"}\nbad\n') == [{'type': 'result'}, {'type': 'bridge.invalid_event', 'raw': 'bad'}]
    assert lines.feed(b'tail') == []
    assert lines.finish() == [{'type': 'bridge.partial_event', 'raw': 'tail'}]
