import json
import os
import subprocess
import sys


def test_invalid_cli_request_is_safe(tmp_path):
    request = tmp_path / 'request.json'
    request.write_text('{"schema_version":99,"password":"do-not-echo"}')
    request.chmod(0o600)
    result = subprocess.run([sys.executable, '-m', 'agentbridge', 'run', '--request-file', str(request)], capture_output=True, text=True)
    assert result.returncode != 0
    assert result.stdout == ''
    assert json.loads(result.stderr)['error_code'] == 'invalid_request'
    assert 'do-not-echo' not in result.stderr


def test_request_must_be_private(spec, tmp_path):
    request = tmp_path / 'request.json'
    request.write_text(spec.model_dump_json())
    request.chmod(0o644)
    result = subprocess.run([sys.executable, '-m', 'agentbridge', 'run', '--request-file', str(request)], capture_output=True, text=True)
    assert result.returncode != 0
    assert json.loads(result.stderr)['error_code'] == 'request_not_private'


def test_local_execution_request_identity_is_immutable(spec,tmp_path,monkeypatch):
    import asyncio
    import pytest
    from agentbridge.cli import ensure_request_identity
    from agentbridge.errors import BridgeError
    from agentbridge import state
    monkeypatch.setenv('AGENTBRIDGE_STATE',str(tmp_path/'state'))
    ensure_request_identity(spec)
    ensure_request_identity(spec)
    with pytest.raises(BridgeError,match='execution_conflict'):
        ensure_request_identity(spec.model_copy(update={'prompt':'changed'}))


async def test_local_daemon_keeps_interactive_input_until_remote_ready(spec,tmp_path,monkeypatch):
    import asyncio,base64
    from types import SimpleNamespace
    from agentbridge import local,state
    from agentbridge.supervisor import control
    monkeypatch.setenv('AGENTBRIDGE_STATE',str(tmp_path/'state'))
    spec.mode='interactive'
    ready=asyncio.Event();received=[]
    class FakeRunner:
        def __init__(self,request,updated):
            self.runtime=SimpleNamespace(revoke=lambda:None)
            self.connection=None
            self.cancelled=asyncio.Event();self.updated=updated
        async def remote(self,action,**fields):
            assert ready.is_set()
            received.append((action,fields));return {'state':'running'}
        async def run(self):
            await ready.wait()
            self.connection=True
            self.updated({'state':'running'})
            await self.cancelled.wait()
            self.updated({'state':'exited','stop_confirmed':True})
    monkeypatch.setattr(local,'Runner',FakeRunner)
    running=asyncio.create_task(local.daemon(spec))
    try:
        for _ in range(200):
            if state.address(spec.execution_id).exists():break
            await asyncio.sleep(.01)
        result=await control(state.address(spec.execution_id),spec.execution_id,spec.control_revision,
                             {'action':'input','data':base64.b64encode(b'early-input').decode()})
        assert result['state']=='queued'
        assert not received
        ready.set()
        for _ in range(100):
            if received:break
            await asyncio.sleep(.01)
        assert received[0][0]=='input'
        assert base64.b64decode(received[0][1]['data'])==b'early-input'
        await control(state.address(spec.execution_id),spec.execution_id,spec.control_revision,{'action':'cancel'})
        await asyncio.wait_for(running,5)
    finally:
        ready.set()
        if not running.done():
            running.cancel()
            try:await running
            except asyncio.CancelledError:pass
