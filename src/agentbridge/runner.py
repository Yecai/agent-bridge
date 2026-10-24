import asyncio
import base64
import hashlib
import json
import re
import shlex
import time
from contextlib import AsyncExitStack
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import asyncssh

from . import hooks
from .bootstrap import deploy
from .claude import build_argv, context_prompt
from .errors import BridgeError
from .redact import redact
from .runtime import Runtime
from .server import serve
from .sessions import JsonLines
from .ssh import connect_target, reverse_mcp, verify_reverse_listener
from .storage import write_json


def remote_workspace(home, spec):
    identity = hashlib.sha256(json.dumps([spec.client_id, spec.workspace_id]).encode()).hexdigest()
    return home + '/.local/share/agentbridge/workspaces/' + identity


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
        self.fatal_error = None
        self.remote_start_requested = False
        self.remote_cursor = {'stdout': 0, 'stderr': 0, 'transcript': 0}
        self.parser = JsonLines()
        self.transcript_parser = JsonLines()
        self.transcript_offset = -1
        self.transcript_gap = None
        self.reconnect_lock = asyncio.Lock()
        self.connections, self.tunnel, self.local_port = None, None, None
        self.remote_cwd = None
        self.startup_output = bytearray()
        self.workspace_trusted = False
        self.bypass_accepted = False
        self.initial_prompt_sent = False

    async def _interactive_startup(self, data):
        if self.spec.mode != 'interactive' or self.cancelled.is_set() or not self.runtime.enabled:
            return
        self.startup_output.extend(data)
        del self.startup_output[:-16384]
        text = re.sub(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[78])', '', self.startup_output.decode(errors='replace'))
        compact = ''.join(text.split())
        bypass_dialog = 'BypassPermissionsmode' in compact and 'No,exit' in compact and 'Yes,Iaccept' in compact
        ready_screen = (self.runtime.mcp_ready and 'ClaudeCodev' in compact and '❯' in compact
                        and 'Accessingworkspace:' not in compact and 'BypassPermissionsmode' not in compact)
        if bypass_dialog:
            # Fallback when skipDangerousModePermissionPrompt is ignored: move to "Yes, I accept" once.
            self.startup_output.clear()
            if not self.bypass_accepted:
                self.bypass_accepted = True
                self.runtime.audit.append('claude.bypass_accept', 'ok', {'cwd': self.remote_cwd})
                await self.remote('input', data=base64.b64encode(b'\x1b[B\r').decode())
            return
        if self.runtime.session_ready or ready_screen:
            self.startup_output.clear()
            if self.spec.prompt and not self.initial_prompt_sent:
                self.initial_prompt_sent = True
                prompt = context_prompt(self.spec).replace('\r\n', '\n').replace('\r', '\n')
                prompt = ''.join(f'\\u{ord(ch):04x}' if (ord(ch) < 32 and ch not in '\n\t') or ord(ch) == 127 else ch for ch in prompt)
                await self.remote('input', data=base64.b64encode(b'\x1b[200~' + prompt.encode() + b'\x1b[201~').decode())
                await asyncio.sleep(.2)
                await self.remote('input', data=base64.b64encode(b'\r').decode())
            return
        # This exact private directory was created by _configure; it contains no user checkout.
        expected = 'Accessingworkspace:' + str(self.remote_cwd) + 'Quicksafetycheck:'
        if not self.workspace_trusted and expected in compact and '❯No,exit' in compact and 'Yes,Itrustthisfolder' in compact:
            self.workspace_trusted = True
            self.runtime.audit.append('claude.workspace_trust', 'ok', {'cwd': self.remote_cwd})
            await self.remote('input', data=base64.b64encode(b'\x1b[B\r').decode())
            self.startup_output.clear()

    async def remote(self, action, **fields):
        if action == 'input':
            try:
                value = base64.b64decode(fields['data'], validate=True)
                refs = self.runtime.artifacts.append('local:input', value)
                self.runtime.audit.append('claude.input', 'received', {'bytes': len(value)}, artifact_refs=refs)
            except Exception as error:
                self.runtime.revoke()
                self.cancelled.set()
                raise BridgeError('audit_unavailable', '交互输入无法记录，已撤销执行权限') from error
        frame = {'action': action, 'execution_id': self.spec.execution_id,
                 'revision': self.spec.control_revision, 'request_id': str(uuid4()), **fields}
        failed_connection = self.connection
        try:
            return await self._rpc(frame)
        except (OSError, TimeoutError, asyncssh.Error) as error:
            if action == 'input':
                self.runtime.revoke()
                raise BridgeError('input_outcome_unknown', '交互输入回执丢失，禁止自动重放') from error
            await self._reconnect(failed_connection)
            # start is bound to the same execution and remote fingerprint; no new session.
            return await self._rpc(frame)

    async def _rpc(self, frame):
        process = await self.connection.create_process(shlex.join([self.remote_python, '-m', 'agentbridge', '_remote']))
        stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(frame)), 30)
        if self.connection.is_closed():
            raise OSError('SSH control channel disconnected')
        if process.exit_status != 0:
            try:
                code = json.loads(stderr)['error_code']
            except (ValueError, KeyError):
                code = 'remote_control_failed'
            raise BridgeError(code, '远端控制请求失败')
        reply = json.loads(stdout)
        return reply

    async def _reconnect(self, failed_connection):
        async with self.reconnect_lock:
            if self.connection is not failed_connection:
                return
            ticket = self.runtime.pause_transport()
            self.runtime.audit.append('ssh.disconnected', 'running', {'remote_port': self.remote_port})
            failed_connection.close()
            await failed_connection.wait_closed()
            deadline = time.monotonic() + 40
            while True:
                connection = None
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BridgeError('reconnect_timeout', '短时重连窗口已结束，保留停止未知状态')
                    connection = await asyncio.wait_for(self.connections.enter_async_context(connect_target(self.spec.target)), remaining)
                    self.connection = connection
                    status = await self._rpc({'action': 'status', 'execution_id': self.spec.execution_id,
                                              'revision': self.spec.control_revision, 'request_id': str(uuid4())})
                    if status.get('state') == 'running':
                        await self._rpc({'action': 'renew', 'execution_id': self.spec.execution_id,
                                         'revision': self.spec.control_revision, 'request_id': str(uuid4())})
                    self.tunnel = await reverse_mcp(connection, self.local_port, self.remote_port)
                    await verify_reverse_listener(connection, self.remote_port)
                    if status.get('state') == 'running' and not self.cancelled.is_set():
                        self.runtime.resume_transport(ticket)
                    self.runtime.audit.append('ssh.reconnected', 'ok', {'remote_pid': status.get('pid'), 'remote_port': self.remote_port})
                    return
                except (OSError, TimeoutError, asyncssh.Error, BridgeError) as error:
                    if connection:
                        connection.close()
                        await connection.wait_closed()
                    if isinstance(error, BridgeError) and error.code not in {'ssh_unavailable', 'forwarding_denied'}:
                        raise
                    if time.monotonic() >= deadline:
                        raise BridgeError('reconnect_timeout', '重连失败，等待远端租约停止并保留名额') from error
                    await asyncio.sleep(.5)

    async def _configure(self, deployment):
        spec = self.spec
        async with self.connection.start_sftp_client() as sftp:
            home = await sftp.realpath('.')
            root = home + '/.local/share/agentbridge/executions/' + spec.execution_id
            workspace = remote_workspace(home, spec)
            await sftp.makedirs(root, exist_ok=True)
            await sftp.chmod(root, 0o700)
            await sftp.makedirs(workspace, exist_ok=True)
            await sftp.chmod(workspace, 0o700)
            hook_script = str(Path(hooks.__file__).resolve())
            files = {
                'mcp.json': json.dumps({'mcpServers': {'local': {'type': 'http', 'url': f'http://127.0.0.1:{self.remote_port}/mcp',
                                                                'headers': {'Authorization': 'Bearer ' + self.runtime.token}}}}),
                'hook.json': json.dumps({'url': f'http://127.0.0.1:{self.remote_port}/hooks', 'token': self.runtime.token,
                                         'execution_id': spec.execution_id, 'session_id': spec.session_id,
                                         'session_hint': root + '/session.json',
                                         'allow_structured': spec.mode == 'print' and bool(spec.result_schema)}),
                'hooks.py': Path(hook_script).read_text(),
                'prompt.txt': context_prompt(spec),
            }
            command = shlex.join([deployment['python'], root + '/hooks.py', root + '/hook.json'])
            files['settings.json'] = json.dumps(hooks.settings(command))
            for name, content in files.items():
                async with sftp.open(root + '/' + name, 'w') as stream:
                    await stream.write(content)
                await sftp.chmod(root + '/' + name, 0o600)
        return root, workspace

    def event(self, event):
        event_type = event.get('type', 'unknown')
        authentication_failed = event.get('error_status') in (401, 403, '401', '403')
        if authentication_failed:
            self.fatal_error = BridgeError('auth', 'Claude authentication_failed：请更新 VPS 的认证配置')
            self.cancelled.set()
        if event_type == 'result':
            self.result = event
        self.runtime.audit.append('claude.event', 'error' if event.get('is_error') or event.get('error_status') else 'received',
                                  {'type': event_type}, data=event,
                                  session_id=event.get('session_id', self.spec.session_id))

    async def collect(self):
        row = await self.remote('events', cursor=self.remote_cursor)
        for channel in ('stdout', 'stderr', 'transcript'):
            data = base64.b64decode(row[channel])
            if data:
                refs = self.runtime.artifacts.append('remote:' + channel, data)
                self.runtime.audit.append('claude.output', 'running', {'channel': channel, 'bytes': len(data)}, artifact_refs=refs)
                if channel == 'stdout' and self.spec.mode == 'interactive':
                    await self._interactive_startup(data)
                if channel == 'stdout' and self.spec.mode == 'print':
                    for event in self.parser.feed(data):
                        self.event(event)
                elif channel == 'transcript':
                    for event in self.transcript_parser.feed(data):
                        offset = event.get('offset')
                        if isinstance(offset, int) and offset > self.transcript_offset:
                            status = 'error' if (event.get('data') or {}).get('type') == 'bridge.invalid_transcript' else 'received'
                            self.runtime.audit.append('claude.transcript', status, data=event.get('data'),
                                                      session_id=event.get('session_id'), details={'source_offset': offset})
                            self.transcript_offset = offset
        if row.get('transcript_gap') != self.transcript_gap:
            self.transcript_gap = row.get('transcript_gap')
            if self.transcript_gap:
                self.runtime.audit.append('claude.transcript_gap', 'error', {'reason': self.transcript_gap})
        self.remote_cursor = row['next_cursor']
        self.remote_status = row
        return row

    async def run(self):
        runtime, spec = self.runtime, self.spec
        code, error_code, local_stopped, remote_stopped = 1, None, False, False
        try:
            async with serve(runtime) as url:
                async with AsyncExitStack() as connections:
                    self.connections = connections
                    connection = await connections.enter_async_context(connect_target(spec.target))
                    self.connection = connection
                    runtime.audit.append('ssh.connected', 'ok')
                    tunnel = await reverse_mcp(connection, urlsplit(url).port)
                    self.tunnel, self.local_port = tunnel, urlsplit(url).port
                    self.remote_port = tunnel.get_port()
                    await verify_reverse_listener(connection, self.remote_port)
                    deployment = await deploy(connection, runtime)
                    self.remote_python = deployment['python']
                    runtime.audit.append('remote.deployed', 'ok', {'wheel_sha256': deployment['sha256']})
                    remote_root, remote_cwd = await self._configure(deployment)
                    self.remote_cwd = remote_cwd
                    self.remote_start_requested = True
                    row = await self.remote('start', argv=build_argv(spec, remote_root + '/mcp.json', remote_root + '/settings.json'),
                                            cwd=remote_cwd, mode=spec.mode, prompt=context_prompt(spec), secrets=[runtime.token],
                                            session_id=spec.session_id)
                    self.remote_status = row
                    runtime.audit.append('claude.started', 'running', {'remote_pid': row.get('pid'), 'mode': spec.mode}, session_id=spec.session_id)
                    self.on_status({'state': 'running', 'session_id': spec.session_id, 'stop_confirmed': False})
                    renewal = time.monotonic()
                    try:
                        while not self.cancelled.is_set() and not runtime.requested_stop:
                            await self._interactive_startup(b'')
                            row = await self.collect()
                            if row['state'] != 'running':
                                # Drain beyond a single page before publishing completion.
                                if not row['stdout'] and not row['stderr'] and not row.get('transcript'):
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
                        self.tunnel.close()
                        await self.tunnel.wait_closed()
                    if not remote_stopped:
                        raise BridgeError('stop_unconfirmed', '远端停止未确认')
                    if self.fatal_error:
                        raise self.fatal_error
                    code = 0 if runtime.requested_stop or (not self.cancelled.is_set() and row.get('exit_code') == 0) else 1
                    if spec.mode == 'print' and not self.result and not runtime.requested_stop:
                        raise BridgeError('result_missing', 'Claude 未返回最终结构化事件')
                    if self.result and self.result.get('is_error'):
                        code = 1
                    if spec.mode == 'print' and spec.result_schema and not runtime.requested_stop:
                        if not self.result or self.result.get('structured_output') is None:
                            raise BridgeError('schema_result_missing', 'Claude 未返回要求的结构化结果')
        except asyncio.CancelledError:
            error_code = 'cancelled'
            raise
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
            for channel in ('stdout', 'stderr', 'transcript'):
                runtime.artifacts.finish('remote:' + channel)
            runtime.artifacts.finish('local:input')
            for event in self.parser.finish():
                self.event(event)
            # A failure before any remote spawn needs no remote stop confirmation.
            if not self.remote_start_requested:
                remote_stopped = True
            stopped = local_stopped and remote_stopped
            data = {key: (self.result or {}).get(key) for key in ('result', 'structured_output', 'usage', 'num_turns')}
            if self.fatal_error:
                data['result'] = self.fatal_error.message
            data.update(session_id=spec.session_id, is_error=bool(code or error_code), stop_confirmed=stopped)
            runtime.audit.append('bridge.result', 'ok' if code == 0 and not error_code else 'error', data=data, error_code=error_code)
            final = {'state': 'exited' if stopped else 'stop_unconfirmed', 'exit_code': code,
                     'error_code': error_code, 'session_id': spec.session_id, 'stop_confirmed': stopped}
            self.on_status(final)
        return final
