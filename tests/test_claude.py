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


def test_remote_workspace_is_stable_across_execution_ids(spec):
    from agentbridge.runner import remote_workspace
    first=remote_workspace('/remote/home',spec)
    spec.execution_id=str(uuid4())
    assert remote_workspace('/remote/home',spec)==first
    spec.client_id='different-client'
    assert remote_workspace('/remote/home',spec)!=first


def test_only_configured_schema_can_use_the_output_formatter(spec):
    event={'hook_event_name':'PreToolUse','tool_name':'StructuredOutput','tool_input':{'passed':True}}
    assert decide(event,spec.execution_id)['hookSpecificOutput']['permissionDecision']=='deny'
    assert decide(event,spec.execution_id,allow_structured=True)['hookSpecificOutput']['permissionDecision']=='allow'
    for native in ['Bash','Read','Write','StructuredOutputOther']:
        assert decide({**event,'tool_name':native},spec.execution_id,allow_structured=True)['hookSpecificOutput']['permissionDecision']=='deny'


async def test_interactive_prompt_waits_for_controlled_session_start(spec):
    import base64
    from agentbridge.runner import Runner
    spec.mode='interactive';spec.prompt='initial prompt'
    runner=Runner(spec);runner.remote_cwd='/remote/bridge-workspace'
    inputs=[]
    async def remote(action, **fields):
        inputs.append(base64.b64decode(fields['data']))
        return {}
    runner.remote=remote
    await runner._interactive_startup(b'Accessing workspace: /other/workspace Quick safety check: \xe2\x9d\xaf No, exit Yes, I trust this folder')
    assert not inputs
    await runner._interactive_startup(b'\nAccessing workspace: /remote/bridge-workspace Quick safety check: \xe2\x9d\xaf No, exit Yes, I trust this folder')
    assert inputs==[b'\x1b[B\r']
    await runner._interactive_startup(b'')
    assert len(inputs)==1
    runner.runtime.hook({'hook_event_name':'SessionStart','session_id':'other-session'})
    await runner._interactive_startup(b'')
    assert len(inputs)==1
    runner.runtime.hook({'hook_event_name':'SessionStart','session_id':spec.session_id})
    await runner._interactive_startup(b'')
    assert inputs[-2].endswith(b'initial prompt\x1b[201~')
    assert inputs[-1]==b'\r'
    await runner._interactive_startup(b'')
    assert len(inputs)==3


async def test_interactive_ready_screen_handles_deferred_session_start(spec):
    import base64
    from agentbridge.runner import Runner
    spec.mode='interactive';spec.prompt='a\x1b[201~b'
    runner=Runner(spec);runner.runtime.mcp_ready=True
    inputs=[]
    async def remote(action, **fields):
        inputs.append(base64.b64decode(fields['data']))
    runner.remote=remote
    await runner._interactive_startup(b'Claude Code v2.1.310\r\n\xe2\x9d\xaf Try a prompt')
    assert len(inputs)==2
    assert inputs[0].count(b'\x1b[201~')==1
    assert inputs[0].startswith(b'\x1b[200~')
    assert inputs[-1]==b'\r'


async def test_interactive_startup_never_sends_work_after_cancel_or_revocation(spec):
    from agentbridge.runner import Runner
    spec.mode='interactive';spec.prompt='must-not-send'
    for revoke in [False,True]:
        runner=Runner(spec)
        runner.runtime.session_ready=True
        inputs=[]
        async def remote(action,**fields):inputs.append(action)
        runner.remote=remote
        if revoke:runner.runtime.revoke()
        else:runner.cancelled.set()
        await runner._interactive_startup(b'')
        assert not inputs


def test_argv_always_skips_permission_prompts(spec):
    for mode in ['print', 'interactive']:
        spec.mode = mode
        assert '--dangerously-skip-permissions' in build_argv(spec, 'm', 's')


def test_settings_skip_bypass_confirmation_dialog():
    from agentbridge.hooks import settings
    assert settings('hook-cmd')['skipDangerousModePermissionPrompt'] is True


BYPASS_SCREEN = ('WARNING: Claude Code running in Bypass Permissions mode\r\n'
                 'By proceeding, you accept all responsibility\r\n'
                 '\xe2\x9d\xaf 1. No, exit\r\n  2. Yes, I accept').encode('latin-1')


async def test_interactive_startup_accepts_bypass_dialog_once(spec):
    import base64
    from agentbridge.runner import Runner
    spec.mode = 'interactive'; spec.prompt = 'initial prompt'
    runner = Runner(spec); runner.runtime.mcp_ready = True
    inputs = []
    async def remote(action, **fields):
        inputs.append(base64.b64decode(fields['data']))
    runner.remote = remote
    await runner._interactive_startup(b'Claude Code v2.1.310\r\n' + BYPASS_SCREEN)
    assert inputs == [b'\x1b[B\r']
    await runner._interactive_startup(BYPASS_SCREEN)
    assert inputs == [b'\x1b[B\r']
    await runner._interactive_startup(b'Claude Code v2.1.310\r\n\xe2\x9d\xaf Try a prompt')
    assert inputs[-2].endswith(b'initial prompt\x1b[201~')
    assert inputs[-1] == b'\r'
