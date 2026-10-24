import json

import pytest


def test_readonly_denies_mutation_and_records_error(tmp_path):
    from conftest import make_spec
    from agentbridge.runtime import Runtime
    from agentbridge.protocol import ToolMeta
    from agentbridge.errors import BridgeError
    runtime = Runtime(make_spec(tmp_path, "read"))
    meta = ToolMeta(execution_id=runtime.spec.execution_id, tool_call_id="t1", op_id="o1", control_revision=0)
    with pytest.raises(BridgeError, match="tool_not_allowed"):
        runtime.call("exec", {"command": "touch forbidden"}, meta)
    assert not (runtime.spec.cwd / "forbidden").exists()
    assert runtime.audit.read()["events"][-1]["status"] == "error"


def test_runtime_write_idempotent_and_control_revocation(spec):
    from agentbridge.runtime import Runtime
    from agentbridge.protocol import ToolMeta
    from agentbridge.errors import BridgeError
    runtime = Runtime(spec)
    meta = ToolMeta(execution_id=spec.execution_id, tool_call_id="t1", op_id="o1", control_revision=0)
    first = runtime.call("write_file", {"path": "proof.txt", "content": "hello", "create": True}, meta)
    assert runtime.call("write_file", {"path": "proof.txt", "content": "hello", "create": True}, meta) == first
    runtime.revoke()
    with pytest.raises(BridgeError, match="control_lost"):
        runtime.call("read_file", {"path": "proof.txt"}, meta)
    assert "fixture-secret" not in json.dumps(runtime.audit.read())


async def test_real_mcp_auth_discovery_and_call(spec):
    import httpx
    from agentbridge.runtime import Runtime
    from agentbridge.server import serve
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    runtime = Runtime(spec)
    async with serve(runtime) as url:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert response.status_code == 401
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + runtime.token}) as client:
            async with streamable_http_client(url, http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert "exec" in {t.name for t in tools.tools}
                    result = await session.call_tool("workspace_context", {})
                    assert not result.isError
                    assert str(spec.cwd.resolve()) in str(result.content)


def test_duplicate_call_has_terminal_audit(spec):
    from agentbridge.runtime import Runtime
    from agentbridge.protocol import ToolMeta
    runtime = Runtime(spec)
    meta = ToolMeta(execution_id=spec.execution_id, tool_call_id='t', op_id='o', control_revision=0)
    for _ in range(2):
        runtime.call('write_file', {'path': 'a', 'content': 'hi', 'create': True}, meta)
    events = runtime.audit.read()['events']
    assert sum(e['event'] == 'tool.end' for e in events) == 2
    refs = runtime.artifacts.put(b'identical')
    assert runtime.artifacts.put(b'identical') == refs
    assert b''.join(runtime.artifacts.read(r) for r in refs) == b'identical'


def test_audit_failure_revokes_runtime(spec, monkeypatch):
    from agentbridge.runtime import Runtime
    from agentbridge.protocol import ToolMeta
    runtime = Runtime(spec)
    def fail(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(runtime.audit, 'append', fail)
    with pytest.raises(Exception):
        runtime.call('write_file', {'path': 'a', 'content': 'hi', 'create': True},
                     ToolMeta(execution_id=spec.execution_id, tool_call_id='t', op_id='o', control_revision=0))
    assert not runtime.enabled
    assert not (spec.cwd / 'a').exists()


def test_output_storage_failure_revokes_and_stops_command(spec, monkeypatch):
    import sys,time
    from agentbridge.runtime import Runtime
    runtime=Runtime(spec)
    def fail(*args,**kwargs):
        raise OSError('fixture disk full')
    monkeypatch.setattr(runtime.artifacts,'append',fail)
    row=runtime.processes.start([sys.executable,'-c','import time; print("output",flush=True); time.sleep(30)'],str(spec.cwd),'disk-full')
    try:
        deadline=time.monotonic()+3
        while (runtime.enabled or runtime.processes.rows[row['process_id']]['process'].poll() is None) and time.monotonic()<deadline:
            time.sleep(.02)
        assert not runtime.enabled
        assert runtime.processes.rows[row['process_id']]['process'].poll() is not None
    finally:
        runtime.processes.stop_all()


def test_stop_still_kills_owned_group_when_audit_is_unavailable(spec,monkeypatch):
    import sys
    from agentbridge.runtime import Runtime
    from agentbridge.procutil import stop_group
    runtime=Runtime(spec)
    row=runtime.processes.start([sys.executable,'-c','import time;time.sleep(30)'],str(spec.cwd),'stop-disk-full')
    item=runtime.processes.rows[row['process_id']]
    def unavailable(*args,**kwargs):
        raise OSError('fixture disk full')
    monkeypatch.setattr(runtime.audit,'append',unavailable)
    try:
        try:
            runtime.processes.stop_all()
        except Exception:
            pass
        assert item['process'].poll() is not None
        assert not runtime.enabled
    finally:
        stop_group(item['process'],item['row']['start_marker'])


def test_host_action_freezes_mutations_until_controlled_ack(spec, monkeypatch):
    from agentbridge import callbacks
    from agentbridge.runtime import Runtime
    from agentbridge.protocol import ToolMeta
    from agentbridge.errors import BridgeError
    spec.callbacks={'dev_finish':'http://127.0.0.1:8787/internal/agentbridge/actions/dev_finish'}
    runtime=Runtime(spec)
    meta=ToolMeta(execution_id=spec.execution_id,tool_call_id='finish',op_id='finish-op',control_revision=0)
    acked=[]
    monkeypatch.setattr(callbacks,'invoke',lambda *args: {'receipt':'receipt-1','state':'accepted'})
    monkeypatch.setattr(callbacks,'ack',lambda *args: acked.append(args) or {'state':'acked'})
    result=runtime.call('dev_finish',{'summary':'done'},meta)
    assert result['data']['state']=='accepted'
    assert not runtime.requested_stop
    with pytest.raises(BridgeError,match='action_pending'):
        runtime.call('exec',{'command':'touch forbidden'},meta.model_copy(update={'op_id':'another'}))
    assert not (spec.cwd/'forbidden').exists()
    runtime.hook({'hook_event_name':'PreToolUse','tool_name':'mcp__local__dev_finish','tool_input':{'op_id':'finish-op'}})
    assert not acked
    runtime.hook({'hook_event_name':'PostToolUse','tool_name':'mcp__local__dev_finish','tool_input':{'op_id':'wrong'}})
    assert not acked
    runtime.hook({'hook_event_name':'PostToolUse','tool_name':'mcp__local__dev_finish','tool_input':{'op_id':'finish-op'}})
    assert len(acked)==1
    assert runtime.requested_stop
    runtime.hook({'hook_event_name':'PostToolUse','tool_name':'mcp__local__dev_finish','tool_input':{'op_id':'finish-op'}})
    assert len(acked)==1


def test_claude_auth_retry_is_terminal_and_audited_as_error(spec):
    from agentbridge.runner import Runner
    runner=Runner(spec)
    runner.event({'type':'system','subtype':'api_retry','error_status':401,'error':'authentication_failed'})
    assert runner.cancelled.is_set()
    assert runner.fatal_error.code=='auth'
    assert runner.runtime.audit.read()['events'][-1]['status']=='error'
