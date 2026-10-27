"""Exercise the modern discovery probe followed by Claude's legacy fallback."""
import httpx
import pytest

from conftest import make_spec
from agentbridge.runtime import Runtime
from agentbridge.server import serve


@pytest.mark.parametrize('access', ['read', 'full'])
async def test_modern_probe_falls_back_to_legacy_tools(tmp_path, access):
    runtime = Runtime(make_spec(tmp_path, access))
    async with serve(runtime) as url:
        async with httpx.AsyncClient(trust_env=False) as client:
            headers = {'Accept': 'application/json, text/event-stream',
                       'MCP-Protocol-Version': '2026-07-28'}
            probe = {'jsonrpc': '2.0', 'id': 'discover', 'method': 'server/discover',
                     'params': {'_meta': {'io.modelcontextprotocol/protocolVersion': '2026-07-28'}}}
            denied = await client.post(url, headers=headers, json=probe)
            assert denied.status_code == 401
            headers['Authorization'] = 'Bearer ' + runtime.token
            response = await client.post(url, headers=headers, json=probe)
            # This SDK implements the handshake era. A non-modern 400 tells a
            # dual-era client to initialize rather than claiming modern support.
            assert response.status_code == 400
            assert response.json()['error']['code'] == -32600
            assert not runtime.mcp_ready

            headers['MCP-Protocol-Version'] = '2025-11-25'
            response = await client.post(url, headers=headers, json={
                'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                'params': {'protocolVersion': '2025-11-25', 'capabilities': {},
                           'clientInfo': {'name': 'compat-test', 'version': '1'}}})
            assert response.status_code == 200
            assert response.json()['result']['protocolVersion'] == '2025-11-25'
            assert not runtime.mcp_ready
            response = await client.post(url, headers=headers, json={
                'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            assert response.status_code == 202
            response = await client.post(url, headers=headers, json={
                'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
            assert response.status_code == 200
            names = {tool['name'] for tool in response.json()['result']['tools']}
            assert 'workspace_context' in names
            assert ('exec' in names) == (access == 'full')
            assert runtime.mcp_ready
            response = await client.post(url, headers=headers, json={
                'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                'params': {'name': 'workspace_context', 'arguments': {}}})
            assert response.status_code == 200
            result = response.json()['result']
            assert not result.get('isError')
            assert result['structuredContent']['data']['cwd'] == str(runtime.spec.cwd.resolve())


async def test_process_output_wait_keeps_control_loop_responsive(spec):
    """Waiting for a quiet build must leave SSH control/lease timers running."""
    import asyncio
    import threading
    from types import SimpleNamespace

    from agentbridge.server import build_mcp

    runtime = Runtime(spec)
    process_id = 'quiet-build'
    runtime.processes.rows[process_id] = {
        'row': {'process_id': process_id, 'state': 'running'}, 'done': False,
    }
    # Signal the real blocking wait so the check does not depend on thread startup.
    waiting = threading.Event()
    original_wait = runtime.processes.condition.wait

    def wait(timeout=None):
        waiting.set()
        return original_wait(timeout)

    runtime.processes.condition.wait = wait
    mcp = build_mcp(runtime)
    task = asyncio.create_task(mcp._tool_manager.call_tool(
        'process_output', {'process_id': process_id, 'wait_seconds': 1},
        context=SimpleNamespace(request_id='quiet-output')))
    try:
        while not waiting.is_set():
            await asyncio.sleep(.01)
        # A heartbeat coroutine must get a turn before the blocking wait ends.
        await asyncio.sleep(.02)
        assert not task.done(), 'tool wait blocked SSH control and lease timers'
    finally:
        with runtime.processes.condition:
            runtime.processes.rows[process_id]['done'] = True
            runtime.processes.condition.notify_all()
        await task
    assert runtime.enabled
