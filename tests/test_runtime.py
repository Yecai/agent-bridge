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
