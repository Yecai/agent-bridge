import asyncio
import base64
import hashlib
import json
import shlex
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from . import hooks
from .bootstrap import deploy
from .claude import build_argv, context_prompt
from .errors import BridgeError
from .redact import redact
from .runtime import Runtime
from .server import serve
from .sessions import JsonLines
from .ssh import connect_target, reverse_mcp
from .storage import write_json


class Runner:
    def __init__(self, spec, on_status=None):
        self.spec = spec
        if not spec.session_id:
            spec.session_id = str(uuid4())
        self.runtime = Runtime(spec)
        self.cancelled = asyncio.Event()
        self.connection, self.remote_python, self.remote_port = None, None, None
        self.on_status = on_status or (lambda row: None)
        self.result, self.remote_status = None, None
        self.remote_cursor = {'stdout': 0, 'stderr': 0}
        self.parser = JsonLines()

    async def remote(self, action, **fields):
        frame = {'action': action, 'execution_id': self.spec.execution_id,
                 'revision': self.spec.control_revision, 'request_id': str(uuid4()), **fields}
        process = await self.connection.create_process(shlex.join([self.remote_python, '-m', 'agentbridge', '_remote']))
        stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(frame)), 30)
        if process.exit_status != 0:
            try:
                code = json.loads(stderr)['error_code']
            except (ValueError, KeyError):
                code = 'remote_control_failed'
            raise BridgeError(code, '远端控制请求失败')
        reply = json.loads(stdout)
        return reply

    async def _configure(self, deployment):
        spec = self.spec
        async with self.connection.start_sftp_client() as sftp:
            home = await sftp.realpath('.')
            root = home + '/.local/share/agentbridge/executions/' + spec.execution_id
            await sftp.makedirs(root, exist_ok=True)
            await sftp.chmod(root, 0o700)
            hook_script = str(Path(hooks.__file__).resolve())
            files = {
                'mcp.json': json.dumps({'mcpServers': {'local': {'type': 'http', 'url': f'http://127.0.0.1:{self.remote_port}/mcp',
                                                                'headers': {'Authorization': 'Bearer ' + self.runtime.token}}}}),
                'hook.json': json.dumps({'url': f'http://127.0.0.1:{self.remote_port}/hooks', 'token': self.runtime.token,
                                         'execution_id': spec.execution_id}),
                'hooks.py': Path(hook_script).read_text(),
                'prompt.txt': context_prompt(spec),
            }
            command = shlex.join([deployment['python'], root + '/hooks.py', root + '/hook.json'])
            files['settings.json'] = json.dumps(hooks.settings(command))
            for name, content in files.items():
                async with sftp.open(root + '/' + name, 'w') as stream:
                    await stream.write(content)
                await sftp.chmod(root + '/' + name, 0o600)
        return root

    def event(self, event):
        event_type = event.get('type', 'unknown')
        if event_type == 'result':
            self.result = event
        self.runtime.audit.append('claude.event', 'error' if event.get('is_error') else 'received',
                                  {'type': event_type}, data=event,
                                  session_id=event.get('session_id', self.spec.session_id))

    async def collect(self):
        row = await self.remote('events', cursor=self.remote_cursor)
        for channel in ('stdout', 'stderr'):
            data = base64.b64decode(row[channel])
            if data:
                refs = self.runtime.artifacts.append('remote:' + channel, data)
                self.runtime.audit.append('claude.output', 'running', {'channel': channel, 'bytes': len(data)}, artifact_refs=refs)
                if channel == 'stdout' and self.spec.mode == 'print':
                    for event in self.parser.feed(data):
                        self.event(event)
        self.remote_cursor = row['next_cursor']
        self.remote_status = row
        return row

    async def run(self):
        runtime, spec = self.runtime, self.spec
        code, error_code, local_stopped, remote_stopped = 1, None, False, False
        try:
            async with serve(runtime) as url:
                async with connect_target(spec.target) as connection:
                    self.connection = connection
                    runtime.audit.append('ssh.connected', 'ok')
                    tunnel = await reverse_mcp(connection, urlsplit(url).port)
                    self.remote_port = tunnel.get_port()
                    deployment = await deploy(connection, runtime)
                    self.remote_python = deployment['python']
                    runtime.audit.append('remote.deployed', 'ok', {'wheel_sha256': deployment['sha256']})
                    remote_root = await self._configure(deployment)
                    row = await self.remote('start', argv=build_argv(spec, remote_root + '/mcp.json', remote_root + '/settings.json'),
                                            cwd=remote_root, mode=spec.mode, prompt=context_prompt(spec), secrets=[runtime.token])
                    self.remote_status = row
                    runtime.audit.append('claude.started', 'running', {'remote_pid': row.get('pid'), 'mode': spec.mode}, session_id=spec.session_id)
                    self.on_status({'state': 'running', 'session_id': spec.session_id, 'stop_confirmed': False})
                    if spec.mode == 'interactive' and spec.prompt:
                        await self.remote('input', data=base64.b64encode((context_prompt(spec) + '\r').encode()).decode())
                    renewal = time.monotonic()
                    try:
                        while not self.cancelled.is_set():
                            row = await self.collect()
                            if row['state'] != 'running':
                                # Drain beyond a single page before publishing completion.
                                if not row['stdout'] and not row['stderr']:
                                    break
                                continue
                            if not runtime.enabled:
                                raise BridgeError('control_lost', '本机工具权限已撤销')
                            if time.monotonic() - renewal >= 10:
                                await self.remote('renew')
                                renewal = time.monotonic()
                            await asyncio.sleep(.2)
                    finally:
                        runtime.revoke()
                        stopped = await self.remote('cancel')
                        remote_stopped = stopped.get('stop_confirmed', False)
                        runtime.audit.append('remote.stopped', 'ok' if remote_stopped else 'stop_unconfirmed',
                                             {'exit_code': stopped.get('exit_code')})
                        await self.collect()
                        tunnel.close()
                        await tunnel.wait_closed()
                    if not remote_stopped:
                        raise BridgeError('stop_unconfirmed', '远端停止未确认')
                    code = 0 if not self.cancelled.is_set() and row.get('exit_code') == 0 else 1
                    if spec.mode == 'print' and not self.result:
                        raise BridgeError('result_missing', 'Claude 未返回最终结构化事件')
                    if self.result and self.result.get('is_error'):
                        code = 1
        except Exception as exc:
            error_code = exc.code if isinstance(exc, BridgeError) else 'bridge_failed'
            runtime.audit.append('execution.error', 'error', {'error_type': type(exc).__name__}, error_code=error_code)
        finally:
            runtime.revoke()
            try:
                await asyncio.to_thread(runtime.processes.stop_all)
                local_stopped = True
            except Exception:
                error_code = 'stop_unconfirmed'
            for channel in ('stdout', 'stderr'):
                runtime.artifacts.finish('remote:' + channel)
            for event in self.parser.finish():
                self.event(event)
            # A failure before any remote spawn needs no remote stop confirmation.
            if self.remote_status is None and error_code not in {'remote_control_failed', 'outcome_unknown'}:
                remote_stopped = True
            stopped = local_stopped and remote_stopped
            data = {key: (self.result or {}).get(key) for key in ('result', 'structured_output', 'usage', 'num_turns')}
            data.update(session_id=spec.session_id, is_error=bool(code or error_code), stop_confirmed=stopped)
            runtime.audit.append('bridge.result', 'ok' if code == 0 and not error_code else 'error', data=data, error_code=error_code)
            final = {'state': 'exited' if stopped else 'stop_unconfirmed', 'exit_code': code,
                     'error_code': error_code, 'session_id': spec.session_id, 'stop_confirmed': stopped}
            self.on_status(final)
            return final
