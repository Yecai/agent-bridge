"""停止遗留执行；远端入口只用标准库，不重新部署或启动模型。"""
import asyncio
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4


def marker(pid):
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='],
                            capture_output=True, text=True, timeout=5, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def group_alive(pid):
    result = subprocess.run(['ps', '-Ao', 'pgid=,stat='], capture_output=True,
                            text=True, timeout=5, check=True)
    return any(len(parts := line.split()) >= 2 and parts[0] == str(pid)
               and not parts[1].startswith('Z') for line in result.stdout.splitlines())


def stop_group(row):
    pid = row.get('pid')
    if type(pid) is not int or pid <= 1:
        raise ValueError('invalid_process_identity')
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not group_alive(pid):
            return {'pid': pid, 'group_stopped': True}
        if not row.get('start_marker') or marker(pid) != row['start_marker']:
            raise ValueError('process_identity_changed')
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 3
        while group_alive(pid) and time.monotonic() < deadline:
            time.sleep(.05)
    if group_alive(pid):
        raise ValueError('process_group_still_alive')
    return {'pid': pid, 'group_stopped': True}


def remote_stop(frame):
    """可发送到旧远端的固定程序。变量只由 stdin JSON 传入。"""
    identifier = str(UUID(frame['execution_id']))
    root = Path(os.environ.get('AGENTBRIDGE_REMOTE_ROOT',
                Path.home() / '.local/share/agentbridge/executions')) / identifier
    path = root / 'status.json'
    row = json.loads(path.read_text())
    if row.get('execution_id') != identifier or row.get('revision') != frame['revision']:
        raise ValueError('remote_identity_changed')
    if row.get('stop_confirmed') is True:
        return row
    # A live supervisor remains the owner. Do not race its startup/cleanup.
    address = root / 'control.sock'
    if len(os.fsencode(address)) >= 90:
        import hashlib
        address = Path('/tmp') / f'agentbridge-{os.getuid()}' / (
            hashlib.sha256(os.fsencode(address)).hexdigest()[:32] + '.sock')
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(8)
        try:
            client.connect(str(address))
        except (FileNotFoundError, ConnectionRefusedError):
            return {**row, **stop_group(row), 'state': 'stopped',
                    'stop_confirmed': True, 'stop_evidence': 'recovered_remote_process_group'}
        client.sendall((json.dumps({**frame, 'action': 'cancel', 'request_id': str(uuid4())}) + '\n').encode())
        with client.makefile('rb') as stream:
            reply = json.loads(stream.readline(1024 * 1024))
        if reply.get('error_code'):
            raise ValueError('remote_cancel_rejected')
        result = reply['result']
        if result.get('execution_id') != identifier or result.get('revision') != frame['revision']:
            raise ValueError('remote_identity_changed')
        return result


async def remote_proof(spec):
    from .ssh import connect_target
    # No request content or credentials are interpolated into the shell command.
    source = Path(__file__).read_text()
    async with connect_target(spec.target) as connection:
        process = await connection.create_process('python3 -c ' + shlex.quote(source))
        stdout, _ = await asyncio.wait_for(process.communicate(json.dumps({
            'execution_id': spec.execution_id, 'revision': spec.control_revision})), 25)
        if process.exit_status != 0:
            raise ValueError('remote_stop_unconfirmed')
        row = json.loads(stdout)
        if (row.get('execution_id') != spec.execution_id or row.get('revision') != spec.control_revision
                or row.get('stop_confirmed') is not True):
            raise ValueError('remote_stop_unconfirmed')
        return row


def recover(execution_id, request_path=None):
    from . import state
    from .cli import ensure_request_identity, load_request, saved_status
    from .errors import BridgeError
    from .storage import file_lock, write_json
    root = state.execution(execution_id)
    try:
        with file_lock(root / '.recovery.lock'):
            row = saved_status(execution_id)
            if row.get('stop_confirmed') is True:
                return row
            pid = row.get('pid')
            if type(pid) is not int or pid <= 1 or not row.get('start_marker'):
                raise ValueError('supervisor_identity_missing')
            if marker(pid) == row['start_marker']:
                return None
            if request_path is None:
                request_path = Path((root / 'request-path').read_text())
            spec = load_request(request_path)
            if (spec.execution_id != execution_id or spec.control_revision != row['revision']
                    or row.get('execution_id') != execution_id or str(spec.log_dir) != row['log_dir']):
                raise ValueError('request_identity_changed')
            if not (root / 'request-fingerprint').is_file():
                raise ValueError('request_fingerprint_missing')
            ensure_request_identity(spec)
            remote = asyncio.run(remote_proof(spec))
            groups = []
            for path in sorted((spec.log_dir / 'processes').glob('*.json')):
                process = json.loads(path.read_text())
                if process.get('execution_id') != execution_id:
                    raise ValueError('writer_identity_changed')
                groups.append(stop_group(process))
            # Recheck before publishing proof. Never infer a successful model exit.
            if marker(pid) == row['start_marker']:
                raise ValueError('supervisor_still_alive')
            row.update(state='stopped', stop_confirmed=True,
                       stop_evidence='recovered_local_and_remote_process_groups',
                       recovered_groups=groups, remote_stop_evidence=remote)
            write_json(root / 'status.json', row)
            return row
    except BridgeError:
        raise
    except Exception as error:
        raise BridgeError('stop_unconfirmed', '停止恢复尚未取得两端进程停止证据') from error


if __name__ == '__main__':
    try:
        print(json.dumps(remote_stop(json.load(sys.stdin))))
    except Exception:
        print(json.dumps({'error_code': 'stop_unconfirmed'}), file=sys.stderr)
        sys.exit(1)
