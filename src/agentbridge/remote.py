"""Detached remote Claude owner. Only fixed control actions are exposed."""
import asyncio
import base64
import fcntl
import json
import os
import pty
import struct
import subprocess
import termios
import threading
from pathlib import Path

from .errors import BridgeError
from .procutil import group_alive, start_marker, stop_group
from .redact import StreamRedactor
from .storage import private_dir, write_json
from .supervisor import ControlServer, Lease


class RemoteExecution:
    def __init__(self, root, execution_id, revision, ttl=60):
        self.root = private_dir(Path(root))
        self.execution_id, self.revision = execution_id, revision
        self.lease = Lease(ttl=ttl)
        self.lease.renew()
        self.proc, self.master, self.monitor = None, None, None
        self.row = {'execution_id': execution_id, 'revision': revision, 'state': 'ready', 'stop_confirmed': True}
        self.readers = []
        self.failure = None
        self.cancel_lock = asyncio.Lock()

    def _persist(self):
        write_json(self.root / 'status.json', self.row)

    def _reader(self, channel, fd, secrets):
        redactor = StreamRedactor(secrets)
        out = os.open(self.root / (channel + '.bin'), os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(out, 'ab', buffering=0) as stream:
                while True:
                    try:
                        data = os.read(fd, 8192)
                    except OSError as exc:
                        if self.master is not None and exc.errno == 5:  # PTY EOF on Linux
                            break
                        raise
                    if not data:
                        break
                    stream.write(redactor.feed(data))
                    os.fsync(stream.fileno())
                stream.write(redactor.finish())
                os.fsync(stream.fileno())
        except Exception as exc:
            self.failure = type(exc).__name__
        finally:
            if fd != self.master:
                os.close(fd)

    async def _wait(self):
        code = await asyncio.to_thread(self.proc.wait)
        # An exited parent does not prove descendants stopped.
        if group_alive(self.proc.pid):
            await asyncio.to_thread(stop_group, self.proc, self.row['start_marker'])
        for reader in self.readers:
            await asyncio.to_thread(reader.join, 10)
        confirmed = not any(r.is_alive() for r in self.readers) and not group_alive(self.proc.pid)
        self.row.update(state='exited' if confirmed else 'stop_unconfirmed', exit_code=code,
                        stop_confirmed=confirmed, output_error=self.failure)
        self._persist()

    async def handle(self, frame):
        action = frame['action']
        if action == 'start':
            if self.proc is not None:
                return dict(self.row)
            if (self.root / 'status.json').exists():
                raise BridgeError('outcome_unknown', '发现旧执行记录，拒绝重复启动')
            argv = frame['argv']
            if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and '\x00' not in x for x in argv):
                raise BridgeError('invalid_command', '远端启动参数无效')
            env = os.environ.copy()
            env.pop('CLAUDECODE', None)
            if frame['mode'] == 'interactive':
                self.master, slave = pty.openpty()
                self.proc = subprocess.Popen(argv, cwd=frame['cwd'], env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
                os.close(slave)
                streams = [('stdout', self.master)]
            else:
                self.proc = subprocess.Popen(argv, cwd=frame['cwd'], env=env, stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                streams = [('stdout', os.dup(self.proc.stdout.fileno())), ('stderr', os.dup(self.proc.stderr.fileno()))]
                self.proc.stdout.close()
                self.proc.stderr.close()
                self.proc.stdin.write(frame.get('prompt', '').encode())
                self.proc.stdin.close()
            self.row.update(state='running', pid=self.proc.pid, start_marker=start_marker(self.proc.pid), stop_confirmed=False)
            self._persist()
            secrets = [s.encode() for s in frame.get('secrets', [])]
            for channel, fd in streams:
                reader = threading.Thread(target=self._reader, args=(channel, fd, secrets), daemon=True)
                self.readers.append(reader)
                reader.start()
            self.monitor = asyncio.create_task(self._wait())
            return dict(self.row)
        if action == 'status':
            return dict(self.row)
        if action == 'renew':
            self.lease.renew()
            return dict(self.row)
        if action == 'cancel':
            async with self.cancel_lock:
                if self.proc and not self.row.get('stop_confirmed'):
                    await asyncio.to_thread(stop_group, self.proc, self.row['start_marker'])
                    await asyncio.wait_for(asyncio.shield(self.monitor), 15)
                return dict(self.row)
        if action == 'events':
            offsets = frame.get('cursor') or {'stdout': 0, 'stderr': 0}
            result = {'next_cursor': {}, **self.row}
            for channel in ('stdout', 'stderr'):
                path = self.root / (channel + '.bin')
                offset = offsets.get(channel, 0)
                with path.open('rb') if path.exists() else open(os.devnull, 'rb') as stream:
                    stream.seek(offset)
                    data = stream.read(65536)
                result[channel] = base64.b64encode(data).decode()
                result['next_cursor'][channel] = offset + len(data)
            return result
        if action in {'input', 'resize'} and self.master is not None:
            if action == 'input':
                os.write(self.master, base64.b64decode(frame['data'], validate=True))
            else:
                rows, columns = frame['rows'], frame['columns']
                if not (1 <= rows <= 1000 and 1 <= columns <= 1000):
                    raise BridgeError('invalid_range', '终端尺寸无效')
                fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))
            return {'accepted': True}
        raise BridgeError('invalid_action', '未知监督动作')


async def serve_remote(root, execution_id, revision, socket_path, initial=None):
    remote = RemoteExecution(root, execution_id, revision)
    async with ControlServer(socket_path, execution_id, revision, remote.handle):
        try:
            if initial:
                await remote.handle(initial)
            while not remote.lease.expired():
                if remote.failure:
                    break
                await asyncio.sleep(.25)
        finally:
            await remote.handle({'action': 'cancel'})
            if remote.master is not None:
                os.close(remote.master)
