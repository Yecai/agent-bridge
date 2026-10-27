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
