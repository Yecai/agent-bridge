import asyncio
import base64
import json
import os
import time

from . import state
from .errors import BridgeError
from .procutil import start_marker
from .runner import Runner
from .storage import write_json
from .supervisor import ControlServer, Lease


async def daemon(spec):
    root = state.execution(spec.execution_id)
    status_path = root / 'status.json'
    row = {'execution_id': spec.execution_id, 'revision': spec.control_revision, 'pid': os.getpid(),
           'start_marker': start_marker(os.getpid()), 'log_dir': str(spec.log_dir), 'state': 'starting', 'stop_confirmed': False}
    write_json(status_path, row)
    lease = Lease()
    lease.renew()

    def updated(values):
        row.update(values)
        write_json(status_path, row)

    try:
        runner = Runner(spec, updated)
    except Exception as error:
        updated({'state': 'exited', 'exit_code': 1, 'stop_confirmed': True,
                 'error_code': error.code if isinstance(error, BridgeError) else 'initialization_failed'})
        return

    async def handle(frame):
        action = frame['action']
        if action == 'renew':
            lease.renew()
            return dict(row)
        if action == 'status':
            return dict(row)
        if action == 'cancel':
            runner.runtime.revoke()
            runner.cancelled.set()
            return dict(row)
        if action == 'events':
            return runner.runtime.audit.read(cursor=frame.get('cursor', 0), limit=200)
        if action == 'terminal':
            output = runner.runtime.artifacts.output('remote:stdout', frame.get('cursor', 0))
            return {'data': base64.b64encode(output['data']).decode(), 'next_cursor': output['next_cursor']}
        if action in {'input', 'resize'} and runner.connection:
            return await runner.remote(action, **{k: frame[k] for k in ('data', 'rows', 'columns') if k in frame})
        raise BridgeError('invalid_action', '未知本机监督动作')

    async with ControlServer(state.address(spec.execution_id), spec.execution_id, spec.control_revision, handle):
        task = asyncio.create_task(runner.run())
        while not task.done():
            if lease.expired():
                runner.runtime.revoke()
                runner.cancelled.set()
            await asyncio.sleep(.25)
        await task
        # Keep the receipt available while the CLI drains final events.
        await asyncio.sleep(3)
