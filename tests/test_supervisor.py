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
