import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .errors import BridgeError
from .storage import private_dir


def socket_path(path):
    path = Path(path)
    if len(os.fsencode(path)) < 90:
        return path
    root = private_dir(Path('/tmp') / f'agentbridge-{os.getuid()}')
    return root / (hashlib.sha256(os.fsencode(path)).hexdigest()[:32] + '.sock')


@dataclass
class Lease:
    ttl: float = 60.
    now: Callable[[], float] = time.monotonic
    deadline: float = 0.

    def renew(self, *, now=None):
        self.deadline = (self.now() if now is None else now) + self.ttl

    def expired(self, *, now=None):
        return (self.now() if now is None else now) >= self.deadline


class ControlServer:
    def __init__(self, path, execution_id, revision, handler):
        self.path, self.execution_id, self.revision, self.handler = socket_path(path), execution_id, revision, handler

    async def __aenter__(self):
        private_dir(self.path.parent)
        # Never unlink a live supervisor's socket.
        if self.path.exists():
            raise BridgeError('supervisor_exists', '监督器控制地址已存在，拒绝覆盖')
        self.server = await asyncio.start_unix_server(self._client, path=str(self.path), limit=16 * 1024 * 1024)
        self.path.chmod(0o600)
        return self

    async def _client(self, reader, writer):
        try:
            frame = json.loads(await asyncio.wait_for(reader.readline(), 15))
            if frame.get('execution_id') != self.execution_id or frame.get('revision') != self.revision:
                raise BridgeError('control_lost', '执行身份或控制版本不匹配')
            if not isinstance(frame.get('request_id'), str):
                raise BridgeError('invalid_request', '控制请求身份缺失')
            result = await self.handler(frame)
            writer.write(json.dumps({'result': result}, ensure_ascii=False).encode() + b'\n')
        except Exception as exc:
            code = exc.code if isinstance(exc, BridgeError) else 'control_failed'
            writer.write(json.dumps({'error_code': code}).encode() + b'\n')
        finally:
            try:
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

    async def __aexit__(self, *args):
        self.server.close()
        await self.server.wait_closed()
        self.path.unlink(missing_ok=True)


async def control(path, execution_id, revision, frame, timeout=20):
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(socket_path(path)), limit=16 * 1024 * 1024), timeout)
    try:
        writer.write(json.dumps({**frame, 'execution_id': execution_id, 'revision': revision, 'request_id': str(uuid4())}).encode() + b'\n')
        await writer.drain()
        reply = json.loads(await asyncio.wait_for(reader.readline(), timeout))
        if reply.get('error_code'):
            raise BridgeError(reply['error_code'], '监督器拒绝请求')
        return reply['result']
    finally:
        writer.close()
        await writer.wait_closed()
