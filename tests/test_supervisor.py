import asyncio
import base64
import os
import sys

from agentbridge.supervisor import Lease, ControlServer, control
from agentbridge.remote import RemoteExecution


def test_lease_monotonic():
    lease = Lease(ttl=60, now=lambda: 100.)
    lease.renew()
    assert not lease.expired(now=159.)
    assert lease.expired(now=160.)


async def test_remote_real_process_control_and_dedup(tmp_path):
    remote = RemoteExecution(tmp_path, 'execution', 4, ttl=1)
    request = {'argv': [sys.executable, '-c', 'import time; print("ready",flush=True); time.sleep(30)'],
               'cwd': str(tmp_path), 'mode': 'print', 'prompt': ''}
    first = await remote.handle({'action': 'start', **request})
    assert (await remote.handle({'action': 'start', **request}))['pid'] == first['pid']
    await asyncio.sleep(.1)
    events = await remote.handle({'action': 'events', 'cursor': 0})
    assert b'ready' in base64.b64decode(events['stdout'])
    stopped = await remote.handle({'action': 'cancel'})
    assert stopped['state'] == 'exited'
    assert stopped['stop_confirmed']


async def test_private_control_identity(tmp_path):
    async def handle(frame):
        return {'ok': frame['action']}
    async with ControlServer(tmp_path / 'control.sock', 'id', 2, handle):
        assert await control(tmp_path / 'control.sock', 'id', 2, {'action': 'status'}) == {'ok': 'status'}
        try:
            await control(tmp_path / 'control.sock', 'wrong', 2, {'action': 'cancel'})
        except Exception as exc:
            assert 'control_lost' in str(exc)
        else:
            assert False


async def test_control_stream_reuses_connection_and_checks_every_frame(tmp_path):
    import json
    from agentbridge.supervisor import socket_path
    calls = []

    async def handle(frame):
        calls.append(frame['action'])
        return {'ok': frame['action']}

    async with ControlServer(tmp_path / 'stream.sock', 'id', 2, handle):
        reader, writer = await asyncio.open_unix_connection(str(socket_path(tmp_path / 'stream.sock')))
        try:
            for action, identity in [('status', 'id'), ('renew', 'id'), ('cancel', 'wrong')]:
                writer.write(json.dumps({'execution_id': identity, 'revision': 2,
                                         'request_id': action, 'action': action}).encode() + b'\n')
                await writer.drain()
                reply = json.loads(await asyncio.wait_for(reader.readline(), 1))
                if identity == 'id':
                    assert reply == {'result': {'ok': action}}
                else:
                    assert reply == {'error_code': 'control_lost'}
            assert calls == ['status', 'renew']
        finally:
            writer.close()
            await writer.wait_closed()


async def test_interactive_child_has_controlling_terminal(tmp_path):
    import time
    remote=RemoteExecution(tmp_path,'pty-execution',0,ttl=2)
    script='import os; fd=os.open("/dev/tty",os.O_RDWR); print("tty-ready",flush=True); print(input(),flush=True)'
    await remote.handle({'action':'start','argv':[sys.executable,'-c',script],'cwd':str(tmp_path),'mode':'interactive'})
    try:
        await remote.handle({'action':'input','data':base64.b64encode(b'hello-pty\n').decode()})
        deadline=time.monotonic()+3
        while remote.row['state']=='running' and time.monotonic()<deadline:
            await asyncio.sleep(.02)
        output=await remote.handle({'action':'events','cursor':0})
        assert remote.row.get('exit_code')==0,base64.b64decode(output['stdout'])
        assert b'tty-ready' in base64.b64decode(output['stdout'])
        assert b'hello-pty' in base64.b64decode(output['stdout'])
    finally:
        await remote.handle({'action':'cancel'})
        if remote.master is not None:
            os.close(remote.master)


async def test_same_execution_cannot_change_launch_arguments(tmp_path):
    import pytest
    from agentbridge.errors import BridgeError
    remote=RemoteExecution(tmp_path,'stable',0)
    frame={'action':'start','argv':[sys.executable,'-c','import time;time.sleep(30)'],'cwd':str(tmp_path),'mode':'print','prompt':'one'}
    await remote.handle(frame)
    try:
        with pytest.raises(BridgeError,match='execution_conflict'):
            await remote.handle({**frame,'prompt':'different'})
    finally:
        await remote.handle({'action':'cancel'})


async def test_expired_remote_lease_stops_real_owned_process(tmp_path):
    from agentbridge.remote import serve_remote
    from agentbridge.procutil import group_alive
    frame={'action':'start','argv':[sys.executable,'-c','import time;time.sleep(30)'],'cwd':str(tmp_path),'mode':'print','prompt':''}
    await asyncio.wait_for(serve_remote(tmp_path,'lease-execution',0,tmp_path/'lease.sock',frame,ttl=.2),3)
    import json
    row=json.loads((tmp_path/'status.json').read_text())
    assert row['state']=='exited' and row['stop_confirmed']
    assert not group_alive(row['pid'])


async def test_remote_child_runs_as_sandbox_for_root_bypass(tmp_path):
    remote = RemoteExecution(tmp_path, 'execution', 4, ttl=1)
    script = 'import os; print("sandbox=" + os.environ.get("IS_SANDBOX", ""), flush=True)'
    await remote.handle({'action': 'start', 'argv': [sys.executable, '-c', script],
                         'cwd': str(tmp_path), 'mode': 'print', 'prompt': ''})
    output = b''
    for _ in range(50):
        await asyncio.sleep(.1)
        events = await remote.handle({'action': 'events', 'cursor': 0})
        output = base64.b64decode(events['stdout'])
        if b'sandbox=' in output:
            break
    assert b'sandbox=1' in output
    await remote.handle({'action': 'cancel'})
