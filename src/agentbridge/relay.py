import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

from .errors import BridgeError
from .storage import private_dir
from .supervisor import control, socket_path


def execution_root(execution_id):
    identifier = str(UUID(execution_id))
    root = Path(os.environ.get('AGENTBRIDGE_REMOTE_ROOT', Path.home() / '.local/share/agentbridge/executions'))
    return private_dir(root / identifier)


async def remote_command(frame):
    identifier, revision = str(UUID(frame['execution_id'])), int(frame['revision'])
    root = execution_root(identifier)
    address = socket_path(root / 'control.sock')
    if frame['action'] == 'start' and not address.exists() and not (root / 'status.json').exists():
        child = subprocess.Popen([sys.executable, '-m', 'agentbridge', '_remote-daemon'], stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        child.stdin.write(json.dumps(frame).encode())
        child.stdin.close()
        for _ in range(200):
            if address.exists():
                break
            if child.poll() is not None:
                raise BridgeError('remote_start_failed', '远端监督器未能启动')
            await asyncio.sleep(.025)
    if not address.exists():
        status_path = root / 'status.json'
        if status_path.exists():
            status = json.loads(status_path.read_text())
            if status.get('revision') != revision:
                raise BridgeError('control_lost', '远端控制版本变化')
            if frame['action'] in {'status', 'cancel'}:
                if not status.get('stop_confirmed'):
                    status['state'] = 'stop_unconfirmed'
                return status
            if frame['action'] == 'events':
                from .remote import RemoteExecution
                remote = RemoteExecution(root, identifier, revision)
                remote.row = status
                return await remote.handle(frame)
        raise BridgeError('outcome_unknown', '远端监督器不可达，拒绝重放启动')
    return await control(address, identifier, revision, frame)


async def daemon(frame):
    from .remote import serve_remote
    root = execution_root(frame['execution_id'])
    await serve_remote(root, frame['execution_id'], frame['revision'], root / 'control.sock', frame)
