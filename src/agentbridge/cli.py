import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .errors import BridgeError


def load_request(path):
    from .protocol import RunSpec
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise BridgeError('request_not_private', '运行配置必须属于当前用户并设为 0600')
        try:
            return RunSpec.model_validate_json(stream.read(8 * 1024 * 1024))
        except Exception as exc:
            raise BridgeError('invalid_request', '运行配置不符合协议；字段内容不回显') from exc


def private_request(spec):
    result = spec.model_dump(mode='json')
    if spec.target.password:
        result['target']['password'] = spec.target.password.get_secret_value()
    if spec.callback_token:
        result['callback_token'] = spec.callback_token.get_secret_value()
    return json.dumps(result, ensure_ascii=False)


def ensure_request_identity(spec):
    from . import state
    from .storage import file_lock, atomic_write
    root = state.execution(spec.execution_id)
    fingerprint = hashlib.sha256(json.dumps(json.loads(private_request(spec)), sort_keys=True).encode()).hexdigest()
    with file_lock(root / '.request.lock'):
        path = root / 'request-fingerprint'
        if path.exists() and path.read_text() != fingerprint:
            raise BridgeError('execution_conflict', '同一执行身份不能更改运行配置')
        if not path.exists():
            atomic_write(path, fingerprint.encode())


def saved_status(execution_id):
    from . import state
    path = state.execution(execution_id) / 'status.json'
    if not path.exists():
        raise BridgeError('execution_not_found', '找不到执行记录')
    return json.loads(path.read_text())


async def command(execution_id, action, **fields):
    from . import state
    from .supervisor import control
    saved = saved_status(execution_id)
    if saved.get('stop_confirmed'):
        if action in {'status', 'cancel', 'renew'}:
            return saved
        if action == 'events':
            from .audit import AuditLog
            return AuditLog(Path(saved['log_dir']), {}).read(cursor=fields.get('cursor', 0), limit=200)
        if action == 'terminal':
            from .artifacts import Artifacts
            output = Artifacts(Path(saved['log_dir']) / 'artifacts').output('remote:stdout', fields.get('cursor', 0))
            return {'data': base64.b64encode(output['data']).decode(), 'next_cursor': output['next_cursor']}
    try:
        return await control(state.address(execution_id), execution_id, saved['revision'], {'action': action, **fields})
    except (OSError, TimeoutError):
        if action == 'status':
            return {**saved, 'state': saved['state'] if saved.get('stop_confirmed') else 'stop_unconfirmed'}
        raise BridgeError('supervisor_unavailable', '监督器不可达；执行结果不能重放') from None


class TerminalInput:
    """Keep one input RPC in flight so mouse bursts cannot starve control RPCs."""

    def __init__(self, execution_id):
        self.execution_id = execution_id
        self.pending = bytearray()
        self.dimensions = None
        self.ready = asyncio.Event()
        self.error = None

    def feed(self, data):
        if len(self.pending) + len(data) > 65536:
            self.error = BridgeError('input_queue_full', '交互输入缓冲已满，停止发送以避免丢失输入')
        else:
            self.pending.extend(data)
        self.ready.set()

    def resize(self, rows, columns):
        self.dimensions = (rows, columns)
        self.ready.set()

    async def run(self):
        while True:
            await self.ready.wait()
            self.ready.clear()
            if self.error:
                raise self.error
            if self.pending:
                data = bytes(self.pending)
                self.pending.clear()
                await command(self.execution_id, 'input', data=base64.b64encode(data).decode())
            if self.dimensions:
                rows, columns = self.dimensions
                self.dimensions = None
                await command(self.execution_id, 'resize', rows=rows, columns=columns)


async def run_cli(spec):
    from . import state
    root = state.execution(spec.execution_id)
    ensure_request_identity(spec)
    if not (root / 'status.json').exists():
        from .storage import file_lock
        with file_lock(root / '.start.lock'):
            if not (root / 'starting').exists():
                (root / 'starting').touch(mode=0o600)
                child = subprocess.Popen([sys.executable, '-m', 'agentbridge', '_daemon'], stdin=subprocess.PIPE,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                child.stdin.write(private_request(spec).encode())
                child.stdin.close()
    for _ in range(400):
        if state.address(spec.execution_id).exists():
            break
        if (root / 'status.json').exists() and saved_status(spec.execution_id).get('stop_confirmed'):
            break
        await asyncio.sleep(.025)
    if not (root / 'status.json').exists():
        raise BridgeError('supervisor_unavailable', '本机监督器未能启动')
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    cursor, terminal_cursor, renewal = 0, 0, 0.
    seen_gaps = set()
    interactive = spec.mode == 'interactive'
    old_terminal = None
    input_task = None
    if interactive and sys.stdin.isatty():
        import termios
        import tty
        old_terminal = termios.tcgetattr(sys.stdin)
        tty.setraw(sys.stdin.fileno())
        terminal = TerminalInput(spec.execution_id)
        input_task = asyncio.create_task(terminal.run())

        def input_ready():
            data = os.read(sys.stdin.fileno(), 65536)
            if data:
                terminal.feed(data)
            else:
                stopped.set()
        loop.add_reader(sys.stdin, input_ready)

        def resize():
            size = os.get_terminal_size()
            terminal.resize(size.lines, size.columns)
        loop.add_signal_handler(signal.SIGWINCH, resize)
    try:
        while True:
            if input_task is not None and input_task.done():
                input_task.result()
            if stopped.is_set():
                await command(spec.execution_id, 'cancel')
                stopped.clear()
            if time.monotonic() - renewal >= 10:
                await command(spec.execution_id, 'renew')
                renewal = time.monotonic()
            # Terminal status is persisted after bridge.result. Reading it first
            # prevents completion racing past the final page of audit events.
            status = await command(spec.execution_id, 'status')
            page = await command(spec.execution_id, 'events', cursor=cursor)
            for gap in page.get('gaps', []):
                identity = json.dumps(gap, sort_keys=True)
                if identity not in seen_gaps:
                    seen_gaps.add(identity)
                    if interactive:
                        print('AgentBridge 日志存在缺口，请查看诊断记录。', file=sys.stderr, flush=True)
                    else:
                        print(json.dumps({**spec.public_context(), 'schema_version': 1, 'event': 'log.gap',
                                          'status': 'error', 'details': gap}, ensure_ascii=False), flush=True)
            for event in page['events']:
                if not interactive:
                    print(json.dumps(event, ensure_ascii=False), flush=True)
            cursor = page['next_cursor']
            if interactive:
                output = await command(spec.execution_id, 'terminal', cursor=terminal_cursor)
                sys.stdout.buffer.write(base64.b64decode(output['data']))
                sys.stdout.buffer.flush()
                terminal_cursor = output['next_cursor']
            if status['state'] in {'exited', 'stop_unconfirmed'} and page['complete']:
                return 0 if status.get('exit_code') == 0 and status.get('stop_confirmed') and not seen_gaps else 1
            if status['state'] in {'exited', 'stop_unconfirmed'} and not page['events'] and page.get('gaps'):
                return 1
            await asyncio.sleep(.1)
    finally:
        if input_task is not None:
            input_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, BridgeError):
                await input_task
        if old_terminal is not None:
            import termios
            loop.remove_reader(sys.stdin)
            loop.remove_signal_handler(signal.SIGWINCH)
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_terminal)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def main():
    # Remote actions remain stdlib-only; local dependencies are loaded lazily.
    if len(sys.argv) > 2 and sys.argv[1] == '_guard':
        fd = int(sys.argv[2])
        while os.read(fd, 1):
            pass
        os.close(fd)
        # The watchdog is a member of the exact owned group; it never targets an unrelated PID.
        os.killpg(os.getpgrp(), signal.SIGKILL)
    if len(sys.argv) > 4 and sys.argv[1] == '_process':
        fd = int(sys.argv[2])
        permitted = os.read(fd, 1) == b'1'
        os.close(fd)
        if permitted:
            owner_fd = int(sys.argv[3])
            subprocess.Popen([sys.executable, '-m', 'agentbridge', '_guard', str(owner_fd)], pass_fds=(owner_fd,),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.close(owner_fd)
            os.execvpe(sys.argv[4], sys.argv[4:], os.environ)
        sys.exit(1)
    if len(sys.argv) > 2 and sys.argv[1] == '_pty':
        import fcntl
        import termios
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
    if len(sys.argv) > 1 and sys.argv[1] in {'_remote', '_remote-daemon'}:
        from .relay import remote_command, daemon
        try:
            frame = json.load(sys.stdin)
            result = asyncio.run(daemon(frame) if sys.argv[1] == '_remote-daemon' else remote_command(frame))
            if result is not None:
                print(json.dumps(result))
        except Exception as exc:
            print(json.dumps({'error_code': exc.code if isinstance(exc, BridgeError) else 'remote_failed'}), file=sys.stderr)
            sys.exit(1)
        return
    if len(sys.argv) > 1 and sys.argv[1] in {'capabilities', 'tools'}:
        from . import tool_service
        try:
            result = tool_service.capabilities() if sys.argv[1] == 'capabilities' else tool_service.main(sys.argv[2:])
            if result is not None:
                print(json.dumps(result, ensure_ascii=False))
        except Exception as exc:
            print(json.dumps({'error_code': exc.code if isinstance(exc, BridgeError) else 'bridge_failed',
                              'message': exc.message if isinstance(exc, BridgeError) else '工具服务请求失败'}), file=sys.stderr)
            sys.exit(1)
        return
    parser = argparse.ArgumentParser(prog='agentbridge')
    parser.add_argument('--version', action='version', version=__version__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('run', 'doctor'):
        item = sub.add_parser(name)
        item.add_argument('--request-file', required=True)
        if name == 'doctor':
            item.add_argument('--test-tools', action='store_true', help='显式调用模型，在隔离目录验证 MCP、结构化输出、交互审计及停止')
    item = sub.add_parser('trust-host')
    item.add_argument('--request-file', required=True)
    item.add_argument('--challenge', required=True)
    item.add_argument('--fingerprint', required=True)
    for name in ('status', 'cancel', 'logs', 'export'):
        item = sub.add_parser(name)
        item.add_argument('--execution', required=True)
        if name == 'logs':
            item.add_argument('--cursor', type=int, default=0)
        if name == 'export':
            item.add_argument('--output', required=True)
    sub.add_parser('_daemon')
    args = parser.parse_args()
    try:
        if args.command == '_daemon':
            from .protocol import RunSpec
            from .local import daemon
            asyncio.run(daemon(RunSpec.model_validate_json(sys.stdin.buffer.read())))
            return
        if args.command in {'run', 'doctor', 'trust-host'}:
            spec = load_request(args.request_file)
            if args.command == 'run':
                sys.exit(asyncio.run(run_cli(spec)))
            from .doctor import doctor, trust
            result = trust(spec.target, args.challenge, args.fingerprint) if args.command == 'trust-host' else doctor(spec, test_tools=args.test_tools)
            print(json.dumps(asyncio.run(result), ensure_ascii=False))
        elif args.command in {'status', 'cancel'}:
            print(json.dumps(asyncio.run(command(args.execution, args.command)), ensure_ascii=False))
        else:
            from .audit import AuditLog
            row = saved_status(args.execution)
            if args.command == 'logs':
                print(json.dumps(AuditLog(Path(row['log_dir']), {}).read(args.cursor), ensure_ascii=False))
            else:
                from .diagnostics import export
                print(json.dumps({'path': str(export(Path(row['log_dir']), Path(args.output)))}, ensure_ascii=False))
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        print(json.dumps({'error_code': exc.code if isinstance(exc, BridgeError) else 'bridge_failed',
                          'message': exc.message if isinstance(exc, BridgeError) else '执行失败；请查看本机诊断日志'}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
