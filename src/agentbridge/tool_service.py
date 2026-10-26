"""Local tool runtime, independent of SSH/model transports."""
import asyncio
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from jsonschema import validate, ValidationError

from . import state
from .errors import BridgeError
from .protocol import ToolsSpec, ToolMeta
from .procutil import start_marker
from .runtime import Runtime, WRITE_TOOLS
from .storage import write_json, file_lock, atomic_write
from .supervisor import ControlServer, Lease, control
from .tool_results import Results


def load_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 8 * 1024 * 1024:
            raise BridgeError('request_not_private', '请求必须是当前用户的私有普通文件')
        return json.load(stream)


def capabilities():
    return {'schema_version': 1, 'tool_service': {'version': 1, 'operations':
            ['describe', 'start', 'call', 'result', 'freeze', 'renew', 'status', 'cancel']}}


def saved(execution_id):
    path = state.execution(execution_id) / 'status.json'
    if not path.exists():
        raise BridgeError('execution_not_found', '执行尚未登记')
    return json.loads(path.read_text())


async def request(execution_id, action, body=None):
    row = saved(execution_id)
    if row.get('mode') != 'tools':
        raise BridgeError('protocol_mismatch', '执行不是本机工具服务')
    if action == 'result':
        result = Results(state.execution(execution_id) / 'results').lookup(body['op_id'])
        if result is None:
            raise BridgeError('operation_not_found', '未登记此操作')
        if result.get('result') is None:
            raise BridgeError('outcome_unknown', '原操作结果未知')
        return result['result']
    if row.get('stop_confirmed'):
        if action in {'status', 'cancel', 'renew', 'freeze'}:
            return row
        raise BridgeError('control_lost', '执行已经停止')
    try:
        return await control(state.address(execution_id), execution_id, row['revision'],
                             {'action': action, 'body': body or {}}, timeout=25)
    except (OSError, TimeoutError):
        if action == 'status':
            return {**row, 'state': 'stop_unconfirmed', 'stop_confirmed': False}
        raise BridgeError('supervisor_unavailable', '工具监督器不可达，禁止重放') from None


async def start(spec):
    root = state.execution(spec.execution_id)
    raw = spec.model_dump_json()
    fingerprint = hashlib.sha256(raw.encode()).hexdigest()
    with file_lock(root / '.tools-start.lock'):
        path = root / 'request-fingerprint'
        if path.exists() and path.read_text() != fingerprint:
            raise BridgeError('execution_conflict', '执行身份不可更改工作区配置')
        if not path.exists():
            atomic_write(path, fingerprint.encode())
            atomic_write(root / 'tools-request.json', raw.encode())
            child = subprocess.Popen([sys.executable, '-m', 'agentbridge', 'tools', '_daemon',
                                      '--request-file', str(root / 'tools-request.json')],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
            write_json(root / 'starting.json', {'pid': child.pid, 'fingerprint': fingerprint})
    for _ in range(200):
        if state.address(spec.execution_id).exists():
            return await request(spec.execution_id, 'status')
        if (root / 'status.json').exists() and saved(spec.execution_id).get('stop_confirmed'):
            return saved(spec.execution_id)
        await asyncio.sleep(.025)
    raise BridgeError('supervisor_unavailable', '工具监督器启动未确认')


async def daemon(spec):
    root = state.execution(spec.execution_id)
    row = {'schema_version': 1, 'mode': 'tools', 'execution_id': spec.execution_id,
           'revision': spec.control_revision, 'pid': os.getpid(), 'start_marker': start_marker(os.getpid()),
           'state': 'starting', 'frozen': False, 'stop_confirmed': False, 'log_dir': str(spec.log_dir)}
    def update(**changes):
        row.update(changes)
        write_json(root / 'status.json', row)
    update()
    runtime = Runtime(spec)
    results = Results(root / 'results')
    from .tool_catalog import describe_async
    schemas = {item['name']: item['input_schema'] for item in await describe_async(spec.access)}
    lease = Lease()
    lease.renew()
    lock = asyncio.Lock()
    finish = asyncio.Event()

    async def cancel():
        async with lock:
            runtime.revoke()
            update(frozen=True, state='stopping')
        try:
            await asyncio.to_thread(runtime.processes.stop_all)
            update(state='stopped', stop_confirmed=True)
            finish.set()
        except Exception:
            update(state='stop_unconfirmed', stop_confirmed=False)
            raise
        return dict(row)

    async def handle(frame):
        action, body = frame['action'], frame.get('body') or {}
        if action == 'renew':
            lease.renew()
            return dict(row)
        if action == 'status':
            return dict(row)
        if action == 'cancel':
            return await cancel()
        if action == 'freeze':
            async with lock:
                update(frozen=True)
            return dict(row)
        if action != 'call':
            raise BridgeError('invalid_action', '未知工具服务动作')
        if set(body) != {'name', 'arguments', 'event_id', 'op_id', 'control_revision'}:
            raise BridgeError('invalid_request', '工具调用字段不合法')
        name, arguments, op_id = body['name'], body['arguments'], body['op_id']
        if (body['control_revision'] != spec.control_revision or not isinstance(op_id, str)
                or not 1 <= len(op_id) <= 256 or not isinstance(body['event_id'], str)):
            raise BridgeError('control_lost', '工具归属不匹配')
        if name not in schemas or not isinstance(arguments, dict) or 'op_id' in arguments:
            raise BridgeError('tool_not_allowed', '工具或参数未授权')
        try:
            validate(arguments, {**schemas[name], 'additionalProperties': False})
        except ValidationError:
            raise BridgeError('invalid_arguments', '工具参数不符合 schema') from None
        arguments = dict(arguments)
        if name == 'read_file':
            arguments.setdefault('limit', 32768)
        for key, rule in schemas[name].get('properties', {}).items():
            if key != 'op_id' and 'default' in rule:
                arguments.setdefault(key, rule['default'])
        # Keep replies bounded without silently cropping contents or changing mutations.
        if name == 'read_file':
            arguments = {'limit': 32768, **arguments}
            if not 1 <= arguments['limit'] <= 32768:
                raise BridgeError('invalid_arguments', '单次读取上限为32768字节，请使用offset分页')
        if name == 'process_output' and arguments.get('wait_seconds', 1) > 5:
            raise BridgeError('invalid_arguments', '输出单次等待上限为5秒')
        async with lock:
            old = results.lookup(op_id)
            if old:
                reply = results.issue(op_id, name, arguments)
                return reply
            if not runtime.enabled:
                raise BridgeError('control_lost', '工具权限已经撤销')
            if row['frozen'] and name in WRITE_TOOLS - {'process_output', 'process_stop'}:
                raise BridgeError('action_pending', '业务回执等待确认，禁止新增写入')
            results.issue(op_id, name, arguments)
            meta = ToolMeta(execution_id=spec.execution_id, tool_call_id=body['event_id'],
                            op_id=op_id, control_revision=spec.control_revision)
            try:
                reply = await asyncio.to_thread(runtime.call, name, arguments, meta)
            except BridgeError as error:
                if error.code in {'outcome_unknown', 'audit_unavailable'}:
                    update(frozen=True)
                    raise
                reply = {'status': 'error', 'error_code': error.code, 'data': {'message': error.message}}
            reply = {'schema_version': 1, **reply}
            if len(json.dumps(reply, ensure_ascii=False).encode()) > 256 * 1024:
                update(frozen=True)
                raise BridgeError('outcome_unknown', '结果过大，需核对私有日志，禁止重放')
            results.save(op_id, reply)
            return reply

    async with ControlServer(state.address(spec.execution_id), spec.execution_id, spec.control_revision, handle):
        update(state='active')
        while not finish.is_set():
            if lease.expired():
                try:
                    await cancel()
                except Exception:
                    pass
            try:
                await asyncio.wait_for(finish.wait(), timeout=.25)
            except TimeoutError:
                pass
        await asyncio.sleep(.5)


def main(argv):
    import argparse
    parser = argparse.ArgumentParser(prog='agentbridge tools')
    parser.add_argument('action', choices=['describe', 'start', 'call', 'result', 'freeze', 'renew', 'status', 'cancel', '_daemon'])
    parser.add_argument('--execution')
    parser.add_argument('--request-file')
    parser.add_argument('--op-id')
    parser.add_argument('--access', choices=['read', 'full'], default='read')
    args = parser.parse_args(argv)
    if args.action == 'describe':
        from .tool_catalog import describe
        return {'schema_version': 1, 'tools': describe(args.access)}
    if args.action in {'start', '_daemon'}:
        spec = ToolsSpec.model_validate(load_private(args.request_file))
        return asyncio.run(start(spec) if args.action == 'start' else daemon(spec))
    body = load_private(args.request_file) if args.request_file else {'op_id': args.op_id}
    return asyncio.run(request(args.execution, args.action, body))
